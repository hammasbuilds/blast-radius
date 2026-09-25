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
import re

from blast_radius.probe import call
from blast_radius.types import Change, Kind

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


def api_changes(old: dict, new: dict) -> list[Change]:
    """GONE, RESHAPED and ADDED - everything readable without running anything."""
    old_syms = {k: v for k, v in old.items() if k != "__meta__"}
    new_syms = {k: v for k, v in new.items() if k != "__meta__"}
    out: list[Change] = []

    for name, info in sorted(old_syms.items()):
        if name not in new_syms:
            out.append(Change(Kind.GONE, name, f"a {info['kind']} that no longer exists"))
        elif info.get("shape", info["signature"]) != new_syms[name].get(
            "shape", new_syms[name]["signature"]
        ):
            out.append(
                Change(
                    Kind.RESHAPED,
                    name,
                    f"{info['signature'] or '(unknown)'} -> "
                    f"{new_syms[name]['signature'] or '(unknown)'}",
                )
            )
    for name in sorted(set(new_syms) - set(old_syms)):
        out.append(Change(Kind.ADDED, name, f"new {new_syms[name]['kind']}"))
    return out


def stable_callables(old: dict, new: dict, limit: int | None = None) -> dict[str, str]:
    """Functions present in both versions with an identical signature.

    These are the only ones worth executing. Anything whose shape changed has already been
    reported, and a behaviour difference there would be explained by the shape.
    """
    out: dict[str, str] = {}
    for name, info in sorted(old.items()):
        if name == "__meta__" or info["kind"] != "function":
            continue
        other = new.get(name)
        # Same CALL SHAPE, not same signature string. A function that gained type hints
        # and nothing else is still callable exactly as before, and is precisely the kind
        # whose behaviour is worth comparing.
        if not other or other.get("shape", other["signature"]) != info.get(
            "shape", info["signature"]
        ):
            continue
        out[name] = info["signature"]
        if limit and len(out) >= limit:
            break
    return out


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


def behaviour_changes(
    old_dir, new_dir, stable: dict[str, str], timeout: float = 600.0
) -> tuple[list[Change], int, int, list[str], dict[str, int]]:
    """(silent changes, compared, unreachable, stopped_on, reasons).

    `compared` counts only functions that actually ran somewhere. A function that raised on
    every input in both versions was never exercised, so it is neither evidence of a change
    nor evidence of stability, and it is counted apart.

    `stopped_on` names the functions the probe's interpreter died on and had to be
    restarted past. They matter because they are not the same finding as the rest of
    `unreachable`: a function nothing can call is a fact about the package, while a probe
    that died is a fact about this tool, and both otherwise arrive as "could not be called".
    """
    payload = {name: argument_sets(sig) for name, sig in stable.items()}
    old_res = call(old_dir, payload, timeout)
    new_res = call(new_dir, payload, timeout)
    stopped_on: list[str] = []
    for res in (old_res, new_res):
        if res:
            for name in res.pop("__stopped_on__", []):
                if name not in stopped_on:
                    stopped_on.append(name)
    if old_res is None or new_res is None:
        return [], 0, len(payload), stopped_on, {"the probe produced nothing": len(payload)}

    changes: list[Change] = []
    compared = unreachable = 0
    # Why each unreachable name was unreachable. "Could not be called" covers two
    # opposite situations and the report used to give one number for both: a
    # function nothing can construct an argument for is a fact about THIS TOOL,
    # and a function that rejects everything it is handed is a fact about the
    # PACKAGE. Measured on click, the bucket was 71 names and there was no way to
    # tell from the output which kind they were.
    reasons: dict[str, int] = {}

    def unreached(why: str) -> None:
        nonlocal unreachable
        unreachable += 1
        reasons[why] = reasons.get(why, 0) + 1

    for name, argsets in payload.items():
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
            # Every attempt raised. TypeError on every one means the arguments were
            # the wrong shape and this tool never actually reached the function -
            # fixable here, by generating better arguments. Anything else means the
            # function ran and refused, which is the package's own behaviour and
            # nothing to fix.
            every = [r[1] for r in rows_a + rows_b]
            if every and all(t.startswith("TypeError") for t in every):
                unreached("never validly called - every argument set was the wrong type")
            else:
                unreached("called, and rejected every input")
            continue
        compared += 1

        diffs = [i for i in exercised if _row_key(rows_a[i]) != _row_key(rows_b[i])]
        if not diffs:
            continue

        # Lead with a disagreement where both sides returned a value: two differing
        # results are unarguable, where two differing exception types on an argument
        # neither version wanted mostly says the argument was wrong.
        best = min(diffs, key=lambda i: _strength(rows_a[i], rows_b[i]))
        changes.append(
            Change(
                Kind.SILENT,
                name,
                f"same signature {stable[name]}; {len(diffs)} of {len(exercised)} "
                "exercised inputs disagree",
                witness={
                    "args": argsets[best],
                    "old": f"{rows_a[best][0]}: {rows_a[best][1]}",
                    "new": f"{rows_b[best][0]}: {rows_b[best][1]}",
                },
            )
        )
    return changes, compared, unreachable, stopped_on, reasons


def _bindings(tree: ast.Module, package: str) -> tuple[dict[str, str], set[str]]:
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
    root = package.split(".")[0]
    out: dict[str, str] = {}
    starred: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == root or alias.name.startswith(root + "."):
                    out[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue  # a relative import cannot reach a third-party package
            if node.module != root and not node.module.startswith(root + "."):
                continue
            for alias in node.names:
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
    """"a.b.c" for an attribute chain rooted in a plain name, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def find_call_sites(repo, package: str, changes: list[Change]) -> None:
    """Fill in `used_at` for every change the target project actually references.

    Matching is on the trailing attribute name, which over-reports: a project with its own
    `parse` is credited with using `packaging.version.parse`. Over-reporting is the right
    direction here - a missed call site is a break that reaches production, a spurious one
    costs somebody ten seconds.
    """
    from pathlib import Path

    repo = Path(repo)
    wanted: dict[str, list[Change]] = {}
    for c in changes:
        wanted.setdefault(c.qualname.rpartition(".")[2], []).append(c)

    for p in sorted(repo.rglob("*.py")):
        if any(part in {".git", ".venv", "__pycache__", "node_modules"} for part in p.parts):
            continue
        try:
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        if package.split(".")[0] not in src:
            continue  # the module never mentions the package at all
        rel = str(p.relative_to(repo)).replace("\\", "/")
        bound, starred = _bindings(tree, package)
        if not bound:
            # It mentions the package in a string or a comment but imports nothing
            # from it. Nothing in this file can be a call site.
            continue
        by_qualname = {c.qualname: c for c in changes}

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

        for node in ast.walk(tree):
            if not isinstance(node, ast.Name | ast.Attribute):
                continue
            path = _dotted(node)
            if path is None:
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
                    continue
            change = by_qualname.get(resolved)
            if change is None:
                # Also accept a change whose reported name is a shorter public path
                # for the same object: the probe reports `coverage.CoverageData` while
                # a caller may write `coverage.sqldata.CoverageData`.
                change = next(
                    (c for c in changes if c.qualname.rpartition(".")[2] == resolved.rpartition(".")[2]
                     and resolved.startswith(package.split(".")[0])),
                    None,
                )
            if change is None:
                continue
            site = f"{rel}:{getattr(node, 'lineno', 0)}"
            if site not in change.used_at:
                change.used_at.append(site)
