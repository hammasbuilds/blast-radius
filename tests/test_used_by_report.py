"""The `--used-by` section of the terminal report.

This is the part of the output a dependency-bump PR is read for: not "the package
changed" but "the package changed somewhere your code calls". `--fail-on used` gates CI
on it. The first coverage measurement ever taken on this package showed the whole block
unexecuted by the suite - `report.py` 179-199 and the `used_by` branch of the CLI - so
the three distinct messages it can print were never checked against their own wording.

The `maybe` section matters most of the three. It exists because `obj.contains(...)`
cannot be attributed to a class without running the code, so those sites are reported as
possible rather than proven and deliberately do NOT trip `--fail-on used`. A report that
printed them in the same voice as a proven hit would be overstating what it knows.
"""

from __future__ import annotations

from blast_radius.report import summary
from blast_radius.types import Change, Kind, Report


def _report(**kwargs) -> Report:
    report = Report(
        package="packaging",
        old_version="21.3",
        new_version="24.0",
        compared=10,
        behaviour_checked=True,
        modules=["packaging"],
    )
    for key, value in kwargs.items():
        setattr(report, key, value)
    return report


def test_a_change_your_code_calls_is_named_with_its_call_sites() -> None:
    gone = Change(
        kind=Kind.GONE,
        qualname="packaging.version.LegacyVersion",
        used_at=["app/models.py:14", "app/models.py:88"],
    )
    text = summary(_report(changes=[gone], used_by="/repo", scanned_files=12))
    assert "1 change(s) your code references" in text
    assert "packaging.version.LegacyVersion" in text
    assert "2 site(s)" in text
    assert "app/models.py:14" in text


def test_a_possible_hit_is_reported_as_possible_and_says_why() -> None:
    """The wording has to carry the uncertainty, not just the count.

    A method call on a runtime object cannot be tied to a class without executing the
    code. Printed as a proven reference it would claim more than the tool knows, and
    `--fail-on used` would be gating CI on a guess.
    """
    maybe = Change(
        kind=Kind.RESHAPED,
        qualname="packaging.specifiers.SpecifierSet.contains",
        maybe_at=["app/resolve.py:31"],
    )
    text = summary(_report(changes=[maybe], used_by="/repo", scanned_files=12))
    assert "MAY reference" in text
    assert "whose type cannot be known without running it" in text
    assert "app/resolve.py:31" in text
    # And it must not be counted among the proven ones.
    assert "change(s) your code references" not in text


def test_changes_that_reach_nothing_say_how_much_was_searched() -> None:
    """"Nothing reaches you" is only meaningful beside the size of the search.

    Zero hits across 0 files and zero hits across 412 files are the same sentence and
    very different facts, so the file count belongs in it.
    """
    change = Change(kind=Kind.GONE, qualname="packaging.version.LegacyVersion")
    text = summary(_report(changes=[change], used_by="/repo", scanned_files=412))
    assert "None of these changes is referenced by the 412" in text


def test_without_used_by_none_of_the_three_sections_appears() -> None:
    change = Change(
        kind=Kind.GONE,
        qualname="packaging.version.LegacyVersion",
        used_at=["app/models.py:14"],
    )
    text = summary(_report(changes=[change]))
    # Matching on the section headers, not on the phrase: with no --used-by the report
    # ends with the hint "Pass --used-by <your repo> to see which of these your code
    # references", which contains the phrase and is the opposite of a reported hit.
    assert "change(s) your code references:" not in text
    assert "MAY reference" not in text
    assert "None of these changes is referenced" not in text
    assert "Pass --used-by" in text, "the hint that offers the feature should still show"
