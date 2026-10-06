"""Import a package version in a subprocess and report what it contains, or what it does.

Two versions of the same package cannot coexist in one interpreter - `sys.modules` is keyed
by name, so the second import wins and every comparison afterwards is against one version
twice. So each version is probed in its own subprocess with its own directory first on
`sys.path`, and the two results are compared out here.

That is also why the probe returns *strings*. A repr crosses a process boundary; a live
object does not, and serialising one would mean choosing an encoding that is itself a
behaviour difference waiting to be mistaken for the package's.

Every probe runs inside a sandbox (see SANDBOX below). The behaviour pass calls the
package's public functions with generated arguments, and before the sandbox existed a run
against click opened four Explorer windows and left Notepad open: `click.launch` and
`click.edit` did exactly what they are for.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path


@contextlib.contextmanager
def _scratch() -> Iterator[str]:
    """A temp directory that is removed on a best-effort basis and never raises.

    Not `TemporaryDirectory(ignore_cleanup_errors=True)`: when a directory inside it
    keeps refusing removal (a probed function left a process or handle holding it),
    CPython 3.12's cleanup on Linux retries through unlink -> IsADirectoryError ->
    rmtree -> rmdir without end and dies with RecursionError - after the work is
    done. Leaving a temp directory behind costs less than losing the measurement.
    """
    import shutil
    import tempfile

    path = tempfile.mkdtemp(prefix="blast-probe-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# Installed into every probe before any package code runs. Comments, not docstrings,
# throughout the templates: each is a triple-quoted string and a docstring would close it.
SANDBOX = r"""
import os as _os
import sys as _sys


def _br_sandbox():
    # What a probed function is NOT allowed to do, whatever its name.
    #
    # The name filter in diff.py keeps obviously dangerous functions (launch, edit,
    # delete, ...) out of the behaviour pass entirely. This is the second line: a
    # function with an innocent name that opens a browser, starts a process, reaches
    # the network, reads the console or writes outside the probe's own temp directory
    # gets a PermissionError instead - in both versions alike, so it compares as a
    # refusal rather than doing the thing.
    #
    # It is not a security boundary against hostile code (ctypes can do anything). It
    # is a guard against ordinary library code doing ordinary things to the machine of
    # somebody who only asked for a diff.
    import builtins
    import io
    import socket
    import subprocess

    root = _os.path.normcase(_os.path.realpath(_os.environ.get("BR_SANDBOX") or _os.getcwd()))

    def inside(path):
        try:
            if isinstance(path, int):
                return True  # an already-open descriptor
            text = _os.fsdecode(_os.fspath(path))
            full = _os.path.normcase(_os.path.realpath(text))
        except Exception:
            return False
        return full == root or full.startswith(root + _os.sep)

    def deny(what):
        def blocked(*args, **kwargs):
            raise PermissionError("blocked by the blast-radius sandbox: " + what)

        blocked.__name__ = "blocked"
        return blocked

    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(c in str(mode) for c in "wax+") and not inside(file):
            raise PermissionError("blocked by the blast-radius sandbox: write to " + str(file))
        return real_open(file, mode, *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_open

    write_flags = 0
    for flag in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
        write_flags |= getattr(_os, flag, 0)
    real_os_open = _os.open

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & write_flags and not inside(path):
            raise PermissionError("blocked by the blast-radius sandbox: write to " + str(path))
        return real_os_open(path, flags, *args, **kwargs)

    _os.open = guarded_os_open

    def paths_inside(name, count):
        real = getattr(_os, name, None)
        if real is None:
            return

        def guarded(*args, **kwargs):
            for p in args[:count]:
                if not inside(p):
                    raise PermissionError(
                        "blocked by the blast-radius sandbox: " + name + " " + str(p)
                    )
            return real(*args, **kwargs)

        setattr(_os, name, guarded)

    for name in ("remove", "unlink", "rmdir", "mkdir", "chmod", "truncate", "utime"):
        paths_inside(name, 1)
    for name in ("rename", "replace", "link", "symlink"):
        paths_inside(name, 2)

    for name in (
        "system", "popen", "startfile", "fork", "forkpty", "kill", "killpg",
        "posix_spawn", "posix_spawnp", "execv", "execve", "execl", "execle", "execlp",
        "execlpe", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe",
        "spawnv", "spawnve", "spawnvp", "spawnvpe",
    ):  # fmt: skip
        if hasattr(_os, name):
            setattr(_os, name, deny("os." + name))
    subprocess.Popen.__init__ = deny("starting a process")
    for mod_name, attr in (("_winapi", "CreateProcess"), ("_posixsubprocess", "fork_exec")):
        mod = _sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, attr):
            setattr(mod, attr, deny("starting a process"))

    # Loopback stays open: asyncio builds its self-pipe from a socketpair, which on
    # Windows is a bind and a connect on 127.0.0.1. Anything else is the network.
    loopback = {"127.0.0.1", "::1", "localhost", "", None}

    def host_of(address):
        if isinstance(address, tuple) and address:
            return address[0]
        return address if isinstance(address, str) else None

    def local_only(real, what):
        def guarded(self, address, *args, **kwargs):
            if isinstance(address, tuple) and host_of(address) not in loopback:
                raise PermissionError("blocked by the blast-radius sandbox: " + what)
            return real(self, address, *args, **kwargs)

        return guarded

    for name in ("connect", "connect_ex", "bind"):
        setattr(socket.socket, name, local_only(getattr(socket.socket, name), "network"))
    socket.socket.sendto = deny("network")
    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in loopback:
            raise PermissionError("blocked by the blast-radius sandbox: network lookup")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo
    for name in ("create_connection", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        setattr(socket, name, deny("network"))

    try:
        import webbrowser

        for name in ("open", "open_new", "open_new_tab", "get"):
            setattr(webbrowser, name, deny("opening a browser"))
    except Exception:
        pass

    def no_console(*args, **kwargs):
        raise EOFError("no console in the blast-radius sandbox")

    builtins.input = no_console
    try:
        import getpass

        getpass.getpass = no_console
    except Exception:
        pass
    try:
        import msvcrt

        for name in ("getch", "getwch", "getche", "getwche"):
            if hasattr(msvcrt, name):
                setattr(msvcrt, name, no_console)
        if hasattr(msvcrt, "kbhit"):
            msvcrt.kbhit = lambda: False
    except ImportError:
        pass

    _sys._br_real_exit = _os._exit

    def soft_exit(code=0):
        raise SystemExit(code)

    _os._exit = soft_exit

    # Second layer, under the name patches above. Patching names in `os` and `builtins`
    # can only cover what Python code reaches by those names, so it cannot see a module
    # that does its file I/O in C: sqlite3.connect("outside.db") created a real file
    # outside the root with every guard above installed. That is not hostile code, it is
    # ordinary library code - any package with a local cache or an on-disk index does it
    # on import - and it is exactly what this sandbox exists to prevent.
    #
    # CPython raises audit events from inside those C implementations, so a hook sees
    # them however they are reached, and cannot be bypassed by a reference bound before
    # the sandbox was installed. The policy is the same `inside(root)` rule as above, so
    # the probe's own scratch writes keep working.
    # Flag bits on the os.open form of the event that mean the call will change the file.
    # O_RDONLY is 0, so a read still passes.
    _open_write_flags = 0
    for _flag_name in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
        _open_write_flags |= getattr(_os, _flag_name, 0)

    def _audit(event, args):
        if event == "open":
            # The name patches above cover `builtins.open`, `io.open` and `os.open` and
            # nothing else. `io.FileIO` is a different callable that opens the file in C,
            # and `_io` holds the same objects under names nothing rebound - so io.FileIO,
            # _io.open and _io.FileIO each wrote outside the root with every patch
            # installed. One hook branch covers all of them, and anything else that opens
            # a file, because CPython raises this event from inside the C implementation.
            #
            # Two call shapes share the event: a mode STRING as args[1] (builtins.open,
            # io.open, io.FileIO), or None there and an integer flag set as args[2]
            # (os.open).
            target = args[0] if args else None
            mode = args[1] if len(args) > 1 else None
            if mode is None:
                flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
                writing = bool(flags & _open_write_flags)
            else:
                writing = any(c in str(mode) for c in "wax+")
            if writing and not inside(target):
                raise PermissionError(
                    "blocked by the blast-radius sandbox: write to " + str(target)
                )
        elif event == "sqlite3.connect":
            target = args[0] if args else None
            if target not in (":memory:", "", None) and not inside(target):
                raise PermissionError(
                    "blocked by the blast-radius sandbox: sqlite3.connect " + str(target)
                )
        elif event in ("ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function"):
            # The module docstring is honest that ctypes can do anything. It can still
            # be refused when a diff is all that was asked for.
            raise PermissionError("blocked by the blast-radius sandbox: ctypes")
        elif event == "os.truncate":
            # Guarded by path above, but the fd form slips through `inside`, which
            # treats any integer as an already-open descriptor.
            if args and isinstance(args[0], int):
                raise PermissionError("blocked by the blast-radius sandbox: truncate by fd")
        elif event in ("shutil.copyfile", "shutil.copymode", "shutil.copystat",
                       "shutil.move", "shutil.rmtree", "shutil.unpack_archive"):
            for candidate in args:
                if isinstance(candidate, (str, bytes)) and not inside(candidate):
                    raise PermissionError(
                        "blocked by the blast-radius sandbox: " + event + " " + str(candidate)
                    )

    try:
        _sys.addaudithook(_audit)
    except Exception:
        # An interpreter without audit hooks keeps the name-patch layer, which is what
        # it had before. Losing the second layer must not stop a diff.
        pass
"""

COMMON = r"""
import importlib, inspect, json, os, pkgutil, sys, warnings

warnings.simplefilter("ignore")


def call_shape(obj):
    # The part of a signature that decides whether existing call sites still work:
    # parameter names, their kinds, and whether each has a default. Annotations are
    # excluded deliberately - adding a type hint does not break a caller.
    #
    # None means UNKNOWN (a C function with no text signature). An empty string means
    # a function that takes no arguments. They used to be the same "", so every
    # f() -> f(x=None) read as "could not compare" and was reported as breaking.
    try:
        sig = inspect.signature(obj)
    except (ValueError, TypeError):
        return None, ""
    parts = []
    for name, p in sig.parameters.items():
        parts.append(name + ":" + p.kind.name + (":d" if p.default is not p.empty else ""))
    return ",".join(parts), str(sig)


def kind_of(obj):
    return (
        "class" if inspect.isclass(obj)
        else "function" if inspect.isroutine(obj)
        else "module" if inspect.ismodule(obj)
        else "other"
    )
"""

SURFACE = r"""
_br_sandbox()
target = sys.argv[1]
sys.path.insert(0, target)
with open(sys.argv[2], encoding="utf-8") as fh:
    config = json.load(fh)
modules = config["modules"]
owned = config["owned"]

# Segments that mark a module as not part of anyone's API. numpy ships its test suite
# inside the package, so without this 184 of the 218 names reported as "gone" between
# numpy 2.2 and 2.5 were numpy.*.tests.* modules. `testing` is excluded only below the
# top level: numpy.testing is published API, numpy._core.tests.testing is not.
EXCLUDED = {"tests", "test", "conftest", "vendor", "vendored", "benchmarks", "benchmark"}

out = {}


def is_excluded(dotted):
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        if i and part.startswith("_"):
            return True
        if part in EXCLUDED or (part == "testing" and i >= 2):
            return True
    return False


def owned_by(obj):
    # Is this symbol part of the distribution, or merely imported into its namespace?
    #
    # "Part of the distribution" means defined in ANY module the distribution installs,
    # not only under the import name. pytest.fixture is defined in _pytest.fixtures and
    # attrs.define in attr._next_gen; checking only the import name's prefix found 13
    # symbols in pytest and 0 in attrs, and reported "nothing changed".
    #
    # Third-party names still do not count: packaging 21.3 does `from pyparsing import
    # ...`, and crediting packaging with pyparsing's API turned dropping that dependency
    # into 455 removed symbols.
    mod = getattr(obj, "__module__", None)
    if not isinstance(mod, str) or not mod:
        return False
    return any(mod == p or mod.startswith(p + ".") for p in owned)


def better_path(new, old, home=""):
    # Which of two import paths for the same object is the one a user would write?
    # Fewer dots first, then the defining module, then the shorter string, then
    # alphabetical - deterministic, or a stable symbol reads as removed under one name
    # and added under another.
    def rank(path):
        return (path.count("."), path != home, len(path), path)

    return rank(new) < rank(old)


def identity(obj, fallback):
    mod = getattr(obj, "__module__", "") or ""
    qual = getattr(obj, "__qualname__", "") or ""
    if isinstance(mod, str) and mod and isinstance(qual, str) and qual:
        return mod + "." + qual
    return fallback


def describe(obj, qualname, exported=False):
    # One object, one entry - keyed by where it is DEFINED, reported under the
    # shortest path it can be reached by. Every other path is kept as an alias, so a
    # method that moved to a base class (bs4 4.13 moved BeautifulSoup.append to Tag)
    # is still found under the name callers write.
    key = identity(obj, qualname)
    existing = out.get(key)
    if existing is not None:
        existing["aliases"] = sorted(set(existing["aliases"]) | {qualname})
        if better_path(qualname, existing["name"], key):
            existing["name"] = qualname
        existing["exported"] = existing["exported"] or exported
        return
    shape, sig = call_shape(obj)
    doc = (inspect.getdoc(obj) or "").strip().splitlines()
    out[key] = {
        "name": qualname,
        "aliases": [qualname],
        "exported": exported,
        "kind": kind_of(obj),
        "signature": sig,
        "shape": shape,
        "doc": doc[0][:120] if doc else "",
    }


def walk(mod, prefix, top):
    allowed = getattr(mod, "__all__", None)
    for name in dir(mod):
        if name.startswith("_"):
            continue
        if allowed is not None and name not in allowed:
            continue
        try:
            obj = getattr(mod, name)
        except Exception:
            continue
        if not owned_by(obj):
            continue
        qual = prefix + "." + name
        exported = allowed is not None or prefix == top
        if inspect.isroutine(obj) or inspect.isclass(obj):
            describe(obj, qual, exported)
        if inspect.isclass(obj):
            for mname in dir(obj):
                if mname.startswith("_"):
                    continue
                try:
                    m = getattr(obj, mname)
                except Exception:
                    continue
                if inspect.isroutine(m) and owned_by(m):
                    describe(m, qual + "." + mname, exported)


def submodules(name, path):
    # Our own walk rather than pkgutil.walk_packages, which imports every package it
    # finds in order to recurse - including the tests packages being skipped, and
    # whatever those import.
    try:
        found = list(pkgutil.iter_modules(path, name + "."))
    except Exception:
        return
    for info in sorted(found, key=lambda i: i.name):
        if is_excluded(info.name):
            continue
        try:
            sub = importlib.import_module(info.name)
        except BaseException:
            continue
        yield info.name, sub
        if info.ispkg:
            yield from submodules(info.name, getattr(sub, "__path__", []))


def origin_of(module):
    origin = getattr(module, "__file__", None)
    if origin:
        return [origin]
    # A namespace package (jaraco.functools's `jaraco`) has no __file__, only a
    # __path__ that may span several directories.
    return [str(p) for p in getattr(module, "__path__", [])]


meta = {"kind": "meta", "signature": "", "doc": "", "origin": "", "version": "",
        "modules": [], "failed": {}}
here = os.path.normcase(os.path.abspath(target))
for top in modules:
    try:
        root = importlib.import_module(top)
    except BaseException as exc:
        meta["failed"][top] = type(exc).__name__ + ": " + str(exc)[:200]
        continue
    # Prove the import came from the directory we were pointed at. Python silently
    # ignores a sys.path entry that does not exist, so a mistyped path means the import
    # falls through to whatever the interpreter already has - and two probes of one
    # copy agree perfectly that nothing changed.
    origins = origin_of(root)
    norm = [os.path.normcase(os.path.abspath(o)) for o in origins]
    if not any(o.startswith(here) for o in norm):
        print("__BR_WRONGDIR__" + (origins[0] if origins else top + " (no file or path)"))
        sys.exit(0)
    meta["modules"].append(top)
    if not meta["origin"]:
        meta["origin"] = origins[0] if origins else ""
    if not meta["version"]:
        meta["version"] = str(getattr(root, "__version__", "") or "")
    walk(root, top, top)
    for name, sub in submodules(top, getattr(root, "__path__", [])):
        walk(sub, name, top)

if not meta["modules"]:
    # Nothing imported. Reported with the reasons rather than as an empty surface,
    # which would read as "this version has no API" and every symbol as gone.
    print("__BR_JSON__" + json.dumps({"__meta__": meta}))
    sys.exit(0)

# Re-key from definition site to public name, so moving a class between internal
# modules is not one removal plus one addition.
final = {}
for record in out.values():
    name = record["name"]
    previous = final.get(name)
    if previous is not None:
        previous["shadowed"] = True
        continue
    final[name] = record
final["__meta__"] = meta
print("__BR_JSON__" + json.dumps(final))
"""

RESOLVE = r"""
_br_sandbox()
sys.path.insert(0, sys.argv[1])
with open(sys.argv[2], encoding="utf-8") as fh:
    names = json.load(fh)

# Does each name still resolve - import the longest module prefix that imports, then
# getattr the rest, inherited attributes included? A name the surface walk did not see
# may still work: pydantic 2 serves pydantic.json.pydantic_encoder through a module
# __getattr__ that dir() cannot list, and bs4 moved methods onto a base class. Calling
# those "gone" is telling a user their import will fail when it will not.
found = {}
for qualname in names:
    parts = qualname.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:cut]))
        except BaseException:
            continue
        try:
            for attr in parts[cut:]:
                obj = getattr(obj, attr)
        except BaseException:
            continue
        shape, sig = call_shape(obj) if callable(obj) else (None, "")
        found[qualname] = {
            "name": qualname, "aliases": [qualname], "exported": False,
            "kind": kind_of(obj), "signature": sig, "shape": shape, "doc": "",
            "resolved": True, "callable": callable(obj),
        }
        break
print("__BR_JSON__" + json.dumps(found))
"""

CALL = r"""
import contextlib, io, re, tempfile

if not os.path.isdir(sys.argv[1]):
    print("__BR_WRONGDIR__no such directory: " + sys.argv[1])
    raise SystemExit(0)
target = sys.argv[1]
sys.path.insert(0, target)

# The payload arrives in a FILE, not on the command line: a command line has a length
# limit, and on Windows a payload past ~12k characters returned nothing with exit 0.
with open(sys.argv[2], "r", encoding="utf-8") as fh:
    payload = json.load(fh)
calls = payload["calls"]

# Results are journalled as they are produced, so anything that stops this
# interpreter - a hang, a segfault - costs only the function in flight.
journal = open(sys.argv[3], "a", encoding="utf-8")
_br_sandbox()

# What makes two runs differ without the package behaving differently.
#   <Foo object at 0x7f...>            the default repr
#   <py314-none-win @ 2380956229248>   id(self) in decimal (packaging.tags.Tag)
#   <Template memory:2707908b560>      id(self) in bare hex (jinja2)
#   option.<locals>.decorator          a closure named after whichever helper built it
#   C:\...\blast-x\2023.11.17\certifi  the directory each version was installed into
#   python-requests/2.31.0             the package's own version string
ADDR = re.compile(r"0x[0-9a-fA-F]{4,}")
DECIMAL_ID = re.compile(r"@ ?\d{7,}")
BARE_HEX_ID = re.compile(
    r"(?<![0-9A-Za-z])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{9,16}(?![0-9A-Za-z])"
)
CLOSURE = re.compile(r"<function [\w.]*<locals>\.[\w.<>]+ at 0x\.\.\.>")
MAX_ITEMS = 64


def path_forms(path):
    # A path can appear raw, with forward slashes, or repr-escaped with doubled
    # backslashes. Longest first, so a parent never eats part of a child.
    forms = set()
    for p in {path, os.path.abspath(path), os.path.realpath(path)}:
        if not p or len(p) < 4:
            continue
        forms |= {p, p.replace("\\", "/"), p.replace("\\", "\\\\")}
    return forms


PLACES = []
for place, label in (
    (target, "<site>"),
    (os.getcwd(), "<cwd>"),
    (os.path.expanduser("~"), "<home>"),
    (tempfile.gettempdir(), "<tmp>"),
    (sys.prefix, "<python>"),
    (sys.base_prefix, "<python>"),
    (sys.exec_prefix, "<python>"),
):
    for form in path_forms(place):
        PLACES.append((form, label))
PLACES.sort(key=lambda p: -len(p[0]))
PLACE_RE = [(re.compile(re.escape(form), re.IGNORECASE), label) for form, label in PLACES]
VERSION_RE = [
    re.compile(r"(?<![0-9.])" + re.escape(v) + r"(?!\.?[0-9])")
    for v in sorted(set(payload.get("versions", [])), key=len, reverse=True)
    if v and v.count(".") >= 1 and len(v) >= 3
]


def scrub(text):
    for rx, label in PLACE_RE:
        text = rx.sub(lambda m, label=label: label, text)
    for rx in VERSION_RE:
        text = rx.sub("<version>", text)
    text = DECIMAL_ID.sub("@ id", ADDR.sub("0x...", text))
    text = BARE_HEX_ID.sub("<id>", text)
    return CLOSURE.sub("<function (a closure)>", text)


def text_of(obj, hook):
    # repr() and str() run the package's OWN code and can raise, here, outside any
    # handler expecting it (click.ClickException.__str__ can return a non-string).
    try:
        return hook(obj)
    except BaseException as exc:
        return "<" + hook.__name__ + " raised " + type(exc).__name__ + ">"


def describe(exc):
    return scrub(type(exc).__name__ + ": " + text_of(exc, str)[:150])


def render(value):
    # A lazy iterator reprs identically whatever it would yield, so it is drained.
    if hasattr(value, "__next__") and not isinstance(value, (str, bytes)):
        items = []
        try:
            for i, item in enumerate(value):
                if i >= MAX_ITEMS:
                    return text_of(items, repr) + "...(truncated)"
                items.append(item)
        except Exception as exc:
            return text_of(items, repr) + "...then " + describe(exc)[:120]
        return text_of(items, repr)
    return text_of(value, repr)


class NeedsInstance(Exception):
    pass


ARG_BY_TYPE = {
    "int": 1, "float": 1.0, "bool": True, "str": "x", "bytes": b"x",
    "list": [], "tuple": (), "dict": {}, "set": set(),
}


def value_for(parameter):
    # A plausible argument for a constructor parameter - only used to BUILD AN
    # INSTANCE, never compared. A wrong guess raises and the name is reported as
    # needing an instance, so this can only turn a refusal into a comparison.
    annotation = parameter.annotation
    if annotation is not inspect.Parameter.empty:
        text = (getattr(annotation, "__name__", None) or str(annotation)).lower()
        for name, value in ARG_BY_TYPE.items():
            if name in text:
                return value
    return "x"


def build_instance(owner):
    # (instance, how it was built). The "how" goes into the result, because a method
    # is only comparable across versions when both instances were built the same way:
    # pytest's LineMatcher got [] in one version and "x" in the other (its annotation
    # changed), and every method on it then "changed behaviour".
    #
    # BaseException, not Exception: pytest refuses direct construction by raising
    # Failed, which is a BaseException, and that used to escape as a resolve error.
    try:
        return owner(), owner.__name__ + "()"
    except KeyboardInterrupt:
        raise
    except BaseException:
        pass
    try:
        signature = inspect.signature(owner)
    except (TypeError, ValueError) as exc:
        raise NeedsInstance(
            owner.__name__ + " has no inspectable constructor: " + type(exc).__name__
        ) from None
    args = []
    for name, parameter in signature.parameters.items():
        if name in ("self", "cls"):
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.default is not parameter.empty:
            break
        args.append(value_for(parameter))
    how = owner.__name__ + "(" + ", ".join(repr(a) for a in args) + ")"
    if args:
        try:
            return owner(*args), how
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            raise NeedsInstance(how + " raised " + type(exc).__name__) from None
    raise NeedsInstance(owner.__name__ + "() takes constructor arguments")


def needs_self(owner, name, obj):
    # Is `owner.name` an instance method? Decided from the class __dict__, not from
    # inspect.isfunction: numpy's Generator.beta is a Cython method, not a function,
    # so it was called unbound with a generated string as `self` - and that crashed the
    # interpreter rather than raising.
    if getattr(obj, "__self__", None) is not None:
        return False  # already bound: a classmethod, or a C classmethod
    for klass in getattr(owner, "__mro__", ()):
        if name in vars(klass):
            raw = vars(klass)[name]
            return not isinstance(raw, (staticmethod, classmethod)) and not inspect.isclass(raw)
    return False


def resolve(qualname):
    # Returns a callable that takes no `self`: a method is bound to a real instance
    # when one can be built, and reported as needing one otherwise - never called
    # with a generated literal standing in for `self`.
    parts = qualname.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        try:
            mod = importlib.import_module(".".join(parts[:cut]))
        except Exception:
            continue
        obj = mod
        owner = None
        for attr in parts[cut:]:
            owner, obj = obj, getattr(obj, attr)
        if inspect.isclass(owner) and needs_self(owner, parts[-1], obj):
            instance, how = build_instance(owner)
            return getattr(instance, parts[-1]), how
        return obj, None
    raise ImportError(qualname)


def record(key, value):
    results[key] = value
    journal.write(json.dumps({"q": key, "r": value}) + "\n")
    journal.flush()


results = {}
for qualname, argsets in calls.items():
    journal.write(json.dumps({"q": qualname, "start": 1}) + "\n")
    journal.flush()
    rows = []
    try:
        fn, instance = resolve(qualname)
    except NeedsInstance as exc:
        record(qualname, {"needs_instance": text_of(exc, str)[:150]})
        continue
    except BaseException as exc:
        record(qualname, {"error": describe(exc)})
        continue
    for src in argsets:
        try:
            args = eval(src)
        except Exception as exc:
            rows.append(["badargs", type(exc).__name__])
            continue
        # Output is captured (it would corrupt the result line) and SystemExit is
        # caught (it inherits BaseException, and one click command calling sys.exit()
        # used to end the whole probe).
        buf_out, buf_err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                value = fn(*args)
            rows.append(["ok", scrub(render(value))])
        except SystemExit as exc:
            rows.append(["exit", "SystemExit: " + scrub(text_of(exc.code, str)[:80])])
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            rows.append(["raise", describe(exc)])
    record(qualname, {"rows": rows, "instance": instance} if instance else {"rows": rows})

journal.close()
sys.stdout.write("__BR_JSON__" + json.dumps(results) + "\n")
sys.stdout.flush()
# os._exit, not a normal return: a probed function may have started a non-daemon
# thread or registered an atexit hook, and either can keep this interpreter alive
# past the end of the batch.
sys._br_real_exit(0)
"""


class WrongVersionImported(RuntimeError):
    """The probe loaded a different copy of the package than it was pointed at.

    Raised rather than returned, because every downstream number would be wrong and
    plausible: two probes of the same installed copy agree perfectly, and the report says
    the upgrade changes nothing.
    """


Progress = Callable[[int, int], None]


def _recover(journal: Path) -> dict | None:
    """Whatever the probe managed to finish before it died, read back off disk.

    Returns None if it never got far enough to produce anything. The last line may be
    half-written, so an unparsable line is skipped rather than treated as the end.
    """
    if not journal.exists():
        return None
    done: dict = {}
    started = None
    for line in journal.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "r" in record:
            done[record["q"]] = record["r"]
        elif record.get("start"):
            started = record["q"]
    if started is not None and started not in done:
        done["__incomplete__"] = {"died_on": started, "completed": len(done)}
    return done or None


def _finished(journal: Path) -> int:
    try:
        return journal.read_bytes().count(b'"r": ')
    except OSError:
        return 0


def _wait(
    proc: subprocess.Popen,
    timeout: float,
    journal: Path | None,
    progress: Callable[[int], None] | None = None,
) -> bool:
    """Wait for the child; kill it and return True if it overran.

    Without a journal, `timeout` bounds the whole run. With one it bounds each
    FUNCTION: a journal that has not grown for `timeout` seconds means one call is stuck.
    """
    start = last_change = last_report = time.monotonic()
    last_size = -1
    while True:
        try:
            proc.wait(timeout=0.2)
            return False
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if journal is not None:
            try:
                size = journal.stat().st_size
            except OSError:
                size = 0
            if size != last_size:
                last_size, last_change = size, now
            overran = now - last_change > timeout
            if progress is not None and now - last_report >= 2:
                last_report = now
                progress(_finished(journal))
        else:
            overran = now - start > timeout
        if overran:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            return True


def sandbox_env(root: Path) -> dict[str, str]:
    """The child's environment: every place a package might write or look for a user,
    redirected into the probe's own temp directory.

    HOME/USERPROFILE/APPDATA are where configuration and caches get written; EDITOR,
    VISUAL, BROWSER and PAGER are what click.edit, click.launch and friends execute. A
    fixed PYTHONHASHSEED makes set and dict orderings identical across the two versions
    and across repeat runs, so an ordering is never mistaken for a behaviour.
    """
    home, tmp = root / "home", root / "tmp"
    for d in (home, tmp, home / "AppData" / "Roaming", home / "AppData" / "Local"):
        d.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSTARTUP")}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(home / "AppData" / "Roaming"),
        LOCALAPPDATA=str(home / "AppData" / "Local"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        TMP=str(tmp),
        TEMP=str(tmp),
        TMPDIR=str(tmp),
        EDITOR="blast-radius-no-editor",
        VISUAL="blast-radius-no-editor",
        BROWSER="blast-radius-no-browser",
        PAGER="blast-radius-no-pager",
        PYTHONHASHSEED="0",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        BR_SANDBOX=str(root),
    )
    return env


def _run(
    script: str,
    args: list[str],
    timeout: float,
    journal: Path | None = None,
    workdir: Path | None = None,
    sandbox: bool = True,
    progress: Callable[[int], None] | None = None,
) -> dict | None:
    # _scratch, not TemporaryDirectory: a probed function may leave a process holding
    # the directory, and its removal must never cost the measurement.
    with contextlib.ExitStack() as stack:
        if workdir is None:
            workdir = Path(stack.enter_context(_scratch()))
        path = workdir / "probe.py"
        prologue = SANDBOX + COMMON
        if not sandbox:
            prologue += "\n_br_sandbox = lambda: None\n_sys._br_real_exit = _os._exit\n"
        path.write_text(prologue + script, encoding="utf-8", newline="")
        # Output goes to FILES, not pipes: a grandchild that inherits a pipe keeps it
        # open after the child is killed, and the wait for EOF never ends.
        out_path, err_path = workdir / "out.txt", workdir / "err.txt"
        timed_out = False
        try:
            with open(out_path, "wb") as out_fh, open(err_path, "wb") as err_fh:
                proc = subprocess.Popen(
                    # -S: no site-packages and no .pth files from the interpreter
                    # running blast-radius. Without it an optional import the target
                    # does not ship (`try: import colorama`) resolved against whatever
                    # was installed next to this tool, and click 7.1.2's surface was
                    # 248 symbols in one environment and 250 in another. The probe
                    # sees the stdlib and the target directory, nothing else.
                    [sys.executable, "-S", str(path), *args],
                    stdout=out_fh,
                    stderr=err_fh,
                    # No stdin: a prompt gets EOF at once instead of blocking.
                    stdin=subprocess.DEVNULL,
                    cwd=workdir,
                    env=sandbox_env(workdir) if sandbox else None,
                )
                timed_out = _wait(proc, timeout, journal, progress)
        except OSError:
            return _recover(journal) if journal else None
        try:
            out = out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            out = ""
        if timed_out and "__BR_JSON__" not in out:
            recovered = _recover(journal) if journal else None
            if recovered and "__incomplete__" in recovered:
                recovered["__incomplete__"]["timed_out"] = True
            return recovered
    if "__BR_WRONGDIR__" in out:
        # Loud on purpose: returning None here would read as "no public API".
        lines = out.split("__BR_WRONGDIR__", 1)[1].strip().splitlines()
        detail = lines[0] if lines else "(no location reported)"
        raise WrongVersionImported(f"the import did not come from the target directory: {detail}")
    marker = out.find("__BR_JSON__")
    if marker < 0:
        return _recover(journal) if journal else None
    try:
        return json.loads(out[marker + len("__BR_JSON__") :].splitlines()[0])
    except (json.JSONDecodeError, IndexError):
        return _recover(journal) if journal else None


def surface(
    target_dir: Path,
    package: str | list[str],
    timeout: float = 300.0,
    owned: list[str] | None = None,
) -> dict | None:
    """Every public callable reachable from `package` (one import name or several).

    `owned` is every module prefix the distribution installs, private ones included
    (pytest installs `_pytest`); a symbol defined under any of them counts as the
    package's own. It defaults to the import names themselves.
    """
    modules = [package] if isinstance(package, str) else list(package)
    with _scratch() as tmp:
        config = Path(tmp) / "config.json"
        config.write_text(
            json.dumps({"modules": modules, "owned": sorted(set(owned or []) | set(modules))}),
            encoding="utf-8",
        )
        return _run(SURFACE, [str(Path(target_dir).resolve()), str(config)], timeout)


def resolve_names(target_dir: Path, names: list[str], timeout: float = 300.0) -> dict:
    """{name: record} for each of `names` that still resolves in this version."""
    if not names:
        return {}
    with _scratch() as tmp:
        blob = Path(tmp) / "names.json"
        blob.write_text(json.dumps(names), encoding="utf-8")
        out = _run(RESOLVE, [str(Path(target_dir).resolve()), str(blob)], timeout)
    return out or {}


# Restarts after a HANG each cost one full timeout, so they are capped hard. Restarts
# after a CRASH cost only an interpreter start (numpy 2.2 has half a dozen C methods that
# take the process down on a bad argument), and capping those at the same five left 242
# numpy functions "not attempted".
MAX_RESTARTS = 5
MAX_CRASHES = 100


def _attempt(
    target_dir: Path,
    payload: dict,
    timeout: float,
    sandbox: bool,
    progress: Callable[[int], None] | None,
) -> dict | None:
    with _scratch() as tmp:
        blob = Path(tmp) / "payload.json"
        blob.write_text(json.dumps(payload), encoding="utf-8")
        journal = Path(tmp) / "journal.jsonl"
        return _run(
            CALL,
            [str(Path(target_dir).resolve()), str(blob), str(journal)],
            timeout,
            journal=journal,
            workdir=Path(tmp),
            sandbox=sandbox,
            progress=progress,
        )


def call(
    target_dir: Path,
    payload: dict[str, list[str]],
    timeout: float = 20.0,
    versions: list[str] | None = None,
    sandbox: bool = True,
    progress: Progress | None = None,
) -> dict | None:
    """Call each qualname on each argument set, and return what came back as strings.

    `timeout` is per function: one that runs longer is abandoned and the batch
    restarts after it, up to MAX_RESTARTS times; a function that crashes the
    interpreter is skipped the same way, up to MAX_CRASHES times. `versions` are
    strings to normalise out of every result (the package's own version numbers).
    `progress(done, total)` is called every couple of seconds while the batch runs.
    """
    if not payload:
        return {}

    results: dict = {}
    stopped_on: list[str] = []
    remaining = list(payload)
    total = len(payload)
    hangs = crashes = 0
    while True:
        base = len(results)
        report = (lambda n, base=base: progress(min(base + n, total), total)) if progress else None
        out = _attempt(
            target_dir,
            {"calls": {q: payload[q] for q in remaining}, "versions": versions or []},
            timeout,
            sandbox,
            report,
        )
        if out is None:
            break
        incomplete = out.pop("__incomplete__", None)
        results.update(out)
        if incomplete is None:
            remaining = []
            break
        died_on = incomplete["died_on"]
        if died_on not in remaining:
            break
        stopped_on.append(died_on)
        if incomplete.get("timed_out"):
            hangs += 1
            results[died_on] = {"error": "no result: it did not return, and was skipped"}
        else:
            crashes += 1
            results[died_on] = {"error": "no result: the interpreter died in it (a crash)"}
        remaining = remaining[remaining.index(died_on) + 1 :]
        if not remaining or hangs > MAX_RESTARTS or crashes > MAX_CRASHES:
            break

    # Anything unattempted gets an entry of its own, so "this tool gave up" never reads
    # as "this package cannot be exercised".
    for name in remaining:
        results[name] = {"error": "not attempted: the probe was restarted too many times"}
    if progress:
        progress(total, total)
    if stopped_on:
        results["__stopped_on__"] = stopped_on
    return results or None
