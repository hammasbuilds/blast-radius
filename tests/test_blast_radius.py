"""Tests for the parts that decide. No network, no installs.

Every failure this tool can have produces a *confident wrong answer* rather than an error,
and three of them actually happened while it was being built:

- both probes loaded the same installed copy, so the report said an upgrade changed nothing
- object identity in a repr read as a behaviour change, four times over
- a package's re-exported third-party names read as 455 removals

So these tests are mostly about refusing to claim things.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blast_radius.diff import _params, _strength, api_changes, argument_sets, find_call_sites
from blast_radius.probe import WrongVersionImported, surface
from blast_radius.types import Change, Kind, Report


def sym(kind="function", signature="(x)", doc=""):
    return {"kind": kind, "signature": signature, "doc": doc}


# --- the API diff -------------------------------------------------------------------------


def test_a_removed_name_is_gone():
    old = {"p.f": sym(), "p.g": sym()}
    new = {"p.f": sym()}
    changes = api_changes(old, new)
    assert [(c.kind, c.qualname) for c in changes] == [(Kind.GONE, "p.g")]


def test_a_changed_signature_is_reshaped_not_gone():
    changes = api_changes({"p.f": sym(signature="(x)")}, {"p.f": sym(signature="(x, y)")})
    assert changes[0].kind is Kind.RESHAPED
    assert "(x) -> (x, y)" in changes[0].detail


def test_a_new_name_is_added():
    changes = api_changes({}, {"p.f": sym()})
    assert changes[0].kind is Kind.ADDED


def test_the_meta_key_is_not_a_symbol():
    # The probe reports which file it loaded under __meta__. Diffing it would produce a
    # spurious change on every comparison, since the paths always differ.
    old = {"__meta__": {"kind": "meta", "signature": "", "doc": "", "version": "1"}, "p.f": sym()}
    new = {"__meta__": {"kind": "meta", "signature": "", "doc": "", "version": "2"}, "p.f": sym()}
    assert api_changes(old, new) == []


def test_an_identical_surface_yields_nothing():
    same = {"p.f": sym(), "p.C": sym(kind="class", signature="(a)")}
    assert api_changes(same, same) == []


# --- severity and ordering ----------------------------------------------------------------


def test_a_change_your_code_uses_outranks_one_it_does_not():
    # An upgrade removing forty functions nobody calls is a non-event; the same upgrade
    # touching one you call in a loop is an incident.
    unused_silent = Change(Kind.SILENT, "p.a")
    used_reshaped = Change(Kind.RESHAPED, "p.b", used_at=["x.py:1"])
    assert used_reshaped.severity > unused_silent.severity


def test_silent_outranks_gone_when_neither_is_used():
    # An import error announces itself; a changed answer does not.
    assert Change(Kind.SILENT, "p.a").severity > Change(Kind.GONE, "p.b").severity


def test_report_sorts_by_what_can_reach_you():
    r = Report(
        changes=[
            Change(Kind.ADDED, "p.new"),
            Change(Kind.SILENT, "p.quiet"),
            Change(Kind.GONE, "p.used", used_at=["a.py:3"]),
        ]
    )
    assert [c.qualname for c in r.sorted()] == ["p.used", "p.quiet", "p.new"]
    assert [c.qualname for c in r.reaching_you] == ["p.used"]


# --- witness grading ----------------------------------------------------------------------


def test_two_returned_values_are_the_strongest_evidence():
    assert _strength(["ok", "[1, 2]"], ["ok", "[2, 1]"]) == 0
    assert _strength(["ok", "1"], ["raise", "ValueError: x"]) == 1
    assert _strength(["raise", "TypeError: a"], ["raise", "ValueError: b"]) == 2


def test_a_drained_iterator_that_raised_is_not_a_clean_return():
    # An iterator is drained before comparison, so a generator that yields then raises
    # renders as `ok: [...]...then TypeError`. Reading only the `ok` prefix would promote
    # it to unarguable evidence.
    a = ["ok", "[1]...then TypeError: bad"]
    b = ["ok", "[1]...then ValueError: worse"]
    assert _strength(a, b) == 2


# --- signatures ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signature,expected",
    [
        ("()", 0),
        ("(x)", 1),
        ("(x, y)", 2),
        ("(self, x)", 1),
        ("(x, *args, **kwargs)", 1),
        ("(x: Sequence[int], y: dict[str, int])", 2),
    ],
)
def test_parameter_counting(signature, expected):
    assert _params(signature) == expected


def test_a_zero_argument_function_gets_one_empty_call():
    assert argument_sets("()") == ["()"]


def test_argument_sets_vary_one_parameter_at_a_time():
    import ast

    sets = argument_sets("(a, b)", cap=8)

    def parts(call):
        return [ast.unparse(e) for e in ast.parse(call).body[0].value.elts]

    baseline = parts(sets[0])
    for call in sets[1:]:
        differing = sum(x != y for x, y in zip(parts(call), baseline, strict=True))
        assert differing <= 1


# --- call sites ---------------------------------------------------------------------------


def test_call_sites_are_found_and_recorded(tmp_path):
    (tmp_path / "app.py").write_text(
        "from packaging.version import parse\n\ndef go():\n    return parse('1.0')\n",
        encoding="utf-8",
    )
    changes = [Change(Kind.GONE, "packaging.version.parse")]
    find_call_sites(tmp_path, "packaging", changes)
    assert changes[0].used_at
    assert changes[0].used_at[0].startswith("app.py:")


def test_a_file_that_never_mentions_the_package_is_skipped(tmp_path):
    # Cheap and important: a project of a thousand modules should not be AST-walked for
    # attribute names it cannot possibly be using.
    (tmp_path / "unrelated.py").write_text("def parse(x):\n    return x\n", encoding="utf-8")
    changes = [Change(Kind.GONE, "packaging.version.parse")]
    find_call_sites(tmp_path, "packaging", changes)
    assert changes[0].used_at == []


# --- the version guard --------------------------------------------------------------------


def test_a_missing_target_directory_is_refused_loudly(tmp_path):
    # The bug this pins: Python silently ignores a sys.path entry that does not exist, so
    # the import falls through to whatever the interpreter already has. Both probes then
    # load the SAME copy, agree perfectly, and the report says the upgrade changed nothing.
    with pytest.raises(WrongVersionImported):
        surface(tmp_path / "does-not-exist", "json")


def test_importing_a_stdlib_module_from_a_target_dir_is_refused(tmp_path):
    # `json` resolves from the standard library, not from the directory given - which is
    # exactly the fall-through the guard exists to catch.
    (tmp_path / "placeholder.txt").write_text("", encoding="utf-8")
    with pytest.raises(WrongVersionImported):
        surface(tmp_path, "json")


def test_a_package_inside_the_target_dir_is_accepted(tmp_path):
    pkg = tmp_path / "tinypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        '__version__ = "1.0"\n\n\ndef go(x):\n    return x + 1\n', encoding="utf-8"
    )
    out = surface(tmp_path, "tinypkg")
    assert out is not None
    assert out["__meta__"]["version"] == "1.0"
    assert "tinypkg.go" in out


def test_names_imported_from_elsewhere_are_not_counted_as_the_packages_own(tmp_path):
    # packaging 21.3 does `from pyparsing import ...`, and a naive walk credited packaging
    # with pyparsing's whole API - so dropping pyparsing read as 455 removed "packaging"
    # symbols. They were never packaging's to remove.
    pkg = tmp_path / "borrower"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from json import dumps\n\n\ndef mine(x):\n    return x\n", encoding="utf-8"
    )
    out = surface(tmp_path, "borrower")
    assert out is not None
    assert "borrower.mine" in out
    assert "borrower.dumps" not in out


# --- accounting ---------------------------------------------------------------------------


def test_counts_group_by_kind():
    r = Report(changes=[Change(Kind.GONE, "a"), Change(Kind.GONE, "b"), Change(Kind.SILENT, "c")])
    assert r.counts() == {"gone": 2, "silent": 1}


def test_an_empty_report_has_no_counts():
    assert Report().counts() == {}


def test_a_change_with_no_call_sites_does_not_reach_you():
    assert Report(changes=[Change(Kind.SILENT, "a")]).reaching_you == []


def test_paths_are_reported_relative_to_the_repo(tmp_path):
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    (nested / "m.py").write_text("import packaging\npackaging.parse('1')\n", encoding="utf-8")
    changes = [Change(Kind.GONE, "packaging.parse")]
    find_call_sites(tmp_path, "packaging", changes)
    assert changes[0].used_at
    assert not Path(changes[0].used_at[0].split(":")[0]).is_absolute()
