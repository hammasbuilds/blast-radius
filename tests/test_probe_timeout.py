"""A timeout has to actually time out.

The probe runs code somebody else wrote, and that code may start a process of its
own. `click.edit` opens an editor, `click.launch` a browser. If the child's stdout
is a pipe, the grandchild inherits it, and killing the child at the deadline does
not close a pipe the grandchild still holds - so `subprocess.run(timeout=60)` waits
in `communicate()` for an EOF that never arrives.

Measured: a click 7.1.2 -> 8.1.7 comparison bounded to roughly twelve minutes sat
for fifty-six, with Notepad.exe alive in the process tree holding the pipe open.
A `--timeout` that can be ignored is worse than no timeout, because the run looks
like it is still working.
"""

from __future__ import annotations

import sys
import time

from blast_radius.probe import call


def _pkg(root, name, body):
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(body, encoding="utf-8")
    return pkg


def test_a_spawned_process_that_outlives_the_child_does_not_hang_the_timeout(tmp_path):
    """The function starts a long-lived grandchild and then blocks forever itself.

    With pipes, the parent's wait outlasts the grandchild - 20 seconds here, and
    unbounded in the click case. With files there is no reader to block on: the
    child is killed at the deadline and whatever it wrote is read off disk.
    """
    body = (
        "import subprocess, sys, time\n"
        "\n"
        "\n"
        "def quick(x):\n"
        "    return x * 2\n"
        "\n"
        "\n"
        "def spawns_and_blocks(x):\n"
        "    # A grandchild that inherits this process's stdout and outlives it.\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        "    time.sleep(20)\n"
    )
    _pkg(tmp_path, "spawner", body)

    started = time.monotonic()
    out = call(
        tmp_path,
        {"spawner.quick": ["(21,)"], "spawner.spawns_and_blocks": ["(1,)"]},
        timeout=5,
    )
    elapsed = time.monotonic() - started

    # Five seconds per attempt, and the restart loop makes one more attempt after
    # skipping the blocker. Generous ceiling; the failure mode is 20s+ or forever.
    assert elapsed < 18, f"the timeout was not honoured: {elapsed:.1f}s for a 5s limit"
    assert out is not None, "the results already on disk were lost"
    assert out["spawner.quick"]["rows"][0] == ["ok", "42"], "work before the block was lost"
    assert out["__stopped_on__"] == ["spawner.spawns_and_blocks"]


def test_output_written_before_the_deadline_survives_the_kill(tmp_path):
    """Killing the child must not discard what it had already produced - which is
    what a pipe does when the read never completes."""
    body = (
        "import time\n"
        "\n"
        "\n"
        "def first(x):\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def second(x):\n"
        "    return x + 2\n"
        "\n"
        "\n"
        "def never(x):\n"
        "    time.sleep(30)\n"
    )
    _pkg(tmp_path, "partial", body)

    out = call(
        tmp_path,
        {"partial.first": ["(1,)"], "partial.second": ["(1,)"], "partial.never": ["(1,)"]},
        timeout=5,
    )

    assert out is not None
    assert out["partial.first"]["rows"][0] == ["ok", "2"]
    assert out["partial.second"]["rows"][0] == ["ok", "3"]
    assert "rows" not in out["partial.never"]


if sys.platform == "win32":  # pragma: no cover - a note, not a test
    # On POSIX the same deadlock exists and is easier to hit, because a shell in the
    # middle of a pipeline keeps the descriptor too. Nothing here is Windows-specific.
    pass


def test_the_timeout_bounds_each_function_not_the_whole_batch(tmp_path):
    """Five functions of 1.5 seconds each under a 4-second timeout.

    As a bound on the whole batch this lost the tail of every large package unless
    the timeout was huge - and a huge timeout made each function that never returns
    cost that much. Per function, all five finish and nothing is restarted.
    """
    body = "import time\n\n\n" + "".join(
        f"def slow{i}(x):\n    time.sleep(1.5)\n    return x + {i}\n\n\n" for i in range(5)
    )
    _pkg(tmp_path, "slowpkg", body)
    names = [f"slowpkg.slow{i}" for i in range(5)]
    out = call(tmp_path, {n: ["(1,)"] for n in names}, timeout=4)

    assert out is not None
    assert "__stopped_on__" not in out
    assert [out[n]["rows"][0] for n in names] == [["ok", str(1 + i)] for i in range(5)]
