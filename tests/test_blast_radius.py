"""Tests for the parts that decide. No network, no installs.

Every failure this tool can have produces a *confident wrong answer* rather than an error,
and three of them actually happened while it was being built:

- both probes loaded the same installed copy, so the report said an upgrade changed nothing
- object identity in a repr read as a behaviour change, four times over
- a package's re-exported third-party names read as 455 removals

So these tests are mostly about refusing to claim things.
"""

from __future__ import annotations

import json
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


def _write(root, name, files):
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    for filename, body in files.items():
        path = pkg / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return pkg


def test_one_object_imported_into_several_modules_is_counted_once(tmp_path):
    """The bug that made every count meaningless.

    `walk()` visits every module, so a class imported into five of them was recorded
    five times under five paths. coverage.CoverageData is imported into collector,
    control, data, html and sqldata, so ONE signature change to `update` was reported
    as six reshaped symbols. Counting the same object once per importer is not a
    measurement of anything.
    """
    _write(
        tmp_path,
        "aliased",
        {
            "__init__.py": "from .core import Thing\n",
            "core.py": "class Thing:\n    def run(self, a):\n        return a\n",
            "other.py": "from .core import Thing\n",
            "third.py": "from .core import Thing\n",
        },
    )
    out = surface(tmp_path, "aliased")
    assert out is not None

    things = [k for k in out if k.endswith(".Thing")]
    assert len(things) == 1, f"one class recorded {len(things)} times: {things}"

    runs = [k for k in out if k.endswith(".Thing.run")]
    assert len(runs) == 1, f"one method recorded {len(runs)} times: {runs}"


def test_a_symbol_is_reported_under_its_shortest_public_path(tmp_path):
    """`aliased.Thing` is what a user writes, not `aliased.core.Thing`."""
    _write(
        tmp_path,
        "shortest",
        {
            "__init__.py": "from .deep.inner import Thing\n",
            "deep/__init__.py": "",
            "deep/inner.py": "class Thing:\n    pass\n",
        },
    )
    out = surface(tmp_path, "shortest")
    assert out is not None
    assert "shortest.Thing" in out
    assert out["shortest.Thing"]["name"] == "shortest.Thing"


def test_moving_a_class_between_internal_modules_is_not_an_api_change(tmp_path):
    """The other half of the same bug.

    coverage 7.5.0 -> 7.5.4 moved PathAliases out of coverage.sqldata. Keyed on where
    a symbol is defined, that reads as four removals. Keyed on the name people import,
    it is nothing - because `from coverage import PathAliases` still works, and no
    caller can tell.
    """
    before = _write(
        tmp_path / "before",
        "mover",
        {
            "__init__.py": "from .old_home import Thing\n",
            "old_home.py": "class Thing:\n    def run(self, a):\n        return a\n",
        },
    )
    after = _write(
        tmp_path / "after",
        "mover",
        {
            "__init__.py": "from .new_home import Thing\n",
            "new_home.py": "class Thing:\n    def run(self, a):\n        return a\n",
        },
    )
    assert before.exists() and after.exists()

    old = surface(tmp_path / "before", "mover")
    new = surface(tmp_path / "after", "mover")
    assert old is not None and new is not None
    assert api_changes(old, new) == []


def test_all_marks_a_symbol_as_exported(tmp_path):
    """Internal churn and published breakage must be countable separately.

    Without this, `coverage.parser.join_regex` - an internal helper in an internal
    module - weighed exactly as much as `coverage.CoverageData.update`, so a release
    that tidied its internals looked like one that broke its users.
    """
    _write(
        tmp_path,
        "declared",
        {
            "__init__.py": "from .pub import Shown\n",
            "pub.py": (
                '__all__ = ["Shown"]\n\n\nclass Shown:\n    pass\n\n\nclass Hidden:\n    pass\n'
            ),
            "internal.py": "class Internal:\n    pass\n",
        },
    )
    out = surface(tmp_path, "declared")
    assert out is not None

    # In the package root namespace, so published whatever else is true.
    assert out["declared.Shown"]["exported"] is True
    # Not in pub.__all__, so never collected at all.
    assert not [k for k in out if k.endswith(".Hidden")]
    # Reachable, but in a module that declares no public surface.
    assert out["declared.internal.Internal"]["exported"] is False


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


# --- executing somebody else's code, safely -------------------------------------------
#
# The behaviour pass calls arbitrary functions from an installed package. Three
# things those functions do are not exceptions, and each one silently destroyed
# a whole run before it was handled.


def _pkg(root, name, body):
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(body, encoding="utf-8")
    return pkg


def test_a_function_that_exits_does_not_kill_the_run(tmp_path):
    """SystemExit inherits from BaseException, not Exception.

    `except Exception` does not catch it, so one function calling sys.exit()
    ended the probe mid-run - no output, exit code 0, and the caller saw an
    empty result indistinguishable from "this package has no stable functions".

    This is why the published click comparison reported 0 functions exercised
    and 400 unreachable. They were reachable. The first one to exit took the
    process with it.
    """
    from blast_radius.probe import call

    _pkg(
        tmp_path,
        "exiter",
        "import sys\n\n\ndef quits(x):\n    sys.exit(3)\n\n\ndef fine(x):\n    return x + 1\n",
    )
    out = call(tmp_path, {"exiter.quits": ["(1,)"], "exiter.fine": ["(1,)"]}, timeout=120)

    assert out is not None, "the probe died instead of recording the exit"
    assert out["exiter.fine"]["rows"][0] == ["ok", "2"], "work after the exit was lost"
    assert out["exiter.quits"]["rows"][0][0] == "exit"


def test_a_function_that_prints_does_not_corrupt_the_results(tmp_path):
    """Results cross the process boundary as one __BR_JSON__ line on stdout.

    Anything the called function prints lands in the same stream. click's entry
    points print their usage text, which is exactly what happened.
    """
    from blast_radius.probe import call

    _pkg(tmp_path, "noisy", "def chatty(x):\n    print('usage: something')\n    return x * 2\n")
    out = call(tmp_path, {"noisy.chatty": ["(21,)"]}, timeout=120)

    assert out is not None
    assert out["noisy.chatty"]["rows"][0] == ["ok", "42"]


def test_a_large_payload_survives_the_command_line_limit(tmp_path):
    """The payload used to be an argv entry.

    Measured on this machine, passing the payload as an argv entry:

        20 functions,  2,105 chars  ->  20 results
        60 functions,  6,123 chars  ->  60 results
       120 functions, 12,290 chars  ->   0 results, exit code 0
       400 functions                ->  WinError 206, filename too long

    The silent case is the dangerous one: the behaviour pass - the only part of
    this tool that finds what nothing else warns you about - reported no silent
    changes rather than reporting that it had not run. The default limit is 400
    functions, so this was the default behaviour on Windows for any package big
    enough to be worth checking.
    """
    from blast_radius.probe import call

    body = "".join(f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(300))
    _pkg(tmp_path, "wide", body)
    payload = {f"wide.f{i}": ["(1,)", "(2,)", "(3,)", "(4,)"] for i in range(300)}
    # Comfortably past 12,290, the size at which the old argv path went silent.
    assert len(json.dumps(payload)) > 13_000, "fixture is too small to exercise the limit"

    out = call(tmp_path, payload, timeout=300)
    assert out is not None
    assert len(out) == 300
    assert out["wide.f7"]["rows"][0] == ["ok", "8"]


def test_a_method_needing_an_instance_is_reported_not_faked(tmp_path):
    """`getattr(SomeClass, "method")` is the unbound function, which still wants
    `self` - while the signature is measured with `self` excluded, because a
    caller writing `obj.method(x)` passes one argument.

    Those two together meant the generated literal was bound to `self`: the tool
    called `Argument.add_to_parser("a", "b")` with the string "a" as the
    Argument. Most such calls raise and are merely noisy, but a method that
    never touches `self` runs happily against a string, and the two versions are
    then compared on a call no user could make.
    """
    from blast_radius.probe import call

    _pkg(
        tmp_path,
        "needy",
        "class Thing:\n"
        "    def __init__(self, required):\n"
        "        self.required = required\n\n"
        "    def scaled(self, n):\n"
        "        return self.required * n\n",
    )
    out = call(tmp_path, {"needy.Thing.scaled": ["(2,)"]}, timeout=120)

    assert out is not None
    assert "needs_instance" in out["needy.Thing.scaled"]
    assert "rows" not in out["needy.Thing.scaled"]


def test_a_method_on_a_constructible_class_is_bound_and_called(tmp_path):
    """When the class takes no arguments there IS a real instance to use, and
    the method is worth comparing like any other function."""
    from blast_radius.probe import call

    _pkg(
        tmp_path,
        "easy",
        "class Thing:\n"
        "    def __init__(self):\n"
        "        self.base = 10\n\n"
        "    def plus(self, n):\n"
        "        return self.base + n\n",
    )
    out = call(tmp_path, {"easy.Thing.plus": ["(5,)"]}, timeout=120)

    assert out is not None
    assert out["easy.Thing.plus"]["rows"][0] == ["ok", "15"]
