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


def _params(signature: str) -> int:
    """How many positional parameters a signature takes, `self` excluded."""
    inner = signature.strip()[1:-1] if signature.startswith("(") else signature
    if not inner.strip():
        return 0
    depth, count, current = 0, 0, ""
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            count += 1
            current = ""
            continue
        current += ch
    count += 1 if inner.strip() else 0
    names = [p.strip() for p in re.split(r",(?![^\[\]()]*[\]\)])", inner)]
    names = [n for n in names if n and not n.startswith("*")]
    names = [n for n in names if n.split(":")[0].strip() not in ("self", "cls")]
    return len(names)


def argument_sets(signature: str, cap: int = 12) -> list[str]:
    """Call strings for a signature, varying one parameter at a time around a baseline."""
    n = _params(signature)
    if n == 0:
        return ["()"]
    if n > 3:
        # Beyond three parameters the baseline is unlikely to be valid for any of them,
        # and a call that raises on both sides establishes nothing.
        n = 3
    baseline = [POOL[0]] * n
    out = ["(" + ", ".join(baseline) + ",)"]
    for i in range(n):
        for value in POOL[1:]:
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
) -> tuple[list[Change], int, int, list[str]]:
    """(silent changes, compared, unreachable, stopped_on).

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
        return [], 0, len(payload), stopped_on

    changes: list[Change] = []
    compared = unreachable = 0

    for name, argsets in payload.items():
        a, b = old_res.get(name, {}), new_res.get(name, {})
        if "rows" not in a or "rows" not in b:
            unreachable += 1
            continue
        rows_a, rows_b = a["rows"], b["rows"]
        if len(rows_a) != len(rows_b):
            unreachable += 1
            continue

        exercised = [
            i
            for i, (ra, rb) in enumerate(zip(rows_a, rows_b, strict=True))
            if ra[0] == "ok" or rb[0] == "ok"
        ]
        if not exercised:
            unreachable += 1
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
    return changes, compared, unreachable, stopped_on


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
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            if name and name in wanted:
                for c in wanted[name]:
                    site = f"{rel}:{getattr(node, 'lineno', 0)}"
                    if site not in c.used_at:
                        c.used_at.append(site)
