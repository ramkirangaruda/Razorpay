# Backstop — handoff, 2 September 2026

You are picking up an in-flight buildathon project with a hard deadline. Read all of this before
touching the repository.

**Repo:** `C:\Users\ramki\Desktop\here\Razorpay` — also `github.com/ramkirangaruda/Razorpay`
**Deadline:** 4 September 2026 (the original brief says 5 Sep; work to 4)
**Event:** Razorpay AI Builder Internship 2026 buildathon, Track 3 — AI Revenue Recovery
**State:** `main` at `ecaae1b`, working tree clean. **Every item on the original §5 list is
closed** (§5.1's optional live LLM run aside) — read §5 before assuming there is undone work
matching the original brief; anything else, including §10 (the live decision console), is new
scope the project owner asked for directly, not a leftover.
**Tests:** 298 passing/skipping cleanly (`python -m pytest`) — 244 pre-existing + 26 for
`app/executor.py` (22 always-on, 4 live-only against a real Razorpay test-mode account —
2 of which currently SKIP, not fail, against an exhausted test-mode payment_link quota;
see §7) + 13 for `app/audit.py` + 5 for `sim/render_calibration.py` + 10 for `app/api.py`;
see §5.1–§5.3 and §10 below, and `docs/build-log.md`'s 1-2 September entries

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
  L3  app/executor.py       idempotent Razorpay calls, live-verified — see §5.2
      │
      ▼
  app/audit.py — append-only writer, no update/delete method — see §5.3
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

**The calibration plot is done** (`sim/render_calibration.py` → `docs/results/calibration.html`).
**It is not a clean confirmation, and do not "fix" the page to make it look like one.** The table's
realized rate orders close to monotonically; the model's does not — its own peak is at MEDIUM
(87%), not at VERY_HIGH (70%, nearly tied with LOW's 67%). That is a different axis from the
83%-vs-78% classification accuracy already reported (getting the failure class right vs. whether
the recovery bucket then orders realized outcomes), and both are true at once. `tests/test_calibration.py`
pins the specific numbers this claim depends on — if a future change moves them, update the page's
prose deliberately, the same rule as `test_reported_claims.py`.

### 5.2 ~~L3~~ — DONE

`app/executor.py` exists: `Executor` protocol, `FakeExecutor`, `RazorpayExecutor`. Only
`RETRY_NOW`/`RETRY_SCHEDULED` (→ Orders) and `REQUEST_INSTRUMENT_UPDATE` (→ Payment Links)
ever reach L3 — `EXECUTABLE_ACTIONS` is a checked boundary, not a convention.

**Idempotency was verified live, not assumed from documentation, because the documentation
was wrong.** A summary of Razorpay's docs claimed `order.create`'s `receipt` is treated as
an idempotency key. It is not — two live calls with an identical `receipt` created two
distinct orders (confirmed 1 September 2026). `payment_link.create`'s `reference_id`
*does* reject a duplicate. So for retries, the caller's `existing` argument (whatever
`ExecutionResult` already exists for `(invoice_id, attempt_no)`) is the **only** guard —
Razorpay provides none — and a real second layer only for `REQUEST_INSTRUMENT_UPDATE`.
Both are pinned by live tests in `tests/test_executor.py`. **Do not "fix" this by adding a
try/except around `order.create` for a duplicate-receipt case that does not fire** —
see `app/executor.py`'s module docstring before touching this.

**A real bug the live run caught:** the first `idempotency_key()` truncated the whole
formatted string to Razorpay's 40-char cap. A 36-char UUID `invoice_id` alone already
exceeds that budget, so `attempt_no` was silently dropped and every attempt on an invoice
collided onto one key. Fixed — reserve the suffix's space, truncate the invoice id, not
the finished string.

**The one real end-to-end trace is done too.** `sim/data/live_failure_capture.json` is a
genuine Razorpay test-mode decline (order created via `RazorpayExecutor`, checked out
through real `checkout.js` with a documented failing card, Failure clicked on the mock
bank screen — by the user by hand; **browser automation could not type into the nested
card-entry iframe**, in both the sandboxed pane and a real Chrome tab via Claude-in-Chrome,
clicks landed but keystrokes did not — worth knowing before trying again for §5.4).
`sim/demo_live_trace.py` runs that real decline through L1 → L2b → L2a and then executes
L2a's permitted action for real too (`REQUEST_INSTRUMENT_UPDATE` → a genuine payment
link). Re-running it creates a **new** real order/link each time — it is a demo script,
not idempotent across runs, unlike the executor it exercises.

Live tests need `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` in `.env` (gitignored,
`tests/conftest.py` loads it — no new dependency). Without them, `test_executor.py`'s live
tests skip and `sim/demo_live_trace.py` prints what L1/L2b/L2a decided without executing.

### 5.3 ~~`app/audit.py`~~ — DONE

`AuditLog` takes a caller-owned `Session`; typed methods (`classified`, `policy_decision`,
`executed`, `outcome_recorded`, `contact_sent`, `circuit_breaker_tripped/reset`) take the
actual domain object (`Classification`, `PolicyDecision`, `ExecutionResult`) rather than a
hand-assembled payload dict, so every call site produces the same payload shape.
`policy_decision()` writes one `STOPPING_RULE_FIRED` row per fired rule — tagged with its
`REGULATORY`/`BACKSTOP` basis from `app/rule_basis.py` — plus one `POLICY_PERMITTED` or
`POLICY_VETOED` row for the outcome, so the veto-rate-by-basis metric is readable straight
off the table.

**No update/delete method exists — checked structurally, not left as a comment.**
`test_append_only_no_update_or_delete_method_exists_on_the_class` walks `dir(AuditLog)`
for the words update/delete/remove/clear, so a future addition of `update_entry` fails a
test instead of quietly weakening the guarantee. **Do not add such a method** — if a row
is ever wrong, the correct fix is a new row, not an edit to an old one.

`AuditLog` flushes on every append but never commits — the caller's classify/score/
gate/execute cycle should commit as one unit, or a crash mid-cycle could leave a
committed audit row describing a decision whose `Attempt` update never landed.
`test_append_never_commits_the_session` proves this by rolling back and finding nothing
persisted.

**Not wired into `sim/run_arms.py`.** The simulator is a pure in-memory harness with no DB
and carries traces in memory by design (documented and accepted, not a gap) — `AuditLog` is
a standalone module ready for a real caller, e.g. a future `app/api.py` or `sim/demo_live_trace.py`.
Wiring it into the live demo trace so a judge can see a real audit trail for the real
declined payment would be a cheap, worthwhile follow-up if time allows — not required.

### 5.4 ~~Demo walkthrough~~ — DONE

`docs/demo-walkthrough.md`, linked from the top of the README. Sequences five artifacts —
frontier → synthetic per-bucket trace → the one real Razorpay decline (transcript quoted
verbatim, not paraphrased) → the adversarial result → the calibration check — with what to
look for in each, plus a two-minute version. **Do not let this drift from the artifacts it
describes.** If a regenerated page's numbers move (a constant changes, a seed changes), the
quoted transcript and any inline figures in this doc need updating by hand — nothing
regenerates it automatically the way the HTML pages regenerate themselves.

### 5.5 ~~Verify the RBI circular wording~~ — DONE, nothing needed correcting

Checked directly against the URL already cited as the primary source
(`sim/world_model_constants.RBI_EMANDATE_2026`), cross-confirmed against several independent
legal-publication summaries of the same circular. Every figure holds: circular number and date,
the 24h pre-transaction notice, its required contents (merchant name, amount, debit date/time,
mandate reference, debit reason), the per-transaction opt-out, and both AFA-exemption ceilings
(₹15,000 general, ₹1,00,000 for insurance/mutual funds/credit card bills). The 24h-notice/opt-out
requirement is Section 6(c) specifically; withdrawing the mandate entirely is a different right,
Section 4(b) — that section-level precision is now in the Source's citation text in
`sim/world_model_constants.py`.

**A real, unrelated bug surfaced by actually running the generator:** `sim/gen_world_model_doc.py`
had no explicit encoding on its `open()` call, and writing the Rupee sign on Windows (cp1252
default) raised `UnicodeEncodeError`. Fixed with `encoding="utf-8"`. If you touch a renderer that
writes a file, check it specifies an encoding — this project's default `open(..., "w")` calls do
not, and only this one happened to contain a character that made the gap visible.

**This closes every item on the original §5 list.** What remains is optional, not required:
running `LLMClassifier` against the real Anthropic endpoint (§5.1, needs a key), and any further
polish — e.g. wiring `AuditLog` into `sim/demo_live_trace.py` so the one real trace also prints its
own audit rows (§5.3 flagged this as cheap and worthwhile, not required).

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
- **Catching an exception type is not the same as catching the specific error you meant.**
  `RazorpayExecutor`'s payment-link path used to catch any `BadRequestError` from
  `payment_link.create` and treat it as the one case it exists for — a reused `reference_id` — and
  mark it `replayed=True`. A live run on 1 September 2026 hit a *rate-limited* `BadRequestError`
  instead, and the old code silently reported "this was already handled" when nothing had happened
  at all. Fixed by checking the message for Razorpay's actual duplicate-reference wording before
  treating it as the idempotency case; see `app/executor.py`'s `_DUPLICATE_REFERENCE_MARKER`. If you
  add another caught-exception branch anywhere that calls Razorpay, ask what ELSE could raise that
  same exception type before assuming the catch only fires for the case you had in mind.
- **This Razorpay test-mode account has a 30-payment_link cap**, discovered by exhausting it on
  1 September 2026 and confirmed still exhausted a full day later — it does not reset daily. This
  used to make two live tests fail outright. As of 2 September 2026 it no longer does:
  `tests/test_executor.py`'s `skip_on_environmental_failure()` converts the reproduced quota message
  (`ServerError: "test mode limit of ... reached for ..."`) and a reproduced rate-limit message
  (`BadRequestError: "Too many requests"`, triggered live by a 25-call burst the same day) into a
  `pytest.skip`, not a failure — so `python -m pytest` is green on a clean clone even when this
  account's quota is exhausted or the network is unreachable, and a red result keeps meaning "the
  code is wrong." A genuine credential error or any other Razorpay error still fails loudly; the
  function's own docstring has the exact line between the two, and
  `test_skip_on_environmental_failure_reraises_an_unrelated_razorpay_error` /
  `..._does_not_swallow_assertion_errors` guard that the catch stays narrow. **If a live test starts
  skipping that you did not expect to, read the skip reason before assuming it's this quota** — the
  message tells you which of the two reproduced signatures matched.

---

## 8. Commands

```
python -m pytest                                   # 288 tests, green (skips cleanly if the payment_link quota is exhausted - see §7)
python -m sim.generate_batch --n 120 --seed 42
python -m sim.run_arms --n 120 --seed 42
python -m sim.run_arms --adversarial
python -m sim.run_arms --belief-error 0.5 --belief-error 2.0
python -m sim.render_trace                         # docs/results/trace.html
python -m sim.render_frontier                      # docs/results/frontier.html
python -m sim.render_calibration                    # docs/results/calibration.html
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
  executor.py          L3: Executor protocol, FakeExecutor, RazorpayExecutor
  audit.py             append-only writer, no update/delete method (checked structurally)
sim/
  world_model.py             the WORLD's truth — not the agent's beliefs
  world_model_constants.py   the AGENT's beliefs, three-tier provenance
  generate_batch.py          40% clean / 35% ambiguous / 25% context, with ground truth
  run_arms.py                do-nothing / naive / rules-only / backstop
  demo_live_trace.py         the one real end-to-end trace (§5.2)
  data/live_failure_capture.json  a genuine Razorpay decline, committed like the L1 recording
  render_trace.py            docs/results/trace.html
  render_frontier.py         docs/results/frontier.html
  render_calibration.py      docs/results/calibration.html
  gen_world_model_doc.py     docs/world-model.md
tests/
  conftest.py           loads .env (RAZORPAY_KEY_ID/SECRET, ANTHROPIC_API_KEY) for pytest
  test_audit.py          audit.py's contract; the structural no-update/delete check lives here
  test_policy.py             149 tests with test_circuit_breaker  [FROZEN]
  test_circuit_breaker.py                                          [FROZEN]
  test_compliance_rules.py   interaction tests for the added rules
  test_scorer.py             including findings we would rather were untrue
  test_reported_claims.py    asserts the README's numbers against a live run
  test_l1_measurement.py     pins what the model bought, including where it TIES
  test_calibration.py        pins the calibration page's numbers, incl. the model's non-monotonic bucket order
  test_executor.py           L3 contract; 4 tests live-only (skip cleanly on missing keys,
                              network failure, or exhausted quota - see skip_on_environmental_failure())
  test_api.py                 the live console's endpoints, in-process TestClient, no server needed
docs/
  architecture.md  world-model.md (generated)  build-log.md  demo-walkthrough.md  results/
```

The world model and the agent's beliefs are **deliberately different functional forms** — saturating
vs geometric contact fatigue, linear vs exponential recency decay, per-customer vs flat LTV. That is
the anti-circularity mitigation. Do not collapse them into one set of constants.

---

## 10. The live decision console (new scope, 2 September 2026)

`app/api.py` (FastAPI) + `app/static/` (plain HTML/CSS/vanilla JS, no framework, no build step).
Requested directly by the project owner, not part of the original brief — see the build log's
2 September entry for the full reasoning. Run it with `uvicorn app.api:app --reload`.

**Every number it shows comes from the real pipeline.** `POST /api/decide` runs a picked case
through the actual `classify()` → `score()` → `evaluate()` chain — the same functions
`sim/run_arms.py` and `sim/demo_live_trace.py` call — and the frontend only renders the JSON it gets
back. Nothing about L1/L2b/L2a is reimplemented in JavaScript. If you ever find the console
disagreeing with `sim/render_trace.py` on the same case, that is a real bug in one of the two
wiring paths, not a rounding difference to shrug off.

**Section 6's "do not build a dashboard with a framework" rule still holds, and this does not
violate it.** That rule was written for the static judge-facing reports specifically, so a judge
never needs `npm install` to look at one. The console is a different kind of artifact — genuinely
interactive, needs a running process — but it keeps the same spirit: no React, no npm, no build
step. `pip install -r requirements.txt` and one `uvicorn` command is the entire setup. If a future
change to this console ever pulls in a JS framework or a build tool, that is a real regression
against this rule, not an exception to it.

**L3 defaults to `FakeExecutor`.** `execute_live` is an explicit, per-request opt-in
(`RazorpayExecutor`) - the console must never spend this account's Razorpay quota or create a real
order/payment link just because someone clicked through the case list. Do not change this default.

**Styled in Razorpay's brand colors, checked live against `razorpay.com`'s own computed styles
(2 September 2026), not guessed or copied from their actual page markup:** `#305eff` blue,
`#0d1a48` navy, `#009e5c` green, `#f0f4f6`/`#f8fafc` neutrals - see `app/static/style.css`'s
opening comment for the full palette and provenance. **This is deliberately Backstop's own console,
not a Razorpay site clone** - it says so in its own header ("Built for the Razorpay AI Builder
Internship 2026 buildathon — not a Razorpay product"), and it does not reproduce Razorpay's actual
marketing copy, layout, or logo. Keep it that way if you touch the styling - color language is fair
game, content and identity are not.

**`httpx` is a hard dependency now**, even though nothing imports it directly - `fastapi.testclient.TestClient`
(used by `tests/test_api.py`) requires it, and it is NOT a transitive dependency of `fastapi` itself
(checked: `pip show fastapi` lists no httpx). It was only present in the dev environment via
unrelated packages before this was caught - a fresh clone would have hit `ImportError` on the test
suite without the explicit pin now in `requirements.txt`.
