"""Turn the pair census into the table docs/SEMVER.md publishes.

    python scripts/census_table.py [--census docs/pair-census.json] [--md out.md]

Reports, per bump class: how many pairs were measured, how many independent packages
those pairs came from, how many broke the exported API, how many exported symbols
moved, and how many pairs could not be measured at all.

Three things are deliberate:

  * **Packages are counted as well as pairs.** The hand-written sweep had 22 pairs from
    18 packages, so two cohorts held fewer independent projects than rows. The census
    takes at most one pair per class per package, and printing both numbers is how a
    reader checks that.
  * **A rate is never printed on a denominator too small to carry one.** The old sweep
    reported "2 of 3" for major bumps with no percentage, which was right. The rule is
    applied here rather than remembered: under MIN_FOR_RATE pairs, the count is shown
    and the percentage is not.
  * **Unmeasured pairs are counted and shown.** A pair whose install or import failed
    is not silently dropped; it appears beside the cohort it would have joined.

`widened` is not a break. A signature that only grew leaves every existing call
working, which is the distinction the whole tool rests on.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

MIN_FOR_RATE = 10  # below this, print the count and withhold the percentage

ORDER = ["patch", "minor", "major", "0.x-patch", "0.x-breaking", "0.0.z"]
BREAKING = ("exported_gone", "exported_reshaped")


def summarise(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for cohort in ORDER:
        mine = [r for r in rows if r.get("cohort") == cohort]
        ok = [r for r in mine if "error" not in r]
        failed = len(mine) - len(ok)
        broke = [r for r in ok if any(r.get(k) for k in BREAKING)]
        symbols = sum(len(r.get(k, [])) for r in ok for k in BREAKING)
        exported = sum(r.get("exported_symbols", 0) for r in ok)
        widened = sum(len(r.get("exported_widened", [])) for r in ok)
        if not mine:
            continue
        out[cohort] = {
            "pairs": len(ok),
            "packages": len({r["package"] for r in ok}),
            "unmeasured": failed,
            "broke": len(broke),
            "broke_rate": (len(broke) / len(ok)) if len(ok) >= MIN_FOR_RATE else None,
            "broken_symbols": symbols,
            "exported_symbols": exported,
            "symbol_rate": (symbols / exported) if exported and len(ok) >= MIN_FOR_RATE else None,
            "widened": widened,
        }
    return out


def fmt_rate(value: float | None, count: int, total: int) -> str:
    if value is None:
        return f"**{count} of {total}**"
    return f"**{count} of {total} ({value * 100:.0f}%)**"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--census", type=Path, default=Path("docs/pair-census.json"))
    ap.add_argument("--md", type=Path)
    args = ap.parse_args()

    data = json.loads(args.census.read_text(encoding="utf-8"))
    rows = data["results"] if isinstance(data, dict) else data
    table = summarise(rows)

    measured = sum(v["pairs"] for v in table.values())
    unmeasured = sum(v["unmeasured"] for v in table.values())
    packages = len({r["package"] for r in rows if "error" not in r})

    lines = [
        f"Measured {measured:,} upgrade pairs across {packages:,} independent packages. "
        f"{unmeasured:,} further pairs could not be measured and are listed with their "
        f"reason in `{args.census.name}`.",
        "",
        "| Bump | Pairs | Packages | Broke exported API | Broken symbols | Of exported symbols | Widened |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for cohort in ORDER:
        if cohort not in table:
            continue
        v = table[cohort]
        rate = (f"{v['symbol_rate'] * 100:.2f}%" if v["symbol_rate"] is not None else "-")
        lines.append(
            f"| **{cohort}** | {v['pairs']:,} | {v['packages']:,} | "
            f"{fmt_rate(v['broke_rate'], v['broke'], v['pairs'])} | {v['broken_symbols']:,} | "
            f"{v['broken_symbols']:,} of {v['exported_symbols']:,} ({rate}) | {v['widened']:,} |")

    thin = [c for c, v in table.items() if v["broke_rate"] is None]
    if thin:
        lines += ["", f"No percentage is given for {', '.join(thin)}: under {MIN_FOR_RATE} "
                      "measured pairs, a rate would read as precision the sample cannot carry."]

    why = collections.Counter(
        str(r["error"]).split(":")[0][:48] for r in rows if "error" in r)
    if why:
        lines += ["", "Pairs that could not be measured, by reason:", "",
                  "| Reason | Pairs |", "|---|---:|"]
        lines += [f"| {reason} | {count:,} |" for reason, count in why.most_common(10)]

    text = "\n".join(lines)
    print(text)
    if args.md:
        args.md.write_text(text + "\n", encoding="utf-8")
        print(f"\nwritten to {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
