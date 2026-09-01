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

# --- Card-network limits (build spec §11). Not ours, not sweepable. ---
# Mastercard Merchant Advice Codes that forbid any reattempt: 03 = fraudulent,
# 21 = lost or stolen. Retrying after either bills a TPE fee immediately.
MC_NEVER_RETRY_ADVICE_CODES = frozenset({"03", "21"})
VISA_REATTEMPT_CAP_PER_30D = 15   # reattempts per card per 30 days
MC_AUTH_CAP_PER_24H = 10          # authorisation attempts per PAN per 24 hours

# --- RBI e-mandate (build spec §11) ---
# A pre-transaction notification must reach the customer at least 24h before every
# e-mandate debit. Stripe builds a 26h buffer into their India recurring flow for
# downstream slack; we use 26h for the same reason.
EMANDATE_PREDEBIT_NOTICE = timedelta(hours=24)
EMANDATE_NOTICE_BUFFER = timedelta(hours=26)

CONTACT_ACTIONS = frozenset({InterventionAction.NUDGE, InterventionAction.REQUEST_INSTRUMENT_UPDATE})
RETRY_ACTIONS = frozenset({InterventionAction.RETRY_NOW, InterventionAction.RETRY_SCHEDULED})


class RuleName(str, Enum):
    HARD_DECLINE_NO_RETRY = "HARD_DECLINE_NO_RETRY"
    MAX_LIFETIME_ATTEMPTS = "MAX_LIFETIME_ATTEMPTS"
    MIN_ATTEMPT_INTERVAL = "MIN_ATTEMPT_INTERVAL"
    CONTACT_FREQUENCY_CAP = "CONTACT_FREQUENCY_CAP"
    QUIET_HOURS = "QUIET_HOURS"
    ISSUER_CIRCUIT_BREAKER = "ISSUER_CIRCUIT_BREAKER"
    # Added for build spec §11 (Razorpay-specific compliance). These are card-network
    # and RBI constraints; they are not policy we chose and have nothing to sweep.
    MC_NEVER_RETRY_ADVICE_CODE = "MC_NEVER_RETRY_ADVICE_CODE"
    NETWORK_REATTEMPT_CAP = "NETWORK_REATTEMPT_CAP"
    EMANDATE_PREDEBIT_NOTICE = "EMANDATE_PREDEBIT_NOTICE"
    HARD_RISK_NO_CONTACT = "HARD_RISK_NO_CONTACT"


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

    # --- Added for build spec §11's compliance rules. ---
    # Defaulted and appended deliberately: every existing caller and all 149
    # existing tests construct PolicyContext without them and must keep working.
    # A default of "absent / zero" is also the safe direction for each — an
    # unknown advice code does not forbid a retry, and an unseen reattempt
    # history does not exhaust a cap.
    is_recurring: bool = False
    # The Mastercard advice code the issuer attached, when it attached one. Many
    # Indian issuers do not do so consistently, which is why absence is modelled
    # as a real state rather than a data gap.
    mastercard_advice_code: str | None = None
    # e-mandate rail only: when the RBI pre-debit notification was actually sent.
    predebit_notice_sent_at_utc: datetime | None = None
    # Reattempts on this CARD across all invoices — a per-invoice attempt count
    # cannot see these, which is why the network caps are not redundant with
    # MAX_LIFETIME_ATTEMPTS.
    card_reattempts_in_30d: int = 0
    auth_attempts_in_24h: int = 0

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

# ---------------------------------------------------------------------------
# Build spec §11 — Razorpay-specific compliance
#
# Three rules added after the original six. They follow the same RuleOutcome
# contract and none of them touches the logic of a rule that was already here.
#
# What makes them worth their own section: every one of them is somebody else's
# rule. The card networks and the RBI wrote them, we only encode them. They have
# no threshold to tune and nothing to sweep, and when one of them disagrees with
# an economic argument the rule wins by definition rather than by weight.
def hard_risk_no_contact(
    ctx: PolicyContext, proposed_action: InterventionAction, proposed_scheduled_for: datetime | None
) -> RuleOutcome:
    """
    A HARD_RISK decline admits NO automated customer contact — not a nudge, and
    not an instrument-update request either.

    Added to close a gap found by an interaction test rather than by design.
    `hard_decline_no_retry` already forbids NUDGE on HARD_RISK, implementing the
    build spec's "flag and escalate, never retry, never nudge" literally. But
    REQUEST_INSTRUMENT_UPDATE is the other customer-facing action, it carries the
    same hazard, and nothing was stopping it: a proposal of
    REQUEST_INSTRUMENT_UPDATE on a suspected-fraud decline passed the entire
    pipeline untouched.

    Why that matters more than the wording gap suggests. The standard recovery
    move on a declined card is to ask the customer for a different payment
    method. Sent to a fraud or lost/stolen decline, that is the merchant telling
    whoever is holding the card which instrument to try next, with the merchant's
    own branding on the message. At this point the merchant cannot tell the real
    cardholder from the fraudster, which is exactly why the correct destination
    is a human who can look at the account rather than an automated ask.

    Implemented as a separate rule rather than a line inside
    hard_decline_no_retry, because that function is part of the tested-and-frozen
    original pipeline (build spec §12). The outcome is identical either way —
    both redirect to ESCALATE_HUMAN — and this leaves the existing rule's logic
    and its branch coverage untouched.
    """
    if ctx.failure_class != FailureClass.HARD_RISK:
        return RuleOutcome.NOT_FIRED
    if proposed_action not in CONTACT_ACTIONS:
        return RuleOutcome.NOT_FIRED
    return RuleOutcome(
        fired=True, rule=RuleName.HARD_RISK_NO_CONTACT,
        forced_action=InterventionAction.ESCALATE_HUMAN,
        note=(
            f"HARD_RISK admits no automated customer contact; {proposed_action.value} "
            "would suggest which instrument to try next. Escalating to a human."
        ),
    )


# ---------------------------------------------------------------------------


def mastercard_never_retry(
    ctx: PolicyContext, proposed_action: InterventionAction, proposed_scheduled_for: datetime | None
) -> RuleOutcome:
    """
    MAC 03 (fraudulent) or 21 (lost/stolen): any reattempt bills a Mastercard TPE
    fee immediately.

    This is deliberately separate from hard_decline_no_retry rather than folded
    into it, because it fires on the NETWORK's label instead of on our own
    classification. That is the whole point of having it: it still catches the
    case where L1 read a fraud decline as SOFT_FUNDS and proposed a retry in
    good faith. A backstop that only works when our classification was already
    correct is not a backstop against our classification being wrong.

    Source: Mastercard Transaction Processing Excellence programme.
    """
    if proposed_action not in RETRY_ACTIONS:
        return RuleOutcome.NOT_FIRED
    code = ctx.mastercard_advice_code
    if code is None or code not in MC_NEVER_RETRY_ADVICE_CODES:
        return RuleOutcome.NOT_FIRED
    return RuleOutcome(
        fired=True, rule=RuleName.MC_NEVER_RETRY_ADVICE_CODE,
        forced_action=InterventionAction.ESCALATE_HUMAN,
        note=(
            f"Issuer returned Mastercard advice code {code}; any reattempt is fee-bearing "
            "under the TPE programme. Escalating rather than retrying."
        ),
    )


def network_reattempt_cap(
    ctx: PolicyContext, proposed_action: InterventionAction, proposed_scheduled_for: datetime | None
) -> RuleOutcome:
    """
    Visa: 15 reattempts per card per 30 days. Mastercard: 10 authorisations per
    PAN per 24 hours. Past either, every further attempt is fee-bearing.

    Not redundant with MAX_LIFETIME_ATTEMPTS, which counts attempts on ONE
    invoice. These count attempts on one CARD across every invoice it backs, and
    a customer with several failing subscriptions exhausts the network's budget
    long before any single invoice exhausts ours.

    Sources: Visa Excessive Reattempts Rule; Mastercard TPE.
    """
    if proposed_action not in RETRY_ACTIONS:
        return RuleOutcome.NOT_FIRED

    if ctx.card_reattempts_in_30d >= VISA_REATTEMPT_CAP_PER_30D:
        return RuleOutcome(
            fired=True, rule=RuleName.NETWORK_REATTEMPT_CAP,
            forced_action=InterventionAction.ESCALATE_HUMAN,
            note=(
                f"{ctx.card_reattempts_in_30d} reattempts on this card in the trailing 30 days "
                f"(Visa cap {VISA_REATTEMPT_CAP_PER_30D}); further attempts are fee-bearing."
            ),
        )
    if ctx.auth_attempts_in_24h >= MC_AUTH_CAP_PER_24H:
        return RuleOutcome(
            fired=True, rule=RuleName.NETWORK_REATTEMPT_CAP,
            forced_action=InterventionAction.ESCALATE_HUMAN,
            note=(
                f"{ctx.auth_attempts_in_24h} authorisations on this PAN in the trailing 24 hours "
                f"(Mastercard cap {MC_AUTH_CAP_PER_24H}); further attempts are fee-bearing."
            ),
        )
    return RuleOutcome.NOT_FIRED


def emandate_predebit_notice(
    ctx: PolicyContext, proposed_action: InterventionAction, proposed_scheduled_for: datetime | None
) -> RuleOutcome:
    """
    No e-mandate debit until the customer has had the RBI pre-transaction notice
    for 24 hours. We enforce 26 to leave downstream slack, matching Stripe.

    SCOPE THIS CAREFULLY — it is the easiest rule here to get wrong in the
    conservative direction, and being wrong that way is expensive and quiet. The
    RBI notification obligation binds the E-MANDATE AUTO-DEBIT RAIL. It does not
    bind a customer-initiated payment-link retry, so this rule checks
    `is_recurring` and only ever looks at retry actions. Applying it to a
    REQUEST_INSTRUMENT_UPDATE would suppress the correct action on every
    recurring invoice while looking entirely reasonable in review.

    A consequence worth stating: this makes "retry within the hour" illegal on
    the Indian recurring rail, so Mastercard advice code 24 is unfollowable
    here. Where the network and the regulator disagree, the regulator wins.

    Source: RBI/DPSS/2026-27/396, 21 April 2026.
    """
    if proposed_action not in RETRY_ACTIONS or not ctx.is_recurring:
        return RuleOutcome.NOT_FIRED

    effective_time = (
        proposed_scheduled_for
        if proposed_action == InterventionAction.RETRY_SCHEDULED and proposed_scheduled_for
        else ctx.now_utc
    )

    if ctx.predebit_notice_sent_at_utc is None:
        # No notice has been sent, so the earliest lawful debit is a full buffer
        # from now — sending the notice is part of scheduling the debit.
        earliest = ctx.now_utc + EMANDATE_NOTICE_BUFFER
        note = (
            "No pre-debit notice on record for this mandate; earliest lawful debit is "
            f"{earliest.isoformat()} ({EMANDATE_NOTICE_BUFFER} after the notice goes out)."
        )
    else:
        earliest = ctx.predebit_notice_sent_at_utc + EMANDATE_NOTICE_BUFFER
        if effective_time >= earliest:
            return RuleOutcome.NOT_FIRED
        note = (
            f"Pre-debit notice sent {ctx.predebit_notice_sent_at_utc.isoformat()}; "
            f"debit not lawful before {earliest.isoformat()} "
            f"(statutory {EMANDATE_PREDEBIT_NOTICE}, buffered to {EMANDATE_NOTICE_BUFFER})."
        )

    return RuleOutcome(
        fired=True, rule=RuleName.EMANDATE_PREDEBIT_NOTICE,
        forced_action=InterventionAction.RETRY_SCHEDULED, forced_scheduled_for=earliest,
        note=note,
    )

RULE_PIPELINE = [
    hard_decline_no_retry,
    # §11 network rules sit here: like hard_decline_no_retry they redirect to a
    # different action rather than adjusting timing, and they must be evaluated
    # before anything that only pushes a schedule around.
    mastercard_never_retry,
    network_reattempt_cap,
    hard_risk_no_contact,
    max_lifetime_attempts,
    contact_frequency_cap,
    issuer_circuit_breaker,
    # Timing rules. emandate_predebit_notice comes before min_attempt_interval so
    # that a debit pushed to the notice window can still be pushed further out by
    # the interval rule — the later rule sees the schedule the earlier one set.
    emandate_predebit_notice,
    min_attempt_interval,
    quiet_hours,
]
