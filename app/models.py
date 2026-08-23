"""
Backstop schema — invoices, attempts, audit log.

Design notes (see docs/architecture.md for the full L1/L2/L3 picture):

- `Attempt` is written once by L1 (proposal), then updated in place by L2 (gate decision) and
  L3 (execution outcome) — it is the per-attempt working record, and it IS mutated as a row
  moves through the pipeline.
- `AuditLogEntry` is the opposite: append-only, one row per event, never updated or deleted.
  Every decision, veto, execution, and outcome gets its own row here. This table is the actual
  answer to "every money action explainable, bounded and gated" — Attempt is a convenience view,
  AuditLogEntry is the source of truth.
- Idempotency key for L3 is (razorpay_payment_id, attempt_no), enforced with a unique constraint,
  matching build spec §5 ("idempotent call ... Key = (payment_id, attempt_no)").
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums — mirror the build spec's closed taxonomies exactly. The agent may
# never propose or execute a value outside these sets; that's enforced at the
# DB layer too, not just in application code, since this is money movement.
# ---------------------------------------------------------------------------

class FailureClass(str, enum.Enum):
    SOFT_TRANSIENT = "SOFT_TRANSIENT"
    SOFT_FUNDS = "SOFT_FUNDS"
    SOFT_LIMIT = "SOFT_LIMIT"
    SOFT_AUTH = "SOFT_AUTH"
    HARD_INSTRUMENT = "HARD_INSTRUMENT"
    HARD_RISK = "HARD_RISK"
    HARD_MANDATE = "HARD_MANDATE"

    @property
    def is_hard(self) -> bool:
        return self.value.startswith("HARD_")


class InterventionAction(str, enum.Enum):
    RETRY_NOW = "RETRY_NOW"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    REQUEST_INSTRUMENT_UPDATE = "REQUEST_INSTRUMENT_UPDATE"
    NUDGE = "NUDGE"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    STOP_PERMANENT = "STOP_PERMANENT"


class InvoiceKind(str, enum.Enum):
    ONE_TIME = "ONE_TIME"
    RECURRING = "RECURRING"


class InvoiceStatus(str, enum.Enum):
    OPEN = "OPEN"
    RECOVERED = "RECOVERED"
    STOPPED = "STOPPED"          # STOP_PERMANENT fired
    ESCALATED = "ESCALATED"      # ESCALATE_HUMAN fired, awaiting a human


class AttemptOutcome(str, enum.Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"          # vetoed to STOP/ESCALATE before execution


class AuditEventType(str, enum.Enum):
    CLASSIFIED = "CLASSIFIED"          # L1 proposal recorded
    POLICY_PERMITTED = "POLICY_PERMITTED"
    POLICY_VETOED = "POLICY_VETOED"    # L2 downgraded/blocked L1's proposal
    STOPPING_RULE_FIRED = "STOPPING_RULE_FIRED"
    EXECUTED = "EXECUTED"              # L3 call made
    OUTCOME_RECORDED = "OUTCOME_RECORDED"
    CONTACT_SENT = "CONTACT_SENT"
    CIRCUIT_BREAKER_TRIPPED = "CIRCUIT_BREAKER_TRIPPED"
    CIRCUIT_BREAKER_RESET = "CIRCUIT_BREAKER_RESET"


# ---------------------------------------------------------------------------
# Core tables
# ---------------------------------------------------------------------------

class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="customer")
    contacts: Mapped[list["ContactLog"]] = relationship(back_populates="customer")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)

    kind: Mapped[InvoiceKind] = mapped_column(Enum(InvoiceKind))
    amount_paise: Mapped[int] = mapped_column(Integer)   # INR only, stored in paise
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    # Recurring-only fields; null for ONE_TIME.
    mandate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[InvoiceStatus] = mapped_column(Enum(InvoiceStatus), default=InvoiceStatus.OPEN)
    stop_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="invoices")
    attempts: Mapped[list["Attempt"]] = relationship(back_populates="invoice", order_by="Attempt.attempt_no")


class Attempt(Base):
    """
    One row per attempt on an invoice. Written by L1, then updated in place as it moves through
    L2 and L3 — see module docstring. `attempt_no` is 1-indexed and counts toward
    MAX_LIFETIME_ATTEMPTS regardless of which class/action it ended up being.
    """
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("razorpay_payment_id", "attempt_no", name="uq_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)

    # Input to L1.
    issuer: Mapped[str | None] = mapped_column(String(50), nullable=True)
    instrument_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # card/upi/netbanking
    decline_reason_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    decline_reason_raw: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # L1 output — proposal only, not yet permitted.
    proposed_class: Mapped[FailureClass | None] = mapped_column(Enum(FailureClass), nullable=True)
    proposed_action: Mapped[InterventionAction | None] = mapped_column(Enum(InterventionAction), nullable=True)
    proposed_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rationale: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # L2 output — what actually happened to the proposal.
    permitted_action: Mapped[InterventionAction | None] = mapped_column(Enum(InterventionAction), nullable=True)
    vetoed: Mapped[bool] = mapped_column(Boolean, default=False)
    veto_rule: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # L3 output.
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[AttemptOutcome] = mapped_column(Enum(AttemptOutcome), default=AttemptOutcome.PENDING)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    invoice: Mapped["Invoice"] = relationship(back_populates="attempts")


class AuditLogEntry(Base):
    """
    Append-only. Application code must never UPDATE or DELETE a row here — enforce with a DB
    trigger or a read-only role in production; for the buildathon, `app/audit.py` is the only
    file allowed to write to this table, and it exposes no update/delete methods at all.
    """
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.id"), nullable=True, index=True)

    event_type: Mapped[AuditEventType] = mapped_column(Enum(AuditEventType))
    rule_name: Mapped[str | None] = mapped_column(String(100), nullable=True)  # stopping rule / veto rule
    actor: Mapped[str] = mapped_column(String(10))  # "L1" | "L2" | "L3" | "SYSTEM"
    payload: Mapped[dict] = mapped_column(JSON)      # full structured detail for this event

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ContactLog(Base):
    """Backs CONTACT_FREQUENCY_CAP (3 contacts / 30 days, across all invoices for a customer)."""
    __tablename__ = "contact_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"))
    channel: Mapped[str] = mapped_column(String(20))  # sms/email/whatsapp
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    customer: Mapped["Customer"] = relationship(back_populates="contacts")


class IssuerCircuitBreakerState(Base):
    """
    One row per issuer. `stopping_rules.py` / `circuit_breaker.py` read and update this; it is
    the only mutable state the policy gate depends on besides the attempt/contact history, which
    is why it's modeled as a table rather than in-process Redis-only state — every trip and reset
    still needs an AuditLogEntry, and the row here is what a dashboard drill-down reads.
    """
    __tablename__ = "issuer_circuit_breaker_state"

    issuer: Mapped[str] = mapped_column(String(50), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    tripped: Mapped[bool] = mapped_column(Boolean, default=False)
    tripped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
