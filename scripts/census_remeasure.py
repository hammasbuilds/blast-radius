"""Re-measure named pairs with the current code and replace their rows in a census.

A census takes about a day, so a defect found after it finishes is not a reason to throw
the whole file away - but it is a reason to say which rows were measured by which code.
This re-measures the pairs you name, replaces their rows, and stamps each replaced row
with `remeasured_at` so the file states plainly that it is not all one revision.

    python scripts/census_remeasure.py --revision 7f3a1c2 --pair ansible 14.3.0 14.3.1
    python scripts/census_remeasure.py --revision 7f3a1c2 --self-repeating

`--self-repeating` selects the rows that carry a dotted path with a repeated segment,
which is what a submodule walk that descends into a directory twice produces. That
selection is a query, not a judgement: it prints what it matched and re-measures exactly
those.

A pair that cannot be measured now is written as the error it gave, not left as the old
row - an old row kept because the new measurement failed is the worst of both.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_census import _pin_temp_to_scratch_drive  # noqa: E402
from semver_sweep import measure  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

SYMBOL_KEYS = (
    "exported_gone", "exported_reshaped", "exported_widened",
    "internal_gone", "internal_reshaped", "internal_widened",
)


def self_repeating(name: str) -> bool:
    """A dotted path that walks through the same directory twice.

    A repeated BLOCK, not a repeated segment:
    `ansible_collections.infinidat.infinibox.infinidat.infinibox...` repeats the same two
    segments twenty times over, where
    `cfnlint.rules...DomainValidationOptions.DomainValidationOptions.check_value` is a
    class with the same name as its module, which is ordinary Python. Matching single
    repeats selected four rows that were never recursive, and they were re-measured for
    nothing.

    A long name on its own is not the tell either: some real packages nest deeply.
    """
    parts = name.split(".")
    if len(parts) <= 6:
        return False
    grams = [tuple(parts[i:i + 2]) for i in range(len(parts) - 1)]
    return len(set(grams)) != len(grams)


def _timed_out_installing(row: dict) -> bool:
    error = row.get("error") or ""
    return error.startswith("install ") and "did not finish within" in error


def rows_with_self_repeating_names(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        hits = sum(1 for k in SYMBOL_KEYS for n in row.get(k, []) if self_repeating(n))
        if hits:
            out.append({**row, "_hits": hits})
    return sorted(out, key=lambda r: -r["_hits"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--census", type=Path, default=ROOT / "docs" / "pair-census.json")
    ap.add_argument("--revision", required=True,
                    help="the commit the re-measurement runs at; stamped on each row")
    ap.add_argument("--pair", nargs=3, metavar=("PACKAGE", "OLD", "NEW"), action="append",
                    default=[], help="a pair to re-measure; repeatable")
    ap.add_argument("--self-repeating", action="store_true",
                    help="also re-measure every row carrying a self-repeating dotted path")
    ap.add_argument("--dry-run", action="store_true", help="print what would be re-measured")
    ap.add_argument("--install-timeout", type=float, default=600.0,
                    help="seconds allowed for installing each version (default 600). "
                         "ansible and sagemaker need several times that on a slow "
                         "connection, and a timeout would otherwise replace a measured "
                         "row with an error about this machine")
    args = ap.parse_args()

    # Before the first install. Without it `tempfile` follows TMPDIR, which is on C:
    # here, and each re-measured ansible version is about half a gigabyte - the reason
    # C: went from 58 GB free to 31 over one day's measurements.
    _pin_temp_to_scratch_drive()

    census = json.loads(args.census.read_text(encoding="utf-8"))
    rows = census["results"]
    index = {(r["package"], r["old"], r["new"]): i for i, r in enumerate(rows)}

    wanted: list[tuple[str, str, str]] = [tuple(p) for p in args.pair]
    if args.self_repeating:
        for row in rows_with_self_repeating_names(rows):
            key = (row["package"], row["old"], row["new"])
            print(f"  self-repeating names: {row['_hits']:>5}  "
                  f"{key[0]} {key[1]}->{key[2]} ({row['cohort']})")
            if key not in wanted:
                wanted.append(key)
    if not wanted:
        print("nothing selected")
        return 0
    missing = [k for k in wanted if k not in index]
    if missing:
        raise SystemExit(f"not in the census: {missing}")
    print(f"\n{len(wanted)} pair(s) to re-measure at {args.revision}")
    if args.dry_run:
        return 0

    for n, key in enumerate(wanted, 1):
        package, old, new = key
        was = rows[index[key]]
        before = {k: len(was.get(k, [])) for k in SYMBOL_KEYS if was.get(k)}
        print(f"\n[{n}/{len(wanted)}] {package} {old}->{new}: was {before or 'nothing'}",
              flush=True)
        start = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            fresh = measure(package, old, new, Path(tmp),
                            install_timeout=args.install_timeout)
        row = {"package": package, "old": old, "new": new, "cohort": was["cohort"],
               **fresh, "remeasured_at": args.revision}
        after = {k: len(row.get(k, [])) for k in SYMBOL_KEYS if row.get(k)}
        if _timed_out_installing(fresh) and not was.get("error"):
            # The old row stays. It was measured, this one was not, and the difference
            # between them is this machine's network - which is not a fact about the
            # upgrade. Recorded on the row so the file says the pair was tried.
            row = {**was, "remeasure_failed": fresh.get("error", "install timed out"),
                   "remeasure_attempted_at": args.revision}
            print(f"            install timed out; keeping the measured row "
                  f"({time.time() - start:.0f}s)", flush=True)
        else:
            print(f"            now {after or row.get('error') or 'nothing'} "
                  f"({time.time() - start:.0f}s)", flush=True)
        rows[index[key]] = row
        # Written after every pair: these installs are large, and a run interrupted
        # halfway should leave the rows it finished replaced rather than all of them
        # unreplaced.
        args.census.write_text(json.dumps(census, indent=1), encoding="utf-8")

    stamped = sum(1 for r in rows if r.get("remeasured_at"))
    kept = sum(1 for r in rows if r.get("remeasure_failed"))
    print(f"\n{stamped} of {len(rows)} rows now carry remeasured_at; "
          f"{kept} kept their old measurement because the install timed out")
    print(f"written to {args.census.name} - re-run scripts/census_summary.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
