"""
The claims in README.md, asserted against a live run.

Documentation drifts. A number written into a README on the day it was measured
is a number that will be wrong three commits later, and a submission whose
headline figures do not reproduce is worse than one with no figures — a judge
who checks one number and finds it stale stops trusting the rest of the page.

So the claims are tests. If a change moves the result, this file fails and the
README has to be updated deliberately rather than silently going out of date.

These are slower than the unit tests because each one runs the full batch
through every arm. That is the cost of the guarantee and it is worth paying.
"""

from __future__ import annotations

import pytest

from app.scorer import Beliefs
from sim.run_arms import ADVERSARIAL, WorldParams, run

N, SEED = 120, 42


@pytest.fixture(scope="module")
def arms():
    return run(N, SEED, WorldParams(), Beliefs.from_constants())


@pytest.fixture(scope="module")
def floor(arms):
    return arms["0_do_nothing"]


def test_do_nothing_adds_exactly_nothing(arms, floor):
    """The reference arm is the zero by definition. If it drifts, so does every
    other arm's headline number, since they are all measured against it."""
    assert floor.value_added_over(floor) == 0.0
    assert floor.attempts == 0 and floor.contacts == 0 and floor.recovered == 0


def test_backstop_does_not_beat_naive_on_raw_value(arms, floor):
    """
    The README says so in bold, and it is the claim most likely to be quietly
    reversed by a well-meaning tuning change. If Backstop ever does win here,
    that is a genuine result and the README should be rewritten to say it — but
    deliberately, with the reason understood, not by a number moving unnoticed.
    """
    naive = arms["A_naive"].value_added_over(floor)
    backstop = max(
        arms["C_backstop"].value_added_over(floor),
        arms["D_backstop_llm"].value_added_over(floor),
    )
    assert backstop < naive, (
        "Backstop now beats naive on raw value. That is not a failure — but the "
        "README's headline framing is built on it not being true, so update the "
        "README rather than this assertion."
    )


def test_the_efficiency_claim_holds(arms, floor):
    """
    "93% of the value, 49% of the attempts, 82% of the contacts."

    Now measured on arm D, the model-classifier arm, which is the headline the
    README leads with. Arm C (table classifier) is asserted separately below.

    These moved when the parallel L2a was discarded in favour of the existing
    one. The ordering in app/policy.BLAST_RADIUS_RANK is the reason: it ranks a
    retry above a customer contact, where the earlier draft ranked them the other
    way, so the scorer's candidate set is different and the arm trades attempts
    for contacts rather than the reverse.

    Tolerances are wide enough to survive a harmless refactor and tight enough
    that a real regression trips them.
    """
    naive, backstop = arms["A_naive"], arms["D_backstop_llm"]
    value_ratio = backstop.value_added_over(floor) / naive.value_added_over(floor)
    attempt_ratio = backstop.attempts / naive.attempts
    contact_ratio = backstop.contacts / naive.contacts

    assert value_ratio == pytest.approx(0.93, abs=0.04), f"value ratio {value_ratio:.3f}"
    assert attempt_ratio == pytest.approx(0.49, abs=0.06), f"attempt ratio {attempt_ratio:.3f}"
    assert contact_ratio == pytest.approx(0.82, abs=0.06), f"contact ratio {contact_ratio:.3f}"

    # The shape of the claim, independent of the exact figures: Backstop is
    # cheaper on BOTH harm axes than the baseline it is being compared to.
    assert attempt_ratio < 1.0 and contact_ratio < 1.0


def test_backstop_is_not_dominated_by_rules_only(arms, floor):
    """
    An earlier build had Backstop worse than rules-only on value AND on harm,
    which is not restraint, it is a bug — a real trade-off shows up as a
    frontier, not as one arm losing on every axis. This asserts the EV layer
    buys something: it must use fewer contacts than rules-only.
    """
    rules, backstop = arms["B_rules_only"], arms["C_backstop"]
    assert backstop.contacts < rules.contacts, (
        "the EV layer is meant to buy a contact reduction over the plain rules arm"
    )


def test_naive_arm_matches_razorpays_documented_schedule(arms):
    """
    The baseline has to stay Razorpay's actual default — three daily retries
    then a card-update email — or the comparison stops being against something
    a judge recognises and becomes a strawman we invented.
    """
    naive = arms["A_naive"]
    assert naive.attempts / naive.invoices == pytest.approx(2.33, abs=0.4), (
        "naive should average close to the documented three retries per invoice"
    )
    assert naive.vetoes == 0, "the naive arm runs no policy gate, by construction"


def test_vetoes_are_attributed_to_a_basis(arms):
    """Every veto must be classifiable as compliance or as a scorer backstop —
    that split is what makes the veto-rate metric mean anything."""
    for name in ("B_rules_only", "C_backstop", "D_backstop_llm"):
        r = arms[name]
        assert sum(r.veto_by_basis.values()) == r.vetoes
        assert set(r.veto_by_basis) <= {"REGULATORY", "BACKSTOP"}


def test_under_proposals_are_measured_and_nonzero(arms):
    """
    Measuring L1 in only one direction tells half the story. Under-proposal is
    the direction L2a structurally cannot catch, so a zero here would mean the
    metric is broken rather than that the agent is perfect.
    """
    assert arms["C_backstop"].under_proposals > 0
    assert arms["0_do_nothing"].under_proposals > 0, (
        "an arm that never acts must under-propose on every recoverable invoice"
    )


def test_the_batch_composition_is_40_35_25():
    """§8's design. If the split drifts, the 'rules-only ties on the clean 40%'
    framing stops being true and the README has to change with it."""
    from dataclasses import asdict

    from sim.generate_batch import generate_batch

    cases = [asdict(c) for c in generate_batch(N, SEED)]
    counts = {b: sum(1 for c in cases if c["bucket"] == b) for b in ("CLEAN", "AMBIGUOUS", "CONTEXT")}
    assert counts["CLEAN"] / N == pytest.approx(0.40, abs=0.02)
    assert counts["AMBIGUOUS"] / N == pytest.approx(0.35, abs=0.02)
    assert counts["CONTEXT"] / N == pytest.approx(0.25, abs=0.02)


def test_ground_truth_never_rewards_retrying_an_otp_dropoff():
    """
    Regression guard on the oracle bug that cost a day. A SOFT_AUTH failure is a
    customer who never authorised anything; no number of re-presentments fixes
    it. If this fails, ground truth is once again paying the naive arm for doing
    the one thing every source agrees does not work.
    """
    from dataclasses import asdict

    from sim.generate_batch import generate_batch

    for c in (asdict(x) for x in generate_batch(N, SEED)):
        gt = c["ground_truth"]
        if gt["true_class"] in ("SOFT_AUTH", "HARD_INSTRUMENT", "HARD_RISK", "HARD_MANDATE"):
            assert gt["retry_attempts_needed"] is None, (
                f"{c['case_id']} ({gt['true_class']}) is marked recoverable by retrying"
            )


def test_the_advantage_survives_being_wrong_about_the_constants():
    """
    §9.4's requirement, as an assertion. The agent is handed churn beliefs from
    a third of ours to triple, the world is held fixed, and the value added must
    degrade gracefully rather than inverting.
    """
    base = Beliefs.from_constants()
    world = WorldParams()
    values = []
    for factor in (0.3, 0.5, 1.0, 2.0, 3.0):
        r = run(N, SEED, world, base.perturbed({"contact_fatigue_base": factor}))
        values.append(r["C_backstop"].value_added_over(r["0_do_nothing"]))

    spread = (max(values) - min(values)) / max(values)
    assert spread < 0.10, (
        f"value added swung {spread:.1%} across a 10x error in the agent's churn "
        f"belief; the robustness claim in the README allows well under that. {values}"
    )
    assert all(v > 0 for v in values)


def test_the_adversarial_world_is_reported_not_hidden():
    """
    §10.3. With contact fatigue and issuer penalties both zero, blind retrying
    costs nothing beyond the gateway fee, and naive SHOULD gain. Asserting the
    uncomfortable direction so nobody can quietly make the adversarial arm
    flattering.
    """
    base = Beliefs.from_constants()
    normal = run(N, SEED, WorldParams(), base)
    adv = run(N, SEED, ADVERSARIAL, base)

    naive_gain = (adv["A_naive"].value_added_over(adv["0_do_nothing"])
                  - normal["A_naive"].value_added_over(normal["0_do_nothing"]))
    assert naive_gain > 0, (
        "in a world where retrying is free, the arm that retries most should do "
        "better. If it does not, the adversarial arm is not adversarial."
    )
