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
