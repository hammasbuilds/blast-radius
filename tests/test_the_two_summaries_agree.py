"""The census is published twice, and the two publications must say the same thing.

`docs/census-summary.json` feeds the README; `research/results/rate_by_cohort.json` and
its siblings feed `paper/main.md`. Both are reduced from the same committed pair rows by
different code, and nothing compared them to each other - only each to the rows, and only
in places.

They agree today. The reason to assert it is that a sibling repository in this set had the
same shape and did not: one summary counted 63 packages in a stage where the other counted
67, because the second grouped exclusion reasons by substring and `memory budget exceeded`
contains `budget exceeded`; and one reported a median of 0.8481 where the other reported
0.8500, because one used `statistics.median` and the other a nearest-rank quantile. Both
were found by reading the two files side by side. Here that comparison runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUMMARY = ROOT / "docs" / "census-summary.json"
BY_COHORT = ROOT / "research" / "results" / "rate_by_cohort.json"

pytestmark = pytest.mark.skipif(
    not (SUMMARY.exists() and BY_COHORT.exists()),
    reason="the census summary or the research tree is not shipped with the tests",
)


@pytest.fixture(scope="module")
def summary() -> dict:
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def by_cohort() -> dict:
    return json.loads(BY_COHORT.read_text(encoding="utf-8"))


def test_the_two_files_describe_the_same_cohorts(summary, by_cohort):
    assert set(summary["cohorts"]) == set(by_cohort), (
        sorted(summary["cohorts"]),
        sorted(by_cohort),
    )


def test_each_cohort_has_the_same_denominator_and_numerator(summary, by_cohort):
    """A rate is a pair of counts. Checking the rate alone lets both halves drift."""
    for cohort, body in summary["cohorts"].items():
        other = by_cohort[cohort]
        assert body["measured"] == other["n"], (
            f"{cohort}: census-summary.json measured {body['measured']}, "
            f"rate_by_cohort.json n={other['n']}"
        )
        assert body["broke_exported"] == other["successes"], (
            f"{cohort}: census-summary.json broke_exported {body['broke_exported']}, "
            f"rate_by_cohort.json successes={other['successes']}"
        )


def test_each_cohort_rate_is_its_own_two_counts(summary, by_cohort):
    for cohort, other in by_cohort.items():
        assert other["n"] > 0, cohort
        assert abs(other["successes"] / other["n"] - other["rate"]) < 5e-5, cohort
        body = summary["cohorts"][cohort]
        assert abs(body["broke_exported"] / body["measured"] - body["broke_exported_share"]) < 5e-5


def test_the_cohorts_partition_the_measured_pairs(summary, by_cohort):
    """Every measured pair is in exactly one cohort, so the parts must sum to the whole.

    If they did not, the per-cohort rates and the headline rate would be over different
    samples while being printed in the same table.
    """
    assert sum(b["measured"] for b in summary["cohorts"].values()) == summary["measured"]
    assert (
        sum(b["broke_exported"] for b in summary["cohorts"].values()) == summary["broke_exported"]
    )
    assert sum(o["n"] for o in by_cohort.values()) == summary["measured"]
    assert sum(o["successes"] for o in by_cohort.values()) == summary["broke_exported"]


def test_the_headline_rate_is_its_own_two_counts(summary):
    rate = summary["broke_exported"] / summary["measured"]
    assert abs(rate - summary["broke_exported_share"]) < 5e-5, (
        rate,
        summary["broke_exported_share"],
    )


def test_every_cohort_interval_contains_its_own_rate(by_cohort):
    """A Wilson interval that does not bracket its point estimate is a computation error."""
    for cohort, other in by_cohort.items():
        assert other["lo"] <= other["rate"] <= other["hi"], (cohort, other)
        assert other["lo"] >= 0.0 and other["hi"] <= 1.0, (cohort, other)
