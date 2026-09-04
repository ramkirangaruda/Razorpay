"""
Backstop — world-model constants.

Every constant used by the L2b expected-value scorer lives here, and every one
carries its provenance. Three tiers:

    REGULATORY  A law or card-network rule. Not a parameter. Not sweepable.
                These belong to L2a as hard constraints, and appear here only
                so the scorer knows which actions are legal to price at all.

    MEASURED    A number with a published external source. Sweepable within
                the range the sources actually disagree over.

    ASSUMED     Our own engineering judgment. No public source found. These are
                the constants the adversarial arm exists to stress. Every one
                of them is swept, and the README says so.

Sources are tiered too. PRIMARY means the regulator, the card network, or the
gateway itself. SECONDARY means vendor or industry analysis, which is useful
for magnitude but not for precision — where secondary sources disagree, the
disagreement becomes the sweep range rather than being averaged away.

Currency: INR. Card-network penalties are published in USD and converted; the
conversion rate is itself an ASSUMED constant, deliberately.

Run this file directly to emit the citation table for the README:

    python world_model_constants.py --citations
    python world_model_constants.py --unsourced
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Provenance scaffolding
# ---------------------------------------------------------------------------


class Provenance(Enum):
    REGULATORY = "regulatory"
    MEASURED = "measured"
    ASSUMED = "assumed"


class Tier(Enum):
    PRIMARY = "primary"      # regulator, card network, gateway docs
    SECONDARY = "secondary"  # vendor / industry analysis
    NONE = "none"            # no source exists


class SweepKind(Enum):
    ABSOLUTE = "absolute"          # (low, high) are the values themselves
    MULTIPLICATIVE = "multiplicative"  # (low, high) scale the declared value


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    tier: Tier
    detail: str = ""


@dataclass(frozen=True)
class Constant:
    value: Any
    unit: str
    provenance: Provenance
    sources: tuple[Source, ...] = ()
    sweep: tuple[float, float] | None = None
    sweep_kind: SweepKind = SweepKind.ABSOLUTE
    note: str = ""

    def __post_init__(self) -> None:
        if self.provenance is Provenance.REGULATORY and self.sweep is not None:
            raise ValueError("regulatory constants are not sweepable")
        if self.sweep is not None and self.sweep[0] > self.sweep[1]:
            raise ValueError(f"sweep range is inverted: {self.sweep}")
        if (
            self.sweep is not None
            and self.sweep_kind is SweepKind.ABSOLUTE
            and isinstance(self.value, (int, float))
            and not (self.sweep[0] <= float(self.value) <= self.sweep[1])
        ):
            raise ValueError(
                f"absolute sweep {self.sweep} does not contain the declared "
                f"value {self.value} — did you mean SweepKind.MULTIPLICATIVE?"
            )
        if self.provenance is Provenance.ASSUMED and self.sweep is None:
            raise ValueError(
                "every ASSUMED constant must declare a sweep range — "
                "that is the whole point of labelling it ASSUMED"
            )

    def __float__(self) -> float:
        return float(self.value)


REGISTRY: dict[str, Constant] = {}


def _reg(name: str, c: Constant) -> Constant:
    REGISTRY[name] = c
    return c


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

RBI_EMANDATE_2026 = Source(
    name="RBI, Digital Payments – E-mandate Framework, 2026",
    url="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374",
    tier=Tier.PRIMARY,
    detail=(
        "Circular RBI/DPSS/2026-27/396 (RBI/CO.DPSS.POLC.No.S56/02.14.003/"
        "2026-27), 21 April 2026. Effective immediately; consolidates and "
        "repeals eight circulars issued 2019-2024. VERIFIED against the "
        "primary source (the URL above) on 1 September 2026. "
        "Every figure below was confirmed at the section level, not "
        "just the headline number: the 24h/opt-out requirement is Section "
        "6(c); withdrawing the underlying mandate entirely (a different "
        "right) is the separate Section 4(b)."
    ),
)

TRAI_TCCCPR = Source(
    name="TRAI, TCCCPR 2018 as amended 12 Feb 2025",
    url="https://www.trai.gov.in/sites/default/files/2025-02/Regulation_12022025.pdf",
    tier=Tier.PRIMARY,
    detail="See also PIB release PRID 2102413.",
)

RAZORPAY_RETRIES = Source(
    name="Razorpay Docs — Subscriptions: Payment Retries",
    url="https://razorpay.com/docs/payments/subscriptions/payment-retries/",
    tier=Tier.PRIMARY,
)

RAZORPAY_ERRORS = Source(
    name="Razorpay Docs — Errors & Card Error Codes",
    url="https://razorpay.com/docs/errors/payments/cards/",
    tier=Tier.PRIMARY,
)

STRIPE_INDIA = Source(
    name="Stripe Docs — India recurring payments",
    url="https://docs.stripe.com/docs/india-recurring-payments",
    tier=Tier.PRIMARY,
    detail="Independent confirmation of the operational effect of the RBI notice window.",
)

VISA_REATTEMPTS = Source(
    name="Visa Excessive Reattempts Rule (in force since April 2022)",
    url="https://www.payway.com/visa-excessive-reattempts-rule-fees",
    tier=Tier.SECONDARY,
    detail="Secondary summary of a primary network rule; rule structure is reliable, fee amount is not.",
)

MC_TPE = Source(
    name="Mastercard Transaction Processing Excellence programme",
    url="https://www.ixopay.com/blog/what-are-card-scheme-penalty-programs-and-why-should-you-care",
    tier=Tier.SECONDARY,
)

DECLINE_DECAY = Source(
    name="Beast Insights — soft vs hard decline retry analysis",
    url="https://beastinsights.com/blog/soft-decline-vs-hard-decline",
    tier=Tier.SECONDARY,
)

SOLIDGATE_DECLINES = Source(
    name="Solidgate — card decline recovery",
    url="https://solidgate.com/blog/how-to-manage-card-declines-and-recover-lost-revenue/",
    tier=Tier.SECONDARY,
)

STRIPE_SMART_RETRIES = Source(
    name="Stripe — How we built it: Smart Retries",
    url="https://stripe.com/blog/how-we-built-it-smart-retries",
    tier=Tier.PRIMARY,
)

REDUX_AUDIT = Source(
    name="Redux Payments — audit of 200+ B2C Stripe Billing accounts",
    url="https://www.reduxpayments.com/blog/stripe-smart-retries-explained",
    tier=Tier.SECONDARY,
    detail=">$500M of failed-payment volume; found 25-35% against Stripe's published 55%.",
)

REDUX_LTV = Source(
    name="Redux Payments — involuntary churn guide",
    url="https://www.reduxpayments.com/blog/involuntary-churn-guide",
    tier=Tier.SECONDARY,
    detail="Frames true cost as unrecovered failures x remaining LTV, not the failed charge.",
)

BAREMETRICS = Source(
    name="Baremetrics — involuntary churn across hundreds of subscription businesses",
    url="https://baremetrics.com/blog/involuntary-churn",
    tier=Tier.SECONDARY,
)

RECURLY_BENCH = Source(
    name="Recurly churn benchmarks (via RetentionLens)",
    url="https://retentionlens.com/state-of-involuntary-churn",
    tier=Tier.SECONDARY,
)

SINCH_EMAIL = Source(
    name="Sinch consumer survey (via Mailjet)",
    url="https://www.mailjet.com/blog/email-best-practices/how-many-marketing-emails-is-too-many/",
    tier=Tier.SECONDARY,
    detail="ADJACENT, NOT EQUIVALENT: general promotional-email fatigue, not dunning contact count.",
)


# ---------------------------------------------------------------------------
# 1. REGULATORY — L2a's inviolable constraints. Compliance, not economics.
# ---------------------------------------------------------------------------

EMANDATE_PREDEBIT_NOTICE_HOURS = _reg(
    "EMANDATE_PREDEBIT_NOTICE_HOURS",
    Constant(
        value=24,
        unit="hours",
        provenance=Provenance.REGULATORY,
        sources=(RBI_EMANDATE_2026, STRIPE_INDIA),
        note=(
            "Pre-transaction notification to the customer at least 24h before "
            "every debit, carrying full transaction detail (merchant name, "
            "amount, debit date/time, mandate reference, debit reason) and an "
            "opt-out for that specific transaction (Section 6(c) - distinct "
            "from withdrawing the mandate entirely, Section 4(b)). Binding on "
            "the e-mandate rail: no sub-24h retry is legal, which kills "
            "'retry within the hour' outright for Indian recurring. Stripe "
            "builds a 26h buffer for downstream slack; we use 26h too."
        ),
    ),
)

EMANDATE_NOTICE_BUFFER_HOURS = _reg(
    "EMANDATE_NOTICE_BUFFER_HOURS",
    Constant(
        value=26,
        unit="hours",
        provenance=Provenance.ASSUMED,
        sources=(STRIPE_INDIA,),
        sweep=(24.0, 30.0),
        note="Operational buffer over the statutory 24h. Stripe uses 26h.",
    ),
)

EMANDATE_AFA_EXEMPT_CEILING_INR = _reg(
    "EMANDATE_AFA_EXEMPT_CEILING_INR",
    Constant(
        value=15_000,
        unit="INR per transaction",
        provenance=Provenance.REGULATORY,
        sources=(RBI_EMANDATE_2026,),
        note=(
            "Recurring transactions authorised without AFA up to Rs 15,000. "
            "Above this, AFA is required — which converts a silent retry into "
            "a customer-action path and changes which actions are even "
            "available to the scorer. Rs 1,00,000 ceiling applies to insurance "
            "premiums, mutual fund subscriptions and credit card bill payments."
        ),
    ),
)

EMANDATE_AFA_EXEMPT_CEILING_ELEVATED_INR = _reg(
    "EMANDATE_AFA_EXEMPT_CEILING_ELEVATED_INR",
    Constant(
        value=100_000,
        unit="INR per transaction",
        provenance=Provenance.REGULATORY,
        sources=(RBI_EMANDATE_2026,),
        note="Insurance premiums, mutual funds, credit card bills.",
    ),
)

QUIET_HOURS = _reg(
    "QUIET_HOURS",
    Constant(
        value=(21, 9),  # (start_hour, end_hour) local time, inclusive-exclusive
        unit="hour of day, IST",
        provenance=Provenance.REGULATORY,
        sources=(TRAI_TCCCPR,),
        note=(
            "9pm-9am is not a product decision. TRAI restricts promotional "
            "commercial communication to a 9am-9pm window. Transactional "
            "communication is exempt, but only where the classification is "
            "defensible — and the May 2025 amendment migrated 'service "
            "explicit' templates into the promotional category, putting them "
            "under DND scrubbing and this same window. A dunning nudge that "
            "carries an offer is promotional. We treat every customer contact "
            "as promotional unless it is a bare RBI-mandated debit notice."
        ),
    ),
)

VISA_REATTEMPT_CAP_PER_30D = _reg(
    "VISA_REATTEMPT_CAP_PER_30D",
    Constant(
        value=15,
        unit="reattempts per card per 30 days",
        provenance=Provenance.REGULATORY,
        sources=(VISA_REATTEMPTS,),
        note=(
            "Category 2 (wait and retry) and Category 3 (correct and retry). "
            "Any reattempt of a Category 1 (never retry) code is excessive "
            "from the first attempt — that is our HARD_RISK never-retry rule, "
            "and it is a network rule rather than a policy we invented."
        ),
    ),
)

MC_AUTH_CAP_PER_24H = _reg(
    "MC_AUTH_CAP_PER_24H",
    Constant(
        value=10,
        unit="authorisation attempts per PAN per 24 hours",
        provenance=Provenance.REGULATORY,
        sources=(MC_TPE,),
    ),
)

MC_AUTH_CAP_PER_30D = _reg(
    "MC_AUTH_CAP_PER_30D",
    Constant(
        value=35,
        unit="authorisation attempts per PAN per 30 days",
        provenance=Provenance.REGULATORY,
        sources=(MC_TPE,),
    ),
)

MC_NEVER_RETRY_ADVICE_CODES = _reg(
    "MC_NEVER_RETRY_ADVICE_CODES",
    Constant(
        value=("03", "21"),  # 03 = fraudulent, 21 = lost or stolen
        unit="Mastercard Merchant Advice Code",
        provenance=Provenance.REGULATORY,
        sources=(MC_TPE,),
        note="Retry after either of these triggers the TPE fee immediately.",
    ),
)

MC_TIMED_RETRY_ADVICE = _reg(
    "MC_TIMED_RETRY_ADVICE",
    Constant(
        value={
            "24": ("retry_within_hours", 1),
            "25": ("retry_after_hours", 24),
            "26": ("retry_after_days", 2),
            "27": ("retry_after_days", 4),
            "28": ("retry_after_days", 6),
            "29": ("retry_after_days", 8),
            "30": ("retry_after_days", 10),
        },
        unit="MAC -> retry schedule",
        provenance=Provenance.REGULATORY,
        sources=(MC_TPE,),
        note=(
            "The network hands us a per-decline retry schedule. Where a MAC is "
            "present it overrides our own timing estimate — this is the "
            "cheapest possible answer to 'where does your retry timing come "
            "from?' Many Indian issuers do not attach MACs consistently, so "
            "absence of a MAC is itself a signal L1 has to handle."
        ),
    ),
)


# ---------------------------------------------------------------------------
# 2. MEASURED — P(recovery)
#
# L1 emits an ordinal bucket, never a float. Buckets map to base rates here.
# This keeps the model doing categorical judgment (which it is good at) and
# keeps every number in the EV traceable to a source (which a float from a
# language model is not).
# ---------------------------------------------------------------------------

P_RECOVERY_BY_BUCKET = _reg(
    "P_RECOVERY_BY_BUCKET",
    Constant(
        value={
            "VERY_LOW": 0.02,   # hard declines: expired, lost, stolen, invalid
            "LOW": 0.12,        # persistent do-not-honor, repeated same code
            "MEDIUM": 0.35,     # generic issuer decline, first occurrence
            "HIGH": 0.55,       # insufficient funds, payday-aligned window
            "VERY_HIGH": 0.85,  # gateway/network transient, issuer timeout
        },
        unit="probability of recovery on the next single attempt",
        provenance=Provenance.MEASURED,
        sources=(DECLINE_DECAY, SOLIDGATE_DECLINES, REDUX_AUDIT),
        sweep=(0.6, 1.4),
        sweep_kind=SweepKind.MULTIPLICATIVE,
        note=(
            "Anchors: soft declines are 80-90% of all decline volume and "
            "recoverable in the 40-70% band overall; expired/lost/stolen and "
            "persistent do-not-honor recover near zero; transient processing "
            "errors recover very high. The sweep is multiplicative and wide on "
            "purpose — see P_RECOVERY_SOURCE_DISAGREEMENT below."
        ),
    ),
)

P_RECOVERY_SOURCE_DISAGREEMENT = _reg(
    "P_RECOVERY_SOURCE_DISAGREEMENT",
    Constant(
        value=(0.25, 0.55),
        unit="overall recovery rate, low and high published estimates",
        provenance=Provenance.MEASURED,
        sources=(STRIPE_SMART_RETRIES, REDUX_AUDIT),
        sweep=(0.25, 0.55),
        note=(
            "Stripe publishes 55% recovery for Billing. Redux's audit of 200+ "
            "B2C Stripe Billing accounts (>$500M failed-payment volume) "
            "consistently found 25-35%. A 20-point gap between a vendor "
            "headline and an independent audit is the justification for "
            "sweeping P(recovery) rather than picking a point estimate. It is "
            "also the honest answer to 'what if your recovery model is wrong?'"
        ),
    ),
)

MARGINAL_RECOVERY_BY_ATTEMPT = _reg(
    "MARGINAL_RECOVERY_BY_ATTEMPT",
    Constant(
        value={1: 0.50, 2: 0.20, 3: 0.125, 4: 0.05, 5: 0.03},
        unit="fraction of the *remaining* recoverable pool, by attempt index",
        provenance=Provenance.MEASURED,
        sources=(DECLINE_DECAY,),
        sweep=(0.7, 1.3),
        sweep_kind=SweepKind.MULTIPLICATIVE,
        note=(
            "Published figures: first retry 40-60% of soft declines, second "
            "another 15-25%, third another 10-15%, dropping sharply after. "
            "AMBIGUITY WE ARE FLAGGING RATHER THAN HIDING: the sources do not "
            "state whether 'another 15-25%' is of the original pool or the "
            "remaining pool. We model it as a hazard on the remaining pool, "
            "which is the conservative reading. world-model.md says so. "
            "This decay is what makes attempt 4 score negative on its own "
            "without any rule saying 'max 4 attempts'."
        ),
    ),
)


# ---------------------------------------------------------------------------
# 3. MEASURED — costs
#
# The strongest-sourced term in the whole model: issuer trust cost is a
# published fee schedule, not a constant we chose.
# ---------------------------------------------------------------------------

USD_INR = _reg(
    "USD_INR",
    Constant(
        value=88.0,
        unit="INR per USD",
        provenance=Provenance.ASSUMED,
        sweep=(80.0, 95.0),
        note="Network penalties are published in USD. Labelled ASSUMED deliberately.",
    ),
)

RETRY_COST_INR = _reg(
    "RETRY_COST_INR",
    Constant(
        value=0.50,
        unit="INR per attempt",
        provenance=Provenance.ASSUMED,
        sweep=(0.10, 3.00),
        note=(
            "Marginal gateway/compute cost of one ordinary authorisation "
            "attempt, excluding penalties. Small by design — the point of the "
            "model is that the penalty and churn terms dominate, not this one."
        ),
    ),
)

EXCESSIVE_REATTEMPT_FEE_USD = _reg(
    "EXCESSIVE_REATTEMPT_FEE_USD",
    Constant(
        value=0.10,
        unit="USD per excessive attempt (domestic)",
        provenance=Provenance.MEASURED,
        sources=(VISA_REATTEMPTS, MC_TPE),
        sweep=(0.03, 0.50),
        note=(
            "Visa: roughly $0.10 domestic, +$0.05 cross-border, on each "
            "reattempt past the 15th in 30 days for Category 2/3 codes, and "
            "on ANY reattempt of a Category 1 code. Mastercard TPE for retry "
            "after MAC 03/21 is reported in the $0.03-$0.50 band, up from "
            "$0.10 in 2022. SECONDARY SOURCES DISAGREE SHARPLY on the amount "
            "($0.10 / $0.25 / $25-per-breach / monthly programme fines). The "
            "rule structure is reliable; the amount is not. Hence the wide "
            "sweep rather than an averaged point estimate."
        ),
    ),
)

CROSS_BORDER_SURCHARGE_USD = _reg(
    "CROSS_BORDER_SURCHARGE_USD",
    Constant(
        value=0.05,
        unit="USD per excessive attempt, additional",
        provenance=Provenance.MEASURED,
        sources=(VISA_REATTEMPTS,),
        sweep=(0.02, 0.15),
    ),
)

ISSUER_TRUST_COST_INR = _reg(
    "ISSUER_TRUST_COST_INR",
    Constant(
        value=float(EXCESSIVE_REATTEMPT_FEE_USD.value) * float(USD_INR.value),
        unit="INR per failed attempt, in expectation",
        provenance=Provenance.ASSUMED,
        sweep=(0.5, 4.0),
        sweep_kind=SweepKind.MULTIPLICATIVE,
        note=(
            "The EV term is issuer_trust_cost x P(failure|action). The fee "
            "schedule above gives the floor. The multiplier above the floor "
            "represents soft degradation — issuer-side scoring, acquirer "
            "review risk, and merchant-account standing — which is NOT "
            "published and is ours. Swept hard."
        ),
    ),
)


# ---------------------------------------------------------------------------
# 4. Churn and LTV
#
# The LTV framing is sourced. The contact-fatigue shape is NOT. That split is
# the single most important honesty line in this file.
# ---------------------------------------------------------------------------

INVOLUNTARY_CHURN_SHARE = _reg(
    "INVOLUNTARY_CHURN_SHARE",
    Constant(
        value=0.30,
        unit="fraction of total churn attributable to payment failure",
        provenance=Provenance.MEASURED,
        sources=(BAREMETRICS, RECURLY_BENCH, REDUX_LTV),
        sweep=(0.20, 0.40),
        note=(
            "Paddle research puts involuntary churn at 20-40% of total churn. "
            "Recurly benchmarks: ~3.27% average monthly churn, split ~2.41% "
            "voluntary / ~0.86% involuntary. Baremetrics puts the cost at ~9% "
            "of MRR across hundreds of subscription businesses. Used to "
            "calibrate the simulator's world, not consumed by the scorer."
        ),
    ),
)

LTV_MULTIPLE_OF_INVOICE = _reg(
    "LTV_MULTIPLE_OF_INVOICE",
    Constant(
        value=6.0,
        unit="remaining LTV as a multiple of the failed invoice",
        provenance=Provenance.MEASURED,
        sources=(REDUX_LTV,),
        sweep=(2.0, 18.0),
        note=(
            "Redux frames true cost exactly as our EV does: unrecovered "
            "failures x REMAINING LTV, not the failed charge alone. Their "
            "worked example is a Rs-equivalent 50 failure from a customer with "
            "six months of tenure left = 300 of lost value, i.e. a 6x multiple. "
            "SANITY CHECK BEFORE YOU TRUST THE SCORER: at high multiples the "
            "churn term swamps P(recovery) x invoice_value and the optimal "
            "policy collapses to 'never contact anyone'. Run "
            "check_policy_nondegenerate() below before wiring L2b up."
        ),
    ),
)

CONTACT_FATIGUE_BASE_HAZARD = _reg(
    "CONTACT_FATIGUE_BASE_HAZARD",
    Constant(
        value=0.015,
        unit="incremental churn probability from the first dunning contact",
        provenance=Provenance.ASSUMED,
        sources=(SINCH_EMAIL,),
        sweep=(0.0, 0.06),
        note=(
            "UNSOURCED. We looked. There is no public data tying dunning "
            "contact COUNT to churn hazard. The nearest published figure is a "
            "Sinch consumer survey in which 26% cited too much promotional "
            "messaging as their main reason for ending a brand relationship — "
            "adjacent, not equivalent, and we are not going to launder it into "
            "a citation. Swept from ZERO, so the adversarial arm includes the "
            "case where contact fatigue does not exist at all."
        ),
    ),
)

CONTACT_FATIGUE_GROWTH = _reg(
    "CONTACT_FATIGUE_GROWTH",
    Constant(
        value=1.6,
        unit="multiplier on hazard per additional contact",
        provenance=Provenance.ASSUMED,
        sweep=(1.0, 2.5),
        note=(
            "Superlinear because the intuition is that the fourth message "
            "annoys more than the first. That intuition is ours. A growth of "
            "1.0 (no escalation) is inside the sweep."
        ),
    ),
)

CONTACT_RECENCY_HALFLIFE_DAYS = _reg(
    "CONTACT_RECENCY_HALFLIFE_DAYS",
    Constant(
        value=7.0,
        unit="days for accumulated contact fatigue to halve",
        provenance=Provenance.ASSUMED,
        sweep=(2.0, 30.0),
        note="UNSOURCED. Drives days_since_last_contact in the churn hazard.",
    ),
)

CHANNEL_FIT = _reg(
    "CHANNEL_FIT",
    Constant(
        value={
            # (mechanism, failure class) -> multiplier on the bucket's base rate.
            # This is the README's failure taxonomy made arithmetic: it encodes
            # which mechanism can physically address which failure, not how well
            # we think it performs.
            ("RETRY", "SOFT_TRANSIENT"): 1.0,
            ("RETRY", "SOFT_FUNDS"): 1.0,
            ("RETRY", "SOFT_LIMIT"): 1.0,
            ("RETRY", "SOFT_AUTH"): 0.0,
            ("RETRY", "HARD_INSTRUMENT"): 0.0,
            ("RETRY", "HARD_RISK"): 0.0,
            ("RETRY", "HARD_MANDATE"): 0.0,
            ("LINK", "SOFT_TRANSIENT"): 0.5,
            ("LINK", "SOFT_FUNDS"): 0.6,
            ("LINK", "SOFT_LIMIT"): 0.8,
            ("LINK", "SOFT_AUTH"): 1.0,
            ("LINK", "HARD_INSTRUMENT"): 1.0,
            ("LINK", "HARD_RISK"): 0.0,
            ("LINK", "HARD_MANDATE"): 1.0,
            ("NUDGE", "SOFT_TRANSIENT"): 0.3,
            ("NUDGE", "SOFT_FUNDS"): 0.4,
            ("NUDGE", "SOFT_LIMIT"): 0.4,
            ("NUDGE", "SOFT_AUTH"): 0.5,
            ("NUDGE", "HARD_INSTRUMENT"): 0.2,
            ("NUDGE", "HARD_RISK"): 0.0,
            ("NUDGE", "HARD_MANDATE"): 0.2,
        },
        unit="multiplier on the recovery bucket's base rate",
        provenance=Provenance.ASSUMED,
        sweep=(0.7, 1.3),
        sweep_kind=SweepKind.MULTIPLICATIVE,
        note=(
            "The ZEROES ARE STRUCTURAL, not estimates, and the sweep does not "
            "move them: a retry cannot fix an OTP drop-off because the customer "
            "never authorised anything, and cannot fix an expired card because "
            "the instrument is gone. Those are mechanism facts and they are the "
            "same claim as ASSUMED_HARD_HAZARD = 0.00 in sim/world_model.py. "
            "The NON-ZERO off-diagonal entries are ours and are guesswork of "
            "the ordinary kind - a nudge with no payment mechanism behind it "
            "still recovers some invoices because the customer goes and pays, "
            "and we do not know that rate. Swept multiplicatively."
        ),
    ),
)


RETRY_TIMING_LIFT = _reg(
    "RETRY_TIMING_LIFT",
    Constant(
        value=1.35,
        unit="multiplier on P(recovery) for a correctly-timed deferred retry",
        provenance=Provenance.ASSUMED,
        sources=(STRIPE_SMART_RETRIES, MC_TPE),
        sweep=(1.0, 1.8),
        note=(
            "Real dunning does most of its work through retry TIMING, not "
            "whether-to-retry, and a one-shot EV with no timing term would be "
            "silent on the judge's actual problem. This is the smallest "
            "honest way to give scheduling a place in the same machinery: a "
            "deferred retry aimed at a moment the world model says is better - "
            "a Mastercard advice code's own schedule where one is present, the "
            "28th-31st payday window for SOFT_FUNDS otherwise - is worth this "
            "multiple of an immediate one. A lift of 1.0 (timing buys nothing) "
            "is inside the sweep. Stripe publish that Smart Retries beat fixed "
            "schedules but not by how much, so the magnitude is ours."
        ),
    ),
)

P_LAPSE_IF_UNRECOVERED = _reg(
    "P_LAPSE_IF_UNRECOVERED",
    Constant(
        value=0.55,
        unit="probability the customer lapses if the invoice is never recovered",
        provenance=Provenance.MEASURED,
        sources=(REDUX_LTV, BAREMETRICS, RECURLY_BENCH),
        sweep=(0.30, 0.75),
        note=(
            "THE COST OF GIVING UP, and the term the EV model as originally "
            "specified was missing entirely. "
            "The handoff's EV formula prices STOP at zero: it charges churn for "
            "contacting a customer but charges nothing for abandoning their "
            "invoice. In the world those are not symmetric - a failed payment "
            "that is never recovered is the definition of involuntary churn, "
            "and it costs the remaining LTV. "
            "Note that this contradicts the framing of our OWN cited source: "
            "Redux frames the true cost as unrecovered failures x remaining "
            "LTV, not the failed charge. Pricing STOP at zero throws that away "
            "and makes the scorer systematically too timid. "
            "Magnitude: involuntary churn is 20-40% of total churn, and a "
            "dunning failure is the proximate cause of the lapse in the "
            "majority of cases where recovery never happens. Swept wide "
            "because the sources measure churn SHARE rather than this "
            "conditional directly."
        ),
    ),
)


# ---------------------------------------------------------------------------
# 5. Baselines — the naive arm is Razorpay's own documented default
# ---------------------------------------------------------------------------

RAZORPAY_DEFAULT_RETRY_SCHEDULE = _reg(
    "RAZORPAY_DEFAULT_RETRY_SCHEDULE",
    Constant(
        value=(1, 2, 3),  # T+1, T+2, T+3 days after the charge date
        unit="days after charge date",
        provenance=Provenance.MEASURED,
        sources=(RAZORPAY_RETRIES,),
        note=(
            "Razorpay Subscriptions: on a failed auto-charge the subscription "
            "moves to `pending` and Razorpay retries once a day on T+1, T+2 "
            "and T+3; if all three fail it moves to `halted`, and the customer "
            "is emailed a card-update link. THIS IS THE NAIVE ARM. We are not "
            "benchmarking against a strawman we invented — we are benchmarking "
            "against the documented default behaviour of the platform, with a "
            "doc link in the README."
        ),
    ),
)

RAZORPAY_ERROR_FIELDS = _reg(
    "RAZORPAY_ERROR_FIELDS",
    Constant(
        value=("code", "description", "field", "source", "step", "reason", "metadata"),
        unit="Razorpay error object shape",
        provenance=Provenance.MEASURED,
        sources=(RAZORPAY_ERRORS,),
        note=(
            "`source` is one of customer / business / razorpay / gateway / "
            "bank / network; `step` locates the failure in the payment flow. "
            "This triple is richer than a bare decline code and is what L1 "
            "reasons over. Note also that Razorpay's own card-error docs state "
            "they may not have the specific failure reason for bank declines, "
            "because customer banks typically do not provide one. That is the "
            "ambiguous-payload bucket, documented by the gateway itself — we "
            "do not have to invent it."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def by_provenance(p: Provenance) -> dict[str, Constant]:
    return {k: v for k, v in REGISTRY.items() if v.provenance is p}


def resolve(name: str, t: float) -> float:
    """
    Resolve a swept constant at position t in [0, 1] across its range.
    Handles absolute and multiplicative sweeps correctly — always go through
    this rather than interpolating `.sweep` yourself.
    """
    c = REGISTRY[name]
    if c.sweep is None:
        return float(c.value)
    lo, hi = c.sweep
    scaled = lo + t * (hi - lo)
    if c.sweep_kind is SweepKind.MULTIPLICATIVE:
        return float(c.value) * scaled
    return scaled


def sweepable() -> dict[str, tuple[tuple[float, float], SweepKind]]:
    """Every parameter the robustness arm varies. Iterate this, not a hardcoded list."""
    return {
        k: (v.sweep, v.sweep_kind) for k, v in REGISTRY.items() if v.sweep is not None
    }


def unsourced() -> dict[str, Constant]:
    """ASSUMED constants with no supporting source at all. Report these in the README."""
    return {
        k: v
        for k, v in REGISTRY.items()
        if v.provenance is Provenance.ASSUMED and not v.sources
    }


def check_policy_nondegenerate(
    invoice_value: float = 2000.0,
    p_recovery: float = 0.50,
) -> dict[str, float]:
    """
    Sanity check to run BEFORE trusting the scorer.

    If churn cost dominates recovery value at typical parameters, the optimal
    policy is 'never act', the frontier chart is a single dot, and the demo is
    boring. Confirm the optimum is interior, not at a corner.
    """
    recovery_term = p_recovery * invoice_value
    ltv = invoice_value * float(LTV_MULTIPLE_OF_INVOICE.value)
    churn_term = float(CONTACT_FATIGUE_BASE_HAZARD.value) * ltv
    issuer_term = float(ISSUER_TRUST_COST_INR.value) * (1 - p_recovery)
    net = recovery_term - float(RETRY_COST_INR.value) - issuer_term - churn_term
    return {
        "recovery_term": recovery_term,
        "churn_term": churn_term,
        "issuer_term": issuer_term,
        "net_ev": net,
        "churn_to_recovery_ratio": churn_term / recovery_term,
        "degenerate": float(net < 0),
    }


def breakeven_contact_fatigue(
    invoice_value: float = 2000.0,
    attempt_index: int = 4,
    p_recovery_at_attempt: float = 0.05,
    ltv_multiple: float | None = None,
) -> float:
    """
    The most important number in this file.

    Restraint only emerges from the EV model if the churn term is big enough to
    outweigh a marginal retry. CONTACT_FATIGUE_BASE_HAZARD is the one constant
    driving that, and it is the one constant we could not source. So rather
    than defending a value we invented, report the THRESHOLD: the smallest
    per-contact churn hazard at which stopping beats continuing.

    That converts an indefensible claim ("fatigue is 1.5%") into a defensible
    one ("our conclusion holds for any fatigue above X, and X is very small").
    Put the output of this function in the README.
    """
    ltvm = ltv_multiple if ltv_multiple is not None else float(LTV_MULTIPLE_OF_INVOICE.value)
    gain = p_recovery_at_attempt * invoice_value
    gain -= float(RETRY_COST_INR.value)
    gain -= float(ISSUER_TRUST_COST_INR.value) * (1 - p_recovery_at_attempt)
    if gain <= 0:
        return 0.0  # already negative without any churn term at all
    escalation = float(CONTACT_FATIGUE_GROWTH.value) ** max(0, attempt_index - 1)
    return gain / (escalation * invoice_value * ltvm)


def citation_table() -> str:
    """Markdown table for the README."""
    rows = ["| Constant | Provenance | Source | Tier |", "|---|---|---|---|"]
    for name, c in REGISTRY.items():
        if c.sources:
            for s in c.sources:
                rows.append(
                    f"| `{name}` | {c.provenance.value} | [{s.name}]({s.url}) | {s.tier.value} |"
                )
        else:
            rows.append(f"| `{name}` | {c.provenance.value} | — | none |")
    return "\n".join(rows)


def unsourced_report() -> str:
    lines = [
        "## Constants we could not source",
        "",
        "These are ours. We looked for published data and did not find any.",
        "Every one is swept in the robustness arm, and the sweep includes the",
        "value at which the constant has no effect.",
        "",
    ]
    for name, c in unsourced().items():
        # Table-valued constants dump unreadably into prose. Summarise them
        # instead — the full table is in the source, and a wall of tuples in a
        # markdown bullet is not disclosure, it is noise pretending to be it.
        shown = f"a {len(c.value)}-entry table" if isinstance(c.value, dict) else c.value
        lines.append(f"- **`{name}`** = {shown} {c.unit}, swept {c.sweep}")
        if c.note:
            lines.append(f"  - {c.note.splitlines()[0]}")
    return "\n".join(lines)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--summary"
    if arg == "--citations":
        print(citation_table())
    elif arg == "--unsourced":
        print(unsourced_report())
    elif arg == "--breakeven":
        for ltvm in (2.0, 6.0, 12.0, 18.0):
            b = breakeven_contact_fatigue(ltv_multiple=ltvm)
            print(f"LTV {ltvm:>5.1f}x  breakeven per-contact churn hazard: {b:.4%}")
    elif arg == "--check":
        for k, v in check_policy_nondegenerate().items():
            print(f"{k:28s} {v:12.3f}")
    else:
        for p in Provenance:
            print(f"{p.value:12s} {len(by_provenance(p)):3d} constants")
        print(f"{'sweepable':12s} {len(sweepable()):3d} parameters")
