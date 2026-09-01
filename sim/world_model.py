"""
The disclosed world model. Every constant here is documented in docs/world-model.md — this file
is the executable form of that document, and the two must stay in sync (a divergence found while
building belongs in docs/build-log.md, not a silent edit to either file).

Naming convention (read the identifier, not a comment, to know the evidentiary status):
    MEASURED_*  -> grounded in a cited external source (docs/world-model.md §"Sources consulted")
    ASSUMED_*   -> no public source; an engineering judgment call, stated as a number so it can be
                   argued with and swept in the sensitivity analysis (docs/results/sensitivity-sweep.md)

This module has no I/O and no dependency on app/*. It is used by sim/generate_batch.py (to draw a
realistic decline mix) and sim/run_arms.py (to decide, stochastically, whether a given retry would
have succeeded in the simulated world). It is NOT used by app/policy.py — the policy gate must never
depend on whether a retry would "actually" succeed; it only bounds what the agent is allowed to try.
Conflating the two would let the simulator's assumptions leak into the thing being measured against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class FailureClass(str, Enum):
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


# ---------------------------------------------------------------------------
# §1 Decline reason distribution — docs/world-model.md §1
# ---------------------------------------------------------------------------

# MEASURED: Slicker "2025 Involuntary Churn Benchmarks" card-only decline mix.
MEASURED_DECLINE_MIX_CARD_ONLY: dict[FailureClass, float] = {
    FailureClass.SOFT_FUNDS: 0.35,
    FailureClass.HARD_INSTRUMENT: 0.50,   # 28% expired + 22% changed card, source reports jointly
    FailureClass.SOFT_TRANSIENT: 0.15,
}

# ASSUMED: India-adjusted mix actually used by the generator. See docs/world-model.md §1 for the
# rationale (OTP/3DS prevalence has no analogue in the card-only source; UPI failure surface differs).
ASSUMED_INDIA_DECLINE_MIX: dict[FailureClass, float] = {
    FailureClass.SOFT_FUNDS: 0.28,
    FailureClass.HARD_INSTRUMENT: 0.22,
    FailureClass.SOFT_TRANSIENT: 0.14,
    FailureClass.SOFT_AUTH: 0.20,
    FailureClass.SOFT_LIMIT: 0.08,
    FailureClass.HARD_RISK: 0.05,
    FailureClass.HARD_MANDATE: 0.03,
}
assert abs(sum(ASSUMED_INDIA_DECLINE_MIX.values()) - 1.0) < 1e-9

# Representative decline reason codes/text per class, for populating Attempt.decline_reason_raw
# with something a classifier can actually parse instead of the class name itself.
DECLINE_REASON_SAMPLES: dict[FailureClass, list[str]] = {
    FailureClass.SOFT_TRANSIENT: [
        "issuer_unavailable", "gateway_timeout", "network_error", "issuer_or_switch_inoperative",
    ],
    FailureClass.SOFT_FUNDS: ["insufficient_funds", "code_51_not_sufficient_funds"],
    FailureClass.SOFT_LIMIT: ["daily_limit_exceeded", "per_txn_limit_exceeded", "velocity_limit_exceeded"],
    FailureClass.SOFT_AUTH: ["otp_timeout", "3ds_authentication_abandoned", "customer_did_not_complete_auth"],
    FailureClass.HARD_INSTRUMENT: ["expired_card", "card_blocked", "invalid_account", "account_closed"],
    FailureClass.HARD_RISK: ["issuer_risk_decline", "suspected_fraud", "do_not_honor_risk"],
    FailureClass.HARD_MANDATE: ["mandate_revoked", "mandate_paused", "mandate_exhausted"],
}


# ---------------------------------------------------------------------------
# §2.1 Base first-attempt hazard by class — docs/world-model.md §2.1
# ---------------------------------------------------------------------------

MEASURED_TRANSIENT_HAZARD_1 = 0.70   # Rechurn/Slicker: smart retry recovers 70-85% of soft declines
MEASURED_FUNDS_HAZARD_1 = 0.35       # calibrated so the attempt-4 series converges near source ceiling
ASSUMED_LIMIT_HAZARD_1 = 0.45        # no source; set above funds hazard, no external-cash-flow wait
MEASURED_AUTH_HAZARD_1 = 0.55        # fresh link removes original friction; customer still must act
ASSUMED_HARD_HAZARD = 0.00           # structural: HARD_* is never retried, by policy (build spec §3)

BASE_HAZARD_1: dict[FailureClass, float] = {
    FailureClass.SOFT_TRANSIENT: MEASURED_TRANSIENT_HAZARD_1,
    FailureClass.SOFT_FUNDS: MEASURED_FUNDS_HAZARD_1,
    FailureClass.SOFT_LIMIT: ASSUMED_LIMIT_HAZARD_1,
    FailureClass.SOFT_AUTH: MEASURED_AUTH_HAZARD_1,
    FailureClass.HARD_INSTRUMENT: ASSUMED_HARD_HAZARD,
    FailureClass.HARD_RISK: ASSUMED_HARD_HAZARD,
    FailureClass.HARD_MANDATE: ASSUMED_HARD_HAZARD,
}

# §2.2 Hazard decay across attempts — Slicker: "a third attempt rarely changes the outcome"
ASSUMED_HAZARD_DECAY = 0.55


def hazard_for_attempt(failure_class: FailureClass, attempt_no: int) -> float:
    """attempt_no is 1-indexed. Hazard = base * decay^(attempt_no - 1)."""
    base = BASE_HAZARD_1[failure_class]
    return base * (ASSUMED_HAZARD_DECAY ** (attempt_no - 1))


# ---------------------------------------------------------------------------
# §2.3 Timing multiplier — the India-specific insight, docs/world-model.md §2.3
# ---------------------------------------------------------------------------

# MEASURED: Remunance — Indian payroll fund transfers cluster 28th-31st, not the 1st.
MEASURED_PAYDAY_WINDOW_DAYS = (28, 31)

ASSUMED_PAYDAY_MULTIPLIER = 1.6       # no source gives a direct multiplier; sized to be meaningful
                                       # without claiming near-certainty
ASSUMED_OFF_PEAK_MULTIPLIER = 0.85    # first-half-of-month SOFT_FUNDS retry, furthest from any payday


def _last_day_of_month(d: date) -> int:
    if d.month == 12:
        nxt = date(d.year + 1, 1, 1)
    else:
        nxt = date(d.year, d.month + 1, 1)
    return (nxt - date(d.year, d.month, 1)).days


def timing_multiplier(failure_class: FailureClass, scheduled_for: datetime) -> float:
    """
    Only SOFT_FUNDS has a timing mechanism (external cash inflow). Every other class is timing-
    invariant in this model — see docs/world-model.md §2.3 for why inventing one for e.g.
    SOFT_TRANSIENT would be assumption for its own sake rather than a modeled mechanism.
    """
    if failure_class != FailureClass.SOFT_FUNDS:
        return 1.0

    day = scheduled_for.day
    last_day = _last_day_of_month(scheduled_for.date())
    lo, hi = MEASURED_PAYDAY_WINDOW_DAYS
    # Clamp the window's upper bound to the actual last day of a short month (Feb, 30-day months).
    hi = min(hi, last_day)
    if lo <= day <= hi:
        return ASSUMED_PAYDAY_MULTIPLIER
    if day <= 14:
        return ASSUMED_OFF_PEAK_MULTIPLIER
    return 1.0  # mid-month: neither penalized nor boosted


# ---------------------------------------------------------------------------
# §2.4 NUDGE contact lift — docs/world-model.md §2.4 (weakest assumption; sweep first)
# ---------------------------------------------------------------------------

ASSUMED_NUDGE_LIFT = 0.12             # additive, applied on top of the retry-path hazard
ASSUMED_CONTACT_FATIGUE = -0.03       # per contact beyond the first, within the 30-day cap window


def apply_nudge_lift(hazard: float, prior_contacts_in_window: int) -> float:
    lift = ASSUMED_NUDGE_LIFT + ASSUMED_CONTACT_FATIGUE * max(0, prior_contacts_in_window - 1)
    return max(0.0, min(1.0, hazard + lift))


# ---------------------------------------------------------------------------
# §3 Issuer circuit breaker — docs/world-model.md §3 (largest unvalidated assumption)
# ---------------------------------------------------------------------------

ASSUMED_ISSUER_DEGRADATION_THRESHOLD = 0.20   # rolling 30-min success rate below which breaker trips
ASSUMED_BREAKER_PENALTY = 0.5                 # hazard multiplier for in-flight retries while tripped


@dataclass(frozen=True)
class SensitivitySweepSpec:
    """One row per swept ASSUMED_* constant. Used by sim/run_arms.py's sweep driver."""
    name: str
    baseline: float
    low: float
    high: float


SENSITIVITY_SWEEP: list[SensitivitySweepSpec] = [
    SensitivitySweepSpec("ASSUMED_LIMIT_HAZARD_1", ASSUMED_LIMIT_HAZARD_1, 0.25, 0.65),
    SensitivitySweepSpec("ASSUMED_HAZARD_DECAY", ASSUMED_HAZARD_DECAY, 0.35, 0.75),
    SensitivitySweepSpec("ASSUMED_PAYDAY_MULTIPLIER", ASSUMED_PAYDAY_MULTIPLIER, 1.1, 2.2),
    SensitivitySweepSpec("ASSUMED_OFF_PEAK_MULTIPLIER", ASSUMED_OFF_PEAK_MULTIPLIER, 0.6, 1.0),
    SensitivitySweepSpec("ASSUMED_NUDGE_LIFT", ASSUMED_NUDGE_LIFT, 0.0, 0.25),
    SensitivitySweepSpec("ASSUMED_CONTACT_FATIGUE", ASSUMED_CONTACT_FATIGUE, -0.08, 0.0),
    SensitivitySweepSpec("ASSUMED_ISSUER_DEGRADATION_THRESHOLD", ASSUMED_ISSUER_DEGRADATION_THRESHOLD, 0.10, 0.35),
    SensitivitySweepSpec("ASSUMED_BREAKER_PENALTY", ASSUMED_BREAKER_PENALTY, 0.3, 0.7),
]


# ---------------------------------------------------------------------------
# §4 Customer churn — the WORLD's process, deliberately not the agent's
# ---------------------------------------------------------------------------
#
# Read this before comparing anything to anything.
#
# The sharpest attack on a simulated result is that we write both the constants
# that generate outcomes and the constants the agent optimises against. If the
# simulator's churn function and the agent's churn_hazard are the same function,
# Backstop beats naive BY CONSTRUCTION — we told the world that contacts cause
# churn, then told the agent to avoid contacts, and then reported our own
# premise back as a finding.
#
# So the two are kept structurally different, not merely differently-numbered:
#
#   The agent (app/scorer.churn_hazard, constants in world_model_constants.py)
#   believes fatigue is GEOMETRIC in contact count — base x growth^n — with an
#   exponential recency decay on the accumulated count.
#
#   The world (below) uses a SATURATING process: each contact adds a hazard
#   increment that shrinks as the customer's patience is consumed, and patience
#   recovers linearly with time rather than exponentially.
#
# Neither form is more correct; there is no public data on either, which is the
# honest position stated in docs/world-model.md. The point is that an agent
# optimising the geometric belief against a saturating world is solving a
# problem it has been given the wrong shape for, so any advantage it shows is
# not an artefact of the two being the same equation. sim/run_arms.py sweeps
# agent belief against world truth to show what happens as the two diverge.

ASSUMED_WORLD_CHURN_CEILING = 0.22     # max cumulative churn probability from contact alone
ASSUMED_WORLD_CHURN_SATURATION = 2.5   # contacts at which roughly 63% of the ceiling is reached
ASSUMED_WORLD_PATIENCE_RECOVERY_DAYS = 21.0   # linear, not exponential — unlike the agent's belief

# Remaining customer lifetime value as a multiple of the failed invoice. The
# agent believes 6.0x (Redux's worked example). The world is drawn per customer
# across a wide band, because a single multiple across every customer is the
# assumption most likely to be flattering us.
ASSUMED_WORLD_LTV_MULTIPLE_RANGE = (2.0, 14.0)

# The cost of giving up. An invoice that is never recovered does not simply
# vanish — a meaningful share of those customers lapse, which is what
# "involuntary churn" names.
#
# This constant is what stops the policy being degenerate in the other
# direction. Without it, stopping is free: the agent would stop on everything,
# score a perfect zero on harm, and the frontier chart would be a single dot at
# the origin. With it, both doing too much and doing too little cost real money,
# which is the only configuration in which an efficient frontier exists at all.
ASSUMED_WORLD_UNRECOVERED_CHURN = 0.55

# Cost the merchant actually bears per failed authorisation, over and above the
# gateway fee: issuer-side scoring, acquirer review risk, network penalties on
# excessive reattempts. The agent has its own belief about this number; this is
# what the world charges.
ASSUMED_WORLD_ISSUER_PENALTY_INR = 8.80
ASSUMED_WORLD_RETRY_FEE_INR = 0.50


def world_churn_probability(contacts: int, days_since_last: float | None) -> float:
    """
    Ground truth: probability this customer churns as a result of dunning
    contact, given how many they have had and how long ago.

    Saturating exponential in contact count, linear patience recovery in time.
    Deliberately a different shape from the agent's geometric belief — see the
    section header.
    """
    if contacts <= 0:
        return 0.0
    effective = float(contacts)
    if days_since_last is not None:
        recovered = min(1.0, days_since_last / ASSUMED_WORLD_PATIENCE_RECOVERY_DAYS)
        effective = max(0.0, effective - recovered * effective)
    return ASSUMED_WORLD_CHURN_CEILING * (
        1.0 - 2.718281828459045 ** (-effective / ASSUMED_WORLD_CHURN_SATURATION)
    )


SENSITIVITY_SWEEP.extend(
    [
        SensitivitySweepSpec(
            "ASSUMED_WORLD_CHURN_CEILING", ASSUMED_WORLD_CHURN_CEILING, 0.0, 0.40
        ),
        SensitivitySweepSpec(
            "ASSUMED_WORLD_CHURN_SATURATION", ASSUMED_WORLD_CHURN_SATURATION, 1.0, 6.0
        ),
        SensitivitySweepSpec(
            "ASSUMED_WORLD_UNRECOVERED_CHURN", ASSUMED_WORLD_UNRECOVERED_CHURN, 0.25, 0.80
        ),
        SensitivitySweepSpec(
            "ASSUMED_WORLD_ISSUER_PENALTY_INR", ASSUMED_WORLD_ISSUER_PENALTY_INR, 0.5, 40.0
        ),
    ]
)
