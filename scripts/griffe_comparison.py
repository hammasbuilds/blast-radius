"""Rebuild docs/griffe-comparison.json: both tools over the same two directories.

griffe reads source and never imports it; blast-radius installs both versions, imports
each in its own subprocess, and executes what survived. To compare them at all, both have
to be looking at the same bytes - so this installs each version once, into
`.griffe-targets/<package>-<version>`, and points both tools there.

griffe is not a dependency of this package and never will be. It is installed into its own
virtualenv, `.venv-griffe`, which this script creates if it is missing, so the comparison
can be reproduced without changing what `pip install blast-radius` pulls in.

    python scripts/griffe_comparison.py
    python scripts/griffe_comparison.py --pair click 7.1.2 8.1.7

Writing the figures is the point, so a tool that fails is a row with its error rather than
a crash: a comparison that silently drops the side that did worse is not a comparison.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ROOT / ".griffe-targets"
VENV = ROOT / ".venv-griffe"
ARTEFACT = ROOT / "docs" / "griffe-comparison.json"

PAIRS = [("click", "7.1.2", "8.1.7"), ("urllib3", "2.2.1", "2.2.2")]

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The griffe side runs in its own interpreter, so it is a script rather than an import.
# `allow_inspection=False` is the whole point of comparing against it: griffe must not
# fall back to importing the package, or both tools would be doing the same thing.
GRIFFE_SCRIPT = '''
import collections, json, sys, time
import griffe

package, old_dir, new_dir = sys.argv[1], sys.argv[2], sys.argv[3]
start = time.perf_counter()
old = griffe.load(package, search_paths=[old_dir], allow_inspection=False)
new = griffe.load(package, search_paths=[new_dir], allow_inspection=False)
breakages = list(griffe.find_breaking_changes(old, new))
elapsed = time.perf_counter() - start


def count_members(obj):
    n = 0
    for m in obj.members.values():
        if m.is_alias:
            continue
        n += 1
        if m.is_module or m.is_class:
            n += count_members(m)
    return n


rows = []
for b in breakages:
    try:
        explain = b.explain(style=griffe.ExplanationStyle.ONE_LINE)
    except Exception as exc:
        explain = "<could not explain: %s>" % exc
    rows.append({
        "kind": b.kind.value,
        "path": b.obj.path,
        "owner": b.obj.path.rsplit(".", 1)[0],
        "explain": explain,
    })

print("__JSON__" + json.dumps({
    "breakages": len(breakages),
    "distinct_owners": len({r["owner"] for r in rows}),
    "by_kind": dict(collections.Counter(r["kind"] for r in rows).most_common()),
    "objects_walked_old": count_members(old),
    "objects_walked_new": count_members(new),
    "seconds": round(elapsed, 1),
    "rows": rows,
}))
'''


def _python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_griffe() -> str:
    """griffe in its own virtualenv, and the version actually installed there."""
    python = _python(VENV)
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    shown = subprocess.run(
        [str(python), "-m", "pip", "show", "griffe"], capture_output=True, text=True
    )
    if shown.returncode != 0:
        subprocess.run(
            [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", "griffe"],
            check=True,
        )
        shown = subprocess.run(
            [str(python), "-m", "pip", "show", "griffe"], capture_output=True, text=True, check=True
        )
    for line in shown.stdout.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def install(package: str, version: str) -> Path:
    """One version into its own directory, --no-deps so only the package itself is there."""
    target = TARGETS / f"{package}-{version}"
    if target.exists():
        return target
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
         "--target", str(target), "--no-deps", f"{package}=={version}"],
        check=True,
    )
    return target


def run_griffe(package: str, old: str, new: str) -> dict:
    old_dir, new_dir = install(package, old), install(package, new)
    proc = subprocess.run(
        [str(_python(VENV)), "-c", GRIFFE_SCRIPT, package, str(old_dir), str(new_dir)],
        capture_output=True, text=True,
    )
    marker = proc.stdout.find("__JSON__")
    if marker < 0:
        return {"error": (proc.stderr or proc.stdout or "no output")[-400:]}
    out = json.loads(proc.stdout[marker + len("__JSON__"):].splitlines()[0])
    prefix = str(TARGETS) + os.sep
    for row in out["rows"]:
        row["explain"] = ANSI.sub("", row["explain"]).replace(prefix, "")
    return out


def run_blast_radius(package: str, old: str, new: str) -> dict:
    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "blast_radius", "check", package, old, new, "--json"],
        capture_output=True, text=True, cwd=ROOT,
    )
    wall = round(time.perf_counter() - start, 1)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"error": (proc.stderr or "no output")[-400:], "wall_seconds": wall}
    r = json.loads(proc.stdout)
    return {
        "counts": r["counts"],
        "compared": r["compared"],
        "unreachable": r["unreachable"],
        "behaviour_candidates": r["behaviour_candidates"],
        "nondeterministic": r["nondeterministic"],
        "unknown_signatures": len(r["unknown_signatures"]),
        # `seconds` is the measured phase; the wall clock includes installing both
        # versions, which the griffe side does not pay because this script installed
        # them. Both are published rather than one being passed off as the other.
        "seconds": r["seconds"],
        "wall_seconds": wall,
        "changes": r["changes"],
        "weak_differences": r["weak_differences"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pair", nargs=3, metavar=("PACKAGE", "OLD", "NEW"), action="append",
                    help="a pair to compare; repeatable (default: the two in docs/COMPARISON.md)")
    ap.add_argument("--out", type=Path, default=ARTEFACT)
    args = ap.parse_args()
    pairs = [tuple(p) for p in args.pair] if args.pair else PAIRS

    griffe_version = ensure_griffe()
    runs_griffe, runs_br = [], {}
    for package, old, new in pairs:
        label = f"{package} {old} -> {new}"
        print(f"griffe        {label} ...", flush=True)
        row = run_griffe(package, old, new)
        row.update(package=package, old=old, new=new)
        runs_griffe.append(row)
        print(f"blast-radius  {label} ...", flush=True)
        runs_br[label] = run_blast_radius(package, old, new)

    out = {
        "measured": time.strftime("%Y-%m-%d"),
        "machine": "one machine, one interpreter, the same two installed directories per pair",
        "griffe": {
            "version": griffe_version,
            "how": "griffe.load(package, search_paths=[dir]) on each version, then "
                   "griffe.find_breaking_changes(old, new); allow_inspection=False, so "
                   "nothing is imported",
            "note": "griffe's own one-line explanations, with its ANSI colour codes "
                    "stripped and the local target directory removed from each path",
            "runs": runs_griffe,
        },
        "blast_radius": {
            "version": subprocess.run(
                [sys.executable, "-m", "blast_radius", "--version"],
                capture_output=True, text=True, cwd=ROOT,
            ).stdout.strip().split()[-1],
            "how": "blast-radius check <package> <old> <new> --json, which installs both "
                   "versions, imports each in its own subprocess, and executes the "
                   "functions whose call shape survived",
            "runs": runs_br,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")

    print()
    for row in runs_griffe:
        if "error" in row:
            print(f"  griffe {row['package']}: FAILED - {row['error'][:80]}")
            continue
        print(f"  griffe {row['package']} {row['old']} -> {row['new']}: "
              f"{row['breakages']} breakage(s) at {row['distinct_owners']} owner(s), "
              f"{row['seconds']}s")
        for kind, n in row["by_kind"].items():
            print(f"      {kind:52}{n:>5}")
    for label, row in runs_br.items():
        if "error" in row:
            print(f"  blast-radius {label}: FAILED - {row['error'][:80]}")
            continue
        counts = ", ".join(f"{k} {v}" for k, v in row["counts"].items())
        print(f"  blast-radius {label}: {counts} ({row['seconds']}s measured, "
              f"{row['wall_seconds']}s including both installs)")
    print(f"\nwritten to {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
