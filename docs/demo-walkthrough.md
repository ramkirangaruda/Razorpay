# Demo walkthrough

Five artifacts, five minutes, in the order that makes the argument. Each one is generated
by a command you can re-run; none of it is hand-edited after the fact.

The claim, stated once: Backstop trades some recovered value for a large cut in
authorisation attempts and customer contacts, the trade is measured rather than assumed,
and every number here is either regenerable from a seed or a real Razorpay API response —
never both at once, and each artifact says which it is.

---

## 1. The frontier — the aggregate trade-off

**Open [`docs/results/frontier.html`](results/frontier.html)** (or regenerate:
`python -m sim.render_frontier`).

Read the scatter first, then the table under it. Four policies on a seeded 120-invoice
batch: Razorpay's own documented default (naive), rules with no economics, Backstop on the
decision table, Backstop on the measured LLM. The naive arm sits highest on raw value —
this page does not hide that — and furthest right, meaning the most customer contacts and
authorisation attempts per invoice recovered. Backstop sits lower and further left. The
dashed curve is one policy (table-classifier Backstop) swept across how strongly it
believes contacts cause churn; it traces a mechanism, not a cherry-picked point.

**What to look for:** the headline in the caption — Backstop takes roughly 93% of naive's
value on about half the authorisation attempts and 82–86% of the contacts. That is the
whole trade in one sentence, and the page states plainly that it does not beat the
baseline on raw value.

## 2. One invoice, end to end — the synthetic trace

**Open [`docs/results/trace.html`](results/trace.html)** (or regenerate:
`python -m sim.render_trace`).

Three invoices, one from each difficulty bucket the eval batch defines (clean, ambiguous,
context-dependent). For each: the real payload structure L1 receives, its classification
and written rationale, every term of the expected-value calculation broken out as its own
line (not a single opaque number), the policy gate's verdict, and — where relevant — which
rule fired and why. Ground-truth is shown alongside for a reader's benefit only; no layer
of the agent ever sees it.

**What to look for:** the AMBIGUOUS case. Watch the classifier reason through a payload
with a missing or contradictory `reason` field the way Razorpay's own documentation says
real bank declines often arrive — and watch the EV breakdown show which single term
(usually the lapse-avoided or churn term) actually decided the action, rather than asking
you to trust an aggregate score.

## 3. One real failure, end to end — not simulated

Run: `python -m sim.demo_live_trace` (creates a new real Razorpay test-mode order/link
each run — see the note at the end of this section if you don't have a key set).

This is the artifact that answers "have you actually touched Razorpay's API, or is this
all simulation." `sim/data/live_failure_capture.json` is a genuine declined payment:
an order was created via `RazorpayExecutor`, checked out through Razorpay's real test-mode
`checkout.js` with one of their documented failing test cards, and declined for real by
completing the mock bank's Failure step. The full provenance — how, when, which card — is
in that file, including a deliberate note on what was excluded (the real payment object
also carried a phone number and email from checkout; neither is needed for classification
and neither is kept).

The transcript from the run that produced the committed fixture:

```
ATTEMPT 1 - real, already executed against Razorpay test mode
razorpay_order_id:   order_TWmdhEisIcRhAM
razorpay_payment_id: pay_TWmt4DqxW5pZUm
error: {'code': 'BAD_REQUEST_ERROR', 'description': 'Payment failed',
        'source': 'gateway', 'step': 'payment_authorization', 'reason': 'payment_failed'}

L1 - classification (app/classifier.LookupClassifier)
classification:  SOFT_FUNDS        confidence: LOW      recovery_bucket: HIGH
proposed_action: RETRY_SCHEDULED
ambiguity_flags: ('unmapped_reason',)
rationale: reason='payment_failed' is not in the decision table; defaulted to SOFT_FUNDS

L2b - expected value (app/scorer.score)
REQUEST_INSTRUMENT_UPDATE  EV=   664.00  <- chosen
RETRY_SCHEDULED            EV=   465.64
NUDGE                      EV=   427.50
STOP_PERMANENT             EV=     0.00

L2a - policy gate (app/policy.evaluate)
permitted_action: REQUEST_INSTRUMENT_UPDATE   vetoed: False   downgraded: False
no rule fired

L3 - attempt 2 (app/executor.RazorpayExecutor)
outcome: PENDING   razorpay_order_id: plink_TWmzYg3YvKaVgY   error: None
```

**What to look for:** the reason field is `payment_failed` — Razorpay's mock bank's
generic decline, not the specific `gateway_technical_error` the test card is documented to
produce. That mismatch between documented and live behaviour is a recurring finding in this
project, surfaced by actually running the live path rather than trusting the docs. L1 correctly flags the reason as unmapped and falls back
honestly rather than guessing specifically; L2b prices asking for a new instrument above a
blind retry once the lapse and churn terms are in; L2a passes it through clean; L3 then
creates `plink_TWmzYg3YvKaVgY` — an actual Razorpay payment link, not a mock object.

*Without `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` set (see `.env`, gitignored), the script
still runs and prints what L1/L2b/L2a decided — it just stops before the live L3 call and
says so, rather than failing silently or fabricating a result.*

## 4. The adversarial world — the question a sharp reader asks first

Run: `python -m sim.run_arms --adversarial`

Contact fatigue and issuer penalties both set to zero in the *world* (not the agent's
belief) — a world where restraint buys nothing. Naive gains value here; Backstop loses
ground. That is the correct result, and it is reported rather than left for a judge to
have to ask about: in a world where restraint is free to skip, restraint is not worth
buying, and a simulator that couldn't produce this result when asked would not be
trustworthy on the result it reports everywhere else.

## 5. The calibration check — where the model's numbers are honest and where they aren't

**Open [`docs/results/calibration.html`](results/calibration.html)** (or regenerate:
`python -m sim.render_calibration`).

Does a higher predicted `recovery_bucket` actually correspond to a higher realized
recovery rate? The decision table orders close to monotonically. The LLM does not — its
own peak realized rate is at MEDIUM, not at its top bucket. This page reports that
directly rather than only reporting the classification-accuracy win (83% vs 78%) that
looks better on its own. Classification accuracy and bucket calibration are different
axes, and this batch shows the model ahead on one and behind on the other.

---

## The two-minute version, if that's all you have

1. Frontier caption: 93% of the value, ~half the attempts.
2. The live trace transcript in §3 above: a real Razorpay decline, run through the real
   pipeline, ending in a real payment link.
3. The adversarial number in §4: the advantage disappears when restraint stops paying for
   itself, and that's reported rather than hidden.

Everything else is the evidence behind those three lines.
