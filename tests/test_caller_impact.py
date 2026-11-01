"""Section 4.4's caller-level numbers, and the hypergeometric model behind them.

The section converts a pair-level rate into a caller-level one. That is a model, not a
measurement, so two separate things need guarding: the arithmetic, which has a textbook
closed form and a handful of edges where it can quietly return the most alarming possible
answer; and the published figures, which are quoted in prose beside an artefact that
nothing otherwise compares them against.

The edges matter more than the middle here. `_at_least_one` is asked about callers
importing ten names from modules that export three, and about pairs where every exported
name broke. Returning 1.0 for the first would turn "the question does not apply" into
certainty of breakage, which is the direction a reader would believe.
"""

from __future__ import annotations

import json
import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))

MEASURED = ROOT / "research" / "results" / "semver_compliance.json"
PAPER = ROOT / "paper" / "main.md"

# Section 4.4's table and prose, as published.
PUBLISHED_PAIRS = 1_840
PUBLISHED_API_RATE = 0.0886
PUBLISHED_CALLER = {1: 0.0058, 3: 0.0140, 10: 0.0345}
PUBLISHED_MEDIAN_SHARE = 0.0217
PUBLISHED_MEAN_SHARE = 0.0652
PUBLISHED_PROMISING = 2_010
PUBLISHED_EXPORTING_NOTHING = 170
PUBLISHED_BROKE = 163


def _model():
    import semver_compliance  # noqa: PLC0415

    return semver_compliance


def _measured():
    if not MEASURED.exists():
        pytest.skip("research/results/semver_compliance.json is not present")
    return json.loads(MEASURED.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------ the model


def test_the_model_is_the_hypergeometric_closed_form():
    at_least_one = _model()._at_least_one
    for exported, broken, k in ((100, 5, 1), (100, 5, 3), (800, 1, 10), (50, 20, 4)):
        expected = 1.0 - math.comb(exported - broken, k) / math.comb(exported, k)
        assert at_least_one(exported, broken, k) == pytest.approx(expected)


def test_one_name_in_n_gives_exactly_the_broken_share():
    """The k=1 case has to reduce to b/n, or the model is not what the paper says."""
    at_least_one = _model()._at_least_one
    for exported, broken in ((100, 7), (800, 1), (3, 2)):
        assert at_least_one(exported, broken, 1) == pytest.approx(broken / exported)


def test_a_caller_importing_more_names_is_never_safer():
    at_least_one = _model()._at_least_one
    values = [at_least_one(200, 6, k) for k in range(1, 20)]
    assert values == sorted(values)
    assert all(0.0 <= v <= 1.0 for v in values)


def test_nothing_broken_is_zero_not_a_rounding():
    at_least_one = _model()._at_least_one
    for k in (1, 3, 10):
        assert at_least_one(500, 0, k) == 0.0


def test_a_pair_that_lost_everything_is_certain_for_any_caller():
    """The max observed broken-surface share is 100%, so this case is in the data."""
    at_least_one = _model()._at_least_one
    for k in (1, 3, 10):
        assert at_least_one(10, 10, k) == 1.0


def test_a_caller_larger_than_the_surface_is_refused_not_answered_with_one():
    """Returning 1.0 here would read as certain breakage rather than "does not apply"."""
    at_least_one = _model()._at_least_one
    assert at_least_one(3, 1, 10) is None
    assert at_least_one(0, 0, 1) is None
    assert at_least_one(100, 5, 0) is None
    assert at_least_one(100, 5, -1) is None
    # Exactly at the surface size it does apply.
    assert at_least_one(10, 1, 10) == pytest.approx(1.0)


def test_more_broken_than_exported_is_clamped_rather_than_raising():
    """A count and a surface are measured separately and need not agree."""
    at_least_one = _model()._at_least_one
    assert at_least_one(5, 99, 2) == 1.0


def test_the_promising_cohorts_exclude_the_ones_semver_permits_to_break():
    model = _model()
    assert set(model.PROMISING) == {"patch", "minor", "0.x-patch"}
    assert "major" not in model.PROMISING
    assert "0.x-breaking" not in model.PROMISING
    # 0.0.z promises nothing and is counted in neither direction.
    assert set(model.UNCLASSIFIED) == {"0.0.z"}
    assert not set(model.PROMISING) & set(model.NOT_PROMISING)
    assert not set(model.PROMISING) & set(model.UNCLASSIFIED)


# ------------------------------------------------------------ the numbers in the section


def test_the_denominators_account_for_the_step_between_the_two_rates():
    """8.11% and 8.86% share a numerator; the gap is the pairs exporting nothing."""
    data = _measured()
    compliance = data["semver_compliance"]["compliance_rate"]
    impact = data["caller_impact"]
    promising = data["caller_impact"]["promising"]
    assert compliance["pairs"] == PUBLISHED_PROMISING
    assert compliance["broke"] == PUBLISHED_BROKE
    assert promising["pairs"] == PUBLISHED_PAIRS
    assert promising["api_level_rate"]["successes"] == PUBLISHED_BROKE
    # The step is exactly the pairs with no exported surface, not a changed predicate.
    assert compliance["pairs"] - promising["pairs"] == PUBLISHED_EXPORTING_NOTHING
    assert impact["pairs_exporting_nothing"] >= PUBLISHED_EXPORTING_NOTHING


def test_the_caller_rates_are_the_ones_the_table_prints():
    promising = _measured()["caller_impact"]["promising"]
    assert promising["api_level_rate"]["rate"] == pytest.approx(PUBLISHED_API_RATE, abs=5e-5)
    for k, value in PUBLISHED_CALLER.items():
        assert promising[f"expected_caller_rate_k{k}"] == pytest.approx(value, abs=5e-5), k


def test_the_broken_surface_share_is_the_one_the_section_quotes():
    share = _measured()["caller_impact"]["promising"]["broken_surface_share"]
    assert share["median"] == pytest.approx(PUBLISHED_MEDIAN_SHARE, abs=5e-5)
    assert share["mean"] == pytest.approx(PUBLISHED_MEAN_SHARE, abs=5e-5)
    assert share["max"] == pytest.approx(1.0, abs=1e-9)


def test_the_caller_rate_is_below_the_pair_rate_at_every_caller_size():
    """The direction of the whole section; if it inverted, 4.4 would be wrong."""
    promising = _measured()["caller_impact"]["promising"]
    pair_rate = promising["api_level_rate"]["rate"]
    for k in PUBLISHED_CALLER:
        assert promising[f"expected_caller_rate_k{k}"] < pair_rate, k


def test_the_section_quotes_the_artefact_and_not_a_rounding_of_its_own():
    data = _measured()
    promising = data["caller_impact"]["promising"]
    text = PAPER.read_text(encoding="utf-8")
    start = text.index("**4.4 A caller is not a package")
    section = " ".join(text[start : text.index("\n# 5.", start)].split())
    for k in (1, 3, 10):
        rendered = f"{promising[f'expected_caller_rate_k{k}'] * 100:.2f}%"
        assert rendered in section, (k, rendered)
    assert f"{promising['api_level_rate']['rate'] * 100:.2f}%" in section
    share = promising["broken_surface_share"]
    assert f"{share['median'] * 100:.2f}%" in section
    assert f"{share['mean'] * 100:.2f}%" in section
    assert f"{promising['pairs']:,}" in section


def test_the_section_keeps_the_direction_of_its_own_bias():
    """The uniform assumption overstates impact; an edit must not drop that."""
    text = PAPER.read_text(encoding="utf-8")
    start = text.index("**4.4 A caller is not a package")
    section = " ".join(text[start : text.index("\n# 5.", start)].split())
    assert "overstates" in section
    assert "upper bound on caller impact" in section
    assert "is not an estimate of the" in section


def test_the_abstract_and_the_heading_agree_on_how_many_qualifications_there_are():
    text = PAPER.read_text(encoding="utf-8")
    assert "# 4. Four qualifications" in text
    assert "Four results qualify those rates" in text
    assert "Three results qualify those rates" not in text
    # And every numbered qualification exists.
    for n in (1, 2, 3, 4):
        assert f"**4.{n} " in text, n
    assert "**4.5 " not in text


def test_the_new_comparison_rows_have_references():
    text = PAPER.read_text(encoding="utf-8")
    for key in ("@raemaekers2014", "@breaking-bad"):
        # Cited in the table and defined in the reference list.
        assert text.count(f"[{key}]") >= 2, key
    assert "14.5%" in text
    assert "16.6%" in text


def test_pypi_is_still_the_lowest_row_in_the_comparison_table():
    """Two rows were added to that table; the sentence after it makes a claim about it."""
    data = _measured()
    mine = data["semver_compliance"]["compliance_rate_excluding_0x"]["rate"]["rate"]
    published = [s["rate"] for s in data["semver_compliance"]["published"]]
    assert all(mine < other for other in published), (mine, published)
    text = PAPER.read_text(encoding="utf-8")
    assert "lowest in the table" in text
    assert "lowest of the four" not in text
