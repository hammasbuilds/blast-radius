"""Every pair the census measures must be an upgrade, and its rates must say over what.

`pairs_for` walks releases ordered by UPLOAD DATE and pairs each with the next. That is
not the same as ordering by version: a project that publishes a backport after a newer
release yields a pair that runs backwards. 21 of 2,238 pairs in one census did, and five
were published as upgrades that removed an exported name - including
`snowflake-connector-python 5.1.0 -> 4.8.1` and `dulwich 1.2.19 -> 1.2.18`, where the
"removed" symbol was one the newer release had added.

The label could not catch it. `classify` compares version components for equality to
find which position moved, so 5.1.0 -> 4.8.1 is a well-formed "major" pair to it, exactly
like 4.8.1 -> 5.1.0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from semver_sweep import classify, is_upgrade, version_key  # noqa: E402

CENSUS = ROOT / "docs" / "pair-census.json"
SUMMARY = ROOT / "docs" / "census-summary.json"


def test_is_upgrade_reads_direction_where_classify_cannot() -> None:
    """Both directions, and the case classify is blind to."""
    assert is_upgrade("1.2.18", "1.2.19")
    assert not is_upgrade("1.2.19", "1.2.18")
    assert not is_upgrade("5.1.0", "4.8.1")
    # classify gives the same label either way round, which is why a direction check has
    # to be separate rather than folded into it.
    assert classify("5.1.0", "4.8.1") == classify("4.8.1", "5.1.0") == "major"


def test_a_fourth_component_is_not_ignored_when_ordering() -> None:
    """classify reads three components; ordering must read them all.

    1.2.19 and 1.2.19.1 agree on the first three, so a three-component key would call
    them equal and let the pair through in either direction.
    """
    assert is_upgrade("1.2.19", "1.2.19.1")
    assert not is_upgrade("1.2.19.1", "1.2.19")
    assert version_key("1.2.19") < version_key("1.2.19.1")


def test_a_pre_release_sorts_before_the_release_it_precedes() -> None:
    """Both of the two `version_key` definitions this module used to carry got this
    wrong, in opposite ways, and both made `is_upgrade("1.0rc1", "1.0")` false - which
    drops a genuine upgrade as if it ran backwards.

    One truncated `1.0rc1` to (1, 0) and called it equal to `1.0`; the other made the
    `0rc1` chunk sort after every number and called `1.0rc1` the later release. Pinned
    in both directions, because a key that ordered them equal passes a one-sided check.
    """
    assert version_key("1.0rc1") < version_key("1.0")
    assert version_key("1.0.dev1") < version_key("1.0")
    assert version_key("1.0b2") < version_key("1.0rc1")
    assert is_upgrade("1.0rc1", "1.0")
    assert not is_upgrade("1.0", "1.0rc1")
    assert not is_upgrade("1.0", "1.0")


def test_there_is_exactly_one_version_key() -> None:
    """The defect was a second definition shadowing the first, which no test could see.

    Counted in the source, because importing the module gives whichever one survived and
    cannot report that another was overwritten on the way.
    """
    import re  # noqa: PLC0415

    source = (ROOT / "scripts" / "semver_sweep.py").read_text(encoding="utf-8")
    found = re.findall(r"^def (version_key|is_upgrade|classify)\(", source, re.M)
    assert sorted(found) == ["classify", "is_upgrade", "version_key"], (
        f"expected one definition of each, found {found}"
    )


def test_no_measured_pair_in_the_census_runs_backwards() -> None:
    """The census on disk, after the summariser's filter, holds only real upgrades."""
    if not (CENSUS.exists() and SUMMARY.exists()):
        pytest.skip("the census has not been run in this checkout")
    import census_summary

    census = json.loads(CENSUS.read_text(encoding="utf-8"))
    kept = [r for r in census["results"] if is_upgrade(r["old"], r["new"])]
    summary = census_summary.summarise(census)
    # Two filters, not one: backwards pairs, and a second pair for a package already
    # represented in its cohort - which a run resumed over several days writes when PyPI
    # moves under it. This asserted `rows == len(kept)` and so failed the day the second
    # filter arrived, which is the test doing its job; both deductions are published, and
    # that is what makes this arithmetic checkable.
    assert summary["rows"] == len(kept) - summary["dropped_second_pair_for_a_package"], (
        "rows is not the row count after both of the summary's own deductions"
    )
    assert summary["dropped_not_an_upgrade"] == len(census["results"]) - len(kept)
    assert len(summary["second_pairs"]) == summary["dropped_second_pair_for_a_package"]


def test_every_published_share_states_its_denominator() -> None:
    """A cohort that measured nothing has no rate, and must not report 0%.

    `broke / measured` with measured == 0 is the shape that publishes "0% of these
    upgrades break anything" about a cohort nothing was measured in.
    """
    if not SUMMARY.exists():
        pytest.skip("docs/census-summary.json has not been generated")
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    for name, cohort in summary["cohorts"].items():
        if cohort["measured"] == 0:
            assert cohort["broke_exported_share"] is None, (
                f"cohort {name} measured nothing but publishes a rate"
            )
        else:
            assert cohort["broke_exported_share"] == pytest.approx(
                cohort["broke_exported"] / cohort["measured"], abs=5e-5
            ), f"cohort {name}'s share is not broke_exported / measured"
        assert cohort["measured"] <= cohort["pairs"]
