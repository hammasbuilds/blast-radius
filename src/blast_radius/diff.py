"""Compare two versions: what disappeared, what changed shape, and what quietly lies.

The first two questions are answered by reading the surfaces. The third needs execution, and
it is the only one worth building a tool for - a name that vanished announces itself at
import, and a changed signature is caught by anything that type-checks. A function that kept
its name and its signature and now returns something else announces nothing at all.

Which is why the behaviour pass deliberately runs **only on symbols that survived unchanged
in both name and signature**. Comparing behaviour across a signature change would find
differences that the signature already explained, and burying the silent ones among them is
how this report would become another changelog.
"""

from __future__ import annotations

import ast
import os
import re
import tokenize
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import NamedTuple

from blast_radius.probe import call
from blast_radius.types import BREAKING, Change, Kind

# Values chosen to exercise the shapes a public API usually takes. The pool is deliberately
# small: the point is to detect a difference, not to explore an input space.
POOL = [
    '""',
    '"1.0"',
    '"1.0.0"',
    '"2.0.dev1"',
    '"1.0-alpha"',
    '"x"',
    '"a b"',
    "0",
    "1",
    "-1",
    "2",
    "None",
    "True",
    "[]",
    "[1, 2]",
    "()",
    "{}",
]


# Per-annotation pools. The generic POOL above is tried against every parameter,
# which means a parameter annotated `int` gets `""` first and raises TypeError -
# and that raise is indistinguishable in the report from a function that rejects
# its input. Measured on click 8.1.6 -> 8.1.7: of 234 stable callables, 81 raised
# on every generated argument set. Most of those are this, not the package.
#
# `call_shape` deliberately ignores annotations when deciding whether two
# signatures match, because adding a type hint cannot break a caller. That is a
# statement about COMPARISON. It is not a reason to ignore them when choosing
# what to pass, which is what this fixes.
TYPED_POOL: dict[str, list[str]] = {
    "int": ["1", "0", "-1", "2"],
    "float": ["1.0", "0.0", "-1.5"],
    "complex": ["1j"],
    "bool": ["True", "False"],
    "str": ['"x"', '""', '"a b"', '"1.0"'],
    "bytes": ['b"x"', 'b""'],
    "list": ["[]", "[1, 2]"],
    "tuple": ["()", "(1, 2)"],
    "set": ["set()", "{1, 2}"],
    "frozenset": ["frozenset()"],
    "dict": ["{}", '{"a": 1}'],
    "sequence": ["[]", "[1, 2]"],
    "iterable": ["[]", "[1, 2]"],
    "iterator": ["iter([])", "iter([1, 2])"],
    "mapping": ["{}", '{"a": 1}'],
    "callable": ["(lambda *a, **k: None)"],
    "path": ['__import__("pathlib").Path(".")', '"."'],
    "none": ["None"],
}


def _pool_for(annotation: str | None) -> list[str]:
    """Values worth passing to a parameter annotated like this.

    Matching is on substrings of the annotation text rather than on resolved
    types, because the parent process never imports the package - it only has the
    signature string the probe reported. `dict[str, int]`, `Mapping[str, Any]` and
    `Optional[Dict]` all have to be recognised from their spelling.
    """
    if not annotation:
        return POOL
    text = annotation.strip().lower()
    # `None` and `Optional` are stripped BEFORE matching, not matched alongside.
    # Otherwise `str | None` matches the "none" key - which is four characters and
    # so sorts ahead of "str" - and the parameter is offered nothing but None.
    optional = bool(re.search(r"\bnone\b|\boptional\b", text))
    if optional:
        text = re.sub(r"\boptional\b|\bnone\b", " ", text)
    # Longest key first, so "frozenset" is not matched as "set".
    for key in sorted(TYPED_POOL, key=len, reverse=True):
        if key == "none":
            continue
        if re.search(rf"\b{key}\b", text):
            values = list(TYPED_POOL[key])
            if optional:
                values.append("None")
            return values
    return ["None", *POOL] if optional else POOL


def _param_annotations(signature: str) -> list[str | None]:
    """The annotation text of each positional parameter, `self` excluded.

    Parsed from the signature STRING because that is all that crosses the process
    boundary. A real `inspect.Parameter` would be easier and would require
    importing the package here, which is the one thing this design does not do.
    """
    text = signature.strip()
    if text.startswith("("):
        # Find the paren that closes the parameter list rather than assuming it is
        # the last character. "(value: int) -> bool" ends in "l", and slicing
        # [1:-1] silently dropped the final parameter.
        depth = 0
        for i, ch in enumerate(text):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    inner = text[1:i]
                    break
        else:
            inner = text[1:]
    else:
        inner = text.split("->")[0]
    out: list[str | None] = []
    depth, current = 0, ""
    for ch in inner + ",":
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            piece = current.strip()
            current = ""
            if not piece or piece == "/":
                # "/" marks the end of positional-only parameters. The ones before
                # it are still positional, so keep going.
                continue
            if piece.startswith("*"):
                # A bare "*", "*args" or "**kwargs": everything after this point is
                # keyword-only and cannot be passed positionally. Stop rather than
                # skip, or a keyword-only parameter gets counted as positional and
                # every generated call raises TypeError.
                break
            name, _, rest = piece.partition(":")
            if name.strip() in ("self", "cls"):
                continue
            annotation = rest.split("=")[0].strip() if rest else None
            out.append(annotation or None)
            continue
        current += ch
    return out


def _params(signature: str) -> int:
    """How many positional parameters a signature takes, `self` excluded.

    One parser, not two. This used to split the parameter list with its own regex
    and its own `[1:-1]` slice, and both were wrong about the same thing: a return
    annotation. On `(value: int, name: str = "x") -> bool` the slice left a stray
    `)` in the text, the regex read the comma as being inside brackets, and the
    function was reported as taking ONE parameter.

    Every annotated function with two or more parameters was therefore called with
    too few arguments, raised TypeError on every attempt, and was counted as a
    function that rejects all input. click annotated its entire public API in
    version 8.
    """
    return len(_param_annotations(signature))


def argument_sets(signature: str, cap: int = 12) -> list[str]:
    """Call strings for a signature, varying one parameter at a time around a baseline.

    Each parameter draws from a pool chosen for its annotation where it has one, so
    a parameter annotated `int` is never offered `""` first. Unannotated
    parameters fall back to the generic pool, which is what every parameter used
    to get.
    """
    n = _params(signature)
    if n == 0:
        return ["()"]
    if n > 3:
        # Beyond three parameters the baseline is unlikely to be valid for any of them,
        # and a call that raises on both sides establishes nothing.
        n = 3
    annotations = _param_annotations(signature)
    pools = [_pool_for(annotations[i] if i < len(annotations) else None) for i in range(n)]
    baseline = [pool[0] for pool in pools]
    out = ["(" + ", ".join(baseline) + ",)"]
    for i in range(n):
        for value in pools[i][1:]:
            args = list(baseline)
            args[i] = value
            out.append("(" + ", ".join(args) + ",)")
            if len(out) >= cap:
                return out
    return out


class Param(NamedTuple):
    name: str
    kind: str
    default: bool


def _parse_shape(shape: str) -> list[Param]:
    """`name:KIND[:d],...` (the probe's call shape) -> [Param]."""
    out = []
    for part in shape.split(","):
        if not part:
            continue
        bits = part.split(":")
        out.append(
            Param(bits[0], bits[1] if len(bits) > 1 else "", len(bits) > 2 and bits[2] == "d")
        )
    return out


_POSITIONAL = ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")
_KINDS = frozenset({*_POSITIONAL, "VAR_POSITIONAL", "KEYWORD_ONLY", "VAR_KEYWORD"})
_NAMED = ("POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY")
_VAR = ("VAR_POSITIONAL", "VAR_KEYWORD")
_RECEIVERS = ("self", "cls")


def compatible(old_shape: str | None, new_shape: str | None) -> bool:
    """Is every call that is valid against the old shape still valid against the new one,
    binding each argument to the same parameter?

    Shapes are the probe's `name:KIND[:d],...` strings. An empty string is a function
    that takes no arguments; None is a signature that could not be read, and is never
    called compatible - "could not read it" must not read as "safe". (`api_changes`
    tells unknown apart before it gets here, and does not report it as a change.)

    Each rule is a way an existing call could stop working:

    * Positional capacity: the new function takes at least as many positional arguments
      as the old one, or has a `*args` that absorbs them. If the old one had `*args`,
      the new one keeps it (it may add optional positional slots in front of it: the
      call still binds, though a value that used to reach `*args` now has a name).
    * An old positional slot that callers may also pass BY NAME keeps its name and stays
      passable by name. A slot absorbed by a new `*args` is fine while `**kwargs` still
      accepts the name: `attr.evolve(inst, **c)` -> `(*args, **c)` breaks no call.
    * Every old keyword-only name is still accepted by keyword.
    * Nothing becomes required that an old call may have left out: no default taken
      away, no new required positional, no new required keyword-only parameter.
    * An old `**kwargs` stays.
    """
    if old_shape is None or new_shape is None:
        return False
    if old_shape == new_shape:
        return True
    old, new = _parse_shape(old_shape), _parse_shape(new_shape)
    if any(p.kind not in _KINDS for p in old + new):
        return False  # not a call shape
    # A method's receiver is bound by the attribute lookup, never passed by a caller, so
    # its name and kind are not API. bs4 4.13 dropped an `encode(self, encoding)`
    # override and inherited str.encode's `(self, /, encoding='utf-8', ...)`: reading the
    # positional-only `self` as a change reported a reshape nobody can trip on.
    if old and new and old[0].name in _RECEIVERS and new[0].name in _RECEIVERS:
        old, new = old[1:], new[1:]
    old_pos = [p for p in old if p.kind in _POSITIONAL]
    new_pos = [p for p in new if p.kind in _POSITIONAL]
    old_var = any(p.kind == "VAR_POSITIONAL" for p in old)
    new_var = any(p.kind == "VAR_POSITIONAL" for p in new)
    old_kw = any(p.kind == "VAR_KEYWORD" for p in old)
    new_kw = any(p.kind == "VAR_KEYWORD" for p in new)
    old_by_name = {p.name: p for p in old if p.kind not in _VAR}
    new_by_name = {p.name: p for p in new if p.kind not in _VAR}

    # Positional capacity. New optional slots in front of a kept *args are allowed:
    # every old call still binds (pytest 9's raises(exc, *args) -> raises(exc,
    # func=None, *args) is exactly this), which is the definition used throughout.
    if old_var and not new_var:
        return False
    if len(new_pos) < len(old_pos) and not new_var:
        return False

    # Each old positional slot.
    for i, o in enumerate(old_pos):
        if i < len(new_pos):
            n = new_pos[i]
            if o.kind == "POSITIONAL_OR_KEYWORD" and (n.name != o.name or n.kind != o.kind):
                return False  # renamed, shifted, or made positional-only
            if o.default and not n.default:
                return False  # a caller leaving it out now fails
            continue
        # Absorbed by the new *args. A positional call still binds; a keyword call needs
        # the name accepted, and not by a separate keyword-only parameter - positional
        # callers would leave that parameter unset.
        if o.kind == "POSITIONAL_OR_KEYWORD" and (o.name in new_by_name or not new_kw):
            return False

    # New positional slots past the old ones must be optional - unless every old call
    # already passed that name by keyword, because it was keyword-only and required.
    for n in new_pos[len(old_pos) :]:
        if n.default:
            continue
        o = old_by_name.get(n.name)
        if not (o and o.kind == "KEYWORD_ONLY" and not o.default and n.kind in _NAMED):
            return False

    # Old keyword-only parameters are still accepted by name.
    for o in old:
        if o.kind != "KEYWORD_ONLY":
            continue
        n = new_by_name.get(o.name)
        if n is None:
            if not new_kw:
                return False
            continue
        if n.kind not in _NAMED or (o.default and not n.default):
            return False
        if n.kind == "POSITIONAL_OR_KEYWORD" and new_pos.index(n) < len(old_pos):
            return False  # old positional callers now fill it AND pass it by name

    # A new required keyword-only parameter must be one every old call already passed.
    for n in new:
        if n.kind == "KEYWORD_ONLY" and not n.default:
            o = old_by_name.get(n.name)
            if o is None or o.default or o.kind != "KEYWORD_ONLY":
                return False

    return not (old_kw and not new_kw)


def _shape(info: dict) -> str | None:
    """The call shape, or None when the probe could not read a signature."""
    if "shape" in info:
        return info["shape"]
    return info.get("signature") or None


def _informative(shape: str | None) -> str | None:
    """The shape, or None when it says nothing about what a call may pass.

    A bare `(*args, **kwargs)` is a forwarding wrapper (a decorator, a `__new__`, a
    metaclass), not a contract: the real constraints live wherever it forwards to. bs4
    4.12's HTMLFormatter read as `(*args, **kwargs)`, and its real, compatible
    constructor in 4.13 was reported as a reshape because it no longer accepts "anything".
    """
    if shape is None:
        return None
    params = [p for p in _parse_shape(shape) if p.name not in _RECEIVERS]
    if [p.kind for p in params] == ["VAR_POSITIONAL", "VAR_KEYWORD"]:
        return None
    return shape


def _index(syms: dict) -> dict[str, dict]:
    """Every name and alias -> its record, canonical names first."""
    out: dict[str, dict] = dict(syms)
    for info in syms.values():
        for alias in info.get("aliases", []):
            out.setdefault(alias, info)
    return out


def _symbols(surface: dict) -> dict[str, dict]:
    return {k: v for k, v in surface.items() if k != "__meta__"}


def _counterpart(name: str, info: dict, index: dict[str, dict]) -> dict | None:
    """The record for `name` in the other version: listed there under that exact path,
    as its public name or as an alias.

    Only `name` itself counts, never the old record's other aliases. urllib3 1.26's
    `urllib3.request.RequestMethods` was also importable as
    `urllib3.poolmanager.RequestMethods`; in 2.x the second path survives and the first
    does not, and matching on any alias reported the removed import as still there.
    """
    return index.get(name)


def gone_candidates(old: dict, new: dict) -> list[str]:
    """Old names the new surface does not list, as a public name or an alias.

    Candidates, not verdicts: a name is only reported gone after the new version has
    also failed to resolve it at runtime (see probe.resolve_names).
    """
    new_index = _index(_symbols(new))
    return [
        name
        for name, info in sorted(_symbols(old).items())
        if _counterpart(name, info, new_index) is None
    ]


def unknown_signatures(old: dict, new: dict, resolved: dict | None = None) -> list[str]:
    """Names present in both versions whose signature is readable in only one.

    numpy 2.5 made dozens of C functions introspectable, and "(unknown) -> (a, b)" was
    reported as 84 reshaped functions. Nothing a caller does changed; the tool can now
    read what it could not before. An old bare `(*args, **kwargs)` counts as unreadable
    too (see `_informative`). Counted, never reported as a change.
    """
    new_index = _index(_symbols(new))
    resolved = resolved or {}
    out = []
    for name, info in sorted(_symbols(old).items()):
        if info.get("kind") not in ("function", "class"):
            continue
        other = _counterpart(name, info, new_index) or resolved.get(name)
        if other is None or other.get("kind") not in ("function", "class"):
            continue
        if _shape(info) == _shape(other):
            continue
        if _informative(_shape(info)) is None or _shape(other) is None:
            out.append(name)
    return out


def api_changes(old: dict, new: dict, resolved: dict | None = None) -> list[Change]:
    """GONE, RESHAPED, WIDENED and ADDED - everything readable without running anything.

    An old name is found in the new version if the new surface lists that exact path,
    as a public name or as an alias - so a method that moved to a base class, and is
    now an alias of `Base.method`, is not "gone". `resolved` holds old names the new surface walk
    did not list but that still resolve in the new version (pydantic 2 serves
    `pydantic.json.pydantic_encoder` from a module `__getattr__`): an import of them
    still works, so they are not gone either.
    """
    old_syms, new_syms = _symbols(old), _symbols(new)
    new_index, old_index = _index(new_syms), _index(old_syms)
    resolved = resolved or {}
    out: list[Change] = []

    def aliases(name: str, *infos: dict) -> list[str]:
        seen = {a for info in infos for a in info.get("aliases", [])}
        return sorted(seen - {name})

    for name, info in sorted(old_syms.items()):
        other = _counterpart(name, info, new_index) or resolved.get(name)
        if other is None:
            out.append(
                Change(
                    Kind.GONE,
                    name,
                    f"a {info['kind']} that no longer exists",
                    aliases=aliases(name, info),
                )
            )
            continue
        callable_kinds = ("function", "class")
        # pytest 9's `pytest.fail` and friends are callable objects, not functions:
        # still called exactly as before, so only a NON-callable replacement counts.
        still_callable = other.get("kind") in callable_kinds or other.get("callable")
        if info.get("kind") in callable_kinds and not still_callable:
            now = other.get("kind", "different object")
            out.append(
                Change(
                    Kind.RESHAPED,
                    name,
                    f"was a {info['kind']}, is now a {now}",
                    aliases=aliases(name, info, other),
                    before=info["kind"],
                    after=now,
                )
            )
            continue
        old_shape, new_shape = _shape(info), _shape(other)
        if old_shape == new_shape:
            continue
        # Only the OLD side can be uninformative: a new `(*args, **kwargs)` accepts every
        # call, and compatible() says so.
        old_shape = _informative(old_shape)
        if old_shape is None or new_shape is None:
            continue  # unchanged, or not comparable (counted by unknown_signatures)
        kind = Kind.WIDENED if compatible(old_shape, new_shape) else Kind.RESHAPED
        before, after = info.get("signature") or "()", other.get("signature") or "()"
        out.append(
            Change(
                kind,
                name,
                f"{before} -> {after}",
                aliases=aliases(name, info, other),
                before=before,
                after=after,
            )
        )
    for name, info in sorted(new_syms.items()):
        if _counterpart(name, info, old_index) is not None:
            continue
        out.append(Change(Kind.ADDED, name, f"new {info['kind']}", aliases=aliases(name, info)))
    return out


def stable_callables(
    old: dict, new: dict, limit: int | None = None, resolved: dict | None = None
) -> dict[str, str]:
    """Functions present in both versions that every old call still binds against.

    These are the only ones worth executing. A function whose shape changed incompatibly
    has already been reported, and a behaviour difference there would be explained by the
    shape. A WIDENED one is run on calls built from the OLD signature, which bind
    identically in both - so any difference is behaviour, not shape.
    """
    new_index = _index(_symbols(new))
    resolved = resolved or {}
    out: dict[str, str] = {}
    for name, info in sorted(_symbols(old).items()):
        if info["kind"] != "function":
            continue
        other = _counterpart(name, info, new_index) or resolved.get(name)
        if not other or not compatible(_shape(info), _shape(other)):
            continue
        out[name] = info["signature"]
        if limit and len(out) >= limit:
            break
    return out


# Words that mark a function as one that DOES something to the machine rather than
# computing an answer. Matched against the words of the function's own name (split on
# underscores and camelCase), not substrings: `open` catches `open_file` and `urlopen`
# but not `opener`. Deliberately broad - a function skipped here costs one comparison;
# a function run here cost one user four Explorer windows (click.launch) and a Notepad
# left open (click.edit).
_UNSAFE_WORDS = frozenset(
    {
        "launch", "open", "urlopen", "startfile", "edit", "editor", "system", "exec",
        "execute", "spawn", "popen", "run", "main", "kill", "terminate", "shutdown",
        "remove", "delete", "unlink", "rmtree", "rmdir", "mkdir", "makedirs", "rename",
        "move", "chmod", "chown", "chdir", "touch", "truncate", "write", "save", "dump",
        "send", "sendall", "sendmail", "upload", "download", "fetch", "urlretrieve",
        "connect", "listen", "serve", "bind", "input", "prompt", "getpass", "getchar",
        "pause", "confirm", "pager", "sleep", "wait", "exit", "quit", "install",
        "uninstall", "browser", "clipboard",
    }
)  # fmt: skip

# HTTP verbs are unsafe only on something that talks to the network: `requests.get` and
# `Session.post` are; `Headers.get` and every dict-like `.get` are not.
_HTTP_WORDS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
)
_NETWORK_OWNER = re.compile(
    r"session|client|pool|connection|transport|adapter|opener|http|request|api", re.I
)


def _words(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [w for w in spaced.lower().split("_") if w]


def unsafe_to_call(qualname: str) -> str | None:
    """Why this function must not be executed, or None if its name suggests it is safe."""
    parts = qualname.split(".")
    leaf = parts[-1]
    hit = next((w for w in _words(leaf) if w in _UNSAFE_WORDS), None)
    if hit:
        return f"its name suggests a side effect ({hit})"
    if leaf.lower() in _HTTP_WORDS:
        owner = parts[-2] if len(parts) > 1 else ""
        # A module-level verb (requests.get, httpx.post) or a verb on a client class.
        if owner[:1].islower() or _NETWORK_OWNER.search(owner):
            return f"its name suggests a network request ({leaf})"
    return None


def _row_key(row: list) -> tuple:
    return tuple(row)


def _strength(a: list, b: list) -> int:
    """How convincing one disagreement is. Lower is better.

    A drained iterator that raised renders as `ok: [...]...then TypeError`, which starts
    with `ok` and is an exception - reading only the prefix promotes it to unarguable.
    """

    def clean(row: list) -> bool:
        return row[0] == "ok" and "...then " not in str(row[1])

    ca, cb = clean(a), clean(b)
    if ca and cb:
        return 0
    return 1 if (ca or cb) else 2


_WRONG_TYPE = ("TypeError", "AttributeError")


def _wrong_type(a: list, b: list) -> bool:
    """Does one side of this disagreement reject the argument as the wrong type?"""
    return any(row[0] == "raise" and str(row[1]).startswith(_WRONG_TYPE) for row in (a, b))


class Behaviour(NamedTuple):
    """What the behaviour pass found. See `behaviour_changes`."""

    silent: list[Change]
    compared: int
    unreachable: int
    stopped_on: list[str]
    reasons: dict[str, int]
    weak: list[Change]
    nondeterministic: list[str]


# How each version re-runs a function that disagreed across versions. A disagreement
# is only reported on inputs where every re-run of BOTH versions reproduced that
# version's own first answer exactly.
#
# Each re-run is a fresh interpreter, later in time, with a different history of earlier
# calls, so randomness (jinja2's generate_lorem_ipsum, bs4's diagnose.rword), clocks
# (urllib3's Timeout.start_connect) and fresh identifiers (choose_boundary) show up as a
# version disagreeing with itself. The two orders catch state: "twice" calls every input
# a second time after the first pass and "reversed" calls them backwards, so a result
# that depends on how many calls came before it - attr.ib's process-global counter read
# 21 in one version and 26 in the other - changes between runs of the same version.
RERUNS = ("twice", "reversed")


def _rerun_plan(argsets: list[str], how: str) -> list[str]:
    if how == "twice":
        return argsets + argsets
    if how == "reversed":
        return argsets[::-1]
    return list(argsets)


def _rerun_rows(rows: list, n: int, how: str) -> list[list]:
    """The re-run's rows mapped back to the original input order, one list per pass."""
    if how == "twice":
        return [rows[:n], rows[n:]]
    if how == "reversed":
        return [rows[::-1]]
    return [rows]


Progress = Callable[[str, int, int], None]


def behaviour_changes(
    old_dir,
    new_dir,
    stable: dict[str, str],
    timeout: float = 20.0,
    widened: set[str] | frozenset[str] = frozenset(),
    versions: list[str] | None = None,
    progress: Progress | None = None,
    reruns: tuple[str, ...] = RERUNS,
    sandbox: bool = True,
) -> Behaviour:
    """Run every stable function on the same inputs in both versions and compare.

    `weak` holds functions whose ONLY disagreements are on inputs one version rejects as
    the wrong type - a TypeError or AttributeError on a generated argument, such as a
    string passed where a `Context` belongs. No real caller passes those; they are
    reported apart, never as SILENT.

    `nondeterministic` holds functions whose disagreement did not survive re-running:
    some version gave a different answer to the same call the second time. They are
    neither evidence of a change nor of stability, and never counted as SILENT.

    `compared` counts only functions that actually ran somewhere. A function that raised
    on every input in both versions was never exercised and is counted in `unreachable`,
    with the reason in `reasons`. `stopped_on` names the functions the probe's
    interpreter had to be restarted past - a fact about this tool, not the package.
    """
    payload = {name: argument_sets(sig) for name, sig in stable.items()}

    def run(where, label: str, calls: dict[str, list[str]]) -> dict | None:
        report = (lambda done, total: progress(label, done, total)) if progress else None
        return call(where, calls, timeout, versions=versions, sandbox=sandbox, progress=report)

    old_res = run(old_dir, "old", payload)
    new_res = run(new_dir, "new", payload)
    stopped_on: list[str] = []
    for res in (old_res, new_res):
        if res:
            for name in res.pop("__stopped_on__", []):
                if name not in stopped_on:
                    stopped_on.append(name)
    if old_res is None or new_res is None:
        why = {"the probe produced nothing": len(payload)}
        return Behaviour([], 0, len(payload), stopped_on, why, [], [])

    compared = unreachable = 0
    reasons: dict[str, int] = {}

    def unreached(why: str) -> None:
        nonlocal unreachable
        unreachable += 1
        reasons[why] = reasons.get(why, 0) + 1

    # name -> (rows_a, rows_b, exercised indices), for every function that disagreed.
    disagreeing: dict[str, tuple[list, list, list[int]]] = {}
    for name in payload:
        a, b = old_res.get(name, {}), new_res.get(name, {})
        if "rows" not in a or "rows" not in b:
            missing = a if "rows" not in a else b
            if "needs_instance" in missing:
                unreached("needs an instance this tool could not build")
            elif "error" in missing:
                unreached("could not be resolved or the probe stopped there")
            else:
                unreached("no result from one of the two versions")
            continue
        if a.get("instance") != b.get("instance"):
            # The method ran on differently built objects, so a difference in what it
            # returns says nothing about the method.
            unreached("the two versions needed different constructor arguments")
            continue
        rows_a, rows_b = a["rows"], b["rows"]
        if len(rows_a) != len(rows_b):
            unreached("the two versions produced different numbers of rows")
            continue
        exercised = [
            i
            for i, (ra, rb) in enumerate(zip(rows_a, rows_b, strict=True))
            if ra[0] == "ok" or rb[0] == "ok"
        ]
        if not exercised:
            # TypeError on every attempt means the arguments were the wrong shape and
            # this tool never reached the function; anything else means it ran and
            # refused, which is the package's own behaviour.
            every = [r[1] for r in rows_a + rows_b]
            if every and all(t.startswith("TypeError") for t in every):
                unreached("never validly called - every argument set was the wrong type")
            else:
                unreached("called, and rejected every input")
            continue
        compared += 1
        if any(_row_key(rows_a[i]) != _row_key(rows_b[i]) for i in exercised):
            disagreeing[name] = (rows_a, rows_b, exercised)

    # Re-run every disagreement and keep only the inputs on which BOTH versions
    # reproduce their own first answer in every run.
    reproducible: dict[str, set[int]] = {
        name: set(range(len(rows_a))) for name, (rows_a, _b, _e) in disagreeing.items()
    }
    for n, how in enumerate(reruns if disagreeing else ()):
        again = {name: _rerun_plan(payload[name], how) for name in disagreeing}
        for where, first, label in ((old_dir, 0, "old"), (new_dir, 1, "new")):
            res = run(where, f"{label}, re-run {n + 1} of {len(reruns)}", again) or {}
            for name, rows in disagreeing.items():
                got = res.get(name, {}).get("rows")
                want = rows[first]
                if got is None or len(got) != len(again[name]):
                    reproducible[name] = set()
                    continue
                for attempt in _rerun_rows(got, len(want), how):
                    reproducible[name] &= {
                        i for i, row in enumerate(attempt) if _row_key(row) == _row_key(want[i])
                    }

    changes: list[Change] = []
    weak: list[Change] = []
    nondeterministic: list[str] = []
    for name, (rows_a, rows_b, exercised) in disagreeing.items():
        diffs = [
            i
            for i in exercised
            if i in reproducible[name] and _row_key(rows_a[i]) != _row_key(rows_b[i])
        ]
        if not diffs:
            nondeterministic.append(name)
            continue
        argsets = payload[name]
        # Lead with a disagreement where both sides returned a value: two differing
        # results are unarguable, where two differing exception types on an argument
        # neither version wanted mostly says the argument was wrong.
        strong = [i for i in diffs if not _wrong_type(rows_a[i], rows_b[i])]
        best = min(strong or diffs, key=lambda i: _strength(rows_a[i], rows_b[i]))
        (changes if strong else weak).append(
            Change(
                Kind.SILENT,
                name,
                (
                    f"old calls still bind (signature widened from {stable[name]})"
                    if name in widened
                    else f"same signature {stable[name]}"
                )
                + f"; {len(strong or diffs)} of {len(exercised)} exercised inputs disagree",
                witness={
                    "args": argsets[best],
                    "old": f"{rows_a[best][0]}: {rows_a[best][1]}",
                    "new": f"{rows_b[best][0]}: {rows_b[best][1]}",
                },
            )
        )
    return Behaviour(changes, compared, unreachable, stopped_on, reasons, weak, nondeterministic)


def _bindings(tree: ast.Module, packages: list[str]) -> tuple[dict[str, str], set[str]]:
    """Local name -> the dotted path in `package` it refers to, from this file's imports.

    This is what makes a call site a fact rather than a coincidence. Matching on the
    trailing attribute name alone credits any project that happens to define its own
    `parse` with calling `packaging.version.parse` - and a report that names files
    which do not use the package is a report people stop reading.

    Resolution is complete for the ways a symbol can actually arrive:

        import packaging.version              packaging.version -> packaging.version
        import packaging.version as v         v                 -> packaging.version
        from packaging import version         version           -> packaging.version
        from packaging.version import parse   parse             -> packaging.version.parse
        from packaging.version import parse as p   p            -> packaging.version.parse

    What it does not cover is dynamic access - getattr on a module, importlib by
    string - which no static pass can see and which is rare in call sites that
    matter.
    """

    def ours(dotted: str) -> bool:
        return any(dotted == p or dotted.startswith(p + ".") for p in packages)

    def parent_of_ours(dotted: str) -> bool:
        # `import jaraco` / `from jaraco import functools` for the namespace package
        # jaraco.functools: the import names a parent of the package.
        return any(p.startswith(dotted + ".") for p in packages)

    out: dict[str, str] = {}
    starred: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if ours(alias.name) or (alias.asname is None and parent_of_ours(alias.name)):
                    out[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue  # a relative import cannot reach a third-party package
            whole = ours(node.module)
            if not whole and not parent_of_ours(node.module):
                continue
            for alias in node.names:
                if not whole and not ours(f"{node.module}.{alias.name}"):
                    continue
                if alias.name == "*":
                    # `from packaging.version import *` binds every public name in
                    # that module and there is no way to know which from here. The
                    # module is recorded so bare names can be tried against it.
                    starred.add(node.module)
                    out[node.module] = node.module
                    continue
                out[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return out, starred


def _dotted(node: ast.AST) -> str | None:
    """ "a.b.c" for an attribute chain rooted in a plain name, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


# Directories that hold somebody else's code. Anything under them is a dependency, a build
# product or a cache, and crediting it as "your code" puts a vendored copy of the package
# itself at the top of the report. A directory holding `pyvenv.cfg` is a virtualenv whatever
# it is called, and is skipped by that test rather than by name.
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "__pycache__", "node_modules",
        ".venv", "venv", ".env", "virtualenv", ".virtualenv",
        "site-packages", "dist-packages", "__pypackages__",
        ".eggs", ".tox", ".nox",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis",
    }
)  # fmt: skip

# Skipped only when they are NOT a Python package. `build/` is usually setuptools output,
# but pypa/build keeps its own source in src/build/ - skipping that by name dropped the
# very project the README uses as its example, and reported that it used nothing.
SKIP_UNLESS_PACKAGE = frozenset({"build", "dist", "env"})


def _skip_dir(parent: Path, name: str) -> bool:
    if name in SKIP_DIRS or name.endswith(".egg-info"):
        return True
    here = parent / name
    if (here / "pyvenv.cfg").exists():
        return True  # a virtualenv, whatever it is called
    return name in SKIP_UNLESS_PACKAGE and not (here / "__init__.py").exists()


@dataclass
class Scan:
    """What `find_call_sites` looked at, so the caller can say what it could NOT read."""

    files: int = 0
    unreadable: list[str] = dc_field(default_factory=list)


def _python_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    out: list[Path] = []
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not _skip_dir(Path(here), d))
        out.extend(Path(here) / f for f in sorted(files) if f.endswith((".py", ".pyi")))
    return out


def _read_source(path: Path) -> str:
    """The file's text, decoded the way Python itself would decode it.

    `tokenize.open` honours a PEP 263 coding cookie and a UTF-8 BOM. Reading everything as
    UTF-8 silently dropped any file declaring `# -*- coding: latin-1 -*-`, and a dropped
    file is a call site nobody is told about.
    """
    with tokenize.open(path) as fh:
        return fh.read()


def find_call_sites(repo, package: str | list[str], changes: list[Change]) -> Scan:
    """Fill in `used_at` for every change the target project actually references.

    `repo` may be a directory or a single `.py` file. Returns what was scanned, including
    the files that could not be read or parsed - those are reported rather than silently
    dropped, because an unread file is exactly where a missed call site would be.
    """
    repo = Path(repo)
    packages = [package] if isinstance(package, str) else list(package)
    roots = {p.split(".")[0] for p in packages}
    scan = Scan()
    by_qualname: dict[str, Change] = {}
    for c in changes:
        for name in (c.qualname, *c.aliases):
            by_qualname.setdefault(name, c)
    base = repo.parent if repo.is_file() else repo
    # Methods by their bare name, for calls on an object whose type is not static.
    methods: dict[str, list[Change]] = {}
    for c in changes:
        owner, _, leaf = c.qualname.rpartition(".")
        # Breaking kinds only: a possible call to a method that merely gained a
        # parameter is noise, not a warning.
        if (
            c.kind in BREAKING
            and owner.rpartition(".")[2][:1].isupper()
            and not leaf.startswith("_")
        ):
            methods.setdefault(leaf, []).append(c)

    for p in _python_files(repo):
        rel = str(p.relative_to(base)).replace("\\", "/")
        try:
            src = _read_source(p)
        except (SyntaxError, UnicodeDecodeError, LookupError, OSError):
            scan.unreadable.append(rel)
            continue
        scan.files += 1
        if not any(root in src for root in roots):
            continue  # the module never mentions the package at all
        try:
            tree = ast.parse(src, filename=str(p))
        except (SyntaxError, ValueError):
            scan.unreadable.append(rel)
            continue
        bound, starred = _bindings(tree, packages)
        if not bound:
            # It mentions the package in a string or a comment but imports nothing
            # from it. Nothing in this file can be a call site.
            continue

        # The import line is a call site in its own right, and the earliest one: a
        # removed name fails at import, before any of the code that uses it runs.
        # `from packaging.version import *` has no other site to find, since the
        # names it binds cannot be enumerated from here.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [a.name for a in node.names]
            elif node.module and not node.level:
                imported = [node.module] + [
                    f"{node.module}.{a.name}" for a in node.names if a.name != "*"
                ]
            for name in imported:
                change = by_qualname.get(name)
                if change is not None:
                    site = f"{rel}:{getattr(node, 'lineno', 0)}"
                    if site not in change.used_at:
                        change.used_at.append(site)

        def maybe(node: ast.AST, rel: str = rel) -> None:
            # `req.specifier.contains(...)`: a method called on an object whose
            # type only exists at runtime. It cannot be proven to be the package's,
            # and it cannot be ignored either - pypa/build calls a silently changed
            # SpecifierSet.contains exactly like this. Recorded as possible, and
            # only in a file that imports the package.
            if isinstance(node, ast.Attribute) and node.attr in methods:
                site = f"{rel}:{getattr(node, 'lineno', 0)}"
                for c in methods[node.attr]:
                    if site not in c.maybe_at:
                        c.maybe_at.append(site)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Name | ast.Attribute):
                continue
            path = _dotted(node)
            if path is None:
                maybe(node)
                continue
            # Resolve the LONGEST bound prefix, not the first segment. `import
            # packaging.version` binds the dotted name "packaging.version", so
            # splitting `packaging.version.parse` at the first dot looks up
            # "packaging" and finds nothing.
            resolved = None
            parts = path.split(".")
            for cut in range(len(parts), 0, -1):
                target = bound.get(".".join(parts[:cut]))
                if target is not None:
                    rest = parts[cut:]
                    resolved = ".".join([target, *rest]) if rest else target
                    break
            if resolved is None:
                # A bare name under `from pkg.mod import *`. The star binds names
                # this pass cannot enumerate, so the only honest resolution is to
                # try it against the modules that were star-imported - which is
                # narrow enough to stay precise, and the alternative is dropping
                # the file entirely.
                if isinstance(node, ast.Name):
                    for mod in starred:
                        if f"{mod}.{node.id}" in by_qualname:
                            resolved = f"{mod}.{node.id}"
                            break
                if resolved is None:
                    maybe(node)
                    continue
            change = by_qualname.get(resolved)
            if change is None and any(p.startswith("_") for p in resolved.split(".")[1:]):
                # Every public path to an object is already in `by_qualname`, as an
                # alias. What is left is a caller reaching in through a private
                # module (`pkg._impl.Thing`), which the probe never walks - matched
                # on the final name, the only handle there is.
                leaf = resolved.rpartition(".")[2]
                change = next(
                    (c for c in changes if c.qualname.rpartition(".")[2] == leaf),
                    None,
                )
            if change is None:
                continue
            site = f"{rel}:{getattr(node, 'lineno', 0)}"
            if site not in change.used_at:
                change.used_at.append(site)
    return scan
