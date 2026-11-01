"""The README's census section agrees with the files it was written from.

Every figure in that section comes from one of three places - docs/census-summary.json,
the table scripts/census_table.py computes, and docs/census-recheck.json - and this checks
each against its source rather than against the last time anyone looked.

The section is read from the committed ledger where it can be, because that is the file
anyone else can recompute from: docs/pair-census.json is 4.3 MB and gitignored, and a test
that needs it would pass here and skip everywhere else.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SUMMARY = ROOT / "docs" / "census-summary.json"
LEDGER = ROOT / "docs" / "census-rows.json.gz"
RECHECK = ROOT / "docs" / "census-recheck.json"

pytestmark = pytest.mark.skipif(
    not (README.exists() and SUMMARY.exists() and LEDGER.exists()),
    reason="README.md and docs/ are not shipped with the tests",
)

HEADING = "### 2,937 upgrade pairs: how often does a patch release break public API?"
NEXT_HEADING = "### 22 upgrade pairs"


@pytest.fixture(scope="module")
def section() -> str:
    """Only the census section. The 27-pair section below it quotes different numbers
    for the same cohorts, and a guard that scanned the whole README would read a patch
    rate of 15% from the page it corrects."""
    text = README.read_text(encoding="utf-8")
    assert text.count(HEADING) == 1, "the census heading moved"
    assert text.count(NEXT_HEADING) == 1
    body = text.split(HEADING, 1)[1]
    assert NEXT_HEADING in body, "the 27-pair section is no longer below the census"
    return body.split(NEXT_HEADING, 1)[0]


@pytest.fixture(scope="module")
def summary() -> dict:
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def table() -> dict:
    """The per-cohort table, computed from the committed ledger by the real script."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from census_table import summarise  # noqa: PLC0415

    with gzip.open(LEDGER, "rb") as fh:
        rows = json.loads(fh.read().decode("utf-8"))["rows"]
    return summarise(rows)


def test_every_cohort_row_matches_the_ledger(section, table):
    """Pairs, breaking count, rate, symbols and denominator, for all six cohorts."""
    checked = 0
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 6:
            continue
        cohort = cells[0].strip("*").strip()
        if cohort not in table:
            continue
        v = table[cohort]
        pairs = int(cells[1].replace(",", ""))
        assert pairs == v["pairs"], f"{cohort}: README {pairs} pairs, ledger {v['pairs']}"

        broke = re.match(r"\*{0,2}([\d,]+) \(([\d.]+)%\)", cells[2])
        assert broke, f"{cohort}: cannot read the breaking cell {cells[2]!r}"
        assert int(broke.group(1).replace(",", "")) == v["broke"]
        assert abs(float(broke.group(2)) - v["broke_rate"] * 100) < 0.05, (
            f"{cohort}: README {broke.group(2)}%, ledger {v['broke_rate'] * 100:.2f}%"
        )

        assert int(cells[3].replace(",", "")) == v["broken_symbols"]
        of = re.match(r"([\d,]+) of ([\d,]+) \(([\d.]+)%\)", cells[4])
        assert of, f"{cohort}: cannot read the symbol cell {cells[4]!r}"
        assert int(of.group(1).replace(",", "")) == v["broken_symbols"]
        assert int(of.group(2).replace(",", "")) == v["exported_symbols"]
        assert abs(float(of.group(3)) - v["symbol_rate"] * 100) < 0.005

        share = re.match(r"([\d]+)%", cells[5])
        assert share, f"{cohort}: cannot read the share cell {cells[5]!r}"
        assert abs(int(share.group(1)) - v["largest_pair_share"] * 100) < 0.5, (
            f"{cohort}: README says the largest pair is {share.group(1)}%, "
            f"the ledger says {v['largest_pair_share'] * 100:.0f}%"
        )
        checked += 1
    assert checked == 6, f"only {checked} cohort rows checked"


def test_the_named_largest_pairs_are_the_largest(section, table):
    """Two pairs are named in the prose; both have to be the cohort's biggest."""
    assert table["patch"]["largest_pair"] == "azure-mgmt-resource 23.1.0->23.1.1"
    assert "`azure-mgmt-resource` 23.1.0 -> 23.1.1 is 49%" in section
    assert table["0.x-breaking"]["largest_pair"] == "meson 0.63.3->0.64.0"
    assert "`meson` 0.63.3 -> 0.64.0 is 64%" in section


def test_the_totals_and_the_deductions_match_the_summary(section, summary):
    assert "**3,777 pairs were walked**" in section
    assert (
        summary["rows"]
        + summary["dropped_not_an_upgrade"]
        + summary["dropped_second_pair_for_a_package"]
        == 3777
    )
    assert f"{summary['dropped_not_an_upgrade']} are dropped because" in section
    assert f"{summary['dropped_second_pair_for_a_package']} because a sweep resumed" in section
    unmeasured = summary["rows"] - summary["measured"]
    assert (
        f"**{unmeasured} could\nnot be measured**" in section
        or f"**{unmeasured} could not be measured**" in section
    )


def test_the_headline_counts_are_the_summarys(section, summary):
    assert f"{summary['measured']:,} upgrade pairs" in README.read_text(encoding="utf-8")
    assert f"**{summary['packages_in_the_rows']:,} package names**" in section


def test_the_section_says_how_many_rows_lack_a_revision(section, summary):
    """The provenance caveat is a number, so it is checked like the others."""
    unstamped = summary["revisions"].get("not recorded", 0)
    assert unstamped, "every row now carries a revision - the caveat should be rewritten"
    assert (
        f"**The sweep spans revisions of this tool, and {unstamped:,} of its "
        f"{summary['rows']:,} rows do not record which.**" in section
    )


def test_the_recheck_numbers_are_the_rechecks(section):
    """The re-check table, and the claim that its zero was proved sensitive."""
    if not RECHECK.exists():
        pytest.skip("docs/census-recheck.json is not shipped")
    recheck = json.loads(RECHECK.read_text(encoding="utf-8"))
    assert f"| pairs re-measured | {recheck['re_measured']} |" in section
    assert (
        f"| verdicts that changed (would move a rate) | **{recheck['verdict_flips']}** |"
    ) in section
    assert (
        "| exported symbols that moved without changing a verdict | "
        f"**{recheck['exported_symbol_changes_without_a_flip']}** |"
    ) in section
    assert (
        f"| rows where only internal symbols moved | {recheck['internal_only_changes']} |"
    ) in section


def test_the_two_internal_differences_are_the_ones_described(section):
    if not RECHECK.exists():
        pytest.skip("docs/census-recheck.json is not shipped")
    recheck = json.loads(RECHECK.read_text(encoding="utf-8"))
    internal = [
        c
        for c in recheck["changed"]
        if not c["verdict_flipped"] and not c["exported_symbols_moved"]
    ]
    assert len(internal) == 2
    pairs = {c["pair"].split(" (")[0] for c in internal}
    assert pairs == {"pyiceberg 0.11.1->0.12.0", "aws-lambda-powertools 2.43.1->3.0.0"}
    for name in ("pyiceberg", "aws-lambda-powertools", "REST_SCAN_PLANNING_ENABLED"):
        assert name in section

    # The prose spells these counts as words, so the count becomes the word the prose
    # would have to use. Written as `f"... {n} ..." or "... four ..."`, the computed half
    # of the assertion never ran: the literal matched whatever n was.
    words = {
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        13: "thirteen",
        14: "fourteen",
        15: "fifteen",
    }
    by_pair = {c["pair"].split(" (")[0]: c for c in internal}
    ice = by_pair["pyiceberg 0.11.1->0.12.0"]["internal_symbols_moved"]["internal_gone"]
    alp = by_pair["aws-lambda-powertools 2.43.1->3.0.0"]["internal_symbols_moved"]["internal_gone"]
    more, fewer = len(ice["gained"]), len(alp["lost"])
    assert more in words and fewer in words, f"no word for {more} or {fewer}"
    assert f"loses {words[more]} more" in section, (
        f"the re-check says pyiceberg loses {more} more, and the README does not say so"
    )
    assert f"loses {words[fewer]} fewer" in section, (
        f"the re-check says aws-lambda-powertools loses {fewer} fewer, "
        "and the README does not say so"
    )
    assert not ice["lost"], "pyiceberg also lost names; the sentence describes only gains"
    assert not alp["gained"], "aws-lambda-powertools also gained names"


def test_the_scramp_example_is_a_patch_release_and_reads_right(section):
    """The example for 'exported understates' has to be a patch release."""
    assert (
        "`scramp`\n1.4.18, a **patch** release" in section
        or "`scramp` 1.4.18, a **patch** release" in section
    )
    for name in ("MECHANISMS", "CHANNEL_TYPES", "MAX_ITERATION_COUNT"):
        assert name in section
    assert "declares no `__all__`" in section


def test_the_section_says_the_old_page_is_corrected(section):
    assert "which put patch at 15%" in section
    assert "27 hand-chosen pairs" in section


def test_the_rebuild_command_names_a_file_that_is_committed(section):
    assert (
        "scripts/census_summary.py --census\ndocs/census-rows.json.gz" in section
        or "scripts/census_summary.py --census docs/census-rows.json.gz" in section
    )
    assert LEDGER.exists(), "the README offers a command against a file that is not here"


def test_the_decoded_body_claim_holds(section):
    """A property cannot reach a surface, which is the point of the sentence.

    Checked against the code rather than quoted from a run: `walk` describes a class
    member only when `inspect.isroutine` is true of it, and that is false of a property.
    """
    assert "`decoded_body` is a `@property`" in section
    probe = (ROOT / "src" / "blast_radius" / "probe.py").read_text(encoding="utf-8")
    assert "if inspect.isroutine(m) and owned_by(m):" in probe, (
        "the class-member filter changed; a property may now reach a surface and the "
        "README's claim about decoded_body would no longer be true"
    )


# --- the title and the abstract quote the ledger too ---------------------------------


def _measured_rows() -> list[dict]:
    import gzip
    import json as _json
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    import census_summary as cs  # noqa: PLC0415

    with gzip.open(ROOT / "docs" / "census-rows.json.gz", "rt", encoding="utf-8") as fh:
        raw = _json.loads(fh.read())
    rows = raw["rows"] if isinstance(raw, dict) else raw
    rows = [r for r in rows if cs.is_upgrade(r["old"], r["new"])]
    rows, _ = cs.one_pair_per_package(rows)
    return [r for r in rows if r.get("module")]


def test_the_title_and_abstract_quote_the_measured_pair_count() -> None:
    """The place a withdrawn number survived longest.

    Section 6 records that an earlier draft described 2,997 pairs where the rest of the
    paper describes 2,937, and says every figure in section 4 was regenerated. The
    title, the abstract and contribution 2 were not, so the paper was called
    *A 2,997-Pair Census* while section 6 explained that 2,997 was wrong. Nothing read
    the title.
    """
    paper = (ROOT / "paper" / "main.md").read_text(encoding="utf-8")
    measured = len(_measured_rows())
    assert measured > 0
    shown = f"{measured:,}"

    title = next(ln for ln in paper.splitlines() if ln.startswith("title:"))
    assert shown in title, f"the title says {title!r}, the ledger measures {shown} pairs"

    abstract = paper[paper.index("# Abstract") : paper.index("# 1. Introduction")]
    assert shown in abstract, f"the abstract does not quote {shown}"

    stale = {"2,997", "2997"}
    for name, body in (("title", title), ("abstract", abstract)):
        for wrong in stale:
            assert wrong not in body, f"the {name} still carries the withdrawn {wrong}"


def test_the_abstract_quotes_the_distinct_package_count() -> None:
    """1,204 against the ledger's 1,202: wrong by two, in the abstract, unguarded."""
    paper = (ROOT / "paper" / "main.md").read_text(encoding="utf-8")
    packages = len({r["package"] for r in _measured_rows()})
    abstract = paper[paper.index("# Abstract") : paper.index("# 1. Introduction")]
    assert f"{packages:,} distinct PyPI packages" in abstract, (
        f"the ledger holds {packages:,} distinct packages among measured pairs"
    )


def test_the_cross_ecosystem_table_states_our_own_non_major_rate_correctly() -> None:
    """Section 3.1 compares PyPI with three other ecosystems; recompute our row.

    A comparison table is where a number is most tempting to round in the direction
    that makes the comparison cleaner.
    """
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    import census_summary as cs  # noqa: PLC0415

    # cs.BREAKING, not a predicate retyped here. Section 6 records what happened the
    # last time this repository reproduced the summariser's filters instead of
    # importing them: a table of figures over 2,997 pairs that the rest of the paper
    # did not recognise. `exported_widened` is not breaking - adding an accepted
    # argument does not break a caller - and a hand-written predicate that included it
    # put this rate at 14.3% instead of 8.2%.
    rows = _measured_rows()
    non_major = [r for r in rows if r["cohort"] in {"patch", "minor"}]
    broke = [r for r in non_major if any(r.get(k) for k in cs.BREAKING)]
    rate = len(broke) / len(non_major)
    paper = (ROOT / "paper" / "main.md").read_text(encoding="utf-8")
    section = paper[paper.index("## 3.1 The same question") : paper.index("# 4. ")]
    assert f"**{rate * 100:.1f}%**" in section, (
        f"the non-major rate is {rate * 100:.1f}% over {len(non_major)} pairs"
    )


def test_the_cross_ecosystem_row_describes_the_predicate_it_used() -> None:
    """The row said "changing an exported name", which includes widening. It does not.

    `census_summary.BREAKING` is ('exported_gone', 'exported_reshaped'); a signature
    that only grew is explicitly not breaking, because every existing call still binds.
    The sentence explaining what the number means described a different number - and a
    hand-written predicate built from that description put the rate at 14.3% instead of
    8.2% when this test was first written, which is how the error was found.
    """
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    import census_summary as cs  # noqa: PLC0415

    paper = (ROOT / "paper" / "main.md").read_text(encoding="utf-8")
    section = paper[paper.index("## 3.1 The same question") : paper.index("# 4. ")]
    assert "widened" in section, "the section must say what it excludes, not only what it counts"
    assert "exported_widened" not in cs.BREAKING
    for field in cs.BREAKING:
        word = field.removeprefix("exported_")
        assert f"`{word}`" in section, (
            f"the section does not name `{word}`, which is one of the two conditions "
            f"its rate counts"
        )


def test_no_latex_command_lost_its_backslash() -> None:
    """A heredoc ate the backslash out of `\times` twice in this set of papers.

    It renders as a tab followed by `imes`, which is invisible in a diff and survives
    every numeric check, because no number changed.
    """
    paper = (ROOT / "paper" / "main.md").read_text(encoding="utf-8")
    for fragment in ("imes10", "ext{", "rac{", "imes "):
        assert "\t" + fragment not in paper, (
            f"a tab precedes {fragment!r}: a LaTeX command lost its backslash"
        )
    assert "\t" not in paper, "a literal tab in the manuscript is almost always an eaten escape"
