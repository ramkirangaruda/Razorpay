"""
The one real end-to-end trace docs/HANDOFF.md Section 5.2 asks for: a genuine
Razorpay test-mode failure, run through the actual L1 -> L2b -> L2a pipeline,
with L2a's permitted action then actually executed against Razorpay for real
when it is executable - not a fabricated payload anywhere in the chain.

sim/data/live_failure_capture.json is attempt 1: a real order, checked out
through Razorpay's real test-mode checkout.js with a documented failing card,
declined for real (see that file's "provenance" block for exactly how). This
script treats that decline as what just happened, classifies it, scores the
candidates, gates the result, and - if RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET are
set - executes attempt 2 for real too, so both ends of the trace are genuine
Razorpay responses rather than one real failure glued to a simulated recovery.

Every stage also writes through app/audit.AuditLog, against a fresh in-memory
SQLite DB (this is a one-shot script, not a long-lived server - there is
nothing to share across runs). The resulting rows are printed at the end, so
the trace this script produces has a real audit trail behind it, not just
stdout - the same discipline app/api.py's console applies per decision.

Usage:
    python -m sim.demo_live_trace
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.audit import AuditLog, make_engine
from app.classifier import LookupClassifier
from app.executor import EXECUTABLE_ACTIONS, RazorpayExecutor
from app.models import Attempt as AttemptRow
from app.models import Customer, FailureClass, Invoice, InvoiceKind
from app.policy import L1Proposal, evaluate
from app.rule_basis import basis_of
from app.scorer import Beliefs, ScoreContext, score
from app.stopping_rules import PolicyContext
from sqlalchemy.orm import Session

CAPTURE_PATH = "sim/data/live_failure_capture.json"


def _load():
    with open(CAPTURE_PATH) as f:
        data = json.load(f)
    return data["provenance"], data["case"]


def main() -> None:
    provenance, case = _load()
    captured_at = datetime.fromisoformat(provenance["captured_at_utc"])

    print("=" * 78)
    print("ATTEMPT 1 - real, already executed against Razorpay test mode")
    print("=" * 78)
    print(f"razorpay_order_id:   {provenance['razorpay_order_id']}")
    print(f"razorpay_payment_id: {provenance['razorpay_payment_id']}")
    print(f"error:               {case['error']}")
    print(f"(see {CAPTURE_PATH} for full provenance - how this was captured,")
    print(" and the note on documented vs. live behaviour not matching)")

    # --- L1 --------------------------------------------------------------
    # attempts=1 because the real decline above WAS attempt 1; this call asks
    # what Backstop would do next, given that it just happened for real.
    classification = LookupClassifier().classify(case, {"attempts": 1, "contacts": 0})
    print("\n" + "=" * 78)
    print("L1 - classification (app/classifier.LookupClassifier)")
    print("=" * 78)
    print(f"classification:  {classification.classification.value}")
    print(f"confidence:      {classification.classification_confidence}")
    print(f"recovery_bucket: {classification.recovery_bucket}")
    print(f"proposed_action: {classification.proposed_action.value}")
    print(f"ambiguity_flags: {classification.ambiguity_flags}")
    print(f"rationale:       {classification.rationale}")

    # --- L2b ---------------------------------------------------------------
    beliefs = Beliefs.from_constants()
    sctx = ScoreContext(
        invoice_value_inr=case["amount_paise"] / 100.0,
        recovery_bucket=classification.recovery_bucket,
        failure_class=classification.classification,
        attempt_no=2,
        contacts_so_far=0,
        days_since_last_contact=None,
        now=captured_at,
        is_recurring=case["kind"] == "RECURRING",
        mastercard_advice_code=case.get("mastercard_advice_code"),
    )
    result = score(classification.proposed_action, sctx, beliefs)
    print("\n" + "=" * 78)
    print("L2b - expected value (app/scorer.score)")
    print("=" * 78)
    for s in result.scores:
        marker = " <- chosen" if s.action is result.chosen else ""
        print(f"{s.action.value:26s} EV={s.ev:9.2f}{marker}")
    best = result.best
    if best is not None:
        for label, value in best.as_terms():
            print(f"    {label:30s} {value:9.2f}")

    # --- L2a ---------------------------------------------------------------
    proposal = L1Proposal(
        failure_class=classification.classification,
        proposed_action=result.chosen,
        proposed_scheduled_for=None,
        rationale=classification.rationale,
    )
    ctx = PolicyContext(
        now_utc=captured_at,
        failure_class=classification.classification,
        attempts_so_far=1,
        last_attempt_at_utc=captured_at,
        fast_retries_used=0,
        customer_contacts_in_window=0,
        issuer_breaker_tripped=False,
        issuer_breaker_reset_eta_utc=None,
        is_recurring=case["kind"] == "RECURRING",
        mastercard_advice_code=case.get("mastercard_advice_code"),
    )
    decision = evaluate(proposal, ctx)
    print("\n" + "=" * 78)
    print("L2a - policy gate (app/policy.evaluate)")
    print("=" * 78)
    print(f"permitted_action: {decision.permitted_action.value}")
    print(f"vetoed:           {decision.vetoed}")
    print(f"downgraded:       {decision.downgraded}")
    if decision.rules_fired:
        for rule in decision.rules_fired:
            print(f"  fired: {rule} ({basis_of(rule)})")
    else:
        print("  no rule fired")

    # --- L3 (attempt 2) -----------------------------------------------------
    print("\n" + "=" * 78)
    print("L3 - attempt 2 (app/executor.RazorpayExecutor)")
    print("=" * 78)
    exec_result = None
    if decision.permitted_action not in EXECUTABLE_ACTIONS:
        print(
            f"{decision.permitted_action.value} never reaches L3 - Backstop's "
            "decision after a real failure is to stop or escalate rather than "
            "call Razorpay again. No further API call is made."
        )
    else:
        try:
            executor = RazorpayExecutor()
            exec_result = executor.execute(
                invoice_id=provenance["razorpay_order_id"],
                attempt_no=2,
                action=decision.permitted_action,
                amount_paise=case["amount_paise"],
            )
            print(f"outcome:             {exec_result.outcome.value}")
            print(f"razorpay_order_id:   {exec_result.razorpay_order_id}")
            print(f"error:               {exec_result.error}")
            print(
                "\nBoth attempt 1 (the decline above) and attempt 2 (this call) are "
                "real Razorpay test-mode API responses."
            )
        except RuntimeError as e:
            # RAZORPAY_KEY_ID/SECRET not set - see app/executor.py.
            print(f"Not executed live: {e}")
        except Exception as e:  # noqa: BLE001 - a one-shot demo script, not
            # a test: any live failure (an exhausted quota, a rate limit, a
            # genuine API error) should still let L1/L2b/L2a's real work reach
            # the audit trail below, not crash the script uninformatively.
            print(f"L3 call failed live ({type(e).__name__}): {e}")

    # --- Audit trail ---------------------------------------------------------
    # A fresh in-memory DB per run - this is a one-shot script, not a server,
    # so there is no prior state to preserve and nothing to share across runs.
    engine = make_engine()
    with Session(engine) as audit_session:
        customer = Customer(name="demo_live_trace")
        audit_session.add(customer)
        audit_session.flush()
        invoice_row = Invoice(
            customer_id=customer.id,
            kind=InvoiceKind(case["kind"]),
            amount_paise=case["amount_paise"],
        )
        audit_session.add(invoice_row)
        audit_session.flush()
        attempt_row = AttemptRow(invoice_id=invoice_row.id, attempt_no=2)
        audit_session.add(attempt_row)
        audit_session.flush()

        audit_log = AuditLog(audit_session)
        audit_log.classified(invoice_row.id, attempt_row.id, classification)
        audit_log.policy_decision(invoice_row.id, attempt_row.id, decision)
        if exec_result is not None:
            audit_log.executed(invoice_row.id, attempt_row.id, exec_result)
        audit_session.commit()

        print("\n" + "=" * 78)
        print("Audit trail (app/audit.AuditLog) - what this run actually wrote")
        print("=" * 78)
        for entry in audit_log.entries_for_invoice(invoice_row.id):
            rule = f" ({entry.rule_name})" if entry.rule_name else ""
            print(f"{entry.actor:6s} {entry.event_type.value}{rule}")


if __name__ == "__main__":
    main()
