"""Re-run the semver sweep behind docs/SEMVER.md.

    python scripts/semver_sweep.py [--out sweep.json]

For each upgrade pair: install both versions, read both public surfaces, and count
the exported symbols that became GONE or RESHAPED (breaking) versus WIDENED (a
signature that grew in a way every existing call survives, which is not a break).
API surface only - the behaviour pass is not run here.

Installs honour UV_OFFLINE=1, so a run against a warm uv cache needs no network.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blast_radius.cli import import_plan, install  # noqa: E402
from blast_radius.diff import api_changes, gone_candidates  # noqa: E402
from blast_radius.probe import resolve_names, surface  # noqa: E402
from blast_radius.types import Kind  # noqa: E402

PAIRS = [
    ("beautifulsoup4", "4.12.2", "4.12.3", "patch"),
    ("charset-normalizer", "3.3.0", "3.3.2", "patch"),
    ("click", "8.1.6", "8.1.7", "patch"),
    ("coverage", "7.5.0", "7.5.4", "patch"),
    ("filelock", "3.15.1", "3.15.4", "patch"),
    ("httpx", "0.27.0", "0.27.2", "patch"),
    ("jinja2", "3.1.2", "3.1.4", "patch"),
    ("markupsafe", "2.1.3", "2.1.5", "patch"),
    ("platformdirs", "4.2.0", "4.2.2", "patch"),
    ("pytest", "8.2.0", "8.2.2", "patch"),
    ("requests", "2.32.2", "2.32.3", "patch"),
    ("rich", "13.7.0", "13.7.1", "patch"),
    ("tqdm", "4.66.1", "4.66.4", "patch"),
    ("typer", "0.12.0", "0.12.3", "patch"),
    ("urllib3", "2.2.1", "2.2.2", "patch"),
    ("anyio", "4.1.0", "4.4.0", "minor"),
    ("click", "8.0.4", "8.1.7", "minor"),
    ("filelock", "3.13.1", "3.15.4", "minor"),
    ("httpx", "0.26.0", "0.27.0", "minor"),
    ("pluggy", "1.4.0", "1.5.0", "minor"),
    ("rich", "13.0.1", "13.7.1", "minor"),
    ("starlette", "0.35.1", "0.37.2", "minor"),
    ("typer", "0.9.0", "0.12.3", "minor"),
    ("urllib3", "2.1.0", "2.2.2", "minor"),
    ("click", "7.1.2", "8.1.7", "major"),
    ("rich", "12.6.0", "13.7.1", "major"),
    ("urllib3", "1.26.18", "2.2.2", "major"),
]


def measure(package: str, old_v: str, new_v: str, work: Path) -> dict:
    for v in (old_v, new_v):
        ok, why = install(package, v, work / v)
        if not ok:
            return {"error": f"install {v}: {why.splitlines()[-1] if why else '?'}"}
    plan = import_plan(work / old_v, package)
    owned = sorted(set(plan.owned) | set(import_plan(work / new_v, package).owned))
    module = ",".join(plan.modules)
    old = surface(work / old_v, plan.modules, owned=owned)
    new = surface(work / new_v, plan.modules, owned=owned)
    if not old or not new or len(old) < 2 or len(new) < 2:
        return {"error": f"import {module} failed or found nothing"}
    exported = {k for k, v in old.items() if k != "__meta__" and v.get("exported")}
    resolved = resolve_names(work / new_v, gone_candidates(old, new))
    rows: dict[str, list[str]] = {}
    for c in api_changes(old, new, resolved):
        if c.kind is Kind.ADDED:
            continue
        scope = "exported" if c.qualname in exported else "internal"
        rows.setdefault(f"{scope}_{c.kind.value}", []).append(c.qualname)
    return {"module": module, "exported_symbols": len(exported), **rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sweep.json")
    args = ap.parse_args()
    results = []
    for package, old_v, new_v, bump in PAIRS:
        work = Path(tempfile.mkdtemp(prefix="sweep-"))
        try:
            row = {"package": package, "old": old_v, "new": new_v, "bump": bump}
            row.update(measure(package, old_v, new_v, work))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        results.append(row)
        broke = len(row.get("exported_gone", [])) + len(row.get("exported_reshaped", []))
        print(
            f"{bump:5} {package:20} {old_v:>8} -> {new_v:<8} "
            + (row["error"] if "error" in row else
               f"exported {row['exported_symbols']:4}  broken {broke:3}  "
               f"widened {len(row.get('exported_widened', [])):3}"),
            flush=True,
        )  # fmt: skip
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")

    print()
    for bump in ("patch", "minor", "major"):
        rows = [r for r in results if r["bump"] == bump and "error" not in r]
        broke = [r for r in rows if r.get("exported_gone") or r.get("exported_reshaped")]
        widened_only = [
            r for r in rows if r.get("exported_widened") and r not in broke
        ]  # fmt: skip
        n_sym = sum(len(r.get("exported_gone", [])) + len(r.get("exported_reshaped", []))
                    for r in rows)  # fmt: skip
        print(
            f"{bump:5}  pairs {len(rows):2}  broke exported API {len(broke):2}"
            f"  symbols {n_sym:3}  widened-only pairs {len(widened_only)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
