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
│ L2  POLICY GATE (no LLM)  │  → may VETO or DOWNGRADE. Never upgrades. Pure deterministic code.
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
`app/audit.py` is the only file permitted to write to it, and exposes no update/delete methods.

## Status

Written day 1, alongside the schema it describes. `classifier.py`, `policy.py`,
`stopping_rules.py`, `circuit_breaker.py`, `executor.py`, and `audit.py` are not yet implemented —
next on the build plan (25–30 Aug). This document describes the target shape; if implementation
forces a deviation, the deviation and its reason go in `docs/build-log.md`, and this file gets
updated to match, not silently left stale.
