"""Section 4.5's figures, against `docs/caller-sweep.json`.

The section's claim is that §4.4's model points the right way and overstates by a fifth, and
there are three ways a later edit could make that sentence say something it has not earned:

  * by comparing the measured rate against §4.4's *published* 0.58%-3.45%, which is an
    average over a different population at a different import count. The comparable number
    is the model evaluated on these clients, and the test below checks the section quotes
    that one.
  * by quoting one tier of the matcher. The exact and enclosing tiers are the two defensible
    readings of the same observations and the model's prediction lies between them, so a
    section quoting either end alone would be choosing its own answer.
  * by dropping the clients that import the module and name nothing in it. They cannot be
    broken by a name going away, and removing them from the denominator would raise every
    rate for a reason that is not about the ecosystem.

The network is not exercised here. What is exercised is that the published figures are the
artefact's and that the prose carries the qualifications the numbers need.
"""

from __future__ import annotations

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MEASURED = ROOT / "docs" / "caller-sweep.json"
PAPER = ROOT / "paper" / "main.md"

# As section 4.5 publishes them.
PUBLISHED_OBSERVATIONS = 175
PUBLISHED_USERS = 136
PUBLISHED_NON_USERS = 39
PUBLISHED_PAIRS = 7
PUBLISHED_INDEX = 4_427
PUBLISHED_EXACT = 0.2941
PUBLISHED_ENCLOSING = 0.6029
PUBLISHED_LOOSE = 0.6691
PUBLISHED_MODEL = 0.3721
PUBLISHED_RATIO = 0.79
PUBLISHED_MEDIAN_K = 7


def _measured() -> dict:
    if not MEASURED.exists():
        pytest.skip("docs/caller-sweep.json is not present")
    return json.loads(MEASURED.read_text(encoding="utf-8"))


def _summary() -> dict:
    payload = _measured()
    if "summary" not in payload:
        pytest.skip("caller-sweep.json predates the summary")
    return payload["summary"]


def _repo_file(path: pathlib.Path) -> str:
    if not path.is_file():
        pytest.skip(f"{path.name} is not present - running against an installed wheel")
    return path.read_text(encoding="utf-8")


def _section() -> str:
    text = _repo_file(PAPER)
    start = text.index("**4.5 Measured against real clients")
    return " ".join(text[start : text.index("\n# 5. ", start)].split())


# ------------------------------------------------------------------ what was measured


def test_the_sample_is_the_one_the_section_describes():
    payload = _measured()
    summary = _summary()
    assert payload["source"] == "semver-sweep"
    assert payload["dependents_per_pair"] == 25
    assert payload["top_packages_indexed"] == 2_000
    assert summary["pairs_with_a_surface"] == PUBLISHED_PAIRS
    assert summary["observations"] == PUBLISHED_OBSERVATIONS


def test_the_clients_that_name_nothing_stay_in_the_denominator():
    summary = _summary()
    assert summary["observations_where_the_client_uses_any_name"] == PUBLISHED_USERS
    assert summary["observations_where_the_client_imports_but_uses_nothing"] == PUBLISHED_NON_USERS
    assert summary["observations"] == PUBLISHED_USERS + PUBLISHED_NON_USERS
    # Both denominators are reported, so neither can be the silent one.
    for tier in ("exact", "enclosing", "loose"):
        assert f"{tier}_rate_over_observations" in summary
        assert f"{tier}_rate_among_clients_that_use_a_name" in summary


def test_the_three_tiers_are_the_published_ones_and_they_nest():
    summary = _summary()
    exact = summary["exact_rate_among_clients_that_use_a_name"]
    enclosing = summary["enclosing_rate_among_clients_that_use_a_name"]
    loose = summary["loose_rate_among_clients_that_use_a_name"]
    assert exact == pytest.approx(PUBLISHED_EXACT, abs=5e-5)
    assert enclosing == pytest.approx(PUBLISHED_ENCLOSING, abs=5e-5)
    assert loose == pytest.approx(PUBLISHED_LOOSE, abs=5e-5)
    assert exact <= enclosing <= loose


def test_the_model_is_evaluated_on_these_clients_rather_than_quoted_from_the_table():
    model = _summary()["model_under_uniform_choice"]
    assert model["mean_predicted_rate"] == pytest.approx(PUBLISHED_MODEL, abs=5e-5)
    assert model["measured_exact_rate"] == pytest.approx(PUBLISHED_EXACT, abs=5e-5)
    assert model["ratio_measured_over_predicted"] == pytest.approx(PUBLISHED_RATIO, abs=5e-3)
    assert model["direction"] == "overstates"
    # Every client the model could be asked about was asked about.
    assert model["observations_the_model_can_be_asked_about"] == PUBLISHED_USERS
    assert model["observations_too_small_for_the_model"] == 0


def test_the_model_prediction_lies_between_the_two_defensible_tiers():
    """So the measurement bounds the model instead of replacing it."""
    summary = _summary()
    model = summary["model_under_uniform_choice"]["mean_predicted_rate"]
    assert summary["exact_rate_among_clients_that_use_a_name"] < model
    assert model < summary["enclosing_rate_among_clients_that_use_a_name"]


def test_the_clients_import_more_than_one_name_each():
    """The table's k=1 row is the one a reader quotes, and it is not these clients."""
    import statistics

    ks = [
        seen["names_used"]
        for row in _measured()["rows"]
        for seen in row.get("observations", [])
        if seen.get("names_used")
    ]
    assert len(ks) == PUBLISHED_USERS
    assert statistics.median(ks) == PUBLISHED_MEDIAN_K
    assert min(ks) >= 1
    assert max(ks) > 10


def test_the_tool_only_package_reaches_none_of_its_dependents():
    """coverage is depended on as a tool, not as an API, and the rows show it."""
    rows = {r["package"]: r for r in _measured()["rows"] if r["package"] == "coverage"}
    row = rows["coverage"]
    assert row["cohort"] == "patch"
    assert len(row["broken_names"]) == 1
    assert row["observations"], "no dependents were read"
    assert all(seen.get("names_used") == 0 for seen in row["observations"] if "skipped" not in seen)


def test_every_pair_had_dependents_read_for_it():
    for row in _measured()["rows"]:
        read = [s for s in row.get("observations", []) if "skipped" not in s]
        assert read, row["package"]
        assert "error" not in row, row.get("error")


# ------------------------------------------------------------------------- the prose


def test_every_figure_in_the_section_comes_from_the_artefact():
    summary = _summary()
    model = summary["model_under_uniform_choice"]
    section = _section()
    for value in (
        str(summary["observations"]),
        str(summary["observations_where_the_client_uses_any_name"]),
        str(summary["observations_where_the_client_imports_but_uses_nothing"]),
        f"{summary['exact_rate_among_clients_that_use_a_name']:.4f}",
        f"{summary['enclosing_rate_among_clients_that_use_a_name']:.4f}",
        f"{summary['loose_rate_among_clients_that_use_a_name']:.4f}",
        f"{model['mean_predicted_rate']:.4f}",
        f"{model['ratio_measured_over_predicted']:.2f}",
        f"{PUBLISHED_INDEX:,}",
    ):
        assert value in section, value


def test_the_section_refuses_the_comparison_a_reader_would_make():
    """29% against 3.45% is two different questions, and the section says so."""
    section = _section()
    assert "3.45%" in section
    assert "different question" in section or "two different questions" in section
    assert "91%" in section, "the share of promising pairs that broke nothing is the reason"


def test_the_section_states_that_the_sample_is_hand_picked():
    section = _section()
    assert "hand" in section
    assert "not" in section and "ecosystem" in section


def test_the_section_states_the_static_import_limitation_and_its_direction():
    """A limitation whose direction is unstated is half a limitation."""
    section = _section()
    assert "static" in section
    assert "never reaches" in section or "never reached" in section
    assert "conservative" in section


def test_section_4_4_no_longer_says_nothing_here_looks_at_a_client():
    text = _repo_file(PAPER)
    assert "because nothing here looks\nat a client" not in text
    start = text.index("**4.4 A caller is not a package")
    body = " ".join(text[start : text.index("**4.5 ", start)].split())
    assert "4.5" in body, "4.4 has to point at the section that measured its estimate"
    assert "0.79" in body


def test_the_section_count_in_the_heading_matches_the_sections():
    text = _repo_file(PAPER)
    assert "# 4. Five qualifications" in text
    for n in range(1, 6):
        assert f"**4.{n} " in text, n
    assert "**4.6 " not in text
