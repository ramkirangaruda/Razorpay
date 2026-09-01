"""
app/audit.py's contract: append-only, one row per domain event, and every
typed method produces the same payload shape a real caller would get. The
structural test at the bottom is the one that matters most - it is what
turns "no update/delete method" from a comment into something that fails CI
if violated.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.audit import AuditLog, make_engine
from app.classifier import Classification
from app.executor import ExecutionResult
from app.models import (
    AttemptOutcome,
    AuditEventType,
    AuditLogEntry,
    Customer,
    FailureClass,
    Invoice,
    InterventionAction,
    InvoiceKind,
    Attempt as AttemptRow,
)
from app.policy import PolicyDecision

FC = FailureClass
A = InterventionAction

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        yield s


def seed_invoice(session: Session) -> tuple[str, str]:
    customer = Customer(name="Test Customer")
    session.add(customer)
    session.flush()
    invoice = Invoice(customer_id=customer.id, kind=InvoiceKind.ONE_TIME, amount_paise=50_000)
    session.add(invoice)
    session.flush()
    attempt = AttemptRow(invoice_id=invoice.id, attempt_no=1)
    session.add(attempt)
    session.flush()
    return invoice.id, attempt.id


def a_classification(**overrides) -> Classification:
    defaults = dict(
        classification=FC.SOFT_FUNDS,
        classification_confidence="LOW",
        recovery_bucket="HIGH",
        proposed_action=A.RETRY_SCHEDULED,
        rationale="reason='payment_failed' is not in the decision table",
        ambiguity_flags=("unmapped_reason",),
        deferral_days=0,
    )
    defaults.update(overrides)
    return Classification(**defaults)


def a_decision(**overrides) -> PolicyDecision:
    defaults = dict(
        permitted_action=A.RETRY_SCHEDULED,
        permitted_scheduled_for=None,
        vetoed=False,
        downgraded=False,
        rules_fired=(),
        notes=(),
    )
    defaults.update(overrides)
    return PolicyDecision(**defaults)


def an_execution_result(**overrides) -> ExecutionResult:
    defaults = dict(
        outcome=AttemptOutcome.PENDING,
        razorpay_order_id="order_fake123",
        razorpay_payment_id=None,
        error=None,
        executed_at=NOW,
        replayed=False,
    )
    defaults.update(overrides)
    return ExecutionResult(**defaults)


# ---------------------------------------------------------------------------
# L1
# ---------------------------------------------------------------------------


def test_classified_writes_one_l1_row(session):
    invoice_id, attempt_id = seed_invoice(session)
    log = AuditLog(session)

    entry = log.classified(invoice_id, attempt_id, a_classification())

    assert entry.event_type is AuditEventType.CLASSIFIED
    assert entry.actor == "L1"
    assert entry.invoice_id == invoice_id
    assert entry.attempt_id == attempt_id
    assert entry.payload["classification"] == "SOFT_FUNDS"
    assert entry.payload["proposed_action"] == "RETRY_SCHEDULED"
    assert entry.payload["ambiguity_flags"] == ["unmapped_reason"]


# ---------------------------------------------------------------------------
# L2
# ---------------------------------------------------------------------------


def test_policy_decision_with_no_rules_fired_writes_only_the_permitted_row(session):
    invoice_id, attempt_id = seed_invoice(session)
    log = AuditLog(session)

    rows = log.policy_decision(invoice_id, attempt_id, a_decision())

    assert len(rows) == 1
    assert rows[0].event_type is AuditEventType.POLICY_PERMITTED
    assert rows[0].actor == "L2"
    assert rows[0].payload["rules_fired"] == []


def test_policy_decision_writes_one_row_per_fired_rule_plus_the_outcome_row(session):
    invoice_id, attempt_id = seed_invoice(session)
    log = AuditLog(session)
    decision = a_decision(
        permitted_action=A.STOP_PERMANENT,
        vetoed=True,
        downgraded=True,
        rules_fired=("QUIET_HOURS", "MAX_LIFETIME_ATTEMPTS"),
        notes=("outside 09:00-21:00 IST", "4 attempts already made"),
    )

    rows = log.policy_decision(invoice_id, attempt_id, decision)

    assert len(rows) == 3  # 2 rules + 1 outcome
    rule_rows = rows[:2]
    assert [r.event_type for r in rule_rows] == [AuditEventType.STOPPING_RULE_FIRED] * 2
    assert [r.rule_name for r in rule_rows] == ["QUIET_HOURS", "MAX_LIFETIME_ATTEMPTS"]
    assert rule_rows[0].payload["basis"] == "REGULATORY"
    assert rule_rows[1].payload["basis"] == "BACKSTOP"
    assert rule_rows[0].payload["note"] == "outside 09:00-21:00 IST"


def test_policy_decision_vetoed_uses_the_vetoed_event_type_not_permitted(session):
    invoice_id, attempt_id = seed_invoice(session)
    log = AuditLog(session)
    decision = a_decision(permitted_action=A.STOP_PERMANENT, vetoed=True, downgraded=True,
                           rules_fired=("HARD_DECLINE_NO_RETRY",), notes=("hard decline",))

    rows = log.policy_decision(invoice_id, attempt_id, decision)

    outcome_row = rows[-1]
    assert outcome_row.event_type is AuditEventType.POLICY_VETOED


def test_policy_decision_permitted_scheduled_for_is_serialised(session):
    invoice_id, attempt_id = seed_invoice(session)
    log = AuditLog(session)
    scheduled = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    decision = a_decision(downgraded=True, permitted_scheduled_for=scheduled)

    rows = log.policy_decision(invoice_id, attempt_id, decision)

    assert rows[0].payload["permitted_scheduled_for"] == scheduled.isoformat()


# ---------------------------------------------------------------------------
# L3
# ---------------------------------------------------------------------------


def test_executed_writes_l3_row_with_razorpay_ids(session):
    invoice_id, attempt_id = seed_invoice(session)
    log = AuditLog(session)

    entry = log.executed(invoice_id, attempt_id, an_execution_result())

    assert entry.event_type is AuditEventType.EXECUTED
    assert entry.actor == "L3"
    assert entry.payload["razorpay_order_id"] == "order_fake123"
    assert entry.payload["outcome"] == "PENDING"


def test_executed_preserves_a_real_error_object_shape(session):
    invoice_id, attempt_id = seed_invoice(session)
    log = AuditLog(session)
    result = an_execution_result(
        outcome=AttemptOutcome.FAILED,
        razorpay_order_id=None,
        error={
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "Payment failed",
            "error_source": "gateway",
            "error_step": "payment_authorization",
            "error_reason": "payment_failed",
        },
    )

    entry = log.executed(invoice_id, attempt_id, result)

    assert entry.payload["error"]["error_reason"] == "payment_failed"


def test_outcome_recorded_writes_system_row(session):
    invoice_id, attempt_id = seed_invoice(session)
    log = AuditLog(session)

    entry = log.outcome_recorded(invoice_id, attempt_id, AttemptOutcome.SUCCEEDED, {"via": "webhook"})

    assert entry.event_type is AuditEventType.OUTCOME_RECORDED
    assert entry.actor == "SYSTEM"
    assert entry.payload == {"outcome": "SUCCEEDED", "via": "webhook"}


# ---------------------------------------------------------------------------
# Contact / circuit breaker
# ---------------------------------------------------------------------------


def test_contact_sent_allows_a_null_attempt_id(session):
    invoice_id, _ = seed_invoice(session)
    log = AuditLog(session)

    entry = log.contact_sent(invoice_id, None, "sms")

    assert entry.attempt_id is None
    assert entry.payload["channel"] == "sms"


def test_circuit_breaker_tripped_and_reset_share_the_rule_name(session):
    invoice_id, attempt_id = seed_invoice(session)
    log = AuditLog(session)

    tripped = log.circuit_breaker_tripped(invoice_id, attempt_id, "HDFC", {"fail_count": 5})
    reset = log.circuit_breaker_reset(invoice_id, attempt_id, "HDFC")

    assert tripped.event_type is AuditEventType.CIRCUIT_BREAKER_TRIPPED
    assert reset.event_type is AuditEventType.CIRCUIT_BREAKER_RESET
    assert tripped.rule_name == reset.rule_name == "ISSUER_CIRCUIT_BREAKER"
    assert tripped.payload["fail_count"] == 5


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------


def test_entries_for_invoice_returns_only_that_invoices_rows_in_order(session):
    invoice_a, attempt_a = seed_invoice(session)
    invoice_b, attempt_b = seed_invoice(session)
    log = AuditLog(session)

    log.classified(invoice_a, attempt_a, a_classification())
    log.classified(invoice_b, attempt_b, a_classification())
    log.executed(invoice_a, attempt_a, an_execution_result())

    rows = log.entries_for_invoice(invoice_a)

    assert [r.invoice_id for r in rows] == [invoice_a, invoice_a]
    assert [r.event_type for r in rows] == [AuditEventType.CLASSIFIED, AuditEventType.EXECUTED]


# ---------------------------------------------------------------------------
# The two guarantees the whole module exists for
# ---------------------------------------------------------------------------


def test_append_only_no_update_or_delete_method_exists_on_the_class():
    """Not a convention - a fact about the class checked by name. A future
    change that adds `update_entry` or `delete_entry` fails this test."""
    forbidden = {"update", "delete", "remove", "clear"}
    for name in dir(AuditLog):
        if name.startswith("_"):
            continue
        lowered = name.lower()
        assert not any(word in lowered for word in forbidden), (
            f"AuditLog.{name} looks like a mutation method - audit_log is append-only"
        )


def test_append_never_commits_the_session():
    """AuditLog.flush()es so callers see entry.id/created_at immediately, but
    committing is the caller's job - proven here by rolling back and finding
    nothing persisted."""
    engine = make_engine()
    with Session(engine) as session:
        invoice_id, attempt_id = seed_invoice(session)
        session.commit()  # commit the seed data only

        log = AuditLog(session)
        log.classified(invoice_id, attempt_id, a_classification())
        session.rollback()

        remaining = session.query(AuditLogEntry).filter_by(invoice_id=invoice_id).all()
        assert remaining == []
