"""
Synthetic failure batch generator.

Draws a seeded batch of failed-payment "events" (invoice + first failure) using the disclosed
decline-mix in sim/world_model.py (ASSUMED_INDIA_DECLINE_MIX). This is the raw input all three
arms (naive / rules-only / full agent, build spec §7) run against — same batch, same seed, so the
comparison is apples-to-apples.

This module does NOT decide whether any given attempt recovers — that's sim/run_arms.py's job,
using world_model.hazard_for_attempt / timing_multiplier at simulation time, once an arm has
picked an action and (if it's a retry) a schedule. generate_batch.py only creates the population
of failures the arms will each independently try to work.

Usage:
    python -m sim.generate_batch --n 200 --seed 42 --out sim/data/batch_seed42.json
"""

from __future__ import annotations

import argparse
import json
import random
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sim.world_model import (
    ASSUMED_INDIA_DECLINE_MIX,
    DECLINE_REASON_SAMPLES,
    FailureClass,
)

# Representative Indian issuer set (mix of banks + a couple of gateways for SOFT_TRANSIENT cases).
# Not sourced from a specific market-share table — a plausible mix for demo/testing purposes only,
# unrelated to any recovery-probability claim (those live entirely in world_model.py).
ISSUERS = [
    "HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "YES_BANK", "IDFC_FIRST", "PNB", "BOB", "INDUSIND",
]

INSTRUMENT_BY_CLASS: dict[FailureClass, list[str]] = {
    FailureClass.SOFT_TRANSIENT: ["card", "upi", "netbanking"],
    FailureClass.SOFT_FUNDS: ["card", "upi"],
    FailureClass.SOFT_LIMIT: ["card", "upi"],
    FailureClass.SOFT_AUTH: ["card"],  # 3DS/OTP is a card-rail concept
    FailureClass.HARD_INSTRUMENT: ["card"],
    FailureClass.HARD_RISK: ["card", "upi"],
    FailureClass.HARD_MANDATE: ["card", "upi"],  # e-mandate on card or UPI Autopay
}

# Amount bands (paise), roughly log-distributed across a small-merchant SaaS/subscription profile.
AMOUNT_BANDS_PAISE = [
    (29900, 99900),      # ~299 - 999
    (99900, 499900),     # ~999 - 4999
    (499900, 1999900),   # ~4999 - 19999
]


@dataclass
class SyntheticFailure:
    invoice_id: str
    customer_id: str
    kind: str                    # ONE_TIME | RECURRING
    amount_paise: int
    currency: str
    mandate_id: str | None
    failure_class: str
    decline_reason_raw: str
    issuer: str
    instrument_type: str
    failed_at: str                # ISO timestamp
    customer_prior_contacts_30d: int   # seeds CONTACT_FREQUENCY_CAP state realistically


def _weighted_choice(rng: random.Random, mix: dict[FailureClass, float]) -> FailureClass:
    classes = list(mix.keys())
    weights = list(mix.values())
    return rng.choices(classes, weights=weights, k=1)[0]


def generate_batch(n: int, seed: int, start: datetime | None = None) -> list[SyntheticFailure]:
    rng = random.Random(seed)
    start = start or datetime(2026, 8, 1, 9, 0, 0)

    # Recurring share: mandate-relevant classes (HARD_MANDATE) only make sense on recurring
    # invoices; everything else can be either. 40% recurring is a plausible small-merchant mix,
    # not a sourced figure — cosmetic to the batch, doesn't feed any recovery-probability claim.
    recurring_share = 0.40

    batch: list[SyntheticFailure] = []
    # A shared pool of customers so CONTACT_FREQUENCY_CAP and repeat-issuer circuit-breaker logic
    # both have realistic repeat structure instead of every event being a distinct customer.
    n_customers = max(1, int(n * 0.6))
    customer_ids = [str(uuid.uuid4()) for _ in range(n_customers)]

    for i in range(n):
        failure_class = _weighted_choice(rng, ASSUMED_INDIA_DECLINE_MIX)
        is_recurring = rng.random() < recurring_share or failure_class == FailureClass.HARD_MANDATE
        kind = "RECURRING" if is_recurring else "ONE_TIME"

        lo, hi = rng.choice(AMOUNT_BANDS_PAISE)
        amount_paise = rng.randint(lo, hi)

        instrument = rng.choice(INSTRUMENT_BY_CLASS[failure_class])
        issuer = rng.choice(ISSUERS)
        reason_raw = rng.choice(DECLINE_REASON_SAMPLES[failure_class])

        # Spread events across the batch window (23 Aug window, matching the day-1 plan) with
        # some clustering around plausible business hours (09:00-21:00 IST) rather than uniform
        # across 24h — failed *payments* happen whenever customers transact, but this keeps the
        # QUIET_HOURS stopping rule exercised meaningfully rather than trivially in the sim.
        day_offset = rng.randint(0, 29)
        hour = rng.randint(7, 23)
        minute = rng.randint(0, 59)
        failed_at = start + timedelta(days=day_offset, hours=hour - start.hour, minutes=minute)

        customer_id = rng.choice(customer_ids)
        prior_contacts = rng.choices([0, 1, 2, 3], weights=[0.55, 0.25, 0.13, 0.07], k=1)[0]

        batch.append(
            SyntheticFailure(
                invoice_id=str(uuid.uuid4()),
                customer_id=customer_id,
                kind=kind,
                amount_paise=amount_paise,
                currency="INR",
                mandate_id=(str(uuid.uuid4()) if is_recurring else None),
                failure_class=failure_class.value,
                decline_reason_raw=reason_raw,
                issuer=issuer,
                instrument_type=instrument,
                failed_at=failed_at.isoformat(),
                customer_prior_contacts_30d=prior_contacts,
            )
        )

    batch.sort(key=lambda f: f.failed_at)
    return batch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args()

    batch = generate_batch(args.n, args.seed)
    payload = {
        "seed": args.seed,
        "n": len(batch),
        "generated_at": datetime.utcnow().isoformat(),
        "world_model_ref": "sim/world_model.py (ASSUMED_INDIA_DECLINE_MIX)",
        "events": [asdict(f) for f in batch],
    }

    out_path = args.out or f"sim/data/batch_seed{args.seed}.json"
    import os

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    class_counts: dict[str, int] = {}
    for e in batch:
        class_counts[e.failure_class] = class_counts.get(e.failure_class, 0) + 1
    print(f"Wrote {len(batch)} events (seed={args.seed}) to {out_path}")
    print("Class distribution:", json.dumps(class_counts, indent=2))


if __name__ == "__main__":
    main()
