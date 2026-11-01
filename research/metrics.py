"""Distributional analysis of the release-pair census, with intervals.

The README publishes six rates. A reviewer's first three questions about a rate are what
its interval is, whether the differences between cohorts survive it, and what the
distribution behind it looks like - and a cohort rate hides all three.

What is computed here, all from `docs/census-rows.json.gz`, so it rebuilds from the
repository without the 4.3 MB artefact:

  * **Wilson score intervals** on every rate. Not normal-approximation: with 8 breaking
    pairs in 49 (the 0.0.z cohort) the normal interval runs past zero, and an interval
    that includes an impossible value is not an interval.
  * **Fisher's exact test** between cohorts, pairwise, with Holm correction across the
    fifteen comparisons. The claim the census makes is ordinal - patch breaks less than
    minor breaks less than major - and that claim needs the pairwise tests, not three
    percentages next to each other.
  * **breakage against surface size**, because "big APIs break more" is the obvious
    confound: a package exporting 3,000 names has more chances to lose one than a package
    exporting 12, and if the cohorts differ in surface size the cohort rates are partly
    measuring that.
  * **the concentration of broken symbols**, as a Lorenz curve and a Gini coefficient. One
    pair owns 49% of the patch cohort's symbols; a sum over a cohort is a number one
    package can own, and this is how to say that quantitatively instead of in a sentence.

No release-cadence analysis: the ledger records versions, not dates, so cadence would
need PyPI metadata the committed artefact does not carry. Stated rather than approximated.
"""

from __future__ import annotations

import gzip
import json
import math
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "docs" / "census-rows.json.gz"

BREAKING = ("exported_gone", "exported_reshaped")
COHORTS = ["patch", "minor", "major", "0.x-patch", "0.x-breaking", "0.0.z"]
# Surface-size buckets, by exported symbol count. Log-ish, because the distribution is:
# the median package exports tens of names and the largest exports thousands.
BUCKETS = [(0, 10), (10, 30), (30, 100), (100, 300), (300, 1000), (1000, 10**9)]


def load(path: Path = LEDGER) -> list[dict]:
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))["rows"]


def eligible(rows: list[dict]) -> list[dict]:
    """The rows the published summary computes over - its two filters, not a guess.

    `census_summary.summarise` drops pairs whose `new` version is older than its `old`
    (PyPI orders by upload date, not by version) and then keeps at most one pair per
    package per cohort. This module loaded the ledger raw and so measured 2,997 pairs
    where the published summary measures 2,937 - a different population, with patch at
    23/830 instead of 20/808. Every figure built on it described numbers the paper does
    not quote.

    Imported rather than reimplemented: a second copy of a population rule drifts from
    the first, and the copy is the one nobody notices is wrong.
    """
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from semver_sweep import is_upgrade, one_pair_per_package  # noqa: PLC0415

    kept = [r for r in rows if is_upgrade(r.get("old", ""), r.get("new", ""))]
    kept, _ = one_pair_per_package(kept)
    return kept


def measured(rows: list[dict]) -> list[dict]:
    """Rows that produced a surface comparison, after the summary's own filters."""
    return [r for r in eligible(rows) if not r.get("error") and r.get("module")]


def broke(row: dict) -> bool:
    return any(row.get(k) for k in BREAKING)


def symbols(row: dict) -> int:
    total = 0
    for key in BREAKING:
        value = row.get(key)
        if value:
            total += value if isinstance(value, int) else len(value)
    return total


# --- intervals -----------------------------------------------------------------------


def wilson(successes: int, n: int, z: float = 1.96) -> dict | None:
    """Wilson score interval for a proportion.

    Not the normal approximation. With 8 of 49 the normal interval reaches below zero,
    and an interval containing an impossible value is not an interval. Wilson stays
    inside [0, 1] and behaves at small n and at rates near the boundary, which is most of
    this census: the patch cohort is 20 of 808.
    """
    if n == 0:
        return None
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return {
        "rate": round(p, 4),
        "lo": round(max(0.0, centre - half), 4),
        "hi": round(min(1.0, centre + half), 4),
        "n": n,
        "successes": successes,
    }


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher's exact p for the 2x2 table [[a, b], [c, d]].

    Exact rather than chi-square: the 0.0.z cohort has 49 measured pairs and chi-square
    is not trustworthy at those counts. Computed by summing hypergeometric probabilities
    no more likely than the observed table, which is the two-sided definition that does
    not depend on an arbitrary choice of tail.
    """
    total = a + b + c + d
    row1, col1 = a + b, a + c

    def probability(x: int) -> float:
        return math.exp(
            math.lgamma(row1 + 1)
            + math.lgamma(total - row1 + 1)
            + math.lgamma(col1 + 1)
            + math.lgamma(total - col1 + 1)
            - math.lgamma(total + 1)
            - math.lgamma(x + 1)
            - math.lgamma(row1 - x + 1)
            - math.lgamma(col1 - x + 1)
            - math.lgamma(total - row1 - col1 + x + 1)
        )

    observed = probability(a)
    lo = max(0, row1 + col1 - total)
    hi = min(row1, col1)
    # 1e-12 slack: two tables of equal probability must both be counted, and floating
    # point makes an exact equality test miss one of them.
    return min(
        1.0,
        sum(probability(x) for x in range(lo, hi + 1) if probability(x) <= observed * (1 + 1e-12)),
    )


def holm(pairs: dict[str, float]) -> dict[str, dict]:
    """Holm-Bonferroni over a family of p-values.

    Fifteen pairwise cohort comparisons means fifteen chances to find a difference that
    is not there. Holm rather than Bonferroni: same guarantee, uniformly more power, and
    no reason to prefer the weaker one.
    """
    ordered = sorted(pairs.items(), key=lambda kv: kv[1])
    m = len(ordered)
    out: dict[str, dict] = {}
    running = 0.0
    for i, (name, p) in enumerate(ordered):
        adjusted = min(1.0, max(running, (m - i) * p))
        running = adjusted
        out[name] = {
            "p": p,
            "p_holm": adjusted,
            "significant_at_05": adjusted < 0.05,
        }
    return out


# --- distribution ----------------------------------------------------------------


def by_cohort(rows: list[dict]) -> dict:
    out = {}
    for cohort in COHORTS:
        mine = [r for r in rows if r.get("cohort") == cohort]
        out[cohort] = wilson(sum(1 for r in mine if broke(r)), len(mine))
    return out


def cohort_tests(rows: list[dict]) -> dict:
    """Every pair of cohorts, Fisher exact, Holm-corrected."""
    counts = {}
    for cohort in COHORTS:
        mine = [r for r in rows if r.get("cohort") == cohort]
        counts[cohort] = (sum(1 for r in mine if broke(r)), len(mine))
    raw = {}
    for i, one in enumerate(COHORTS):
        for other in COHORTS[i + 1 :]:
            a, n1 = counts[one]
            b, n2 = counts[other]
            raw[f"{one} vs {other}"] = fisher_exact(a, n1 - a, b, n2 - b)
    return {"counts": counts, "tests": holm(raw)}


def by_surface_size(rows: list[dict]) -> dict:
    """Breakage rate against how many names the package exports.

    The obvious confound for every cohort rate: a package exporting 3,000 names has more
    opportunities to lose one than a package exporting 12. If the cohorts differ in
    surface size, their rates are partly measuring that instead of release discipline.
    """
    out = {}
    for low, high in BUCKETS:
        mine = [r for r in rows if low <= r.get("exported_symbols", 0) < high]
        label = f"{low}-{high}" if high < 10**9 else f"{low}+"
        out[label] = wilson(sum(1 for r in mine if broke(r)), len(mine))
    return out


def surface_size_distribution(rows: list[dict]) -> dict:
    sizes = sorted(r.get("exported_symbols", 0) for r in rows)
    if not sizes:
        return {}

    def quantile(q: float) -> int:
        return sizes[min(len(sizes) - 1, int(q * len(sizes)))]

    return {
        "n": len(sizes),
        "min": sizes[0],
        "p25": quantile(0.25),
        "median": quantile(0.5),
        "p75": quantile(0.75),
        "p95": quantile(0.95),
        "max": sizes[-1],
        "mean": round(sum(sizes) / len(sizes), 1),
    }


def concentration(rows: list[dict], cohort: str | None = None) -> dict:
    """How much of the broken-symbol total belongs to how few pairs.

    A Lorenz curve and a Gini coefficient. The README already names one pair that owns
    49% of a cohort's symbols; this is the same fact as a number, so a reader does not
    have to take the sentence on trust.
    """
    mine = rows if cohort is None else [r for r in rows if r.get("cohort") == cohort]
    counts = sorted((symbols(r) for r in mine if symbols(r)), reverse=True)
    total = sum(counts)
    if not counts or not total:
        return {"pairs_with_a_broken_symbol": 0, "symbols": 0}
    cumulative, running = [], 0
    for value in counts:
        running += value
        cumulative.append(running / total)
    # Gini over the pairs that broke something, ascending.
    ascending = sorted(counts)
    n = len(ascending)
    gini = (2 * sum((i + 1) * v for i, v in enumerate(ascending))) / (n * total) - (n + 1) / n
    return {
        "cohort": cohort or "all",
        "pairs_with_a_broken_symbol": n,
        "symbols": total,
        "largest_pair_share": round(counts[0] / total, 4),
        "top_1_percent_share": round(cumulative[max(0, int(0.01 * n) - 1)], 4),
        "top_10_percent_share": round(cumulative[max(0, int(0.10 * n) - 1)], 4),
        "gini": round(gini, 4),
        "lorenz": [round(v, 4) for v in cumulative],
    }


def unmeasured_reasons(rows: list[dict]) -> dict:
    """Why a pair produced no comparison, as counts.

    768 of 3,705 rows. A census that reports only the measured rows is reporting a
    denominator it chose.
    """
    counted = Counter()
    for row in rows:
        if row.get("error"):
            counted[str(row["error"]).split(":")[0][:60]] += 1
    return dict(counted.most_common())
