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
import contextlib, importlib, inspect, io, json, re, sys

import os

if not os.path.isdir(sys.argv[1]):
    print("__BR_WRONGDIR__no such directory: " + sys.argv[1])
    raise SystemExit(0)
sys.path.insert(0, sys.argv[1])

# The payload arrives in a FILE, not on the command line.
#
# It used to be argv[2]. A command line has a length limit, and this payload is
# one entry per stable function with four argument sets each. Measured on
# Windows: 6,123 characters worked, 12,290 returned nothing at all with a zero
# exit code, and a few hundred functions failed outright with
# "[WinError 206] The filename or extension is too long".
#
# The default limit is 400 functions. So on Windows the behaviour pass - the
# only part of this tool that finds what nothing else warns you about - did
# nothing at all on any package big enough to be worth checking, and said so by
# reporting zero silent changes.
with open(sys.argv[2], "r", encoding="utf-8") as fh:
    payload = json.load(fh)

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


class NeedsInstance(Exception):
    # The name is a method and no instance could be made to call it on.
    #
    # A comment, not a docstring: this module is a triple-quoted template and a
    # docstring here closes it. The SURFACE template above says the same thing
    # for the same reason.
    pass


def resolve(qualname):
    # Returns a CALLABLE THAT TAKES NO self.
    #
    # `getattr(SomeClass, "method")` hands back the plain function, which still
    # wants `self` first - while `_params` deliberately excludes `self` from the
    # count, because a caller writing `obj.method(x)` passes one argument. Those
    # two facts together meant the generated literal was bound to `self`: the
    # tool called `Argument.add_to_parser("a", "b")` with "a" as the Argument.
    #
    # That is worse than failing. Most such calls raise TypeError and are
    # counted as unreachable, which is merely noisy - but a method that does not
    # touch `self` will happily run against a string and return something, and
    # the two versions then get compared on a call no user could ever make.
    #
    # So: build an instance when the class allows it and bind the method to it,
    # and otherwise say the name needs an instance rather than inventing one.
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

        if inspect.isclass(owner) and inspect.isfunction(obj):
            # A plain function on a class is an instance method. staticmethod
            # and classmethod do not arrive here: getattr already returns them
            # bound, or as a function with no `self` parameter.
            first = next(iter(inspect.signature(obj).parameters), None)
            if first in ("self", "cls"):
                try:
                    instance = owner()
                except Exception as exc:
                    raise NeedsInstance(
                        owner.__name__ + "() takes constructor arguments: "
                        + type(exc).__name__
                    ) from None
                return getattr(instance, parts[-1])
        return obj
    raise ImportError(qualname)


# Results are written out as they are produced, not only at the end.
#
# Every function in the batch runs in ONE interpreter, so anything that stops
# that interpreter - a hang hitting the timeout, a segfaulting C extension,
# os._exit - used to discard the results of every function that had already
# finished. click 8.1.6 -> 8.1.7 has 234 stable callables; the first 160 are
# probed in about a second, and one function later in the list was enough to
# report all 234 as unreachable.
#
# The journal also names what was in flight when the interpreter died, which is
# the one thing the parent cannot work out for itself.
journal = open(sys.argv[3], "a", encoding="utf-8")


def record(key, value):
    results[key] = value
    journal.write(json.dumps({"q": key, "r": value}) + "\n")
    journal.flush()


results = {}
for qualname, argsets in payload.items():
    journal.write(json.dumps({"q": qualname, "start": 1}) + "\n")
    journal.flush()
    rows = []
    try:
        fn = resolve(qualname)
    except NeedsInstance as exc:
        # Honest unreachability: the name exists and is callable, but only on an
        # object this tool cannot build. Kept separate from a resolve failure,
        # which means the name could not be found at all.
        record(qualname, {"needs_instance": str(exc)[:150]})
        continue
    except Exception as exc:
        record(qualname, {"error": type(exc).__name__ + ": " + str(exc)[:150]})
        continue
    for src in argsets:
        try:
            args = eval(src)
        except Exception as exc:
            rows.append(["badargs", type(exc).__name__])
            continue
        # Two things a called function can do that are not exceptions.
        #
        # It can WRITE TO STDOUT. This channel carries the results as a single
        # __BR_JSON__ line, so anything a function prints corrupts it. click's
        # entry points print their usage text.
        #
        # It can EXIT. SystemExit inherits from BaseException, not Exception,
        # so `except Exception` does not catch it and the interpreter simply
        # stops - mid-run, with no output and a zero exit code. One click
        # command calling sys.exit() ended the whole probe, which is why the
        # published click comparison reported 0 functions exercised and 400
        # unreachable. They were reachable. The first one to exit took the
        # process with it and everything after it was never attempted.
        buf_out, buf_err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                value = fn(*args)
            rows.append(["ok", scrub(render(value))])
        except SystemExit as exc:
            # Recorded, not fatal. A function that exits is a real behaviour and
            # worth comparing across versions like any other outcome.
            rows.append(["exit", "SystemExit: " + str(exc.code)[:80]])
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            rows.append(["raise", scrub(type(exc).__name__ + ": " + str(exc)[:150])])
    record(qualname, {"rows": rows})

journal.close()
print("__BR_JSON__" + json.dumps(results))
"""


class WrongVersionImported(RuntimeError):
    """The probe loaded a different copy of the package than it was pointed at.

    Raised rather than returned, because every downstream number would be wrong and
    plausible: two probes of the same installed copy agree perfectly, and the report says
    the upgrade changes nothing.
    """


def _recover(journal: Path) -> dict | None:
    """Whatever the probe managed to finish before it died, read back off disk.

    Returns None if it never got far enough to produce anything, so that a probe
    which failed immediately is still distinguishable from one that ran.

    The last line may be a half-written record - the interpreter can be killed
    mid-write - so an unparsable line is skipped rather than treated as the end
    of the file.
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
    # The name that was in flight when it died. Recorded even when nothing at all
    # finished - the FIRST function in the batch hanging is exactly the case where
    # the caller most needs to be told which one, and it is the case with no
    # results to carry the news.
    if started is not None and started not in done:
        done["__incomplete__"] = {"died_on": started, "completed": len(done)}
    return done or None


def _run(script: str, args: list[str], timeout: float, journal: Path | None = None) -> dict | None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.py"
        # newline="" or Windows rewrites the newlines and breaks any continuation.
        path.write_text(script, encoding="utf-8", newline="")
        try:
            proc = subprocess.run(
                [sys.executable, str(path), *args],
                capture_output=True,
                # No stdin. A function that reads from it - click.confirm, and
                # every other prompt helper - otherwise blocks until the timeout
                # and takes the batch with it. click.confirm is the function that
                # was stopping the click run at 141 of 234. With the descriptor
                # closed it gets EOF immediately and raises, which is a real
                # behaviour worth comparing like any other.
                stdin=subprocess.DEVNULL,
                # Not text=True: that decodes with the locale codec, which on Windows is
                # cp1252. The child writes UTF-8, and any probed function whose repr or
                # output carries a non-cp1252 character then raises UnicodeDecodeError in
                # the parent - out of a reader thread, so not an OSError, and not caught
                # below. Decoding here with errors="replace" keeps a stray byte from
                # deciding whether the whole run reports anything.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                cwd=tmp,
            )
        except (subprocess.TimeoutExpired, OSError):
            return _recover(journal) if journal else None
    out = proc.stdout or ""
    if "__BR_WRONGDIR__" in out:
        # Loud on purpose. Silently returning None here would look like "this version has
        # no public API", and the diff would then report every symbol as removed.
        detail = out.split("__BR_WRONGDIR__", 1)[1].strip().splitlines()[0]
        raise WrongVersionImported(f"the import did not come from the target directory: {detail}")
    marker = out.find("__BR_JSON__")
    if marker < 0:
        # No summary line: the interpreter did not reach the end. Anything it
        # finished first is still on disk and still worth comparing.
        return _recover(journal) if journal else None
    try:
        return json.loads(out[marker + len("__BR_JSON__") :])
    except json.JSONDecodeError:
        return _recover(journal) if journal else None


def surface(target_dir: Path, package: str, timeout: float = 180.0) -> dict | None:
    """Every public callable in an installed version, with its signature."""
    return _run(SURFACE, [str(target_dir.resolve()), package], timeout)


# Each restart costs one full timeout, so this is a budget, not a target. click
# 8.1.x needs exactly three - getchar, launch and termui.hidden_prompt_func, all
# of which read the console directly rather than stdin, so closing the descriptor
# does not reach them. A cap of three would clear click with nothing to spare,
# and the failure mode of being one short is the tail of the package silently
# counted unreachable.
MAX_RESTARTS = 5


def _attempt(target_dir: Path, payload: dict[str, list[str]], timeout: float) -> dict | None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        blob = Path(tmp) / "payload.json"
        blob.write_text(json.dumps(payload), encoding="utf-8")
        journal = Path(tmp) / "journal.jsonl"
        return _run(
            CALL,
            [str(target_dir.resolve()), str(blob), str(journal)],
            timeout,
            journal=journal,
        )


def call(target_dir: Path, payload: dict[str, list[str]], timeout: float = 300.0) -> dict | None:
    """Call each qualname on each argument set, and return what came back as strings.

    The payload goes through a temporary file rather than the command line,
    which has a length limit the payload routinely exceeded. See the comment at
    the top of CALL.

    One function that never returns should cost one function. It used to cost
    the whole batch: every name shares an interpreter, so a `time.sleep` in a
    default argument, a C extension waiting on a socket, or anything else that
    outlasts the timeout took the results of the functions before it and
    prevented the ones after it from being attempted at all.

    The journal recovers the first group. This loop recovers the second: the
    name the probe died on is recorded as unreachable - which is the truth about
    it, from this tool's point of view - and a fresh interpreter picks up at the
    next name. Restarts are capped, because each one costs a full timeout and a
    package that hangs everywhere should be reported, not waited on.
    """
    if not payload:
        return {}

    results: dict = {}
    stopped_on: list[str] = []
    remaining = list(payload)
    for restart in range(MAX_RESTARTS + 1):
        out = _attempt(target_dir, {q: payload[q] for q in remaining}, timeout)
        if out is None:
            break
        incomplete = out.pop("__incomplete__", None)
        results.update(out)
        if incomplete is None:
            remaining = []
            break
        died_on = incomplete["died_on"]
        if died_on not in remaining:
            # The journal named something this attempt was not asked for, so there
            # is no next name to resume from. Stopping is the only safe move:
            # guessing one risks re-running the name that just killed the probe,
            # and looping on it until the restart cap with nothing to show.
            break
        stopped_on.append(died_on)
        results[died_on] = {"error": "no result: the probe stopped here and was restarted"}
        remaining = remaining[remaining.index(died_on) + 1 :]
        if not remaining or restart == MAX_RESTARTS:
            break

    # Anything still unattempted gets an entry of its own rather than being left
    # out. A name missing from the results is counted as unreachable either way,
    # so leaving it out would quietly turn "this tool gave up" into "this package
    # cannot be exercised" - the one distinction this whole path exists to keep.
    for name in remaining:
        results[name] = {"error": "not attempted: the probe was restarted too many times"}
    if stopped_on:
        results["__stopped_on__"] = stopped_on
    return results or None
