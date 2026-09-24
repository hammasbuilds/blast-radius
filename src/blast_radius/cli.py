"""Command line entry point.

    blast-radius check packaging 21.3 24.0 [--used-by /path/to/your/repo]

Installs both versions into throwaway directories, reads both public surfaces, and then
executes the functions that survived unchanged. `--used-by` narrows everything to the parts
your own code actually references.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from blast_radius.diff import (
    api_changes,
    behaviour_changes,
    find_call_sites,
    stable_callables,
)
from blast_radius.probe import WrongVersionImported, surface
from blast_radius.report import summary, write_json, write_markdown
from blast_radius.types import Kind, Report


def install(package: str, version: str, into: Path, timeout: float = 600.0) -> tuple[bool, str]:
    """Install one version into its own directory. `uv` if present, else pip.

    Returns (installed, reason). The reason is empty on success and carries the
    installer's own last line on failure - a bare False leaves the caller printing
    "install failed" with nothing to act on, and the real causes are specific and
    fixable: no wheel for this interpreter, no pip in the running environment, a
    yanked version.
    """
    into.mkdir(parents=True, exist_ok=True)
    spec = f"{package}=={version}"
    # uv adopts an active virtualenv over --target unless it is cleared.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    reasons: list[str] = []
    for cmd in (
        ["uv", "pip", "install", "--quiet", "--target", str(into), spec],
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(into), spec],
    ):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=env,
                # NOT text=True. That decodes with the locale codec, which on a
                # Windows console is cp1252 - and uv draws its errors with box
                # characters that cp1252 cannot represent. The decode then raises
                # UnicodeDecodeError from subprocess's reader thread, which is not
                # OSError and was not caught here, so a failed install crashed the
                # whole run instead of moving on to pip.
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            reasons.append(f"{cmd[0]}: {type(exc).__name__}")
            continue
        if proc.returncode == 0 and any(into.iterdir()):
            return True, ""
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = next(
            (line.strip() for line in reversed(lines) if line.strip()), f"exit {proc.returncode}"
        )
        reasons.append(f"{cmd[0]}: {tail[:160]}")
    return False, " | ".join(reasons)


def cmd_check(args: argparse.Namespace) -> int:
    t0 = time.time()
    workdir = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="blast-"))
    report = Report(
        package=args.package, old_version=args.old_version, new_version=args.new_version
    )

    try:
        for version in (args.old_version, args.new_version):
            print(f"installing {args.package}=={version} ...")
            installed, why = install(args.package, version, workdir / version)
            if not installed:
                print(f"  could not install {args.package}=={version}")
                if why:
                    print(f"  {why}")
                return 1

        print("\nreading both public surfaces...")
        try:
            old = surface(workdir / args.old_version, args.package)
            new = surface(workdir / args.new_version, args.package)
        except WrongVersionImported as exc:
            # Never downgrade this to a warning. Two probes of the same copy agree
            # perfectly, and the report would say the upgrade changes nothing.
            print(f"  ABORTED: {exc}")
            return 1
        if not old or not new:
            print("  a version could not be imported at all")
            return 1

        om, nm = old.get("__meta__", {}), new.get("__meta__", {})
        print(
            f"  {args.old_version}: {len(old) - 1} public symbols "
            f"(reports version {om.get('version') or '?'})"
        )
        print(
            f"  {args.new_version}: {len(new) - 1} public symbols "
            f"(reports version {nm.get('version') or '?'})"
        )

        report.changes.extend(api_changes(old, new))

        if not args.no_behaviour:
            stable = stable_callables(old, new, limit=args.limit)
            print(f"\nexecuting {len(stable)} function(s) that kept both name and signature...")
            silent, compared, unreachable, stopped_on = behaviour_changes(
                workdir / args.old_version, workdir / args.new_version, stable, args.timeout
            )
            report.changes.extend(silent)
            report.compared = compared
            report.unreachable = unreachable
            print(f"  {compared} exercised, {unreachable} could not be called in either")
            if stopped_on:
                # Said out loud, because the count above cannot distinguish "the
                # package has functions nothing can call" from "this tool stopped
                # there", and only the first is a finding about the package.
                print(
                    f"  the probe did not return from {len(stopped_on)} function(s) and was"
                    f" restarted past them: {', '.join(stopped_on[:5])}"
                    + (" ..." if len(stopped_on) > 5 else "")
                )
                # Deliberately not "try a longer --timeout". These functions do not
                # return - click.getchar and click.launch wait on the console and
                # the desktop - so a longer timeout reaches nothing and costs that
                # many more seconds per name. Each one costs one full --timeout,
                # which is the knob that actually matters, in the other direction.
                print(
                    f"  they are counted as unreachable above, and cost {args.timeout:g}s each."
                    " A shorter --timeout makes them cheaper, not fewer."
                )

        if args.used_by:
            print(f"\nmatching against {args.used_by} ...")
            find_call_sites(args.used_by, args.package, report.changes)
            print(f"  {len(report.reaching_you)} change(s) your code references")

        report.seconds = time.time() - t0
        print()
        print(summary(report))

        if args.out:
            out = Path(args.out).resolve()
            out.mkdir(parents=True, exist_ok=True)
            write_json(report, out / "blast-radius.json")
            write_markdown(report, out / "REPORT.md")
            print(f"\n  wrote {out / 'REPORT.md'} and {out / 'blast-radius.json'}")

        if args.fail_on_silent and report.of(Kind.SILENT):
            return 1
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="blast-radius", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="what an upgrade actually changes")
    c.add_argument("package")
    c.add_argument("old_version")
    c.add_argument("new_version")
    c.add_argument("--used-by", help="a repo to match call sites against")
    c.add_argument("--limit", type=int, default=400, help="max functions to execute")
    c.add_argument("--timeout", type=float, default=600.0)
    c.add_argument("--no-behaviour", action="store_true", help="API diff only, no execution")
    c.add_argument("--out", help="directory for REPORT.md and blast-radius.json")
    c.add_argument("--keep", help="keep the installs in this directory")
    c.add_argument(
        "--fail-on-silent",
        action="store_true",
        help="exit 1 if any silent behaviour change is found",
    )
    c.set_defaults(fn=cmd_check)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
