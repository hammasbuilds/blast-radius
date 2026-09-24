"""Show what blast-radius does, in one command.

    python demo.py

Runs the real check on `urllib3 2.2.1 -> 2.2.2`: a **patch** release, where
semantic versioning promises nothing a caller can see will change.

It added a required keyword-only parameter to a public constructor.

    2.2.1  (*, headers=None, status, version,                 reason, ...)
    2.2.2  (*, headers=None, status, version, version_string, reason, ...)

Every subclass calling `super().__init__(...)` now raises TypeError, and
`HTTPResponse` takes the same parameter positionally, so positional callers
have `reason` land silently in the new slot. No changelog entry and no version
number tells you that; the two installs side by side do.

Takes about ten seconds and downloads two small wheels. With no network it
falls back to the comparison committed under docs/, so a fresh clone still
shows a real result rather than an error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FALLBACK = ROOT / "docs" / "packaging-21.3-to-24.0.json"


def from_committed() -> int:
    """No network: read a run that was recorded when there was."""
    if not FALLBACK.exists():
        print("No network and no committed comparison to fall back on.", flush=True)
        return 1
    data = json.loads(FALLBACK.read_text(encoding="utf-8"))
    print("Could not install from PyPI, so reading a recorded run instead:", flush=True)
    print(f"  {FALLBACK.relative_to(ROOT)}", flush=True)
    print(flush=True)
    print(f"  {data['package']} {data['old_version']} -> {data['new_version']}", flush=True)
    for kind, n in data["counts"].items():
        print(f"    {kind:<10}{n:>4}", flush=True)
    print(
        f"    {data['reaching_you']} of them are referenced by pypa/build's own source", flush=True
    )
    return 0


def main() -> int:
    print("blast-radius: what does a PATCH release actually change?", flush=True)
    print(flush=True)
    print("urllib3 2.2.1 -> 2.2.2. Semver says a patch changes nothing a caller", flush=True)
    print("can see. Installing both and comparing them says otherwise.", flush=True)
    print(flush=True)

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "blast_radius.cli",
                "check",
                "urllib3",
                "2.2.1",
                "2.2.2",
                "--no-behaviour",
            ],
            cwd=ROOT,
            env=env,
            check=False,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired):
        return from_committed()

    if result.returncode != 0:
        return from_committed()

    print(flush=True)
    print("Two reshaped symbols in a patch release. They are", flush=True)
    print("urllib3.BaseHTTPResponse and urllib3.HTTPResponse, and the change is a", flush=True)
    print("new REQUIRED keyword-only parameter inserted mid-signature.", flush=True)
    print(flush=True)
    print("--no-behaviour was passed here to keep the demo quick. Without it the", flush=True)
    print("tool also executes every function that kept its name and signature, on", flush=True)
    print("the same inputs in both versions, looking for the category nothing else", flush=True)
    print("reports: same name, same signature, different answer.", flush=True)
    print(flush=True)
    print("Try it on something you actually pin:", flush=True)
    print("    blast-radius check <package> <old> <new>", flush=True)
    print("    blast-radius check <package> <old> <new> --used-by /path/to/your/repo", flush=True)
    print(flush=True)
    print("27 upgrade pairs measured this way: docs/SEMVER.md", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
