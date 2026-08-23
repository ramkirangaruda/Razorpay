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

## 2026-08-23 — continued: policy.py, stopping_rules.py, circuit_breaker.py (pulled forward from
the 25–27 Aug slot; moved faster than planned on day 1's momentum)

**The stale-lock filesystem issue from the local-folder bridge recurred and needed a firmer fix.**
Every git operation there was leaving an un-removable `index.lock`/`HEAD.lock` behind (delete is
restricted on that mount). Worked around it the same way as before — rename the stale lock out of
the way with `mv` before each git command that touches the index — but this is now clearly a
per-command tax on that path, not a one-off. Noting it here again because it'll recur every session
that writes through the desktop bridge until the repo is added to this session's authorized sources
and pushes can go straight from the cloud side instead.

**`vetoed` needed a real definition, not just "the action changed."** First cut of
`PolicyDecision.vetoed` was `action != proposal.proposed_action`. That made `RETRY_NOW` deferred to
`RETRY_SCHEDULED` by `MIN_ATTEMPT_INTERVAL` count as a "veto," which doesn't match how the build
spec talks about vetoes (a compliance-flavored refusal) versus downgrades (softened timing/severity,
same kind of action). Introduced action *families* (retry / contact / terminal) in `app/policy.py`
so `vetoed` is now "the family changed," and a same-family swap or timing push is `downgraded` only.
Caught by a failing test (`test_soft_funds_retried_too_soon_gets_rescheduled_not_vetoed`), not by
re-reading the spec — the test was right and the implementation's first assumption was wrong.

**Chasing 100% branch coverage on `policy.py` surfaced dead code, not just missing tests.** Two
branches wouldn't cover no matter what input was constructed for them. Rather than writing
contrived tests to hit lines that can never actually execute given the real rule set, traced why
they were unreachable and simplified the code instead: an `elif` guarding against a rule that
"changes the action to something other than RETRY_SCHEDULED" turned out to be true for every rule
in the pipeline by construction, so the condition was redundant; collapsed it to a plain `else` and
documented the invariant it relies on directly in `stopping_rules.py`'s `RULE_PIPELINE` comment
block. Coverage gaps here were a better signal than a linter would have been — they pointed at
actual unnecessary complexity, not just untested lines.

**`circuit_breaker.py`'s first version had a real bug, and it was pure luck it wasn't in `policy.py`
instead.** The rolling-window/cooldown logic reset trip state unconditionally on every window roll
(`state = fresh_window(...)`) despite the code comment right above it explicitly saying a trip
should persist until cooldown clears — comment and code disagreed, and nothing caught it until
`tests/test_circuit_breaker.py`'s `test_trip_persists_across_a_window_roll_before_cooldown_elapses`
failed. Fixed it, then a second test (`test_stays_tripped_past_cooldown_if_still_unhealthy`) failed
too, revealing the deeper problem: `RESET_COOLDOWN` and `ROLLING_WINDOW` were both 30 minutes, which
made the "cooldown elapsed but window hasn't rolled yet" code path essentially unreachable — by the
time cooldown expired, the window had already rolled over and cleared the trip unconditionally
anyway. Rather than shrinking `RESET_COOLDOWN` to paper over it, removed the separate cooldown
concept entirely: trip state is now recomputed live from whatever's in the *current* window on
every call, and `MIN_SAMPLE_SIZE` alone (not a timer) is what prevents flapping right after a roll.
Simpler, and every branch is now both reachable and tested at 100%. This is exactly the "measurement
apparatus lies to you" pattern the plan warned about, except this time it caught a bug in the system
under test, not in the harness measuring it — worth remembering that both directions are possible.

**Net result:** `app/policy.py`, `app/stopping_rules.py`, `app/circuit_breaker.py` at 100% branch
coverage (`coverage run --branch`, 149 tests, verified — not asserted from reading the code).
