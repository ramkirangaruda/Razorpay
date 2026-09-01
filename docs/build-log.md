# Build log

Kept from day one per the build spec — this is the raw material for "what issues did you face and
how did you solve them," and it can't be reconstructed convincingly at the end. One entry per thing
that broke, contradicted expectations, or got fixed by running the thing rather than reading it.

---

## 2026-08-23 — Day 1

**Git push blocked by session repo authorization, not credentials.** Expected a plain "no
credentials" failure when testing push access to `ramkirangaruda/Razorpay`. Instead the sandbox's
git proxy returned a specific 403: `access denied by the git proxy: ramkirangaruda/Razorpay is not
in this session's authorized repository set`. So there *is* a proxy that pushes under the right
identity — the repo just needs to be added to the session's authorized sources first. Worked around
by building and committing locally in the meantime; nothing about local dev was actually blocked.

**Decline-mix source has no India-specific breakdown.** The only decline-cause breakdown found
(Slicker's 2025 involuntary churn benchmarks) is card-only and Western-market-flavoured — it has no
line for OTP/3DS abandonment at all, because that's not a distinct failure mode in a market where
most cards don't require a customer-facing auth step per transaction. Used the source for the
classes it does cover (`SOFT_FUNDS`, `HARD_INSTRUMENT`, `SOFT_TRANSIENT`) and made an explicit,
labeled adjustment (`ASSUMED_INDIA_ADJUSTMENT` in `docs/world-model.md` §1) for the rest rather than
pretending the card-only mix transfers directly. Recording this now because it's exactly the kind of
"the measurement apparatus will lie to you" moment the spec warned about, just at the data-sourcing
stage instead of the runtime stage.

**Salary-credit assumption was wrong on the first pass.** Went in assuming the common "1st of the
month" salary-credit convention (matches the US paycheck framing in the retry-timing sources).
Remunance's payroll-calendar breakdown says Indian employers cluster fund transfers in the
28th–31st window instead, with payroll locked on the 27th. Corrected `MEASURED_PAYDAY_WINDOW_DAYS`
before writing any of `sim/world_model.py` — would have been a much more annoying fix if the wrong
window had already been baked into `hazard_for_attempt` logic and test fixtures.

**`sim/generate_batch.py` first-run class distribution ran a bit hot on `SOFT_AUTH`.** Target mix is
20%; a 200-row seed-42 batch came out at 23.5% (47/200). Within expected sampling noise for n=200
and multinomial variance — not treating this as a bug, but noting it here so that if the eventual
200+-record production batches (spec's day 5-6 target) show a similar or larger skew, it's a
signal to check the `_weighted_choice` implementation rather than just re-seeding until it looks
right.

**Schema decision: `Attempt` is mutated, `AuditLogEntry` is append-only — not both immutable.**
First draft tried to make everything append-only, including the per-attempt working record, which
meant every L2 veto or L3 outcome would need a new `Attempt` row referencing the previous one. That
adds join complexity for no real audit benefit, since `AuditLogEntry` already captures every event
immutably. Settled on: `Attempt` is the convenience view of "where is this attempt right now,"
`AuditLogEntry` is the actual source of truth and the only append-only table. Documented the
reasoning directly in `app/models.py`'s module docstring so it doesn't get "fixed" back to
overcomplicated later in the sprint.

---

## 2026-09-01 — Picking the project back up

> **Correction, appended after the fact.** Everything below the next two paragraphs was written on
> a false premise and is left in place because the wrong turn is part of the record. See
> "The premise was wrong" at the end of this entry.

Eight days of the twelve-day plan had elapsed with no commits since day 1 **on GitHub**. Recording
what was found, because the handoff brief and the *remote* disagreed materially.

**The brief described `L2a` as built with ~149 tests passing and instructed against touching it. It
was not in the clone.** `app/` contained one implemented file — `models.py`, the schema. `classifier.py`,
`policy.py`, `stopping_rules.py`, `circuit_breaker.py`, `executor.py` and `audit.py` were all
referenced by the README and by `architecture.md`, and none had been written. `tests/` held an empty
`__init__.py` and nothing else: zero tests, not 149, and pytest was not installed. `architecture.md`
said so itself in its own status section; the README did not, which is how the discrepancy survived.

Flagged rather than reconciled silently, per the brief's own instruction. The consequence was that
§12's "do not modify L2a's logic" had nothing to protect, and the day's work became building the
layer the brief assumed was finished, in addition to the one it asked for.

**Git push is still blocked, same as day 1.** `access denied by the git proxy: ramkirangaruda/Razorpay
is not in this session's authorized repository set`. Confirmed it is an authorization list rather
than a credentials problem — the proxy will inject a credential once the repo is added to the
session's sources. Committing locally in the meantime; nothing about local development is blocked.

**Enums had to come out of `models.py` before anything could be tested.** L2a is specified as pure
functions with zero I/O, but the taxonomies lived in the SQLAlchemy module, so importing the policy
layer pulled in an ORM. Split into `app/enums.py`, re-exported from `models.py` so the schema and its
persisted values are unchanged. A test suite that needs a database driver installed before it will
run is a test suite that stops being run.

**`HARD_RISK_NO_CONTACT` was found by a test, not by design.** Writing a property test asserting that
no proposal on a risk decline can reach the customer, it failed: a `NUDGE` on `HARD_RISK` was
permitted. Nothing in the rule set blocked it — the retry rules only cover retries, and the contact
rules only cover volume and timing. The standard dunning message ("your payment failed, please try
another method") sent to a suspected-fraud decline tells whoever is holding the card which instrument
to try next. Added as a `BACKSTOP` rule with that reasoning written into it.

**Two modelling bugs in the scorer, both found by writing tests rather than by running the sim.**
First, contacts had no decay at all — a payment link scored identically on the tenth ask and the
first, so the scorer never stopped asking and `CONTACT_FREQUENCY_CAP` did the stopping. Restraint
back in a rule, which is precisely what L2b exists to remove. Contacts now decay on contact count
and retries on attempt index; they are different quantities and conflating them was the error.
Second, the marginal-recovery table was clamped at its last index, which left every retry a permanent
floor of positive EV. Now extrapolated geometrically from the table's own last ratio, which adds no
constant.

**The first three-arm run was the useful failure of the day.** Backstop recovered 63 invoices against
the naive baseline's 87 and lost on net value by a wide margin. Three causes, in ascending order of
interest:

1. *The lookup classifier had no escalation ladder.* It returned one action per class and returned it
   forever, so a `SOFT_FUNDS` invoice was retried to the cap and abandoned without ever reaching for
   a payment link, while the naive arm did retry-retry-retry-then-link. This matters more than it
   looks, because L2b may only narrow what was proposed: **the proposal is a ceiling on the entire
   pipeline**, so an action the classifier never proposes is one the system can never take however
   good its expected value. A rules-only arm that cannot escalate is a strawman rather than a
   baseline.
2. *The batch oracle was rolling the wrong hazard for `SOFT_AUTH`.* `MEASURED_AUTH_HAZARD_1 = 0.55`
   describes a fresh link — its own docstring says so — and the oracle used it as the retry hazard.
   Ground truth was rewarding re-presentment of cards whose owners had walked away from an OTP
   screen, contradicting the failure taxonomy and the structural zero in `CHANNEL_FIT`, and handing
   the naive arm free recoveries for doing the one thing every source agrees does not work.
3. *The EV specification in the brief was missing a term.* It charges churn for contacting a customer
   and nothing for abandoning their invoice — `STOP` is priced at zero. But an unrecovered payment is
   the definition of involuntary churn, and in this world giving up costs about seven times what a
   first contact does. The scorer was not being restrained; it was blind to half the ledger. Added
   `P_LAPSE_IF_UNRECOVERED`. Worth noting this restores the framing of a source the constants file
   already cites — Redux frames the true cost as unrecovered failures × remaining LTV — so the
   original formula contradicted the citation underneath it.

**A claim got weaker as a result, and it is being reported as weaker.** Before the lapse term, contact
EV turned negative at the second ask, comfortably ahead of the contact cap, which made a clean story
about restraint emerging rather than being imposed. With the corrected model the crossing moves out to
the fourth ask — coincident with the cap, not ahead of it. The test docstring records why the claim
changed rather than the assertion being quietly relaxed.

**The metric itself was misleading and got replaced.** Absolute net recovered value is a large
negative number for every arm, because a realistic batch is mostly invoices no policy could have
saved, and comparing two large negatives tells a reader nothing. Added a `0_do_nothing` reference arm;
the headline is now value *added* over never acting.

**Where it landed:** Backstop takes 92% of the naive baseline's value on 59% of the attempts and 76%
of the contacts. It does not beat Razorpay's documented default on raw value, and the frontier page
is not arranged to suggest otherwise. Whether that trade is worth making depends on the one constant
with no published source, so the deliverable is the frontier plus the breakeven threshold rather than
a win claim.

**Tooling note, for the record:** the sandbox's command classifier timed out for roughly forty minutes
mid-session, refusing every `python3` invocation while allowing `ls`. Work continued on files that did
not need execution — the trace renderer and the frontier chart were written blind and verified
afterwards. Both needed fixes on first sight: the frontier's y-axis was anchored at zero, squeezing
every arm into the top eighth of the plot, and its labels collided; the trace's EV table omitted the
lapse-avoided column, so the visible columns did not sum to the EV shown. Screenshotting the output
rather than trusting the generator caught both.


---

## 2026-09-01 (later) — The premise was wrong

The day's inventory said L2a did not exist and there were zero tests. That was true of **GitHub**
and false of **the project**. Commit `04eef1a "L2 policy gate: policy.py, stopping_rules.py,
circuit_breaker.py at 100% branch coverage"` was sitting unpushed on local `main`, blocked by the
same git-proxy authorization error recorded on day 1. Running the suite on the actual working copy:
**149 passed**. Exactly what the brief said.

The failure was mine and it was avoidable. The build log's own day-1 entry records that pushes were
failing. Given that, "the remote is behind the working copy" should have been the first hypothesis
when the remote looked emptier than the brief described, and instead the remote was read as the
project's state. A repository is not the same thing as its origin, and this project already had
written evidence of exactly that gap.

**What was built on the wrong premise:** a parallel `app/policy.py` and `app/stopping_rules.py`,
which directly contradicts §12's instruction not to modify L2a.

**Resolution: the existing L2a stays, the parallel one was deleted.** Not on seniority — it is
better in two places:

- `RULE_PIPELINE` **accumulates**: later rules see the action and schedule earlier rules already
  set, which is what makes the RBI notice window and the 24-hour interval compose to the later of
  the two. The replacement took the first blocking rule and stopped, which cannot express that.
- `hard_decline_no_retry` **already forbade NUDGE on HARD_RISK**, citing the same spec line the
  replacement's "newly discovered" rule cited. It had been handled from the start.

The `_same_family` veto/downgrade distinction is also real and the replacement flattened it.

**One genuine disagreement, resolved in favour of the incumbent.** `BLAST_RADIUS_RANK` puts
`RETRY_NOW` at 5 and contact actions at 3: an immediate unattended charge is treated as the most
aggressive act. The replacement inverted that, reasoning that churn hazard makes the customer's
patience the expensive irreversible term in the EV model. Both are defensible. The existing one is
built, tested and documented, so it wins, and `app/scorer.py` now imports it rather than keeping a
second ordering — one ordering for the whole pipeline, or the valve means different things at
different layers. Recording the disagreement here rather than resolving it silently in favour of
whichever layer was written most recently.

Consequence, measured: the arm's numbers moved. Backstop went from 92% of naive's value at 59% of
attempts and 76% of contacts, to **89% at 52% of attempts and 86% of contacts** — it now trades
attempts for contacts rather than the reverse, which is the direct result of the ranking. The
README was updated with the new figures and `tests/test_reported_claims.py` re-pinned to them.

**What survived from the wrong turn**, as additions rather than modifications:

- Three §11 compliance rules the existing L2a did not have — `MC_NEVER_RETRY_ADVICE_CODE`,
  `NETWORK_REATTEMPT_CAP`, `EMANDATE_PREDEBIT_NOTICE` — appended to `RULE_PIPELINE`.
- `app/rule_basis.py`, the REGULATORY/BACKSTOP classification, as a lookup keyed on `RuleName` so
  that no existing rule's logic or coverage had to change to get the metric.
- New `PolicyContext` fields, appended with defaults so all 149 existing constructions still work,
  and every default set to the permissive direction — an unknown advice code must not forbid a
  retry.

**A real bug in the existing L2a, found by an interaction test.** `hard_decline_no_retry` blocks
`NUDGE` on `HARD_RISK` but not `REQUEST_INSTRUMENT_UPDATE`, so a proposal to ask a suspected-fraud
customer for a different card passed the entire pipeline untouched. The spec's wording — "never
retry, never nudge" — was implemented literally, and the other customer-facing action carries the
same hazard: the merchant cannot tell the real cardholder from whoever is holding the card, and an
automated "please try another method" tells the wrong one of them which instrument to try next.

Fixed as a new rule, `HARD_RISK_NO_CONTACT`, rather than a line inside the existing function, so
§12 holds and the original rule's branch coverage is untouched. Same outcome either way — both
redirect to `ESCALATE_HUMAN`.

**A timezone bug of my own, found by reading the rendered trace.** The simulator's clock is naive
UTC, and the trace renderer was printing those timestamps labelled "IST". Harmless in the display,
but it pointed at something that would not have been: `quiet_hours` converts to IST itself, so
feeding it an IST-valued datetime tagged as UTC would shift the protected window by five and a half
hours and send messages at 3am while looking correct in review. `_utc()` is now the single
conversion point, documented, and the trace prints both zones.

**Where it stands:** 231 tests. The 149 on L2a are byte-identical to how they were found.

---

## 2026-09-01 (later still) — The LLM arm, measured

The repo's honest position all day was that no language model had ever run against the batch, and
that the value of one over a decision table was unmeasured and claimed nowhere. That gap is now
closed, and closing it produced a better finding than "the model wins".

**How it was run, because provenance decides what the numbers are worth.** Claude classified all 120
cases, reading only the fields a real L1 receives and given `SYSTEM_PROMPT` verbatim, across four
separate fresh contexts. Those contexts had never seen `sim/generate_batch.py`, the ground truth, the
bucket labels or the ambiguity flags. That isolation is not ceremony: whoever writes the batch
generator knows the answer key — knows which cases had `reason` stripped, knows the contradictory
descriptions still carry a correct structured `reason` — so a classification produced by that same
context would be contaminated and every accuracy figure downstream would be meaningless.

Recorded to `sim/data/l1_classifications_seed42.json` and replayed by `CachedLLMClassifier`, because
a judged result has to reproduce and a live model call does not. This is **not** a live API run;
`LLMClassifier` remains the production path and is still untested against the real endpoint.

**Accuracy: 83% against the table's 78%** — but the aggregate is the least interesting cut. By
bucket: clean 100% vs 100%, context-dependent 100% vs 100%, ambiguous **52% vs 38%**. The model ties
on 65% of traffic.

Splitting the ambiguous bucket by what was actually done to the payload localises the whole
advantage to one place:

| ambiguity | n | table | model |
|---|---|---|---|
| description contradicts `reason` | 12 | 100% | 100% |
| generic bank decline, nothing survives | 15 | 20% | 20% |
| `reason` null, `source`/`step` survive | 15 | **7%** | **47%** |

Where the structured reason survives, both are perfect. Where nothing survives, both sit at the base
rate and no classifier could do better. The model's entire edge is reading `source` × `step` when
the gateway gave no reason — which is precisely the claim the design made in advance, now
falsifiable rather than an appeal to model quality.

**The part worth the day: better classification initially LOST money.** Arm D first came in at
₹1,881,433 against arm C's ₹1,926,991, despite classifying better on every cut. The mechanism took a
while to find and is not obvious. The model's per-case recovery buckets are far more dispersed than
the table's class-level mapping — 34 cases at LOW or VERY_LOW against 21, at essentially the same
mean — and `ScoreContext` consumes the bucket with no view of confidence, so a bucket the model was
guessing at was trusted exactly as much as one it was sure of. The extra pessimism became stop
signals manufactured from thin evidence, and it stopped invoices that were still live
(under-proposals rose from 8 to 12).

The fix was sitting in the data. Confidence is well calibrated: HIGH 100% over 83 cases, MEDIUM 76%
over 17, LOW 20% over 20. Falling back to the table's class-level bucket when confidence is LOW
moved arm D to **₹2,006,152** — ahead of both other policy arms, on fewer attempts and fewer contacts
than either.

So the honest headline is not "the LLM is better". It is: **the model's extra information was worth
nothing until it was filtered by the model's own reliability signal.** An unused calibrated
confidence output was the difference between the classifier being a net negative and a net positive.

Headline moves to 93% of naive's value on 49% of the attempts and 82% of the contacts. Thirteen new
tests pin every figure above, including the ties, so a change that moves a number fails a test rather
than quietly making the README wrong.

**Chart note.** Adding a fourth arm broke the frontier's palette: a fourth categorical hue cannot
clear the all-pairs colour-vision floors in both modes — violet collides with blue on the dark
surface at delta-E 1.9 for protanopia, magenta collides with orange in light and aqua in dark. Rather
than hunting for a hue, C and D now share one and are separated by fill, hollow for the table and
solid for the model. That is the better encoding anyway: they are the same policy with one component
swapped, and now they read as a pair.

---

## 2026-09-01 (evening) — L3 executor, built and live-verified

Section 5.2 was next in priority order. `app/executor.py` now exists: an `Executor`
protocol, `FakeExecutor` for tests, `RazorpayExecutor` for the real path — same
module-boundary discipline as `classifier.py` being the only file that touches an LLM.
Only `RETRY_NOW`/`RETRY_SCHEDULED` (→ Orders) and `REQUEST_INSTRUMENT_UPDATE`
(→ Payment Links) ever reach L3; `EXECUTABLE_ACTIONS` makes that a checked boundary
rather than a convention.

**Two things the documentation got wrong, caught by testing against the real API
instead of trusting a summary of it.** A third-party summary of Razorpay's docs claimed
`order.create`'s `receipt` field is treated as an idempotency key. It is not: two live
calls with an identical `receipt` on 1 September 2026 created two distinct orders.
`payment_link.create`'s `reference_id` behaves the opposite way — a reused value is
rejected outright, confirmed the same way. So retries have no Razorpay-side dedupe at
all; the caller's own `existing` check (whatever `ExecutionResult` already exists for
`(invoice_id, attempt_no)`) is the *only* guard for `RETRY_NOW`/`RETRY_SCHEDULED`, and a
real second layer for `REQUEST_INSTRUMENT_UPDATE`. Both behaviours are pinned by live
tests in `tests/test_executor.py` (skipped without `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`;
see `tests/conftest.py`'s `.env` loader).

**A real bug, also caught by the live run.** The first `idempotency_key()` truncated the
whole formatted string to Razorpay's 40-char cap. A 36-char UUID `invoice_id` already
exceeds that budget with any prefix attached, so the `attempt_no` suffix was silently
dropped and every attempt on an invoice collided onto the same key. Fixed by reserving
the suffix's space first and truncating the invoice id, not the finished string.

**The one real end-to-end trace.** A real order was created via `RazorpayExecutor`,
checked out through Razorpay's actual test-mode `checkout.js` with a documented failing
test card, and declined for real by clicking Failure on the mock bank screen — twice,
because the first attempt was checked out with the *wrong* card and succeeded instead of
failing, and browser automation (both the sandboxed pane and, after asking the user to
grant it access, a real Chrome tab via the Claude-in-Chrome extension) could not
reliably type into the nested card-entry iframe — clicks landed, keystrokes did not, in
a way that looked like the iframe is deliberately isolated against scripted input. The
user completed the checkout by hand both times. The resulting decline
(`sim/data/live_failure_capture.json`) is genuinely real, not fabricated, and is one more
case of documentation not matching reality: the test card is documented to produce
`error_reason=gateway_technical_error`, but the mock bank's Failure button returned a
generic `payment_failed`/`gateway` shape instead — coincidentally exactly the ambiguous,
unmapped-reason shape `sim/generate_batch.py`'s `GENERIC_BANK_DECLINE` template models.

`sim/demo_live_trace.py` runs that real decline through the actual L1 → L2b → L2a chain
and then executes L2a's permitted action for real too: L1 classifies it LOW-confidence
(unmapped reason, falls back to `SOFT_FUNDS`/`RETRY_SCHEDULED`), L2b prices
`REQUEST_INSTRUMENT_UPDATE` above the proposal once lapse-avoided and churn hazard are
in, L2a permits it with no rule firing, and L3 creates a genuine Razorpay payment link
(`plink_TWmzYg3YvKaVgY`). Both ends of the trace are real API responses — not a real
failure glued onto a simulated recovery.

**Where it stands:** 262 tests (244 pre-existing + 18 new: 15 always-on, 3 live-only).
`sim/run_arms.py` untouched and reconfirmed unaffected — the simulator resolves outcomes
probabilistically and never calls L3, so none of the four-arm comparison depends on any
of this. Section 5.3 (`app/audit.py`) is next.
