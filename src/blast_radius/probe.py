"""Import a package version in a subprocess and report what it contains, or what it does.

Two versions of the same package cannot coexist in one interpreter - `sys.modules` is keyed
by name, so the second import wins and every comparison afterwards is against one version
twice. So each version is probed in its own subprocess with its own directory first on
`sys.path`, and the two results are compared out here.

That is also why the probe returns *strings*. A repr crosses a process boundary; a live
object does not, and serialising one would mean choosing an encoding that is itself a
behaviour difference waiting to be mistaken for the package's.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SURFACE = r"""
import importlib, inspect, json, pkgutil, sys

sys.path.insert(0, sys.argv[1])
package = sys.argv[2]

out = {}


def owned_by(obj, package):
    # Is this symbol part of the package, or merely imported into its namespace?
    #
    # dir(a_module) returns everything reachable, third-party names included. packaging
    # 21.3 does `from pyparsing import ...`, so a naive walk credits packaging with
    # pyparsing's whole API - and dropping pyparsing in 22.0 then reads as 455 removed
    # "packaging" symbols. They were never packaging's to remove.
    #
    # (Comments, not a docstring: this module is itself a triple-quoted template, and a
    # docstring here would terminate it.)
    mod = getattr(obj, "__module__", None)
    if not mod:
        return False
    root = package.split(".")[0]
    return mod == root or mod.startswith(root + ".")


def call_shape(sig):
    # The part of a signature that decides whether existing call sites still work:
    # parameter names, their kinds, and whether each has a default. Annotations are
    # excluded deliberately.
    #
    # click 8 annotated its entire API, so comparing signature STRINGS reported 795
    # "reshaped" functions and left nothing stable - which meant the behaviour pass, the
    # only part of this tool that finds what nothing else warns you about, ran on zero
    # functions. Adding a type hint does not break a caller.
    parts = []
    for name, p in sig.parameters.items():
        parts.append(name + ":" + str(p.kind) + (":d" if p.default is not p.empty else ""))
    return ",".join(parts)


def better_path(new, old):
    # Which of two import paths for the same object is the one a user would write?
    # The shorter the better: `coverage.CoverageData` over
    # `coverage.sqldata.CoverageData`. Ties go to the shorter string, then
    # alphabetically, so the choice is deterministic across versions - which it has
    # to be, or a stable symbol reads as removed under one name and added under
    # another.
    return (new.count("."), len(new), new) < (old.count("."), len(old), old)


def identity(obj, fallback):
    # Where the object actually lives, regardless of who imported it.
    mod = getattr(obj, "__module__", "") or ""
    qual = getattr(obj, "__qualname__", "") or ""
    return (mod + "." + qual) if (mod and qual) else fallback


def describe(obj, qualname, exported=False):
    # One object, one entry - keyed by where it is DEFINED, reported under the
    # shortest path it can be reached by.
    #
    # walk() visits every module in the package, so a class imported into several of
    # them used to be recorded once per module. coverage.CoverageData is imported
    # into collector, control, data, html and sqldata, so a single signature change
    # to `update` was counted six times. Six is not a measurement of anything.
    #
    # Keying on the definition site also means a class moved between two internal
    # modules is no longer a removal plus an addition, as long as the public name
    # people import still resolves. That move is invisible to callers and should be
    # invisible here.
    key = identity(obj, qualname)
    existing = out.get(key)
    if existing is not None:
        existing["aliases"] = sorted(set(existing["aliases"]) | {qualname})
        if better_path(qualname, existing["name"]):
            existing["name"] = qualname
        # Reachable as public under ANY path is public: a class defined in an
        # internal module and re-exported from the package root is part of the
        # published API under that second name.
        existing["exported"] = existing["exported"] or exported
        return

    shape = ""
    try:
        signature = inspect.signature(obj)
        sig = str(signature)
        shape = call_shape(signature)
    except (ValueError, TypeError):
        sig = ""
    doc = (inspect.getdoc(obj) or "").strip().splitlines()
    kind = (
        "class" if inspect.isclass(obj)
        else "function" if inspect.isroutine(obj)
        else "other"
    )
    out[key] = {
        "name": qualname,
        "aliases": [qualname],
        "exported": exported,
        "kind": kind,
        "signature": sig,
        "shape": shape,
        "doc": doc[0][:120] if doc else "",
    }


def walk(mod, prefix):
    allowed = getattr(mod, "__all__", None)
    # Did the author say this module has a public surface, and is this name on it?
    #
    # Without this everything reachable counts equally, so coverage.parser.join_regex
    # - an internal helper in an internal module - weighs the same as
    # coverage.CoverageData.update. A release that reshuffles its internals then
    # looks exactly like one that breaks its users.
    #
    # Two things count as the author saying "public": the name is in a module's
    # __all__, or it sits directly in the package's own namespace (`coverage.X`),
    # which is the import path people actually write.
    for name in dir(mod):
        # `_name` is private by convention; `__all__` is honoured when a module defines it.
        if name.startswith("_"):
            continue
        if allowed is not None and name not in allowed:
            continue
        try:
            obj = getattr(mod, name)
        except Exception:
            continue
        if not owned_by(obj, package):
            continue
        qual = prefix + "." + name
        exported = allowed is not None or prefix == package
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
                if inspect.isroutine(m) and owned_by(m, package):
                    # A method is as public as the class that carries it.
                    describe(m, qual + "." + mname, exported)


try:
    root = importlib.import_module(package)
except Exception as exc:
    print("__BR_FAIL__" + type(exc).__name__ + ": " + str(exc)[:200])
    raise SystemExit(0)

# Prove the import came from the directory we were pointed at.
#
# Python silently ignores a sys.path entry that does not exist, so a mistyped or
# non-native path means the import quietly falls through to whatever the interpreter
# already has. The probe then compares a version against itself and reports, with
# complete confidence, that nothing changed between them.
origin = getattr(root, "__file__", "") or ""
import os
if not os.path.abspath(origin).startswith(os.path.abspath(sys.argv[1])):
    print("__BR_WRONGDIR__" + origin)
    raise SystemExit(0)

out["__meta__"] = {
    "kind": "meta",
    "signature": "",
    "doc": "",
    "origin": origin,
    "version": str(getattr(root, "__version__", "")),
}

walk(root, package)
for info in pkgutil.walk_packages(getattr(root, "__path__", []), package + "."):
    if any(p.startswith("_") for p in info.name.split(".")[1:]):
        continue
    try:
        walk(importlib.import_module(info.name), info.name)
    except Exception:
        continue

# Re-key from definition site to public name.
#
# The walk keys on where an object is defined, because that is what makes two
# aliases of one object the same object. But the DIFF has to key on the name a
# caller writes, or moving a class between internal modules reads as one symbol
# removed and another added - a breaking change that breaks nobody.
#
# coverage 7.5.0 -> 7.5.4 moved PathAliases out of coverage.sqldata. Keyed on the
# definition site that is 4 removals; keyed on the public name it is nothing,
# which is correct, because `from coverage import PathAliases` still works.
final = {}
for record in out.values():
    if record.get("kind") == "meta":
        continue
    name = record["name"]
    previous = final.get(name)
    # Two distinct objects under one public name can only happen if a package
    # rebinds it; keep the first and note the collision rather than silently
    # dropping one.
    if previous is not None:
        previous["shadowed"] = True
        continue
    final[name] = record
final["__meta__"] = out["__meta__"]

print("__BR_JSON__" + json.dumps(final))
"""

CALL = r"""
import importlib, json, re, sys

import os

if not os.path.isdir(sys.argv[1]):
    print("__BR_WRONGDIR__no such directory: " + sys.argv[1])
    raise SystemExit(0)
sys.path.insert(0, sys.argv[1])
payload = json.loads(sys.argv[2])

# Two shapes of object identity, both of which look like a behaviour change and are not.
#   <Foo object at 0x7f...>   - the default repr
#   <py314-none-win @ 2380956229248>  - a custom __repr__ embedding id(self) in DECIMAL
# packaging.tags.Tag uses the second, and normalising only the first reported four
# identical tag lists as four silent behaviour changes.
ADDR = re.compile(r"0x[0-9a-fA-F]{4,}")
DECIMAL_ID = re.compile(r"@ ?\d{7,}")
MAX_ITEMS = 64


def scrub(text):
    return DECIMAL_ID.sub("@ id", ADDR.sub("0x...", text))


def render(value):
    # A lazy iterator reprs identically whatever it would yield, so it is drained first.
    # Without this, any function returning a generator compares equal across versions and
    # a real change in what it produces is invisible.
    if hasattr(value, "__next__") and not isinstance(value, (str, bytes)):
        items = []
        try:
            for i, item in enumerate(value):
                if i >= MAX_ITEMS:
                    return repr(items) + "...(truncated)"
                items.append(item)
        except Exception as exc:
            return repr(items) + "...then " + type(exc).__name__ + ": " + str(exc)[:120]
        return repr(items)
    return repr(value)


def resolve(qualname):
    parts = qualname.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        try:
            mod = importlib.import_module(".".join(parts[:cut]))
        except Exception:
            continue
        obj = mod
        for attr in parts[cut:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(qualname)


results = {}
for qualname, argsets in payload.items():
    rows = []
    try:
        fn = resolve(qualname)
    except Exception as exc:
        results[qualname] = {"error": type(exc).__name__ + ": " + str(exc)[:150]}
        continue
    for src in argsets:
        try:
            args = eval(src)
        except Exception as exc:
            rows.append(["badargs", type(exc).__name__])
            continue
        try:
            rows.append(["ok", scrub(render(fn(*args)))])
        except Exception as exc:
            rows.append(["raise", scrub(type(exc).__name__ + ": " + str(exc)[:150])])
    results[qualname] = {"rows": rows}

print("__BR_JSON__" + json.dumps(results))
"""


class WrongVersionImported(RuntimeError):
    """The probe loaded a different copy of the package than it was pointed at.

    Raised rather than returned, because every downstream number would be wrong and
    plausible: two probes of the same installed copy agree perfectly, and the report says
    the upgrade changes nothing.
    """


def _run(script: str, args: list[str], timeout: float) -> dict | None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.py"
        # newline="" or Windows rewrites the newlines and breaks any continuation.
        path.write_text(script, encoding="utf-8", newline="")
        try:
            proc = subprocess.run(
                [sys.executable, str(path), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=tmp,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
    out = proc.stdout or ""
    if "__BR_WRONGDIR__" in out:
        # Loud on purpose. Silently returning None here would look like "this version has
        # no public API", and the diff would then report every symbol as removed.
        detail = out.split("__BR_WRONGDIR__", 1)[1].strip().splitlines()[0]
        raise WrongVersionImported(f"the import did not come from the target directory: {detail}")
    marker = out.find("__BR_JSON__")
    if marker < 0:
        return None
    try:
        return json.loads(out[marker + len("__BR_JSON__") :])
    except json.JSONDecodeError:
        return None


def surface(target_dir: Path, package: str, timeout: float = 180.0) -> dict | None:
    """Every public callable in an installed version, with its signature."""
    return _run(SURFACE, [str(target_dir.resolve()), package], timeout)


def call(target_dir: Path, payload: dict[str, list[str]], timeout: float = 300.0) -> dict | None:
    """Call each qualname on each argument set, and return what came back as strings."""
    if not payload:
        return {}
    return _run(CALL, [str(target_dir.resolve()), json.dumps(payload)], timeout)
