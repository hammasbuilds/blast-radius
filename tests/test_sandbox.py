"""The behaviour pass must never do to the machine what the probed functions are for.

An audit run against click 8.1.6 -> 8.1.7 executed `click.launch` (four explorer.exe
processes) and `click.edit` (Notepad windows left open). Two layers now stop that:
functions whose names suggest a side effect are never called, and everything that IS
called runs in a sandbox where starting a process, opening a browser, reaching the
network, reading the console and writing outside a temp directory all raise.

Each sandbox test below would, without the sandbox, leave evidence behind - a file
written by a child process, a file outside the temp dir - and asserts it is absent.
"""

from __future__ import annotations

import os
import sys

import pytest

from blast_radius import cli
from blast_radius.diff import unsafe_to_call
from blast_radius.probe import call


def _pkg(root, name, body):
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(body, encoding="utf-8")
    return pkg


def _row(out, name):
    return out[name]["rows"][0]


@pytest.mark.parametrize(
    "name",
    [
        "click.launch",
        "click.edit",
        "click.termui.edit",
        "click.termui.launch",
        "click.utils.open_file",
        "click.getchar",
        "click.pause",
        "click.prompt",
        "click.confirm",
        "click.echo_via_pager",
        "click.termui.hidden_prompt_func",
        "webbrowser.open",
        "os.system",
        "shutil.rmtree",
        "pkg.remove_file",
        "pkg.deleteAll",
        "pkg.save",
        "pkg.write_config",
        "requests.get",
        "requests.api.request",
        "httpx.post",
        "requests.Session.get",
        "urllib3.PoolManager.request",
        "pkg.Client.send",
        "time.sleep",
        "sys.exit",
        "subprocess.run",
    ],
)
def test_functions_named_for_a_side_effect_are_never_called(name):
    assert unsafe_to_call(name), name


@pytest.mark.parametrize(
    "name",
    [
        "packaging.version.parse",
        "click.echo",
        "click.style",
        "jinja2.utils.urlize",
        "werkzeug.datastructures.Headers.get",  # a mapping's .get is not a request
        "requests.structures.CaseInsensitiveDict.get",
        "bs4.BeautifulSoup.get_text",
        "pkg.opener_for",  # the word is "opener", not "open"
        "pkg.runtime_version",  # "runtime", not "run"
    ],
)
def test_ordinary_functions_are_still_called(name):
    assert unsafe_to_call(name) is None, name


def test_starting_a_process_is_refused(tmp_path):
    marker = tmp_path / "child-ran.txt"
    body = (
        "import subprocess, sys\n\n\n"
        "def spawn(x):\n"
        f'    code = \'open("{marker.as_posix()}", "w").write("ran")\'\n'
        "    subprocess.run([sys.executable, '-c', code], check=True)\n"
        "    return 'spawned'\n\n\n"
        "def shell(x):\n"
        "    import os\n"
        "    return os.system('echo hi')\n"
    )
    _pkg(tmp_path / "site", "spawner", body)
    out = call(tmp_path / "site", {"spawner.spawn": ["(1,)"], "spawner.shell": ["(1,)"]})
    assert _row(out, "spawner.spawn")[0] == "raise"
    assert "PermissionError" in _row(out, "spawner.spawn")[1]
    assert "PermissionError" in _row(out, "spawner.shell")[1]
    assert not marker.exists(), "a child process ran inside the behaviour pass"


def test_click_launch_and_edit_shaped_functions_start_nothing(tmp_path):
    """The two functions from the audit, as click implements them: launch hands a URL
    to the OS (os.startfile on Windows, a subprocess elsewhere) and edit runs $EDITOR."""
    marker = tmp_path / "editor-ran.txt"
    body = (
        "import os, subprocess, sys, webbrowser\n\n\n"
        "def launch(url):\n"
        "    if hasattr(os, 'startfile'):\n"
        "        os.startfile(url)\n"
        "    return subprocess.call(['xdg-open', url])\n\n\n"
        "def edit(text):\n"
        "    editor = os.environ.get('EDITOR', 'notepad')\n"
        f'    code = \'open("{marker.as_posix()}", "w").write("ran")\'\n'
        "    subprocess.Popen([sys.executable, '-c', code, editor]).wait()\n"
        "    return text\n\n\n"
        "def show(url):\n"
        "    return webbrowser.open(url)\n"
    )
    _pkg(tmp_path / "site", "clicky", body)
    out = call(
        tmp_path / "site",
        {
            n: ['("https://example.invalid",)']
            for n in ("clicky.launch", "clicky.edit", "clicky.show")
        },
    )
    for name in ("clicky.launch", "clicky.edit", "clicky.show"):
        assert _row(out, name)[0] == "raise", name
        assert "PermissionError" in _row(out, name)[1], name
    assert not marker.exists()


def test_the_network_is_refused(tmp_path):
    body = (
        "import socket\n\n\n"
        "def reach(x):\n"
        "    s = socket.socket()\n"
        "    s.settimeout(2)\n"
        "    s.connect(('192.0.2.1', 80))\n"
        "    return 'connected'\n\n\n"
        "def lookup(x):\n"
        "    return socket.getaddrinfo('example.com', 80)\n"
    )
    _pkg(tmp_path / "site", "netty", body)
    out = call(tmp_path / "site", {"netty.reach": ["(1,)"], "netty.lookup": ["(1,)"]})
    assert "PermissionError" in _row(out, "netty.reach")[1]
    assert "PermissionError" in _row(out, "netty.lookup")[1]


def test_writing_outside_the_temp_directory_is_refused(tmp_path):
    outside = tmp_path / "outside.txt"
    body = (
        "import os, pathlib\n\n\n"
        "def write(x):\n"
        f"    open(r'{outside}', 'w').write('x')\n\n\n"
        "def write_path(x):\n"
        f"    pathlib.Path(r'{outside}').write_text('x')\n\n\n"
        "def remove(x):\n"
        f"    os.remove(r'{tmp_path / 'keep.txt'}')\n\n\n"
        "def write_here(x):\n"
        "    with open('scratch.txt', 'w') as fh:\n"
        "        fh.write('fine')\n"
        "    return open('scratch.txt').read()\n"
    )
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    _pkg(tmp_path / "site", "writer", body)
    names = ["writer.write", "writer.write_path", "writer.remove", "writer.write_here"]
    out = call(tmp_path / "site", {n: ["(1,)"] for n in names})
    for name in names[:3]:
        assert "PermissionError" in _row(out, name)[1], name
    assert not outside.exists()
    assert (tmp_path / "keep.txt").exists()
    # Inside the probe's own temp directory, writing is fine.
    assert _row(out, "writer.write_here") == ["ok", "'fine'"]


def test_writing_through_io_fileio_and_the_io_module_is_refused(tmp_path):
    """The routes the name patches cannot reach.

    The sandbox rebinds `builtins.open`, `io.open` and `os.open`. `io.FileIO` is a
    different callable that opens the file in C, and `_io` holds the same objects under
    names nothing rebound - so each of these wrote a file outside the probe's root with
    every name patch installed, while `open()` next to them was blocked. Ordinary library
    code uses `io.FileIO` for raw binary I/O; the name filter cannot help, because the
    function's own name is innocent.

    Closed in the audit hook rather than with more name patches, for the reason the hook's
    sqlite3 branch already gives: CPython raises `open` from inside the C implementation,
    so one branch sees every route and cannot be bypassed by a reference bound before the
    sandbox was installed.
    """
    outside = tmp_path / "outside_io.txt"
    body = (
        "import io\nimport _io\n\n\n"
        "def write_fileio(x):\n"
        f"    io.FileIO(r'{outside}', 'w').close()\n\n\n"
        "def write_io_module(x):\n"
        f"    _io.open(r'{outside}', 'w').close()\n\n\n"
        "def write_io_module_fileio(x):\n"
        f"    _io.FileIO(r'{outside}', 'w').close()\n\n\n"
        "def read_outside(x):\n"
        f"    return io.FileIO(r'{tmp_path / 'readable.txt'}', 'r').read(4).decode()\n"
    )
    (tmp_path / "readable.txt").write_text("keep", encoding="utf-8")
    _pkg(tmp_path / "site", "rawio", body)
    writers = ["rawio.write_fileio", "rawio.write_io_module", "rawio.write_io_module_fileio"]
    out = call(tmp_path / "site", {n: ["(1,)"] for n in [*writers, "rawio.read_outside"]})
    for name in writers:
        assert "PermissionError" in _row(out, name)[1], name
    assert not outside.exists(), "a write escaped the sandbox"
    # Reading is not a side effect, and blocking it would make the behaviour pass useless
    # rather than safe.
    assert _row(out, "rawio.read_outside") == ["ok", "'keep'"]


def test_home_and_the_console_are_not_the_users(tmp_path):
    body = (
        "import getpass, os\n\n\n"
        "def home(x):\n"
        "    return os.path.expanduser('~')\n\n\n"
        "def password(x):\n"
        "    return getpass.getpass()\n\n\n"
        "def editor(x):\n"
        "    return os.environ.get('EDITOR')\n"
    )
    _pkg(tmp_path / "site", "who", body)
    out = call(tmp_path / "site", {n: ["(1,)"] for n in ("who.home", "who.password", "who.editor")})
    assert os.path.expanduser("~") not in _row(out, "who.home")[1]
    assert _row(out, "who.home") == ["ok", "'<home>'"], "home is redirected and normalised"
    assert "EOFError" in _row(out, "who.password")[1]
    assert "blast-radius" in _row(out, "who.editor")[1]


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt is Windows-only")
def test_reading_the_windows_console_directly_does_not_block(tmp_path):
    """click.getchar reads with msvcrt.getwch, around stdin, so closing stdin did not
    reach it and each call cost a full --timeout per version."""
    _pkg(tmp_path / "site", "keys", "import msvcrt\n\n\ndef key(x):\n    return msvcrt.getwch()\n")
    out = call(tmp_path / "site", {"keys.key": ["(1,)"]}, timeout=10)
    assert "EOFError" in _row(out, "keys.key")[1]
    assert "__stopped_on__" not in out


def test_the_cli_skips_side_effect_names_and_says_it_runs_code(tmp_path, fake_index, capsys):
    marker = tmp_path / "launched.txt"
    body = (
        "def launch(url):\n"
        f"    open(r'{marker}', 'w').write('launched')\n\n\n"
        "def double(x):\n"
        "    return x * 2\n"
    )
    fake_index({"1.0": {"fakepkg/__init__.py": body}, "2.0": {"fakepkg/__init__.py": body}})
    code = cli.main(["check", "fakepkg", "1.0", "2.0", "--out", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 0
    assert not marker.exists()
    assert "1 skipped: names suggest side effects" in captured.out
    assert "runs fakepkg's own code, in a sandboxed subprocess" in captured.err
    assert "--no-behaviour" in captured.err
    import json

    data = json.loads((tmp_path / "out" / "blast-radius.json").read_text(encoding="utf-8"))
    assert list(data["not_run_side_effects"]) == ["fakepkg.launch"]
