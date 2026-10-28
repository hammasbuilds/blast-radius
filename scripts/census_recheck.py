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
# Everything a surface comparison can report. The published rates are built from BREAKING
# alone, so only those can move a figure - but a change the code made to `internal_*` is
# still a change the code made, and a check that reads only the published keys cannot say
# whether anything happened. The constants change lands almost entirely in `internal_*`,
# because a constant in a submodule is neither in __all__ nor at the top level.
ALL_KEYS = ("exported_gone", "exported_reshaped", "exported_widened",
            "internal_gone", "internal_reshaped", "internal_widened")
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
    ap.add_argument("--plant", type=int, default=0, metavar="N",
                    help="sensitivity run: corrupt N sampled rows in a copy of the census "
                         "and report whether the re-check contradicts each one. "
                         "'nothing changed' is not evidence until a planted change is found")
    args = ap.parse_args()

    # Read once and never written back: --plant corrupts rows in memory, and a run that
    # wrote them out would plant a false removal in the published census.
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

    planted: set[str] = set()
    if args.plant:
        # Each plant must be a claim a fresh measurement contradicts. Adding a symbol
        # nothing can report is such a claim: the re-check must come back having lost it.
        for row in sample[: args.plant]:
            row.setdefault("exported_gone", [])
            row["exported_gone"] = [*row["exported_gone"], "planted.symbol.that.never.existed"]
            planted.add(f"{row['package']} {row['old']}->{row['new']} ({row['cohort']})")
        print(f"planted a false removal in {len(planted)} row(s); each must come back "
              f"as a change\n")

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
            moved = {
                k: {"gained": sorted(set(now.get(k, [])) - set(row.get(k, []))),
                    "lost": sorted(set(row.get(k, [])) - set(now.get(k, [])))}
                for k in ALL_KEYS
                if set(now.get(k, [])) != set(row.get(k, []))
            }
            exported_moved = {k: v for k, v in moved.items() if k.startswith("exported_")}
            internal_moved = {k: v for k, v in moved.items() if k.startswith("internal_")}
            if was != is_now or moved:
                changed.append({
                    "pair": label,
                    "was": was,
                    "now": is_now,
                    "verdict_flipped": was != is_now,
                    "exported_symbols_moved": exported_moved,
                    "internal_symbols_moved": internal_moved,
                })
            if was != is_now or exported_moved:
                mark = "CHANGED (a published rate depends on this)"
            elif internal_moved:
                mark = "internal only"
            else:
                mark = "same"
        print(f"  [{n}/{len(sample)}] {label}: {mark}", flush=True)

    flips = [c for c in changed if c["verdict_flipped"]]
    exported_moves = [c for c in changed
                      if not c["verdict_flipped"] and c["exported_symbols_moved"]]
    internal_only = [c for c in changed
                     if not c["verdict_flipped"] and not c["exported_symbols_moved"]]
    out = {
        "census": str(args.census.name),
        "measured_pairs_in_census": len(rows),
        "re_measured": len(sample),
        "comparable": len(checked),
        "verdict_flips": len(flips),
        "exported_symbol_changes_without_a_flip": len(exported_moves),
        "internal_only_changes": len(internal_only),
        "unmeasurable_now": unmeasurable,
        "changed": changed,
        "seed": SEED,
        "minutes": round((time.time() - start) / 60, 1),
    }
    args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")

    print(f"\n{len(checked)} pair(s) comparable; {len(flips)} changed verdict, "
          f"{len(exported_moves)} moved an exported symbol without changing the verdict, "
          f"{len(internal_only)} changed only internal symbols")
    for c in changed:
        where = "exported" if (c["verdict_flipped"] or c["exported_symbols_moved"]) else "internal"
        print(f"  {c['pair']}: was {c['was']}, now {c['now']} ({where})")
        for key, move in {**c["exported_symbols_moved"], **c["internal_symbols_moved"]}.items():
            if move["gained"]:
                print(f"      {key} +{len(move['gained'])}: "
                      f"{', '.join(move['gained'][:4])}")
            if move["lost"]:
                print(f"      {key} -{len(move['lost'])}: "
                      f"{', '.join(move['lost'][:4])}")
    if unmeasurable:
        print(f"{len(unmeasurable)} pair(s) cannot be measured today and were not compared:")
        for u in unmeasurable:
            print(f"  {u['pair']}: {u['reason'][:70]}")
    if planted:
        found = {c["pair"] for c in changed}
        missed = sorted(planted - found)
        print(f"\nsensitivity: {len(planted) - len(missed)} of {len(planted)} planted "
              f"changes were caught")
        for pair in missed:
            print(f"  MISSED {pair}")
        out["planted"] = sorted(planted)
        out["planted_missed"] = missed
        if missed:
            print("\nthe re-check cannot be trusted to report a difference, so "
                  "'nothing changed' from it means nothing")
            args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")
            return 1

    print(f"\nwritten to {args.out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
