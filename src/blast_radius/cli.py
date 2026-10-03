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
from dataclasses import dataclass
from pathlib import Path

from blast_radius import __version__
from blast_radius.diff import (
    api_changes,
    behaviour_changes,
    find_call_sites,
    gone_candidates,
    stable_callables,
    unknown_signatures,
    unsafe_to_call,
)
from blast_radius.probe import WrongVersionImported, resolve_names, surface
from blast_radius.report import as_json, summary, write_json, write_markdown
from blast_radius.types import BREAKING, Kind, Report

DESCRIPTION = """\
What a dependency upgrade actually changes.

Installs both versions of a package into throwaway directories, compares their public
API (gone / reshaped / widened / added), then executes every function that kept a
compatible signature on the same inputs in both versions, looking for SILENT changes:
same name, same signature, different answer.

The behaviour pass runs the package's own code, in a sandboxed subprocess: no network,
no child processes, no writes outside a temp directory, and functions whose names
suggest a side effect (launch, edit, delete, send, ...) are never called. --no-behaviour
turns it off.
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
  2  could not finish: bad arguments, an install or import that failed, no public API
     found, or an unexpected error
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

# PEP 440, permissively: what `pip install name==VERSION` could accept as an exact pin.
# Checked before anything is installed, because a specifier like ">=21" used to reach
# the filesystem as a directory name and crash with WinError 123.
_VERSION = re.compile(
    r"^v?(\d+!)?\d+(\.\d+)*"
    r"([-_.]?(a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*)?"
    r"([-_.]?(post|rev|r)[-_.]?\d*|-\d+)?"
    r"([-_.]?dev[-_.]?\d*)?"
    r"(\+[a-z0-9]+([-_.][a-z0-9]+)*)?$",
    re.IGNORECASE,
)
_NAME = re.compile(r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$", re.IGNORECASE)

_stdout_gone = False
# With --json, stdout carries exactly one JSON document and the human report goes to stderr.
_report_to_stderr = False


def _say(text: str = "") -> None:
    """Print a line of the report. A closed pipe (`| head`) ends the output, not the run:
    the comparison still finishes and the exit status still reports the gate."""
    global _stdout_gone
    if _report_to_stderr:
        _note(text)
        return
    if _stdout_gone:
        return
    try:
        print(text, flush=True)
    except (BrokenPipeError, OSError):
        # BrokenPipeError on POSIX, OSError [Errno 22] on Windows.
        _stdout_gone = True
        _silence_stdout()


def _silence_stdout() -> None:
    """Point stdout at the null device, descriptor included, so the interpreter's own
    flush at exit cannot fail on the closed pipe (which exits 120 with a traceback)."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, sys.stdout.fileno())
    with contextlib.suppress(OSError, ValueError):
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def _note(text: str) -> None:
    """Progress and notices: stderr, so they never mix into a report piped elsewhere."""
    with contextlib.suppress(OSError, ValueError):
        print(text, file=sys.stderr, flush=True)


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dist_info(target: Path, distribution: str) -> Path | None:
    wanted = _normalise(distribution)
    for info in sorted(target.glob("*.dist-info")):
        dist_name = info.name[: -len(".dist-info")].rsplit("-", 1)[0]
        if _normalise(dist_name) == wanted:
            return info
    return None


def _record_modules(info: Path) -> list[list[str]]:
    """Every module the distribution installs, as dotted parts, read from RECORD."""
    record = info / "RECORD"
    if not record.is_file():
        return []
    out = []
    for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split(",", 1)[0].replace("\\", "/").split("/")
        if not parts or parts[0] in ("..", "") or parts[0].endswith((".dist-info", ".data")):
            continue
        leaf = parts[-1]
        if leaf.endswith(".py"):
            stem = leaf[:-3]
        elif leaf.endswith((".pyd", ".so")):
            stem = leaf.split(".", 1)[0]
        else:
            continue
        dotted = parts[:-1] + ([] if stem == "__init__" else [stem])
        if dotted and all(p.isidentifier() for p in dotted):
            out.append(dotted if stem != "__init__" else [*dotted, "__init__"])
    return out


# Top-level packages some distributions install that are nobody's API.
_NOT_API = frozenset({"tests", "test", "testing", "docs", "examples", "benchmarks", "conftest"})


@dataclass
class ImportPlan:
    """Which modules to walk, and which module prefixes count as the package's own."""

    modules: list[str]
    owned: list[str]


def import_plan(target: Path, distribution: str) -> ImportPlan:
    """Read the import names from the distribution's own RECORD.

    The name you install is not the name you import (beautifulsoup4 is `bs4`), one
    distribution can install several (attrs installs `attr` AND `attrs`; pytest installs
    `pytest`, `py` and the private `_pytest` that defines most of its API), and a
    namespace package is imported by its dotted name (jaraco.functools installs into a
    `jaraco/` directory with no `__init__.py` - guessing `jaraco` crashed the probe).

    `modules` are the public import names; `owned` also includes private ones, so that
    `pytest.fixture`, defined in `_pytest.fixtures`, counts as pytest's own.
    """
    guess = re.sub(r"[-]", "_", distribution)
    info = _dist_info(target, distribution)
    roots: set[str] = set()
    if info is not None:
        files = _record_modules(info)
        regular = {".".join(p[:-1]) if p[-1] == "__init__" else ".".join(p) for p in files if p}
        for parts in files:
            parts = parts[:-1] if parts[-1] == "__init__" else parts
            for k in range(1, len(parts) + 1):
                prefix = ".".join(parts[:k])
                if prefix in regular:
                    roots.add(prefix)
                    break
        top = info / "top_level.txt"
        if not roots and top.is_file():
            roots = {n.strip() for n in top.read_text(encoding="utf-8").splitlines() if n.strip()}
    roots = {r for r in roots if all(p.isidentifier() for p in r.split("."))}
    if not roots:
        return ImportPlan([guess], [guess])
    public = sorted(
        r
        for r in roots
        if not any(p.startswith("_") for p in r.split(".")) and r.split(".")[-1] not in _NOT_API
    )
    # The name matching the distribution first, so it is the one the report leads with.
    public.sort(key=lambda r: _normalise(r) != _normalise(distribution))
    return ImportPlan(public or sorted(roots), sorted(roots))


def pick_import_name(target: Path, distribution: str) -> tuple[str | None, list[str]]:
    """(the main import name, every public import name found). Kept for scripts."""
    plan = import_plan(target, distribution)
    return (plan.modules[0] if plan.modules else None), plan.modules


# uv draws its errors with box-drawing glyphs. On a console with a legacy code page they
# print as "???" and take the structure of the message with them; ASCII survives anywhere.
_GLYPHS = str.maketrans({"×": "x", "╰": "", "─": "-", "▶": ">", "│": "|", "├": "|"})


def _plain(text: str) -> str:
    return text.translate(_GLYPHS)


def install(package: str, version: str, into: Path, timeout: float = 600.0) -> tuple[bool, str]:
    """Install one version into its own directory. `uv` if present, else pip.

    Returns (installed, reason). The reason carries the installer's own complete error:
    the real causes are specific and fixable, and a one-line summary hid which one it was.
    pip is only tried when uv is not installed.
    """
    into.mkdir(parents=True, exist_ok=True)
    spec = f"{package}=={version}"
    # uv adopts an active virtualenv over --target unless it is cleared.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    uv = shutil.which("uv")
    if uv:
        # --python pins the wheels to the interpreter that will import them.
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
            # NOT text=True: the locale codec (cp1252 on Windows) cannot decode uv's
            # box-drawing characters, and the decode raised from a reader thread.
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


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class _Progress:
    """Counts and an ETA on stderr, at most every `every` seconds per phase.

    A behaviour pass on a large package takes minutes, and click's used to run for eight
    of them without printing a line.
    """

    def __init__(self, versions: dict[str, str], every: float = 10.0) -> None:
        self.versions = versions
        self.every = every
        self.started: dict[str, float] = {}
        self.last: dict[str, float] = {}
        self.done: set[str] = set()

    def __call__(self, label: str, done: int, total: int) -> None:
        now = time.monotonic()
        start = self.started.setdefault(label, now)
        finished = done >= total
        if label in self.done or (not finished and now - self.last.get(label, start) < self.every):
            return
        self.last[label] = now
        if finished:
            self.done.add(label)
        side, _, rest = label.partition(",")
        name = f"{side} {self.versions.get(side, '')}".strip() + (f",{rest}" if rest else "")
        elapsed = now - start
        if finished:
            _note(f"  [{name}] {total}/{total} functions in {_duration(elapsed)}")
            return
        eta = f", about {_duration(elapsed / done * (total - done))} left" if done else ""
        _note(f"  [{name}] {done}/{total} functions, {_duration(elapsed)} elapsed{eta}")


def install_headline(package: str, version: str, why: str) -> str:
    """One line saying why an install failed, when the installer's text makes it clear.

    The installer's own output stays below it - it is specific and sometimes the only
    clue - but a CI log should not need a resolver explanation read to learn of a typo.
    """
    flat = " ".join(why.split()).lower()
    if "not found in the package registry" in flat or (
        "no matching distribution" in flat and "from versions: none" in flat
    ):
        return f"{package} is not on the package index (check the spelling of the name)"
    if "there is no version of" in flat or "no matching distribution" in flat:
        return f"{package} {version} does not exist on the package index"
    if "did not finish within" in flat:
        return "the install timed out; raise --install-timeout or check the network"
    return ""


def cmd_check(args: argparse.Namespace) -> int:
    global _report_to_stderr
    _report_to_stderr = bool(args.json)
    t0 = time.time()
    fail_on = _parse_fail_on(args.fail_on, args.fail_on_silent)
    used_by: Path | None = None
    if args.used_by is not None:
        used_by = Path(args.used_by)
        # Checked BEFORE anything is installed: a typo used to scan nothing and exit 0,
        # which makes a CI gate permanently green.
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
    # One directory per version, named by it (validated in main, so safe as a name),
    # so that --keep can be reused across runs.
    old_dir, new_dir = workdir / args.old_version, workdir / args.new_version

    try:
        for version, into in ((args.old_version, old_dir), (args.new_version, new_dir)):
            _say(f"installing {args.package}=={version} ...")
            installed, why = install(args.package, version, into, timeout=args.install_timeout)
            if not installed:
                headline = install_headline(args.package, version, why)
                _say(
                    f"\nerror: could not install {args.package}=={version}"
                    + (f": {headline}" if headline else "")
                )
                for line in why.splitlines():
                    _say(f"  {line}")
                return EXIT_ERROR

        plan = import_plan(old_dir, args.package)
        owned = sorted(set(plan.owned) | set(import_plan(new_dir, args.package).owned))
        modules = plan.modules
        if args.import_name:
            modules = [m.strip() for m in args.import_name.split(",") if m.strip()]
            owned = sorted(set(owned) | set(modules))
        report.modules = modules
        if modules != [args.package]:
            _say(f"  (imported as {', '.join(f'`{m}`' for m in modules)})")

        _say("\nreading both public surfaces...")
        try:
            old = surface(old_dir, modules, owned=owned)
            new = surface(new_dir, modules, owned=owned)
        except WrongVersionImported as exc:
            # Never downgrade this to a warning: two probes of the same copy agree
            # perfectly, and the report would say the upgrade changes nothing.
            _say(f"  ABORTED: {exc}")
            return EXIT_ERROR
        for version, data in ((args.old_version, old), (args.new_version, new)):
            meta = (data or {}).get("__meta__", {})
            if not data or not meta.get("modules"):
                _say(f"\nerror: could not import {', '.join(modules)} from {version}")
                for mod, why in (meta.get("failed") or {}).items():
                    _say(f"  {mod}: {why}")
                _say("  If the import name differs from the package name, pass --import-name.")
                return EXIT_ERROR
            for mod, why in (meta.get("failed") or {}).items():
                _say(f"  WARNING: {version}: `import {mod}` failed ({why}); its names are absent")
        assert old is not None and new is not None
        om, nm = old["__meta__"], new["__meta__"]
        n_old, n_new = len(old) - 1, len(new) - 1
        for version, n, meta in ((args.old_version, n_old, om), (args.new_version, n_new, nm)):
            reported = meta.get("version") or "?"
            _say(f"  {version}: {n} public symbols (reports version {reported})")
        if n_old == 0 or n_new == 0:
            # An empty surface compares equal to an empty surface, and "nothing changed"
            # would be a confident wrong answer. attrs 21.4.0 -> 23.2.0 used to report
            # exactly that.
            empty = args.old_version if n_old == 0 else args.new_version
            _say(
                f"\nerror: found no public functions or classes in {', '.join(modules)} {empty}."
                " Pass --import-name if the package is imported under another name."
            )
            return EXIT_ERROR

        # A name the walk did not list may still import (a module __getattr__, a method
        # inherited from a moved base class). Only names that fail to resolve are gone.
        candidates = gone_candidates(old, new)
        resolved = resolve_names(new_dir, candidates) if candidates else {}
        report.still_resolve = len(resolved)
        report.changes.extend(api_changes(old, new, resolved))
        report.unknown_signatures = unknown_signatures(old, new, resolved)

        if not args.no_behaviour:
            report.behaviour_checked = True
            eligible = stable_callables(old, new, resolved=resolved)
            report.candidates = len(eligible)
            safe: dict[str, str] = {}
            for name, sig in eligible.items():
                why = unsafe_to_call(name)
                if why:
                    report.skipped_unsafe[name] = why
                else:
                    safe[name] = sig
            if args.limit and len(safe) > args.limit:
                report.truncated = len(safe) - args.limit
                safe = dict(list(safe.items())[: args.limit])
            widened = {c.qualname for c in report.of(Kind.WIDENED)}
            _say(
                f"\nexecuting {len(safe)} function(s) whose existing calls still bind"
                f" ({len(report.skipped_unsafe)} skipped: names suggest side effects)..."
            )
            if report.truncated:
                _say(
                    f"  WARNING: --limit {args.limit}: checking {len(safe)} of"
                    f" {len(safe) + report.truncated}; {report.truncated} were not run"
                )
            _note(
                f"  note: this runs {args.package}'s own code, in a sandboxed subprocess"
                " (no network, no child processes, no writes outside a temp dir)."
                " --no-behaviour skips it."
            )
            found = behaviour_changes(
                old_dir,
                new_dir,
                safe,
                args.timeout,
                widened=widened,
                versions=sorted(
                    {args.old_version, args.new_version, om.get("version"), nm.get("version")}
                    - {None, ""}
                ),
                progress=_Progress({"old": args.old_version, "new": args.new_version}),
            )
            report.changes.extend(found.silent)
            report.weak = found.weak
            report.compared = found.compared
            report.unreachable = found.unreachable
            report.nondeterministic = found.nondeterministic
            _say(f"  {found.compared} exercised, {found.unreachable} could not be called in either")
            for why, n in sorted(found.reasons.items(), key=lambda kv: -kv[1]):
                _say(f"      {n:>4}  {why}")
            if found.nondeterministic:
                _say(
                    f"  {len(found.nondeterministic)} disagreement(s) dropped: the same version"
                    " gave a different answer when re-run (randomness, clocks, counters)"
                )
            if found.stopped_on:
                shown = ", ".join(found.stopped_on[:5])
                more = " ..." if len(found.stopped_on) > 5 else ""
                _say(
                    f"  the probe stopped in {len(found.stopped_on)} function(s) (a hang or a"
                    f" crash) and was restarted past them: {shown}{more}"
                )
                _say(
                    "  they are counted as unreachable above. Each hang costs"
                    f" {args.timeout:g}s per version; a shorter --timeout makes them cheaper,"
                    " not fewer."
                )

        if used_by is not None:
            _say(f"\nmatching against {used_by} ...")
            scan = find_call_sites(used_by, modules, report.changes)
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
        if args.json:
            try:
                print(as_json(report), flush=True)
            except (BrokenPipeError, OSError):
                _silence_stdout()
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
        if action.default not in (None, False, 0, argparse.SUPPRESS) and "%(default)" not in text:
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
    c.add_argument("old_version", help="the exact version you have now, e.g. 21.3")
    c.add_argument("new_version", help="the exact version you are upgrading to, e.g. 24.0")
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
        "--json",
        action="store_true",
        help="print the blast-radius.json report on stdout (the human report moves to "
        "stderr); on an error stdout stays empty and the exit status is 2",
    )
    c.add_argument(
        "--import-name",
        metavar="NAME",
        help="the module(s) to compare, comma-separated, when they cannot be read from the "
        "package's metadata (default: every public top-level module it installs)",
    )
    c.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="execute at most N functions in the behaviour pass (default: all of them; "
        "a partial pass says so in the report)",
    )
    c.add_argument(
        "--timeout",
        type=float,
        default=20.0,
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
    # Witnesses are the package's own reprs and can hold any character; a legacy code
    # page raised UnicodeEncodeError after the whole comparison had finished.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace", line_buffering=True)  # type: ignore[union-attr]
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "check":
        for value in _parse_fail_on(args.fail_on, False):
            if value not in FAIL_ON:
                ap.error(f"--fail-on: unknown value {value!r} (choose from {', '.join(FAIL_ON)})")
        if not _NAME.match(args.package):
            ap.error(f"{args.package!r} is not a package name")
        for version in (args.old_version, args.new_version):
            if not _VERSION.match(version):
                ap.error(
                    f"{version!r} is not an exact version; pass one like 21.3 or 2.0.0rc1,"
                    " not a range"
                )
        if args.old_version == args.new_version:
            ap.error("old_version and new_version are the same")
        if args.limit < 0:
            ap.error("--limit must be 0 (no limit) or more")
        if args.timeout <= 0 or args.install_timeout <= 0:
            ap.error("timeouts must be positive")
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        _note("\ninterrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - the last line of defence, by design
        # Exit 1 means "the gate tripped". A crash must never be mistaken for that, and
        # nobody should have to read a traceback to find out what went wrong.
        _note(f"\nerror: {type(exc).__name__}: {exc}")
        _note("  this is a bug in blast-radius; please report it with the command you ran")
        return EXIT_ERROR
    finally:
        if _stdout_gone:
            _silence_stdout()


if __name__ == "__main__":
    sys.exit(main())
