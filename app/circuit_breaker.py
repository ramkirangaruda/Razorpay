"""
Issuer circuit breaker — build spec §6 (ISSUER_CIRCUIT_BREAKER) and §5 (the flash-sale transplant:
"a crowd of retries that keep failing does not merely waste attempts, it degrades the success rate
of the retries that would otherwise have worked").

Pure logic, zero I/O, same as stopping_rules.py: this module takes a rolling window of recent
outcomes for one issuer and answers "is this issuer tripped right now, and when might it next get
re-evaluated." Persisting the window (app/models.py's IssuerCircuitBreakerState) and feeding
outcomes in as they happen is the caller's job — production would back the rolling window with
Redis (per build spec §9) for low-latency reads on every classification; this module doesn't care
where the window came from.

Threshold and window are ASSUMED_* constants mirroring sim/world_model.py — see docs/world-model.md
§3 for why they're the largest unvalidated assumption in the whole system and are swept in the
sensitivity analysis rather than hardcoded with false confidence.

Design note (docs/build-log.md, 23 Aug): the first version of this module had a separate
RESET_COOLDOWN on top of the rolling window, meant to stop a breaker from flapping trip/reset the
instant one retry succeeded. It was buggy (a window roll could silently clear an active trip) and,
once fixed, turned out to be redundant with ROLLING_WINDOW itself — with a 30-minute window and
MIN_SAMPLE_SIZE=5, a fresh window can't accumulate enough evidence to re-trip within the time a
separate cooldown would have blocked anyway. Removed it. Trip state is now recomputed live from
whatever's in the *current* window every call: tripped is true iff the window has at least
MIN_SAMPLE_SIZE samples and its success rate is below DEGRADATION_THRESHOLD, full stop. The
MIN_SAMPLE_SIZE requirement is what prevents flapping — a freshly-rolled window with 1 sample can't
trip regardless of that sample's outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

ROLLING_WINDOW = timedelta(minutes=30)
DEGRADATION_THRESHOLD = 0.20   # trip when the window's success rate drops below this
MIN_SAMPLE_SIZE = 5            # don't trip on noise from a handful of attempts


@dataclass(frozen=True)
class IssuerWindowState:
    issuer: str
    window_start: datetime
    success_count: int
    fail_count: int
    tripped: bool
    tripped_at: datetime | None  # when `tripped` most recently flipped True; None while untripped

    @property
    def total(self) -> int:
        return self.success_count + self.fail_count

    @property
    def success_rate(self) -> float | None:
        if self.total == 0:
            return None
        return self.success_count / self.total


def fresh_window(issuer: str, now: datetime) -> IssuerWindowState:
    return IssuerWindowState(
        issuer=issuer, window_start=now, success_count=0, fail_count=0, tripped=False, tripped_at=None,
    )


def record_outcome(state: IssuerWindowState, succeeded: bool, now: datetime) -> IssuerWindowState:
    """
    Roll the window forward if it's expired, record one outcome, and recompute trip state from
    scratch based on the (possibly just-rolled) window's counts. No separate cooldown — see the
    module docstring for why one turned out to be unnecessary once MIN_SAMPLE_SIZE is respected.
    """
    if now - state.window_start >= ROLLING_WINDOW:
        state = fresh_window(state.issuer, now)

    success_count = state.success_count + (1 if succeeded else 0)
    fail_count = state.fail_count + (0 if succeeded else 1)
    total = success_count + fail_count
    rate = success_count / total  # total >= 1 always: we just recorded this call's outcome

    now_tripped = total >= MIN_SAMPLE_SIZE and rate < DEGRADATION_THRESHOLD

    if now_tripped and not state.tripped:
        tripped_at = now          # just flipped true
    elif now_tripped:
        tripped_at = state.tripped_at  # stays true; keep the original trip timestamp
    else:
        tripped_at = None

    return replace(
        state, success_count=success_count, fail_count=fail_count,
        tripped=now_tripped, tripped_at=tripped_at,
    )


def reset_eta(state: IssuerWindowState) -> datetime | None:
    """
    Best-effort estimate of when this issuer will next get a clean re-evaluation: no later than
    the current window rolling over, since a fresh window is the only way MIN_SAMPLE_SIZE resets
    to zero. It could clear sooner if enough *successful* outcomes land within the current window
    first — this is a ceiling, not a promise.
    """
    if not state.tripped:
        return None
    return state.window_start + ROLLING_WINDOW
