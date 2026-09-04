"""
What a language model actually bought over a decision table.

These pin the L1 measurement, which is the one part of this project where the
temptation to overclaim is strongest. Every number the README reports about the
model is asserted here against the recorded classifications, so a change that
moves a figure fails a test rather than quietly making a document wrong.

PROVENANCE. The classifications in `sim/data/l1_classifications_seed42.json`
were produced by a large language model reading only the fields a real L1
receives, in four separate fresh contexts that had never seen `sim/generate_batch.py` or the
ground truth. That isolation is what makes these numbers mean anything —
whoever writes the batch generator knows the answer key, so a classification
produced by that same context would be contaminated. It is not a live API run
and nothing here claims it is.
"""

from __future__ import annotations

import collections
import json

import pytest

from app.classifier import CachedLLMClassifier, LookupClassifier
from sim.generate_batch import generate_batch

N, SEED = 120, 42
ORDINAL = ["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"]


@pytest.fixture(scope="module")
def cases():
    from dataclasses import asdict

    return [asdict(c) for c in generate_batch(N, SEED)]


@pytest.fixture(scope="module")
def llm():
    return CachedLLMClassifier()


@pytest.fixture(scope="module")
def table():
    return LookupClassifier()


def _accuracy(clf, cases, bucket=None) -> float:
    sel = [c for c in cases if bucket is None or c["bucket"] == bucket]
    hits = sum(
        clf.classify(c, {"attempts": 0, "contacts": 0}).classification.value
        == c["ground_truth"]["true_class"]
        for c in sel
    )
    return hits / len(sel)


# ---------------------------------------------------------------------------
# The recording itself
# ---------------------------------------------------------------------------


def test_the_recording_covers_the_whole_batch(llm, cases):
    """A partial recording would score the model on a subset while the table is
    scored on all of it, which is not a comparison."""
    assert llm.available
    assert len(llm._records) == N
    for c in cases:
        llm.classify(c, {"attempts": 0, "contacts": 0})
    assert llm.misses == 0


def test_the_recording_carries_its_provenance(llm):
    """
    The file has to say what it is. A judge who finds a folder of model outputs
    with no note about how they were produced is right to discount all of them.
    """
    raw = json.load(open(llm.path))
    note = raw["_README"].lower()
    assert "never saw ground_truth" in note or "never saw" in note
    assert "not" in note and "live api run" in note


def test_no_classification_is_a_float(llm, cases):
    """The whole reason L1 emits an ordinal. Guarded at the boundary too, but a
    recording is a file on disk and files get hand-edited."""
    for c in cases:
        assert llm.classify(c, {"attempts": 0, "contacts": 0}).recovery_bucket in ORDINAL


# ---------------------------------------------------------------------------
# Where the model wins, and where it ties
# ---------------------------------------------------------------------------


def test_the_model_ties_the_table_on_clean_payloads(llm, table, cases):
    """
    Both are perfect on the clean 40%, and the README says so rather than
    reporting a uniform win. "The model adds nothing on 40% of traffic, and here
    is the 60% where it does" is checkable; a uniform win invites the reader to
    go looking for the smoothing, and the ablation would show it.
    """
    assert _accuracy(llm, cases, "CLEAN") == 1.0
    assert _accuracy(table, cases, "CLEAN") == 1.0


def test_the_model_ties_on_context_dependent_cases_too(llm, table, cases):
    """
    Also a tie, and worth stating because it is easy to assume otherwise. The
    CONTEXT bucket has clean payloads — its difficulty is in choosing the
    ACTION from customer history, not in naming the class. Classification
    accuracy is the wrong instrument for that bucket, and reporting a win here
    would be measuring the wrong thing.
    """
    assert _accuracy(llm, cases, "CONTEXT") == 1.0
    assert _accuracy(table, cases, "CONTEXT") == 1.0


def test_the_model_wins_on_ambiguous_payloads(llm, table, cases):
    """~52% against ~38%. The only bucket where the classifier matters."""
    a_llm = _accuracy(llm, cases, "AMBIGUOUS")
    a_tbl = _accuracy(table, cases, "AMBIGUOUS")
    assert a_llm > a_tbl
    assert a_llm == pytest.approx(0.52, abs=0.06)
    assert a_tbl == pytest.approx(0.38, abs=0.06)


def test_the_entire_advantage_comes_from_the_source_step_triple(llm, table, cases):
    """
    The sharpest version of the finding, and the one worth defending.

    Split the ambiguous bucket by what was actually done to the payload:

      - `description_contradicts_reason` — the structured `reason` survives
        intact and both classifiers trust it. Tie at 100%.
      - `generic_decline_no_specific_reason` — code, description, source, step
        and reason are ALL replaced by a bare bank decline. No signal survives,
        and neither classifier can do better than the base rate. Tie.
      - `missing_reason` — `reason` is null but `source` and `step` survive.
        The table keys on `reason` alone and collapses to its fallback; the
        model reads source × step and recovers most of them.

    So the model's whole advantage lives in one place: a payload where the
    gateway gave no reason but did say WHERE and FROM WHOM the failure came.
    That is exactly the claim the design made in advance — the source × step ×
    reason triple is what makes L1 worth having over a dictionary — and it is
    falsifiable rather than a vague appeal to model quality.
    """
    amb = [c for c in cases if c["bucket"] == "AMBIGUOUS"]
    groups = collections.defaultdict(list)
    for c in amb:
        groups[tuple(sorted(c["ambiguity"]))].append(c)

    def acc(clf, sel):
        return sum(
            clf.classify(c, {"attempts": 0, "contacts": 0}).classification.value
            == c["ground_truth"]["true_class"]
            for c in sel
        ) / len(sel)

    missing = next(v for k, v in groups.items() if "missing_reason" in k)
    contradict = next(v for k, v in groups.items() if "description_contradicts_reason" in k)
    generic = next(v for k, v in groups.items() if any("generic_decline" in x for x in k))

    # The win, and it is a large one.
    assert acc(llm, missing) > acc(table, missing) + 0.25

    # The ties, which matter just as much for the honesty of the claim.
    assert acc(llm, contradict) == acc(table, contradict) == 1.0
    assert abs(acc(llm, generic) - acc(table, generic)) < 0.15


# ---------------------------------------------------------------------------
# Calibration — the finding that turned a loss into a win
# ---------------------------------------------------------------------------


def test_confidence_is_well_calibrated(llm, cases):
    """
    HIGH ~100%, MEDIUM ~76%, LOW ~20%. Strictly decreasing.

    This is the most useful single number the model produces, because it is
    actionable: it says which of its own answers to discount. A model that were
    uniformly 83% accurate with no usable confidence signal would be worth much
    less, whatever its headline accuracy.
    """
    truth = {c["case_id"]: c["ground_truth"]["true_class"] for c in cases}
    by_conf = collections.defaultdict(list)
    for cid, rec in llm._records.items():
        by_conf[rec["classification_confidence"]].append(
            rec["classification"] == truth[cid]
        )

    acc = {k: sum(v) / len(v) for k, v in by_conf.items()}
    assert acc["HIGH"] > acc["MEDIUM"] > acc["LOW"], acc
    assert acc["HIGH"] == pytest.approx(1.00, abs=0.05)
    assert acc["LOW"] < 0.40, "LOW-confidence answers must be visibly unreliable"


def test_gating_the_bucket_on_confidence_is_on_by_default(llm):
    """
    The scorer consumes `recovery_bucket` and has no view of confidence, so a
    LOW-confidence bucket is trusted exactly as much as a HIGH-confidence one
    unless the classifier does the gating itself.

    Leaving that ungated measurably cost value — see the test below. Default on.
    """
    assert llm.trust_bucket_below_confidence is False


def test_ungated_buckets_are_more_dispersed_than_the_tables(cases):
    """
    The mechanism behind the loss, isolated.

    The model's per-case buckets spread wider than the table's class-level
    mapping at roughly the same mean — more mass at both ends. That extra
    dispersion is real information when the model is right, and a stop-signal
    manufactured from thin evidence when it is not. Since accuracy at LOW
    confidence is ~20%, most of the extra pessimism is noise.
    """
    raw = CachedLLMClassifier(trust_bucket_below_confidence=True)
    tbl = LookupClassifier()
    idx = {b: i for i, b in enumerate(ORDINAL)}

    def spread(clf):
        vals = [
            idx[clf.classify(c, {"attempts": 0, "contacts": 0}).recovery_bucket]
            for c in cases
        ]
        mean = sum(vals) / len(vals)
        return mean, sum((v - mean) ** 2 for v in vals) / len(vals)

    m_raw, var_raw = spread(raw)
    m_tbl, var_tbl = spread(tbl)
    assert var_raw > var_tbl, "the model's buckets should be the more dispersed"
    assert abs(m_raw - m_tbl) < 0.3, "at a comparable mean — this is spread, not bias"


def test_confidence_gating_pays_for_itself(cases):
    """
    The result, end to end, and the reason the default is what it is.

    Same recording, same scorer, same policy gate — the only difference is
    whether a LOW-confidence bucket is believed. Trusting it loses value.

    Worth stating plainly because it is the counterintuitive part: better
    classification accuracy did NOT by itself produce more money. The model's
    extra information had to be filtered by the model's own reliability signal
    before it was worth anything.
    """
    from app.scorer import Beliefs
    from sim.run_arms import WorldParams, run

    beliefs, world = Beliefs.from_constants(), WorldParams()
    results = run(N, SEED, world, beliefs)
    floor = results["0_do_nothing"]

    gated = results["D_backstop_llm"].value_added_over(floor)
    table_arm = results["C_backstop"].value_added_over(floor)

    assert gated > table_arm, (
        "with confidence gating the model arm should beat the table arm; "
        f"got {gated:,.0f} against {table_arm:,.0f}"
    )


def test_the_model_arm_is_cheaper_on_both_harm_axes(cases):
    """
    The claim that makes the arm worth shipping rather than merely interesting:
    it is not buying value by doing more. Fewer attempts AND fewer contacts than
    both the table arm and the rules-only arm.
    """
    from app.scorer import Beliefs
    from sim.run_arms import WorldParams, run

    r = run(N, SEED, WorldParams(), Beliefs.from_constants())
    d, c, b = r["D_backstop_llm"], r["C_backstop"], r["B_rules_only"]
    assert d.attempts < c.attempts < b.attempts
    assert d.contacts < c.contacts < b.contacts


def test_arms_degrade_to_abc_without_a_recording(monkeypatch):
    """
    A fresh checkout, or a batch regenerated at a different seed, must still
    produce A/B/C rather than crashing or scoring the model on cases it never
    saw.
    """
    from app.scorer import Beliefs
    from sim import run_arms

    monkeypatch.setattr(
        run_arms, "CachedLLMClassifier", lambda: CachedLLMClassifier(path="/nonexistent.json")
    )
    names = [a.name for a in run_arms.build_arms(Beliefs.from_constants())]
    assert "D_backstop_llm" not in names
    assert names == ["0_do_nothing", "A_naive", "B_rules_only", "C_backstop"]
