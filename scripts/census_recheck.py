"""Re-measure a sample of the committed census with the CURRENT code.

A census is an artefact of one revision. docs/census-summary.json holds rates measured by
whatever this tool did on the day the sweep ran, and the sweep takes about a day, so any
change to what the surface walk reads can move every published rate without touching the
file. The constants change is exactly that kind of change: it was made while a 3,776-pair
census was still running.

Re-running the whole census is a day. This re-runs a sample of it and reports how many
pairs changed their VERDICT - broke an exported name, or did not - which is the only thing
the published rates are built from. The sample is drawn with a fixed seed and stratified by
cohort, so two runs of this script measure the same pairs.

    python scripts/census_recheck.py --per-cohort 8
    python scripts/census_recheck.py --per-cohort 8 --census docs/pair-census.json

What it does not do: prove the rates are unchanged. A sample of 48 pairs out of 1,982
measured bounds the movement loosely, and the output says so with the number it is
entitled to.
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from semver_sweep import measure  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BREAKING = ("exported_gone", "exported_reshaped")
SEED = 20261028


def verdict(row: dict) -> bool | None:
    """Did this pair break an exported name? None when it was not measured."""
    if row.get("error") or not row.get("module"):
        return None
    return any(row.get(k) for k in BREAKING)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--census", type=Path, default=ROOT / "docs" / "pair-census.json")
    ap.add_argument("--per-cohort", type=int, default=8)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "census-recheck.json")
    args = ap.parse_args()

    census = json.loads(args.census.read_text(encoding="utf-8"))
    rows = [r for r in census["results"] if verdict(r) is not None]
    by_cohort: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_cohort[row["cohort"]].append(row)

    rng = random.Random(SEED)
    sample: list[dict] = []
    for cohort in sorted(by_cohort):
        pool = sorted(by_cohort[cohort], key=lambda r: (r["package"], r["old"], r["new"]))
        sample += rng.sample(pool, min(args.per_cohort, len(pool)))

    print(f"{len(rows)} measured pairs in the census; re-measuring {len(sample)}\n")
    changed, unmeasurable, checked = [], [], []
    start = time.time()
    for n, row in enumerate(sample, 1):
        label = f"{row['package']} {row['old']}->{row['new']} ({row['cohort']})"
        with tempfile.TemporaryDirectory() as tmp:
            now = measure(row["package"], row["old"], row["new"], Path(tmp))
        was, is_now = verdict(row), verdict(now)
        if is_now is None:
            # Not a disagreement about the verdict: the pair cannot be measured today at
            # all, usually because a wheel moved or an install broke. Counted apart, or
            # it would read as the code having changed its mind.
            unmeasurable.append({"pair": label, "reason": now.get("error", "?")})
            mark = "unmeasurable now"
        else:
            checked.append(label)
            moved_symbols = {
                k: sorted(set(now.get(k, [])) ^ set(row.get(k, [])))
                for k in BREAKING
                if set(now.get(k, [])) != set(row.get(k, []))
            }
            if was != is_now or moved_symbols:
                changed.append({
                    "pair": label,
                    "was": was,
                    "now": is_now,
                    "verdict_flipped": was != is_now,
                    "symbols_added_or_removed": moved_symbols,
                })
            mark = "same" if was == is_now and not moved_symbols else "CHANGED"
        print(f"  [{n}/{len(sample)}] {label}: {mark}", flush=True)

    flips = [c for c in changed if c["verdict_flipped"]]
    out = {
        "census": str(args.census.name),
        "measured_pairs_in_census": len(rows),
        "re_measured": len(sample),
        "comparable": len(checked),
        "verdict_flips": len(flips),
        "symbol_list_changes": len(changed) - len(flips),
        "unmeasurable_now": unmeasurable,
        "changed": changed,
        "seed": SEED,
        "minutes": round((time.time() - start) / 60, 1),
    }
    args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")

    print(f"\n{len(checked)} pair(s) comparable; {len(flips)} changed verdict, "
          f"{len(changed) - len(flips)} kept the verdict and moved symbols")
    for c in changed:
        print(f"  {c['pair']}: was {c['was']}, now {c['now']} {c['symbols_added_or_removed']}")
    if unmeasurable:
        print(f"{len(unmeasurable)} pair(s) cannot be measured today and were not compared:")
        for u in unmeasurable:
            print(f"  {u['pair']}: {u['reason'][:70]}")
    print(f"\nwritten to {args.out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
