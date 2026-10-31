"""Figures for the census, one function each.

Same house rules as the sibling repositories: matplotlib only, Okabe-Ito colours,
nothing encoded by colour alone, PDF for the manuscript and PNG beside it, no chart junk,
and every caption states its n so a figure cannot reach a paper without its denominator.

Each function returns (pdf, png, caption).
"""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIGURES = ROOT / "research" / "figures"

BLUE, ORANGE, GREEN, GREY, PURPLE = "#0072B2", "#E69F00", "#009E73", "#999999", "#CC79A7"
# SemVer cohorts in blue, 0.x cohorts in orange: the promise differs, so the colour does.
COHORT_COLOUR = {
    "patch": BLUE, "minor": BLUE, "major": BLUE,
    "0.x-patch": ORANGE, "0.x-breaking": ORANGE, "0.0.z": ORANGE,
}
COHORT_HATCH = {
    "patch": "", "minor": "//", "major": "xx",
    "0.x-patch": "", "0.x-breaking": "//", "0.0.z": "xx",
}
ORDER = ["patch", "minor", "major", "0.x-patch", "0.x-breaking", "0.0.z"]


def _save(fig, name: str) -> tuple[Path, Path]:
    FIGURES.mkdir(parents=True, exist_ok=True)
    pdf, png = FIGURES / f"{name}.pdf", FIGURES / f"{name}.png"
    fig.savefig(pdf, bbox_inches="tight")
    fig.savefig(png, bbox_inches="tight", dpi=200)
    plt.close(fig)
    return pdf, png


def _bare(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


# --- 1. the headline, with intervals -------------------------------------------------


def rate_by_cohort_figure(cohorts: dict) -> tuple[Path, Path, str]:
    """Breakage rate per cohort with Wilson 95% intervals.

    The README prints six percentages. This is the same six with the error bars that say
    which of them are distinguishable - and the 0.0.z bar, 8 of 49, has an interval wide
    enough to make the point that a rate needs its n beside it.
    """
    present = [c for c in ORDER if cohorts.get(c)]
    rates = [cohorts[c]["rate"] for c in present]
    los = [cohorts[c]["rate"] - cohorts[c]["lo"] for c in present]
    his = [cohorts[c]["hi"] - cohorts[c]["rate"] for c in present]

    fig, ax = plt.subplots(figsize=(7.0, 4.0))
    for i, cohort in enumerate(present):
        ax.bar(i, rates[i], width=0.62, color=COHORT_COLOUR[cohort],
               hatch=COHORT_HATCH[cohort], edgecolor="white", linewidth=0.6)
    ax.errorbar(range(len(present)), rates, yerr=[los, his], fmt="none",
                ecolor="#333333", capsize=4, linewidth=1.2)
    for i, cohort in enumerate(present):
        row = cohorts[cohort]
        ax.text(i, row["hi"] + 0.012, f"{row['successes']}/{row['n']}",
                ha="center", fontsize=8, color="#333333")
    ax.set_xticks(range(len(present)))
    ax.set_xticklabels(present, fontsize=9, rotation=20, ha="right")
    ax.set_ylim(0, max(cohorts[c]["hi"] for c in present) + 0.06)
    ax.set_ylabel("share of pairs that broke an exported name", fontsize=10)
    _bare(ax)
    ax.text(0.99, 0.97, "blue: SemVer promises\norange: 0.x, no promise",
            transform=ax.transAxes, ha="right", va="top", fontsize=8, color=GREY)
    total = sum(cohorts[c]["n"] for c in present)
    caption = (
        f"Breakage rate by bump class over {total:,} measured release pairs, with Wilson "
        "95% intervals. Counts above each bar are breaking pairs over measured pairs. "
        "The y-axis starts at zero."
    )
    return (*_save(fig, "fig1_rate_by_cohort"), caption)


# --- 2. the confound -----------------------------------------------------------------


def surface_size_figure(rows: list[dict], buckets: dict) -> tuple[Path, Path, str]:
    """Breakage rate against exported surface size, over the size distribution.

    The obvious confound, and the figure that tests it: a package exporting thousands of
    names has more chances to lose one. If the rate rises with size, the cohort rates are
    partly measuring surface size.
    """
    labels = [k for k, v in buckets.items() if v and v["n"]]
    rates = [buckets[k]["rate"] for k in labels]
    los = [buckets[k]["rate"] - buckets[k]["lo"] for k in labels]
    his = [buckets[k]["hi"] - buckets[k]["rate"] for k in labels]
    ns = [buckets[k]["n"] for k in labels]

    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(7.0, 4.8), sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1.0], "hspace": 0.1},
    )
    top.errorbar(range(len(labels)), rates, yerr=[los, his], fmt="o-", color=BLUE,
                 ecolor="#333333", capsize=4, linewidth=2, markersize=6)
    top.set_ylabel("breakage rate", fontsize=10)
    top.set_ylim(0, max(buckets[k]["hi"] for k in labels) + 0.03)
    _bare(top)
    bottom.bar(range(len(labels)), ns, color=GREY, width=0.6)
    for i, n in enumerate(ns):
        bottom.text(i, n, f"{n:,}", ha="center", va="bottom", fontsize=7.5)
    bottom.set_ylabel("pairs", fontsize=9)
    bottom.set_xticks(range(len(labels)))
    bottom.set_xticklabels(labels, fontsize=9)
    bottom.set_xlabel("exported names in the old version", fontsize=10)
    _bare(bottom)
    first, last = rates[0], rates[-1]
    caption = (
        f"Breakage rate against the size of the exported surface, Wilson 95% intervals, "
        f"{sum(ns):,} measured pairs. The rate moves from {first:.1%} in the smallest "
        f"bucket to {last:.1%} in the largest, so surface size is a confound for any "
        "cohort comparison and is reported beside it rather than adjusted away."
    )
    return (*_save(fig, "fig2_rate_by_surface_size"), caption)


# --- 3. concentration ----------------------------------------------------------------


def lorenz_figure(concentrations: dict) -> tuple[Path, Path, str]:
    """Lorenz curves: what share of broken symbols belongs to what share of pairs.

    The quantitative form of "one pair owns 49% of that cohort". The diagonal is perfect
    equality; distance from it is the concentration.
    """
    fig, ax = plt.subplots(figsize=(5.6, 5.2))
    ax.plot([0, 1], [0, 1], color=GREY, linewidth=1, linestyle=":",
            label="equal contribution")
    shown = [c for c in ["all", *ORDER]
             if concentrations.get(c, {}).get("lorenz")]
    styles = {"all": ("#333333", "-", 2.2)}
    for cohort in shown:
        curve = concentrations[cohort]["lorenz"]
        n = len(curve)
        xs = [(i + 1) / n for i in range(n)]
        colour, style, width = styles.get(
            cohort, (COHORT_COLOUR.get(cohort, GREY), "-", 1.2)
        )
        ax.plot(xs, curve, color=colour, linestyle=style, linewidth=width,
                label=f"{cohort} (Gini {concentrations[cohort]['gini']:.2f})")
    ax.set_xlabel("share of breaking pairs, largest first", fontsize=10)
    ax.set_ylabel("cumulative share of broken symbols", fontsize=10)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    ax.legend(fontsize=7.5, frameon=False, loc="lower right")
    _bare(ax)
    overall = concentrations.get("all", {})
    caption = (
        "Concentration of broken exported symbols. Curves run from the largest "
        "contributing pair, so a curve that rises steeply is a cohort whose symbol total "
        f"belongs to a few pairs. Over all cohorts, {overall.get('top_10_percent_share', 0):.0%} "
        f"of symbols come from the top 10% of breaking pairs (Gini "
        f"{overall.get('gini', 0):.2f}, {overall.get('pairs_with_a_broken_symbol', 0):,} "
        "pairs). This is why the pair-level rate is the headline and the symbol-level rate "
        "is reported with its largest contributor named."
    )
    return (*_save(fig, "fig3_lorenz"), caption)


# --- 4. the distribution itself ------------------------------------------------------


def symbol_count_figure(rows: list[dict]) -> tuple[Path, Path, str]:
    """How many exported names a breaking pair loses, on a log axis.

    A heavy tail: most breaking pairs lose one or two names and a handful lose thousands.
    Plotted on a log x-axis because a linear one is a single bar at the left and one
    invisible mark at 2,941.
    """
    counts = []
    for row in rows:
        total = 0
        for key in ("exported_gone", "exported_reshaped"):
            value = row.get(key)
            if value:
                total += value if isinstance(value, int) else len(value)
        if total:
            counts.append(total)
    counts.sort()
    top = max(counts)
    edges = [1]
    while edges[-1] < top:
        edges.append(edges[-1] * 2)

    fig, ax = plt.subplots(figsize=(7.0, 4.0))
    ax.hist(counts, bins=edges, color=BLUE, edgecolor="white", linewidth=0.6)
    ax.set_xscale("log")
    ax.set_xlabel("exported names lost or reshaped by one pair", fontsize=10)
    ax.set_ylabel("pairs", fontsize=10)
    _bare(ax)
    median = counts[len(counts) // 2]
    one_or_two = sum(1 for c in counts if c <= 2) / len(counts)
    ax.axvline(median, color=ORANGE, linewidth=1.4, linestyle="--")
    ax.text(median * 1.15, ax.get_ylim()[1] * 0.9, f"median {median}",
            fontsize=8, color=ORANGE)
    caption = (
        f"Distribution of how much a breaking pair breaks, {len(counts):,} breaking pairs, "
        f"log x-axis with doubling bins. The median breaking pair loses {median} exported "
        f"name(s) and {one_or_two:.0%} lose two or fewer, while the largest loses "
        f"{top:,}. A mean over this distribution would describe no pair in it."
    )
    return (*_save(fig, "fig4_symbols_per_breaking_pair"), caption)


# --- 5. what could not be measured ---------------------------------------------------


def unmeasured_figure(reasons: dict, total_rows: int) -> tuple[Path, Path, str]:
    """Why a pair produced no comparison.

    A census that charts only its measured rows is charting a denominator it chose. The
    bar for "the surface probe did not finish" is the one that matters: those are pairs
    too large to walk in the time allowed, not packages without an API.
    """
    top = sorted(reasons.items(), key=lambda kv: -kv[1])[:8]
    labels = [k if len(k) <= 44 else k[:41] + "..." for k, _ in top]
    values = [v for _, v in top]

    fig, ax = plt.subplots(figsize=(7.6, 3.8))
    ax.barh(range(len(labels)), values, color=GREY, edgecolor="white", linewidth=0.6)
    for i, v in enumerate(values):
        ax.text(v + max(values) * 0.01, i, f"{v:,}", va="center", fontsize=8)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("pairs", fontsize=10)
    _bare(ax)
    unmeasured = sum(reasons.values())
    caption = (
        f"Why {unmeasured:,} of {total_rows:,} pairs produced no surface comparison, top "
        "eight reasons. Reported because a rate over the measured pairs alone is a rate "
        "over a denominator this census chose, and the reasons are not independent of the "
        "packages: a package with no importable module is a different exclusion from one "
        "whose walk did not finish."
    )
    return (*_save(fig, "fig5_unmeasured"), caption)
