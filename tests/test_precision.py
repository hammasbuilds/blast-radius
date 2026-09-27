"""Regression tests for the second audit: real upgrades where the report was wrong.

Every test runs the whole CLI on small fake packages shaped like the real case (see
conftest.fake_index), so nothing here needs the network. The real package each one is
modelled on is named in the test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from blast_radius import cli
from blast_radius.diff import behaviour_changes


def run(tmp_path, *extra: str, package: str = "fakepkg", old="1.0.0", new="2.0.0") -> tuple:
    out = tmp_path / "out"
    code = cli.main(["check", package, old, new, "--out", str(out), *extra])
    data = json.loads((out / "blast-radius.json").read_text(encoding="utf-8"))
    return code, data


def names(data: dict, kind: str) -> list[str]:
    return [c["qualname"] for c in data["changes"] if c["kind"] == kind]


# --- the public surface -------------------------------------------------------------------


def test_names_defined_in_a_private_sibling_package_are_the_packages_own(tmp_path, fake_index):
    """pytest.fixture is defined in _pytest.fixtures; attrs.define in attr._next_gen.
    Checking only the import name's prefix saw 13 symbols in pytest and 0 in attrs, and
    reported attrs 21.4.0 -> 23.2.0 as "Nothing changed", exit 0."""
    impl = {
        "1.0.0": "def fixture(func):\n    return func\n",
        "2.0.0": "def fixture(func, scope):\n    return func\n",
    }
    fake_index(
        {
            v: {
                "_fakepkg/__init__.py": "",
                "_fakepkg/fixtures.py": body,
                "fakepkg/__init__.py": "from _fakepkg.fixtures import fixture\n",
            }
            for v, body in impl.items()
        }
    )
    code, data = run(tmp_path, "--no-behaviour")
    assert code == 0
    assert names(data, "reshaped") == ["fakepkg.fixture"]


def test_every_public_top_level_module_is_compared(tmp_path, fake_index, capsys):
    """attrs installs `attr` and `attrs`. Most code imports `attr`."""
    files = {
        "1.0.0": {"attr/__init__.py": "def s(x):\n    return x\n", "attrs/__init__.py": ""},
        "2.0.0": {"attr/__init__.py": "", "attrs/__init__.py": ""},
    }
    fake_index(files, dist="fakepkg")
    # 2.0.0 has nothing public at all, which must be an error, not "everything is gone".
    code = cli.main(["check", "fakepkg", "1.0.0", "2.0.0", "--no-behaviour"])
    out = capsys.readouterr().out
    assert "imported as `attr`, `attrs`" in out
    assert code == 2
    assert "found no public functions or classes" in out


def test_an_empty_surface_is_an_error_not_a_clean_bill_of_health(tmp_path, fake_index, capsys):
    fake_index({v: {"fakepkg/__init__.py": "VERSION = 1\n"} for v in ("1.0.0", "2.0.0")})
    code = cli.main(["check", "fakepkg", "1.0.0", "2.0.0"])
    assert code == 2
    assert "found no public functions or classes" in capsys.readouterr().out


def test_a_namespace_package_is_imported_by_its_dotted_name(tmp_path, fake_index, capsys):
    """jaraco.functools installs into jaraco/ with no __init__.py. The import name was
    guessed as `jaraco`, and the probe crashed with IndexError, exit 1."""
    body = {"1.0.0": "def pipe(x):\n    return x\n", "2.0.0": "def pipe(x, y):\n    return x\n"}
    fake_index(
        {v: {"nsfake/tools/__init__.py": b} for v, b in body.items()},
        dist="nsfake.tools",
    )
    code, data = run(tmp_path, "--no-behaviour", package="nsfake.tools")
    assert code == 0
    assert data["modules"] == ["nsfake.tools"]
    assert names(data, "reshaped") == ["nsfake.tools.pipe"]


def test_a_test_suite_shipped_inside_the_package_is_not_api(tmp_path, fake_index):
    """184 of numpy 2.2 -> 2.5's 218 "gone" names were numpy.*.tests.* modules."""
    fake_index(
        {
            "1.0.0": {
                "fakepkg/__init__.py": "def f(x):\n    return x\n",
                "fakepkg/core/__init__.py": "",
                "fakepkg/core/tests/__init__.py": "",
                "fakepkg/core/tests/test_f.py": "def test_f():\n    pass\n",
                "fakepkg/conftest.py": "def pytest_configure(config):\n    pass\n",
            },
            "2.0.0": {"fakepkg/__init__.py": "def f(x):\n    return x\n"},
        }
    )
    code, data = run(tmp_path, "--no-behaviour")
    assert names(data, "gone") == []


# --- "gone" must mean the import fails ------------------------------------------------------

KEPT = "def kept(x):\n    return x\n"


def test_a_name_served_by_a_module_getattr_is_not_gone(tmp_path, fake_index):
    """pydantic 2 serves pydantic.json.pydantic_encoder through a module __getattr__. It
    was reported gone, matched as used, and tripped --fail-on used."""
    shim = textwrap.dedent(
        """
        def __getattr__(name):
            if name == "encoder":
                from fakepkg._deprecated import encoder
                return encoder
            raise AttributeError(name)
        """
    )
    fake_index(
        {
            "1.0.0": {
                "fakepkg/__init__.py": KEPT,
                "fakepkg/json.py": "def encoder(obj):\n    return str(obj)\n\n\n"
                "def removed(obj):\n    return obj\n",
            },
            "2.0.0": {
                "fakepkg/__init__.py": KEPT,
                "fakepkg/json.py": shim,
                "fakepkg/_deprecated.py": "def encoder(obj):\n    return str(obj)\n",
            },
        }
    )
    code, data = run(tmp_path, "--no-behaviour")
    assert names(data, "gone") == ["fakepkg.json.removed"]
    assert data["still_resolve"] == 1


def test_a_method_moved_to_a_base_class_is_not_gone(tmp_path, fake_index):
    """bs4 4.13 moved BeautifulSoup.append and friends onto Tag: 89 of 91 "gone" names
    between 4.12.3 and 4.15.0 still resolved."""
    fake_index(
        {
            "1.0.0": {
                "fakepkg/__init__.py": "class Soup:\n"
                "    def append(self, tag):\n        return tag\n"
            },
            "2.0.0": {
                "fakepkg/__init__.py": "class Tag:\n"
                "    def append(self, tag):\n        return tag\n"
                "\n\nclass Soup(Tag):\n    pass\n"
            },
        }
    )
    code, data = run(tmp_path, "--no-behaviour")
    assert names(data, "gone") == []
    assert names(data, "reshaped") == []


# --- silent changes must be real ------------------------------------------------------------

NOISY = textwrap.dedent(
    """
    import os, random, time

    __version__ = "{version}"
    _counter = {counter}


    def where(x):
        return os.path.dirname(__file__)


    def agent(x):
        return "fakepkg/" + __version__


    def lorem(x):
        return " ".join(random.choice(["a", "b", "c", "d"]) for _ in range(8))


    def started(x):
        return time.time()


    def ib(x):
        global _counter
        _counter += 1
        return "_CountingAttr(counter=%d)" % _counter


    class Template:
        pass


    def from_string(x):
        t = Template()
        return "<Template memory:%x>" % id(t)


    def real(x):
        return {answer}
    """
)


def test_noise_is_not_reported_as_a_silent_change(tmp_path, fake_index):
    """Of about 30 SILENT findings on real upgrades, about 23 were noise: install paths
    (certifi.where), version strings (requests' default_user_agent), randomness (jinja2's
    generate_lorem_ipsum), clocks (urllib3's Timeout.start_connect), a process-global
    counter (attr.ib) and bare hex ids (jinja2's Template reprs). Only `real` changed."""
    fake_index(
        {
            "1.0.0": {"fakepkg/__init__.py": NOISY.format(version="1.0.0", counter=21, answer=1)},
            "2.0.0": {"fakepkg/__init__.py": NOISY.format(version="2.0.0", counter=26, answer=2)},
        }
    )
    code, data = run(tmp_path, "--fail-on", "silent")
    assert names(data, "silent") == ["fakepkg.real"]
    assert code == 1, "the gate trips on the one real change"
    assert {"fakepkg.lorem", "fakepkg.started", "fakepkg.ib"} <= set(data["nondeterministic"])


# --- a partial pass has to say so -----------------------------------------------------------


def test_a_limited_behaviour_pass_says_it_was_partial(tmp_path, fake_index, capsys):
    """--limit 400 used to truncate silently: jinja2 has 494 functions."""
    body = "".join(f"def f{i}(x):\n    return x\n\n\n" for i in range(5))
    fake_index({v: {"fakepkg/__init__.py": body} for v in ("1.0.0", "2.0.0")})
    code, data = run(tmp_path, "--limit", "2")
    out = capsys.readouterr().out
    assert "checking 2 of 5; 3 were not run" in out
    assert "PARTIAL: --limit checked 2 of 5 functions" in out
    assert data["partial"] is True and data["not_run_limit"] == 3


def test_there_is_no_limit_by_default(tmp_path, fake_index):
    body = "".join(f"def f{i}(x):\n    return x\n\n\n" for i in range(5))
    fake_index({v: {"fakepkg/__init__.py": body} for v in ("1.0.0", "2.0.0")})
    _code, data = run(tmp_path)
    assert data["partial"] is False
    assert data["compared"] == 5


def test_progress_is_reported_with_counts(tmp_path):
    for sub in ("old", "new"):
        pkg = tmp_path / sub / "prog"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    seen: list[tuple] = []
    behaviour_changes(
        tmp_path / "old",
        tmp_path / "new",
        {"prog.f": "(x)"},
        progress=lambda label, done, total: seen.append((label, done, total)),
    )
    assert ("old", 1, 1) in seen and ("new", 1, 1) in seen


def test_the_progress_printer_writes_counts_and_an_eta_to_stderr(capsys):
    show = cli._Progress({"old": "1.0", "new": "2.0"}, every=0)
    show("old", 5, 10)
    show("old", 10, 10)
    err = capsys.readouterr().err
    assert "[old 1.0] 5/10 functions" in err and "left" in err
    assert "[old 1.0] 10/10 functions in" in err


# --- failures are exit 2, with a sentence, never a traceback --------------------------------


@pytest.mark.parametrize("version", [">=21", "latest", "1.0,<2", "../x"])
def test_a_version_that_is_not_an_exact_version_is_a_usage_error(version, capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["check", "fakepkg", version, "2.0"])
    assert info.value.code == 2
    assert "is not an exact version" in capsys.readouterr().err


def test_an_unexpected_error_exits_2_with_one_line_not_1(tmp_path, fake_index, monkeypatch, capsys):
    """Exit 1 means "the gate tripped"; a crash must never look like that."""
    fake_index({v: {"fakepkg/__init__.py": "def f(x):\n    return x\n"} for v in ("1.0", "2.0")})

    def boom(*a, **k):
        raise IndexError("list index out of range")

    monkeypatch.setattr(cli, "import_plan", boom)
    assert cli.main(["check", "fakepkg", "1.0", "2.0"]) == 2
    err = capsys.readouterr().err
    assert "error: IndexError: list index out of range" in err
    assert "Traceback" not in err


RUNNER = """
import sys
from pathlib import Path
sys.path.insert(0, {tests!r})
from conftest import write_version
from blast_radius import cli

def fake_install(package, version, into, timeout=600.0):
    body = "".join("def f%d(x):\\n    return x\\n\\n\\n" % i for i in range(200))
    write_version(Path(into), "fakepkg", version, {{"fakepkg/__init__.py": body}})
    return True, ""

cli.install = fake_install
sys.exit(cli.main(["check", "fakepkg", "1.0", "2.0", "--no-behaviour"]))
"""


def test_a_closed_pipe_ends_the_output_not_the_run(tmp_path):
    """`blast-radius check ... | head` raised OSError [Errno 22] with a traceback and
    exited 120 on Windows (BrokenPipeError on POSIX)."""
    script = tmp_path / "runner.py"
    script.write_text(RUNNER.format(tests=str(Path(__file__).parent)), encoding="utf-8")
    src = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**__import__("os").environ, "PYTHONPATH": src},
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.readline()  # read one line, as `head -1` would, then hang up
    proc.stdout.close()
    err = proc.stderr.read().decode("utf-8", "replace")
    code = proc.wait(timeout=120)
    assert "Traceback" not in err, err
    assert code == 0, (code, err)
