"""
The three §11 compliance rules, and how they interact with the six that were
already here.

Deliberately NOT more branch coverage. `tests/test_policy.py` already covers the
original pipeline at 100% branches and the build spec is explicit that adding
more of the same is not where the value is. These are interaction tests: what
happens when a new rule and an old one both have something to say about the same
proposal, and which one should win.

The rules under test are the ones nobody chose — Visa's, Mastercard's, the RBI's.
That makes their failure mode different from the rest of the file: a bug here is
a compliance incident rather than a suboptimal decision, and the two scoping
tests below guard the direction that would be quiet and expensive.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import FailureClass, InterventionAction
from app.policy import BLAST_RADIUS_RANK, L1Proposal, evaluate
from app.rule_basis import BACKSTOP, REGULATORY, basis_of, citation_of
from app.stopping_rules import (
    EMANDATE_NOTICE_BUFFER,
    MC_AUTH_CAP_PER_24H,
    VISA_REATTEMPT_CAP_PER_30D,
    PolicyContext,
    RuleName,
)

A = InterventionAction
FC = FailureClass

# 12:00 IST — outside quiet hours, so contact rules do not fire incidentally in
# tests about something else.
NOW = datetime(2026, 9, 1, 6, 30, tzinfo=timezone.utc)


def ctx(**overrides) -> PolicyContext:
    defaults = dict(
        now_utc=NOW,
        failure_class=FC.SOFT_FUNDS,
        attempts_so_far=0,
        last_attempt_at_utc=None,
        fast_retries_used=0,
        customer_contacts_in_window=0,
        issuer_breaker_tripped=False,
        issuer_breaker_reset_eta_utc=None,
    )
    defaults.update(overrides)
    return PolicyContext(**defaults)


def propose(action, fc=FC.SOFT_FUNDS, scheduled_for=None) -> L1Proposal:
    return L1Proposal(fc, action, scheduled_for, "test")


# ---------------------------------------------------------------------------
# The new rules do not disturb the old ones
# ---------------------------------------------------------------------------


def test_a_clean_proposal_still_passes_through_untouched():
    """Nine rules in the pipeline and none of them should have an opinion about
    an ordinary first retry on a soft decline."""
    d = evaluate(propose(A.RETRY_NOW), ctx())
    assert d.permitted_action is A.RETRY_NOW
    assert d.rules_fired == ()
    assert not d.vetoed and not d.downgraded


def test_new_context_fields_default_to_the_permissive_direction():
    """
    The fields were appended with defaults so the existing 149 tests keep
    constructing PolicyContext without them. That is only safe if every default
    means "this constraint does not apply" — a default that silently blocked
    would change the behaviour of every existing caller.
    """
    c = ctx()
    assert c.mastercard_advice_code is None
    assert c.card_reattempts_in_30d == 0 and c.auth_attempts_in_24h == 0
    assert c.is_recurring is False
    assert evaluate(propose(A.RETRY_NOW), c).rules_fired == ()


# ---------------------------------------------------------------------------
# Mastercard advice codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["03", "21"])
def test_never_retry_advice_codes_escalate(code):
    d = evaluate(propose(A.RETRY_NOW), ctx(mastercard_advice_code=code))
    assert d.permitted_action is A.ESCALATE_HUMAN
    assert RuleName.MC_NEVER_RETRY_ADVICE_CODE.value in d.rules_fired


@pytest.mark.parametrize("code", [None, "24", "26", "30"])
def test_other_advice_codes_are_left_alone(code):
    d = evaluate(propose(A.RETRY_NOW), ctx(mastercard_advice_code=code))
    assert d.permitted_action is A.RETRY_NOW


def test_the_advice_code_rule_catches_a_misclassified_fraud_decline():
    """
    The reason this rule exists separately from HARD_DECLINE_NO_RETRY.

    L1 read a fraud decline as SOFT_FUNDS, so the class-based rule has nothing to
    say — the proposal looks like an ordinary retry on a soft failure. The
    network's own label still has to stop it, or the backstop only works in the
    cases where our classification was already correct, which are precisely the
    cases that did not need a backstop.
    """
    c = ctx(failure_class=FC.SOFT_FUNDS, mastercard_advice_code="03")
    d = evaluate(propose(A.RETRY_NOW, fc=FC.SOFT_FUNDS), c)
    assert RuleName.HARD_DECLINE_NO_RETRY.value not in d.rules_fired
    assert RuleName.MC_NEVER_RETRY_ADVICE_CODE.value in d.rules_fired
    assert d.permitted_action is A.ESCALATE_HUMAN


def test_advice_codes_do_not_block_contacting_the_customer_about_a_soft_failure():
    """The rule is about reattempts. It has no view on a payment link."""
    d = evaluate(propose(A.REQUEST_INSTRUMENT_UPDATE), ctx(mastercard_advice_code="03"))
    assert d.permitted_action is A.REQUEST_INSTRUMENT_UPDATE


# ---------------------------------------------------------------------------
# Network reattempt caps
# ---------------------------------------------------------------------------


def test_visa_cap_binds_across_invoices():
    assert evaluate(
        propose(A.RETRY_NOW), ctx(card_reattempts_in_30d=VISA_REATTEMPT_CAP_PER_30D)
    ).permitted_action is A.ESCALATE_HUMAN
    assert evaluate(
        propose(A.RETRY_NOW), ctx(card_reattempts_in_30d=VISA_REATTEMPT_CAP_PER_30D - 1)
    ).permitted_action is A.RETRY_NOW


def test_mastercard_24h_cap():
    assert evaluate(
        propose(A.RETRY_NOW), ctx(auth_attempts_in_24h=MC_AUTH_CAP_PER_24H)
    ).permitted_action is A.ESCALATE_HUMAN


def test_network_caps_are_not_redundant_with_the_per_invoice_cap():
    """
    A first attempt on a brand-new invoice — MAX_LIFETIME_ATTEMPTS has nothing to
    say, because it counts attempts on this invoice and there have been none. The
    network counts attempts on the CARD, and a customer with several failing
    subscriptions exhausts that budget while every individual invoice still looks
    untouched.
    """
    c = ctx(attempts_so_far=0, card_reattempts_in_30d=VISA_REATTEMPT_CAP_PER_30D + 3)
    d = evaluate(propose(A.RETRY_NOW), c)
    assert RuleName.MAX_LIFETIME_ATTEMPTS.value not in d.rules_fired
    assert RuleName.NETWORK_REATTEMPT_CAP.value in d.rules_fired


# ---------------------------------------------------------------------------
# RBI e-mandate — including the two scoping tests that matter most
# ---------------------------------------------------------------------------


def test_a_mandate_debit_with_no_notice_is_deferred_by_the_full_buffer():
    d = evaluate(propose(A.RETRY_NOW), ctx(is_recurring=True))
    assert d.permitted_action is A.RETRY_SCHEDULED
    assert d.permitted_scheduled_for == NOW + EMANDATE_NOTICE_BUFFER
    assert RuleName.EMANDATE_PREDEBIT_NOTICE.value in d.rules_fired


def test_a_notice_that_is_old_enough_lets_the_debit_through():
    c = ctx(is_recurring=True, predebit_notice_sent_at_utc=NOW - timedelta(hours=27))
    assert evaluate(propose(A.RETRY_NOW), c).permitted_action is A.RETRY_NOW


def test_a_notice_that_is_too_recent_defers_to_the_moment_it_matures():
    sent = NOW - timedelta(hours=10)
    c = ctx(is_recurring=True, predebit_notice_sent_at_utc=sent)
    d = evaluate(propose(A.RETRY_NOW), c)
    assert d.permitted_action is A.RETRY_SCHEDULED
    assert d.permitted_scheduled_for == sent + EMANDATE_NOTICE_BUFFER


def test_the_rbi_notice_does_not_bind_a_one_time_payment():
    """
    Scope check 1. The obligation binds the e-mandate auto-debit rail, not every
    payment that happens to be a retry.
    """
    d = evaluate(propose(A.RETRY_NOW), ctx(is_recurring=False))
    assert RuleName.EMANDATE_PREDEBIT_NOTICE.value not in d.rules_fired
    assert d.permitted_action is A.RETRY_NOW


def test_the_rbi_notice_does_not_block_a_payment_link():
    """
    Scope check 2, and the one most likely to be got wrong in the conservative
    direction — where it would look entirely reasonable in review and quietly
    suppress the correct action on every recurring invoice.

    A customer-initiated link payment is not a mandate debit. The RBI notice has
    no bearing on it.
    """
    c = ctx(is_recurring=True, predebit_notice_sent_at_utc=None)
    d = evaluate(propose(A.REQUEST_INSTRUMENT_UPDATE), c)
    assert RuleName.EMANDATE_PREDEBIT_NOTICE.value not in d.rules_fired
    assert d.permitted_action is A.REQUEST_INSTRUMENT_UPDATE


# ---------------------------------------------------------------------------
# Interactions between new and old
# ---------------------------------------------------------------------------


def test_the_rbi_window_and_the_attempt_interval_compose_to_the_later_of_the_two():
    """
    The pipeline accumulates, so the interval rule sees the schedule the mandate
    rule already set. A debit pushed to the notice window must still respect the
    24-hour spacing, and vice versa — taking whichever is later. Scheduling
    against the earlier of the two would produce an illegal debit.
    """
    sent = NOW - timedelta(hours=20)          # notice matures at NOW + 6h
    last = NOW - timedelta(hours=2)           # interval matures at NOW + 22h
    c = ctx(is_recurring=True, predebit_notice_sent_at_utc=sent, last_attempt_at_utc=last,
            failure_class=FC.SOFT_FUNDS)
    d = evaluate(propose(A.RETRY_NOW), c)
    assert d.permitted_action is A.RETRY_SCHEDULED
    assert d.permitted_scheduled_for == last + timedelta(hours=24)
    assert RuleName.EMANDATE_PREDEBIT_NOTICE.value in d.rules_fired
    assert RuleName.MIN_ATTEMPT_INTERVAL.value in d.rules_fired


def test_a_fraud_advice_code_beats_the_mandate_deferral():
    """
    Both rules have something to say about a recurring retry with MAC 03. The
    advice code redirects to a human; the mandate rule would merely reschedule.
    Deferring a debit the network has already told us never to attempt would be
    the wrong resolution, and the pipeline order is what prevents it.
    """
    c = ctx(is_recurring=True, mastercard_advice_code="03", predebit_notice_sent_at_utc=None)
    d = evaluate(propose(A.RETRY_NOW), c)
    assert d.permitted_action is A.ESCALATE_HUMAN


def test_hard_risk_still_never_reaches_the_customer_with_the_new_rules_in_place():
    """
    Regression guard on the pre-existing guarantee. Three rules were inserted
    into the pipeline; none of them may open a path from a risk decline to a
    customer message.
    """
    for action in A:
        d = evaluate(propose(action, fc=FC.HARD_RISK), ctx(failure_class=FC.HARD_RISK))
        assert d.permitted_action not in (A.NUDGE, A.REQUEST_INSTRUMENT_UPDATE), (
            f"{action.value} on HARD_RISK reached the customer as {d.permitted_action.value}"
        )


def test_the_valve_still_holds_across_every_new_rule():
    """
    The blast-radius invariant, re-checked over contexts that exercise the added
    rules. `evaluate()` raises PolicyViolation on a breach, so reaching the
    assertion at all is most of the test.
    """
    contexts = [
        ctx(mastercard_advice_code="03"),
        ctx(card_reattempts_in_30d=99, auth_attempts_in_24h=99),
        ctx(is_recurring=True),
        ctx(is_recurring=True, predebit_notice_sent_at_utc=NOW - timedelta(hours=1)),
        ctx(failure_class=FC.HARD_RISK, mastercard_advice_code="21", is_recurring=True,
            card_reattempts_in_30d=99, customer_contacts_in_window=99, attempts_so_far=99,
            issuer_breaker_tripped=True),
    ]
    for c in contexts:
        for action in A:
            d = evaluate(propose(action, fc=c.failure_class), c)
            assert BLAST_RADIUS_RANK[d.permitted_action] <= BLAST_RADIUS_RANK[action]


# ---------------------------------------------------------------------------
# Basis classification
# ---------------------------------------------------------------------------


def test_every_rule_declares_a_basis():
    for name in RuleName:
        assert basis_of(name.value) in (REGULATORY, BACKSTOP)
        assert citation_of(name.value)


def test_the_new_rules_are_all_regulatory():
    """None of the three is ours. That is the point of adding them."""
    for name in (
        RuleName.MC_NEVER_RETRY_ADVICE_CODE,
        RuleName.NETWORK_REATTEMPT_CAP,
        RuleName.EMANDATE_PREDEBIT_NOTICE,
    ):
        assert basis_of(name.value) == REGULATORY


def test_the_caps_are_ours_and_say_so():
    """
    The reframe, asserted. Attempt and contact caps used to be presented as the
    policy; they are now backstops against scorer error, and the metric has to be
    able to tell that apart from a compliance veto.
    """
    for name in (
        RuleName.MAX_LIFETIME_ATTEMPTS,
        RuleName.CONTACT_FREQUENCY_CAP,
        RuleName.MIN_ATTEMPT_INTERVAL,
        RuleName.ISSUER_CIRCUIT_BREAKER,
    ):
        assert basis_of(name.value) == BACKSTOP
