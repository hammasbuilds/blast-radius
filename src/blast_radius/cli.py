"""Command line entry point for `blast-radius`."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from blast_radius import __version__
from blast_radius.diff import (
    api_changes,
    behaviour_changes,
    find_call_sites,
    stable_callables,
)
from blast_radius.probe import WrongVersionImported, surface
from blast_radius.report import summary, write_json, write_markdown
from blast_radius.types import BREAKING, Kind, Report

DESCRIPTION = """\
What a dependency upgrade actually changes.

Installs both versions of a package into throwaway directories, compares their public
API (gone / reshaped / widened / added), then executes every function that kept a
compatible signature on the same inputs in both versions, looking for SILENT changes:
same name, same signature, different answer.
"""

EPILOG = """\
examples:
  blast-radius check packaging 21.3 24.0
  blast-radius check packaging 21.3 24.0 --used-by path/to/your/repo
  blast-radius check requests 2.28.0 2.31.0 --no-behaviour
  blast-radius check some-lib 1.2.0 1.3.0 --used-by . --fail-on used

exit status:
  0  finished, and no --fail-on condition was met
  1  finished, and a --fail-on condition was met
  2  could not finish: bad arguments, an install that failed, an import that failed
"""

FAIL_ON = ("any", "gone", "reshaped", "silent", "used")
FAIL_ON_HELP = """\
exit 1 when the report contains this; comma-separated or repeated.
  gone, reshaped, silent   any change of that kind
  any                      any of the three breaking kinds
  used                     any breaking change your --used-by code references
widened and added changes never fail the run: they cannot break a caller.
"""

EXIT_OK, EXIT_GATE, EXIT_ERROR = 0, 1, 2


def _say(text: str = "") -> None:
    print(text, flush=True)


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def import_names(target: Path, distribution: str) -> list[str]:
    """The top-level names a distribution installs, read from its own dist-info.

    The name you install is not always the name you import - beautifulsoup4 is `bs4`,
    PyYAML is `yaml`, Pillow is `PIL`. Probing the distribution name for those found
    nothing and reported "could not be imported" for a package that installed fine.
    """
    wanted = _normalise(distribution)
    for info in target.glob("*.dist-info"):
        dist_name = info.name[: -len(".dist-info")].rsplit("-", 1)[0]
        if _normalise(dist_name) != wanted:
            continue
        top = info / "top_level.txt"
        names: set[str] = set()
        if top.is_file():
            names = {n.strip() for n in top.read_text(encoding="utf-8").splitlines()}
        else:
            record = info / "RECORD"
            if record.is_file():
                for line in record.read_text(encoding="utf-8").splitlines():
                    first = line.split(",", 1)[0].replace("\\", "/").split("/", 1)
                    head = first[0]
                    if len(first) == 1 and head.endswith(".py"):
                        names.add(head[:-3])
                    elif len(first) == 2 and not head.endswith((".dist-info", ".data")):
                        names.add(head)
        names = {
            n
            for n in names
            if n and not n.startswith("_") and n.isidentifier() and n != "__pycache__"
        }
        return sorted(names)
    return []


def pick_import_name(target: Path, distribution: str) -> tuple[str | None, list[str]]:
    """(the import name to probe, every candidate found). None when it is ambiguous."""
    candidates = import_names(target, distribution)
    guess = re.sub(r"[-.]", "_", distribution)
    for c in candidates:
        if c.lower() == guess.lower():
            return c, candidates
    if len(candidates) == 1:
        return candidates[0], candidates
    if not candidates:
        return guess, candidates  # no dist-info to read; the plain guess is all there is
    return None, candidates


# uv draws its errors with box-drawing glyphs. On a console with a legacy code page they
# print as "???" and take the structure of the message with them; ASCII survives anywhere.
_GLYPHS = str.maketrans({"×": "x", "╰": "", "─": "-", "▶": ">", "│": "|", "├": "|"})


def _plain(text: str) -> str:
    return text.translate(_GLYPHS)


def install(package: str, version: str, into: Path, timeout: float = 600.0) -> tuple[bool, str]:
    """Install one version into its own directory. `uv` if present, else pip.

    Returns (installed, reason). The reason is empty on success and carries the
    installer's own complete error on failure: the real causes are specific and
    fixable - no such version, no wheel for this interpreter, no network - and a
    one-line summary cut at 160 characters hid which one it was.

    pip is only tried when uv is not installed. Falling back after uv has already
    said "no such version" only added a second, unrelated error ("No module named
    pip", in a uv-made virtualenv) on top of the one that mattered.
    """
    into.mkdir(parents=True, exist_ok=True)
    spec = f"{package}=={version}"
    # uv adopts an active virtualenv over --target unless it is cleared.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    uv = shutil.which("uv")
    if uv:
        # --python pins the interpreter the wheels are chosen for to the one that
        # will import them. Without it uv picks whatever Python it discovers first,
        # and a compiled wheel for another minor version installs fine and then
        # fails to import in the probe.
        cmd = [uv, "pip", "install", "--quiet", "--python", sys.executable]
        cmd += ["--target", str(into), spec]
        name = "uv"
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
        cmd += ["--target", str(into), spec]
        name = "pip"
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
            # NOT text=True. That decodes with the locale codec, which on a Windows
            # console is cp1252 - and uv draws its errors with box characters that
            # cp1252 cannot represent, so the decode raised from subprocess's reader
            # thread and crashed the whole run.
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"{name} did not finish within {timeout:g}s"
    except OSError as exc:
        return False, f"could not run {name}: {exc}"
    if proc.returncode == 0 and any(into.iterdir()):
        return True, ""
    text = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    if name == "pip" and "No module named pip" in text:
        return False, (
            "neither uv nor pip is available to install packages. Install uv "
            "(https://docs.astral.sh/uv/) or run blast-radius from an environment with pip."
        )
    return False, f"{name} failed (exit {proc.returncode}):\n{_plain(text) or '(no output)'}"


def _parse_fail_on(values: list[str] | None, fail_on_silent: bool) -> set[str]:
    out: set[str] = set()
    for value in values or []:
        for part in value.split(","):
            part = part.strip().lower()
            if part:
                out.add(part)
    if fail_on_silent:
        out.add("silent")
    return out


def gate(report: Report, fail_on: set[str]) -> list[str]:
    """Why the run should fail, one line per tripped condition. Empty means pass."""
    reasons: list[str] = []
    kinds = set(BREAKING) if "any" in fail_on else {Kind(k) for k in fail_on & set(Kind)}
    for kind in sorted(kinds, key=lambda k: k.value):
        n = len(report.of(kind))
        if n:
            reasons.append(f"{n} {kind.value} change(s)")
    if "used" in fail_on:
        used = [c for c in report.breaking if c.used_at]
        if used:
            reasons.append(f"{len(used)} breaking change(s) referenced by your code")
    return reasons


def cmd_check(args: argparse.Namespace) -> int:
    t0 = time.time()
    fail_on = _parse_fail_on(args.fail_on, args.fail_on_silent)
    used_by: Path | None = None
    if args.used_by is not None:
        used_by = Path(args.used_by)
        # Checked BEFORE anything is installed. A typo here used to scan nothing,
        # report "none of these were matched to your code" and exit 0 - which makes a
        # CI gate permanently green.
        if not used_by.exists():
            _say(f"error: --used-by {args.used_by}: no such file or directory")
            return EXIT_ERROR
        if used_by.is_file() and used_by.suffix not in (".py", ".pyi"):
            _say(f"error: --used-by {args.used_by}: not a directory or a .py file")
            return EXIT_ERROR
    if "silent" in fail_on and args.no_behaviour:
        _say("error: --fail-on silent needs the behaviour pass; drop --no-behaviour")
        return EXIT_ERROR

    workdir = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="blast-"))
    report = Report(
        package=args.package, old_version=args.old_version, new_version=args.new_version
    )

    try:
        for version in (args.old_version, args.new_version):
            _say(f"installing {args.package}=={version} ...")
            installed, why = install(
                args.package, version, workdir / version, timeout=args.install_timeout
            )
            if not installed:
                _say(f"\nerror: could not install {args.package}=={version}")
                for line in why.splitlines():
                    _say(f"  {line}")
                return EXIT_ERROR

        module = args.import_name
        if module is None:
            module, candidates = pick_import_name(workdir / args.old_version, args.package)
            if module is None:
                _say(
                    f"\nerror: {args.package} installs several top-level packages "
                    f"({', '.join(candidates)}); pick one with --import-name"
                )
                return EXIT_ERROR
            if module != args.package:
                _say(f"  (imported as `{module}`)")

        _say("\nreading both public surfaces...")
        try:
            old = surface(workdir / args.old_version, module)
            new = surface(workdir / args.new_version, module)
        except WrongVersionImported as exc:
            # Never downgrade this to a warning. Two probes of the same copy agree
            # perfectly, and the report would say the upgrade changes nothing.
            _say(f"  ABORTED: {exc}")
            return EXIT_ERROR
        if not old or not new:
            _say(
                f"\nerror: `import {module}` failed in "
                f"{args.old_version if not old else args.new_version}."
                " If the import name differs from the package name, pass --import-name."
            )
            return EXIT_ERROR

        om, nm = old.get("__meta__", {}), new.get("__meta__", {})
        _say(
            f"  {args.old_version}: {len(old) - 1} public symbols "
            f"(reports version {om.get('version') or '?'})"
        )
        _say(
            f"  {args.new_version}: {len(new) - 1} public symbols "
            f"(reports version {nm.get('version') or '?'})"
        )

        report.changes.extend(api_changes(old, new))

        if not args.no_behaviour:
            report.behaviour_checked = True
            stable = stable_callables(old, new, limit=args.limit)
            widened = {c.qualname for c in report.of(Kind.WIDENED)}
            _say(f"\nexecuting {len(stable)} function(s) whose existing calls still bind...")
            silent, compared, unreachable, stopped_on, reasons, weak = behaviour_changes(
                workdir / args.old_version,
                workdir / args.new_version,
                stable,
                args.timeout,
                widened=widened,
            )
            report.changes.extend(silent)
            report.weak = weak
            report.compared = compared
            report.unreachable = unreachable
            _say(f"  {compared} exercised, {unreachable} could not be called in either")
            # Broken out because "could not be called" covers two opposite things:
            # an argument this tool could not generate, and a function that ran and
            # refused. Only the second is a fact about the package.
            for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
                _say(f"      {n:>4}  {why}")
            if stopped_on:
                _say(
                    f"  the probe did not return from {len(stopped_on)} function(s) and was"
                    f" restarted past them: {', '.join(stopped_on[:5])}"
                    + (" ..." if len(stopped_on) > 5 else "")
                )
                # Deliberately not "try a longer --timeout". These functions do not
                # return, so a longer timeout reaches nothing and costs more per name.
                _say(
                    "  they are counted as unreachable above, and cost"
                    f" {args.timeout:g}s each per version."
                    " A shorter --timeout makes them cheaper, not fewer."
                )

        if used_by is not None:
            _say(f"\nmatching against {used_by} ...")
            scan = find_call_sites(used_by, module, report.changes)
            report.used_by = str(used_by)
            report.scanned_files = scan.files
            _say(f"  read {scan.files} Python file(s); {len(report.reaching_you)} change(s) used")
            if scan.unreadable:
                shown = ", ".join(scan.unreadable[:5])
                more = " ..." if len(scan.unreadable) > 5 else ""
                _say(
                    f"  WARNING: {len(scan.unreadable)} file(s) could not be read or parsed and"
                    f" were not searched: {shown}{more}"
                )
            if scan.files == 0:
                _say("  WARNING: no Python files found there - is that the right path?")

        report.seconds = time.time() - t0
        _say()
        _say(summary(report))

        if args.out:
            out = Path(args.out).resolve()
            out.mkdir(parents=True, exist_ok=True)
            write_json(report, out / "blast-radius.json")
            write_markdown(report, out / "REPORT.md")
            _say(f"\n  wrote {out / 'REPORT.md'} and {out / 'blast-radius.json'}")

        tripped = gate(report, fail_on)
        if tripped:
            _say(f"\n  FAIL (--fail-on {','.join(sorted(fail_on))}): {'; '.join(tripped)}")
            return EXIT_GATE
        return EXIT_OK
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


class _Formatter(argparse.RawDescriptionHelpFormatter):
    """Keeps the hand-wrapped description and epilog, and shows defaults."""

    def _get_help_string(self, action: argparse.Action) -> str | None:
        text = action.help or ""
        if action.default not in (None, False, argparse.SUPPRESS) and "%(default)" not in text:
            text += " (default: %(default)s)"
        return text

    def _split_lines(self, text: str, width: int) -> list[str]:
        if "\n" in text:
            return text.splitlines()
        return super()._split_lines(text, width)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="blast-radius",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=_Formatter,
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    c = sub.add_parser(
        "check",
        help="compare two versions of a package",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=_Formatter,
    )
    c.add_argument("package", help="the name you install, e.g. packaging or beautifulsoup4")
    c.add_argument("old_version", help="the version you have now, e.g. 21.3")
    c.add_argument("new_version", help="the version you are upgrading to, e.g. 24.0")
    c.add_argument(
        "--used-by",
        metavar="PATH",
        help="your project (a directory or a .py file): mark and rank the changes it references",
    )
    c.add_argument(
        "--fail-on",
        action="append",
        metavar="WHAT",
        help=FAIL_ON_HELP,
    )
    c.add_argument(
        "--fail-on-silent",
        action="store_true",
        help="same as --fail-on silent",
    )
    c.add_argument(
        "--no-behaviour",
        "--no-behavior",
        action="store_true",
        help="API diff only: skip executing functions (much faster; SILENT is not checked)",
    )
    c.add_argument("--out", metavar="DIR", help="also write REPORT.md and blast-radius.json here")
    c.add_argument(
        "--import-name",
        metavar="NAME",
        help="the module to import, when it differs from the package name and cannot be "
        "read from the package's metadata",
    )
    c.add_argument(
        "--limit",
        type=int,
        default=400,
        metavar="N",
        help="execute at most N functions in the behaviour pass",
    )
    c.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="seconds one function may run in the behaviour pass before it is abandoned and "
        "counted as unreachable; a function that never returns costs this once per version",
    )
    c.add_argument(
        "--install-timeout",
        type=float,
        default=600.0,
        metavar="SECONDS",
        help="seconds allowed for installing each version",
    )
    c.add_argument(
        "--keep",
        metavar="DIR",
        help="install into DIR and leave it there, instead of a temporary directory",
    )
    c.set_defaults(fn=cmd_check)
    return ap


def main(argv: list[str] | None = None) -> int:
    # Witnesses are the package's own reprs and can hold any character. On a Windows
    # console with a legacy code page, printing one that the page cannot represent
    # raised UnicodeEncodeError after the whole comparison had finished.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "check":
        for value in _parse_fail_on(args.fail_on, False):
            if value not in FAIL_ON:
                ap.error(f"--fail-on: unknown value {value!r} (choose from {', '.join(FAIL_ON)})")
        if args.old_version == args.new_version:
            ap.error("old_version and new_version are the same")
        if args.limit < 1:
            ap.error("--limit must be at least 1")
        if args.timeout <= 0 or args.install_timeout <= 0:
            ap.error("timeouts must be positive")
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        _say("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
