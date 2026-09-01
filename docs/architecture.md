# Architecture

## Why three layers, not one

A single LLM call that both classifies a failure and decides the action is the obvious design and
the wrong one for money movement. The brief's bar — "every money action explainable, bounded and
gated" — is a claim about the *system*, not about prompt quality. A system makes that claim
credibly only if the component that gates money movement cannot be argued with, and an LLM,
however well-prompted, can be argued with. So the classification (where judgment and free-text
reasoning genuinely help) and the gating (where the requirement is "never do the wrong thing,"
not "usually do the right thing") are split into layers with different failure modes.

```
failure payload + customer history
        │
        ▼
┌───────────────────────────┐
│ L1  CLASSIFIER  (LLM)     │  → failure class, proposed action, written rationale (structured JSON)
└───────────────────────────┘
        │  proposes
        ▼
┌───────────────────────────┐
│ L2b EV SCORER (no LLM)    │  → prices every candidate action. STOP scores zero.
└───────────────────────────┘
        │  may narrow, never widen
        ▼
┌───────────────────────────┐
│ L2a POLICY GATE (no LLM)  │  → may VETO or DEFER. Never upgrades. Pure deterministic code.
└───────────────────────────┘
        │  permits
        ▼
┌───────────────────────────┐
│ L3  EXECUTOR              │  → idempotent call to Razorpay test-mode API. Key = (payment_id, attempt_no)
└───────────────────────────┘
        │
        ▼
   APPEND-ONLY AUDIT LOG   (decision · rationale · veto · execution · outcome)
```

## L1 — Classifier

Input: the failure payload (decline reason code/text, issuer, instrument type) plus the customer's
attempt and contact history for this invoice and this customer. Output: structured JSON — a
`FailureClass`, a proposed `InterventionAction` (optionally with a scheduled timestamp), and a
short written rationale. Low temperature; the rationale is not decoration, it's what a human
reviewing a veto or an escalation reads first.

`classifier.py` is the *only* file in the repo that calls an LLM. Nothing else — not `policy.py`,
not `stopping_rules.py`, not `executor.py` — has a code path that reaches a model. That's
enforced by the module boundary, not just convention, so that "which parts of this system touch an
LLM" is answerable by grepping for one filename.

## L2 — Policy gate

Pure functions, zero I/O, exhaustively tested (`tests/test_policy.py` is the most important test
file in the repo, by design). Given L1's proposal plus the deterministic state needed to evaluate
the stopping rules (attempt count, last attempt time, contact count in the trailing 30 days, issuer
circuit-breaker state), L2 returns either the proposal unchanged, a downgraded action, or a veto —
and if it vetoes or downgrades, it names the specific rule from `stopping_rules.py` that fired.

**The one property that matters most: L2 can only reduce the blast radius of L1's proposal, never
expand it.** If L1 proposes `NUDGE` and the policy gate would have permitted `RETRY_NOW`, the
system still only sends a nudge — L2 has no "upgrade" path, by construction. This means the ceiling
on how aggressive the system can be is set entirely by the LLM's own proposal, and the floor on how
*careful* it is set entirely by deterministic code the LLM cannot influence. A classifier that
misjudges a `HARD_RISK` decline as recoverable and proposes `RETRY_NOW` gets vetoed by
`HARD_DECLINE_NO_RETRY` outright — not downgraded to a gentler retry, blocked.

### Policy veto rate

Every veto or downgrade is logged with the rule name. The aggregate — **how often L1 proposed
something L2 had to refuse, broken down by rule** — is reported as a headline metric on Arm C, not
buried. It is a self-critical number: high veto rate is a finding about LLM reliability in money
movement (published as such, not apologized for); a veto rate near zero is evidence the classifier
is well-calibrated to the policy it operates under. Either reading is a better answer to "how do you
know the agent won't do something stupid" than an architecture diagram alone.

## L3 — Executor

Idempotent calls to the Razorpay test-mode API. Idempotency key is `(razorpay_payment_id,
attempt_no)`, enforced at the DB layer (`app/models.py`, `Attempt.__table_args__`) as well as in
application logic — a retried executor call (crash-and-resume, at-least-once delivery from a queue)
must not double-charge or double-send. `executor.py` is the only file that makes an outbound
Razorpay API call.

## Audit log

Append-only, one row per event (`CLASSIFIED`, `POLICY_PERMITTED`, `POLICY_VETOED`,
`STOPPING_RULE_FIRED`, `EXECUTED`, `OUTCOME_RECORDED`, `CONTACT_SENT`,
`CIRCUIT_BREAKER_TRIPPED/RESET` — see `AuditEventType` in `app/models.py`). This table, not the
`Attempt` row, is the actual source of truth for "what happened and why" — `Attempt` is a mutable
convenience view of an attempt's current state; `AuditLogEntry` is never updated or deleted.
`app/audit.py` is the only file permitted to write to it, and exposes no update/delete methods -
built and tested (`tests/test_audit.py`), including a structural test that the class has no method
whose name contains update/delete/remove/clear. Not yet wired into `sim/run_arms.py`, which is a
pure in-memory harness with no DB by design; `AuditLog` is a standalone module ready for a real
caller.

## The L2 split

The original design had one policy layer answering "is this action allowed?". It now has two:

- **`L2b` (`app/scorer.py`) asks whether an action is WORTH taking.** Expected value, every term
  traceable to a citation. Stop is not a rule here — it is what wins when nothing else scores above
  zero.
- **`L2a` (`app/policy.py`, `app/stopping_rules.py`, `app/circuit_breaker.py`) asks whether it is
  ALLOWED.** This layer was already built and tested at 149 tests; the split wrapped it rather than
  rewriting it.

`app/rule_basis.py` classifies every rule as `REGULATORY` (Indian law or a card-network rule, not
ours, nothing to sweep) or `BACKSTOP` (ours, bounding scorer error). That reclassification is not
cosmetic: a `REGULATORY` veto is compliance working as designed, while a `BACKSTOP` veto is a
finding that the arithmetic should have stopped us first — and the veto-rate metric separates them
so "we were saved by a rule" cannot be reported as "the model was right".

It is a lookup keyed on `RuleName` rather than a field on `RuleOutcome` precisely so that the
existing rule logic and its branch coverage stay untouched.

## Rules added since

Three came from build spec §11, which asks for the Indian and card-network constraints to be
encoded in L2a with citations:

- `MC_NEVER_RETRY_ADVICE_CODE` — MAC 03/21. Deliberately separate from `HARD_DECLINE_NO_RETRY`
  because it fires on the **network's** label rather than our classification, so it still catches
  the case where L1 read a fraud decline as `SOFT_FUNDS`. A backstop that only works when our
  classification was already right is not a backstop against it being wrong.
- `NETWORK_REATTEMPT_CAP` — Visa 15/card/30d, Mastercard 10 auths/PAN/24h. Not redundant with
  `MAX_LIFETIME_ATTEMPTS`, which counts attempts on one *invoice*; these count attempts on one
  *card* across every invoice it backs.
- `EMANDATE_PREDEBIT_NOTICE` — the RBI 24-hour notice, buffered to 26h. Scoped to the e-mandate
  rail and to retry actions only. A consequence worth naming: this makes "retry within the hour"
  illegal on the Indian recurring rail, so Mastercard advice code 24 is unfollowable there. Where
  the network and the regulator disagree, the regulator wins.

A fourth, `HARD_RISK_NO_CONTACT`, came from an interaction test rather than from the spec — see the
build log entry for 1 September.

All four are additions to `RULE_PIPELINE`. None modifies an existing rule.

## Deferral is not a veto

`quiet_hours` and `emandate_predebit_notice` block because of *when* it is, and both know the moment
they stop binding, so they return a `forced_scheduled_for` rather than a refusal. The simulator
honours it by advancing its clock instead of closing the invoice.

Collapsing "not until 09:00" into a stop would abandon invoices that a few hours' wait recovers, and
would turn the quiet-hours rule — which exists to protect a customer from a 3am message — into a
rule that costs them their subscription.

Because `RULE_PIPELINE` accumulates, a debit pushed to the RBI notice window can still be pushed
further out by `min_attempt_interval`: the later rule sees the schedule the earlier one set, and the
result is the later of the two constraints rather than whichever fired last.

## A note on clocks

The simulation clock is **UTC**. `PolicyContext.now_utc` is timezone-aware, and `quiet_hours`
converts to IST itself via `PolicyContext.now_ist`. Feeding it an IST-valued datetime tagged as UTC
would shift the protected window by five and a half hours and silently send messages at 3am — a bug
that would look entirely correct in review, which is why `sim/run_arms._utc` is the single place the
conversion happens and why the trace renderer prints both zones.

## Status

**Built:** `models.py`, `policy.py`, `stopping_rules.py`, `circuit_breaker.py` (L2a, pre-existing),
`rule_basis.py`, `scorer.py` (L2b), `classifier.py` (L1 interface, lookup + LLM implementations),
`executor.py` (L3: `Executor` protocol, `FakeExecutor`, `RazorpayExecutor`) — live-verified against a
real Razorpay test-mode account, including one full real end-to-end trace
(`sim/demo_live_trace.py`): a genuine declined payment, classified, priced, gated, and followed by a
genuine payment link created in response. See `docs/HANDOFF.md` §5.2 and `docs/build-log.md`'s
1 September evening entry for two places the live API's actual behaviour did not match its
documentation, and `audit.py` (append-only writer, `AuditLog`, no update/delete method - checked
structurally by `tests/test_audit.py`). 275 tests, of which the 149 on L2a are unmodified.

**Not built:** nothing on the original L1/L2/L3/audit list. `audit.py` is not yet wired into
`sim/run_arms.py`, which stays a pure in-memory harness with no DB by design - see §5.3 of
`docs/HANDOFF.md`.
