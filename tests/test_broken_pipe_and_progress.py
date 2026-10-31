"""Two code paths nothing reached: the broken pipe, and the progress counter.

`suite-auditor coverage` on this package reports 81 of 84 functions reached by a passing
test. Two of the three it does not reach are live paths that matter, and the third was
dead code.

`_silence_stdout` is the broken-pipe handler. The README's own example is
`blast-radius check ... --json | jq .counts`, and a pipeline whose reader exits first is
exactly what triggers it - without it the interpreter's flush at exit fails on the closed
pipe and the process exits 120 with a traceback. Untested defensive code whose failure
mode is a traceback is worse than none, because it reads as covered.

`_finished` counts completed probes by counting a marker in the journal, and feeds the
progress line. If the journal format ever changed it would return 0 forever and the
progress would read "0 done" through a successful run - a counter that silently measures
nothing, which is the shape that has already cost this session several hours elsewhere.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blast_radius.cli import _silence_stdout  # noqa: E402
from blast_radius.probe import _finished  # noqa: E402


class TestSilenceStdout:
    def test_writing_after_it_does_not_raise(self, monkeypatch) -> None:
        """The whole point: a later print, including the interpreter's own flush."""
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        _silence_stdout()
        print("this must not raise")
        sys.stdout.flush()

    def test_it_survives_a_stdout_with_no_file_descriptor(self, monkeypatch) -> None:
        """pytest's captured stdout has no real fileno, and nor does a StringIO.

        `os.dup2(null, sys.stdout.fileno())` raises for both, and this must swallow that
        rather than turn a broken pipe into a second exception.
        """

        class NoFileno(io.StringIO):
            def fileno(self):  # noqa: ANN201
                raise io.UnsupportedOperation("no fileno")

        monkeypatch.setattr(sys, "stdout", NoFileno())
        _silence_stdout()
        print("still fine")

    def test_what_is_written_afterwards_goes_nowhere(self, monkeypatch) -> None:
        """Silenced, not merely non-raising: a report must not leak into a closed pipe."""
        sink = io.StringIO()
        monkeypatch.setattr(sys, "stdout", sink)
        _silence_stdout()
        print("swallowed")
        assert "swallowed" not in sink.getvalue()


class TestFinishedCount:
    def test_it_counts_the_results_in_a_journal(self, tmp_path: Path) -> None:
        journal = tmp_path / "j.jsonl"
        journal.write_text(
            "".join(json.dumps({"q": f"f{i}", "r": "ok"}) + "\n" for i in range(3)),
            encoding="utf-8",
        )
        assert _finished(journal) == 3

    def test_an_empty_journal_is_zero(self, tmp_path: Path) -> None:
        journal = tmp_path / "j.jsonl"
        journal.write_text("", encoding="utf-8")
        assert _finished(journal) == 0

    def test_a_missing_journal_is_zero_rather_than_an_error(self, tmp_path: Path) -> None:
        """It is called from the progress loop, so it must never interrupt a probe."""
        assert _finished(tmp_path / "not_there.jsonl") == 0

    def test_a_partial_last_line_is_not_counted(self, tmp_path: Path) -> None:
        """The journal is read while it is being written."""
        journal = tmp_path / "j.jsonl"
        journal.write_text(
            json.dumps({"q": "f0", "r": "ok"}) + "\n" + '{"q": "f1"',
            encoding="utf-8",
        )
        assert _finished(journal) == 1

    def test_the_marker_matches_what_the_writer_writes(self, tmp_path: Path) -> None:
        """The reason this is a test and not an assumption.

        `_finished` counts the bytes `"r": `, which is how json.dumps renders the key
        with its default separators. If the writer ever switched to compact separators
        the count would be 0 through a successful run and the progress line would read
        zero forever.
        """
        import blast_radius.probe as probe

        source = Path(probe.__file__).read_text(encoding="utf-8")
        assert "b'\"r\": '" in source, "the marker moved; check the writer still matches"
        assert "separators=" not in source, (
            "probe.py now passes explicit json separators - confirm _finished's marker "
            "still matches what the journal writer emits"
        )
