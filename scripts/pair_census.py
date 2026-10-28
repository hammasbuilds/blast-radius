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
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from semver_sweep import (  # noqa: E402
    SEMVER_COHORTS,
    ZERO_COHORTS,
    classify,
    is_upgrade,
    measure,
)

# A census installs thousands of packages. The system temp directory is on C: here,
# which filled to zero bytes free; `uv venv` then failed, and the failure was recorded
# as a property of the package rather than of the run.
SCRATCH = Path(os.environ.get("CENSUS_SCRATCH", r"D:\_census_scratch"))

# Owned by _pin_temp_to_scratch_drive(); sweep_scratch() must leave them alone.
PINNED_DIRS = frozenset({"tmp", "uv-cache"})


# Phrases that describe the run rather than the candidate. A row excluded for one of
# these was never measured, so --resume drops it and measures it again. C: filling to
# zero bytes free produced 295 such rows across two censuses; keeping them would have
# published an infrastructure failure as a property of those packages.
# Phrases that mean "this machine failed", never "this project cannot be measured".
# The first list came from the failures visible at the time. A completed 1,000-project
# census then still carried seven rows this list did not match: five reading
# "failed to write to file C:\...\uv\cache\..." - uv's wording when the drive it
# caches to is full, which never contains the phrase "not enough space" - and two
# TimeoutExpired on `uv pip install`, produced while three censuses shared one throttled
# link. Matching on the symptom one failure happened to print is how six hours of
# measurement keeps a verdict about the machine as a verdict about a package, so each
# entry below names a mechanism and the wordings it is known to take.
INFRA_FAILURES = (
    "could not create a venv",
    # Disk full, in every wording seen: Python's, the OS's, and uv's.
    "not enough space on the disk",
    "no space left on device",
    "failed to write to file",
    "OSError: [WinError 112]",
    # Index unreachable or too slow to use - a property of the link, not the package.
    "could not reach the index",
    "IndexUnreachable",
    "TimeoutExpired: Command '['uv', 'pip', 'install'",
)


def _is_infra_failure(row: dict) -> bool:
    """True when this row records a failure of the harness, not a fact about the target."""
    text = f"{row.get('excluded') or ''} {row.get('error') or ''}"
    return any(phrase.lower() in text.lower() for phrase in INFRA_FAILURES)


def _pin_temp_to_scratch_drive() -> None:
    """Send every temp path a child process may use to the scratch drive.

    SCRATCH alone is not enough. `tempfile.mkdtemp` inside the tools and inside the
    suites under test follows TMPDIR, and uv caches downloads under %LOCALAPPDATA%;
    both are on C: by default here, and C: filled to zero bytes because of it. Set
    before the first subprocess so everything spawned inherits it.
    """
    tmp = SCRATCH / "tmp"
    cache = SCRATCH / "uv-cache"
    tmp.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    for name in ("TMPDIR", "TMP", "TEMP"):
        os.environ[name] = str(tmp)
    os.environ.setdefault("UV_CACHE_DIR", str(cache))
    tempfile.tempdir = None  # drop any value tempfile cached from the old TMPDIR
    # Assert, do not announce. tempfile silently ignores an unusable TMPDIR and falls
    # back to the system temp, so printing the intended path proves nothing.
    landed = pathlib.Path(tempfile.gettempdir()).resolve()
    if landed.drive.upper() != SCRATCH.resolve().drive.upper():
        raise RuntimeError(
            f"temp did not pin: wanted a path on {SCRATCH.resolve().drive}, "
            f"tempfile resolves to {landed}. Refusing to run - this is how C: filled."
        )
    print(f"temp pinned to {landed}")
    print(f"uv cache pinned to {os.environ['UV_CACHE_DIR']}")

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


def released_versions(package: str) -> list[tuple[str, str]]:
    """Every non-yanked final release as (upload date, version), oldest first."""
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
    return sorted(dated)


def pairs_for(package: str, since: str) -> dict[str, tuple[str, str]]:
    """The most recent consecutive pair of each bump class, within a recency window.

    The window is not a convenience. Without it the rule picks, for every mature
    package, the last 0.x pair it ever had - `requests` 0.13.9 from 2013, `idna` 0.8 -
    because that is genuinely the most recent pair of that class. Those releases often
    cannot be installed at all on a current interpreter (requests 0.13.9 imports `cgi`,
    removed from the standard library in 3.13), and a decade-old release is not evidence
    about how projects version today.

    So a pair qualifies only when the NEWER version was published on or after `since`.
    The cut is on the release date, never on the result, and it is reported with the
    table so a reader can see what the sample covers.
    """
    versions = released_versions(package)
    chosen: dict[str, tuple[str, str]] = {}
    for (_, old), (new_date, new) in zip(versions, versions[1:]):
        if new_date < since:
            continue
        # Consecutive BY DATE is not consecutive by version. A project that ships a
        # backport after a newer release gives a pair that runs backwards, and the
        # measurement then reports the symbols the newer release ADDED as removed. The
        # label cannot catch it: classify() compares components for equality, so
        # 5.1.0 -> 4.8.1 is a well-formed "major" pair to it.
        if not is_upgrade(old, new):
            continue
        chosen[classify(old, new)] = (old, new)  # later pairs overwrite earlier ones
    return chosen


def revision() -> str:
    """The commit this run measures at, stamped on every row it writes.

    A census is resumed across days, and across commits: the first full sweep holds rows
    measured by code that no longer exists, and one of them reports 1,240 removed names
    for an ansible pair whose surface walk does not finish in 1,800 seconds at any
    revision now in the tree. The summary carried ONE revision for the whole file, which
    was tidy and not true of it.

    'unknown' rather than a guess when git cannot answer.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent),
             "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown" if out.returncode == 0 else "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packages", type=int, default=200,
                    help="how many of the top packages to walk")
    ap.add_argument("--cohorts", default=",".join(SEMVER_COHORTS + ZERO_COHORTS))
    ap.add_argument("--since", default="2021-01-01",
                    help="a pair counts only if the newer version was published on or "
                         "after this date (2021-01-01)")
    ap.add_argument("--workers", type=int, default=1,
                    help="pairs measured at once. A pair is mostly waiting on network "
                         "and disk, and each worker installs into its own directory; "
                         "the only shared state is uv's cache (1)")
    ap.add_argument("--resume", action="store_true",
                    help="keep pairs already present in --out and measure only the rest")
    ap.add_argument("--out", default="census.json")
    ap.add_argument("--plan-only", action="store_true",
                    help="choose the pairs and write them, without installing anything")
    args = ap.parse_args()
    wanted = set(args.cohorts.split(","))

    names = top_packages(args.packages)
    print(f"{len(names)} packages from the external top-downloads list, "
          f"pairs released on or after {args.since}")

    plan = []
    for i, name in enumerate(names, 1):
        for cohort, (old, new) in sorted(pairs_for(name, args.since).items()):
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

    SCRATCH.mkdir(parents=True, exist_ok=True)
    _pin_temp_to_scratch_drive()
    out = Path(args.out)
    if args.plan_only:
        out.write_text(
            json.dumps({"since": args.since, "packages_requested": args.packages,
                        "cohorts": sorted(wanted), "top_downloads_list": TOP,
                        "command": "python " + " ".join(["scripts/pair_census.py",
                                                         *sys.argv[1:]]),
                        "plan": plan, "counts": counts}, indent=1),
            encoding="utf-8")
        print(f"\nplan written to {out}")
        return 0

    results: list[dict] = []
    if args.resume and out.exists():
        try:
            prior = json.loads(out.read_text(encoding="utf-8"))
            results = prior["results"] if isinstance(prior, dict) else list(prior)
        except (OSError, ValueError, KeyError):
            results = []
    retry = [r for r in results if _is_infra_failure(r)]
    if retry:
        print(f"retrying {len(retry):,} pairs whose failure was infrastructure, not a result")
        results = [r for r in results if not _is_infra_failure(r)]
    seen = {(r["package"], r["old"], r["new"]) for r in results}
    todo = [p for p in plan if (p["package"], p["old"], p["new"]) not in seen]
    if seen:
        print(f"resuming: {len(seen):,} pairs already measured, {len(todo):,} to go")

    started = time.time()
    done_count = 0
    lock = threading.Lock()
    here = revision()
    print(f"measuring at {here}")
    # The run's own arguments, in its output. Without them the file cannot say how many
    # package names it was given, which is the first thing anyone writing up its rates
    # needs - `since` and the plan counts were all it carried.
    provenance = {
        "since": args.since,
        "packages_requested": args.packages,
        "cohorts": sorted(wanted),
        "workers": args.workers,
        "top_downloads_list": TOP,
        "revision_of_this_run": here,
        "command": "python " + " ".join(["scripts/pair_census.py", *sys.argv[1:]]),
    }

    def one(index_and_pair: tuple[int, dict], root: Path) -> dict:
        nonlocal done_count
        index, spec = index_and_pair
        work = root / f"{spec['package']}-{index}"
        work.mkdir(parents=True, exist_ok=True)
        row = dict(spec)
        row["measured_at"] = here
        try:
            row.update(measure(spec["package"], spec["old"], spec["new"], work))
        except Exception as exc:  # noqa: BLE001 - a crash is a result, and is recorded
            row["error"] = f"{type(exc).__name__}: {exc}"
        with lock:
            results.append(row)
            done_count += 1
            n = done_count
            rate = (time.time() - started) / n
            print(f"  [{len(seen) + n}/{len(plan)}] {spec['package']} "
                  f"{spec['old']}->{spec['new']} ({spec['cohort']}) "
                  f"{'error' if 'error' in row else 'ok'}  {rate:.1f}s/pair "
                  f"~{rate * (len(todo) - n) / 60:.0f}min left", flush=True)
            out.write_text(json.dumps({**provenance, "plan_counts": counts,
                                       "results": results}, indent=1), encoding="utf-8")
        return row

    with tempfile.TemporaryDirectory(prefix="pair-census-", dir=SCRATCH) as tmp:
        root = Path(tmp)
        if args.workers > 1:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                list(pool.map(lambda ip: one(ip, root), enumerate(todo, 1)))
        else:
            for pair in enumerate(todo, 1):
                one(pair, root)

    ok = [r for r in results if "error" not in r]
    print(f"\n{len(ok)} of {len(results)} pairs produced a surface; "
          f"{len(results) - len(ok)} could not be measured and are kept in the file")
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
