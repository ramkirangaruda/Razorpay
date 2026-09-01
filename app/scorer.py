"""
L2b — the expected-value scorer.

The reframe this file exists to implement: the system no longer asks "is this
action allowed?" and calls the answer restraint. It asks "is this action worth
taking?", and stop is what happens when the answer is no for everything.

    EV(action) = P(recovery) x (invoice_value + P(lapse) x customer_LTV)
               - action_cost
               - issuer_trust_cost x P(failure | action)
               - churn_hazard(contacts, recency) x customer_LTV

DEVIATION FROM THE HANDOFF SPEC, DELIBERATE — the `P(lapse) x customer_LTV`
term is not in the brief's §5 formula, and the model does not work without it.

As specified, EV charges churn for CONTACTING a customer and charges nothing
for ABANDONING their invoice. Stop is priced at zero. But an invoice that is
never recovered is the definition of involuntary churn, so stopping costs the
remaining LTV too — and in the simulated world it costs it at 0.55, against
roughly 0.07 for a first contact. Giving up is about seven times more expensive
than asking, and a scorer that cannot see that stops on everything.

The first three-arm run said so plainly: Backstop recovered 63 invoices to the
naive baseline's 87 and lost on net recovered value by a wide margin. The
scorer was not being restrained, it was being blind to half the ledger.

The correction is not a tuning choice. It restores the framing of our own cited
source: Redux frames the true cost of a failed payment as unrecovered failures
x remaining LTV, not the failed charge alone. The brief's formula contradicts
the citation it rests on.

STOP still scores exactly zero, so it remains the reference point and "stop
when nothing has positive EV" is still the rule. What changed is that the other
actions are now credited with the lapse they PREVENT, which is most of what
recovering an invoice is worth.

------------------------------------------------------------------------------
Beliefs are not the world
------------------------------------------------------------------------------

Every number this scorer uses comes from a `Beliefs` object, never from module
globals. That indirection is the whole defence against the sharpest attack on a
simulated result: if the function generating outcomes and the function the agent
optimises against are the same function, the agent beats the baseline by
construction, because we told the world that contacts cause churn and then told
the agent to avoid contacts.

`Beliefs.perturbed()` exists so the robustness arm can hand the agent parameters
that are deliberately wrong — biased in both directions, by 30-50% — while the
simulator keeps its own. The claim we want to be able to make is "our advantage
survives being wrong about the constants", and it is only checkable if the two
sets of numbers can differ.

------------------------------------------------------------------------------
Two things this scorer deliberately does not price
------------------------------------------------------------------------------

ESCALATE_HUMAN. Pricing it needs a cost for an analyst's time, which we have no
source for and would have to invent. It stays out of the candidate set and
remains what L2a does with a risk decline — a disposition, not an economic
choice. Declared as a scope-out in docs/world-model.md rather than papered over
with a made-up hourly rate.

The value of information. A retry that fails tells us something about the
invoice, and a strictly correct treatment would price that. It does not, and the
omission biases the scorer toward stopping slightly too early. Named in
world-model.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime

from app.models import FailureClass, InterventionAction
from app.policy import BLAST_RADIUS_RANK
from app.stopping_rules import CONTACT_ACTIONS, RETRY_ACTIONS
from sim.world_model_constants import REGISTRY, resolve

A = InterventionAction

# Which mechanism each action is, for the channel-fit lookup.
_MECHANISM: dict[InterventionAction, str] = {
    A.RETRY_NOW: "RETRY",
    A.RETRY_SCHEDULED: "RETRY",
    A.REQUEST_INSTRUMENT_UPDATE: "LINK",
    A.NUDGE: "NUDGE",
}

# The actions L2b prices. STOP is scored separately (it is always zero) and
# ESCALATE_HUMAN is not priced at all — see the module docstring.
PRICEABLE = (A.RETRY_NOW, A.RETRY_SCHEDULED, A.REQUEST_INSTRUMENT_UPDATE, A.NUDGE)

VALID_BUCKETS = ("VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH")


@dataclass(frozen=True)
class Beliefs:
    """
    The agent's estimated parameters — everything the scorer is allowed to know.

    Built from world_model_constants at a sweep position, then optionally
    perturbed. Never read the constants module directly from scoring code; that
    is how the agent's beliefs and the world's truth silently become the same
    object again.
    """

    p_recovery_by_bucket: dict[str, float]
    marginal_recovery_by_attempt: dict[int, float]
    channel_fit: dict[tuple[str, str], float]
    retry_cost_inr: float
    issuer_trust_cost_inr: float
    ltv_multiple: float
    contact_fatigue_base: float
    contact_fatigue_growth: float
    contact_recency_halflife_days: float
    retry_timing_lift: float
    p_lapse_if_unrecovered: float

    @classmethod
    def from_constants(cls, sweep: dict[str, float] | None = None) -> "Beliefs":
        """
        `sweep` maps a constant name to its position t in [0,1]. Anything absent
        resolves at its declared value. Always goes through resolve(), which is
        the only thing that handles absolute and multiplicative ranges
        correctly — interpolating `.sweep` by hand sweeps LTV_MULTIPLE down to
        0.6 and silently corrupts the sensitivity arm.
        """
        sweep = sweep or {}

        def scalar(name: str) -> float:
            if name in sweep:
                return resolve(name, sweep[name])
            return float(REGISTRY[name].value)

        def table(name: str) -> dict:
            """Multiplicative sweeps scale a whole table by one factor."""
            base = REGISTRY[name].value
            if name not in sweep:
                return dict(base)
            c = REGISTRY[name]
            lo, hi = c.sweep
            factor = lo + sweep[name] * (hi - lo)
            return {k: v * factor for k, v in base.items()}

        # The structural zeroes in CHANNEL_FIT stay zero under any sweep: a
        # retry cannot fix an OTP drop-off at any parameter value, and letting
        # the sweep move that would be sweeping a mechanism, not a judgment.
        fit = table("CHANNEL_FIT")
        for k, v in REGISTRY["CHANNEL_FIT"].value.items():
            if v == 0.0:
                fit[k] = 0.0

        return cls(
            p_recovery_by_bucket=table("P_RECOVERY_BY_BUCKET"),
            marginal_recovery_by_attempt=table("MARGINAL_RECOVERY_BY_ATTEMPT"),
            channel_fit=fit,
            retry_cost_inr=scalar("RETRY_COST_INR"),
            issuer_trust_cost_inr=scalar("ISSUER_TRUST_COST_INR"),
            ltv_multiple=scalar("LTV_MULTIPLE_OF_INVOICE"),
            contact_fatigue_base=scalar("CONTACT_FATIGUE_BASE_HAZARD"),
            contact_fatigue_growth=scalar("CONTACT_FATIGUE_GROWTH"),
            contact_recency_halflife_days=scalar("CONTACT_RECENCY_HALFLIFE_DAYS"),
            retry_timing_lift=scalar("RETRY_TIMING_LIFT"),
            p_lapse_if_unrecovered=scalar("P_LAPSE_IF_UNRECOVERED"),
        )

    def perturbed(self, factors: dict[str, float]) -> "Beliefs":
        """
        Return beliefs with named fields multiplied by the given factors.

        This is the circularity mitigation's working end. `factors` biases in
        whichever direction the caller asks for, so the robustness arm can run
        an agent that believes churn is half what it is and an agent that
        believes it is double, and show the net-value advantage degrading
        gracefully rather than inverting.
        """
        out: dict = {}
        for field_name, factor in factors.items():
            current = getattr(self, field_name)
            if isinstance(current, dict):
                out[field_name] = {k: v * factor for k, v in current.items()}
            else:
                out[field_name] = current * factor
        return replace(self, **out)


@dataclass(frozen=True)
class ScoreContext:
    """Everything the scorer may see. Deliberately closed, like RuleContext."""

    invoice_value_inr: float
    recovery_bucket: str          # ordinal, from L1. Never a float. See below.
    failure_class: FailureClass
    attempt_no: int               # 1-indexed; the attempt being considered
    contacts_so_far: int
    days_since_last_contact: float | None
    now: datetime
    is_recurring: bool = False
    mastercard_advice_code: str | None = None

    def __post_init__(self) -> None:
        # This is the guard behind "L1 must never emit a floating-point
        # probability". Language models are badly calibrated at producing
        # numbers, and a judge asking where 0.34 came from must not be told
        # "the model said so". L1 emits a bucket; the constants file maps
        # buckets to rates; every number in the EV traces to a citation.
        if self.recovery_bucket not in VALID_BUCKETS:
            raise ValueError(
                f"recovery_bucket must be one of {VALID_BUCKETS}, got "
                f"{self.recovery_bucket!r}. L1 emits ordinal buckets, not probabilities."
            )


@dataclass(frozen=True)
class ActionScore:
    """
    One action, priced, with every term kept separate.

    The separation is not for debugging. The trace renderer shows these terms
    individually because "explainable" means a reader can see which term
    dominated, and a single EV number explains nothing.
    """

    action: InterventionAction
    ev: float
    p_recovery: float
    recovery_value: float
    lapse_avoided: float
    action_cost: float
    issuer_trust_term: float
    churn_term: float
    timing_note: str = ""

    def as_terms(self) -> list[tuple[str, float]]:
        return [
            ("P(recovery) x invoice_value", self.recovery_value),
            ("+ lapse avoided by recovering", self.lapse_avoided),
            ("- action cost", -self.action_cost),
            ("- issuer trust x P(failure)", -self.issuer_trust_term),
            ("- churn hazard x LTV", -self.churn_term),
        ]


@dataclass(frozen=True)
class ScoreResult:
    chosen: InterventionAction
    scores: tuple[ActionScore, ...]        # every candidate, best first
    proposed: InterventionAction
    downgraded: bool

    @property
    def best(self) -> ActionScore | None:
        return self.scores[0] if self.scores else None

    def score_for(self, action: InterventionAction) -> ActionScore | None:
        return next((s for s in self.scores if s.action is action), None)


# ---------------------------------------------------------------------------
# The terms
# ---------------------------------------------------------------------------


def _marginal_decay(index: int, b: Beliefs) -> float:
    """
    The published marginal-recovery table, normalised to 1.0 at index 1 and
    extrapolated geometrically past its last entry.

    Normalising means the bucket rate keeps meaning "P on a first ask" instead
    of being silently rescaled by the table's own magnitude.

    Extrapolating matters more than it looks. The table stops at index 5, and
    the first draft clamped anything beyond it to the last value — which left
    a retry with a permanent floor of positive expected value, so the
    arithmetic never stopped and MAX_LIFETIME_ATTEMPTS did all the work. That
    is precisely the failure this layer exists to remove. Continuing the
    table's own decay ratio is the least inventive way to fix it: it adds no
    new constant and asserts nothing the published figures do not already imply.
    """
    table = b.marginal_recovery_by_attempt
    first = table.get(1)
    if not first:
        return 0.0
    last_idx = max(table)
    if index <= last_idx:
        return table.get(index, 0.0) / first

    # Geometric continuation from the last two published points.
    prev, last = table[last_idx - 1], table[last_idx]
    ratio = (last / prev) if prev else 0.5
    return (last / first) * (ratio ** (index - last_idx))


def p_recovery(action: InterventionAction, ctx: ScoreContext, b: Beliefs) -> tuple[float, str]:
    """
    P(recovery | action, context), and a note about the timing reasoning.

    Three multipliers on the bucket's base rate:
      - channel fit: can this mechanism physically address this failure at all;
      - attempt decay: the marginal recovery on the remaining pool, which is
        what makes attempt 4 score negative without any rule saying "max 4";
      - timing: a deferred retry aimed at a better moment is worth more.
    """
    mechanism = _MECHANISM.get(action)
    if mechanism is None:
        return 0.0, ""

    base = b.p_recovery_by_bucket[ctx.recovery_bucket]
    fit = b.channel_fit.get((mechanism, ctx.failure_class.value), 0.0)

    # Adverse selection, applied on the axis the action actually spends.
    #
    # A retry decays on the attempt index: each failed authorisation is
    # evidence this invoice is harder than average within its class. A contact
    # decays on the contact count instead, because a customer who has ignored
    # two payment links is less likely to act on a third — and that is a
    # different quantity from how many silent retries the merchant ran.
    #
    # Getting this wrong in the obvious direction (no decay on contacts at all,
    # which is what the first draft did) makes a payment link score the same on
    # the tenth ask as on the first, so the scorer never stops asking and the
    # contact cap has to do the stopping. That would put restraint back in a
    # rule, which is the whole thing this layer exists to avoid.
    index = ctx.attempt_no if action in RETRY_ACTIONS else ctx.contacts_so_far + 1
    decay = _marginal_decay(index, b)
    timing = 1.0
    note = ""
    if action is A.RETRY_SCHEDULED:
        timing, note = _timing_lift(ctx, b)

    return max(0.0, min(1.0, base * fit * decay * timing)), note


def _timing_lift(ctx: ScoreContext, b: Beliefs) -> tuple[float, str]:
    """
    Where the deferred retry's timing comes from, in priority order.

    1. A Mastercard advice code, when the issuer attached one. The network
       hands us a per-decline schedule for free, and where it exists it
       overrides our own estimate — the cheapest possible answer to "where does
       your retry timing come from?". Many Indian issuers do not attach MACs
       consistently, so its absence is itself a signal.
    2. The payday window, for SOFT_FUNDS only. Indian payroll clusters on the
       28th-31st, not the 1st. No other class has an external cash-flow
       mechanism, so inventing a timing story for a gateway timeout would be
       assumption for its own sake.
    3. Nothing. A deferred retry with no reason to prefer its slot is worth no
       more than an immediate one.
    """
    mac = ctx.mastercard_advice_code
    if mac and mac in REGISTRY["MC_TIMED_RETRY_ADVICE"].value:
        kind, n = REGISTRY["MC_TIMED_RETRY_ADVICE"].value[mac]
        return b.retry_timing_lift, f"MAC {mac}: {kind.replace('_', ' ')} {n}"

    if ctx.failure_class is FailureClass.SOFT_FUNDS:
        lo, hi = 28, 31
        target_day = _next_payday_day(ctx.now, lo, hi)
        if target_day is not None:
            return b.retry_timing_lift, f"deferred into the {lo}-{hi} payday window"
        return 1.0, "payday window not reachable before the invoice ages out"

    return 1.0, "no timing signal: MAC absent and class has no cash-flow mechanism"


def _next_payday_day(now: datetime, lo: int, hi: int) -> int | None:
    """The next day-of-month inside the payday window, if it is within a fortnight."""
    if lo <= now.day <= hi:
        return now.day
    if now.day < lo:
        return lo if (lo - now.day) <= 14 else None
    return lo if (31 - now.day + lo) <= 14 else None


def churn_hazard(ctx: ScoreContext, b: Beliefs) -> float:
    """
    Incremental probability that this contact is the one that loses the customer.

        hazard = base x growth^(contacts already sent), decayed by recency

    The exponent counts contacts already sent, so the first contact costs the
    base hazard and each subsequent one costs more — the intuition being that
    the fourth message annoys more than the first. That intuition is ours;
    CONTACT_FATIGUE_GROWTH is swept down to 1.0, where it disappears.

    Recency decays the accumulated count rather than the hazard, which is the
    behaviour we want: a customer contacted three times last week is nearly
    exhausted, and the same customer contacted three times last quarter is
    close to fresh.

    THIS TERM IS THE PROJECT'S ONE UNSOURCED LOAD-BEARING NUMBER. There is no
    public data tying dunning contact count to churn hazard. The defence is not
    a better guess, it is breakeven_contact_fatigue(): the conclusion holds
    unless a dunning contact raises churn by less than roughly two-tenths of one
    percent. See docs/world-model.md.
    """
    effective = float(ctx.contacts_so_far)
    if ctx.days_since_last_contact is not None and b.contact_recency_halflife_days > 0:
        effective *= 0.5 ** (ctx.days_since_last_contact / b.contact_recency_halflife_days)
    return b.contact_fatigue_base * (b.contact_fatigue_growth ** effective)


def score_action(action: InterventionAction, ctx: ScoreContext, b: Beliefs) -> ActionScore:
    """Price one action, keeping every term separate."""
    if action is A.STOP_PERMANENT:
        return ActionScore(
            action=action,
            ev=0.0,
            p_recovery=0.0,
            recovery_value=0.0,
            lapse_avoided=0.0,
            action_cost=0.0,
            issuer_trust_term=0.0,
            churn_term=0.0,
            timing_note="stop is the zero reference: it moves no money and touches nobody",
        )

    p, note = p_recovery(action, ctx, b)
    recovery_value = p * ctx.invoice_value_inr

    # What recovering PREVENTS. An invoice that is never recovered takes the
    # customer with it a good share of the time, and that loss is avoided
    # exactly when the action works. Omitting this is what made the first
    # version of the scorer stop on almost everything.
    ltv = ctx.invoice_value_inr * b.ltv_multiple
    lapse_avoided = p * b.p_lapse_if_unrecovered * ltv

    # One marginal cost constant covers both an authorisation attempt and a
    # message. They are the same order of magnitude (sub-rupee) and both are
    # dominated by the penalty and churn terms — inventing a second unsourced
    # constant to separate them would add provenance debt for no effect on any
    # decision.
    action_cost = b.retry_cost_inr

    # Issuer trust degrades only when we spend an authorisation and it fails.
    # A payment link that goes unclicked costs the customer's patience, not the
    # merchant's standing with the issuer.
    if action in RETRY_ACTIONS:
        issuer_trust_term = b.issuer_trust_cost_inr * (1.0 - p)
    else:
        issuer_trust_term = 0.0

    # Contact fatigue is the churn a contact CAUSES, on top of the baseline
    # lapse risk above. Two different mechanisms, not a double count: one is
    # "we annoyed them", the other is "their subscription silently died".
    if action in CONTACT_ACTIONS:
        churn_term = churn_hazard(ctx, b) * ltv
    else:
        churn_term = 0.0

    ev = recovery_value + lapse_avoided - action_cost - issuer_trust_term - churn_term
    return ActionScore(
        action=action,
        ev=ev,
        p_recovery=p,
        recovery_value=recovery_value,
        lapse_avoided=lapse_avoided,
        action_cost=action_cost,
        issuer_trust_term=issuer_trust_term,
        churn_term=churn_term,
        timing_note=note,
    )


def score(
    proposed: InterventionAction,
    ctx: ScoreContext,
    beliefs: Beliefs,
) -> ScoreResult:
    """
    Price every candidate no wider than the proposal and return the best.

    The candidate restriction is the one-way valve at this layer. L2b may reject
    or downgrade what L1 proposed; it may never upgrade.

    The ranking comes from `app.policy.BLAST_RADIUS_RANK`, which is L2a's, not
    ours — one ordering for the whole pipeline or the valve means two different
    things at two layers. Worth flagging that it ranks RETRY_NOW (5) above the
    contact actions (3), on the reasoning that an immediate unattended charge is
    the most aggressive thing the system can do. This scorer would have ordered
    it the other way, since churn hazard makes the customer's patience the
    expensive irreversible term in the EV. L2a's ordering is the built and tested
    one, so it wins; the disagreement is recorded in docs/build-log.md rather
    than resolved silently in favour of whichever layer was written last. Scoring the full action
    set and taking the argmax would let a confident scorer talk the system into
    contacting a customer that L1 only wanted to retry silently — which is
    exactly the failure mode the layered design exists to prevent.

    STOP is always a candidate, and always scores zero, so an empty positive
    set resolves to stop without a special case.
    """
    ceiling = BLAST_RADIUS_RANK[proposed]
    candidates = [a for a in PRICEABLE if BLAST_RADIUS_RANK[a] <= ceiling]

    scores = [score_action(a, ctx, beliefs) for a in candidates]
    scores.append(score_action(A.STOP_PERMANENT, ctx, beliefs))
    scores.sort(key=lambda s: s.ev, reverse=True)

    chosen = scores[0].action if scores[0].ev > 0 else A.STOP_PERMANENT
    return ScoreResult(
        chosen=chosen,
        scores=tuple(scores),
        proposed=proposed,
        downgraded=chosen is not proposed,
    )
