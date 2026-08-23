"""
The most important test file in the repo (build spec §10). app/policy.py and app/stopping_rules.py
are pure functions with zero I/O specifically so this file can hit every branch without a database,
a clock, or a mock — every test below builds its inputs directly and asserts on the returned
PolicyDecision.

Organized as: one test class per stopping rule (fires / doesn't fire / edge of the boundary), then
a class for the cross-cutting invariant (never upgrades blast radius), then a handful of multi-rule
integration scenarios.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import app.policy as policy_module
from app.models import FailureClass, InterventionAction
from app.policy import BLAST_RADIUS_RANK, L1Proposal, PolicyViolation, evaluate
from app.stopping_rules import RuleName, RuleOutcome
from app.stopping_rules import (
    CONTACT_FREQUENCY_CAP,
    FAST_RETRY_INTERVAL,
    IST,
    MAX_FAST_RETRIES,
    MAX_LIFETIME_ATTEMPTS,
    STANDARD_MIN_ATTEMPT_INTERVAL,
    PolicyContext,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)  # 17:30 IST — safely outside quiet hours


def make_ctx(**overrides) -> PolicyContext:
    defaults = dict(
        now_utc=NOW,
        failure_class=FailureClass.SOFT_TRANSIENT,
        attempts_so_far=0,
        last_attempt_at_utc=None,
        fast_retries_used=0,
        customer_contacts_in_window=0,
        issuer_breaker_tripped=False,
        issuer_breaker_reset_eta_utc=None,
    )
    defaults.update(overrides)
    return PolicyContext(**defaults)


def make_proposal(failure_class, action, scheduled_for=None, rationale="test") -> L1Proposal:
    return L1Proposal(failure_class, action, scheduled_for, rationale)


class TestHardDeclineNoRetry:
    @pytest.mark.parametrize(
        "hard_class", [FailureClass.HARD_INSTRUMENT, FailureClass.HARD_RISK, FailureClass.HARD_MANDATE]
    )
    @pytest.mark.parametrize("action", [InterventionAction.RETRY_NOW, InterventionAction.RETRY_SCHEDULED])
    def test_hard_class_retry_always_vetoed(self, hard_class, action):
        ctx = make_ctx(failure_class=hard_class)
        proposal = make_proposal(hard_class, action, scheduled_for=NOW + timedelta(hours=1) if action == InterventionAction.RETRY_SCHEDULED else None)
        decision = evaluate(proposal, ctx)

        assert decision.vetoed
        assert "HARD_DECLINE_NO_RETRY" in decision.rules_fired
        assert decision.permitted_action != action

    def test_hard_risk_retry_redirects_to_escalate(self):
        ctx = make_ctx(failure_class=FailureClass.HARD_RISK)
        decision = evaluate(make_proposal(FailureClass.HARD_RISK, InterventionAction.RETRY_NOW), ctx)
        assert decision.permitted_action == InterventionAction.ESCALATE_HUMAN

    def test_hard_instrument_retry_redirects_to_instrument_update(self):
        ctx = make_ctx(failure_class=FailureClass.HARD_INSTRUMENT)
        decision = evaluate(make_proposal(FailureClass.HARD_INSTRUMENT, InterventionAction.RETRY_NOW), ctx)
        assert decision.permitted_action == InterventionAction.REQUEST_INSTRUMENT_UPDATE

    def test_hard_mandate_retry_redirects_to_instrument_update(self):
        ctx = make_ctx(failure_class=FailureClass.HARD_MANDATE)
        decision = evaluate(make_proposal(FailureClass.HARD_MANDATE, InterventionAction.RETRY_SCHEDULED, NOW + timedelta(hours=1)), ctx)
        assert decision.permitted_action == InterventionAction.REQUEST_INSTRUMENT_UPDATE

    def test_hard_risk_nudge_is_also_vetoed(self):
        """build spec §3: HARD_RISK is 'never retry, never nudge' — NUDGE is blocked too, not just retries."""
        ctx = make_ctx(failure_class=FailureClass.HARD_RISK)
        decision = evaluate(make_proposal(FailureClass.HARD_RISK, InterventionAction.NUDGE), ctx)
        assert decision.vetoed
        assert decision.permitted_action == InterventionAction.ESCALATE_HUMAN

    def test_hard_instrument_nudge_is_not_touched_by_this_rule(self):
        """Only HARD_RISK forbids NUDGE outright; HARD_INSTRUMENT/HARD_MANDATE don't ban plain
        NUDGE by this rule (a nudge pointing at general support is fine — REQUEST_INSTRUMENT_UPDATE
        is the *recommended* move, not the only legal one)."""
        ctx = make_ctx(failure_class=FailureClass.HARD_INSTRUMENT)
        decision = evaluate(make_proposal(FailureClass.HARD_INSTRUMENT, InterventionAction.NUDGE), ctx)
        assert "HARD_DECLINE_NO_RETRY" not in decision.rules_fired

    def test_soft_class_retry_never_vetoed_by_this_rule(self):
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_NOW), ctx)
        assert "HARD_DECLINE_NO_RETRY" not in decision.rules_fired

    def test_stop_permanent_on_hard_class_passes_through(self):
        """STOP_PERMANENT is already the safest action; the rule has nothing to do."""
        ctx = make_ctx(failure_class=FailureClass.HARD_RISK)
        decision = evaluate(make_proposal(FailureClass.HARD_RISK, InterventionAction.STOP_PERMANENT), ctx)
        assert not decision.vetoed
        assert decision.permitted_action == InterventionAction.STOP_PERMANENT


class TestMaxLifetimeAttempts:
    def test_under_limit_passes(self):
        ctx = make_ctx(attempts_so_far=MAX_LIFETIME_ATTEMPTS - 1)
        decision = evaluate(make_proposal(FailureClass.SOFT_TRANSIENT, InterventionAction.RETRY_NOW), ctx)
        assert "MAX_LIFETIME_ATTEMPTS" not in decision.rules_fired

    def test_at_limit_blocks_retry(self):
        ctx = make_ctx(attempts_so_far=MAX_LIFETIME_ATTEMPTS)
        decision = evaluate(make_proposal(FailureClass.SOFT_TRANSIENT, InterventionAction.RETRY_NOW), ctx)
        assert "MAX_LIFETIME_ATTEMPTS" in decision.rules_fired
        assert decision.permitted_action == InterventionAction.ESCALATE_HUMAN

    def test_over_limit_blocks_retry(self):
        ctx = make_ctx(attempts_so_far=MAX_LIFETIME_ATTEMPTS + 3)
        decision = evaluate(make_proposal(FailureClass.SOFT_TRANSIENT, InterventionAction.RETRY_SCHEDULED, NOW + timedelta(days=1)), ctx)
        assert decision.permitted_action == InterventionAction.ESCALATE_HUMAN

    def test_does_not_block_non_retry_actions_at_limit(self):
        ctx = make_ctx(attempts_so_far=MAX_LIFETIME_ATTEMPTS)
        decision = evaluate(make_proposal(FailureClass.SOFT_TRANSIENT, InterventionAction.ESCALATE_HUMAN), ctx)
        assert "MAX_LIFETIME_ATTEMPTS" not in decision.rules_fired


class TestMinAttemptInterval:
    def test_no_prior_attempt_never_fires(self):
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, last_attempt_at_utc=None)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_NOW), ctx)
        assert "MIN_ATTEMPT_INTERVAL" not in decision.rules_fired

    def test_standard_class_too_soon_is_deferred(self):
        last = NOW - timedelta(hours=1)
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, last_attempt_at_utc=last)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_NOW), ctx)
        assert "MIN_ATTEMPT_INTERVAL" in decision.rules_fired
        assert decision.permitted_action == InterventionAction.RETRY_SCHEDULED
        assert decision.permitted_scheduled_for == last + STANDARD_MIN_ATTEMPT_INTERVAL

    def test_standard_class_after_24h_passes(self):
        last = NOW - STANDARD_MIN_ATTEMPT_INTERVAL - timedelta(minutes=1)
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, last_attempt_at_utc=last)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_NOW), ctx)
        assert "MIN_ATTEMPT_INTERVAL" not in decision.rules_fired

    def test_transient_fast_retry_allowed_within_30min(self):
        last = NOW - timedelta(minutes=10)
        ctx = make_ctx(failure_class=FailureClass.SOFT_TRANSIENT, last_attempt_at_utc=last, fast_retries_used=0)
        decision = evaluate(make_proposal(FailureClass.SOFT_TRANSIENT, InterventionAction.RETRY_NOW), ctx)
        # 10 min < 30 min fast-retry interval -> still too soon, but the *interval* used is 30min not 24h
        assert decision.permitted_scheduled_for == last + FAST_RETRY_INTERVAL

    def test_transient_fast_retry_after_30min_passes(self):
        last = NOW - FAST_RETRY_INTERVAL - timedelta(minutes=1)
        ctx = make_ctx(failure_class=FailureClass.SOFT_TRANSIENT, last_attempt_at_utc=last, fast_retries_used=0)
        decision = evaluate(make_proposal(FailureClass.SOFT_TRANSIENT, InterventionAction.RETRY_NOW), ctx)
        assert "MIN_ATTEMPT_INTERVAL" not in decision.rules_fired

    def test_transient_exhausted_fast_retries_falls_back_to_24h(self):
        """After MAX_FAST_RETRIES uses, a transient failure is held to the standard 24h interval
        even though its class is still SOFT_TRANSIENT."""
        last = NOW - FAST_RETRY_INTERVAL - timedelta(minutes=1)  # would pass the fast-retry check...
        ctx = make_ctx(
            failure_class=FailureClass.SOFT_TRANSIENT, last_attempt_at_utc=last,
            fast_retries_used=MAX_FAST_RETRIES,  # ...but the exception is used up
        )
        decision = evaluate(make_proposal(FailureClass.SOFT_TRANSIENT, InterventionAction.RETRY_NOW), ctx)
        assert "MIN_ATTEMPT_INTERVAL" in decision.rules_fired
        assert decision.permitted_scheduled_for == last + STANDARD_MIN_ATTEMPT_INTERVAL

    def test_retry_scheduled_with_no_timestamp_falls_back_to_now(self):
        """Defensive case: RETRY_SCHEDULED with no scheduled_for at all shouldn't happen from a
        well-behaved L1 (its JSON schema requires a timestamp for that action), but a classifier
        is an LLM and can misbehave — this shouldn't crash L2, it should just treat "no timestamp"
        as "now", which the interval check then correctly still-too-soon."""
        last = NOW - timedelta(hours=1)
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, last_attempt_at_utc=last)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_SCHEDULED, scheduled_for=None), ctx)
        assert "MIN_ATTEMPT_INTERVAL" in decision.rules_fired
        assert decision.permitted_scheduled_for == last + STANDARD_MIN_ATTEMPT_INTERVAL

    def test_retry_scheduled_too_early_gets_pushed_out(self):
        last = NOW - timedelta(hours=2)
        too_early = NOW + timedelta(hours=1)  # still within 24h of `last`
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, last_attempt_at_utc=last)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_SCHEDULED, too_early), ctx)
        assert decision.permitted_scheduled_for == last + STANDARD_MIN_ATTEMPT_INTERVAL
        assert decision.permitted_scheduled_for > too_early


class TestContactFrequencyCap:
    @pytest.mark.parametrize("action", [InterventionAction.NUDGE, InterventionAction.REQUEST_INSTRUMENT_UPDATE])
    def test_under_cap_passes(self, action):
        ctx = make_ctx(failure_class=FailureClass.SOFT_AUTH, customer_contacts_in_window=CONTACT_FREQUENCY_CAP - 1)
        decision = evaluate(make_proposal(FailureClass.SOFT_AUTH, action), ctx)
        assert "CONTACT_FREQUENCY_CAP" not in decision.rules_fired

    @pytest.mark.parametrize("action", [InterventionAction.NUDGE, InterventionAction.REQUEST_INSTRUMENT_UPDATE])
    def test_at_cap_blocks_contact(self, action):
        ctx = make_ctx(failure_class=FailureClass.SOFT_AUTH, customer_contacts_in_window=CONTACT_FREQUENCY_CAP)
        decision = evaluate(make_proposal(FailureClass.SOFT_AUTH, action), ctx)
        assert "CONTACT_FREQUENCY_CAP" in decision.rules_fired
        assert decision.permitted_action == InterventionAction.ESCALATE_HUMAN

    def test_retry_actions_are_not_capped_by_contact_rule(self):
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, customer_contacts_in_window=99)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_NOW), ctx)
        assert "CONTACT_FREQUENCY_CAP" not in decision.rules_fired


class TestQuietHours:
    def _time_at_ist(self, hour: int, minute: int = 0) -> datetime:
        ist_naive = datetime(2026, 8, 23, hour, minute)
        return ist_naive.replace(tzinfo=IST).astimezone(timezone.utc)

    @pytest.mark.parametrize("hour", [21, 22, 23, 0, 3, 8])
    def test_nudge_during_quiet_hours_deferred(self, hour):
        now = self._time_at_ist(hour)
        ctx = make_ctx(now_utc=now, failure_class=FailureClass.SOFT_AUTH)
        decision = evaluate(make_proposal(FailureClass.SOFT_AUTH, InterventionAction.NUDGE), ctx)
        assert "QUIET_HOURS" in decision.rules_fired
        assert decision.permitted_action == InterventionAction.NUDGE  # action unchanged, only timing
        assert decision.permitted_scheduled_for is not None
        assert decision.permitted_scheduled_for > now

    @pytest.mark.parametrize("hour", [9, 12, 17, 20])
    def test_nudge_during_business_hours_untouched(self, hour):
        now = self._time_at_ist(hour)
        ctx = make_ctx(now_utc=now, failure_class=FailureClass.SOFT_AUTH)
        decision = evaluate(make_proposal(FailureClass.SOFT_AUTH, InterventionAction.NUDGE), ctx)
        assert "QUIET_HOURS" not in decision.rules_fired

    def test_boundary_9am_is_allowed(self):
        """09:00 IST is the end of quiet hours, i.e. contact is allowed starting exactly then."""
        now = self._time_at_ist(9, 0)
        ctx = make_ctx(now_utc=now, failure_class=FailureClass.SOFT_AUTH)
        decision = evaluate(make_proposal(FailureClass.SOFT_AUTH, InterventionAction.NUDGE), ctx)
        assert "QUIET_HOURS" not in decision.rules_fired

    def test_boundary_9pm_is_quiet(self):
        now = self._time_at_ist(21, 0)
        ctx = make_ctx(now_utc=now, failure_class=FailureClass.SOFT_AUTH)
        decision = evaluate(make_proposal(FailureClass.SOFT_AUTH, InterventionAction.NUDGE), ctx)
        assert "QUIET_HOURS" in decision.rules_fired

    def test_retries_are_not_subject_to_quiet_hours(self):
        now = self._time_at_ist(2, 0)  # 2am IST
        ctx = make_ctx(now_utc=now, failure_class=FailureClass.SOFT_FUNDS)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_NOW), ctx)
        assert "QUIET_HOURS" not in decision.rules_fired


class TestIssuerCircuitBreaker:
    def test_not_tripped_passes(self):
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, issuer_breaker_tripped=False)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_NOW), ctx)
        assert "ISSUER_CIRCUIT_BREAKER" not in decision.rules_fired

    def test_tripped_defers_retry_now(self):
        reset_eta = NOW + timedelta(minutes=20)
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, issuer_breaker_tripped=True, issuer_breaker_reset_eta_utc=reset_eta)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_NOW), ctx)
        assert "ISSUER_CIRCUIT_BREAKER" in decision.rules_fired
        assert decision.permitted_action == InterventionAction.RETRY_SCHEDULED
        assert decision.permitted_scheduled_for == reset_eta

    def test_tripped_pushes_out_a_too_early_scheduled_retry(self):
        reset_eta = NOW + timedelta(minutes=20)
        too_early = NOW + timedelta(minutes=5)
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, issuer_breaker_tripped=True, issuer_breaker_reset_eta_utc=reset_eta)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_SCHEDULED, too_early), ctx)
        assert decision.permitted_scheduled_for == reset_eta

    def test_tripped_does_not_touch_a_retry_already_scheduled_past_reset(self):
        reset_eta = NOW + timedelta(minutes=20)
        comfortably_later = NOW + timedelta(hours=2)
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, issuer_breaker_tripped=True, issuer_breaker_reset_eta_utc=reset_eta)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_SCHEDULED, comfortably_later), ctx)
        assert "ISSUER_CIRCUIT_BREAKER" not in decision.rules_fired

    def test_tripped_does_not_affect_non_retry_actions(self):
        ctx = make_ctx(failure_class=FailureClass.SOFT_AUTH, issuer_breaker_tripped=True, issuer_breaker_reset_eta_utc=NOW + timedelta(minutes=20))
        decision = evaluate(make_proposal(FailureClass.SOFT_AUTH, InterventionAction.NUDGE), ctx)
        assert "ISSUER_CIRCUIT_BREAKER" not in decision.rules_fired


class TestInputValidation:
    def test_mismatched_context_and_proposal_class_raises(self):
        """evaluate() refuses to run if the caller assembled a PolicyContext for a different
        failure class than the proposal it's paired with — a caller-side bug, not a policy
        decision, so it's a loud ValueError rather than a silently wrong evaluation."""
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS)
        mismatched_proposal = make_proposal(FailureClass.HARD_RISK, InterventionAction.RETRY_NOW)
        with pytest.raises(ValueError):
            evaluate(mismatched_proposal, ctx)


class TestNeverUpgradeInvariant:
    """The property build spec §5 calls out by name: 'L2 can only reduce the blast radius of L1's
    proposal, never expand it.' Every scenario above already asserts this implicitly by checking
    specific outcomes; this class asserts it directly and exhaustively across the whole action
    space, including several rules firing in combination."""

    ALL_ACTIONS = list(InterventionAction)
    ALL_CLASSES = list(FailureClass)

    @pytest.mark.parametrize("failure_class", ALL_CLASSES)
    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_permitted_rank_never_exceeds_proposed_rank_clean_context(self, failure_class, action):
        ctx = make_ctx(failure_class=failure_class)
        scheduled_for = NOW + timedelta(hours=1) if action == InterventionAction.RETRY_SCHEDULED else None
        decision = evaluate(make_proposal(failure_class, action, scheduled_for), ctx)
        assert BLAST_RADIUS_RANK[decision.permitted_action] <= BLAST_RADIUS_RANK[action]

    @pytest.mark.parametrize("failure_class", ALL_CLASSES)
    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_permitted_rank_never_exceeds_proposed_rank_hostile_context(self, failure_class, action):
        """Every rule maximally triggered at once: max attempts blown, contact cap blown, breaker
        tripped, mid-quiet-hours, last attempt seconds ago. If the invariant holds here, it holds
        anywhere — this is deliberately the worst context evaluate() can be called with."""
        hostile_now = datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc).astimezone(IST)
        ctx = make_ctx(
            now_utc=hostile_now.astimezone(timezone.utc),
            failure_class=failure_class,
            attempts_so_far=MAX_LIFETIME_ATTEMPTS + 5,
            last_attempt_at_utc=hostile_now.astimezone(timezone.utc) - timedelta(seconds=1),
            fast_retries_used=MAX_FAST_RETRIES,
            customer_contacts_in_window=CONTACT_FREQUENCY_CAP + 5,
            issuer_breaker_tripped=True,
            issuer_breaker_reset_eta_utc=hostile_now.astimezone(timezone.utc) + timedelta(hours=1),
        )
        scheduled_for = NOW + timedelta(hours=1) if action == InterventionAction.RETRY_SCHEDULED else None
        decision = evaluate(make_proposal(failure_class, action, scheduled_for), ctx)
        assert BLAST_RADIUS_RANK[decision.permitted_action] <= BLAST_RADIUS_RANK[action]

    def test_a_rule_that_tries_to_upgrade_trips_the_safety_net(self):
        """No real rule in stopping_rules.py should ever do this — that's what the exhaustive grid
        above is for. This test proves the *guard itself* actually works, by injecting a
        deliberately broken rule that tries to upgrade a NUDGE proposal to RETRY_NOW, and
        confirming evaluate() refuses to let it through rather than silently complying."""
        evil_rule = lambda ctx, action, scheduled_for: RuleOutcome(  # noqa: E731
            fired=True, rule=RuleName.QUIET_HOURS, forced_action=InterventionAction.RETRY_NOW,
            note="malicious upgrade attempt",
        )
        ctx = make_ctx(failure_class=FailureClass.SOFT_AUTH)
        proposal = make_proposal(FailureClass.SOFT_AUTH, InterventionAction.NUDGE)
        with patch.object(policy_module, "RULE_PIPELINE", [evil_rule]):
            with pytest.raises(policy_module.PolicyViolation):
                evaluate(proposal, ctx)

    def test_no_rule_ever_raises_policy_violation_across_full_grid(self):
        """PolicyViolation is the assertion inside evaluate() itself; this just confirms the grid
        above never trips it, which the parametrized tests already do implicitly (an unhandled
        exception fails the test) — kept as an explicit regression marker."""
        for failure_class in FailureClass:
            for action in InterventionAction:
                ctx = make_ctx(failure_class=failure_class, issuer_breaker_tripped=True,
                                issuer_breaker_reset_eta_utc=NOW + timedelta(minutes=10),
                                attempts_so_far=MAX_LIFETIME_ATTEMPTS,
                                customer_contacts_in_window=CONTACT_FREQUENCY_CAP)
                scheduled_for = NOW + timedelta(hours=1) if action == InterventionAction.RETRY_SCHEDULED else None
                try:
                    evaluate(make_proposal(failure_class, action, scheduled_for), ctx)
                except PolicyViolation:
                    pytest.fail(f"PolicyViolation for ({failure_class}, {action})")


class TestIntegrationScenarios:
    """A few end-to-end scenarios exercising more than one rule together, matching the classes'
    'correct move' column from the build spec's failure taxonomy table."""

    def test_soft_funds_retried_too_soon_gets_rescheduled_not_vetoed(self):
        last = NOW - timedelta(hours=3)
        ctx = make_ctx(failure_class=FailureClass.SOFT_FUNDS, last_attempt_at_utc=last, attempts_so_far=1)
        decision = evaluate(make_proposal(FailureClass.SOFT_FUNDS, InterventionAction.RETRY_NOW), ctx)
        assert decision.downgraded and not decision.vetoed  # same action type, just deferred
        assert decision.permitted_action == InterventionAction.RETRY_SCHEDULED

    def test_hard_mandate_nudge_capped_and_hard_ruled_only_hard_rule_applies(self):
        """A HARD_MANDATE nudge with the contact cap also blown: HARD_DECLINE_NO_RETRY doesn't
        touch plain NUDGE (only HARD_RISK does), so CONTACT_FREQUENCY_CAP is the one that fires."""
        ctx = make_ctx(failure_class=FailureClass.HARD_MANDATE, customer_contacts_in_window=CONTACT_FREQUENCY_CAP)
        decision = evaluate(make_proposal(FailureClass.HARD_MANDATE, InterventionAction.NUDGE), ctx)
        assert decision.rules_fired == ("CONTACT_FREQUENCY_CAP",)
        assert decision.permitted_action == InterventionAction.ESCALATE_HUMAN

    def test_exhausted_attempts_during_quiet_hours_retry_proposal(self):
        night = datetime(2026, 8, 23, 23, 0, tzinfo=IST).astimezone(timezone.utc)
        ctx = make_ctx(now_utc=night, failure_class=FailureClass.SOFT_LIMIT, attempts_so_far=MAX_LIFETIME_ATTEMPTS)
        decision = evaluate(make_proposal(FailureClass.SOFT_LIMIT, InterventionAction.RETRY_NOW), ctx)
        # MAX_LIFETIME_ATTEMPTS fires and redirects to ESCALATE_HUMAN; QUIET_HOURS doesn't apply to
        # ESCALATE_HUMAN so it never gets a chance to fire on this proposal.
        assert decision.permitted_action == InterventionAction.ESCALATE_HUMAN
        assert "MAX_LIFETIME_ATTEMPTS" in decision.rules_fired
        assert "QUIET_HOURS" not in decision.rules_fired

    def test_clean_soft_auth_fresh_link_passes_through(self):
        """The taxonomy's 'correct move' for SOFT_AUTH is a fresh payment link, modeled as NUDGE
        with a link — should sail through untouched with no history against it."""
        ctx = make_ctx(failure_class=FailureClass.SOFT_AUTH)
        decision = evaluate(make_proposal(FailureClass.SOFT_AUTH, InterventionAction.NUDGE), ctx)
        assert not decision.vetoed and not decision.downgraded
        assert decision.rules_fired == ()
