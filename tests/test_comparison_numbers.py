"""docs/COMPARISON.md agrees with docs/griffe-comparison.json.

The comparison against griffe is the only document here that quotes another tool's
output, and the one with the most room to flatter this one. So every figure in it is
checked against the artefact the comparison script wrote, and the two claims that are
about griffe rather than about numbers - that 12 of its 13 urllib3 findings are one
inserted parameter, and that all 15 of its click base-class removals are the word
`object` - are checked against what the script recorded from the installed source.

What is not checked here: whether griffe's findings are right. Its column is its own
output, and the document says so.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "COMPARISON.md"
ARTEFACT = ROOT / "docs" / "griffe-comparison.json"

pytestmark = pytest.mark.skipif(
    not (DOC.exists() and ARTEFACT.exists()),
    reason="docs/ is not shipped with the tests",
)


@pytest.fixture(scope="module")
def artefact() -> dict:
    return json.loads(ARTEFACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8")


def griffe_run(artefact: dict, package: str) -> dict:
    return next(r for r in artefact["griffe"]["runs"] if r["package"] == package)


def br_run(artefact: dict, label: str) -> dict:
    return artefact["blast_radius"]["runs"][label]


def test_the_versions_the_document_names_are_the_versions_that_ran(artefact, doc):
    assert f"griffe {artefact['griffe']['version']}" in doc
    assert f"blast-radius {artefact['blast_radius']['version']}" in doc
    for package in ("click", "urllib3"):
        run = griffe_run(artefact, package)
        assert f"`{package}` {run['old']} -> {run['new']}" in doc


def test_griffes_finding_counts_are_the_ones_it_reported(artefact, doc):
    """The two totals, and the object counts beside them."""
    click, urllib3 = griffe_run(artefact, "click"), griffe_run(artefact, "urllib3")
    assert f"| findings | **{click['breakages']}** |" in doc
    assert f"| findings | **{urllib3['breakages']}** |" in doc
    assert f"| distinct objects they are reported on | {click['distinct_owners']} |" in doc
    assert f"| distinct objects they are reported on | **{urllib3['distinct_owners']}**" in doc


def test_the_kind_counts_quoted_in_the_prose_are_griffes_own(artefact, doc):
    click = griffe_run(artefact, "click")
    moved = click["by_kind"]["Positional parameter was moved"]
    removed = click["by_kind"]["Public object was removed"]
    bases = click["by_kind"]["Base class was removed"]
    assert f"{moved} of griffe's {click['breakages']}" in doc
    assert f"| public objects reported removed | **{removed}** |" in doc
    assert f"**{bases} are *base class was removed*" in doc


def test_all_the_click_base_class_removals_are_the_word_object(artefact, doc):
    """The document's sharpest claim about griffe, held to what was read from the source.

    Checked by hand first, which is a check nothing re-runs: the install directories are
    gitignored, so the comparison script now records each removed base list and this reads
    that. If click's source is ever re-read and one of them is not `object`, the sentence
    is wrong and this fails.
    """
    summary = griffe_run(artefact, "click")["base_class_removals"]
    assert summary["bases_unreadable"] == 0, (
        f"{summary['bases_unreadable']} class line(s) could not be found in click 7.1.2, "
        "so 'all 15 are object' rests on rows nothing read"
    )
    assert summary["bases_object_only"] == summary["rows"]
    assert f"all {summary['rows']} are the word `object`" in doc

    rows = [
        r for r in griffe_run(artefact, "click")["rows"] if r["kind"] == "Base class was removed"
    ]
    assert len(rows) == summary["rows"]
    assert {r["bases_in_old_version"] for r in rows} == {"object"}


def test_the_twelve_shifted_parameters_are_one_change_at_one_object(artefact, doc):
    """`version_string` inserted once, reported twelve times.

    The document's claim is that old and new read the same in those rows, because only
    the index moved. That is what makes the 13-to-3 difference a difference of grain.
    """
    rows = [
        r
        for r in griffe_run(artefact, "urllib3")["rows"]
        if r["kind"] == "Positional parameter was moved"
    ]
    assert len(rows) == 12
    assert {r["owner"] for r in rows} == {"urllib3.response.HTTPResponse"}
    assert "One insertion, counted once per parameter it" in doc
    assert "shifted" in doc


def test_this_tools_counts_are_the_ones_it_reported(artefact, doc):
    click = br_run(artefact, "click 7.1.2 -> 8.1.7")["counts"]
    assert (
        f"| findings | **{griffe_run(artefact, 'click')['breakages']}** | "
        f"{click['gone']} gone, {click['reshaped']} reshaped, {click['widened']} "
        f"widened, {click['silent']} silent, {click['added']} added |"
    ) in doc
    assert f"| public objects reported removed | **{28}** | **{click['gone']}** |" in doc
    assert f"| behaviour changes | not checked | **{click['silent']}** |" in doc

    urllib3 = br_run(artefact, "urllib3 2.2.1 -> 2.2.2")["counts"]
    breaking = urllib3["gone"] + urllib3["reshaped"] + urllib3["silent"]
    assert (
        f"{breaking} breaking ({urllib3['reshaped']} reshaped, {urllib3['silent']} "
        f"silent), {urllib3['added']} added"
    ) in doc


def test_the_four_constants_are_named_and_were_really_removed(artefact, doc):
    """Each constant the document tabulates is a removal griffe reported."""
    removed = {
        r["path"]
        for r in griffe_run(artefact, "click")["rows"]
        if r["kind"] == "Public object was removed"
    }
    for name in (
        "SUBCOMMAND_METAVAR",
        "SUBCOMMANDS_METAVAR",
        "DEPRECATED_HELP_NOTICE",
        "DEPRECATED_INVOKE_NOTICE",
    ):
        assert f"`{name}`" in doc, f"{name} is not in the document's table"
        assert f"click.core.{name}" in removed, (
            f"the document says griffe reported click.core.{name} removed, "
            "and the artefact does not"
        )


def test_this_tool_now_finds_those_constants(artefact, doc):
    """The fix the comparison produced, checked against this tool's own output.

    Without this the document could claim the hole is closed while the run beside it
    shows otherwise.
    """
    gone = {
        c["qualname"]
        for c in br_run(artefact, "click 7.1.2 -> 8.1.7")["changes"]
        if c["kind"] == "gone"
    }
    for name in (
        "SUBCOMMAND_METAVAR",
        "SUBCOMMANDS_METAVAR",
        "DEPRECATED_HELP_NOTICE",
        "DEPRECATED_INVOKE_NOTICE",
    ):
        assert f"click.core.{name}" in gone
    assert "gone count moved\n12 -> 16" in doc or "12 -> 16" in doc


def test_the_five_re_exports_are_named_and_this_tool_reports_them(artefact, doc):
    gone = {
        c["qualname"]
        for c in br_run(artefact, "click 7.1.2 -> 8.1.7")["changes"]
        if c["kind"] == "gone"
    }
    for name in (
        "click.types.filename_to_ui",
        "click.types.get_streerror",
        "click.types.get_filesystem_encoding",
        "click.utils.is_bytes",
        "click.core.iteritems",
    ):
        assert f"`{name}`" in doc
        assert name in gone, f"the document says this tool reports {name} gone; it does not"
    reported_by_griffe = {r["path"] for r in griffe_run(artefact, "click")["rows"]}
    for name in ("click.types.filename_to_ui", "click.utils.is_bytes"):
        assert name not in reported_by_griffe, (
            f"the document says griffe does not list {name}, and the artefact has it"
        )


def test_the_silent_witness_in_the_document_is_the_one_measured(artefact, doc):
    """urllib3's behaviour change is quoted with its witness, so it has to match."""
    silent = [
        c for c in br_run(artefact, "urllib3 2.2.1 -> 2.2.2")["changes"] if c["kind"] == "silent"
    ]
    assert len(silent) == 1
    change = silent[0]
    assert change["qualname"] == "urllib3.HTTPResponse.read"
    assert change["qualname"] in doc
    witness = change["witness"]
    assert witness["old"] in doc, f"the document does not quote {witness['old']!r}"
    assert witness["new"] in doc, f"the document does not quote {witness['new']!r}"


def test_the_document_does_not_claim_griffe_is_slower(artefact, doc):
    """Seconds move run to run, so the document quotes the ordering and not the figures.

    This fails if a figure creeps back into the table, and if the ordering it claims ever
    stops holding.
    """
    assert "under a second" in doc
    assert "several times faster" in doc
    for package, label in (
        ("click", "click 7.1.2 -> 8.1.7"),
        ("urllib3", "urllib3 2.2.1 -> 2.2.2"),
    ):
        assert griffe_run(artefact, package)["seconds"] < br_run(artefact, label)["seconds"]
    table_rows = re.findall(r"^\| seconds \|.*$", doc, re.M)
    assert table_rows, "the seconds row is gone from the tables"
    for row in table_rows:
        assert not re.search(r"\d+\.\d+", row), (
            f"a measured time is pinned in the prose, and it will drift: {row}"
        )


def test_the_limits_section_still_names_what_was_not_audited(doc):
    """A comparison that drops its own caveats is the thing this document is wary of."""
    limits = doc.split("## What this does not show", 1)
    assert len(limits) == 2, "the limits section is gone"
    for phrase in ("were not individually audited", "No recall comparison", "Two packages"):
        assert phrase in limits[1], f"the limits no longer say: {phrase}"
