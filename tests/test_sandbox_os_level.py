"""The sandbox must also cover path changes made below the names it patches in `os`.

`os` is a thin wrapper over a C module - `nt` on Windows, `posix` elsewhere - and
patching a name in `os` leaves that module untouched. This is the same shape of hole as
`_io` sitting beside `io`, which the probe already closes for writes.

A battery of 28 escape routes was run against the sandbox as it stood. Eight got past
it: `nt.unlink`, `nt.rename`, `nt.mkdir`, `nt.utime` and `nt.truncate` each changed a
file outside the root with every name patch installed; a reference bound before the
sandbox was installed (`f = os.unlink` at module import, which every stdlib module
imported during interpreter startup is holding) did the same; and `_socket.socket` -
the C type that `socket.socket` subclasses - reached the real internet and failed only
on a connect timeout.

The first version of that battery reported five rather than eight, because it judged an
escape by "the file vanished or a new one appeared". `nt.truncate` emptied the victim in
place and `nt.utime` only moved its timestamps, so both looked held. A weaker detector
reports a smaller hole, so these tests compare size, mode, mtime and contents.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import blast_radius.probe

# Located through the imported module, not the repo layout: CI also runs this suite
# against the installed wheel from a copied tests/ directory, where ../src/ is absent.
PROBE = Path(blast_radius.probe.__file__)

# Absent in the installed-wheel CI job, which copies only tests/.
README = Path(__file__).parent.parent / "README.md"

# Run the sandbox in a subprocess, as the real probe does and for the same reason:
# installing it in-process patches builtins.open for the whole interpreter, which stops
# pytest from writing its own output.
SCRIPT = r"""
import json, os, sys

# Bound BEFORE the sandbox installs, standing in for the reference every stdlib module
# imported during interpreter startup is already holding.
PRE_UNLINK = os.unlink
PRE_RENAME = os.rename

text = open(sys.argv[1], encoding="utf-8").read()
start = text.index('SANDBOX = r\"\"\"') + len('SANDBOX = r\"\"\"')
body = text[start : text.index('\n\"\"\"', start)]

root, outside = sys.argv[2], sys.argv[3]

# One victim per route, so a blocked route cannot be credited to a route that already
# removed the file.
victims = {}
for name in (
    "os_remove", "os_unlink", "pathlib_unlink", "os_rename", "pathlib_rename",
    "os_replace", "os_chmod", "os_utime", "os_link", "open_write", "io_fileio_write",
    "nt_unlink", "nt_rename", "nt_mkdir", "nt_utime", "nt_truncate", "nt_chmod",
    "prebound_unlink", "prebound_rename",
):
    path = os.path.join(outside, name + ".txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("original")
    victims[name] = path


def snapshot():
    # Size, mode, mtime and contents of everything under the outside directory.
    # Checking only for a missing or an extra name misses a truncate and a utime.
    state = {}
    for base, dirs, files in os.walk(outside):
        for entry in list(dirs) + list(files):
            path = os.path.join(base, entry)
            try:
                st = os.stat(path)
                body = b"<dir>"
                if os.path.isfile(path):
                    with open(path, "rb") as fh:
                        body = fh.read()
            except OSError as exc:
                state[path] = "unreadable:%s" % exc.errno
                continue
            state[path] = [
                st.st_size, st.st_mode, int(st.st_mtime),
                body.decode("utf-8", "replace"),
            ]
    return state


before = snapshot()

os.environ["BR_SANDBOX"] = root
ns = {"__name__": "__probe__", "_os": os, "_sys": sys}
exec(compile(body, "<sandbox>", "exec"), ns)
# The SANDBOX literal only DEFINES _br_sandbox; SURFACE and BEHAVIOUR call it as their
# first statement. Exec'ing the literal without calling it leaves every guard
# uninstalled, and then every attempt looks allowed and the test proves nothing.
ns["_br_sandbox"]()

# The C module `os` wraps. Imported after the sandbox installed, exactly as a package
# under test would reach it.
low = __import__("nt" if os.name == "nt" else "posix")

result = {}


def attempt(label, fn):
    try:
        fn()
        result[label] = "allowed"
    except PermissionError:
        result[label] = "blocked"
    except Exception as exc:
        result[label] = type(exc).__name__


# The Python-level names first. These are the name-patch layer's job and all of them
# held from the beginning - which is exactly why they belong in the test: the layer that
# was never broken is the one a later change is most likely to break quietly, and
# "8 of 28 escaped" is not a measurement anyone can check if only the 8 are run.
attempt("os_remove", lambda: os.remove(victims["os_remove"]))
attempt("os_unlink", lambda: os.unlink(victims["os_unlink"]))
attempt("pathlib_unlink", lambda: __import__("pathlib").Path(victims["pathlib_unlink"]).unlink())
attempt("os_rename", lambda: os.rename(victims["os_rename"], victims["os_rename"] + ".moved"))
attempt("pathlib_rename", lambda: __import__("pathlib").Path(
    victims["pathlib_rename"]).rename(victims["pathlib_rename"] + ".moved"))
attempt("os_replace", lambda: os.replace(victims["os_replace"], victims["os_replace"] + ".moved"))
attempt("os_mkdir", lambda: os.mkdir(os.path.join(outside, "made_by_os")))
attempt("os_chmod", lambda: os.chmod(victims["os_chmod"], 0o600))
attempt("os_utime", lambda: os.utime(victims["os_utime"], (0, 0)))
attempt("os_link", lambda: os.link(victims["os_link"], victims["os_link"] + ".link"))
attempt("open_write", lambda: open(victims["open_write"], "w", encoding="utf-8").write("x"))
attempt("io_fileio_write", lambda: __import__("io").FileIO(
    victims["io_fileio_write"], "w").write(b"x"))
attempt("sqlite_outside", lambda: __import__("sqlite3").connect(
    os.path.join(outside, "made.db")).execute("create table t(x)"))
attempt("shutil_copyfile", lambda: __import__("shutil").copyfile(
    sys.argv[1], os.path.join(outside, "made.copy")))
attempt("subprocess_popen", lambda: __import__("subprocess").Popen(["cmd", "/c", "echo"]))
attempt("socket_connect", lambda: __import__("socket").socket().connect(("93.184.216.34", 80)))
attempt("ctypes_foreign", lambda: __import__("ctypes").CDLL("definitely_not_a_system_library"))

attempt("nt_unlink", lambda: low.unlink(victims["nt_unlink"]))
attempt("nt_rename", lambda: low.rename(victims["nt_rename"], victims["nt_rename"] + ".moved"))
attempt("nt_mkdir", lambda: low.mkdir(os.path.join(outside, "made_by_nt")))
attempt("nt_utime", lambda: low.utime(victims["nt_utime"], (0, 0)))
attempt("nt_truncate", lambda: low.truncate(victims["nt_truncate"], 0))
attempt("nt_chmod", lambda: low.chmod(victims["nt_chmod"], 0o600))
attempt("nt_open_write", lambda: low.close(
    low.open(os.path.join(outside, "made_by_nt_open"), low.O_CREAT | low.O_WRONLY)
))
attempt("prebound_unlink", lambda: PRE_UNLINK(victims["prebound_unlink"]))
attempt("prebound_rename", lambda: PRE_RENAME(
    victims["prebound_rename"], victims["prebound_rename"] + ".moved"
))

# The C socket type that socket.socket subclasses. A refusal is raised by the hook
# before any packet leaves, so this does not wait for a connect timeout.
def _raw_connect():
    import _socket

    _socket.socket().connect(("93.184.216.34", 80))


attempt("raw_socket_connect", _raw_connect)

# And the probe's own work has to keep working, or the behaviour pass cannot run.
attempt("inside_mkdir", lambda: low.mkdir(os.path.join(root, "scratch")))
attempt("inside_write", lambda: low.close(
    low.open(os.path.join(root, "scratch.txt"), low.O_CREAT | low.O_WRONLY)
))
attempt("inside_unlink", lambda: low.unlink(os.path.join(root, "scratch.txt")))
attempt("inside_utime", lambda: low.utime(root, None))
# Reads probe.py, not one of the victims: if a route above actually succeeds in
# deleting its victim, a read of that victim fails for a reason that has nothing to do
# with reading being allowed, and the failure message points at the wrong guard.
attempt("read_outside", lambda: open(sys.argv[1], encoding="utf-8").read())
attempt("import_stdlib", lambda: __import__("csv"))
attempt("loopback_lookup", lambda: __import__("socket").getaddrinfo("127.0.0.1", 80))

result["_changed_outside"] = sorted(
    path for path in set(before) | set(snapshot())
    if before.get(path) != snapshot().get(path)
)
sys.stdout.write("__JSON__" + json.dumps(result))
"""

ESCAPE_ROUTES = (
    # Python-level names, covered by the name patches. All held from the start; they are
    # here because an untested guard is the one a later change breaks quietly.
    "os_remove",
    "os_unlink",
    "pathlib_unlink",
    "os_rename",
    "pathlib_rename",
    "os_replace",
    "os_mkdir",
    "os_chmod",
    "os_utime",
    "os_link",
    "open_write",
    "io_fileio_write",
    "sqlite_outside",
    "shutil_copyfile",
    "subprocess_popen",
    "socket_connect",
    "ctypes_foreign",
    # C-level and pre-bound routes. Eight of these got past an earlier sandbox.
    "nt_unlink",
    "nt_rename",
    "nt_mkdir",
    "nt_utime",
    "nt_truncate",
    "nt_chmod",
    "nt_open_write",
    "prebound_unlink",
    "prebound_rename",
    "raw_socket_connect",
)

MUST_KEEP_WORKING = (
    "inside_mkdir",
    "inside_write",
    "inside_unlink",
    "inside_utime",
    "read_outside",
    "import_stdlib",
    "loopback_lookup",
)


def _run(tmp_path) -> dict:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    script = tmp_path / "run_sandbox.py"
    script.write_text(SCRIPT, encoding="utf-8", newline="\n")
    done = subprocess.run(
        [sys.executable, "-B", str(script), str(PROBE), str(root), str(outside)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert "__JSON__" in done.stdout, done.stdout + done.stderr
    return json.loads(done.stdout.split("__JSON__", 1)[1])


def test_the_c_module_under_os_cannot_change_anything_outside_the_root(tmp_path) -> None:
    """Every route that got past the name patches is refused.

    These are not hostile calls. Ordinary library code deletes a stale cache entry,
    renames a file into place, or touches a timestamp on import, and doing any of it on
    the machine of somebody who only asked for a diff is what the sandbox exists to
    prevent.
    """
    out = _run(tmp_path)
    allowed = {route: out[route] for route in ESCAPE_ROUTES if out[route] != "blocked"}
    assert not allowed, f"these routes were not refused: {allowed}"


def test_nothing_outside_the_root_was_touched(tmp_path) -> None:
    """The verdict that does not depend on an exception being raised.

    A guard can raise PermissionError after the damage is done, so the filesystem gets
    the last word: size, mode, mtime and contents of every path outside the root have
    to be exactly what they were.
    """
    out = _run(tmp_path)
    assert out["_changed_outside"] == [], (
        f"the sandbox let these change outside its root: {out['_changed_outside']}"
    )


def test_the_probes_own_work_still_works(tmp_path) -> None:
    """A guard that blocks everything is not safe, it is broken.

    Writing, creating and removing scratch files inside the root, touching the root's
    own timestamp, reading any file, importing, and resolving loopback all have to keep
    working. The behaviour pass does all of them on every run, and three of the four
    sandbox tightenings in this file's history could have broken one.
    """
    out = _run(tmp_path)
    broken = {name: out[name] for name in MUST_KEEP_WORKING if out[name] != "allowed"}
    assert not broken, f"the sandbox broke the probe's own work: {broken}"


@pytest.mark.skipif(not README.exists(), reason="README.md is not shipped with the tests")
def test_the_readme_quotes_the_number_of_routes_actually_run() -> None:
    """The README said 28 while the tests ran 10.

    "A battery of 28 escape routes is run against the sandbox" was true of a script in a
    scratch directory, not of this repository - so the one claim in the README that reads
    as a measurement anyone can repeat was the one that could not be. The missing 18 were
    the Python-level names: `os.remove`, `pathlib.Path.unlink`, `open(..., "w")`,
    `shutil.copyfile`, `subprocess.Popen`, a foreign `ctypes.CDLL`. Every one of them held
    from the beginning, which is exactly why they were worth adding: the guard that has
    never broken is the one a later change breaks quietly, and "8 of 28 escaped" is not
    checkable if only the 8 are run.
    """
    import re

    from tests import test_sandbox_c_level  # noqa: PLC0415

    c_level = re.findall(r'attempt\("([a-z_0-9]+)"', test_sandbox_c_level.SCRIPT)
    total = len(ESCAPE_ROUTES) + len([n for n in c_level if "outside" in n])

    readme = README.read_text(encoding="utf-8")
    quoted = {int(n) for n in re.findall(r"(\d+) escape routes", readme)}
    assert quoted, "the README no longer says how many escape routes are run"
    assert quoted == {total}, (
        f"the README quotes {sorted(quoted)} escape routes; the tests assert {total}"
    )
