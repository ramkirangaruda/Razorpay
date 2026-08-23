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
