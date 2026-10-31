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
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from semver_sweep import one_pair_per_package  # noqa: E402

MIN_FOR_RATE = 10  # below this, print the count and withhold the percentage

ORDER = ["patch", "minor", "major", "0.x-patch", "0.x-breaking", "0.0.z"]
BREAKING = ("exported_gone", "exported_reshaped")


def symbols_in(row: dict, keys: tuple[str, ...]) -> int:
    """How many symbols this row reports under these keys.

    A census row holds lists of names and the committed ledger holds counts, and this is
    the only place the difference matters. The `widened` sum was left calling len() on it
    and crashed on an int - one of three call sites changed, which is the shape of bug
    this function exists to prevent.
    """
    total = 0
    for key in keys:
        value = row.get(key)
        if value:
            total += value if isinstance(value, int) else len(value)
    return total


def summarise(rows: list[dict]) -> dict[str, dict]:
    # The same rule the census summary applies, by the same function: at most one pair
    # per class per package. This module's own docstring promises it ("The census takes
    # at most one pair per class per package"), and the promise was kept by the planner
    # rather than by anything that reads the file.
    rows, _ = one_pair_per_package(rows)
    out: dict[str, dict] = {}
    for cohort in ORDER:
        mine = [r for r in rows if r.get("cohort") == cohort]
        ok = [r for r in mine if "error" not in r]
        failed = len(mine) - len(ok)
        broke = [r for r in ok if any(r.get(k) for k in BREAKING)]

        symbols = sum(symbols_in(r, BREAKING) for r in ok)
        # The largest single pair, and what share of the cohort's symbols it owns. A sum
        # over a cohort is a number one package can own: 1,240 of the patch cohort's
        # 1,539 symbols were one ansible pair, and the published 0.68% did not hint at it.
        biggest = max(ok, key=lambda r: symbols_in(r, BREAKING), default=None)
        top_symbols = symbols_in(biggest, BREAKING) if biggest is not None else 0
        exported = sum(r.get("exported_symbols", 0) for r in ok)
        widened = sum(symbols_in(r, ("exported_widened",)) for r in ok)
        if not mine:
            continue
        out[cohort] = {
            "pairs": len(ok),
            "packages": len({r["package"] for r in ok}),
            "unmeasured": failed,
            "broke": len(broke),
            "broke_rate": (len(broke) / len(ok)) if len(ok) >= MIN_FOR_RATE else None,
            "broken_symbols": symbols,
            "largest_pair": (
                f"{biggest['package']} {biggest['old']}->{biggest['new']}"
                if biggest is not None and top_symbols
                else None
            ),
            "largest_pair_symbols": top_symbols,
            "largest_pair_share": (top_symbols / symbols) if symbols else None,
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

    # Either the raw census or the committed ledger: the ledger holds counts where the
    # census holds names, and nothing here reads a name.
    if args.census.suffix == ".gz":
        with gzip.open(args.census, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
        rows = data["rows"]
    else:
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
        "| Bump | Pairs | Packages | Broke exported API | Broken symbols "
        "| Of exported symbols | Largest pair's share | Widened |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cohort in ORDER:
        if cohort not in table:
            continue
        v = table[cohort]
        rate = f"{v['symbol_rate'] * 100:.2f}%" if v["symbol_rate"] is not None else "-"
        share = (
            f"{v['largest_pair_share']:.0%} ({v['largest_pair']})"
            if v["largest_pair_share"] is not None
            else "-"
        )
        lines.append(
            f"| **{cohort}** | {v['pairs']:,} | {v['packages']:,} | "
            f"{fmt_rate(v['broke_rate'], v['broke'], v['pairs'])} | {v['broken_symbols']:,} | "
            f"{v['broken_symbols']:,} of {v['exported_symbols']:,} ({rate}) | {share} | "
            f"{v['widened']:,} |"
        )

    thin = [c for c, v in table.items() if v["broke_rate"] is None]
    if thin:
        lines += [
            "",
            f"No percentage is given for {', '.join(thin)}: under {MIN_FOR_RATE} "
            "measured pairs, a rate would read as precision the sample cannot carry.",
        ]

    why = collections.Counter(str(r["error"]).split(":")[0][:48] for r in rows if "error" in r)
    if why:
        lines += [
            "",
            "Pairs that could not be measured, by reason:",
            "",
            "| Reason | Pairs |",
            "|---|---:|",
        ]
        # Sorted by count and then by reason, not most_common(): ties come out in
        # insertion order, so the same census summarised from two files that hold the
        # same rows in a different order printed the same numbers in a different order.
        ranked = sorted(why.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        lines += [f"| {reason} | {count:,} |" for reason, count in ranked]

    text = "\n".join(lines)
    print(text)
    if args.md:
        args.md.write_text(text + "\n", encoding="utf-8")
        print(f"\nwritten to {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
