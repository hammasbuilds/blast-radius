"""Reading a real client's imports, and the two matchers that decide a hit.

Section 4.4 estimates caller-level impact under uniform symbol choice and says plainly that
the assumption overstates. `scripts/caller_census.py` replaces the estimate with a
measurement against real client code, and everything load-bearing about that measurement is
in two pure functions: what names a client is seen to use, and whether any of them is one
that broke. Both have failure modes that make the measured rate wrong in a *stated*
direction, which is worse than noise, so they are pinned here.

The network parts - the reverse index and the sdist downloads - are not exercised. What is
exercised is every decision that turns a downloaded file into a number.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

caller_census = pytest.importorskip("caller_census")


def _used(source: str, module: str = "click") -> set[str]:
    return caller_census.used_names([("client.py", source)], module)


# ------------------------------------------------------------- what a client is seen to use


def test_a_from_import_is_a_usage():
    assert _used("from click import echo") == {"echo"}
    assert _used("from click.core import Option") == {"core.Option"}


def test_an_alias_is_followed_to_the_name_it_stands_for():
    assert _used("from click.core import Option as Opt\nOpt(1)") == {"core.Option"}
    assert _used("import click.testing as t\nt.CliRunner()") == {"testing.CliRunner"}


def test_an_attribute_on_a_plain_import_is_a_usage():
    assert _used("import click\nclick.echo('x')") == {"echo"}
    assert _used("import click\nclick.testing.CliRunner()") == {"testing.CliRunner"}


def test_only_the_outermost_chain_counts():
    """`click.testing.CliRunner` is three nested nodes and one usage.

    Recording the inner ones yielded a bare `testing` alongside `testing.CliRunner`, and a
    bare module path then matches every broken name beneath it - so a client touching one
    function read as exposed to the whole submodule, and the measured rate was too high for
    a reason about the parser rather than about the ecosystem.
    """
    used = _used("import click\nclick.testing.CliRunner().invoke()")
    assert "testing" not in used
    assert used == {"testing.CliRunner"}


def test_importing_a_module_and_touching_nothing_is_not_a_usage():
    """A client that cannot be broken by a name going away must not count as a user.

    Counting it would be the uniform-choice assumption in another form: it would put a
    caller with no exposure into the denominator *and* give it the pair's average chance of
    being hit.
    """
    assert _used("import click") == set()
    assert _used("import click  # noqa\nprint(1)") == set()


def test_another_package_s_names_are_not_collected():
    assert _used("import clicksomething\nclicksomething.echo()") == set()
    assert _used("from clicky import echo") == set()
    assert _used("import other\nother.echo()") == set()


def test_a_star_import_contributes_no_name():
    """It is a usage of the module and of no nameable symbol, so it cannot be matched."""
    assert _used("from click import *") == set()


def test_a_relative_import_is_not_a_usage_of_this_module():
    """`from . import echo` inside the client is the client's own module, not click's."""
    assert _used("from . import echo") == set()
    assert _used("from .click import echo") == set()


def test_a_file_that_will_not_parse_is_skipped_rather_than_failing_the_client():
    """Python 2 files and templates still ship inside sdists."""
    sources = [("old.py", "print 'two'"), ("new.py", "from click import echo")]
    assert caller_census.used_names(sources, "click") == {"echo"}


# --------------------------------------------------------------------------- the matchers


def test_a_used_name_that_broke_is_an_exact_hit():
    got = caller_census._hits({"echo"}, ["click.echo"])
    assert got["exact"] == ["click.echo"]
    assert got["enclosing"] == ["click.echo"]
    assert got["loose"] == ["click.echo"]


def test_a_re_export_still_names_the_same_object():
    """`from click import Option` against `click.core.Option` is the same class."""
    assert caller_census._hits({"Option"}, ["click.core.Option"])["exact"] == ["click.core.Option"]


def test_a_broken_member_of_a_class_the_client_uses_is_enclosing_and_not_exact():
    """The tier that decides the headline, and the reason it has its own column.

    `click.Path.coerce_path_result` is an internal hook. Folding this case into the exact
    tier scored every client doing `click.Path(exists=True)` as hit and put the measured
    rate at 76% for click, on the strength of a method almost none of them call. It is real
    exposure for a subclass or a caller of that member and none at all otherwise, and the
    import graph cannot tell those apart - so it is reported as the middle of a bracket
    rather than as the answer.
    """
    got = caller_census._hits({"Path"}, ["click.Path.coerce_path_result"])
    assert got["exact"] == []
    assert got["enclosing"] == ["click.Path.coerce_path_result"]


def test_a_name_the_client_does_not_use_is_no_hit_at_all():
    got = caller_census._hits({"echo"}, ["click.testing.other"])
    assert got == {"exact": [], "enclosing": [], "loose": []}


def test_a_coincidence_of_the_last_component_is_loose_only():
    """`click.other.Option` against a client using `core.Option` is not the same name."""
    got = caller_census._hits({"core.Option"}, ["click.other.Option"])
    assert got["exact"] == []
    assert got["enclosing"] == []
    assert got["loose"] == ["click.other.Option"]


def test_the_three_tiers_nest_so_they_bracket_the_answer():
    used = {"core.Option", "echo", "testing.CliRunner"}
    broken = [
        "click.core.Option.flag_value",
        "click.echo",
        "click.other.Option",
        "click.testing.CliRunner.invoke",
        "click.unrelated.thing",
    ]
    got = caller_census._hits(used, broken)
    assert set(got["exact"]) <= set(got["enclosing"]) <= set(got["loose"])
    assert got["exact"] == ["click.echo"]
    # The two member-of-a-used-class cases land in the middle tier, not the lower one.
    assert set(got["enclosing"]) - set(got["exact"]) == {
        "click.core.Option.flag_value",
        "click.testing.CliRunner.invoke",
    }
    assert set(got["loose"]) - set(got["enclosing"]) == {"click.other.Option"}
    assert "click.unrelated.thing" not in got["loose"]


def test_no_broken_name_means_no_hit_whatever_the_client_uses():
    assert caller_census._hits({"echo", "core.Option"}, []) == {
        "exact": [],
        "enclosing": [],
        "loose": [],
    }


# ----------------------------------------------------------- the fields they are driven by


def test_a_requirement_specifier_yields_its_distribution_name():
    cases = {
        "click>=8.0": "click",
        'click>=8.0; extra == "cli"': "click",
        'importlib-metadata; python_version < "3.8"': "importlib-metadata",
        "requests[socks]>=2": "requests",
        "A.B_c": "a-b-c",
        "typing_extensions!=4.0": "typing-extensions",
    }
    for spec, expected in cases.items():
        assert caller_census._requirement_name(spec) == expected, spec


def test_an_unparseable_specifier_is_skipped_rather_than_guessed_at():
    for spec in ("", "   ", ">=1.0", "; extra == 'x'"):
        assert caller_census._requirement_name(spec) is None, spec


def test_the_import_roots_come_from_the_ledger_s_display_field():
    """15 of the 163 pairs carry several roots or a truncated list.

    That field is built for display, so it can read `attrs,attr` or end in "and 5 more".
    Passing the whole string as a module name makes its root `attrs,attr`, which matches
    nothing - and a pair that matches nothing is indistinguishable from a pair no client
    uses, which is the measurement.
    """
    assert caller_census.import_roots("attrs,attr", "attrs") == ["attrs", "attr"]
    assert caller_census.import_roots("azure.ai.projects", "azure-ai-projects") == ["azure"]
    assert caller_census.import_roots("a, b, c and 5 more", "x") == ["a", "b", "c"]
    # Nothing usable falls back to the distribution name with its dashes turned to
    # underscores, which is the usual import name.
    assert caller_census.import_roots("", "some-pkg") == ["some_pkg"]
    assert caller_census.import_roots("and 8 more", "some-pkg") == ["some_pkg"]


def test_the_cohorts_measured_are_the_ones_whose_version_promised_compatibility():
    assert set(caller_census.PROMISING) == {"patch", "minor", "0.x-patch"}
