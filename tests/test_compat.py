"""The signature-compatibility checker: is every call valid under the old shape still valid,
and bound the same way, under the new one?

Each case either is a real upgrade the audit hit, or pins one rule. The notation is the
`shape()` helper: `x` positional-or-keyword, `x=` with a default, `/x` positional-only,
`*!x` keyword-only required, `*x=` keyword-only with a default, `*args`, `**kw`.
"""

from __future__ import annotations

import pytest

from blast_radius.diff import api_changes, compatible, stable_callables, unknown_signatures
from blast_radius.types import Kind


def shape(*params: str) -> str:
    out = []
    for p in params:
        if p.startswith("**"):
            out.append(f"{p[2:]}:VAR_KEYWORD")
        elif p.startswith("*!"):
            out.append(f"{p[2:]}:KEYWORD_ONLY")
        elif p.startswith("*") and p.endswith("="):
            out.append(f"{p[1:-1]}:KEYWORD_ONLY:d")
        elif p.startswith("*"):
            out.append(f"{p[1:]}:VAR_POSITIONAL")
        elif p.startswith("/"):
            name = p[1:].rstrip("=")
            out.append(f"{name}:POSITIONAL_ONLY" + (":d" if p.endswith("=") else ""))
        else:
            name = p.rstrip("=")
            out.append(f"{name}:POSITIONAL_OR_KEYWORD" + (":d" if p.endswith("=") else ""))
    return ",".join(out)


NO_ARGS = shape()  # "" - a function that takes nothing, NOT an unknown signature


@pytest.mark.parametrize(
    "old, new",
    [
        # pydantic 1.10 -> 2.x: BaseModel () -> (**data), BaseConfig () -> (*args, **kwargs).
        # Both were reported as reshaped because "" was read as "unknown".
        (NO_ARGS, shape("**data")),
        (NO_ARGS, shape("*args", "**kwargs")),
        (NO_ARGS, shape("x=")),
        (NO_ARGS, shape("*k=")),
        # attrs 21.4 -> 23.2: evolve(inst, **changes) -> evolve(*args, **changes). inst is
        # still accepted positionally (by *args) and by name (by **changes).
        (shape("inst", "**changes"), shape("*args", "**changes")),
        (shape("/a", "/b"), shape("/x", "/y")),  # positional-only names are not API
        (shape("/a"), shape("*args")),
        (shape("*!a"), shape("a")),  # always passed by name, still accepted by name
        (shape("a", "*!k"), shape("a", "k")),
        (shape("a", "*k="), shape("a", "k=")),
        (shape("*!a"), shape("*!a", "*b=")),
        (shape("*!a"), shape("**kw")),
        (shape("self", "x"), shape("self", "x", "*args", "**kw")),
        (shape("**kw"), shape("a=", "**kw")),
        (shape("*args"), shape("*args", "*k=")),
        (shape("x"), shape("x=")),
        (shape("x", "y="), shape("x", "y=", "z=")),
        # pytest 8 -> 9: raises(expected_exception, *args) -> raises(..., func=None, *args)
        (shape("e", "*args", "**kw"), shape("e=", "func=", "*args", "**kw")),
    ],
)
def test_every_old_call_still_binds(old, new):
    assert compatible(old, new)


@pytest.mark.parametrize(
    "old, new",
    [
        (NO_ARGS, shape("x")),  # a new required parameter
        (NO_ARGS, shape("*!k")),
        (shape("a"), shape("*args")),  # f(a=1) no longer binds
        (shape("inst", "**changes"), shape("*args")),  # neither does evolve(inst=...)
        (shape("a"), shape("*args", "*a=")),  # f(1) leaves `a` at its default
        (shape("a", "b"), shape("b", "a")),
        (shape("a", "b"), shape("a")),
        (shape("a", "b"), shape("a", "c")),
        (shape("a", "*!b"), shape("b", "a")),
        (shape("x="), shape("/x=")),  # became positional-only
        (shape("x", "y"), shape("x", "*y=")),  # became keyword-only
        (shape("*args"), NO_ARGS),
        (shape("*args"), shape("x", "*args")),  # a new REQUIRED slot in front of *args
        (shape("**kw"), NO_ARGS),
        (shape("*!a"), NO_ARGS),
        (shape("*a="), shape("*!a")),  # default taken away
        (shape("x="), shape("x")),
        (shape("x", "*!k"), shape("x", "*!k", "*!j")),
        (shape("/a"), shape("/a", "/b")),
        (shape("a", "*!k"), shape("k", "a")),
    ],
)
def test_a_call_that_could_trip_is_not_compatible(old, new):
    assert not compatible(old, new)


def test_an_unknown_signature_is_never_called_compatible():
    assert not compatible(None, shape("x"))
    assert not compatible(shape("x"), None)
    assert not compatible(None, None)


def _sym(sig: str, sh: str | None, kind: str = "function") -> dict:
    return {"kind": kind, "signature": sig, "shape": sh, "aliases": []}


def test_a_signature_that_became_readable_is_not_reshaped():
    """numpy 2.5 made C functions introspectable: 84 of its 95 "reshaped" functions were
    `(unknown) -> (a, b)`. No caller can observe that."""
    old = {"np.f": _sym("", None), "np.g": _sym("(a)", "a:POSITIONAL_OR_KEYWORD")}
    new = {"np.f": _sym("(a, b)", shape("a", "b")), "np.g": _sym("", None)}
    assert api_changes(old, new) == []
    assert unknown_signatures(old, new) == ["np.f", "np.g"]


def test_a_no_argument_class_that_grew_kwargs_is_widened_and_still_run():
    old = {"p.Model": _sym("()", NO_ARGS, "class"), "p.f": _sym("()", NO_ARGS)}
    new = {
        "p.Model": _sym("(**data)", shape("**data"), "class"),
        "p.f": _sym("(x=1)", shape("x=")),
    }
    kinds = {c.qualname: c.kind for c in api_changes(old, new)}
    assert kinds == {"p.Model": Kind.WIDENED, "p.f": Kind.WIDENED}
    assert stable_callables(old, new) == {"p.f": "()"}


def test_a_function_that_became_a_non_callable_is_reshaped():
    old = {"p.f": _sym("(x)", shape("x"))}
    new = {"p.f": _sym("", None, "module")}
    [change] = api_changes(old, new)
    assert change.kind is Kind.RESHAPED
    assert change.detail == "was a function, is now a module"


def test_the_receiver_is_not_part_of_the_call_shape():
    """bs4 4.13 dropped an encode(self, encoding) override and inherited str.encode's
    (self, /, encoding='utf-8', errors='strict'). Nobody passes `self` by name."""
    assert compatible(shape("self", "encoding"), shape("/self", "encoding=", "errors="))
    assert compatible(shape("cls", "x"), shape("/cls", "x"))


def test_an_old_forwarding_signature_is_not_compared():
    """bs4 4.12's HTMLFormatter read as (*args, **kwargs); its 4.13 constructor is the
    real one, and "no longer accepts anything" is not a break anyone can hit."""
    old = {"p.F": _sym("(*args, **kwargs)", shape("*args", "**kwargs"), "class")}
    new = {"p.F": _sym("(entity=None)", shape("entity="), "class")}
    assert api_changes(old, new) == []
    assert unknown_signatures(old, new) == ["p.F"]
    # ...while a new forwarding signature accepts every old call, and says so.
    [change] = api_changes(new, old)
    assert change.kind is Kind.WIDENED


def test_a_function_replaced_by_a_callable_object_is_not_reshaped():
    """pytest 9's pytest.fail/skip/exit/xfail are callable objects, found at runtime."""
    old = {"p.fail": _sym("(reason='')", shape("reason="))}
    resolved = {
        "p.fail": {
            "kind": "other",
            "callable": True,
            "signature": "(reason='')",
            "shape": shape("reason="),
        },
    }
    assert api_changes(old, {}, resolved) == []


def test_a_removed_path_is_gone_even_when_another_alias_survives():
    """urllib3 1.26 exposed RequestMethods as urllib3.request.RequestMethods (canonical)
    and urllib3.poolmanager.RequestMethods. 2.x kept only the second; the first import
    fails, and matching on any alias hid that."""
    old = {
        "u.request.RM": {
            **_sym("()", NO_ARGS, "class"),
            "aliases": ["u.request.RM", "u.poolmanager.RM"],
        }
    }
    new = {
        "u.poolmanager.RM": {
            **_sym("()", NO_ARGS, "class"),
            "aliases": ["u.poolmanager.RM", "u._methods.RM"],
        }
    }
    [change] = api_changes(old, new)
    assert (change.kind, change.qualname) == (Kind.GONE, "u.request.RM")
