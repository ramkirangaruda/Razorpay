"""
The live decision console. FastAPI was already a stated dependency
(requirements.txt) but unused until now - this is the first thing that
actually needs it.

Every decision this serves runs through the real classify() -> score() ->
evaluate() chain sim/run_arms.py and sim/demo_live_trace.py already use.
Nothing is reimplemented in JavaScript; the frontend (app/static/) only
renders what this endpoint returns. L3 defaults to FakeExecutor so clicking
around the console does not spend real Razorpay quota or create real orders
on every click - RazorpayExecutor is opt-in per request, same posture as
sim/demo_live_trace.py.

"now" is the real wall clock, not a frozen timestamp - so QUIET_HOURS and
the RBI e-mandate window behave exactly as they would in production. Running
this console late at night in IST is itself a demonstration of the policy
gate working, not a quirk to route around.

Usage:
    uvicorn app.api:app --reload
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.classifier import CachedLLMClassifier, LookupClassifier
from app.executor import EXECUTABLE_ACTIONS, FakeExecutor, RazorpayExecutor
from app.policy import L1Proposal, evaluate
from app.rule_basis import basis_of
from app.scorer import Beliefs, ScoreContext, score
from app.stopping_rules import PolicyContext
from sim.generate_batch import generate_batch

N, SEED = 120, 42

app = FastAPI(title="Backstop console")

_beliefs = Beliefs.from_constants()
_classifiers = {"table": LookupClassifier(), "model": CachedLLMClassifier()}
_cases: dict[str, dict] | None = None


def _batch() -> dict[str, dict]:
    global _cases
    if _cases is None:
        _cases = {c.case_id: asdict(c) for c in generate_batch(N, SEED)}
    return _cases


class DecideRequest(BaseModel):
    case_id: str
    classifier: str = "table"  # "table" | "model"
    attempts_so_far: int = 0
    contacts_so_far: int = 0
    execute_live: bool = False


def _case_summary(c: dict) -> dict:
    return {
        "case_id": c["case_id"],
        "bucket": c["bucket"],
        "kind": c["kind"],
        "amount_inr": c["amount_paise"] / 100.0,
        "issuer": c["issuer"],
        "instrument_type": c["instrument_type"],
        "error": c["error"],
        "ambiguity": c["ambiguity"],
    }


@app.get("/api/cases")
def list_cases():
    """One row per case, not the full payload - enough to pick one without
    shipping the whole 120-case batch (including ground truth) to the
    browser before a case is even selected."""
    return [_case_summary(c) for c in _batch().values()]


@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    case = _batch().get(case_id)
    if case is None:
        raise HTTPException(404, f"unknown case_id {case_id!r}")
    return {**_case_summary(case), "customer": case["customer"], "ground_truth": case["ground_truth"]}


@app.post("/api/decide")
def decide(req: DecideRequest):
    if req.classifier not in _classifiers:
        raise HTTPException(400, f"classifier must be 'table' or 'model', got {req.classifier!r}")
    case = _batch().get(req.case_id)
    if case is None:
        raise HTTPException(404, f"unknown case_id {req.case_id!r}")

    clf = _classifiers[req.classifier]
    state = {"attempts": req.attempts_so_far, "contacts": req.contacts_so_far}
    classification = clf.classify(case, state)

    now = datetime.now(timezone.utc)
    sctx = ScoreContext(
        invoice_value_inr=case["amount_paise"] / 100.0,
        recovery_bucket=classification.recovery_bucket,
        failure_class=classification.classification,
        attempt_no=req.attempts_so_far + 1,
        contacts_so_far=req.contacts_so_far,
        days_since_last_contact=None,
        now=now,
        is_recurring=case["kind"] == "RECURRING",
        mastercard_advice_code=case.get("mastercard_advice_code"),
    )
    scored = score(classification.proposed_action, sctx, _beliefs)

    proposal = L1Proposal(
        failure_class=classification.classification,
        proposed_action=scored.chosen,
        proposed_scheduled_for=None,
        rationale=classification.rationale,
    )
    ctx = PolicyContext(
        now_utc=now,
        failure_class=classification.classification,
        attempts_so_far=req.attempts_so_far,
        last_attempt_at_utc=None,
        fast_retries_used=0,
        customer_contacts_in_window=req.contacts_so_far,
        issuer_breaker_tripped=False,
        issuer_breaker_reset_eta_utc=None,
        is_recurring=case["kind"] == "RECURRING",
        mastercard_advice_code=case.get("mastercard_advice_code"),
    )
    decision = evaluate(proposal, ctx)

    l3 = None
    l3_note = None
    if decision.permitted_action in EXECUTABLE_ACTIONS:
        executor = RazorpayExecutor() if req.execute_live else FakeExecutor()
        try:
            result = executor.execute(
                invoice_id=case["invoice_id"],
                attempt_no=req.attempts_so_far + 1,
                action=decision.permitted_action,
                amount_paise=case["amount_paise"],
            )
            l3 = {
                "live": req.execute_live,
                "outcome": result.outcome.value,
                "razorpay_order_id": result.razorpay_order_id,
                "razorpay_payment_id": result.razorpay_payment_id,
                "error": result.error,
                "replayed": result.replayed,
            }
        except RuntimeError as e:
            l3_note = str(e)  # RAZORPAY_KEY_ID/SECRET not set - see app/executor.py
    else:
        l3_note = (
            f"{decision.permitted_action.value} never reaches L3 - it moves no money "
            "and asks Razorpay for nothing."
        )

    return {
        "case": _case_summary(case),
        "l1": {
            "classifier": req.classifier,
            "classification": classification.classification.value,
            "confidence": classification.classification_confidence,
            "recovery_bucket": classification.recovery_bucket,
            "proposed_action": classification.proposed_action.value,
            "ambiguity_flags": list(classification.ambiguity_flags),
            "rationale": classification.rationale,
        },
        "l2b": {
            "chosen": scored.chosen.value,
            "downgraded": scored.downgraded,
            "scores": [
                {
                    "action": s.action.value,
                    "ev": s.ev,
                    "terms": s.as_terms(),
                }
                for s in scored.scores
            ],
        },
        "l2a": {
            "permitted_action": decision.permitted_action.value,
            "vetoed": decision.vetoed,
            "downgraded": decision.downgraded,
            "rules_fired": [
                {"rule": rule, "basis": basis_of(rule), "note": note}
                for rule, note in zip(decision.rules_fired, decision.notes)
            ],
        },
        "l3": l3,
        "l3_note": l3_note,
    }



# Absolute, not "app/static" - a relative path resolves against the process's
# cwd, not this file's location, and breaks depending on how/where uvicorn
# was launched from (this bit a first run under --app-dir).
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
