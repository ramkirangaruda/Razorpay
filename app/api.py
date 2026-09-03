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

Every decision also writes real rows through app/audit.AuditLog, against a
shared in-memory SQLite DB that lives for the process's lifetime - a real
Customer/Invoice/Attempt is created per decision (AuditLogEntry's FKs need
something real to point at, matching the schema's own contract) and
GET /api/audit/{invoice_id} reads them back. This is a single-process demo
tool: each request opens its own short-lived Session against one shared
StaticPool-backed engine (the standard, safe pattern for a shared in-memory
SQLite DB under a threaded server), not a claim this scales past one person
clicking through cases at a time.

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
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.audit import AuditLog
from app.classifier import CachedLLMClassifier, LookupClassifier
from app.executor import EXECUTABLE_ACTIONS, FakeExecutor, RazorpayExecutor
from app.models import Attempt as AttemptRow
from app.models import Base, Customer, Invoice, InvoiceKind
from app.policy import L1Proposal, evaluate
from app.rule_basis import basis_of
from app.scorer import Beliefs, ScoreContext, score
from app.stopping_rules import PolicyContext
from sim.generate_batch import generate_batch

N, SEED = 120, 42

app = FastAPI(title="Backstop console")

_beliefs = Beliefs.from_constants()

# Absolute, not CachedLLMClassifier's relative default ("sim/data/...") - that
# path resolves against the SERVER PROCESS's cwd, not the repo root, and
# silently degrades to an empty _records dict (FileNotFoundError is caught
# internally) rather than raising. Under `uvicorn --app-dir Razorpay ...` from
# a different cwd, this made every "model" request in the console silently
# replay as the table with no error anywhere - same class of bug as
# STATIC_DIR below, caught the same way: by actually running it, not by
# reading the code. REPO_ROOT is app/api.py's own grandparent, not the
# process's cwd, so this is correct regardless of how/where the server starts.
REPO_ROOT = Path(__file__).resolve().parent.parent
_classifiers = {
    "table": LookupClassifier(),
    "model": CachedLLMClassifier(path=str(REPO_ROOT / "sim/data/l1_classifications_seed42.json")),
}
_cases: dict[str, dict] | None = None

# One shared in-memory DB for the process's life. StaticPool + check_same_thread
# is what makes a single in-memory SQLite DB safe to open a fresh short-lived
# Session against from any request thread - the usual footgun (each new
# connection to sqlite:///:memory: is normally its OWN empty DB) is exactly
# what StaticPool avoids by reusing one underlying connection.
_audit_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
Base.metadata.create_all(_audit_engine)


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

    exec_result = None
    l3 = None
    l3_note = None
    if decision.permitted_action in EXECUTABLE_ACTIONS:
        executor = RazorpayExecutor() if req.execute_live else FakeExecutor()
        try:
            exec_result = executor.execute(
                invoice_id=case["invoice_id"],
                attempt_no=req.attempts_so_far + 1,
                action=decision.permitted_action,
                amount_paise=case["amount_paise"],
            )
            l3 = {
                "live": req.execute_live,
                "outcome": exec_result.outcome.value,
                "razorpay_order_id": exec_result.razorpay_order_id,
                "razorpay_payment_id": exec_result.razorpay_payment_id,
                "error": exec_result.error,
                "replayed": exec_result.replayed,
            }
        except RuntimeError as e:
            l3_note = str(e)  # RAZORPAY_KEY_ID/SECRET not set - see app/executor.py
    else:
        l3_note = (
            f"{decision.permitted_action.value} never reaches L3 - it moves no money "
            "and asks Razorpay for nothing."
        )

    # A short-lived Session per request, against the one shared engine - see
    # the module docstring for why this is the safe pattern here. Commits as
    # one unit at the end, matching AuditLog's own documented contract: a
    # crash mid-request should not leave a committed audit row describing a
    # decision whose Attempt update never landed.
    with Session(_audit_engine) as audit_session:
        customer = Customer(name=f"console demo ({case['case_id']})")
        audit_session.add(customer)
        audit_session.flush()
        invoice_row = Invoice(
            customer_id=customer.id,
            kind=InvoiceKind(case["kind"]),
            amount_paise=case["amount_paise"],
        )
        audit_session.add(invoice_row)
        audit_session.flush()
        attempt_row = AttemptRow(invoice_id=invoice_row.id, attempt_no=req.attempts_so_far + 1)
        audit_session.add(attempt_row)
        audit_session.flush()

        audit_log = AuditLog(audit_session)
        audit_log.classified(invoice_row.id, attempt_row.id, classification)
        audit_log.policy_decision(invoice_row.id, attempt_row.id, decision)
        if exec_result is not None:
            audit_log.executed(invoice_row.id, attempt_row.id, exec_result)
        audit_session.commit()
        invoice_id = invoice_row.id

    return {
        "invoice_id": invoice_id,
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

@app.get("/api/audit/{invoice_id}")
def get_audit(invoice_id: str):
    """The real, append-only trail app/audit.AuditLog wrote for one decision -
    proof this console is not just displaying numbers but actually recording
    them the way production would have to."""
    with Session(_audit_engine) as audit_session:
        entries = AuditLog(audit_session).entries_for_invoice(invoice_id)
        return [
            {
                "event_type": e.event_type.value,
                "actor": e.actor,
                "rule_name": e.rule_name,
                "payload": e.payload,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ]


# Absolute, not "app/static" - a relative path resolves against the process's
# cwd, not this file's location, and breaks depending on how/where uvicorn
# was launched from (this bit a first run under --app-dir).
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
