"""
L3 contract tests.

FakeExecutor tests run always and cover what the contract actually promises:
the action-family boundary and the idempotency replay. RazorpayExecutor tests
are skipped unless RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET are set (see
tests/conftest.py) and make a small number of real test-mode calls - see
app/executor.py's module docstring for why idempotency has to be proven live
rather than assumed from the fake.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace

import pytest

from app.executor import (
    EXECUTABLE_ACTIONS,
    ExecutionResult,
    FakeExecutor,
    RazorpayExecutor,
    idempotency_key,
)
from app.models import AttemptOutcome, InterventionAction

A = InterventionAction

HAS_LIVE_KEYS = bool(os.environ.get("RAZORPAY_KEY_ID")) and bool(
    os.environ.get("RAZORPAY_KEY_SECRET")
)
live = pytest.mark.skipif(
    not HAS_LIVE_KEYS,
    reason="RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set - see .env in the repo root",
)


def invoice_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# idempotency_key
# ---------------------------------------------------------------------------


def test_idempotency_key_is_deterministic():
    inv = invoice_id()
    assert idempotency_key(inv, 1) == idempotency_key(inv, 1)


def test_idempotency_key_differs_by_attempt_no():
    inv = invoice_id()
    assert idempotency_key(inv, 1) != idempotency_key(inv, 2)


def test_idempotency_key_differs_by_invoice():
    assert idempotency_key(invoice_id(), 1) != idempotency_key(invoice_id(), 1)


def test_idempotency_key_never_exceeds_razorpays_40_char_cap():
    assert len(idempotency_key(invoice_id(), 4)) <= 40


# ---------------------------------------------------------------------------
# The action-family boundary - EXECUTABLE_ACTIONS is the checked half of the
# one-way valve's promise that L3 never invents a reason to touch Razorpay.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(EXECUTABLE_ACTIONS, key=lambda a: a.value))
def test_executable_actions_reach_the_fake(action):
    ex = FakeExecutor()
    result = ex.execute(invoice_id(), 1, action, 200_000)
    assert result.outcome in (AttemptOutcome.SUCCEEDED, AttemptOutcome.FAILED)


@pytest.mark.parametrize(
    "action", [A.NUDGE, A.ESCALATE_HUMAN, A.STOP_PERMANENT]
)
def test_non_executable_actions_are_rejected_not_silently_skipped(action):
    ex = FakeExecutor()
    with pytest.raises(ValueError):
        ex.execute(invoice_id(), 1, action, 200_000)


# ---------------------------------------------------------------------------
# Idempotency replay - the PRIMARY guard, exercised against the fake.
# ---------------------------------------------------------------------------


def test_existing_result_short_circuits_without_a_new_call():
    ex = FakeExecutor()
    inv = invoice_id()
    first = ex.execute(inv, 1, A.RETRY_NOW, 200_000)
    assert len(ex.calls) == 1

    replay = ex.execute(inv, 1, A.RETRY_NOW, 200_000, existing=first)
    assert len(ex.calls) == 1  # no second call recorded
    assert replay.replayed is True
    assert replay.razorpay_order_id == first.razorpay_order_id
    assert replay.outcome == first.outcome


def test_replay_preserves_a_failed_result_rather_than_retrying_it():
    """Idempotency means returning what already happened, not a fresh attempt -
    replaying a FAILED result must not quietly become a second try."""
    ex = FakeExecutor(should_succeed=lambda *_: False)
    inv = invoice_id()
    first = ex.execute(inv, 1, A.REQUEST_INSTRUMENT_UPDATE, 200_000)
    assert first.outcome is AttemptOutcome.FAILED

    replay = ex.execute(inv, 1, A.REQUEST_INSTRUMENT_UPDATE, 200_000, existing=first)
    assert replay.outcome is AttemptOutcome.FAILED
    assert replay.error == first.error
    assert len(ex.calls) == 1


def test_two_different_attempts_on_the_same_invoice_both_execute():
    """Idempotency is keyed on (invoice_id, attempt_no), not invoice_id alone -
    a second, distinct attempt must not be mistaken for a replay of the first."""
    ex = FakeExecutor()
    inv = invoice_id()
    ex.execute(inv, 1, A.RETRY_NOW, 200_000)
    ex.execute(inv, 2, A.RETRY_NOW, 200_000)
    assert ex.calls == [(inv, 1), (inv, 2)]


def test_fake_failure_carries_razorpays_real_field_names():
    """error uses Razorpay's own Payment-entity field names even in the fake, so
    a test written against FakeExecutor's shape does not need rewriting once a
    real captured failure replaces it."""
    ex = FakeExecutor(should_succeed=lambda *_: False)
    result = ex.execute(invoice_id(), 1, A.RETRY_NOW, 200_000)
    assert set(result.error) == {
        "error_code",
        "error_description",
        "error_source",
        "error_step",
        "error_reason",
    }


# ---------------------------------------------------------------------------
# Live: RazorpayExecutor. Skipped without credentials. Three calls, proving
# idempotency for real rather than assuming the fake's behaviour generalises.
# ---------------------------------------------------------------------------


@live
def test_live_order_create_returns_a_real_order_id():
    ex = RazorpayExecutor()
    result = ex.execute(invoice_id(), 1, A.RETRY_NOW, 50_000)
    assert result.outcome is AttemptOutcome.PENDING
    assert result.razorpay_order_id
    assert result.razorpay_order_id.startswith("order_")
    assert result.error is None


@live
def test_live_duplicate_order_receipt_is_not_deduped_by_razorpay():
    """The asymmetric finding this module's docstring documents: order.create
    does NOT reject a reused receipt, unlike payment_link.create below. Calling
    execute() twice for the same (invoice_id, attempt_no) with no `existing`
    passed creates two DISTINCT orders - proving that for retries, the
    caller's `existing` check is the only idempotency guard that exists."""
    ex = RazorpayExecutor()
    inv = invoice_id()
    first = ex.execute(inv, 1, A.RETRY_NOW, 50_000)
    assert first.error is None

    second = ex.execute(inv, 1, A.RETRY_NOW, 50_000)  # no `existing` passed
    assert second.error is None
    assert second.replayed is False
    assert second.razorpay_order_id != first.razorpay_order_id


@live
def test_live_duplicate_payment_link_reference_id_is_rejected_by_razorpay():
    """Unlike order.create, payment_link.create DOES reject a reused
    reference_id - the real backstop for REQUEST_INSTRUMENT_UPDATE that
    RETRY_NOW/RETRY_SCHEDULED do not get. See app/executor.py's module
    docstring."""
    ex = RazorpayExecutor()
    inv = invoice_id()
    first = ex.execute(inv, 1, A.REQUEST_INSTRUMENT_UPDATE, 50_000)
    assert first.error is None

    second = ex.execute(inv, 1, A.REQUEST_INSTRUMENT_UPDATE, 50_000)
    assert second.replayed is True
    assert second.error is not None
    assert second.error["error_code"] == "BAD_REQUEST_ERROR"


@live
def test_live_payment_link_create_returns_a_real_link_id():
    ex = RazorpayExecutor()
    result = ex.execute(invoice_id(), 1, A.REQUEST_INSTRUMENT_UPDATE, 50_000)
    assert result.outcome is AttemptOutcome.PENDING
    assert result.razorpay_order_id
    assert result.razorpay_order_id.startswith("plink_")
    assert result.error is None
