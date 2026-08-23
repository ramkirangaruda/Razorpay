# Backstop

**A payment recovery agent that knows when to stop.**

Built for Razorpay AI Buildathon 2026 — Track 3: AI Revenue Recovery. Submission deadline
4 September 2026.

Every other submission on this track will optimise for recovery rate. Recovery rate alone is
trivially gameable — retry everything forever and contact the customer daily. Backstop's thesis is
that the *restraint* is the product: knowing which failures are unrecoverable, which retries poison
future attempts, and which contacts burn a customer relationship worth more than the invoice.

Razorpay's stated bar for this track asks for measured money recovered, compliant escalation,
stopping rules, and an audit trail. Here, stopping rules are the core mechanism, and they are
measured — not a safety checkbox bolted on at the end.

> The name is a placeholder — kept for now, renameable freely, thesis stays the same.

## Scope boundary

**In scope:** failed one-time payments (card, UPI, netbanking decline paths); failed
recurring/subscription charges and mandate debit failures; a single merchant, INR only.

**Out of scope, and why:**

- *B2B receivables chasing* — different actor (a business AP team, not a consumer), different legal
  escalation ladder, different time constants. It deserves its own agent.
- *Checkout abandonment* — pre-authorisation, so there is no failure payload to classify. Different
  problem wearing the same coat.
- *Voice / Hinglish outreach* — a channel, not a decision problem. Adds demo sparkle and no depth.

## Architecture

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

**The critical property: L2 can only reduce the blast radius of L1's proposal, never expand it.**
The LLM cannot talk the system into a more aggressive action than the policy permits. Every veto is
logged with the rule that fired. See `docs/architecture.md` for the full writeup.

### The failure taxonomy

| Class | Recoverable? | Correct move |
|---|---|---|
| `SOFT_TRANSIENT` | Yes, high | Fast retry with backoff |
| `SOFT_FUNDS` | Yes, timing-dependent | Retry scheduled to likely inflow |
| `SOFT_LIMIT` | Yes | Retry with split amount, or alternate instrument |
| `SOFT_AUTH` | Yes | Fresh payment link — a retry cannot fix user drop-off |
| `HARD_INSTRUMENT` | No, not as-is | Request instrument update. Never retry. |
| `HARD_RISK` | No | Flag and escalate. Never retry, never nudge. |
| `HARD_MANDATE` | No, not as-is | Re-collect mandate authorisation |

Retrying a `HARD_RISK` decline is treated as an inviolable compliance rule, not a tuned parameter.
`SOFT_FUNDS` retry timing is India-specific: salary credit clusters around the 28th–31st of the
month (not the 1st), and scheduling that ignores this leaves money on the table — see
`docs/world-model.md` §2.3.

### Stopping rules

| Rule | Bound |
|---|---|
| `MAX_LIFETIME_ATTEMPTS` | 4 per invoice |
| `MIN_ATTEMPT_INTERVAL` | 24h, except `SOFT_TRANSIENT` (30 min, max 2 fast retries) |
| `CONTACT_FREQUENCY_CAP` | 3 customer contacts per 30 days, across all invoices for that customer |
| `HARD_DECLINE_NO_RETRY` | Zero retries on `HARD_*`. Inviolable. |
| `QUIET_HOURS` | No customer contact 21:00–09:00 IST |
| `ISSUER_CIRCUIT_BREAKER` | If rolling retry success rate for an issuer drops below threshold, pause all retries to that issuer |

## Measurement — three arms

Same batch, same seed, three strategies, per `docs/results/three-arm-comparison.md` (populated once
Arms A/B/C are running):

- **Arm A — Naive.** Immediate retry ×3, contact on every failure. What most systems actually do.
- **Arm B — Rules only.** The deterministic policy layer alone. No LLM anywhere.
- **Arm C — Full agent.** LLM classifier + policy gate.

Arm B is the arm that makes the submission credible — it answers "does the LLM actually beat a
decision table?" honestly, rather than assuming the LLM is doing the work.

**Policy veto rate** — how often L1 proposed an action L2 refused, broken down by rule — is reported
as a self-critical metric on Arm C, not hidden. See `docs/architecture.md`.

## Credibility risk: synthetic data

Synthetic data means the builder decides who would have paid — that's circular unless disclosed.
`sim/world_model.py` names every recovery-probability constant as `MEASURED_*` (cited source) or
`ASSUMED_*` (engineering judgment), documented in full in `docs/world-model.md`, and every
`ASSUMED_*` constant is swept across a plausible range in `docs/results/sensitivity-sweep.md` to
check whether Arm C's advantage survives the assumptions being wrong.

## Repo structure

```
backstop/
├── README.md
├── docs/
│   ├── architecture.md
│   ├── world-model.md           ← every assumed constant, with justification
│   ├── build-log.md             ← running log of what broke and how it was fixed
│   └── results/
│       ├── three-arm-comparison.md
│       ├── sensitivity-sweep.md
│       └── exception-list.md
├── app/
│   ├── models.py                 ← schema: invoices, attempts, audit log
│   ├── classifier.py             ← L1, the only file that touches an LLM
│   ├── policy.py                 ← L2, pure functions, zero I/O, exhaustively tested
│   ├── executor.py               ← L3, idempotent Razorpay calls
│   ├── stopping_rules.py         ← each rule a named, individually testable predicate
│   ├── circuit_breaker.py
│   └── audit.py                  ← append-only, never mutated
├── sim/
│   ├── world_model.py            ← disclosed recovery probabilities
│   ├── generate_batch.py         ← synthetic failures, seeded, realistic decline distribution
│   └── run_arms.py               ← A / B / C on identical seeds
├── dashboard/
└── tests/
    └── test_policy.py            ← the most important test file in the repo
```

## Status

Day 1 of a 12-day build (23 Aug – 4 Sep 2026), moving ahead of schedule. See `docs/build-log.md`
for the running log.

Done so far: repo scaffold, disclosed world model with cited sources, DB schema, seeded synthetic
batch generator (verified: `python3 -m sim.generate_batch --n 200 --seed 42`), and the full L2
policy layer — `app/policy.py`, `app/stopping_rules.py`, `app/circuit_breaker.py` — at **100%
branch coverage**, 149 tests (`python3 -m pytest tests/ -q`; `coverage run --branch --source=app.policy,app.stopping_rules,app.circuit_breaker -m pytest tests/`).
The never-upgrade invariant (L2 can only reduce blast radius, never expand it) is enforced at
runtime, not just tested: `evaluate()` raises `PolicyViolation` if any rule ever tries to increase
an action's rank, and a dedicated test injects a deliberately broken rule to prove that guard
actually fires rather than assuming it would.

Next: `classifier.py` (L1) with structured JSON output, then `executor.py` and the idempotency
plumbing against Razorpay test-mode.
