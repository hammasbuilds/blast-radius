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


def measure(package: str, old_v: str, new_v: str, work: Path) -> dict:
    for v in (old_v, new_v):
        ok, why = install(package, v, work / v)
        if not ok:
            return {"error": f"install {v}: {why.splitlines()[-1] if why else '?'}"}
    plan = import_plan(work / old_v, package)
    owned = sorted(set(plan.owned) | set(import_plan(work / new_v, package).owned))
    module = ",".join(plan.modules)
    old = surface(work / old_v, plan.modules, owned=owned)
    new = surface(work / new_v, plan.modules, owned=owned)
    if not old or not new or len(old) < 2 or len(new) < 2:
        return {"error": f"import {module} failed or found nothing"}
    exported = {k for k, v in old.items() if k != "__meta__" and v.get("exported")}
    resolved = resolve_names(work / new_v, gone_candidates(old, new))
    rows: dict[str, list[str]] = {}
    for c in api_changes(old, new, resolved):
        if c.kind is Kind.ADDED:
            continue
        scope = "exported" if c.qualname in exported else "internal"
        rows.setdefault(f"{scope}_{c.kind.value}", []).append(c.qualname)
    return {"module": module, "exported_symbols": len(exported), **rows}


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
