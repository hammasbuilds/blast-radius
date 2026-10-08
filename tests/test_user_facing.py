"""The parts a user touches directly: arguments, exit codes, what gets printed.

Every test here pins a complaint from an audit that installed the tool and used it
the way a stranger would - a typo in a path, a CI gate, an additive release.
None of them installs anything or reaches the network.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

import blast_radius
from blast_radius import cli
from blast_radius.diff import api_changes, compatible, find_call_sites, stable_callables
from blast_radius.probe import surface
from blast_radius.report import around_difference, summary, write_markdown
from blast_radius.types import Change, Kind, Report


def shape(*params: str) -> str:
    """shape("a", "b=", "*c=") -> the probe's call-shape string.

    `name` positional-or-keyword, `name=` with a default, `/name` positional-only,
    `*name` keyword-only, `*args` / `**kw` the variadics.
    """
    out = []
    for p in params:
        if p.startswith("**"):
            out.append(f"{p[2:]}:VAR_KEYWORD")
        elif p.startswith("*") and p.endswith("="):
            out.append(f"{p[1:-1]}:KEYWORD_ONLY:d")
        elif p.startswith("*") and p[1:].startswith("!"):
            out.append(f"{p[2:]}:KEYWORD_ONLY")
        elif p.startswith("*"):
            out.append(f"{p[1:]}:VAR_POSITIONAL")
        elif p.startswith("/"):
            name = p[1:].rstrip("=")
            out.append(f"{name}:POSITIONAL_ONLY" + (":d" if p.endswith("=") else ""))
        else:
            name = p.rstrip("=")
            out.append(f"{name}:POSITIONAL_OR_KEYWORD" + (":d" if p.endswith("=") else ""))
    return ",".join(out)


# --- additive changes are not breaking ------------------------------------------------------


@pytest.mark.parametrize(
    "old, new",
    [
        # packaging.utils.canonicalize_name, 21.3 -> 24.0
        (shape("name"), shape("name", "*validate=")),
        # packaging.specifiers.SpecifierSet.contains, 21.3 -> 24.0
        (
            shape("self", "item", "prereleases="),
            shape("self", "item", "prereleases=", "installed="),
        ),
        (shape("/x"), shape("x")),  # positional-only may now also be passed by name
        (shape("x"), shape("x=")),  # a required parameter gained a default
        (shape("x"), shape("x", "*args")),
        (shape("x"), shape("x", "**kw")),
        (shape("x", "*!k"), shape("x", "*k=")),
        (shape("x", "*args"), shape("x", "y=", "*args")),  # every old call still binds
    ],
)
def test_a_change_every_existing_call_survives_is_compatible(old, new):
    assert compatible(old, new)


@pytest.mark.parametrize(
    "old, new",
    [
        (shape("x", "y"), shape("x")),  # removed
        (shape("x", "y"), shape("x", "z")),  # renamed
        (shape("x", "y="), shape("x", "new=", "y=")),  # inserted before an existing one
        (shape("x="), shape("x")),  # default taken away
        (shape("x"), shape("x", "*!required")),  # urllib3 2.2.1 -> 2.2.2
        (shape("x", "y"), shape("x", "*y=")),  # became keyword-only
        (shape("x"), shape("/x")),  # became positional-only
        (shape("x", "**kw"), shape("x")),
        (shape("x"), ""),  # unknown must never read as safe
        ("", shape("x")),
    ],
)
def test_a_change_a_caller_can_trip_on_is_not_compatible(old, new):
    assert not compatible(old, new)


def test_an_added_optional_parameter_is_widened_not_reshaped():
    old = {"p.f": {"kind": "function", "signature": "(name)", "shape": shape("name")}}
    new = {
        "p.f": {
            "kind": "function",
            "signature": "(name, *, validate=False)",
            "shape": shape("name", "*validate="),
        }
    }
    [change] = api_changes(old, new)
    assert change.kind is Kind.WIDENED
    assert change.kind not in {Kind.GONE, Kind.RESHAPED, Kind.SILENT}


def test_a_new_required_keyword_is_still_reshaped():
    old = {"p.f": {"kind": "function", "signature": "(*, a)", "shape": shape("*!a")}}
    new = {"p.f": {"kind": "function", "signature": "(*, a, b)", "shape": shape("*!a", "*!b")}}
    [change] = api_changes(old, new)
    assert change.kind is Kind.RESHAPED


def test_a_widened_function_is_still_run_by_the_behaviour_pass():
    old = {"p.f": {"kind": "function", "signature": "(x)", "shape": shape("x")}}
    new = {"p.f": {"kind": "function", "signature": "(x, y=1)", "shape": shape("x", "y=")}}
    assert stable_callables(old, new) == {"p.f": "(x)"}


# --- --used-by -------------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [["--used-by"], ["--used-by="]])
def test_a_used_by_path_that_does_not_exist_is_an_error_not_a_green_run(tmp_path, flag, capsys):
    missing = str(tmp_path / "typo")
    argv = ["check", "packaging", "21.3", "24.0"]
    argv += [flag[0], missing] if flag == ["--used-by"] else [flag[0] + missing]
    assert cli.main(argv) == cli.EXIT_ERROR
    assert "no such file or directory" in capsys.readouterr().out


def test_a_used_by_file_that_is_not_python_is_an_error(tmp_path, capsys):
    notes = tmp_path / "notes.txt"
    notes.write_text("hello", encoding="utf-8")
    assert cli.main(["check", "p", "1", "2", "--used-by", str(notes)]) == cli.EXIT_ERROR
    assert "not a directory or a .py file" in capsys.readouterr().out


def test_a_single_python_file_can_be_matched(tmp_path):
    script = tmp_path / "tool.py"
    script.write_text("from packaging.version import parse\nparse('1')\n", encoding="utf-8")
    changes = [Change(Kind.SILENT, "packaging.version.parse")]
    scan = find_call_sites(script, "packaging", changes)
    assert scan.files == 1
    assert changes[0].used_at == ["tool.py:1", "tool.py:2"]


USES = "from packaging.version import parse\nparse('1')\n"


@pytest.mark.parametrize(
    "where",
    [
        "venv/lib/x.py",
        ".venv/lib/x.py",
        "lib/python3.12/site-packages/x.py",
        "node_modules/pkg/x.py",
        ".tox/py312/x.py",
        "build/lib/x.py",
        "dist/x.py",
        "pkg.egg-info/x.py",
    ],
)
def test_third_party_and_build_directories_are_not_your_code(tmp_path, where):
    target = tmp_path / where
    target.parent.mkdir(parents=True)
    target.write_text(USES, encoding="utf-8")
    changes = [Change(Kind.SILENT, "packaging.version.parse")]
    find_call_sites(tmp_path, "packaging", changes)
    assert changes[0].used_at == [], f"{where} is not the project's own code"


def test_a_virtualenv_is_skipped_whatever_it_is_called(tmp_path):
    venv = tmp_path / "my-odd-env"
    (venv / "Lib").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    (venv / "Lib" / "x.py").write_text(USES, encoding="utf-8")
    changes = [Change(Kind.SILENT, "packaging.version.parse")]
    find_call_sites(tmp_path, "packaging", changes)
    assert changes[0].used_at == []


def test_a_package_called_build_is_still_scanned(tmp_path):
    """pypa/build keeps its source in src/build/. Skipping `build` by name dropped the
    whole project and reported that it used none of the changes."""
    pkg = tmp_path / "src" / "build"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "env.py").write_text(USES, encoding="utf-8")
    changes = [Change(Kind.SILENT, "packaging.version.parse")]
    find_call_sites(tmp_path, "packaging", changes)
    assert changes[0].used_at == ["src/build/env.py:1", "src/build/env.py:2"]


def test_a_file_with_a_coding_cookie_is_read_not_dropped(tmp_path):
    src = "# -*- coding: latin-1 -*-\n# caf\xe9\nfrom packaging.version import parse\nparse('1')\n"
    (tmp_path / "legacy.py").write_bytes(src.encode("latin-1"))
    changes = [Change(Kind.SILENT, "packaging.version.parse")]
    scan = find_call_sites(tmp_path, "packaging", changes)
    assert scan.unreadable == []
    assert "legacy.py:4" in changes[0].used_at


def test_a_file_that_cannot_be_read_is_counted_not_silently_dropped(tmp_path):
    (tmp_path / "broken.py").write_bytes(b"import packaging\nx = '\xff\xfe'\n")
    (tmp_path / "syntax.py").write_text("import packaging\ndef (:\n", encoding="utf-8")
    scan = find_call_sites(tmp_path, "packaging", [Change(Kind.GONE, "packaging.x")])
    assert sorted(scan.unreadable) == ["broken.py", "syntax.py"]


def test_a_method_called_on_an_unknown_object_is_a_possible_site_not_a_proven_one(tmp_path):
    """pypa/build calls `req.specifier.contains(...)`. The receiver's type exists only
    at runtime, so it cannot be proven - and SpecifierSet.contains changed silently."""
    (tmp_path / "util.py").write_text(
        "import packaging.requirements\n\n"
        "def ok(req, v):\n    return req.specifier.contains(v, prereleases=True)\n",
        encoding="utf-8",
    )
    silent = Change(Kind.SILENT, "packaging.specifiers.SpecifierSet.contains")
    widened = Change(Kind.WIDENED, "packaging.specifiers.SpecifierSet.filter")
    find_call_sites(tmp_path, "packaging", [silent, widened])
    assert silent.used_at == []
    assert silent.maybe_at == ["util.py:4"]
    assert widened.maybe_at == [], "a non-breaking change is not worth a maybe"


def test_an_alias_path_is_matched_to_the_reported_symbol(tmp_path):
    (tmp_path / "a.py").write_text(
        "from packaging.specifiers import parse\nparse('1')\n", encoding="utf-8"
    )
    change = Change(Kind.SILENT, "packaging.version.parse", aliases=["packaging.specifiers.parse"])
    find_call_sites(tmp_path, "packaging", [change])
    assert change.used_at == ["a.py:1", "a.py:2"]


# --- CI gates --------------------------------------------------------------------------------


def _report(*changes: Change) -> Report:
    return Report(package="p", old_version="1", new_version="2", changes=list(changes))


@pytest.mark.parametrize(
    "fail_on, changes, trips",
    [
        ({"gone"}, [Change(Kind.GONE, "p.f")], True),
        ({"gone"}, [Change(Kind.RESHAPED, "p.f")], False),
        ({"any"}, [Change(Kind.RESHAPED, "p.f")], True),
        ({"any"}, [Change(Kind.WIDENED, "p.f"), Change(Kind.ADDED, "p.g")], False),
        ({"used"}, [Change(Kind.GONE, "p.f")], False),
        ({"used"}, [Change(Kind.GONE, "p.f", used_at=["a.py:1"])], True),
        ({"used"}, [Change(Kind.WIDENED, "p.f", used_at=["a.py:1"])], False),
        ({"used"}, [Change(Kind.SILENT, "p.f", maybe_at=["a.py:1"])], False),
        (set(), [Change(Kind.GONE, "p.f", used_at=["a.py:1"])], False),
    ],
)
def test_fail_on(fail_on, changes, trips):
    assert bool(cli.gate(_report(*changes), fail_on)) is trips


def test_fail_on_accepts_commas_and_repeats_and_the_old_flag():
    assert cli._parse_fail_on(["gone,reshaped", "used"], True) == {
        "gone",
        "reshaped",
        "used",
        "silent",
    }


def test_an_unknown_fail_on_value_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "p", "1", "2", "--fail-on", "gnoe"])
    assert exc.value.code == 2


def test_fail_on_silent_without_the_behaviour_pass_is_refused(capsys):
    argv = ["check", "p", "1", "2", "--fail-on", "silent", "--no-behaviour"]
    assert cli.main(argv) == cli.EXIT_ERROR
    assert "needs the behaviour pass" in capsys.readouterr().out


# --- what gets printed -----------------------------------------------------------------------


def test_no_behaviour_does_not_claim_nothing_behaved_differently():
    report = _report()
    report.behaviour_checked = False
    text = summary(report)
    assert "nothing behaved differently" not in text
    assert "nothing catches these" not in text
    assert "not checked" in text


def test_a_behaviour_pass_that_ran_still_says_so():
    report = _report()
    report.behaviour_checked = True
    report.compared = 3
    assert "nothing behaved differently" in summary(report)


def test_a_behaviour_pass_that_exercised_nothing_does_not_claim_stability():
    report = _report()
    report.behaviour_checked = True
    text = summary(report)
    assert "nothing behaved differently" not in text
    assert "behaviour is unknown" in text


def test_long_witnesses_are_cut_around_the_difference():
    same = ", ".join(f"<tag{i}>" for i in range(40))
    old, new = around_difference(same + ", <py314-none-any>", same + ", <cp314-none-any>")
    assert old != new
    assert "py314-none-any" in old and "cp314-none-any" in new


def test_short_witnesses_are_left_alone():
    assert around_difference("ok: 1", "ok: 2") == ("ok: 1", "ok: 2")


def test_report_md_names_every_gone_and_reshaped_symbol(tmp_path):
    report = _report(
        Change(Kind.GONE, "p.old_thing"),
        Change(Kind.RESHAPED, "p.f", "(x) -> (x, y)"),
        Change(Kind.WIDENED, "p.g", "(x) -> (x, y=1)"),
    )
    path = tmp_path / "REPORT.md"
    write_markdown(report, path)
    text = path.read_text(encoding="utf-8")
    for name in ("p.old_thing", "p.f", "p.g"):
        assert f"`{name}`" in text
    assert "(x) -> (x, y)" in text


def test_version_flag(capsys):
    from blast_radius import __version__

    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"blast-radius {__version__}"


def test_help_describes_the_tool_and_every_option_has_help():
    text = cli.build_parser().format_help()
    assert "What a dependency upgrade actually changes" in text
    check = cli.build_parser()._subparsers._group_actions[0].choices["check"]  # noqa: SLF001
    for action in check._actions:  # noqa: SLF001
        assert action.help, f"{action.option_strings or action.dest} has no help"
    assert "SECONDS" in check.format_help()


# --- installing ------------------------------------------------------------------------------


def test_a_uv_failure_is_reported_in_full_and_pip_is_not_tried(tmp_path, monkeypatch):
    calls = []
    uv_error = (
        "  x No solution found when resolving dependencies:\n"
        "  -> Because nonexistent-pkg was not found in the package registry\n"
        "      and you require nonexistent-pkg==1.0, we can conclude that your\n"
        "      requirements are unsatisfiable."
    )

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=uv_error)

    monkeypatch.setattr(cli.shutil, "which", lambda name: "uv")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    ok, why = cli.install("nonexistent-pkg", "1.0", tmp_path / "t")
    assert not ok
    assert len(calls) == 1, "pip must not be tried after uv gave a real answer"
    assert "not found in the package registry" in why
    assert "requirements are unsatisfiable" in why
    assert "--python" in calls[0], "wheels must be chosen for the interpreter that imports them"


def test_with_no_uv_and_no_pip_the_message_says_so(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No module named pip")

    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    ok, why = cli.install("x", "1", tmp_path / "t")
    assert not ok
    assert "neither uv nor pip" in why


# --- the name you install is not always the name you import ---------------------------------


def test_the_import_name_is_read_from_top_level_txt(tmp_path):
    info = tmp_path / "beautifulsoup4-4.12.3.dist-info"
    info.mkdir()
    (info / "top_level.txt").write_text("bs4\n", encoding="utf-8")
    assert cli.pick_import_name(tmp_path, "beautifulsoup4") == ("bs4", ["bs4"])


def test_the_import_name_is_read_from_record_when_there_is_no_top_level(tmp_path):
    info = tmp_path / "PyYAML-6.0.2.dist-info"
    info.mkdir()
    (info / "RECORD").write_text(
        "yaml/__init__.py,sha256=x,1\n_yaml/__init__.py,sha256=x,1\n"
        "PyYAML-6.0.2.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    assert cli.pick_import_name(tmp_path, "pyyaml") == ("yaml", ["yaml"])


def test_a_dash_in_the_name_becomes_an_underscore(tmp_path):
    info = tmp_path / "charset_normalizer-3.3.2.dist-info"
    info.mkdir()
    (info / "top_level.txt").write_text("charset_normalizer\n", encoding="utf-8")
    assert cli.pick_import_name(tmp_path, "charset-normalizer")[0] == "charset_normalizer"


def test_several_top_level_packages_are_all_compared(tmp_path):
    """attrs installs `attr` and `attrs`; refusing to pick hid both."""
    info = tmp_path / "combo-1.0.dist-info"
    info.mkdir()
    (info / "top_level.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    assert cli.pick_import_name(tmp_path, "combo") == ("alpha", ["alpha", "beta"])


# --- naming ----------------------------------------------------------------------------------


def test_an_object_is_named_where_it_is_defined_when_paths_are_equally_deep(tmp_path):
    """packaging.utils does `from .version import Version`. Both paths have two dots,
    and 'shortest string wins' reported the class as packaging.utils.Version."""
    pkg = tmp_path / "pk"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "version.py").write_text("class Version:\n    pass\n", encoding="utf-8")
    (pkg / "ut.py").write_text("from .version import Version\n", encoding="utf-8")
    out = surface(tmp_path, "pk")
    assert out is not None
    assert "pk.version.Version" in out
    assert "pk.ut.Version" not in out
    assert "pk.ut.Version" in out["pk.version.Version"]["aliases"]


def test_a_reshaped_signature_is_shown_around_what_changed():
    """urllib3 2.2.2 inserted `version_string` 170 characters into a signature; a cut
    at a fixed width printed two identical prefixes and not the change."""
    head = "(*, headers: 'typing.Mapping[str, str] | None' = None, status: 'int', " * 3
    before = head + "version: 'int', reason: 'str | None')"
    after = head + "version: 'int', version_string: 'str', reason: 'str | None')"
    change = Change(Kind.RESHAPED, "u.R", f"{before} -> {after}", before=before, after=after)
    text = summary(_report(change))
    assert "version_string" in text


def test_uv_box_drawing_is_made_ascii():
    raw = "  × No solution found\n  ╰─▶ Because x"
    assert cli._plain(raw) == "  x No solution found\n  -> Because x"


# --- what counts as a silent change -----------------------------------------------------------


def _twin(root, name, old_body, new_body):
    for sub, body in (("old", old_body), ("new", new_body)):
        pkg = root / sub / name
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(body, encoding="utf-8")


def test_a_difference_only_on_a_wrong_typed_argument_is_not_silent(tmp_path):
    """click 7.1.2 ignored `ctx` in Argument.get_default and 8.1.7 uses it, so passing
    the string "" went from None to AttributeError. No caller passes a string there."""
    from blast_radius.diff import behaviour_changes

    _twin(
        tmp_path,
        "tw",
        "def uses_ctx(ctx):\n    return None\n\n\ndef real(x):\n    return 1\n",
        "def uses_ctx(ctx):\n    return ctx.lookup_default\n\n\ndef real(x):\n    return 2\n",
    )
    stable = {"tw.uses_ctx": "(ctx)", "tw.real": "(x)"}
    silent, _c, _u, _s, _r, weak, _nd = behaviour_changes(
        tmp_path / "old", tmp_path / "new", stable, timeout=30
    )
    assert [c.qualname for c in silent] == ["tw.real"]
    assert [c.qualname for c in weak] == ["tw.uses_ctx"]


def test_a_closure_built_by_a_different_helper_is_not_a_behaviour_change(tmp_path):
    """click 8 routes help_option through option(), so the returned decorator reprs as
    option.<locals>.decorator. That is a refactor, not a behaviour."""
    from blast_radius.diff import behaviour_changes

    old = "def help_option():\n    def decorator(f):\n        return f\n    return decorator\n"
    new = (
        "def option():\n    def decorator(f):\n        return f\n    return decorator\n\n\n"
        "def help_option():\n    return option()\n"
    )
    _twin(tmp_path, "cl", old, new)
    silent, compared, *_ = behaviour_changes(
        tmp_path / "old", tmp_path / "new", {"cl.help_option": "()"}, timeout=30
    )
    assert compared == 1
    assert silent == []


def test_weak_differences_are_listed_apart_and_not_counted():
    report = _report()
    report.behaviour_checked = True
    report.weak = [Change(Kind.SILENT, "p.f", witness={"args": "('',)", "old": "ok", "new": "x"})]
    text = summary(report)
    assert "Not counted: 1 function(s)" in text
    assert report.counts() == {}


def test_the_version_is_the_same_in_every_place_it_is_declared() -> None:
    """__version__, pyproject.toml and the installed metadata must agree.

    The version is written twice - here and in pyproject.toml - and the other tests
    only check that `--version` prints `__version__`, which is true however wrong
    both are. Bump pyproject alone and the wheel says blast-radius {new} while
    `blast-radius --version` says the old one; release.yml compares the tag to
    pyproject, so nothing would have caught it.
    """
    from importlib.metadata import version

    assert version("blast-radius") == blast_radius.__version__

    # pyproject.toml is absent wherever only tests/ is shipped, as in the
    # installed-wheel CI job.
    pyproject = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.exists():
        import tomllib

        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        assert declared["project"]["version"] == blast_radius.__version__
