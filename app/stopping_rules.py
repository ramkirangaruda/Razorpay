"""
Stopping rules — build spec §6. Each rule is a named, individually testable predicate with zero
I/O: it takes an explicit, already-assembled context (no DB session, no clock, no network) and
returns a RuleOutcome. app/policy.py is the only caller; it owns the precedence order these rules
are applied in and turns their outcomes into a single PolicyDecision.

Keeping this file I/O-free is the whole point of "the component that decides whether money moves
is a pure function with full branch coverage" (build spec §10) — every rule here is a plain
function of its inputs, so tests/test_policy.py can hit every branch without a database, a clock,
or a mock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import ClassVar

from app.models import FailureClass, InterventionAction

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Named bounds — build spec §6 table, verbatim.
# ---------------------------------------------------------------------------

MAX_LIFETIME_ATTEMPTS = 4

STANDARD_MIN_ATTEMPT_INTERVAL = timedelta(hours=24)
FAST_RETRY_INTERVAL = timedelta(minutes=30)
MAX_FAST_RETRIES = 2  # cap on how many times the SOFT_TRANSIENT exception may be used per invoice

CONTACT_FREQUENCY_CAP = 3
CONTACT_WINDOW = timedelta(days=30)

QUIET_HOURS_START_IST = 21  # 21:00 IST
QUIET_HOURS_END_IST = 9     # 09:00 IST — window wraps midnight

CONTACT_ACTIONS = frozenset({InterventionAction.NUDGE, InterventionAction.REQUEST_INSTRUMENT_UPDATE})
RETRY_ACTIONS = frozenset({InterventionAction.RETRY_NOW, InterventionAction.RETRY_SCHEDULED})


class RuleName(str, Enum):
    HARD_DECLINE_NO_RETRY = "HARD_DECLINE_NO_RETRY"
    MAX_LIFETIME_ATTEMPTS = "MAX_LIFETIME_ATTEMPTS"
    MIN_ATTEMPT_INTERVAL = "MIN_ATTEMPT_INTERVAL"
    CONTACT_FREQUENCY_CAP = "CONTACT_FREQUENCY_CAP"
    QUIET_HOURS = "QUIET_HOURS"
    ISSUER_CIRCUIT_BREAKER = "ISSUER_CIRCUIT_BREAKER"


@dataclass(frozen=True)
class PolicyContext:
    """
    Everything a rule needs to evaluate one proposal, gathered ahead of time by whatever caller
    owns the I/O (app/policy.py's caller, in production; a test fixture, in tests). No field here
    is ever fetched by a rule itself.
    """
    now_utc: datetime                       # evaluation instant, timezone-aware UTC
    failure_class: FailureClass
    attempts_so_far: int                    # count of prior attempts on this invoice (0-indexed proposal is attempt N = attempts_so_far + 1)
    last_attempt_at_utc: datetime | None
    fast_retries_used: int                  # prior SOFT_TRANSIENT fast retries already spent on this invoice
    customer_contacts_in_window: int        # contacts to this customer, across all their invoices, in the trailing 30 days
    issuer_breaker_tripped: bool
    issuer_breaker_reset_eta_utc: datetime | None

    @property
    def now_ist(self) -> datetime:
        return self.now_utc.astimezone(IST)


@dataclass(frozen=True)
class RuleOutcome:
    fired: bool
    rule: RuleName | None = None
    # When fired, the rule proposes a replacement action/timing. `forced_action=None` means "only
    # the timing changes, action type is unchanged" — policy.py fills in the actual action.
    forced_action: InterventionAction | None = None
    forced_scheduled_for: datetime | None = None
    note: str = ""

    NOT_FIRED: ClassVar["RuleOutcome"]  # set below; not a dataclass field


RuleOutcome.NOT_FIRED = RuleOutcome(fired=False)


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------

def hard_decline_no_retry(
    ctx: PolicyContext, proposed_action: InterventionAction, proposed_scheduled_for: datetime | None
) -> RuleOutcome:
    """Inviolable: zero retries on HARD_*, and HARD_RISK additionally forbids NUDGE (build spec §3:
    'flag and escalate, never retry, never nudge'). Not a tuned parameter — this rule has no
    threshold to sweep and no config to adjust."""
    if not ctx.failure_class.is_hard:
        return RuleOutcome.NOT_FIRED

    if proposed_action in RETRY_ACTIONS:
        if ctx.failure_class == FailureClass.HARD_RISK:
            target = InterventionAction.ESCALATE_HUMAN
        else:
            # HARD_INSTRUMENT / HARD_MANDATE: the correct move is a remediation link, not silence.
            target = InterventionAction.REQUEST_INSTRUMENT_UPDATE
        return RuleOutcome(
            fired=True, rule=RuleName.HARD_DECLINE_NO_RETRY, forced_action=target,
            note=f"{ctx.failure_class.value} may never be retried; redirected to {target.value}.",
        )

    if ctx.failure_class == FailureClass.HARD_RISK and proposed_action == InterventionAction.NUDGE:
        return RuleOutcome(
            fired=True, rule=RuleName.HARD_DECLINE_NO_RETRY, forced_action=InterventionAction.ESCALATE_HUMAN,
            note="HARD_RISK may never be nudged; redirected to ESCALATE_HUMAN.",
        )

    return RuleOutcome.NOT_FIRED


def max_lifetime_attempts(
    ctx: PolicyContext, proposed_action: InterventionAction, proposed_scheduled_for: datetime | None
) -> RuleOutcome:
    """MAX_LIFETIME_ATTEMPTS = 4 per invoice. Blocks a 5th retry; a human should decide what
    happens next, so the downgrade target is ESCALATE_HUMAN rather than a silent STOP."""
    if proposed_action not in RETRY_ACTIONS:
        return RuleOutcome.NOT_FIRED
    if ctx.attempts_so_far < MAX_LIFETIME_ATTEMPTS:
        return RuleOutcome.NOT_FIRED
    return RuleOutcome(
        fired=True, rule=RuleName.MAX_LIFETIME_ATTEMPTS, forced_action=InterventionAction.ESCALATE_HUMAN,
        note=f"{ctx.attempts_so_far} attempts already made (limit {MAX_LIFETIME_ATTEMPTS}); escalating.",
    )


def _required_interval(ctx: PolicyContext) -> timedelta:
    if ctx.failure_class == FailureClass.SOFT_TRANSIENT and ctx.fast_retries_used < MAX_FAST_RETRIES:
        return FAST_RETRY_INTERVAL
    return STANDARD_MIN_ATTEMPT_INTERVAL


def min_attempt_interval(
    ctx: PolicyContext, proposed_action: InterventionAction, proposed_scheduled_for: datetime | None
) -> RuleOutcome:
    """24h between attempts, except SOFT_TRANSIENT gets a 30-minute window for up to
    MAX_FAST_RETRIES retries before falling back to the standard interval."""
    if proposed_action not in RETRY_ACTIONS or ctx.last_attempt_at_utc is None:
        return RuleOutcome.NOT_FIRED

    required = _required_interval(ctx)
    earliest_allowed = ctx.last_attempt_at_utc + required

    effective_time = proposed_scheduled_for if proposed_action == InterventionAction.RETRY_SCHEDULED else ctx.now_utc
    if effective_time is None:
        effective_time = ctx.now_utc

    if effective_time >= earliest_allowed:
        return RuleOutcome.NOT_FIRED

    return RuleOutcome(
        fired=True, rule=RuleName.MIN_ATTEMPT_INTERVAL,
        forced_action=InterventionAction.RETRY_SCHEDULED, forced_scheduled_for=earliest_allowed,
        note=(
            f"Next attempt not allowed before {earliest_allowed.isoformat()} "
            f"(interval={required}, fast_retries_used={ctx.fast_retries_used})."
        ),
    )


def contact_frequency_cap(
    ctx: PolicyContext, proposed_action: InterventionAction, proposed_scheduled_for: datetime | None
) -> RuleOutcome:
    """3 customer contacts / 30 days, across all of that customer's invoices. Applies to both
    NUDGE and REQUEST_INSTRUMENT_UPDATE — both are a message landing in the customer's inbox."""
    if proposed_action not in CONTACT_ACTIONS:
        return RuleOutcome.NOT_FIRED
    if ctx.customer_contacts_in_window < CONTACT_FREQUENCY_CAP:
        return RuleOutcome.NOT_FIRED
    return RuleOutcome(
        fired=True, rule=RuleName.CONTACT_FREQUENCY_CAP, forced_action=InterventionAction.ESCALATE_HUMAN,
        note=(
            f"{ctx.customer_contacts_in_window} contacts already sent in the trailing "
            f"{CONTACT_WINDOW.days} days (cap {CONTACT_FREQUENCY_CAP}); escalating instead of "
            "sending another."
        ),
    )


def _next_quiet_hours_end(now_ist: datetime) -> datetime:
    candidate = now_ist.replace(hour=QUIET_HOURS_END_IST, minute=0, second=0, microsecond=0)
    if candidate <= now_ist:
        candidate += timedelta(days=1)
    return candidate


def _in_quiet_hours(now_ist: datetime) -> bool:
    hour = now_ist.hour
    return hour >= QUIET_HOURS_START_IST or hour < QUIET_HOURS_END_IST


def quiet_hours(
    ctx: PolicyContext, proposed_action: InterventionAction, proposed_scheduled_for: datetime | None
) -> RuleOutcome:
    """No customer contact 21:00-09:00 IST. Defers the same action to the next 09:00 IST rather
    than replacing it — the contact still happens, just not at 2am."""
    if proposed_action not in CONTACT_ACTIONS:
        return RuleOutcome.NOT_FIRED
    if not _in_quiet_hours(ctx.now_ist):
        return RuleOutcome.NOT_FIRED

    next_ok_ist = _next_quiet_hours_end(ctx.now_ist)
    next_ok_utc = next_ok_ist.astimezone(timezone.utc)
    return RuleOutcome(
        fired=True, rule=RuleName.QUIET_HOURS,
        forced_action=None,  # action type unchanged, per RuleOutcome's documented contract
        forced_scheduled_for=next_ok_utc,
        note=f"{ctx.now_ist.strftime('%H:%M')} IST is within quiet hours; deferred to {next_ok_ist.isoformat()}.",
    )


def issuer_circuit_breaker(
    ctx: PolicyContext, proposed_action: InterventionAction, proposed_scheduled_for: datetime | None
) -> RuleOutcome:
    """Pauses retries to an issuer whose rolling success rate has dropped below threshold (see
    app/circuit_breaker.py for how ctx.issuer_breaker_tripped gets computed). This is the direct
    transplant from the flash-sale admission-control finding: a crowd of retries that keep failing
    degrades the success rate of the retries that would otherwise have worked."""
    if proposed_action not in RETRY_ACTIONS or not ctx.issuer_breaker_tripped:
        return RuleOutcome.NOT_FIRED

    reset_eta = ctx.issuer_breaker_reset_eta_utc or (ctx.now_utc + timedelta(minutes=30))
    if proposed_action == InterventionAction.RETRY_SCHEDULED and proposed_scheduled_for and proposed_scheduled_for >= reset_eta:
        return RuleOutcome.NOT_FIRED  # already scheduled past the breaker's expected reset

    return RuleOutcome(
        fired=True, rule=RuleName.ISSUER_CIRCUIT_BREAKER,
        forced_action=InterventionAction.RETRY_SCHEDULED, forced_scheduled_for=reset_eta,
        note=f"Issuer circuit breaker tripped; retries paused until ~{reset_eta.isoformat()}.",
    )


# Contract every rule above upholds (relied on by app/policy.py's evaluate()):
#   1. A fired RuleOutcome (fired=True) always sets at least one of forced_action /
#      forced_scheduled_for — a rule that fires but changes nothing should return NOT_FIRED instead.
#   2. A RuleOutcome that sets forced_action=RETRY_SCHEDULED always pairs it with
#      forced_scheduled_for in the same outcome — never emit RETRY_SCHEDULED with no timestamp;
#      policy.py isn't required to invent one.
#
# Precedence order — see app/policy.py for how this is consumed. HARD_DECLINE_NO_RETRY goes first
# because it's structural (redirects to a wholly different action, not a timing tweak), then the
# rules that can also redirect to ESCALATE_HUMAN, then the ones that only ever adjust timing.
RULE_PIPELINE = [
    hard_decline_no_retry,
    max_lifetime_attempts,
    contact_frequency_cap,
    issuer_circuit_breaker,
    min_attempt_interval,
    quiet_hours,
]
