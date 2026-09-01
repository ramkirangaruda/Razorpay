"""
L2b tests.

The interesting ones here are not the arithmetic — that is a four-term formula
and it either adds up or it does not. They are the three claims the submission
makes about the scorer:

  1. Restraint EMERGES. Stop is not a rule; it is what wins when nothing else
     has positive expected value. If that only happens because a cap fired, the
     central claim is false.
  2. The valve holds at this layer too. L2b may reject or downgrade what L1
     proposed and may never upgrade it.
  3. Beliefs are separable from the world. If the agent's parameters cannot be
     perturbed independently, the simulation result is circular.

There is also a test that records a finding we do NOT like — see
test_retry_ev_stays_positive_which_is_the_honest_finding.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.models import FailureClass as FC
from app.models import InterventionAction as A
from app.policy import BLAST_RADIUS_RANK
from app.scorer import (
    PRICEABLE,
    VALID_BUCKETS,
    Beliefs,
    ScoreContext,
    _marginal_decay,
    churn_hazard,
    p_recovery,
    score,
    score_action,
)
from sim.world_model_constants import REGISTRY, resolve

NOW = datetime(2026, 8, 15, 12, 0)
BELIEFS = Beliefs.from_constants()


def sctx(**kw) -> ScoreContext:
    base = dict(
        invoice_value_inr=2000.0,
        recovery_bucket="HIGH",
        failure_class=FC.SOFT_FUNDS,
        attempt_no=1,
        contacts_so_far=0,
        days_since_last_contact=None,
        now=NOW,
    )
    base.update(kw)
    return ScoreContext(**base)


# ---------------------------------------------------------------------------
# L1 must never emit a float
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bucket", VALID_BUCKETS)
def test_valid_buckets_accepted(bucket):
    assert sctx(recovery_bucket=bucket).recovery_bucket == bucket


@pytest.mark.parametrize("bad", [0.34, "0.34", "high", "MEDIUM_HIGH", None, 3])
def test_a_probability_is_rejected_at_the_boundary(bad):
    """
    The guard behind "L1 must never emit a floating-point probability".
    A judge asking where 0.34 came from must not be told "the model said so",
    so the type system refuses to carry one into the EV at all.
    """
    with pytest.raises(ValueError):
        sctx(recovery_bucket=bad)


def test_buckets_are_monotonic_in_recovery_value():
    """Ordinal means ordered. A bucket scale that is not monotonic is a lookup table."""
    evs = [
        score_action(A.RETRY_NOW, sctx(recovery_bucket=b), BELIEFS).ev
        for b in VALID_BUCKETS
    ]
    assert evs == sorted(evs)


# ---------------------------------------------------------------------------
# Stop is the zero reference
# ---------------------------------------------------------------------------


def test_stop_scores_exactly_zero():
    s = score_action(A.STOP_PERMANENT, sctx(), BELIEFS)
    assert s.ev == 0.0
    assert s.churn_term == 0.0 and s.action_cost == 0.0


def test_stop_wins_when_nothing_is_worth_doing():
    """
    A small invoice, a near-hopeless bucket, and a customer already contacted
    three times. No rule fires here — L2a is not even in the call. Stop wins on
    the arithmetic alone, which is the entire thesis in one assertion.
    """
    c = sctx(
        invoice_value_inr=300.0,
        recovery_bucket="VERY_LOW",
        attempt_no=3,
        contacts_so_far=3,
        days_since_last_contact=1.0,
    )
    result = score(A.NUDGE, c, BELIEFS)
    assert result.chosen is A.STOP_PERMANENT
    assert all(s.ev <= 0 for s in result.scores if s.action is not A.STOP_PERMANENT)


def test_contact_value_turns_negative_at_the_fourth_ask():
    """
    A claim that got WEAKER when the model got more correct, recorded honestly.

    Before the lapse-avoidance term was added, the arithmetic turned a contact
    down at the second ask — comfortably before CONTACT_FREQUENCY_CAP had
    anything to say, which made a nice story about restraint emerging rather
    than being imposed.

    Adding the term that credits an action with the churn it PREVENTS made
    contacts substantially more valuable, and the crossing moved out to the
    fourth ask. The cap sits at three contacts per 30 days, so the arithmetic
    and the backstop now bind in the same place rather than the arithmetic
    binding first.

    That is a real reduction in the strength of the claim and the README says
    so. What survives is still worth stating: the value of a contact collapses
    by roughly an order of magnitude across three asks, so the cap is no longer
    an arbitrary number defended by assertion — it sits almost exactly where the
    expected value crosses zero, which is a much better reason for a cap to be
    at 3 than "3 felt right".
    """
    evs = [
        score_action(A.NUDGE, sctx(contacts_so_far=n, days_since_last_contact=2.0), BELIEFS).ev
        for n in range(0, 5)
    ]
    assert evs[0] > 0, "a first contact should be clearly worth making"
    assert evs[1] > 0
    assert evs[3] < 0, f"the fourth ask must be negative on the arithmetic, got {evs}"
    assert evs == sorted(evs, reverse=True), "contact value must decrease monotonically"

    # The cap sits where the arithmetic crosses, rather than somewhere arbitrary.
    from app.stopping_rules import CONTACT_FREQUENCY_CAP as CONTACT_CAP_PER_30D

    assert evs[CONTACT_CAP_PER_30D] < 0 <= evs[CONTACT_CAP_PER_30D - 1], (
        "CONTACT_FREQUENCY_CAP should sit at the sign change, not beside it"
    )


def test_retry_ev_stays_positive_which_is_the_honest_finding():
    """
    A finding recorded as a test because we would rather it were untrue.

    On the retry axis restraint does NOT emerge from the arithmetic at the
    declared constants. ISSUER_TRUST_COST_INR is around Rs 8.80 — a floor taken
    from a published network fee schedule — and a retry spends nothing else
    except a sub-rupee gateway fee. Neither can outweigh even a fractional
    chance at a Rs 2,000 invoice plus the lapse it would prevent, so retry EV
    decays toward zero from above and never crosses it. MAX_LIFETIME_ATTEMPTS,
    a backstop, is what actually stops the retrying.

    This is exactly what the handoff's §9.3 analysis predicted, and its
    instruction is the right one: do not fix it by guessing a bigger number.
    Report the threshold instead and let the claim be falsifiable.

    The mitigating fact, also asserted: the decay is steep enough that by the
    time the cap binds there is very little left to argue about — attempt 5 is
    worth under a seventh of attempt 1, and the curve is under 1% of the
    invoice by attempt 9.
    """
    evs = [score_action(A.RETRY_NOW, sctx(attempt_no=n), BELIEFS).ev for n in range(1, 11)]
    assert all(e > 0 for e in evs), "if this ever fails, the README claim must change"
    assert evs == sorted(evs, reverse=True), "decay must at least be monotonic"
    assert evs[4] < evs[0] / 7, "decay must be steep, even if it never crosses zero"
    assert evs[-1] / 2000.0 < 0.01, "by attempt 10 the remaining value is under 1%"


def test_the_lapse_term_is_what_makes_action_worth_taking():
    """
    Regression guard on the correction itself.

    The handoff's EV formula charges churn for contacting a customer and
    charges nothing for abandoning their invoice — it prices STOP at zero. In
    the world, failing to recover is the definition of involuntary churn and
    costs the remaining LTV, at roughly seven times the cost of a first
    contact. A scorer blind to that stops on almost everything: the first
    three-arm run had Backstop recovering 63 invoices against the naive
    baseline's 87.

    If this term is ever removed, the arms regress and the README's numbers
    become wrong, so the dependency is asserted rather than left in a comment.
    """
    s = score_action(A.RETRY_NOW, sctx(), BELIEFS)
    assert s.lapse_avoided > s.recovery_value, (
        "the lapse a recovery prevents should dominate the invoice itself — "
        "that is Redux's framing and the reason the term exists"
    )
    blind = BELIEFS.perturbed({"p_lapse_if_unrecovered": 0.0})
    assert score_action(A.RETRY_NOW, sctx(), blind).ev < s.ev


# ---------------------------------------------------------------------------
# Decay, on the axis each action actually spends
# ---------------------------------------------------------------------------


def test_retries_decay_on_attempt_index():
    a = score_action(A.RETRY_NOW, sctx(attempt_no=1), BELIEFS).p_recovery
    b = score_action(A.RETRY_NOW, sctx(attempt_no=3), BELIEFS).p_recovery
    assert b < a


def test_contacts_decay_on_contact_count_not_attempt_count():
    """
    A customer who has ignored two payment links is less likely to act on a
    third. How many silent retries the merchant ran in between is a different
    quantity and must not move this one.
    """
    silent_retries = score_action(
        A.REQUEST_INSTRUMENT_UPDATE, sctx(attempt_no=5, contacts_so_far=0), BELIEFS
    ).p_recovery
    first_ask = score_action(
        A.REQUEST_INSTRUMENT_UPDATE, sctx(attempt_no=1, contacts_so_far=0), BELIEFS
    ).p_recovery
    assert silent_retries == first_ask, "retry history must not decay a first contact"

    third_ask = score_action(
        A.REQUEST_INSTRUMENT_UPDATE, sctx(attempt_no=1, contacts_so_far=2), BELIEFS
    ).p_recovery
    assert third_ask < first_ask


def test_decay_is_extrapolated_not_clamped_past_the_table():
    """
    Regression guard on a bug that silently disabled the whole thesis. Clamping
    the marginal-recovery table at its last index gave every retry a permanent
    floor of positive EV, so the arithmetic never stopped and the attempt cap
    did all the work.
    """
    last = max(BELIEFS.marginal_recovery_by_attempt)
    beyond = [_marginal_decay(i, BELIEFS) for i in range(last, last + 5)]
    assert beyond == sorted(beyond, reverse=True)
    assert beyond[-1] < beyond[0], "decay must continue past the published table"
    assert beyond[-1] > 0


# ---------------------------------------------------------------------------
# Structural zeroes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fc", [FC.SOFT_AUTH, FC.HARD_INSTRUMENT, FC.HARD_RISK, FC.HARD_MANDATE]
)
def test_a_retry_is_worthless_where_it_has_no_mechanism(fc):
    """
    A retry cannot fix an OTP drop-off (the customer never authorised anything)
    or an expired card (the instrument is gone). These are mechanism facts, not
    estimates.
    """
    p, _ = p_recovery(A.RETRY_NOW, sctx(failure_class=fc), BELIEFS)
    assert p == 0.0


def test_structural_zeroes_survive_the_sweep():
    """
    Sweeping CHANNEL_FIT scales the table. It must not lift a structural zero
    off the floor — that would be sweeping a mechanism, not a judgment call.
    """
    for t in (0.0, 0.5, 1.0):
        b = Beliefs.from_constants({"CHANNEL_FIT": t})
        assert b.channel_fit[("RETRY", "SOFT_AUTH")] == 0.0
        assert b.channel_fit[("RETRY", "HARD_INSTRUMENT")] == 0.0


def test_hard_risk_is_worthless_on_every_channel():
    for action in PRICEABLE:
        p, _ = p_recovery(action, sctx(failure_class=FC.HARD_RISK), BELIEFS)
        assert p == 0.0


# ---------------------------------------------------------------------------
# The one-way valve at L2b
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("proposed", sorted(A, key=lambda a: a.value))
def test_scorer_never_upgrades_the_proposal(proposed):
    """
    L2b may reject or downgrade. A confident scorer must not be able to talk
    the system into contacting a customer that L1 only wanted to retry
    silently.
    """
    c = sctx()
    result = score(proposed, c, BELIEFS)
    assert BLAST_RADIUS_RANK[result.chosen] <= BLAST_RADIUS_RANK[proposed]
    for s in result.scores:
        assert BLAST_RADIUS_RANK[s.action] <= BLAST_RADIUS_RANK[proposed]


def test_a_stop_proposal_stays_stopped():
    result = score(A.STOP_PERMANENT, sctx(), BELIEFS)
    assert result.chosen is A.STOP_PERMANENT


def test_scores_are_returned_best_first_with_every_term_separated():
    """The trace renderer depends on both, and 'explainable' depends on the terms."""
    result = score(A.NUDGE, sctx(), BELIEFS)
    evs = [s.ev for s in result.scores]
    assert evs == sorted(evs, reverse=True)
    for s in result.scores:
        terms = s.as_terms()
        assert len(terms) == 5, "every EV term must be shown separately in the trace"
        assert abs(sum(v for _, v in terms) - s.ev) < 1e-9


# ---------------------------------------------------------------------------
# Churn hazard
# ---------------------------------------------------------------------------


def test_churn_hazard_grows_with_contact_count():
    h = [churn_hazard(sctx(contacts_so_far=n, days_since_last_contact=0.0), BELIEFS) for n in range(4)]
    assert h == sorted(h)
    assert h[0] == pytest.approx(BELIEFS.contact_fatigue_base)


def test_recency_decays_accumulated_fatigue_not_the_hazard():
    """
    A customer contacted three times last week is nearly exhausted. The same
    customer contacted three times last quarter is close to fresh.
    """
    recent = churn_hazard(sctx(contacts_so_far=3, days_since_last_contact=1.0), BELIEFS)
    stale = churn_hazard(sctx(contacts_so_far=3, days_since_last_contact=90.0), BELIEFS)
    assert stale < recent
    assert stale == pytest.approx(BELIEFS.contact_fatigue_base, rel=0.02)


def test_zero_fatigue_removes_the_churn_term_entirely():
    """The adversarial arm's world. It must be reachable, not merely swept near."""
    b = BELIEFS.perturbed({"contact_fatigue_base": 0.0})
    s = score_action(A.NUDGE, sctx(contacts_so_far=5, days_since_last_contact=1.0), b)
    assert s.churn_term == 0.0


def test_growth_of_one_means_no_escalation():
    """CONTACT_FATIGUE_GROWTH is swept down to 1.0, where the intuition disappears."""
    b = Beliefs.from_constants({"CONTACT_FATIGUE_GROWTH": 0.0})  # t=0 -> low end = 1.0
    assert b.contact_fatigue_growth == pytest.approx(1.0)
    flat = [
        churn_hazard(sctx(contacts_so_far=n, days_since_last_contact=0.0), b)
        for n in range(5)
    ]
    assert len(set(round(x, 12) for x in flat)) == 1


# ---------------------------------------------------------------------------
# Beliefs vs world truth
# ---------------------------------------------------------------------------


def test_perturbation_does_not_mutate_the_source_beliefs():
    """
    If perturbation aliased the original, every arm in the sweep would share one
    parameter set and the robustness result would be meaningless.
    """
    original = BELIEFS.contact_fatigue_base
    other = BELIEFS.perturbed({"contact_fatigue_base": 3.0})
    assert BELIEFS.contact_fatigue_base == original
    assert other.contact_fatigue_base == pytest.approx(original * 3.0)


def test_perturbation_handles_table_valued_beliefs():
    other = BELIEFS.perturbed({"p_recovery_by_bucket": 0.5})
    for k, v in BELIEFS.p_recovery_by_bucket.items():
        assert other.p_recovery_by_bucket[k] == pytest.approx(v * 0.5)


def test_beliefs_read_nothing_from_module_globals_at_score_time():
    """
    Every number in a score must come from the Beliefs handed in. If halving
    the beliefs does not move the answer, the scorer is reading the world's
    constants behind our back and the whole robustness arm is theatre.
    """
    poor = BELIEFS.perturbed({"p_recovery_by_bucket": 0.1})
    assert score_action(A.RETRY_NOW, sctx(), poor).ev < score_action(A.RETRY_NOW, sctx(), BELIEFS).ev


# ---------------------------------------------------------------------------
# Sweep semantics — the bug the handoff says not to reintroduce
# ---------------------------------------------------------------------------


def test_absolute_and_multiplicative_sweeps_are_not_confused():
    """
    §9.1. LTV_MULTIPLE_OF_INVOICE sweeps absolutely over 2..18; P_RECOVERY
    sweeps multiplicatively over 0.6..1.4. Sharing one field would sweep the
    LTV multiple down to 0.6 and silently corrupt the sensitivity arm.
    """
    assert resolve("LTV_MULTIPLE_OF_INVOICE", 0.0) == pytest.approx(2.0)
    assert resolve("LTV_MULTIPLE_OF_INVOICE", 1.0) == pytest.approx(18.0)

    base = REGISTRY["RETRY_TIMING_LIFT"].value
    assert resolve("RETRY_TIMING_LIFT", 0.0) == pytest.approx(1.0)
    assert resolve("RETRY_TIMING_LIFT", 1.0) == pytest.approx(1.8)
    assert base == pytest.approx(1.35)


def test_every_assumed_constant_declares_a_sweep():
    """The dataclass enforces this; the test states it as a project rule."""
    from sim.world_model_constants import Provenance, by_provenance

    for name, c in by_provenance(Provenance.ASSUMED).items():
        assert c.sweep is not None, f"{name} is ASSUMED with no sweep range"


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def test_a_mastercard_advice_code_overrides_our_own_timing_estimate():
    """The network hands us a schedule for free. Where it exists, it wins."""
    s = score_action(
        A.RETRY_SCHEDULED,
        sctx(failure_class=FC.SOFT_TRANSIENT, mastercard_advice_code="26"),
        BELIEFS,
    )
    assert "MAC 26" in s.timing_note
    assert s.p_recovery > 0


def test_timing_lift_applies_to_soft_funds_via_the_payday_window():
    s = score_action(A.RETRY_SCHEDULED, sctx(now=datetime(2026, 8, 20, 12, 0)), BELIEFS)
    assert "payday" in s.timing_note
    immediate = score_action(A.RETRY_NOW, sctx(now=datetime(2026, 8, 20, 12, 0)), BELIEFS)
    assert s.p_recovery > immediate.p_recovery


def test_no_timing_story_is_invented_for_classes_without_a_mechanism():
    """
    A gateway timeout has no external cash-flow mechanism. Deferring it buys
    nothing, and claiming otherwise would be assumption for its own sake.
    """
    s = score_action(A.RETRY_SCHEDULED, sctx(failure_class=FC.SOFT_TRANSIENT), BELIEFS)
    immediate = score_action(A.RETRY_NOW, sctx(failure_class=FC.SOFT_TRANSIENT), BELIEFS)
    assert s.p_recovery == pytest.approx(immediate.p_recovery)
    assert "no timing signal" in s.timing_note
