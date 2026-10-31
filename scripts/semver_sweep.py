"""Re-run the semver sweep behind docs/SEMVER.md.

    python scripts/semver_sweep.py [--out sweep.json]

For each upgrade pair: install both versions, read both public surfaces, and count
the exported symbols that became GONE or RESHAPED (breaking) versus WIDENED (a
signature that grew in a way every existing call survives, which is not a break).
API surface only - the behaviour pass is not run here.

Installs honour UV_OFFLINE=1, so a run against a warm uv cache needs no network - but
"warm" means a cache that has already held all 45 (package, version) pairs this sweep
installs, which only a previous online run produces. On a cold or partial cache the
affected pairs fail rather than degrade, and one of them, `coverage` 7.5.0 -> 7.5.4, is
behind the patch-row headline. Warm it first with:

    python scripts/semver_sweep.py --out /dev/null    # once, online
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blast_radius.cli import import_plan, install  # noqa: E402
from blast_radius.diff import api_changes, gone_candidates  # noqa: E402
from blast_radius.probe import resolve_names, surface  # noqa: E402
from blast_radius.types import Kind  # noqa: E402


def is_upgrade(old: str, new: str) -> bool:
    """True when `new` is a later release than `old`.

    The census pairs consecutive releases ordered by UPLOAD DATE, so a project that
    publishes a backport after a newer release produces a pair that runs backwards:
    dulwich 1.2.19 -> 1.2.18 was measured and published as a patch upgrade that removed
    `dulwich.index.unchanged_entry_flags`, when in fact 1.2.19 added it.

    classify() cannot catch this - it compares components for equality and never for
    direction, so 5.1.0 -> 4.8.1 is a well-formed "major" pair to it. 21 of 2,238 pairs
    in one run ran backwards, 5 of them counted as breaking an exported name, and the
    worst read as "snowflake-connector-python 5.1.0 -> 4.8.1 removed symbols".
    """
    return version_key(new) > version_key(old)


def classify(old: str, new: str) -> str:
    """Which promise this pair made, read off the versions rather than declared.

    The label used to be hand-written next to each pair, and it drifted: the five 0.x
    pairs were labelled `patch` and `minor` and counted in those rows, while
    docs/SEMVER.md excluded them by hand. So the document published patch 13 / minor 6
    and the script it tells you to reproduce with printed patch 15 / minor 9.

    SemVer 2.0.0 section 4: "Major version zero (0.y.z) is for initial development.
    Anything MAY change at any time." For a 0.y.z release the SECOND component is the
    breaking position, so 0.26.0 -> 0.27.0 is not a minor release in the sense this
    sweep measures. Those pairs get their own two cohorts instead of being discarded:
    the de facto convention they do follow is still a claim worth measuring.

    0.0.z is its own case - with y as well as x at zero there is no patch position left
    to make a promise about - so it is reported separately rather than folded in.
    """

    def parts(text: str) -> list[int]:
        out = []
        for chunk in text.split(".")[:3]:
            digits = ""
            for ch in chunk:
                if not ch.isdigit():
                    break
                digits += ch
            out.append(int(digits) if digits else 0)
        while len(out) < 3:
            out.append(0)
        return out

    a, b = parts(old), parts(new)
    if a[0] >= 1 or b[0] >= 1:
        if a[0] != b[0]:
            return "major"
        return "minor" if a[1] != b[1] else "patch"
    if a[1] == 0 and b[1] == 0:
        return "0.0.z"
    return "0.x-breaking" if a[1] != b[1] else "0.x-patch"


# Cohorts that measured a promise SemVer actually makes, and the 0.x ones that did not.
SEMVER_COHORTS = ("patch", "minor", "major")
ZERO_COHORTS = ("0.x-patch", "0.x-breaking", "0.0.z")

PAIRS = [
    ("beautifulsoup4", "4.12.2", "4.12.3", "patch"),
    ("charset-normalizer", "3.3.0", "3.3.2", "patch"),
    ("click", "8.1.6", "8.1.7", "patch"),
    ("coverage", "7.5.0", "7.5.4", "patch"),
    ("filelock", "3.15.1", "3.15.4", "patch"),
    ("httpx", "0.27.0", "0.27.2", "0.x-patch"),
    ("jinja2", "3.1.2", "3.1.4", "patch"),
    ("markupsafe", "2.1.3", "2.1.5", "patch"),
    ("platformdirs", "4.2.0", "4.2.2", "patch"),
    ("pytest", "8.2.0", "8.2.2", "patch"),
    ("requests", "2.32.2", "2.32.3", "patch"),
    ("rich", "13.7.0", "13.7.1", "patch"),
    ("tqdm", "4.66.1", "4.66.4", "patch"),
    ("typer", "0.12.0", "0.12.3", "0.x-patch"),
    ("urllib3", "2.2.1", "2.2.2", "patch"),
    ("anyio", "4.1.0", "4.4.0", "minor"),
    ("click", "8.0.4", "8.1.7", "minor"),
    ("filelock", "3.13.1", "3.15.4", "minor"),
    ("httpx", "0.26.0", "0.27.0", "0.x-breaking"),
    ("pluggy", "1.4.0", "1.5.0", "minor"),
    ("rich", "13.0.1", "13.7.1", "minor"),
    ("starlette", "0.35.1", "0.37.2", "0.x-breaking"),
    ("typer", "0.9.0", "0.12.3", "0.x-breaking"),
    ("urllib3", "2.1.0", "2.2.2", "minor"),
    # 0.x pairs. Excluded from the three SemVer rows for the reason above, and
    # measured as their own cohorts instead. Ten of these were added after the sweep
    # had five, because two pairs cannot support a rate.
    ("uvicorn", "0.30.0", "0.30.6", "0.x-patch"),
    ("fastapi", "0.110.0", "0.110.3", "0.x-patch"),
    ("starlette", "0.36.0", "0.36.3", "0.x-patch"),
    ("dill", "0.3.7", "0.3.8", "0.x-patch"),
    ("tomlkit", "0.12.0", "0.12.5", "0.x-patch"),
    ("pathspec", "0.12.0", "0.12.1", "0.x-patch"),
    ("distlib", "0.3.7", "0.3.8", "0.x-patch"),
    ("wcwidth", "0.2.6", "0.2.13", "0.x-patch"),
    ("uvicorn", "0.23.2", "0.30.1", "0.x-breaking"),
    ("h11", "0.13.0", "0.14.0", "0.x-breaking"),
    ("fastapi", "0.100.0", "0.111.0", "0.x-breaking"),
    ("tomlkit", "0.12.0", "0.13.2", "0.x-breaking"),
    ("pathspec", "0.11.0", "0.12.1", "0.x-breaking"),
    ("annotated-types", "0.6.0", "0.7.0", "0.x-breaking"),
    ("mdit-py-plugins", "0.3.5", "0.4.1", "0.x-breaking"),
    ("httpcore", "0.16.3", "0.17.0", "0.x-breaking"),
    ("python-multipart", "0.0.6", "0.0.9", "0.0.z"),
    ("click", "7.1.2", "8.1.7", "major"),
    ("rich", "12.6.0", "13.7.1", "major"),
    ("urllib3", "1.26.18", "2.2.2", "major"),
]


def version_key(version: str) -> tuple:
    """A sort key for a version string: release components, then any pre-release tail.

    This module defined `version_key` **twice**, with different return shapes, and the
    second silently shadowed the first - so `is_upgrade`, written against the first,
    had been running on the second since whenever the second was added. The surviving
    one has to do both jobs, and they are:

      * **Order two final releases** on every numeric component, not three.
        `classify()` pads to three because it answers "which position changed"; ordering
        needs them all, since 1.2.19 and 1.2.19.1 agree on three and are not the same
        release.
      * **Be total and deterministic**, so that choosing between two pairs of the same
        package gives the same answer whatever order they were measured in.

    Neither of the two it replaces ordered a pre-release correctly. The first truncated
    `1.0rc1` to (1, 0) and read it as *equal* to `1.0`; the second made the `0rc1` chunk
    sort after every number and read `1.0rc1` as *later* than `1.0`. Both answers make
    `is_upgrade("1.0rc1", "1.0")` false, which drops a real upgrade as backwards. No
    published number moved - neither census contains a single pre- or post-release
    component, which is why this survived - but the rule was wrong and is now right:
    the release components compare first, and a version carrying a pre-release tail
    sorts before the bare release of the same components.

    Still not full PEP 440: `rc` against `b` against `dev` is ordered lexically rather
    than by PEP 440's precedence, and an epoch is not read. Stated rather than implied,
    because the next person to need one of those should know this is not it.
    """
    text = str(version).replace("-", ".").replace("+", ".")
    release: list[int] = []
    tail: list[str] = []
    for part in text.split("."):
        if part.isdigit() and not tail:
            release.append(int(part))
            continue
        # The first component that is not a bare number begins the tail: "1.0rc1" keeps
        # release (1, 0) and tail "rc1", and everything after it is tail too, so a
        # trailing number cannot be read back as a release component.
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if digits and not tail:
            release.append(int(digits))
            tail.append(part[len(digits) :])
        else:
            tail.append(part)
    # 0 before 1 so that a pre-release sorts before the bare release it precedes.
    return (tuple(release), 0 if any(tail) else 1, tuple(tail))


def one_pair_per_package(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped) - at most one pair per package per cohort.

    The census planner picks one pair per class per package, and that is what lets the
    published table count packages as independent projects. A resumed run can still write
    a second pair for a package: `--resume` keys on (package, old, new), and a release
    published between two runs changes which pair is "the latest patch pair". 72 rows of
    one 3,777-row census are second pairs.

    A rule that lives only in the planner is not applied to the file, and the file is what
    every rate is computed from.
    """
    kept: dict[tuple[str, str], dict] = {}
    dropped: list[dict] = []
    for row in rows:
        key = (row.get("package", ""), row.get("cohort", ""))
        here = (version_key(row.get("old", "")), version_key(row.get("new", "")))
        sitting = kept.get(key)
        if sitting is None:
            kept[key] = row
            continue
        theirs = (version_key(sitting.get("old", "")), version_key(sitting.get("new", "")))
        if here < theirs:
            dropped.append(sitting)
            kept[key] = row
        else:
            dropped.append(row)
    return list(kept.values()), dropped


def _named(modules: list[str], limit: int = 3) -> str:
    """A few module names and a count, never the whole list.

    ansible's import plan is 6,846 modules, and the failure message joined every one of
    them: one census row, 1.1 MB, with the reason somewhere in the middle of it.
    """
    shown = ",".join(modules[:limit])
    if len(modules) > limit:
        shown += f" (and {len(modules) - limit} more module(s))"
    return shown or "(no module)"


def _why_empty(version: str, got: dict | None, modules: list[str], timeout: float) -> str:
    """Why a surface walk produced nothing - and these are not the same thing.

    `surface()` returns None when the probe did not finish, and a dict holding only
    `__meta__` when the imports themselves failed. One sentence used to cover both, so a
    package too big to walk in the time allowed was filed as a package that imports
    nothing. ansible is the first: 6,846 modules, and the walk does not finish in 1,800
    seconds on the machine this was measured on, let alone 300.
    """
    if got is None:
        return (
            f"the surface probe for {version} did not finish within {timeout:.0f}s "
            f"({len(modules)} module(s) planned)"
        )
    failed = (got.get("__meta__") or {}).get("failed") or {}
    if failed:
        first = sorted(failed)[0]
        return f"import {_named(sorted(failed))} failed in {version}: {str(failed[first])[:120]}"
    return f"import {_named(modules)} found nothing public in {version}"


def measure(
    package: str,
    old_v: str,
    new_v: str,
    work: Path,
    install_timeout: float = 600.0,
    surface_timeout: float = 300.0,
) -> dict:
    for v in (old_v, new_v):
        ok, why = install(package, v, work / v, timeout=install_timeout)
        if not ok:
            return {"error": f"install {v}: {why.splitlines()[-1] if why else '?'}"}
    plan = import_plan(work / old_v, package)
    owned = sorted(set(plan.owned) | set(import_plan(work / new_v, package).owned))
    # Named, with a count, for the same reason the failure message is: joining 6,846
    # module names into a field that exists to say WHICH modules were compared makes a
    # 300 KB row, and the per-pair ledger carries this field.
    module = _named(plan.modules, limit=8)
    old = surface(work / old_v, plan.modules, owned=owned, timeout=surface_timeout)
    new = surface(work / new_v, plan.modules, owned=owned, timeout=surface_timeout)
    for version, got in ((old_v, old), (new_v, new)):
        if not got or len(got) < 2:
            return {"error": _why_empty(version, got, plan.modules, surface_timeout)}
    exported = {k for k, v in old.items() if k != "__meta__" and v.get("exported")}
    resolved = resolve_names(work / new_v, gone_candidates(old, new))
    rows: dict[str, list[str]] = {}
    for c in api_changes(old, new, resolved):
        if c.kind is Kind.ADDED:
            continue
        scope = "exported" if c.qualname in exported else "internal"
        rows.setdefault(f"{scope}_{c.kind.value}", []).append(c.qualname)
    return {
        "module": module,
        "modules_compared": len(plan.modules),
        "exported_symbols": len(exported),
        **rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sweep.json")
    args = ap.parse_args()
    # The declared label and the one read off the versions have to agree. This is the
    # check that was missing: the labels drifted from the document for five pairs and
    # nothing noticed, because nothing compared them.
    wrong = [
        (p, o, n, declared, classify(o, n))
        for p, o, n, declared in PAIRS
        if classify(o, n) != declared
    ]
    if wrong:
        for p, o, n, declared, derived in wrong:
            print(
                f"{p} {o} -> {n}: declared {declared}, versions say {derived}",
                file=sys.stderr,
            )
        print(
            f"\n{len(wrong)} pair(s) carry a label the version numbers contradict. "
            "Fix the label or the pair; a sweep whose cohorts are hand-written is how "
            "the published table and this script came to disagree.",
            file=sys.stderr,
        )
        return 2

    results = []
    for package, old_v, new_v, bump in PAIRS:
        work = Path(tempfile.mkdtemp(prefix="sweep-"))
        try:
            row = {"package": package, "old": old_v, "new": new_v, "bump": bump}
            row.update(measure(package, old_v, new_v, work))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        results.append(row)
        broke = len(row.get("exported_gone", [])) + len(row.get("exported_reshaped", []))
        print(
            f"{bump:5} {package:20} {old_v:>8} -> {new_v:<8} "
            + (row["error"] if "error" in row else
               f"exported {row['exported_symbols']:4}  broken {broke:3}  "
               f"widened {len(row.get('exported_widened', [])):3}"),
            flush=True,
        )  # fmt: skip
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")

    print()
    print("SemVer cohorts - the promise is that patch and minor break nothing:")
    for bump in SEMVER_COHORTS + ("--",) + ZERO_COHORTS:
        if bump == "--":
            print()
            print(
                "0.x cohorts - SemVer section 4 promises nothing here, so these are NOT "
                "part of the rows above. The second component is the breaking position:"
            )
            continue
        measured = [r for r in results if r["bump"] == bump and "error" not in r]
        # A pair exporting nothing cannot break anything, so counting it as an
        # observation only lowers the rate. mdit-py-plugins 0.3.5 -> 0.4.1 exports 0
        # names (its plugins live in submodules it does not re-export) and would have
        # read as one more clean 0.x bump. The denominator has to be pairs that COULD
        # have broken.
        vacuous = [r for r in measured if r.get("exported_symbols", 0) == 0]
        rows = [r for r in measured if r.get("exported_symbols", 0) > 0]
        broke = [r for r in rows if r.get("exported_gone") or r.get("exported_reshaped")]
        widened_only = [
            r for r in rows if r.get("exported_widened") and r not in broke
        ]  # fmt: skip
        n_sym = sum(len(r.get("exported_gone", [])) + len(r.get("exported_reshaped", []))
                    for r in rows)  # fmt: skip
        print(
            f"{bump:12}  pairs {len(rows):2}  broke exported API {len(broke):2}"
            f"  symbols {n_sym:3}  widened-only pairs {len(widened_only)}"
            + (f"  [{len(vacuous)} excluded: nothing exported]" if vacuous else "")
        )
    failed = [r for r in results if "error" in r]
    if failed:
        # A partial sweep prints a table that looks like a result. Say it is not one:
        # with the network down, 26 of 27 pairs failed to install and the summary still
        # read "patch pairs 1, broke 1" with exit status 0.
        print(
            f"\nINCOMPLETE: {len(failed)} of {len(results)} pairs could not be measured "
            "(see the lines above); the table covers only the rest and is not the "
            "published result. Re-run when the installs succeed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
