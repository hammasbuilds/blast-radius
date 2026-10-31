"""Regenerate the census's statistical treatment and figures from the committed ledger.

    .venv-research/Scripts/python research/run_all.py

`.venv-research` is not in the repository (every `.venv*` is ignored) and holds only
matplotlib, so a fresh clone has to create it before this command can run. A sibling
repository had lost its copy without anything failing, because the committed figures still
looked fine:

    uv venv .venv-research --python 3.12
    uv pip install --python .venv-research/Scripts/python matplotlib==3.11.2

Reads `docs/census-rows.json.gz` only - 115 KB, in the repository - so every number and
figure rebuilds without the 4.3 MB raw census.

Nothing here ships: `research/` is in neither the wheel (`packages = ["src/blast_radius"]`)
nor the sdist's include list.
"""

from __future__ import annotations

import json
from pathlib import Path

import figures
import metrics

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "research" / "results"


def write(name: str, payload) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def main() -> int:
    rows = metrics.load()
    done = metrics.measured(rows)
    print(f"{len(rows):,} rows, {len(done):,} measured, "
          f"{len(rows) - len(done):,} not\n")

    # --- rates with intervals -------------------------------------------------
    cohorts = metrics.by_cohort(done)
    print("breakage rate by cohort, Wilson 95%:")
    for cohort, row in cohorts.items():
        if row:
            print(f"  {cohort:14} {row['successes']:>4}/{row['n']:<5} "
                  f"{row['rate']:.4f}  [{row['lo']:.4f}, {row['hi']:.4f}]  "
                  f"width {row['hi'] - row['lo']:.4f}")
    write("rate_by_cohort", cohorts)

    # --- are the cohorts actually different? ----------------------------------
    tests = metrics.cohort_tests(done)
    print("\npairwise Fisher exact, Holm-corrected over 15 comparisons:")
    for name, row in sorted(tests["tests"].items(), key=lambda kv: kv[1]["p_holm"]):
        mark = "yes" if row["significant_at_05"] else "NO "
        print(f"  {mark}  {name:30} p={row['p']:.3g}  Holm p={row['p_holm']:.3g}")
    write("cohort_tests", tests)
    not_separated = [n for n, r in tests["tests"].items() if not r["significant_at_05"]]
    if not_separated:
        print(f"\n  {len(not_separated)} of 15 cohort pairs are NOT separated at 0.05 "
              f"after correction")

    # --- the confound ---------------------------------------------------------
    sizes = metrics.surface_size_distribution(done)
    print(f"\nexported surface size: median {sizes['median']}, "
          f"p75 {sizes['p75']}, p95 {sizes['p95']}, max {sizes['max']:,} "
          f"(mean {sizes['mean']})")
    buckets = metrics.by_surface_size(done)
    print("breakage rate by surface size, Wilson 95%:")
    for label, row in buckets.items():
        if row and row["n"]:
            print(f"  {label:>10} exports  {row['successes']:>4}/{row['n']:<5} "
                  f"{row['rate']:.4f}  [{row['lo']:.4f}, {row['hi']:.4f}]")
    write("rate_by_surface_size", {"distribution": sizes, "buckets": buckets})

    # --- concentration --------------------------------------------------------
    print("\nconcentration of broken symbols:")
    concentrations = {}
    for cohort in [None, *metrics.COHORTS]:
        row = metrics.concentration(done, cohort)
        concentrations[row.get("cohort", "all")] = row
        if row.get("symbols"):
            print(f"  {row['cohort']:14} {row['symbols']:>6} symbol(s) over "
                  f"{row['pairs_with_a_broken_symbol']:>4} pair(s); largest pair "
                  f"{row['largest_pair_share']:.1%}, top 10% "
                  f"{row['top_10_percent_share']:.1%}, Gini {row['gini']:.3f}")
    write("concentration", concentrations)

    write("unmeasured_reasons", metrics.unmeasured_reasons(rows))

    # --- figures --------------------------------------------------------------
    print("\nfigures:")
    captions = {}
    made = [
        figures.rate_by_cohort_figure(cohorts),
        figures.surface_size_figure(done, buckets),
        figures.lorenz_figure(concentrations),
        figures.symbol_count_figure(done),
        figures.unmeasured_figure(metrics.unmeasured_reasons(rows), len(rows)),
    ]
    for pdf, png, caption in made:
        captions[pdf.stem] = caption
        print(f"  {pdf.name:34} {caption[:86]}...")
    write("captions", captions)
    print(f"\n{len(made)} figure(s) and 6 table(s) written to research/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
