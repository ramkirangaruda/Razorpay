"""
The evaluation batch, with ground truth on both sides of every case.

The previous generator wrote the true failure class into every row and stopped
there. That makes L1 unscoreable in both directions: you can see when the agent
was too aggressive (L2a vetoed it) but never when it was too timid, because
nothing records what the invoice would have done if approached differently.
This one records that.

------------------------------------------------------------------------------
Composition — and why the boring 40% is reported rather than buried
------------------------------------------------------------------------------

    40%  CLEAN     structured, unambiguous decline codes.
    35%  AMBIGUOUS generic, missing, or self-contradicting error payloads.
    25%  CONTEXT   classification is obvious; the right ACTION depends on the
                   customer's history.

On the clean 40% a lookup table ties the full agent, and the honest thing is to
say so. "The model adds nothing on 40% of traffic, and here is the 60% where it
does" is a far more convincing sentence than a claimed uniform win, because the
first is checkable and the second invites the judge to go looking for the
smoothing. The ablation would find it anyway.

The AMBIGUOUS bucket is not an invented difficulty. Razorpay's own card-error
documentation states they may not have a specific failure reason for bank
declines, because customer banks typically do not provide one. The gateway
documents the ambiguity; we only have to reproduce it.

------------------------------------------------------------------------------
Ground truth
------------------------------------------------------------------------------

Each case carries what the invoice would actually have done:

    true_class            the real failure class, whatever the payload says
    retry_attempts_needed how many pure retries would have cleared it (None =
                          never, within the horizon)
    link_recovers         whether a payment link would have cleared it
    oracle_action         what a perfect agent would do first

These come from sim/world_model.py — the WORLD's hazard model — rolled at a
fixed seed. They are never computed from world_model_constants.py, which is the
AGENT's belief. Keeping the two apart is what makes the comparison in
sim/run_arms.py mean anything; see the §4 header in sim/world_model.py.

Labels here are exact by construction rather than hand-annotated, because the
data is synthetic. That is a real limitation and it is disclosed in
docs/world-model.md rather than presented as rigour.

Usage:
    python -m sim.generate_batch --n 120 --seed 42
"""

from __future__ import annotations

import argparse
import json
import os
import random
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from sim.world_model import (
    ASSUMED_WORLD_LTV_MULTIPLE_RANGE,
    MEASURED_AUTH_HAZARD_1,
    FailureClass,
    hazard_for_attempt,
)

# How many retries the oracle is allowed to imagine before declaring an invoice
# unrecoverable by retrying. Beyond this the marginal hazard is a rounding error.
RETRY_HORIZON = 6

ISSUERS = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "YES_BANK", "IDFC_FIRST", "PNB", "BOB", "INDUSIND"]

INSTRUMENT_BY_CLASS: dict[FailureClass, list[str]] = {
    FailureClass.SOFT_TRANSIENT: ["card", "upi", "netbanking"],
    FailureClass.SOFT_FUNDS: ["card", "upi"],
    FailureClass.SOFT_LIMIT: ["card", "upi"],
    FailureClass.SOFT_AUTH: ["card"],
    FailureClass.HARD_INSTRUMENT: ["card"],
    FailureClass.HARD_RISK: ["card", "upi"],
    FailureClass.HARD_MANDATE: ["card", "upi"],
}

AMOUNT_BANDS_PAISE = [(29900, 99900), (99900, 499900), (499900, 1999900)]

# ---------------------------------------------------------------------------
# Razorpay error objects
#
# Shape per Razorpay's error documentation: code, description, field, source,
# step, reason, metadata. `source` is one of customer / business / razorpay /
# gateway / bank / network and `step` locates the failure in the payment flow.
# That triple is richer than a bare decline code, and it is the signal that
# makes an LLM classifier worth having over a dictionary.
# ---------------------------------------------------------------------------

CLEAN_ERRORS: dict[FailureClass, list[dict]] = {
    FailureClass.SOFT_TRANSIENT: [
        dict(code="GATEWAY_ERROR", description="Payment processing failed due to error at bank or wallet gateway",
             source="gateway", step="payment_authorization", reason="gateway_technical_error"),
        dict(code="GATEWAY_ERROR", description="Payment was not completed on time",
             source="gateway", step="payment_authorization", reason="payment_timed_out"),
        dict(code="BAD_REQUEST_ERROR", description="Issuer or switch is inoperative",
             source="bank", step="payment_authorization", reason="issuer_not_available"),
    ],
    FailureClass.SOFT_FUNDS: [
        dict(code="BAD_REQUEST_ERROR", description="Your payment failed because the account has insufficient funds",
             source="customer", step="payment_authorization", reason="payment_failed_insufficient_funds"),
        dict(code="BAD_REQUEST_ERROR", description="Card has insufficient balance to complete this transaction",
             source="customer", step="payment_authorization", reason="insufficient_funds"),
    ],
    FailureClass.SOFT_LIMIT: [
        dict(code="BAD_REQUEST_ERROR", description="Transaction amount exceeds the per-transaction limit set on the card",
             source="bank", step="payment_authorization", reason="payment_limit_exceeded"),
        dict(code="BAD_REQUEST_ERROR", description="Daily transaction limit for this account has been reached",
             source="bank", step="payment_authorization", reason="daily_limit_exceeded"),
    ],
    FailureClass.SOFT_AUTH: [
        dict(code="BAD_REQUEST_ERROR", description="Payment failed because the OTP was not entered in time",
             source="customer", step="payment_authentication", reason="payment_otp_timeout"),
        dict(code="BAD_REQUEST_ERROR", description="Customer cancelled the 3D Secure authentication",
             source="customer", step="payment_authentication", reason="payment_cancelled_by_customer"),
    ],
    FailureClass.HARD_INSTRUMENT: [
        dict(code="BAD_REQUEST_ERROR", description="The card used for this payment has expired",
             source="customer", step="payment_authorization", reason="payment_failed_card_expired"),
        dict(code="BAD_REQUEST_ERROR", description="Card is blocked by the issuing bank",
             source="bank", step="payment_authorization", reason="card_blocked"),
        dict(code="BAD_REQUEST_ERROR", description="The account linked to this card has been closed",
             source="bank", step="payment_authorization", reason="account_closed"),
    ],
    FailureClass.HARD_RISK: [
        dict(code="BAD_REQUEST_ERROR", description="Payment declined by the issuing bank due to suspected fraud",
             source="bank", step="payment_authorization", reason="payment_declined_risk", mac="03"),
        dict(code="BAD_REQUEST_ERROR", description="Card reported lost or stolen by the cardholder",
             source="bank", step="payment_authorization", reason="card_lost_or_stolen", mac="21"),
    ],
    FailureClass.HARD_MANDATE: [
        dict(code="BAD_REQUEST_ERROR", description="The e-mandate has been revoked by the customer",
             source="customer", step="payment_authorization", reason="mandate_revoked"),
        dict(code="BAD_REQUEST_ERROR", description="Mandate has exhausted its authorised debit count",
             source="business", step="payment_authorization", reason="mandate_exhausted"),
    ],
}

# The ambiguity Razorpay documents: a bank decline with no specific reason
# attached, because the customer's bank did not provide one.
GENERIC_BANK_DECLINE = dict(
    code="BAD_REQUEST_ERROR",
    description="Payment failed",
    source="bank",
    step="payment_authorization",
    reason="payment_failed",
)

# Descriptions that point somewhere other than the structured reason. These
# happen in the wild when a gateway maps an issuer's free-text response onto its
# own taxonomy imperfectly.
CONTRADICTORY_DESCRIPTIONS: dict[FailureClass, str] = {
    FailureClass.SOFT_FUNDS: "Card declined by issuer — please contact your bank",
    FailureClass.SOFT_AUTH: "Transaction could not be authorised at this time",
    FailureClass.HARD_INSTRUMENT: "Payment failed. Please retry after some time.",
    FailureClass.HARD_RISK: "Your payment could not be processed",
    FailureClass.SOFT_LIMIT: "Do not honour",
    FailureClass.SOFT_TRANSIENT: "Payment declined by the issuing bank",
    FailureClass.HARD_MANDATE: "Payment failed. Please retry after some time.",
}


@dataclass
class GroundTruth:
    """What the invoice would actually have done. The agent never sees this."""

    true_class: str
    retry_attempts_needed: int | None   # None = no number of retries clears it
    link_recovers: bool
    oracle_action: str
    ltv_multiple: float
    # Whether a lookup table on the structured payload gets the class right.
    # This is what makes the "rules-only ties on the clean 40%" claim checkable
    # rather than asserted.
    lookup_table_would_classify_correctly: bool


@dataclass
class Case:
    case_id: str
    bucket: str                 # CLEAN | AMBIGUOUS | CONTEXT
    invoice_id: str
    customer_id: str
    kind: str
    amount_paise: int
    currency: str
    mandate_id: str | None
    issuer: str
    instrument_type: str
    failed_at: str
    error: dict                 # the Razorpay error object, as L1 receives it
    mastercard_advice_code: str | None
    customer: dict              # tenure, prior contacts, payment history
    ground_truth: GroundTruth
    ambiguity: list[str] = field(default_factory=list)


def _oracle(rng: random.Random, fc: FailureClass) -> tuple[int | None, bool, str]:
    """
    Roll the WORLD's hazard model to decide what this invoice would really do.

    Retries are rolled attempt by attempt against the world's decaying hazard.
    The link path is separate: for the classes where the instrument or the
    authorisation is the problem, a link is the only mechanism that can work,
    and for the rest it is a weaker substitute for a retry.
    """
    # The RETRY route. Only classes with a mechanism a re-authorisation can
    # actually address are eligible.
    #
    # SOFT_AUTH is excluded deliberately, and getting this wrong was a real bug.
    # world_model.MEASURED_AUTH_HAZARD_1 = 0.55 describes a FRESH LINK — its own
    # docstring says "a fresh link removes the original friction" — but an
    # earlier version of this oracle rolled it as the retry hazard. That made
    # ground truth reward re-presenting a card whose owner walked away from the
    # OTP screen, which contradicts both the README's taxonomy and the
    # structural zero in CHANNEL_FIT, and it handed the naive arm a large block
    # of free recoveries for doing the one thing everyone agrees does not work.
    #
    # A retry cannot fix a failure where the customer never authorised anything.
    # The 0.55 belongs to the link route below, where it was always meant to be.
    retryable = fc in (
        FailureClass.SOFT_TRANSIENT,
        FailureClass.SOFT_FUNDS,
        FailureClass.SOFT_LIMIT,
    )
    attempts_needed: int | None = None
    if retryable:
        for attempt in range(1, RETRY_HORIZON + 1):
            if rng.random() < hazard_for_attempt(fc, attempt):
                attempts_needed = attempt
                break

    # The LINK route: the customer is asked to act.
    if fc is FailureClass.SOFT_AUTH:
        link_recovers = rng.random() < MEASURED_AUTH_HAZARD_1
    elif fc in (FailureClass.HARD_INSTRUMENT, FailureClass.HARD_MANDATE):
        link_recovers = rng.random() < 0.62
    elif fc is FailureClass.HARD_RISK:
        link_recovers = False
    else:
        link_recovers = rng.random() < 0.35

    if fc is FailureClass.HARD_RISK:
        oracle = "ESCALATE_HUMAN"
    elif fc.is_hard or fc is FailureClass.SOFT_AUTH:
        oracle = "REQUEST_INSTRUMENT_UPDATE" if link_recovers else "STOP_PERMANENT"
    elif attempts_needed is not None:
        oracle = "RETRY_SCHEDULED"
    elif link_recovers:
        oracle = "REQUEST_INSTRUMENT_UPDATE"
    else:
        oracle = "STOP_PERMANENT"

    return attempts_needed, link_recovers, oracle


def _customer_profile(rng: random.Random, bucket: str, amount_paise: int) -> dict:
    """
    Customer history. For CONTEXT cases this is what decides the action, so it
    is drawn to be genuinely discriminating rather than decorative — a
    long-tenure customer with a clean history and a first failure is a different
    decision from a three-week-old account on its fourth failed invoice, even
    when the decline code is identical.
    """
    if bucket == "CONTEXT":
        archetype = rng.choice(["loyal_first_failure", "chronic_failer", "new_and_shaky", "high_value_quiet"])
    else:
        archetype = rng.choice(["loyal_first_failure", "chronic_failer", "new_and_shaky", "high_value_quiet", "ordinary"])

    profiles = {
        "loyal_first_failure": dict(tenure_days=rng.randint(400, 1400), prior_failures_90d=0,
                                    prior_contacts_30d=0, days_since_last_contact=None,
                                    successful_payments=rng.randint(12, 40)),
        "chronic_failer": dict(tenure_days=rng.randint(200, 700), prior_failures_90d=rng.randint(3, 7),
                               prior_contacts_30d=rng.randint(2, 3), days_since_last_contact=float(rng.randint(1, 6)),
                               successful_payments=rng.randint(3, 10)),
        "new_and_shaky": dict(tenure_days=rng.randint(5, 60), prior_failures_90d=rng.randint(1, 3),
                              prior_contacts_30d=rng.randint(1, 2), days_since_last_contact=float(rng.randint(2, 12)),
                              successful_payments=rng.randint(0, 2)),
        "high_value_quiet": dict(tenure_days=rng.randint(300, 900), prior_failures_90d=rng.randint(0, 1),
                                 prior_contacts_30d=0, days_since_last_contact=None,
                                 successful_payments=rng.randint(8, 25)),
        "ordinary": dict(tenure_days=rng.randint(60, 500), prior_failures_90d=rng.randint(0, 2),
                         prior_contacts_30d=rng.choices([0, 1, 2], weights=[0.6, 0.28, 0.12])[0],
                         days_since_last_contact=float(rng.randint(3, 25)),
                         successful_payments=rng.randint(2, 18)),
    }
    p = dict(profiles[archetype])
    p["archetype"] = archetype
    if p["prior_contacts_30d"] == 0:
        p["days_since_last_contact"] = None
    # Invoice size relative to this customer's normal — a signal L1 can use and
    # a bare decline code cannot carry.
    p["typical_invoice_paise"] = int(amount_paise * rng.uniform(0.6, 1.7))
    return p


def _build_error(rng: random.Random, fc: FailureClass, bucket: str) -> tuple[dict, list[str], bool]:
    """
    Returns the error object, the ambiguity flags a careful reader would raise,
    and whether a lookup table on `reason` would still classify correctly.
    """
    template = dict(rng.choice(CLEAN_ERRORS[fc]))
    mac = template.pop("mac", None)
    flags: list[str] = []
    lookup_ok = True

    if bucket == "AMBIGUOUS":
        mode = rng.choice(["missing_reason", "description_contradicts_reason", "generic_bank_decline"])
        if mode == "missing_reason":
            # Razorpay documents this: banks frequently do not supply a reason.
            template["reason"] = None
            template["description"] = "Payment failed"
            flags.append("missing_reason")
            lookup_ok = False
        elif mode == "description_contradicts_reason":
            template["description"] = CONTRADICTORY_DESCRIPTIONS[fc]
            flags.append("description_contradicts_reason")
            # The structured reason is still right, so a lookup table survives
            # this one — the description is the misleading part. Recording that
            # honestly rather than counting every ambiguous case as a table
            # failure.
            lookup_ok = True
        else:
            template = dict(GENERIC_BANK_DECLINE)
            flags.append("generic_decline_no_specific_reason")
            flags.append("source_is_bank_no_detail")
            lookup_ok = False

    template["field"] = None
    template["metadata"] = {
        "payment_id": f"pay_{uuid.uuid4().hex[:14]}",
        "order_id": f"order_{uuid.uuid4().hex[:14]}",
    }
    return template, flags, lookup_ok


def generate_batch(n: int, seed: int, start: datetime | None = None) -> list[Case]:
    rng = random.Random(seed)
    start = start or datetime(2026, 8, 1, 9, 0, 0)

    # Bucket composition, per the evaluation design.
    n_clean = round(n * 0.40)
    n_ambig = round(n * 0.35)
    buckets = ["CLEAN"] * n_clean + ["AMBIGUOUS"] * n_ambig
    buckets += ["CONTEXT"] * (n - len(buckets))
    rng.shuffle(buckets)

    # India-adjusted decline mix, disclosed in docs/world-model.md §1.
    from sim.world_model import ASSUMED_INDIA_DECLINE_MIX

    classes = list(ASSUMED_INDIA_DECLINE_MIX)
    weights = list(ASSUMED_INDIA_DECLINE_MIX.values())

    n_customers = max(1, int(n * 0.6))
    customer_ids = [str(uuid.uuid4()) for _ in range(n_customers)]

    cases: list[Case] = []
    for i, bucket in enumerate(buckets):
        fc = rng.choices(classes, weights=weights, k=1)[0]
        # CONTEXT cases need a class where the action is genuinely open — if the
        # class already forces the action, customer history cannot be what
        # decides it and the bucket would be mislabelled.
        if bucket == "CONTEXT":
            fc = rng.choice([FailureClass.SOFT_FUNDS, FailureClass.SOFT_LIMIT,
                             FailureClass.SOFT_TRANSIENT, FailureClass.SOFT_AUTH])

        is_recurring = rng.random() < 0.40 or fc is FailureClass.HARD_MANDATE
        lo, hi = rng.choice(AMOUNT_BANDS_PAISE)
        amount_paise = rng.randint(lo, hi)

        error, flags, lookup_ok = _build_error(rng, fc, bucket)
        mac = None
        if fc is FailureClass.HARD_RISK and bucket != "AMBIGUOUS":
            mac = rng.choice(["03", "21"])
        elif fc in (FailureClass.SOFT_FUNDS, FailureClass.SOFT_TRANSIENT) and rng.random() < 0.35:
            # Many Indian issuers do not attach advice codes consistently, so
            # the absence of a MAC is itself a signal rather than a data gap.
            mac = rng.choice(["24", "25", "26", "27"])

        attempts, link_ok, oracle = _oracle(rng, fc)
        customer = _customer_profile(rng, bucket, amount_paise)

        day_offset = rng.randint(0, 29)
        failed_at = start + timedelta(days=day_offset, hours=rng.randint(-2, 14), minutes=rng.randint(0, 59))

        cases.append(
            Case(
                case_id=f"case_{i:04d}",
                bucket=bucket,
                invoice_id=str(uuid.uuid4()),
                customer_id=rng.choice(customer_ids),
                kind="RECURRING" if is_recurring else "ONE_TIME",
                amount_paise=amount_paise,
                currency="INR",
                mandate_id=(str(uuid.uuid4()) if is_recurring else None),
                issuer=rng.choice(ISSUERS),
                instrument_type=rng.choice(INSTRUMENT_BY_CLASS[fc]),
                failed_at=failed_at.isoformat(),
                error=error,
                mastercard_advice_code=mac,
                customer=customer,
                ambiguity=flags,
                ground_truth=GroundTruth(
                    true_class=fc.value,
                    retry_attempts_needed=attempts,
                    link_recovers=link_ok,
                    oracle_action=oracle,
                    ltv_multiple=round(rng.uniform(*ASSUMED_WORLD_LTV_MULTIPLE_RANGE), 2),
                    lookup_table_would_classify_correctly=lookup_ok,
                ),
            )
        )

    cases.sort(key=lambda c: c.failed_at)
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=120)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args()

    cases = generate_batch(args.n, args.seed)
    payload = {
        "seed": args.seed,
        "n": len(cases),
        "generated_at": datetime.utcnow().isoformat(),
        "world_model_ref": "sim/world_model.py (world truth; NOT the agent's beliefs)",
        "retry_horizon": RETRY_HORIZON,
        "cases": [asdict(c) for c in cases],
    }

    out_path = args.out or f"sim/data/batch_seed{args.seed}.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    by_bucket: dict[str, int] = {}
    by_class: dict[str, int] = {}
    recoverable = 0
    for c in cases:
        by_bucket[c.bucket] = by_bucket.get(c.bucket, 0) + 1
        by_class[c.ground_truth.true_class] = by_class.get(c.ground_truth.true_class, 0) + 1
        if c.ground_truth.retry_attempts_needed or c.ground_truth.link_recovers:
            recoverable += 1

    print(f"Wrote {len(cases)} cases (seed={args.seed}) to {out_path}")
    print("Bucket:", json.dumps(by_bucket))
    print("Class :", json.dumps(by_class))
    print(f"Truly recoverable by some route: {recoverable}/{len(cases)} ({recoverable/len(cases):.0%})")


if __name__ == "__main__":
    main()
