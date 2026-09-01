# Backstop

**A payment recovery agent that decides whether an action is worth taking, and stops when nothing is.**

Built for the Razorpay AI Builder Internship 2026 buildathon — Track 3, AI Revenue Recovery.

Most recovery systems maximise attempts. Backstop's claim is that attempts have costs — network
penalty fees, issuer trust degradation, and customer annoyance that turns a recoverable invoice into
a churned customer — and that a system pricing those costs does *less* than a naive one.

The name refers to the hard compliance rules sitting behind the economic decision, as a final
safety net. They are not the product. The arithmetic is.

---

## The headline, stated honestly

On a seeded batch of 120 failures, against Razorpay's own documented default retry behaviour:

| Arm | Net value added | Recovered | Attempts | Contacts |
|---|---|---|---|---|
| Do nothing (reference) | ₹0 | 0 | 0 | 0 |
| **A — Naive** (Razorpay default) | **₹2,161,735** | 80 | 280 | 74 |
| **B — Rules only** | ₹2,000,191 | 78 | 157 | 65 |
| **C — Backstop** (table classifier) | ₹1,926,991 | 73 | 145 | 64 |
| **D — Backstop + LLM** | ₹2,006,152 | 71 | **137** | **61** |

Each arm differs from the one above it by exactly one component, so the comparison isolates what
each change bought. B→C is the expected-value layer with the classifier held fixed. C→D is the
classifier with the economics held fixed.

**Backstop captures 93% of the naive baseline's value using 49% of the authorisation attempts and
82% of the customer contacts. It does not beat the baseline on raw recovered value.**

We are not going to bury that. Whether trading 7% of recovered value for half the authorisation
attempts is a good deal depends on what a dunning contact costs a customer relationship — and that
is the one number in this model with no published source anywhere. So the deliverable is not a win
claim, it is a **frontier** (`docs/results/frontier.html`) plus the threshold at which the trade
flips, so a reader can substitute their own belief and read off the consequence.

"Net value added" is measured against a do-nothing arm rather than reported in absolute terms.
A realistic batch is mostly invoices that were never going to be recovered, so every arm's absolute
net is a large negative number dominated by losses no policy could have prevented. The difference
from the floor is the part a policy is actually responsible for.

---

## Architecture

```
failure payload + customer history
      │
      ▼
┌─────────────┐  classification, recovery bucket (ordinal), rationale
│     L1      │  app/classifier.py — the only file that touches an LLM
└─────────────┘
      │  proposes
      ▼
┌─────────────┐  prices every candidate action; STOP scores zero
│    L2b      │  app/scorer.py — expected value
└─────────────┘
      │  may narrow, never widen
      ▼
┌─────────────┐  vetoes anything illegal or non-compliant
│    L2a      │  app/policy.py + app/stopping_rules.py — pure functions, no I/O
└─────────────┘
      │  permits
      ▼
┌─────────────┐  idempotent call, key = (payment_id, attempt_no)
│     L3      │  app/executor.py — NOT YET BUILT, see Status
└─────────────┘
      │
      ▼
  APPEND-ONLY AUDIT LOG
```

### The one-way valve

**Each layer may only reject or narrow what the previous layer proposed. No layer may escalate.**

This is the structural argument for why a language model in this loop is safe, so it is enforced
rather than intended. `app/actions.py` defines a **blast radius** — an integer ordering of actions
by how much irreversible cost they can inflict:

| rank | action | why it sits there |
|---|---|---|
| 5 | `RETRY_NOW` | an immediate, unattended charge attempt — the most aggressive thing the system can do |
| 4 | `RETRY_SCHEDULED` | the same attempt, deferred |
| 3 | `NUDGE` / `REQUEST_INSTRUMENT_UPDATE` | contact the customer, don't move money. Same tier, so a swap between them is lateral |
| 1 | `ESCALATE_HUMAN` | a person's time, no network or customer contact |
| 0 | `STOP_PERMANENT` | nothing happens |

`evaluate()` raises `PolicyViolation` if any rule ever increases the rank — in production, not only
under test, because a violated invariant here is a correctness bug in the one place correctness bugs
are least acceptable. `tests/test_policy.py` covers the original pipeline at 100% branches;
`tests/test_compliance_rules.py` re-checks the invariant across the rules added since.

`PolicyDecision` distinguishes a **veto** (the action's *family* changed — retry / contact /
terminal) from a **downgrade** (same family, softened timing or a lateral swap). Those are different
events and collapsing them loses the distinction the veto-rate metric depends on.

---

## What L1 is for

If L1's only job were mapping decline codes to seven classes, a lookup table would tie it and the
ablation would say so. Its role is narrowed to four things a dictionary cannot do:

1. **Ambiguous payloads.** Razorpay's own card-error documentation states they may not have a
   specific failure reason for bank declines, because customer banks typically do not provide one.
   That ambiguity is documented by the gateway; we did not invent it. 35% of the eval batch is
   generic, missing, or self-contradicting payloads.
2. **Parameter estimation.** The recovery bucket from payment history, prior failure pattern,
   tenure, invoice size relative to that customer's normal, and prior contact count. None of that
   is in a decline code.
3. **Action selection** among legal options.
4. **The written audit rationale**, which is a judged deliverable and must cite payload fields.

**L1 never emits a floating-point probability.** It emits an ordinal bucket; `world_model_constants`
maps buckets to base rates. A judge asking "where does 0.34 come from?" must not be told "the model
said so". `ScoreContext` raises on anything that is not one of the five bucket labels, so a float
cannot reach the arithmetic even if a prompt change lets one out of the model.

### What the model actually bought

A language model classified all 120 cases. Arms B and C use the decision table, arm D uses the
model, and everything else is held constant — so C→D isolates the classifier.

| Bucket | n | Table | Model |
|---|---|---|---|
| Clean payloads | 48 | 100% | 100% |
| Context-dependent | 30 | 100% | 100% |
| **Ambiguous** | **42** | **38%** | **52%** |
| All | 120 | 78% | 83% |

**The model ties the table on 65% of traffic and wins on one bucket.** Reporting that is more
convincing than a uniform win, because a uniform win invites the reader to look for the smoothing
and the ablation would find it.

Better still, the advantage narrows to one specific place. Splitting the ambiguous bucket by what
was done to the payload:

| Ambiguity | n | Table | Model |
|---|---|---|---|
| Description contradicts `reason` | 12 | 100% | 100% |
| Generic bank decline, no signal at all | 15 | 20% | 20% |
| **`reason` null, `source`/`step` survive** | **15** | **7%** | **47%** |

**The model's entire advantage comes from reading `source` × `step` when the gateway supplied no
reason.** Where the structured reason survives, both are perfect. Where nothing survives, both sit
at the base rate and neither can do better. That is exactly the claim this design made in advance —
the `source` × `step` × `reason` triple is what makes L1 worth having over a dictionary — and it is
falsifiable rather than an appeal to model quality.

**Calibration, and the finding that turned a loss into a win.** The model's confidence tracks its
accuracy closely: HIGH 100% over 83 cases, MEDIUM 76% over 17, LOW 20% over 20.

That matters because the first version of arm D **lost** to the table arm — ₹1,881,433 against
₹1,926,991 — despite classifying better. The mechanism: the model's per-case buckets are far more
dispersed than the table's class-level mapping (34 cases at LOW or VERY_LOW against 21, at the same
mean), and the scorer trusted every one of them equally. Extra pessimism from a LOW-confidence
answer is a stop signal manufactured from thin evidence, and it stopped recoverable invoices.

Falling back to the table's class-level bucket when confidence is LOW — using the model's own
reliability signal, which was sitting there unused — moved arm D to ₹2,006,152, ahead of both other
policy arms, on fewer attempts and fewer contacts than either.

**Better classification did not by itself produce more money.** It had to be filtered by the model's
own confidence first. That is the most useful thing this measurement produced.

> **Provenance, because it changes how much these numbers are worth.** The classifications in
> `sim/data/l1_classifications_seed42.json` were produced by Claude reading only the fields a real
> L1 receives, given `SYSTEM_PROMPT` verbatim, across four separate fresh contexts that had never
> seen `sim/generate_batch.py` or the ground truth. That isolation is deliberate: whoever writes the
> batch generator knows the answer key, so a classification produced by that same context would be
> contaminated and the accuracy figures would mean nothing.
>
> **This is not a live API run.** `LLMClassifier` is the production path and remains untested
> against the real endpoint. `CachedLLMClassifier` replays the recording so the arm is
> reproducible — re-running the model would not reproduce these exact labels.

---

## The EV model

```
EV(action) = P(recovery) × (invoice_value + P(lapse) × customer_LTV)
           − action_cost
           − issuer_trust_cost × P(failure | action)
           − churn_hazard(contacts, recency) × customer_LTV
```

STOP scores exactly zero, so "stop when nothing has positive EV" and "take the highest-EV action"
are the same rule.

**The `P(lapse) × customer_LTV` term is a deliberate correction to the specification we were handed,
and the model does not work without it.** As specified, the formula charges churn for *contacting* a
customer and charges nothing for *abandoning* their invoice. But an invoice that is never recovered
is the definition of involuntary churn: in the simulated world, giving up costs about seven times
what a first contact costs. The first three-arm run said so plainly — Backstop recovered 63 invoices
to the naive baseline's 87. The scorer was not being restrained, it was blind to half the ledger.

The correction restores the framing of our own cited source: Redux frames the true cost of a failed
payment as unrecovered failures × remaining LTV, not the failed charge. The original formula
contradicted the citation underneath it.

Every term is kept separate through to the trace renderer, because "explainable" means a reader can
see which term dominated — and can check that the columns sum to the total.

---

## Where restraint comes from, and where it does not

Two findings, one of which we would rather were untrue. Both are asserted as tests, so they cannot
quietly stop being true.

**On the contact axis, restraint emerges.** The value of a contact collapses by an order of
magnitude across three asks and turns negative on the fourth. `CONTACT_FREQUENCY_CAP` is set to 3 —
so the cap sits almost exactly where the expected value crosses zero, which is a far better reason
for a cap to be at 3 than "3 felt right". Note this claim got *weaker* when the model got more
correct: before the lapse term was added the crossing was at the second ask, comfortably ahead of
the cap. It is now coincident with it, not ahead of it.

**On the retry axis, restraint does not emerge.** `ISSUER_TRUST_COST_INR` is about ₹8.80, a floor
taken from published network fee schedules, and that cannot outweigh even a fractional chance at a
₹2,000 invoice plus the lapse it prevents. Retry EV decays toward zero from above and never crosses
it. `MAX_LIFETIME_ATTEMPTS` — a backstop — is what actually stops the retrying.

We are not fixing that by guessing a larger number. The claim becomes the threshold instead:

| customer LTV | breakeven per-contact churn hazard |
|---|---|
| 2× invoice | 0.5563% |
| 6× invoice | 0.1854% |
| 12× invoice | 0.0927% |
| 18× invoice | 0.0618% |

**Our conclusion holds unless a dunning contact raises churn by less than roughly two-tenths of one
percent.** That is a low bar, it is falsifiable, and it is a much stronger position than defending
an invented number.

---

## Is the result circular?

In a simulator we write both the constants that generate outcomes and the constants the agent
optimises against. If those are the same function, Backstop beats naive *by construction* — we told
the world that contacts cause churn, then told the agent to avoid contacts, then reported our own
premise back as a finding. A sharp reader finds this in seconds, so it is designed against:

- The **world** (`sim/world_model.py`) uses a *saturating* churn process with linear patience
  recovery. The **agent** (`app/scorer.py` + `world_model_constants.py`) believes fatigue is
  *geometric* in contact count with exponential recency decay. Different functional forms, different
  numbers, and no code path from a scorer to the world's constants.
- The world draws a per-customer LTV multiple across 2–14×. The agent believes a flat 6×.
- `Beliefs.perturbed()` exists so the agent can be handed parameters that are deliberately wrong.

**Robustness result:** across a 10× swing in the agent's churn belief (0.3× to 3×), Backstop's value
added stays within ₹1.92M–₹2.02M while its contacts fall from 65 to 47. The advantage degrades
gracefully rather than inverting. *"Our advantage survives being wrong about the constants"* is a
better sentence than *"our advantage exists when the constants are correct."*

**Adversarial arm** (`--adversarial`): contact fatigue and issuer penalties both set to zero, so
blind retrying costs nothing beyond the gateway fee. Naive gains (₹2.69M) and Backstop loses ground
(₹2.03M). That is the correct result — in a world where restraint buys nothing, restraint is not
worth buying — and reporting it is worth more than the number is.

---

## Compliance is not our policy

The rules were reclassified honestly. Each one declares its basis, and the audit log names it:

| Rule | Basis | Source |
|---|---|---|
| `HARD_DECLINE_NO_RETRY` | REGULATORY | Visa Excessive Reattempts (Category 1); Mastercard TPE MAC 03/21 |
| `MC_NEVER_RETRY_ADVICE_CODE` | REGULATORY | Mastercard TPE — fires on the *network's* label, not our classification |
| `NETWORK_REATTEMPT_CAP` | REGULATORY | Visa 15/card/30d; Mastercard 10 auths/PAN/24h |
| `EMANDATE_PREDEBIT_NOTICE` | REGULATORY | RBI/DPSS/2026-27/396, 21 Apr 2026 — 24h notice, 26h buffer |
| `QUIET_HOURS` | REGULATORY | TRAI TCCCPR 2018 as amended 12 Feb 2025 — 9am–9pm |
| `HARD_RISK_NO_CONTACT` | BACKSTOP | ours — a fraud decline is a risk operation, not a dunning one |
| `MAX_LIFETIME_ATTEMPTS` | BACKSTOP | ours — bounds scorer error |
| `MIN_ATTEMPT_INTERVAL` | BACKSTOP | ours |
| `CONTACT_FREQUENCY_CAP` | BACKSTOP | ours |
| `ISSUER_CIRCUIT_BREAKER` | BACKSTOP | ours — transplanted admission-control finding |

The basis lives in `app/rule_basis.py` rather than on the rule itself, so the reclassification could
be added without touching logic that was already built and tested.

Two scope decisions worth naming, because getting either wrong is a plausible-looking bug:

- **The RBI pre-debit notice binds the e-mandate rail, not every payment.** It gates a mandate
  debit; it does not gate a customer-initiated payment link. Over-applying it would suppress the
  correct action on every recurring invoice. There is a test for exactly that.
- **Quiet hours gate contact, not retries.** A retry is machine-to-machine and TCCCPR has nothing to
  say about authorising a card at 3am. Blocking retries there would be over-compliance that costs
  real recoveries.

`HARD_RISK_NO_CONTACT` was found by an interaction test, not by design. `HARD_DECLINE_NO_RETRY`
already forbade `NUDGE` on a risk decline — implementing the spec's "never retry, never nudge"
literally — but `REQUEST_INSTRUMENT_UPDATE` is the other customer-facing action, carries the same
hazard, and passed the whole pipeline untouched. The standard recovery move ("your payment failed,
please try another method") sent to a suspected-fraud decline is the merchant telling whoever holds
the card which instrument to try next, with the merchant's branding on it.

---

## Metrics, measured in both directions

A system that only reports where it was too aggressive is telling half the story.

- **Veto rate**, split by rule and by basis (`app/rule_basis.py`) — how often L2a overruled the
  layer above it. A `REGULATORY` veto is compliance working exactly as designed. A `BACKSTOP` veto
  is a finding about the scorer, because the arithmetic should have stopped us first. Backstop's
  35 firings are **all** `REGULATORY` (every one `QUIET_HOURS`); rules-only takes 6 `BACKSTOP`
  firings the EV layer avoids entirely — which is the cleanest evidence in the run that the
  economics are doing work the caps were previously doing.
- **Under-proposal rate** — how often the agent gave up on an invoice that ground truth says was
  still live. L2a structurally cannot catch this and nothing else reports it. Backstop: 8 of 120,
  against rules-only's 3 — restraint's real cost, measured rather than assumed.

---

## Running it

```bash
pip install -r requirements.txt
python -m pytest                          # 244 tests
python -m sim.generate_batch --n 120 --seed 42
python -m sim.run_arms --n 120 --seed 42
python -m sim.run_arms --adversarial
python -m sim.run_arms --belief-error 0.5 --belief-error 2.0
python -m sim.render_trace                # docs/results/trace.html
python -m sim.render_frontier             # docs/results/frontier.html
python sim/world_model_constants.py --citations   # provenance table
python sim/world_model_constants.py --unsourced   # the honesty section
python sim/world_model_constants.py --breakeven   # the table above
```

The naive baseline is **Razorpay's own documented default**, not a strawman: on a failed auto-charge
the subscription moves to `pending`, Razorpay retries on T+1, T+2 and T+3, and if all three fail it
moves to `halted` and the customer is emailed a card-update link.
([docs](https://razorpay.com/docs/payments/subscriptions/payment-retries/))

---

## Status

**Built and tested:** L2a (policy gate, stopping rules), L2b (EV scorer), L1's interface and both
implementations, the eval batch with ground truth, the three-arm simulator, and both judge-facing
artifacts, and the measured LLM arm. 244 tests (149 pre-existing on L2a, untouched).

**Not built, and not claimed:**

- **L3.** No Razorpay API call has been made. The executor is specified (idempotency key
  `(payment_id, attempt_no)`, unique-constrained in `app/models.py`) but not written, and at least
  one end-to-end demo trace should use a real test-mode failure before submission.
- **`app/audit.py`.** The schema exists; the append-only writer does not. The simulator carries
  per-decision traces in memory instead.

**Known limitation:** the evaluation is entirely synthetic, and ground-truth labels are exact by
construction rather than hand-annotated. `docs/world-model.md` documents every constant, its
provenance tier, and the full list of ones we could not source.

## Repository

```
app/
  enums.py            closed taxonomies, zero dependencies
  actions.py          blast-radius ordering — makes the valve checkable
  classifier.py       L1: lookup table + LLM implementations
  scorer.py           L2b: the EV model
  stopping_rules.py   ten rules, each declaring REGULATORY or BACKSTOP
  policy.py           L2a: the gate, with the valve asserted in code
  models.py           schema: invoices, attempts, append-only audit log
sim/
  world_model.py            the WORLD's truth — not the agent's beliefs
  world_model_constants.py  the AGENT's beliefs, with three-tier provenance
  generate_batch.py         40% clean / 35% ambiguous / 25% context-dependent
  run_arms.py               do-nothing / naive / rules-only / backstop
  render_trace.py           docs/results/trace.html
  render_frontier.py        docs/results/frontier.html
tests/
  test_policy.py      the one-way valve as a property, plus interaction tests
  test_scorer.py      including the findings we would rather were untrue
```
