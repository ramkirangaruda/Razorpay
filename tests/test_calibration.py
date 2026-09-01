"""
Pins the numbers docs/results/calibration.html's prose relies on, same
reasoning as test_l1_measurement.py: the specific finding this page reports -
the model's bucket ordering is NOT monotonic at the top of the range, even
though its classification accuracy beats the table - is exactly the kind of
thing that looks like a bug and gets quietly "fixed" by a future change unless
a test says otherwise. If one of these ever fails, the fix is to update the
prose in sim/render_calibration.py, not to relax the test.
"""

from __future__ import annotations

import pytest

from sim.render_calibration import BUCKETS, collect

N, SEED = 120, 42


@pytest.fixture(scope="module")
def rates():
    return collect(N, SEED)["rates"]


def test_every_bucket_is_covered_for_both_classifiers(rates):
    for name in ("table", "model"):
        assert set(rates[name]) == set(BUCKETS)


def test_table_ordering_is_monotonic_at_the_extremes(rates):
    t = rates["table"]
    assert t["VERY_LOW"][0] < t["LOW"][0] < t["VERY_HIGH"][0]
    assert t["VERY_HIGH"][0] == 1.0  # every VERY_HIGH case in this batch recovered


def test_the_finding_the_page_reports_model_medium_beats_model_very_high(rates):
    """The specific, counterintuitive claim in the "What this says" section:
    the model's own peak realized rate is at MEDIUM, not at its top bucket."""
    m = rates["model"]
    assert m["MEDIUM"][0] > m["VERY_HIGH"][0]
    assert m["VERY_HIGH"][0] < m["LOW"][0] + 0.05  # "sits close to LOW"


def test_the_dip_the_page_reports_high_realizes_below_medium_for_both(rates):
    for name in ("table", "model"):
        assert rates[name]["HIGH"][0] < rates[name]["MEDIUM"][0]


def test_very_low_sample_sizes_are_thin_enough_to_flag(rates):
    """VERY_LOW's table n=3 is the number the page's caveat about thin buckets
    depends on being genuinely small - if the batch generator changes and this
    stops being thin, the caveat's specific wording needs revisiting too."""
    assert rates["table"]["VERY_LOW"][1] <= 5
