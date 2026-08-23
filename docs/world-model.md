# World Model — Disclosed Assumptions

Backstop is evaluated against a synthetic batch, not live merchant data. That means **the person
who builds the simulator also decides who would have paid** — the single biggest credibility risk
in the whole submission (build spec §8). This document is the countermeasure: every probability the
simulator uses to decide "did this retry succeed" is named, sourced or flagged as a judgment call,
and swept for sensitivity in `docs/results/sensitivity-sweep.md`.

## How to read this document

Every probability constant in `sim/world_model.py` is prefixed so its evidentiary status is visible
by reading the identifier, not by reading a comment:

- **`MEASURED_*`** — grounded in a cited external source. Read the citation before trusting the
  number; see the caveat below about source quality.
- **`ASSUMED_*`** — no public source found. This is engineering judgment, stated as a specific
  number so it can be argued with and swept, rather than left implicit in code.

**Caveat on the `MEASURED_*` numbers**: the sources are dunning-software vendor blogs (Slicker,
Rechurn, Paddle, gr4vy), not peer-reviewed studies or a payment network's own published statistics.
Vendors marketing a retry product have an incentive to report generous recovery numbers. Treat every
`MEASURED_*` constant as an *informed prior*, not ground truth — which is precisely why §8's
sensitivity sweep exists: if Arm C's ranking survives varying these numbers across a wide plausible
band, the finding doesn't depend on trusting a vendor blog.

## Sources consulted (23 Aug 2026)

1. [Code 51 Declines: Insufficient Funds Recovery Playbook](https://www.slickerhq.com/resources/blog/code-51-soft-decline-retry-timing-mac-codes) — Slicker
2. [The Best Retry Schedule for Failed Payments in 2026](https://www.rechurn.io/blog/best-retry-schedule-failed-payments) — Rechurn
3. [2025 Involuntary Churn Benchmarks: B2C Subscription Brands](https://www.slickerhq.com/blog/2025-involuntary-churn-benchmarks-b2c-subscription-brands) — Slicker
4. [How Does the Payroll Calendar in India Work?](https://remunance.com/blog/how-does-the-payroll-calendar-in-india-work/) — Remunance
5. [Soft declines: how to diagnose and prevent them](https://www.paddle.com/blog/how-to-prevent-soft-declines-fltr) — Paddle (referenced, not deep-fetched)

None of these publish a joint distribution over `(failure_class, intervention, timing_offset)`. The
model below decomposes their point estimates into that joint distribution; the decomposition itself
is `ASSUMED_*`, even where the inputs are `MEASURED_*`.

## 1. Decline reason distribution (batch generator)

Source 3 gives a decline-cause breakdown. India's card mix differs from the US/EU-heavy sample those
vendors report on — most importantly, UPI has no direct analogue to "expired card," and 3DS/OTP
abandonment is a materially larger share of Indian soft declines than in card-only Western markets
because every card transaction requires an OTP step. `MEASURED_DECLINE_MIX` below is anchored to
source 3's card-only numbers; `ASSUMED_INDIA_ADJUSTMENT` corrects for OTP prevalence and UPI's
different failure surface. The adjustment direction (more `SOFT_AUTH`, less `HARD_INSTRUMENT`) is a
judgment call, not a cited figure.

| Failure class | Source-3 card mix (MEASURED) | India-adjusted mix used in generator (ASSUMED) |
|---|---|---|
| `SOFT_FUNDS` | 35% | 28% |
| `HARD_INSTRUMENT` (expired/changed card) | 28% + 22% = 50% | 22% |
| `SOFT_TRANSIENT` (gateway/technical) | 15% | 14% |
| `SOFT_AUTH` (3DS/OTP abandonment — no US analogue in source) | not reported | 20% |
| `SOFT_LIMIT` | not reported | 8% |
| `HARD_RISK` | not reported | 5% |
| `HARD_MANDATE` | not reported | 3% |

The India-adjusted column is what `sim/generate_batch.py` actually draws from. It is deliberately
not forced to reconcile against the card-only source — the reconciliation itself is the disclosed
assumption.

## 2. Recovery probability model

Recovery is modeled as a per-attempt **hazard rate**: given the invoice has reached attempt *N*
still unpaid, what's the probability *this* attempt clears it. Hazard rates decline with attempt
number (each failed attempt is evidence the failure is harder than average within its class — a
form of adverse selection Backstop's own circuit breaker logic already assumes exists at the issuer
level; see §3) and are modulated by a timing multiplier.

### 2.1 Base first-attempt hazard by class

| Constant | Class | Value | Status |
|---|---|---|---|
| `MEASURED_TRANSIENT_HAZARD_1` | `SOFT_TRANSIENT` | 0.70 | MEASURED — Rechurn/Slicker both frame technical/gateway declines as clearing "almost always" on prompt retry; 0.70 is a conservative read of "smart retry recovers 70-85% of soft declines" (source 1) applied to the most-recoverable subclass. |
| `MEASURED_FUNDS_HAZARD_1` | `SOFT_FUNDS` | 0.35 | MEASURED — derived from "smart retry systems recover 70-85% of soft declines" (source 1) minus the fact that funds declines need multiple attempts to reach that ceiling; 0.35 is the first-attempt share, calibrated so the geometric series below converges near the reported ceiling by attempt 3-4. |
| `ASSUMED_LIMIT_HAZARD_1` | `SOFT_LIMIT` | 0.45 | ASSUMED — no source addresses per-transaction limit declines directly. A split-amount or alternate-instrument retry is structurally similar to a funds retry but doesn't need to wait on external cash flow, so it's set above `MEASURED_FUNDS_HAZARD_1`. |
| `MEASURED_AUTH_HAZARD_1` | `SOFT_AUTH` | 0.55 | MEASURED (indirect) — source 1 distinguishes "hard declines will never succeed on retry" from soft; 3DS/OTP abandonment isn't a payment failure at all but a UX drop-off, so a *fresh link* (not a raw retry) has a materially higher hazard than a funds retry. 0.55 reflects that a fresh link removes the original friction but the customer still has to act. |
| `ASSUMED_HARD_HAZARD` | all `HARD_*` | 0.00 | Structural, not assumed-from-data — this is the policy's inviolable rule (build spec §3, "never retry"), not a probability estimate. Held at exactly 0 in the simulator so a nonzero recovery on `HARD_*` can only come from the *non-retry* remediation path (instrument update / mandate re-collection), never from a bare retry. |

### 2.2 Hazard decay across attempts

`ASSUMED_HAZARD_DECAY = 0.55` — each subsequent attempt's hazard is `previous_hazard × 0.55`,
reflecting adverse selection (source 1: "a third attempt rarely changes the outcome"). Applied to
`MEASURED_FUNDS_HAZARD_1 = 0.35`: attempt 2 hazard ≈ 0.19, attempt 3 ≈ 0.11, attempt 4 ≈ 0.06.
Cumulative recovery by attempt 4 ≈ 1 − (0.65 × 0.81 × 0.89 × 0.94) ≈ 56%, consistent with source 1's
mid-range recovery band once `MAX_LIFETIME_ATTEMPTS = 4` (build spec §6) caps the series.

### 2.3 Timing multiplier — the India-specific insight

`MEASURED_PAYDAY_WINDOW = [28, 31]` — source 4: "most companies paid anytime between 28th-31st of
each month," with fund transfers specifically in that window (not the 1st, which is the more common
Western assumption and would be the wrong constant to hardcode here).

`ASSUMED_PAYDAY_MULTIPLIER = 1.6` — no source gives a direct multiplier for retrying inside vs.
outside the payday window. 1.6× is a judgment call sized so that a `SOFT_FUNDS` retry scheduled
inside the 28th-31st window clears meaningfully more often than one scheduled mid-month, without
claiming near-certainty (which would be unfalsifiable given the available sources).

`ASSUMED_OFF_PEAK_MULTIPLIER = 0.85` — a `SOFT_FUNDS` retry scheduled in the first half of the month
(furthest from any plausible salary credit) is discounted below the class base rate.

Applies only to `SOFT_FUNDS`. Every other class's timing multiplier is `1.0` — there's no
comparable external-cash-flow mechanism for a gateway timeout or an OTP abandonment, and inventing
one would be assumption for its own sake.

### 2.4 `NUDGE` contact lift

`ASSUMED_NUDGE_LIFT = 0.12` (additive, applied to whatever the retry-path hazard already is) —
source 3 explicitly states it found no specific dunning email/SMS lift figure. This is the weakest
assumption in the model precisely because no source addresses it at all; it is the first constant
flagged for the sensitivity sweep and the pitch should say so.

`ASSUMED_CONTACT_FATIGUE = -0.03` per contact beyond the first, within the `CONTACT_FREQUENCY_CAP`
window — repeated nudges to the same customer are assumed to have diminishing and eventually
negative returns, motivating the cap as more than a compliance nicety.

## 3. Issuer circuit breaker

Not from a payment-specific source — this is the disclosed transplant the build spec calls for
(§6): the admission-control finding from the flash-sale project, that a crowd of retries which keep
failing measurably degrades the success rate of retries that would otherwise have worked.

`ASSUMED_ISSUER_DEGRADATION_THRESHOLD = 0.20` — rolling 30-minute retry success rate for a given
issuer BIN range, below which the circuit breaker pauses all retries to that issuer. No published
number exists for the *shape* of this degradation curve in the Indian card/UPI context, so the
simulator implements it as a simple deterministic penalty: once tripped, `ASSUMED_BREAKER_PENALTY
= 0.5×` multiplier on hazard for all in-flight retries to that issuer until the rolling rate recovers
above threshold. This is the single largest unvalidated assumption in the model and is swept across
a wide range (0.10–0.35 threshold, 0.3×–0.7× penalty) in the sensitivity analysis.

## 4. What the sensitivity sweep varies

Per build spec §8, every `ASSUMED_*` constant is swept across a plausible range and Arm C vs Arm A's
ranking is checked for stability at each point. The `MEASURED_*` constants are held fixed (they're
externally anchored, if weakly); sweeping them would be sweeping the sources, not the judgment
calls. Full ranges and results live in `docs/results/sensitivity-sweep.md` once Arms A/B/C are
running (build spec plan: 31 Aug – 1 Sep).

Swept constants: `ASSUMED_LIMIT_HAZARD_1`, `ASSUMED_HAZARD_DECAY`, `ASSUMED_PAYDAY_MULTIPLIER`,
`ASSUMED_OFF_PEAK_MULTIPLIER`, `ASSUMED_NUDGE_LIFT`, `ASSUMED_CONTACT_FATIGUE`,
`ASSUMED_ISSUER_DEGRADATION_THRESHOLD`, `ASSUMED_BREAKER_PENALTY`, `ASSUMED_INDIA_ADJUSTMENT`.

## Status

Draft, day 1 of the build plan. This document is written before `sim/world_model.py` per the plan's
instruction to write assumptions before code; the code should match this document exactly, and any
divergence discovered while implementing belongs in `docs/build-log.md`, not a silent edit here.
