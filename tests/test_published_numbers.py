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


def test_the_semver_summary_rows_are_the_sweep():
    rows = _load("semver-sweep.json")
    assert len(rows) == 27 and not [r for r in rows if "error" in r]
    readme = README.read_text(encoding="utf-8")
    semver = (DOCS / "SEMVER.md").read_text(encoding="utf-8")
    for bump in ("patch", "minor", "major"):
        mine = [r for r in rows if r["bump"] == bump]
        broke = [r for r in mine if _broken(r)]
        share = f"{len(broke)} ({round(100 * len(broke) / len(mine))}%)"
        symbols = sum(_broken(r) for r in mine)
        pattern = re.compile(
            rf"\|\s*\**{bump}\**\s*\|\s*{len(mine)}\s*\|\s*\**{re.escape(share)}\**\s*\|"
            rf"\s*{symbols}\s*\|"
        )
        assert pattern.search(readme), f"README {bump} row"
        assert pattern.search(semver), f"SEMVER.md {bump} row"


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
