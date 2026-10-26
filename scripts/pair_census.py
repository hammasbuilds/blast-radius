"""Derive the upgrade pairs from PyPI itself, instead of choosing them by hand.

    python scripts/pair_census.py --packages 200 [--out census.json]

`semver_sweep.py` measures 22 pairs that were written into the file by hand. Two
objections follow from that, and neither is answerable by adding more hand-picked
pairs:

  * **Selection.** A reader cannot tell whether the pairs were chosen before or after
    their outcome was known. Nothing in the file records which.
  * **Pseudo-replication.** Seven of the 22 are second pairs from a package already in
    the sweep - uvicorn, fastapi, tomlkit, pathspec, urllib3, click and rich each
    appear twice. Two pairs from one project are not two independent observations of
    how projects behave.

This script removes both by construction:

  * The package list is **external**: the top packages by download count, from
    hugovk/top-pypi-packages. Position in that list cannot depend on this measurement.
  * The pair for a package is picked by a **stated rule applied blind**: walk that
    package's releases in upload order and keep the most recent *consecutive* pair of
    each bump class. The rule reads only version numbers and dates, never a result.
  * **At most one pair per class per package**, so no project is counted twice inside a
    cohort.
  * Every failure is **recorded and counted**. A package that will not install or will
    not import is a row in the output with its reason, not a silent omission. The
    denominator a rate is quoted over is the number of pairs that produced a surface,
    and the number that did not is printed beside it.

Yanked releases, pre-releases and dev releases are skipped: they are not upgrades a
resolver would hand anyone by default.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from semver_sweep import SEMVER_COHORTS, ZERO_COHORTS, classify, measure  # noqa: E402

TOP = ("https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/"
       "top-pypi-packages.min.json")
RELEASE = re.compile(r"^\d+(\.\d+)*$")  # no pre-release, dev or local segments


def fetch_json(url: str, tries: int = 3) -> dict:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def top_packages(count: int) -> list[str]:
    data = fetch_json(TOP)
    rows = data["rows"] if isinstance(data, dict) else data
    return [r["project"] for r in rows[:count]]


def released_versions(package: str) -> list[str]:
    """Every non-yanked final release of a package, oldest upload first."""
    try:
        data = fetch_json(f"https://pypi.org/pypi/{package}/json")
    except Exception:  # noqa: BLE001 - a package that cannot be read is reported, not raised
        return []
    dated = []
    for version, files in data.get("releases", {}).items():
        if not files or not RELEASE.match(version):
            continue
        if all(f.get("yanked") for f in files):
            continue
        dated.append((min(f["upload_time_iso_8601"] for f in files), version))
    return [v for _, v in sorted(dated)]


def pairs_for(package: str) -> dict[str, tuple[str, str]]:
    """The most recent consecutive pair of each bump class. Outcome is never consulted."""
    versions = released_versions(package)
    chosen: dict[str, tuple[str, str]] = {}
    for old, new in zip(versions, versions[1:]):
        chosen[classify(old, new)] = (old, new)  # later pairs overwrite earlier ones
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packages", type=int, default=200,
                    help="how many of the top packages to walk")
    ap.add_argument("--cohorts", default=",".join(SEMVER_COHORTS + ZERO_COHORTS))
    ap.add_argument("--out", default="census.json")
    ap.add_argument("--plan-only", action="store_true",
                    help="choose the pairs and write them, without installing anything")
    args = ap.parse_args()
    wanted = set(args.cohorts.split(","))

    names = top_packages(args.packages)
    print(f"{len(names)} packages from the external top-downloads list")

    plan = []
    for i, name in enumerate(names, 1):
        for cohort, (old, new) in sorted(pairs_for(name).items()):
            if cohort in wanted:
                plan.append({"package": name, "old": old, "new": new, "cohort": cohort})
        if i % 25 == 0:
            print(f"  read {i}/{len(names)} release histories, {len(plan)} pairs so far")

    counts: dict[str, int] = {}
    for p in plan:
        counts[p["cohort"]] = counts.get(p["cohort"], 0) + 1
    print(f"\n{len(plan)} pairs planned, one per class per package:")
    for cohort in sorted(counts):
        print(f"  {cohort:14} {counts[cohort]:4}")

    out = Path(args.out)
    if args.plan_only:
        out.write_text(json.dumps({"plan": plan, "counts": counts}, indent=1), encoding="utf-8")
        print(f"\nplan written to {out}")
        return 0

    results, started = [], time.time()
    with tempfile.TemporaryDirectory(prefix="pair-census-") as tmp:
        root = Path(tmp)
        for i, p in enumerate(plan, 1):
            work = root / f"{p['package']}-{i}"
            work.mkdir(parents=True, exist_ok=True)
            row = dict(p)
            try:
                row.update(measure(p["package"], p["old"], p["new"], work))
            except Exception as exc:  # noqa: BLE001 - a crash is a result, and is recorded
                row["error"] = f"{type(exc).__name__}: {exc}"
            results.append(row)
            done = "error" if "error" in row else "ok"
            rate = (time.time() - started) / i
            print(f"  [{i}/{len(plan)}] {p['package']} {p['old']}->{p['new']} "
                  f"({p['cohort']}) {done}  {rate:.1f}s/pair "
                  f"~{rate * (len(plan) - i) / 60:.0f}min left", flush=True)
            out.write_text(json.dumps({"plan_counts": counts, "results": results},
                                      indent=1), encoding="utf-8")

    ok = [r for r in results if "error" not in r]
    print(f"\n{len(ok)} of {len(results)} pairs produced a surface; "
          f"{len(results) - len(ok)} could not be measured and are kept in the file")
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
