"""Reduce docs/pair-census.json to the figures the README quotes, with denominators.

The census walks thousands of real release pairs and records, per pair, which public
names went or changed shape. Nothing read it: no README figure came from it and no test
opened it, so a sweep of 3,736 upgrades finished and published nothing.

What it publishes, per semver cohort:

  pairs          upgrades planned in this cohort
  measured       ... that produced a surface comparison
  unmeasured     ... that did not, by reason
  broke_exported share of MEASURED pairs where an exported name went or changed shape

The denominator is `measured`, never `pairs`, and both are published. A quarter of the
rows in one run carried an error - a third of those because the package has no importable
module at all (the `types-*` stubs, `nvidia-*`) - and a rate over `pairs` would have
counted every one of them as an upgrade that broke nothing.

    python scripts/census_summary.py --revision <the commit the census ran at>
    python scripts/census_summary.py --revision e23233d --census docs/pair-census.json
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_census import _is_infra_failure  # noqa: E402
from semver_sweep import is_upgrade  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# What an exported name going or changing shape is called in a row. `widened` is excluded
# deliberately: the tool reports it as "not breaking - existing calls still work", so
# counting it as a break would inflate the headline with changes that break nobody.
BREAKING = ("exported_gone", "exported_reshaped")


def _reason(row: dict) -> str:
    error = row.get("error") or ""
    if _is_infra_failure(row):
        return "this machine failed (retried, not a finding)"
    if "failed or found nothing" in error:
        return "no importable module (stub-only or namespace package)"
    if error.startswith("install "):
        return "install failed"
    return re.sub(r"[`'\"].*", "", error)[:60] or "no reason recorded"


def summarise(census: dict) -> dict:
    rows = census["results"]
    # Rows that run backwards are dropped here as well as skipped at planning time.
    # The census takes --resume, so the 21 backwards pairs a run had already written
    # stay on disk; a filter that lived only in pairs_for would leave them in every
    # published rate. Counted and reported, not silently removed.
    backwards = [r for r in rows if not is_upgrade(r["old"], r["new"])]
    rows = [r for r in rows if is_upgrade(r["old"], r["new"])]
    by_cohort: dict[str, dict] = {}
    for row in rows:
        c = by_cohort.setdefault(
            row["cohort"],
            {"pairs": 0, "measured": 0, "unmeasured": collections.Counter(), "broke": 0},
        )
        c["pairs"] += 1
        if row.get("error") or not row.get("module"):
            c["unmeasured"][_reason(row)] += 1
            continue
        c["measured"] += 1
        c["broke"] += any(row.get(k) for k in BREAKING)

    cohorts = {}
    for name, c in sorted(by_cohort.items()):
        cohorts[name] = {
            "pairs": c["pairs"],
            "measured": c["measured"],
            "broke_exported": c["broke"],
            # None, not 0.0, when nothing was measured. A cohort with no measurement has
            # no rate, and printing 0% would read as "nothing in this cohort breaks".
            "broke_exported_share": (
                round(c["broke"] / c["measured"], 4) if c["measured"] else None
            ),
            "unmeasured": dict(c["unmeasured"].most_common()),
        }

    total_measured = sum(c["measured"] for c in cohorts.values())
    total_broke = sum(c["broke_exported"] for c in cohorts.values())
    return {
        "since": census.get("since"),
        "planned": dict(census.get("plan_counts") or {}),
        "rows": len(rows),
        "dropped_not_an_upgrade": len(backwards),
        "measured": total_measured,
        "broke_exported": total_broke,
        "broke_exported_share": (
            round(total_broke / total_measured, 4) if total_measured else None
        ),
        "cohorts": cohorts,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--census", type=Path, default=ROOT / "docs" / "pair-census.json")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "census-summary.json")
    # Which revision of this tool produced the CENSUS, which is not the revision
    # summarising it. The sweep takes about a day, and a change to what the surface walk
    # reads moves every rate in this file without touching it - the constants change
    # landed while a 3,776-pair census was still running. HEAD at summary time would be a
    # wrong answer dressed as provenance, so this has no default.
    ap.add_argument("--revision", default="",
                    help="the commit the census itself ran at, e.g. e23233d")
    args = ap.parse_args()
    if not args.census.exists():
        raise SystemExit(f"no census at {args.census} - run scripts/pair_census.py first")
    summary = summarise(json.loads(args.census.read_text(encoding="utf-8")))
    summary = {"measured_at_revision": args.revision or "not recorded", **summary}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    if summary["dropped_not_an_upgrade"]:
        print(
            f"dropped {summary['dropped_not_an_upgrade']} row(s) whose 'new' version is "
            f"older than its 'old' - consecutive by upload date, not by version\n"
        )
    print(f"{summary['rows']} rows, {summary['measured']} measured, "
          f"{summary['broke_exported']} broke an exported name "
          f"({(summary['broke_exported_share'] or 0) * 100:.1f}%)\n")
    if not args.revision:
        print(
            "no --revision given: the summary says the revision is unknown, which is "
            "true and useless. Pass the commit the census itself ran at.\n"
        )
    print(f"  {'cohort':14}{'pairs':>7}{'measured':>10}{'broke':>7}{'share':>8}")
    for name, c in summary["cohorts"].items():
        share = "n/a" if c["broke_exported_share"] is None else f"{c['broke_exported_share']:.1%}"
        print(f"  {name:14}{c['pairs']:>7}{c['measured']:>10}{c['broke_exported']:>7}{share:>8}")
    print(f"\nwritten to {args.out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
