# Backstop — handoff, 1 September 2026

You are picking up an in-flight buildathon project with a hard deadline. Read all of this before
touching the repository.

**Repo:** `C:\Users\ramki\Desktop\here\Razorpay` — also `github.com/ramkirangaruda/Razorpay`
**Deadline:** 4 September 2026 (the original brief says 5 Sep; work to 4)
**Event:** Razorpay AI Builder Internship 2026 buildathon, Track 3 — AI Revenue Recovery
**State:** `main` at `944b86b`, working tree clean
**Tests:** 244 passing (`python -m pytest`)

---

## 0. Do these first

1. **Run the suite and confirm 244 passing.** `pip install -r requirements.txt` then
   `python -m pytest`. If it is not 244, stop and find out why before building anything.

2. **Run the arms and confirm five of them, including `D_backstop_llm`.**
   `python -m sim.run_arms --n 120 --seed 42`. If arm D is missing,
   `sim/data/l1_classifications_seed42.json` did not come through — `CachedLLMClassifier`
   degrades to the decision table rather than failing, so its absence is silent. Every LLM
   figure in the README depends on that file.

3. **Read `docs/build-log.md` from the 1 September entries down.** It records three wrong turns
   from that day and why they were wrong. All three are cheap to repeat.

4. **Push after every self-contained piece of work.** The biggest risk this project has already
   survived: the day-2 policy gate sat unpushed on one laptop for nine days, and a session that
   cloned from GitHub concluded the work did not exist and rebuilt it in parallel.

## 1. What Backstop is

A payment-failure recovery agent. When a recurring charge or invoice payment fails, it decides what
to do: retry now, retry later, send a payment-update link, contact the customer, escalate, or stop.

The distinguishing claim is **restraint**. Most recovery systems maximise attempts. Backstop's
thesis is that attempts have costs — network penalty fees, issuer trust degradation, and customer
annoyance that converts a recoverable invoice into a churned customer — so a system that prices
those costs does *less* and nets *more*.

The name refers to the hard compliance rules sitting behind the economic decision as a final safety
net. They are not the product. The arithmetic is.

---

## 2. Architecture, as built

```
failure payload + customer history
      │
      ▼
  L1  app/classifier.py     classification, ordinal recovery bucket, rationale
      │                     the ONLY file that touches an LLM
      ▼  proposes
  L2b app/scorer.py         prices every candidate action; STOP scores zero
      │                     may narrow the proposal, never widen it
      ▼
  L2a app/policy.py         vetoes anything illegal or non-compliant
      app/stopping_rules.py 10 rules; app/rule_basis.py classifies each
      app/circuit_breaker.py
      │  permits
      ▼
  L3  NOT BUILT — see §5
```

**The one-way valve.** Each layer may only reject or narrow what the previous layer proposed.
`app/policy.BLAST_RADIUS_RANK` gives every action an integer rank and `evaluate()` raises
`PolicyViolation` if any rule ever increases it — in production, not just under test. Ranks:
`RETRY_NOW` 5, `RETRY_SCHEDULED` 4, `NUDGE` / `REQUEST_INSTRUMENT_UPDATE` 3, `ESCALATE_HUMAN` 1,
`STOP_PERMANENT` 0.

**L2a vetoes, it does not propose.** It may defer an action in time, hand a risk decline to a human,
or stop. It may not invent a new way to reach the customer. A gate that rescues the model it audits
destroys the under-proposal metric.

---

## 3. The EV model

```
EV(action) = P(recovery) × (invoice_value + P(lapse) × customer_LTV)
           − action_cost
           − issuer_trust_cost × P(failure | action)
           − churn_hazard(contacts, recency) × customer_LTV
```

STOP scores exactly zero, so "stop when nothing has positive EV" and "take the highest-EV action"
are the same rule.

**The `P(lapse) × LTV` term is a deliberate correction to the original spec.** As handed over, the
formula priced STOP at zero — charging churn for *contacting* a customer and nothing for
*abandoning* their invoice. An unrecovered payment is the definition of involuntary churn and costs
roughly seven times a first contact in this world. Without the term the scorer recovered 63 invoices
against the naive baseline's 87: not restrained, blind. **Do not remove this term** — there is a
test guarding it (`test_the_lapse_term_is_what_makes_action_worth_taking`).

**L1 never emits a float.** It emits an ordinal bucket; the constants file maps buckets to base
rates. `ScoreContext` raises on anything else, so a float cannot reach the arithmetic even if a
prompt change lets one out of the model. Keep it that way — "where does 0.34 come from?" must never
be answered with "the model said so".

---

## 4. Current results

Seeded batch, n=120, seed 42. Value added is measured against a do-nothing arm, because absolute net
is dominated by losses no policy could prevent.

| Arm | Value added | Recovered | Attempts | Contacts |
|---|---|---|---|---|
| Do nothing (reference) | ₹0 | 0 | 0 | 0 |
| A — Naive (Razorpay default) | ₹2,161,735 | 80 | 280 | 74 |
| B — Rules only | ₹2,000,191 | 78 | 157 | 65 |
| C — Backstop (table classifier) | ₹1,926,991 | 73 | 145 | 64 |
| **D — Backstop + LLM** | **₹2,006,152** | 71 | **137** | **61** |

Each arm differs from the one above by exactly one component, so the comparison isolates what each
change bought. B→C is the expected-value layer with the classifier fixed; C→D is the classifier with
the economics fixed.

**Backstop takes 93% of the naive baseline's value on 49% of the attempts and 86% of the contacts.
It does not beat the baseline on raw value, and nothing in the repo is arranged to suggest it does.**
Keep it that way. The deliverable is the frontier plus the breakeven threshold, not a win claim.

**What the model bought.** 83% classification accuracy against the table's 78% — but the aggregate
is the least useful cut. Clean payloads 100/100, context-dependent 100/100, ambiguous **52/38**. The
whole advantage localises to one thing: where `reason` is null but `source` and `step` survive,
**47% against 7%**. Where the structured reason survives both are perfect; where nothing survives
both sit at the base rate.

**And the finding worth protecting:** arm D initially *lost* to arm C despite classifying better.
The model's per-case buckets are more dispersed than the table's class-level mapping, and
`ScoreContext` reads the bucket with no view of confidence — so a guess was trusted as much as a
certainty. Gating the bucket on the model's own confidence (HIGH 100%, MEDIUM 76%, LOW 20%) turned a
₹45k loss into a ₹79k win. **Better classification did not by itself produce more money.** Do not
remove `trust_bucket_below_confidence=False`; `test_confidence_gating_pays_for_itself` guards it.

**Veto rate, split by basis** — Backstop's 35 vetoes are *all* `REGULATORY` (every one
`QUIET_HOURS`). Rules-only takes 6 `BACKSTOP` firings that the EV layer avoids entirely. That is the
run's cleanest evidence the economics do work the caps were previously doing.

**Under-proposal** — Backstop 8 of 120, rules-only 3. Restraint's real cost, measured rather than
assumed.

**Robustness** — across a 10× swing in the agent's churn belief (0.3× to 3×), value added holds
within ₹1.92M–₹2.02M while contacts fall 65 → 47. Degrades gracefully, does not invert.

**Adversarial arm** (`--adversarial`, zero contact fatigue and zero issuer penalty) — naive gains to
₹2.69M and Backstop loses ground to ₹2.13M. That is the correct result and it is reported, because
volunteering it forecloses the sharpest question a panel can ask.

**Where restraint comes from, and where it does not.** On the contact axis it emerges: contact value
collapses by an order of magnitude across three asks and turns negative on the fourth, exactly where
`CONTACT_FREQUENCY_CAP` sits. On the retry axis it does **not** — `ISSUER_TRUST_COST_INR` is ~₹8.80,
a published network fee floor, and cannot outweigh a fractional chance at a ₹2,000 invoice plus the
lapse it prevents. `MAX_LIFETIME_ATTEMPTS` does that work. Both findings are asserted as tests. The
defence is the breakeven table, not a better guess:

| customer LTV | breakeven per-contact churn hazard |
|---|---|
| 2× invoice | 0.5563% |
| 6× invoice | 0.1854% |
| 12× invoice | 0.0927% |
| 18× invoice | 0.0618% |

*Our conclusion holds unless a dunning contact raises churn by less than roughly two-tenths of one
percent.*

---

## 5. What is NOT built — the actual work

In priority order. If time compresses, hold this order.

### 5.1 ~~The LLM arm~~ — DONE, but one thing remains

Measured on 1 September; see §4 and the build log. `CachedLLMClassifier` replays a recording so the
arm reproduces.

**What is still open:** the recording was produced by Claude reading the payloads directly, not by
`app/classifier.LLMClassifier` calling the Anthropic API. That class is written, prompted and
schema-validated, but **has never executed against the real endpoint.** If an API key becomes
available, run it and confirm the live path works — the interesting number is the schema-rejection
rate, which is a reportable fact about model reliability in a money-movement path. Do not overwrite
the existing recording with a live run without keeping both; the README's figures cite the recording.

A calibration plot (predicted bucket against realised recovery rate) is still unbuilt and is cheap —
the data is all in `sim/data/l1_classifications_seed42.json` plus the batch's ground truth.

### 5.2 L3 — at least one real Razorpay test-mode call

`app/executor.py` does not exist. The contract is already fixed: idempotency key
`(razorpay_payment_id, attempt_no)`, unique-constrained at the DB layer in `app/models.py`.

**Do not build exhaustive live-API coverage.** Three or four real test-mode calls that prove
idempotency and capture the real error object shape; everything else runs against a fake. Exhaustive
sandbox coverage earns no points and is a well-known way to lose a day.

At least one end-to-end demo trace must use a **real Razorpay test-mode failure**, not a fabricated
payload. A project that barely touches their API reads as generic to this specific judge.

### 5.3 `app/audit.py`

The schema exists in `app/models.py` (`AuditLogEntry`, append-only, `AuditEventType`). The writer
does not; the simulator carries per-decision traces in memory instead. This is the concrete artifact
behind "every money action explainable", so it is worth the hour: append-only, no update or delete
methods exposed at all.

### 5.4 Demo walkthrough

A short document or script that walks a judge through: one invoice's trace → the frontier → the
adversarial result. `docs/results/trace.html` and `docs/results/frontier.html` already exist and
regenerate from the run.

### 5.5 Verify the RBI circular wording

`EMANDATE_PREDEBIT_NOTICE` cites RBI/DPSS/2026-27/396, 21 April 2026 — 24h pre-transaction notice,
₹15,000 AFA exemption ceiling (₹1,00,000 for insurance, mutual funds, credit card bills). **The exact
wording has not been verified against the circular.** Do that before submission; the citation is
load-bearing in the README and a judge from Razorpay will know it.

---

## 6. Things not to do

- **Do not modify L2a's logic.** `app/policy.py`, `app/models.py`, `app/circuit_breaker.py`,
  `tests/test_policy.py`, `tests/test_circuit_breaker.py` are built and tested at 149 tests and are
  byte-identical to how they were written. `app/stopping_rules.py` has been added to but nothing in
  it removed. Add rules; do not rewrite existing ones.
- **Do not add more branch coverage to L2a.** It is done. Write *interaction* tests instead — see
  `tests/test_compliance_rules.py` for the pattern.
- **Do not let L1 emit floats.**
- **Do not remove the `P(lapse)` term** from the EV model. See §3.
- **Do not tune constants to make Backstop win.** The honest frontier is the deliverable. If a change
  moves the numbers, `tests/test_reported_claims.py` will fail — update the README deliberately
  rather than relaxing the test.
- **Do not launder adjacent data into citations.** If a source is about promotional email fatigue and
  we need dunning contact fatigue, it stays labelled `ASSUMED`.
- **Do not average away source disagreement** into a point estimate. Disagreement becomes a sweep
  range.
- **Do not build a dashboard with a framework.** Static HTML emitted by the simulation run.
- **Never commit the LLM API key.**

---

## 7. Traps that have already bitten this project

- **The remote is not the project.** A session cloned from GitHub, found no L2a, concluded it did not
  exist, and rebuilt it in parallel — while the real one sat unpushed locally. Check
  `git log origin/main..main` before concluding anything is missing.
- **`git status` clean ≠ no changes.** It means no *uncommitted* changes. `git reset --hard <sha>`
  silently discards committed work that status will never warn you about. `git reflog` recovers it;
  commits survive for months.
- **Timezones.** The simulation clock is UTC. `PolicyContext.now_utc` is timezone-aware and
  `quiet_hours` converts to IST itself. Feeding it an IST-valued datetime tagged as UTC shifts the
  protected window by 5½ hours and silently sends messages at 3am while looking correct in review.
  `sim/run_arms._utc` is the single conversion point.
- **The proposal is a ceiling.** L2b may only narrow, so an action the classifier never proposes is
  one the whole pipeline can never take, however good its expected value. A weak proposer caps the
  entire system. This was a real bug: the lookup table returned one action per class forever and
  never reached for a payment link.
- **A gitignored input silently degrades a result.** `sim/data/*.json` is ignored because batches
  rebuild from a seed. The L1 recording does not — it is an INPUT, and `CachedLLMClassifier` falls
  back to the decision table rather than failing when it is missing. Arm D would have quietly
  vanished from a fresh clone while the README kept quoting its numbers. There is now a
  `!sim/data/l1_classifications_seed42.json` exception; do not undo it.
- **A model's confidence output is not decoration.** See §4. The scorer consumed the recovery bucket
  with no view of how sure the model was, and that alone made a better classifier worth less than a
  worse one.
- **Structural zeroes are not estimates.** `CHANNEL_FIT` has retry-on-`SOFT_AUTH` at 0.0 because a
  retry cannot fix a failure where the customer never authorised anything. Sweeps must not move it.
  Ground truth once rolled the fresh-link hazard as the retry hazard and handed the naive arm a block
  of free recoveries.

---

## 8. Commands

```
python -m pytest                                   # 244 tests
python -m sim.generate_batch --n 120 --seed 42
python -m sim.run_arms --n 120 --seed 42
python -m sim.run_arms --adversarial
python -m sim.run_arms --belief-error 0.5 --belief-error 2.0
python -m sim.render_trace                         # docs/results/trace.html
python -m sim.render_frontier                      # docs/results/frontier.html
python -m sim.gen_world_model_doc                  # regenerates docs/world-model.md
python sim/world_model_constants.py --citations
python sim/world_model_constants.py --unsourced
python sim/world_model_constants.py --breakeven
python sim/world_model_constants.py --check
```

Arm D reads `sim/data/l1_classifications_seed42.json`, which IS committed (it is a replay input, not
a generated artifact). Everything else in `sim/data/` rebuilds from a seed and stays ignored.

`docs/world-model.md` is **generated** — the prose is hand-written but the citation table, unsourced
list, breakeven table and provenance counts are read from the registry at build time. Edit
`sim/gen_world_model_doc.py`, not the markdown.

---

## 9. Repository map

```
app/
  models.py            schema + the closed taxonomies      [FROZEN]
  policy.py            L2a gate, blast-radius invariant     [FROZEN]
  stopping_rules.py    10 rules                             [additive only]
  circuit_breaker.py   issuer breaker                       [FROZEN]
  rule_basis.py        REGULATORY / BACKSTOP + citations
  scorer.py            L2b, the EV model
  classifier.py        L1: lookup + LLM implementations
  executor.py          L3 — NOT BUILT
  audit.py             NOT BUILT
sim/
  world_model.py             the WORLD's truth — not the agent's beliefs
  world_model_constants.py   the AGENT's beliefs, three-tier provenance
  generate_batch.py          40% clean / 35% ambiguous / 25% context, with ground truth
  run_arms.py                do-nothing / naive / rules-only / backstop
  render_trace.py            docs/results/trace.html
  render_frontier.py         docs/results/frontier.html
  gen_world_model_doc.py     docs/world-model.md
tests/
  test_policy.py             149 tests with test_circuit_breaker  [FROZEN]
  test_circuit_breaker.py                                          [FROZEN]
  test_compliance_rules.py   interaction tests for the added rules
  test_scorer.py             including findings we would rather were untrue
  test_reported_claims.py    asserts the README's numbers against a live run
  test_l1_measurement.py     pins what the model bought, including where it TIES
docs/
  architecture.md  world-model.md (generated)  build-log.md  results/
```

The world model and the agent's beliefs are **deliberately different functional forms** — saturating
vs geometric contact fatigue, linear vs exponential recency decay, per-customer vs flat LTV. That is
the anti-circularity mitigation. Do not collapse them into one set of constants.
