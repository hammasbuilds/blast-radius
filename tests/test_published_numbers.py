"""Every number the README and docs/ publish matches the committed run it came from.

The runs themselves need the network and minutes, so they are not re-run here; what is
checked is that the prose and the data agree. They had drifted: the semver major row said
32 symbols where the sweep gives 29, docs/SEMVER.md listed pytest 8.2.0 with 13 exported
symbols where the sweep reads 321, and the click sections described a run from before
the behaviour pass skipped functions by name.

Skipped where the docs are not next to the tests (the installed-wheel CI job).
"""

from __future__ import annotations

import json
import pathlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"

pytestmark = pytest.mark.skipif(
    not (README.exists() and (DOCS / "semver-sweep.json").exists()),
    reason="README.md and docs/ are not shipped with the tests",
)


def _load(name: str):
    return json.loads((DOCS / name).read_text(encoding="utf-8"))


def _broken(row: dict, scope: str = "exported") -> int:
    return len(row.get(f"{scope}_gone", [])) + len(row.get(f"{scope}_reshaped", []))


def _made_the_promise(row: dict) -> bool:
    """Did this release make the promise the study measures?

    SemVer 2.0.0 section 4: "Major version zero (0.y.z) is for initial development.
    Anything MAY change at any time." For a 0.x package the SECOND component is the
    breaking position, so a 0.26.0 -> 0.27.0 bump is not a minor release in the sense
    measured here. Encoded in the test so the exclusion cannot quietly lapse - it is the
    same reasoning the study already applies to calendar-versioned packages, and it was
    applied to them and not to these.
    """
    return not str(row.get("old", "")).startswith("0.")


def test_the_semver_summary_rows_are_the_sweep():
    rows = _load("semver-sweep.json")
    assert len(rows) == 27 and not [r for r in rows if "error" in r]
    counted = [r for r in rows if _made_the_promise(r)]
    assert len(counted) == 22, "the 0.x exclusion no longer selects 22 pairs"
    readme = README.read_text(encoding="utf-8")
    semver = (DOCS / "SEMVER.md").read_text(encoding="utf-8")
    for bump in ("patch", "minor", "major"):
        mine = [r for r in counted if r["bump"] == bump]
        broke = [r for r in mine if _broken(r)]
        symbols = sum(_broken(r) for r in mine)
        exported = sum(r.get("exported_symbols", 0) for r in mine)
        # "2 of 13 (15%)" for the two rows with enough pairs to carry a percentage, and a
        # bare "2 of 3" for the major row - a percentage over three observations is not an
        # estimate of anything.
        share = f"{len(broke)} of {len(mine)}"
        if len(mine) > 3:
            share += f" ({round(100 * len(broke) / len(mine))}%)"
        pattern = re.compile(
            rf"\|\s*\**{bump}\**\s*\|\s*{len(mine)}\s*\|\s*\**{re.escape(share)}\**\s*\|"
            rf"\s*{symbols}\s*\|\s*{symbols} of {exported:,}"
        )
        assert pattern.search(readme), f"README {bump} row"
        assert pattern.search(semver), f"SEMVER.md {bump} row"


def test_the_sweep_cannot_see_silent_and_both_documents_say_so():
    """The sweep runs --no-behaviour, so it measures API surface only.

    The README's opening argues SILENT is the category nothing else reports, then leads
    its Results with a table that cannot contain one. SEMVER.md said so at the bottom;
    the README did not say it at all.
    """
    script = (
        pathlib.Path(__file__).resolve().parent.parent / "scripts" / "semver_sweep.py"
    ).read_text(encoding="utf-8")
    # The sweep passes no flag; it simply never runs the behaviour pass. It imports
    # `probe.surface`, which READS a package's public API, and never `probe.call`, which
    # EXECUTES it - so the assertion is about calling, not about importing.
    assert "probe import resolve_names, surface" in script, (
        "the sweep no longer reads the API surface the way this caveat assumes"
    )
    assert "call(" not in script, (
        "the sweep now calls the probe - if it runs the behaviour pass, the surface-only "
        "caveat in both documents is wrong"
    )
    assert "api_changes" in script, "the sweep no longer measures the API surface"
    for name, body in (
        ("README.md", README.read_text(encoding="utf-8")),
        ("SEMVER.md", (DOCS / "SEMVER.md").read_text(encoding="utf-8")),
    ):
        lowered = body.lower()
        assert "api surface only" in lowered, f"{name} does not say the sweep is surface-only"
        assert "cannot detect a single `silent`" in lowered or (
            "cannot see a single `silent`" in lowered
        ), f"{name} does not say the sweep cannot see SILENT"


def test_the_semver_full_table_is_the_sweep():
    semver = (DOCS / "SEMVER.md").read_text(encoding="utf-8")
    for r in _load("semver-sweep.json"):
        broken = _broken(r)
        line = (
            f"| `{r['package']}` | {r['old']} | {r['new']} | {r['bump']} | "
            f"{r['exported_symbols']} | {f'**{broken}**' if broken else 0} | "
            f"{len(r.get('exported_widened', []))} | {_broken(r, 'internal')} |"
        )
        assert line in semver, line


def _counts_block(text: str, after: str) -> dict[str, int]:
    block = text.split(after, 1)[1]
    return {
        kind.lower(): int(n)
        for kind, n in re.findall(r"^\s*(gone|reshaped|SILENT|widened|added)\s+(\d+)", block, re.M)[
            :5
        ]
    }


def test_the_packaging_run_in_the_readme_is_the_committed_one():
    run = _load("packaging-21.3-to-24.0.json")
    expected = {k: run["counts"].get(k, 0) for k in ("gone", "reshaped", "silent", "widened")}
    expected["added"] = run["counts"].get("added", 0)
    shown = _counts_block(README.read_text(encoding="utf-8"), "--used-by pypa-build\n")
    assert shown == expected
    used = {c["qualname"]: c["used_at"] for c in run["changes"] if c["used_at"]}
    maybe = {c["qualname"]: c["maybe_at"] for c in run["changes"] if c.get("maybe_at")}
    assert any("src/build/env.py:38" in s for s in used["packaging.utils.canonicalize_name"])
    assert any("src/build/__main__.py:485" in s for s in used["packaging.metadata.parse_email"])
    assert any(
        "src/build/_util.py:66" in s for s in maybe["packaging.specifiers.SpecifierSet.contains"]
    )


def test_the_click_patch_run_in_the_readme_is_the_committed_one():
    run = _load("click-8.1.6-to-8.1.7.json")
    readme = README.read_text(encoding="utf-8")
    assert run["counts"] == {}
    assert run["behaviour_candidates"] == 234
    assert f"| **exercised in both versions** | **{run['compared']}** |" in readme
    assert re.search(rf"acts on the machine .*\| {len(run['not_run_side_effects'])} \|", readme)


def test_the_click_major_run_in_results_is_the_committed_one():
    run = _load("click-7.1.2-to-8.1.7.json")
    results = (DOCS / "RESULTS.md").read_text(encoding="utf-8")
    section = results.split("## `click` 7.1.2 -> 8.1.7", 1)[1]
    for kind in ("gone", "reshaped", "widened", "added"):
        assert f"| {kind} | {run['counts'][kind]} |" in section, kind
    assert f"| **silent** | **{run['counts']['silent']}** |" in section
    assert f"behaviour was compared on {run['compared']}" in section.replace("\n", " ")
