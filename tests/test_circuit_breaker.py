"""Tests for app/circuit_breaker.py — the rolling-window issuer trip/reset logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.circuit_breaker import (
    DEGRADATION_THRESHOLD,
    MIN_SAMPLE_SIZE,
    ROLLING_WINDOW,
    fresh_window,
    record_outcome,
    reset_eta,
)

T0 = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def test_fresh_window_is_not_tripped():
    w = fresh_window("HDFC", T0)
    assert not w.tripped
    assert w.total == 0
    assert w.success_rate is None


def test_below_min_sample_size_never_trips_even_at_zero_success():
    w = fresh_window("HDFC", T0)
    for i in range(MIN_SAMPLE_SIZE - 1):
        w = record_outcome(w, succeeded=False, now=T0 + timedelta(seconds=i))
    assert not w.tripped


def test_trips_once_sample_size_reached_and_rate_below_threshold():
    w = fresh_window("HDFC", T0)
    for i in range(MIN_SAMPLE_SIZE):
        w = record_outcome(w, succeeded=False, now=T0 + timedelta(seconds=i))
    assert w.tripped
    assert w.tripped_at == T0 + timedelta(seconds=MIN_SAMPLE_SIZE - 1)


def test_does_not_trip_when_rate_at_or_above_threshold():
    w = fresh_window("HDFC", T0)
    for i in range(10):
        w = record_outcome(w, succeeded=(i % 5 != 0), now=T0 + timedelta(seconds=i))  # 8/10 = 0.8
    assert not w.tripped
    assert w.success_rate >= DEGRADATION_THRESHOLD


def test_reset_eta_none_when_not_tripped():
    w = fresh_window("HDFC", T0)
    assert reset_eta(w) is None


def test_reset_eta_is_window_start_plus_rolling_window():
    w = fresh_window("HDFC", T0)
    for i in range(MIN_SAMPLE_SIZE):
        w = record_outcome(w, succeeded=False, now=T0 + timedelta(seconds=i))
    assert w.tripped
    assert reset_eta(w) == w.window_start + ROLLING_WINDOW


def test_recovers_within_the_same_window_once_enough_successes_land():
    """No separate cooldown (see module docstring): trip state is recomputed live from the
    current window's counts every call, so enough good outcomes can clear it before the window
    even rolls over."""
    w = fresh_window("HDFC", T0)
    for i in range(MIN_SAMPLE_SIZE):
        w = record_outcome(w, succeeded=False, now=T0 + timedelta(seconds=i))
    assert w.tripped

    # Flood it with successes, still well within the 30-minute window.
    for i in range(20):
        w = record_outcome(w, succeeded=True, now=T0 + timedelta(seconds=MIN_SAMPLE_SIZE + i))
    assert not w.tripped
    assert w.success_rate >= DEGRADATION_THRESHOLD


def test_a_single_success_does_not_immediately_clear_a_trip():
    """One good outcome right after tripping shouldn't flip it back — the aggregate rate over the
    window still has to clear the threshold, not just the most recent sample."""
    w = fresh_window("HDFC", T0)
    for i in range(MIN_SAMPLE_SIZE):
        w = record_outcome(w, succeeded=False, now=T0 + timedelta(seconds=i))
    assert w.tripped

    w = record_outcome(w, succeeded=True, now=T0 + timedelta(seconds=MIN_SAMPLE_SIZE))
    # 1 success out of 6 total = ~0.17, still below DEGRADATION_THRESHOLD (0.20).
    assert w.tripped


def test_window_rolls_over_and_resets_counts():
    w = fresh_window("HDFC", T0)
    w = record_outcome(w, succeeded=True, now=T0 + timedelta(minutes=5))
    assert w.total == 1

    later = T0 + ROLLING_WINDOW + timedelta(minutes=1)
    w = record_outcome(w, succeeded=True, now=later)
    assert w.total == 1
    assert w.window_start == later


def test_a_trip_does_not_survive_a_window_roll_without_fresh_evidence():
    """A window roll always starts a clean slate — a previously-tripped issuer needs
    MIN_SAMPLE_SIZE fresh samples in the new window before it can trip again, even if the new
    window's first sample is itself a failure."""
    w = fresh_window("HDFC", T0)
    for i in range(MIN_SAMPLE_SIZE):
        w = record_outcome(w, succeeded=False, now=T0 + timedelta(seconds=i))
    assert w.tripped

    after_roll = T0 + ROLLING_WINDOW + timedelta(seconds=1)
    w = record_outcome(w, succeeded=False, now=after_roll)
    assert not w.tripped  # only 1 sample in the fresh window; below MIN_SAMPLE_SIZE

    # ...but it can re-trip once fresh evidence accumulates again.
    for i in range(1, MIN_SAMPLE_SIZE):
        w = record_outcome(w, succeeded=False, now=after_roll + timedelta(seconds=i))
    assert w.tripped
