"""Two numbers the published literature can be compared against. Both from the census.

The paper reports a rate per version-bump cohort - 2.5% for patch, 41.5% for major - which
is the right shape for the question it asks and the wrong shape for comparing against the
semantic-versioning literature. That literature asks one aggregate question: of the
upgrades whose version number *promises* compatibility, how many break it? Three studies
answer it, and they disagree:

* Raemaekers, van Deursen & Visser (2014, 2017), on Maven Central: **29.0%** of non-major
  upgrades introduce a breaking change, 30.0% over all levels. Not read directly; these
  are the replication's reproduction of their results, Ochoa et al. Table 5.1.
* Ochoa, Degueule, Falleri & Vinju, *Breaking Bad? Semantic Versioning and Impact of
  Breaking Changes in Maven Central* (2021), a larger replication over 119,879 upgrades:
  **20.1% of non-major upgrades break**, stated outright. Its 83.4% compliance figure is
  over *all* upgrades, so deriving the non-major rate as 100% less 83.4% - which an
  earlier version of this file did, giving 16.6% - uses the wrong denominator. Per level:
  major 61.8%, minor 37.9%, patch 14.6%. Separately, **only 7.9% of clients are actually
  affected**, because most breaking changes touch declarations no client uses.
* Li, Wu, Fu & Zhou, on the Go ecosystem (2023): **28.6%** of non-major upgrades introduce
  breaking changes over 124K libraries and 532K clients, and **33.3%** of downstream
  clients may be affected. The 7.9% client figure above is Maven's, not Go's; an earlier
  version of this file attached it to this bullet.

`semver_compliance()` computes the first quantity for PyPI, over the same population and
with the same predicate as the published per-cohort rates, so the comparison is a
comparison and not a near-miss. Every one of those studies diffs APIs statically; this
census executes them, which should make it find *more*, and the direction of any gap is
part of the result.

`caller_impact()` computes the second. An API-level rate counts a pair as broken when one
exported name of eight hundred disappeared, which is not what a caller experiences. For a
caller using `k` of a module's exported names, the chance that at least one of them is
among the broken ones is, under uniform choice without replacement,

    1 - C(exported - broken, k) / C(exported, k)

That is a model, and its assumption is the thing to attack: real callers do not pick
symbols uniformly, they pick the popular ones, and breakage is concentrated in exactly the
long tail that nobody imports (which the paper's Gini of 0.887 already says). So this is an
estimate of caller impact under uniform use, it is reported as such, and the direction of
its error is stated rather than left to the reader. It is computed because an API-level rate
with no caller-level counterpart invites the reader to treat 15.6% as the chance their
upgrade breaks, and it is not that.

    .venv/Scripts/python research/semver_compliance.py

Writes research/results/semver_compliance.json.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "research"))

import metrics  # noqa: E402

RESULTS = ROOT / "research" / "results"

# Which cohorts carry a compatibility promise. Under semantic versioning 2.0 a major bump
# is allowed to break anything, and so is a minor bump below 1.0 - "anything MAY change at
# any time" for 0.y.z - so neither belongs in a compliance rate. Including `major` would
# make the figure meaningless and including `0.x-breaking` would punish maintainers for a
# licence the specification grants them.
PROMISING = ("patch", "minor", "0.x-patch")
NOT_PROMISING = ("major", "0.x-breaking")

# `0.0.z` is deliberately in neither. Under 0.0.z every release may break, so it promises
# nothing - but it is also the cohort where a maintainer is least likely to have read the
# specification, and folding 70 pairs of it into either side would move a rate without
# anyone being able to say which way it should have moved. Counted and set aside.
UNCLASSIFIED = ("0.0.z",)

# The published comparisons, with what each one measured, because the numbers are not
# measuring the same thing and a table of four percentages would imply they are.
# Every rate here was checked against its source in November 2026 and two of the three
# moved. The Maven pair had been taken from search summaries: one was a figure (14.5%)
# that appears nowhere in either Maven paper's results - the nearest number in the
# replication's Table 5.1 is 14.8%, which is the *share of upgrades that are major*, not
# a breaking rate - and the other was derived by subtracting a compliance figure whose
# denominator is all upgrades from 100%. Both are replaced by figures the sources state
# outright. Raemaekers et al. themselves remain unread; their rows come from the
# replication's reproduction of their results in Ochoa et al., Table 5.1.
PUBLISHED = [
    {
        "study": "Raemaekers et al. (2014, 2017), via Ochoa et al. Table 5.1",
        "ecosystem": "Maven Central (Java)",
        "rate": 0.290,
        "quantity": "non-major upgrades introducing a breaking change, 68,697 upgrades",
        "method": "static API diff",
    },
    {
        "study": "Ochoa et al. (2021), Breaking Bad?",
        "ecosystem": "Maven Central (Java)",
        "rate": 0.201,
        "quantity": "non-major upgrades that break, stated directly, 98,591 upgrades",
        "method": "static API diff",
    },
    {
        "study": "Ochoa et al. (2021), Breaking Bad?",
        "ecosystem": "Maven Central (Java)",
        "rate": 0.146,
        "quantity": "patch upgrades that break, 75,282 upgrades",
        "method": "static API diff",
    },
    {
        "study": "Li et al. (2023)",
        "ecosystem": "Go",
        "rate": 0.286,
        "quantity": "minor and patch upgrades introducing a breaking change",
        "method": "static API diff",
    },
]

# Symbol counts a caller might plausibly import from one module. Three numbers rather than
# one, because the impact estimate is far more sensitive to this than to anything measured.
CALLER_SIZES = (1, 3, 10)


def _wilson(k: int, n: int) -> dict | None:
    return metrics.wilson(k, n) if n else None


def _broken_symbols(row: dict) -> int:
    return metrics.symbols(row)


def semver_compliance(rows: list[dict]) -> dict:
    """Breakage among the upgrades whose version number promised compatibility."""
    measured = metrics.measured(rows)
    by_cohort = {}
    for cohort in PROMISING + NOT_PROMISING + UNCLASSIFIED:
        in_cohort = [r for r in measured if r.get("cohort") == cohort]
        broke = [r for r in in_cohort if metrics.broke(r)]
        by_cohort[cohort] = {
            "pairs": len(in_cohort),
            "broke": len(broke),
            "rate": _wilson(len(broke), len(in_cohort)),
        }

    promising = [r for r in measured if r.get("cohort") in PROMISING]
    promising_broke = [r for r in promising if metrics.broke(r)]
    # The same question without the 0.x pairs, because two of the three published studies
    # are over Maven, where a 0.x convention of this kind does not operate. A reader
    # comparing against them should be able to see both.
    one_plus = [r for r in promising if not str(r.get("cohort", "")).startswith("0.")]
    one_plus_broke = [r for r in one_plus if metrics.broke(r)]

    return {
        "promising_cohorts": list(PROMISING),
        "set_aside": {c: by_cohort[c]["pairs"] for c in UNCLASSIFIED},
        "by_cohort": by_cohort,
        "compliance_rate": {
            "pairs": len(promising),
            "broke": len(promising_broke),
            "rate": _wilson(len(promising_broke), len(promising)),
        },
        "compliance_rate_excluding_0x": {
            "pairs": len(one_plus),
            "broke": len(one_plus_broke),
            "rate": _wilson(len(one_plus_broke), len(one_plus)),
        },
        "published": PUBLISHED,
    }


def _at_least_one(exported: int, broken: int, k: int) -> float | None:
    """Chance a caller using `k` of `exported` names touches one of the `broken` ones.

    `None` when the question is ill-posed: a caller cannot import ten names from a module
    exporting three, and answering 1.0 there would quietly turn "we do not know" into the
    most alarming possible number.
    """
    if exported <= 0 or k <= 0 or k > exported:
        return None
    broken = min(broken, exported)
    if broken <= 0:
        return 0.0
    safe = exported - broken
    if k > safe:
        return 1.0
    return 1.0 - math.comb(safe, k) / math.comb(exported, k)


def caller_impact(rows: list[dict]) -> dict:
    """Caller-level breakage under uniform symbol choice, per cohort and overall."""
    all_measured = metrics.measured(rows)
    measured = [r for r in all_measured if (r.get("exported_symbols") or 0) > 0]
    out: dict = {
        "caller_sizes": list(CALLER_SIZES),
        # The model needs a denominator, so a pair exporting nothing has to be dropped -
        # and dropping it raises every rate below, because a module with no exported
        # names cannot break one. Recorded, because an 8.9% here beside the 8.1% in
        # `semver_compliance` is two rates for nearly one quantity, and a reader who
        # cannot see which pairs differ has to assume one of them is wrong.
        "pairs_measured": len(all_measured),
        "pairs_with_an_exported_surface": len(measured),
        "pairs_exporting_nothing": len(all_measured) - len(measured),
        "why_the_api_level_rate_differs": (
            "Pairs exporting no names are dropped here and counted in "
            "`semver_compliance`. None of them can break an exported name, so excluding "
            "them raises the API-level rate; the difference is exactly those pairs."
        ),
        "by_cohort": {},
        "overall": {},
    }

    def summarise(subset: list[dict]) -> dict:
        row: dict = {"pairs": len(subset)}
        if not subset:
            return row
        row["api_level_rate"] = _wilson(sum(1 for r in subset if metrics.broke(r)), len(subset))
        # The share of each pair's exported surface that broke, which is the quantity the
        # model consumes and worth publishing on its own: it is what separates "removed
        # one name of eight hundred" from "removed three hundred of five hundred".
        shares = [_broken_symbols(r) / r["exported_symbols"] for r in subset if metrics.broke(r)]
        row["broken_surface_share"] = (
            {
                "n": len(shares),
                "mean": round(sum(shares) / len(shares), 4),
                "median": round(sorted(shares)[len(shares) // 2], 4),
                "max": round(max(shares), 4),
            }
            if shares
            else None
        )
        for k in CALLER_SIZES:
            per_pair = [
                p
                for r in subset
                if (p := _at_least_one(r["exported_symbols"], _broken_symbols(r), k)) is not None
            ]
            row[f"expected_caller_rate_k{k}"] = (
                round(sum(per_pair) / len(per_pair), 4) if per_pair else None
            )
            # Pairs where the question could not be asked, named rather than dropped
            # silently: a module exporting fewer names than the caller imports.
            row[f"pairs_too_small_for_k{k}"] = len(subset) - len(per_pair)
        return row

    for cohort in PROMISING + NOT_PROMISING:
        out["by_cohort"][cohort] = summarise([r for r in measured if r.get("cohort") == cohort])
    out["overall"] = summarise(measured)
    out["promising"] = summarise([r for r in measured if r.get("cohort") in PROMISING])
    out["comparison"] = {
        "study": "Li et al. (2023), Go",
        "reported_client_impact": 0.079,
        "quantity": "share of client programs affected by a breaking change",
        "why_not_the_same": (
            "Theirs is measured against real client code; this is an estimate under "
            "uniform symbol choice. Breakage is concentrated in the unused tail (Gini "
            "0.887), and real callers import the popular names, so uniform choice "
            "overstates the chance a caller is hit."
        ),
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=RESULTS / "semver_compliance.json")
    args = parser.parse_args()

    rows = metrics.load()
    payload = {
        "what": (
            "The semantic-versioning compliance rate for PyPI, comparable to the "
            "published Maven and Go figures, and a caller-level impact estimate under "
            "uniform symbol choice. Same population and predicate as the paper's "
            "per-cohort rates."
        ),
        "semver_compliance": semver_compliance(rows),
        "caller_impact": caller_impact(rows),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    comp = payload["semver_compliance"]
    print("Upgrades whose version number promised compatibility:")
    for cohort in PROMISING:
        row = comp["by_cohort"][cohort]
        rate = row["rate"]
        print(
            f"  {cohort:12} {row['broke']:4} of {row['pairs']:5} broke"
            + (f"  {rate['rate']:.1%} [{rate['lo']:.1%}, {rate['hi']:.1%}]" if rate else "")
        )
    overall = comp["compliance_rate"]
    print(
        f"  {'TOTAL':12} {overall['broke']:4} of {overall['pairs']:5} broke"
        f"  {overall['rate']['rate']:.1%} "
        f"[{overall['rate']['lo']:.1%}, {overall['rate']['hi']:.1%}]"
    )
    no0x = comp["compliance_rate_excluding_0x"]
    print(
        f"  {'1.0+ only':12} {no0x['broke']:4} of {no0x['pairs']:5} broke"
        f"  {no0x['rate']['rate']:.1%} "
        f"[{no0x['rate']['lo']:.1%}, {no0x['rate']['hi']:.1%}]"
    )
    print("\nPublished, for comparison (all static API diffs):")
    for study in PUBLISHED:
        print(f"  {study['rate']:.1%}  {study['study']} - {study['ecosystem']}")

    print("\nCaller-level estimate under uniform symbol choice:")
    impact = payload["caller_impact"]["promising"]
    print(f"  API-level rate over the same pairs: {impact['api_level_rate']['rate']:.1%}")
    for k in CALLER_SIZES:
        value = impact[f"expected_caller_rate_k{k}"]
        print(f"  a caller importing {k:2} name(s): {value:.2%}" if value is not None else "")
    share = impact["broken_surface_share"]
    if share:
        print(
            f"  of a broken pair's exported surface, median {share['median']:.2%} broke "
            f"(mean {share['mean']:.2%}, max {share['max']:.1%})"
        )
    print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
