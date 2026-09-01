"""
L3 - the executor. The only file in the repo that makes an outbound Razorpay API
call - same module-boundary discipline as classifier.py being the only file that
touches an LLM (see docs/architecture.md).

Only two action families ever reach this module. `NUDGE`, `ESCALATE_HUMAN` and
`STOP_PERMANENT` move no money and ask Razorpay for nothing, so nothing upstream
should ever call execute() for them - EXECUTABLE_ACTIONS makes that a checked
boundary rather than a convention:

    RETRY_NOW / RETRY_SCHEDULED       -> an Order, the first step of a charge
    REQUEST_INSTRUMENT_UPDATE          -> a Payment Link asking for a new instrument

Idempotency, and where the key actually comes from
----------------------------------------------------------------------------
The core Payments/Orders API has no dedicated idempotency header - that exists
only for RazorpayX (`X-Payout-Idempotency`, `X-Transfer-Idempotency`,
`X-Refund-Idempotency`), a different product from the one this system calls.

Orders and Payment Links both take a merchant-reference field - `receipt` and
`reference_id` respectively - but they behave DIFFERENTLY, and this was checked
against live test mode rather than assumed from documentation, because the
documentation is not consistent with what the API actually does:

  - `payment_link.create` rejects a reused `reference_id` outright
    (BadRequestError: "payment link with given reference_id: ... already
    exists"). Confirmed live, 1 September 2026 - see the corresponding test in
    test_executor.py.
  - `order.create` does NOT. Two live calls with an identical `receipt` on
    1 September 2026 both succeeded and returned two DIFFERENT order ids. Some
    third-party documentation summaries claim `receipt` is treated as an
    idempotency key; that claim did not survive contact with the live API and
    is not relied on here. `receipt` is a free-text merchant reference only.

So for `REQUEST_INSTRUMENT_UPDATE` (Payment Links), Razorpay itself is a real
backstop against a duplicate call. For `RETRY_NOW`/`RETRY_SCHEDULED` (Orders),
it is not - Razorpay will happily create two orders for the same retry if
asked twice. The `existing` argument below is therefore not an optimisation
for the retry path, it is the ONLY guard: the caller must pass in whatever
ExecutionResult already exists for this (invoice_id, attempt_no) - typically
read off the Attempt row, whose own idempotency key
(`Attempt.__table_args__` in app/models.py) is exactly this pair - and
execute() returns it unchanged (`replayed=True`) without calling out at all.

A caveat worth knowing before trusting a caught error's detail: the razorpay
SDK's BadRequestError carries only the response's `description` string
(razorpay/client.py discards `code`/`source`/`step`/`reason` when raising), so
the duplicate-receipt path below cannot recover the original order's id from
the error alone. That does not affect a genuinely failed PAYMENT, though -
fetching a payment by id returns error_code/error_description/error_source/
error_step/error_reason as ordinary fields on a 200 response, not as a raised
exception, which is why ExecutionResult.error mirrors those five field names
directly rather than inventing a translated shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Protocol

from app.models import AttemptOutcome, InterventionAction

A = InterventionAction

RETRY_ACTIONS = frozenset({A.RETRY_NOW, A.RETRY_SCHEDULED})
CONTACT_ACTIONS = frozenset({A.REQUEST_INSTRUMENT_UPDATE})
EXECUTABLE_ACTIONS = RETRY_ACTIONS | CONTACT_ACTIONS

# Substring of Razorpay's actual live error message for a reused reference_id:
# "payment link with given reference_id: ... already exists. Please create a
# payment link with a different reference_id" (confirmed live, 1 September
# 2026). Any OTHER BadRequestError - a rate limit, a test-mode quota, a bad
# amount - must not be caught by the same branch and relabelled as a safe
# replay; see the except clause below.
_DUPLICATE_REFERENCE_MARKER = "already exists"


def idempotency_key(invoice_id: str, attempt_no: int) -> str:
    """<=40 chars, Razorpay's own cap on receipt/reference_id. A 36-char UUID
    invoice_id already exceeds that budget with any prefix attached, so the
    invoice id is truncated FIRST and the attempt_no suffix is appended after -
    truncating the whole formatted string instead (as an earlier version of this
    function did) silently drops the attempt_no and collides every attempt on
    an invoice onto the same key. See test_idempotency_key_differs_by_attempt_no."""
    prefix, suffix = "bkstp-", f"-{attempt_no}"
    budget = 40 - len(prefix) - len(suffix)
    return f"{prefix}{invoice_id[:budget]}{suffix}"


def _check_action(action: InterventionAction) -> None:
    if action not in EXECUTABLE_ACTIONS:
        names = ", ".join(a.value for a in EXECUTABLE_ACTIONS)
        raise ValueError(
            f"{action.value} never reaches L3 - only {{{names}}} call out to "
            "Razorpay. NUDGE/ESCALATE_HUMAN/STOP_PERMANENT move no money and no "
            "execution should be attempted for them."
        )


@dataclass(frozen=True)
class ExecutionResult:
    """
    L3's structured output, written onto the Attempt row by the caller.

    `error`, when present, uses Razorpay's own Payment-entity field names
    (error_code/error_description/error_source/error_step/error_reason) so a
    captured real failure and FakeExecutor's synthetic one are diffable
    field-for-field - see the module docstring's caveat about what the SDK's
    raised exception does and doesn't carry.
    """

    outcome: AttemptOutcome
    razorpay_order_id: str | None
    razorpay_payment_id: str | None
    error: dict | None
    executed_at: datetime
    replayed: bool = False  # True iff no outbound call was made because this
    # (invoice_id, attempt_no) already had a result - see module docstring.


class Executor(Protocol):
    def execute(
        self,
        invoice_id: str,
        attempt_no: int,
        action: InterventionAction,
        amount_paise: int,
        existing: ExecutionResult | None = None,
    ) -> ExecutionResult: ...


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


@dataclass
class FakeExecutor:
    """
    Deterministic, in-memory, no network - what every test and every other
    caller in this repo should use unless it is specifically testing the live
    Razorpay path.

    NOT used by sim/run_arms.py. The simulator resolves outcomes
    probabilistically through sim/world_model.py and never calls L3 at all, so
    none of the three/four-arm comparison depends on this class - it exists to
    exercise the L3 CONTRACT (the idempotency replay, the action-family
    boundary), not to simulate outcomes for the arms.
    """

    should_succeed: Callable[[str, int], bool] = lambda invoice_id, attempt_no: True
    calls: list[tuple[str, int]] = field(default_factory=list)

    def execute(
        self,
        invoice_id: str,
        attempt_no: int,
        action: InterventionAction,
        amount_paise: int,
        existing: ExecutionResult | None = None,
    ) -> ExecutionResult:
        _check_action(action)
        if existing is not None:
            return replace(existing, replayed=True)

        self.calls.append((invoice_id, attempt_no))
        now = datetime.now(timezone.utc)
        key = idempotency_key(invoice_id, attempt_no)

        if self.should_succeed(invoice_id, attempt_no):
            return ExecutionResult(
                outcome=AttemptOutcome.SUCCEEDED,
                razorpay_order_id=f"order_fake_{key}",
                razorpay_payment_id=f"pay_fake_{key}",
                error=None,
                executed_at=now,
            )
        return ExecutionResult(
            outcome=AttemptOutcome.FAILED,
            razorpay_order_id=f"order_fake_{key}",
            razorpay_payment_id=None,
            error={
                "error_code": "BAD_REQUEST_ERROR",
                "error_description": "fake failure injected by FakeExecutor",
                "error_source": "customer",
                "error_step": "payment_authorization",
                "error_reason": "fake_failure",
            },
            executed_at=now,
        )


# ---------------------------------------------------------------------------
# The real L3
# ---------------------------------------------------------------------------


@dataclass
class RazorpayExecutor:
    """The production path. See module docstring for the idempotency design."""

    name: str = "razorpay"

    def _client(self):
        try:
            import razorpay
        except ImportError as e:  # pragma: no cover - depends on install
            raise RuntimeError("pip install razorpay to run the L3 live path") from e
        key_id = os.environ.get("RAZORPAY_KEY_ID")
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise RuntimeError(
                "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. L3 cannot run "
                "live; FakeExecutor does not need them. Never commit these - .env "
                "is gitignored."
            )
        return razorpay.Client(auth=(key_id, key_secret))

    def execute(
        self,
        invoice_id: str,
        attempt_no: int,
        action: InterventionAction,
        amount_paise: int,
        existing: ExecutionResult | None = None,
    ) -> ExecutionResult:
        _check_action(action)
        if existing is not None:
            return replace(existing, replayed=True)

        import razorpay

        client = self._client()
        key = idempotency_key(invoice_id, attempt_no)
        now = datetime.now(timezone.utc)
        notes = {"invoice_id": invoice_id, "attempt_no": str(attempt_no)}

        if action in RETRY_ACTIONS:
            # No try/except here: order.create does not reject a reused
            # `receipt` (see module docstring, verified live) - there is no
            # Razorpay-side rejection to catch. `existing` above is the only
            # thing standing between a retried call and a duplicate order.
            order = client.order.create(
                {
                    "amount": amount_paise,
                    "currency": "INR",
                    "receipt": key,
                    "payment_capture": 1,
                    "notes": notes,
                }
            )
            return ExecutionResult(
                outcome=AttemptOutcome.PENDING,  # an Order exists; no charge has
                # been attempted against it yet - that requires a checkout, which
                # is what makes this a genuine retry rather than a fabricated one.
                razorpay_order_id=order["id"],
                razorpay_payment_id=None,
                error=None,
                executed_at=now,
            )

        # REQUEST_INSTRUMENT_UPDATE -> a real payment link, which is the actual
        # production behaviour for this action, not a stand-in for it. Unlike
        # order.create above, payment_link.create DOES reject a reused
        # reference_id (verified live - see module docstring), so this except
        # is a real second layer, not dead code.
        try:
            link = client.payment_link.create(
                {
                    "amount": amount_paise,
                    "currency": "INR",
                    "reference_id": key,
                    "description": f"Update payment method for invoice {invoice_id}",
                    "notes": notes,
                }
            )
        except razorpay.errors.BadRequestError as e:
            if _DUPLICATE_REFERENCE_MARKER not in str(e):
                # Not the idempotency case - a real failure (rate limit, quota,
                # bad amount, whatever). Re-raise rather than mislabelling it
                # `replayed=True`: a caller reading that flag is trusting that
                # nothing new happened and the earlier attempt's result still
                # stands, which is only true for the one specific rejection
                # this branch exists to catch. A live run on 1 September 2026
                # hit exactly this - a test-mode payment_link quota error
                # (`ServerError`, not even this exception type) surfaced
                # alongside a rate-limited `BadRequestError`, and the original
                # version of this method silently called both "replayed".
                raise
            return self._duplicate_result(e, now)
        return ExecutionResult(
            outcome=AttemptOutcome.PENDING,
            razorpay_order_id=link["id"],
            razorpay_payment_id=None,
            error=None,
            executed_at=now,
        )

    @staticmethod
    def _duplicate_result(e: Exception, now: datetime) -> ExecutionResult:
        # Payment Links' reference_id rejection fired - see module docstring.
        # This is the network-level-retry backstop for THIS action only
        # (order.create has no equivalent); the normal path is the caller's
        # `existing` check never reaching here at all.
        return ExecutionResult(
            outcome=AttemptOutcome.PENDING,
            razorpay_order_id=None,
            razorpay_payment_id=None,
            error={"error_code": "BAD_REQUEST_ERROR", "error_description": str(e)},
            executed_at=now,
            replayed=True,
        )
