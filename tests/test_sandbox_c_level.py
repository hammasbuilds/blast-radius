"""The sandbox must also cover file I/O that happens below the Python names it patches."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import blast_radius.probe

# Located through the imported module, not the repo layout: CI also runs this suite
# against the installed wheel from a copied tests/ directory, where ../src/ is absent.
PROBE = Path(blast_radius.probe.__file__)

# Run the sandbox in a subprocess, as the real probe does. Installing it in-process
# patches builtins.open for the whole interpreter, which stops pytest from writing its
# own output - the reason the probe uses a subprocess in the first place.
SCRIPT = r"""
import json, os, sys
text = open(sys.argv[1], encoding="utf-8").read()
start = text.index('SANDBOX = r\"\"\"') + len('SANDBOX = r\"\"\"')
body = text[start : text.index('\n\"\"\"', start)]
os.environ["BR_SANDBOX"] = sys.argv[2]
ns = {"__name__": "__probe__", "_os": os, "_sys": sys}
exec(compile(body, "<sandbox>", "exec"), ns)
# The SANDBOX literal only DEFINES _br_sandbox; SURFACE and BEHAVIOUR call it as their
# first statement. Exec'ing the literal without calling it leaves every guard
# uninstalled, and then every attempt looks allowed and the test proves nothing.
ns["_br_sandbox"]()

import sqlite3
root, outside = sys.argv[2], sys.argv[3]
result = {}

def attempt(label, fn):
    try:
        fn()
        result[label] = "allowed"
    except PermissionError:
        result[label] = "blocked"
    except Exception as exc:
        result[label] = type(exc).__name__

attempt("sqlite_outside", lambda: sqlite3.connect(outside).close())
attempt("sqlite_inside", lambda: sqlite3.connect(os.path.join(root, "ok.db")).close())
attempt("sqlite_memory", lambda: sqlite3.connect(":memory:").close())
attempt("read_a_file", lambda: open(sys.argv[1], encoding="utf-8").read())
attempt("import_stdlib", lambda: __import__("csv"))
attempt("listdir", lambda: os.listdir(root))
result["outside_exists"] = os.path.exists(outside)
result["inside_exists"] = os.path.exists(os.path.join(root, "ok.db"))
sys.stdout.write("__JSON__" + json.dumps(result))
"""


def _run(tmp_path) -> dict:
    root = tmp_path / "root"
    root.mkdir()
    outside = Path(tempfile.gettempdir()) / "br_sqlite_probe.db"
    if outside.exists():
        outside.unlink()
    script = tmp_path / "run_sandbox.py"
    script.write_text(SCRIPT, encoding="utf-8", newline="\n")
    done = subprocess.run(
        [sys.executable, "-B", str(script), str(PROBE), str(root), str(outside)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert "__JSON__" in done.stdout, done.stdout + done.stderr
    return json.loads(done.stdout.split("__JSON__", 1)[1])


def test_sqlite_cannot_create_a_file_outside_the_root(tmp_path) -> None:
    """sqlite3 opens its file in C, so patching builtins.open and os.open cannot see it.

    With every name guard installed, `sqlite3.connect("outside.db")` created a real file
    outside the sandbox root. That is not hostile code - any library with a local cache
    or an on-disk index does it on import - and writing to the machine of somebody who
    only asked for a diff is what this sandbox exists to prevent.
    """
    out = _run(tmp_path)
    assert out["sqlite_outside"] == "blocked"
    assert out["outside_exists"] is False, "a file was created outside the sandbox root"


def test_the_probes_own_scratch_space_still_works(tmp_path) -> None:
    """A guard that blocks everything is not safe, it is broken.

    Writing inside the root, an in-memory database, reading, importing and listing all
    have to keep working or the behaviour pass cannot run at all.
    """
    out = _run(tmp_path)
    assert out["sqlite_inside"] == "allowed"
    assert out["inside_exists"] is True
    assert out["sqlite_memory"] == "allowed"
    assert out["read_a_file"] == "allowed"
    assert out["import_stdlib"] == "allowed"
    assert out["listdir"] == "allowed"
