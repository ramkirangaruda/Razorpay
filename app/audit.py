"""
L4 - the append-only audit log writer. The actual answer to "every money
action explainable, bounded and gated" (docs/architecture.md): `AuditLogEntry`
in app/models.py is the source of truth, `Attempt` is a mutable convenience
view, and this file is the only one permitted to write to `audit_log` - it
exposes no update or delete method at all, not "discouraged", not present.

AuditLog cannot enforce that at runtime - there is nothing to check an
argument against. tests/test_audit.py asserts it structurally instead (no
method name on the class contains "update" or "delete"), so the guarantee
cannot regress the way an unenforced convention could.

Two design choices worth stating:

- AuditLog takes a caller-owned SQLAlchemy Session rather than creating its
  own engine or committing on the caller's behalf. Every append flushes
  immediately (so entry.id/created_at are populated for the caller) but never
  commits - an attempt's classify/score/gate/execute cycle should commit as
  one unit, so a crash mid-cycle does not leave a committed audit row
  describing a decision whose corresponding Attempt update never landed.
  make_engine() below is a convenience for tests and demos only; production
  wiring owns its own engine and session scope.
- The typed methods (classified, policy_decision, executed, ...) take the
  actual domain object - Classification, PolicyDecision, ExecutionResult -
  rather than a hand-assembled payload dict, so every call site produces the
  same payload shape for the same event type. A dict built fresh at each call
  site is exactly how a payload schema drifts silently across a codebase.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.classifier import Classification
from app.executor import ExecutionResult
from app.models import AttemptOutcome, AuditEventType, AuditLogEntry, Base
from app.policy import PolicyDecision
from app.rule_basis import basis_of


def make_engine(url: str = "sqlite:///:memory:"):
    """Convenience for tests and demos - not production wiring. This file only
    needs a Session; it has no opinion about where the engine comes from."""
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine


class AuditLog:
    """Append-only. See module docstring."""

    def __init__(self, session: Session):
        self._session = session

    def _append(
        self,
        *,
        invoice_id: str,
        attempt_id: str | None,
        event_type: AuditEventType,
        actor: str,
        payload: dict,
        rule_name: str | None = None,
    ) -> AuditLogEntry:
        entry = AuditLogEntry(
            invoice_id=invoice_id,
            attempt_id=attempt_id,
            event_type=event_type,
            rule_name=rule_name,
            actor=actor,
            payload=payload,
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    # --- L1 ------------------------------------------------------------------

    def classified(
        self, invoice_id: str, attempt_id: str, classification: Classification
    ) -> AuditLogEntry:
        return self._append(
            invoice_id=invoice_id,
            attempt_id=attempt_id,
            event_type=AuditEventType.CLASSIFIED,
            actor="L1",
            payload={
                "classification": classification.classification.value,
                "classification_confidence": classification.classification_confidence,
                "recovery_bucket": classification.recovery_bucket,
                "proposed_action": classification.proposed_action.value,
                "rationale": classification.rationale,
                "ambiguity_flags": list(classification.ambiguity_flags),
                "deferral_days": classification.deferral_days,
            },
        )

    # --- L2 --------------------------------------------------------------------

    def policy_decision(
        self, invoice_id: str, attempt_id: str, decision: PolicyDecision
    ) -> list[AuditLogEntry]:
        """
        One row per fired rule (STOPPING_RULE_FIRED, each tagged with its
        REGULATORY/BACKSTOP basis from app/rule_basis.py) plus one
        POLICY_PERMITTED or POLICY_VETOED row for the outcome - matching
        architecture.md's "every veto or downgrade is logged with the rule
        name" and keeping the veto-rate-by-basis metric readable straight off
        this table rather than requiring a join against stopping_rules.py.
        """
        rows = [
            self._append(
                invoice_id=invoice_id,
                attempt_id=attempt_id,
                event_type=AuditEventType.STOPPING_RULE_FIRED,
                actor="L2",
                rule_name=rule_name,
                payload={"note": note, "basis": basis_of(rule_name)},
            )
            for rule_name, note in zip(decision.rules_fired, decision.notes)
        ]
        rows.append(
            self._append(
                invoice_id=invoice_id,
                attempt_id=attempt_id,
                event_type=(
                    AuditEventType.POLICY_VETOED
                    if decision.vetoed
                    else AuditEventType.POLICY_PERMITTED
                ),
                actor="L2",
                payload={
                    "permitted_action": decision.permitted_action.value,
                    "permitted_scheduled_for": (
                        decision.permitted_scheduled_for.isoformat()
                        if decision.permitted_scheduled_for
                        else None
                    ),
                    "vetoed": decision.vetoed,
                    "downgraded": decision.downgraded,
                    "rules_fired": list(decision.rules_fired),
                },
            )
        )
        return rows

    # --- L3 ----------------------------------------------------------------

    def executed(
        self, invoice_id: str, attempt_id: str, result: ExecutionResult
    ) -> AuditLogEntry:
        return self._append(
            invoice_id=invoice_id,
            attempt_id=attempt_id,
            event_type=AuditEventType.EXECUTED,
            actor="L3",
            payload={
                "outcome": result.outcome.value,
                "razorpay_order_id": result.razorpay_order_id,
                "razorpay_payment_id": result.razorpay_payment_id,
                "error": result.error,
                "executed_at": result.executed_at.isoformat(),
                "replayed": result.replayed,
            },
        )

    def outcome_recorded(
        self,
        invoice_id: str,
        attempt_id: str,
        outcome: AttemptOutcome,
        detail: dict | None = None,
    ) -> AuditLogEntry:
        """
        For an outcome that resolves LATER than the EXECUTED call that
        produced it - e.g. a webhook confirming a PENDING order eventually
        captured or failed. Not wired to a webhook handler (none exists), but
        the event type and row shape are ready for one.
        """
        return self._append(
            invoice_id=invoice_id,
            attempt_id=attempt_id,
            event_type=AuditEventType.OUTCOME_RECORDED,
            actor="SYSTEM",
            payload={"outcome": outcome.value, **(detail or {})},
        )

    # --- contact / circuit breaker --------------------------------------------

    def contact_sent(
        self,
        invoice_id: str,
        attempt_id: str | None,
        channel: str,
        detail: dict | None = None,
    ) -> AuditLogEntry:
        return self._append(
            invoice_id=invoice_id,
            attempt_id=attempt_id,
            event_type=AuditEventType.CONTACT_SENT,
            actor="SYSTEM",
            payload={"channel": channel, **(detail or {})},
        )

    def circuit_breaker_tripped(
        self,
        invoice_id: str,
        attempt_id: str | None,
        issuer: str,
        detail: dict | None = None,
    ) -> AuditLogEntry:
        return self._append(
            invoice_id=invoice_id,
            attempt_id=attempt_id,
            event_type=AuditEventType.CIRCUIT_BREAKER_TRIPPED,
            actor="SYSTEM",
            rule_name="ISSUER_CIRCUIT_BREAKER",
            payload={"issuer": issuer, **(detail or {})},
        )

    def circuit_breaker_reset(
        self,
        invoice_id: str,
        attempt_id: str | None,
        issuer: str,
        detail: dict | None = None,
    ) -> AuditLogEntry:
        return self._append(
            invoice_id=invoice_id,
            attempt_id=attempt_id,
            event_type=AuditEventType.CIRCUIT_BREAKER_RESET,
            actor="SYSTEM",
            rule_name="ISSUER_CIRCUIT_BREAKER",
            payload={"issuer": issuer, **(detail or {})},
        )

    # --- read --------------------------------------------------------------

    def entries_for_invoice(self, invoice_id: str) -> list[AuditLogEntry]:
        """Read-only - not a mutation, so it does not weaken the append-only
        guarantee. Oldest first, matching how the events actually happened."""
        return (
            self._session.query(AuditLogEntry)
            .filter(AuditLogEntry.invoice_id == invoice_id)
            .order_by(AuditLogEntry.created_at)
            .all()
        )
