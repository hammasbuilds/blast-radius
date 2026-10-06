"""Generated inputs have to be the right shape, or SILENT can never be found."""

from __future__ import annotations

import pytest

from blast_radius.diff import TYPED_POOL, argument_sets


def _values(signature: str, index: int = 0) -> list[str]:
    """The distinct values offered for one parameter across the generated calls."""
    seen = []
    for call in argument_sets(signature):
        parts = [p.strip() for p in call.strip("()").rstrip(",").split(",")]
        if index < len(parts) and parts[index] not in seen:
            seen.append(parts[index])
    return seen


@pytest.mark.parametrize(
    "signature",
    ["(price, percent)", "(weight)", "(amount)", "(count, limit)", "(n)", "(timeout)"],
)
def test_an_unannotated_numeric_parameter_gets_numbers(signature: str) -> None:
    """Most Python is unannotated, and it used to get version strings.

    The generic pool leads with "", "1.0", "2.0.dev1" because this tool was written for
    packaging libraries. On an ordinary numeric API every generated call raised TypeError
    on both sides, the function was counted `unreachable` with "never validly called -
    every argument set was the wrong type", and a SILENT change in it could not be found
    at all.
    """
    first = argument_sets(signature)[0]
    assert '"' not in first, f"{signature} was offered a string first: {first}"


def test_an_annotation_still_wins_over_the_name() -> None:
    """The name is a fallback, not an override - an annotation is better evidence."""
    assert argument_sets("(count: str)")[0] == '("x",)'


def test_an_unknown_name_falls_back_to_the_generic_pool() -> None:
    """A name whose meaning varies between codebases must not be guessed at.

    `value` and `data` could be anything, so they are deliberately absent from the
    hints and keep the behaviour they always had.
    """
    assert argument_sets("(thing, other)")[0] == '("", "",)'


def test_the_integer_pool_can_reach_a_moved_threshold() -> None:
    """A numeric behaviour change is usually a moved boundary.

    With a pool of 1, 0, -1, 2 a flat rate below 5 units that became a flat rate below 3
    is invisible: none of those four values falls between the old boundary and the new
    one. That is the single most likely shape of a silent numeric change, so the pool has
    to step into that range.
    """
    numbers = {int(v) for v in TYPED_POOL["int"]}
    assert {3, 4}.issubset(numbers), "nothing between a boundary at 3 and one at 5"
    offered = {int(v) for v in _values("(weight)") if v.lstrip("-").isdigit()}
    assert {3, 4}.issubset(offered), f"weight was never tried at 3 or 4: {sorted(offered)}"


class TestADefaultValueIsNotSyntax:
    """A string default's contents must not be read as part of the signature.

    The signature arrives as text, so a comma, bracket, paren or colon inside a default is
    indistinguishable from syntax unless quotes are tracked. `(items, sep=", ")` parsed as
    THREE parameters, `argument_sets` then built 3-arity calls, every call raised
    TypeError in both versions, and diff.py filed the function as "never validly called -
    every argument set was the wrong type". The types were right, the arity was wrong, and
    the report blamed the package for the tool's parser.

    It matters because SILENT behaviour change is the finding this tool exists to produce,
    and `sep=", "` / `delimiter=","` / `fmt="%Y-%m-%d, %H:%M"` are everywhere.
    """

    @pytest.mark.parametrize(
        ("signature", "arity"),
        [
            ('(items, sep=", ")', 2),
            ('(text: str, sep: str = ",") -> list[str]', 2),
            ("(value, fmt='%Y, %m')", 2),
            ('(x, flags="w+,b")', 2),
            # A colon inside a default used to be read as the annotation separator, so
            # `%M"` came back as this parameter's annotation.
            ('(value, fmt="%H:%M")', 2),
            # A closing paren inside a default ended the parameter list early.
            ('(x, fmt=")")', 2),
            ('(x, s="[")', 2),
            # Escaped quotes must not end the string early either.
            ('(x, q="a\\"b,c")', 2),
        ],
    )
    def test_a_separator_inside_a_string_default_is_not_a_separator(
        self, signature: str, arity: int
    ) -> None:
        from blast_radius.diff import _param_annotations

        assert len(_param_annotations(signature)) == arity

    def test_the_generated_calls_have_the_real_arity(self) -> None:
        """The consequence, not just the parse."""
        for call in argument_sets('(text: str, sep: str = ",") -> list'):
            assert call.strip("()").rstrip(",").count(",") == 1, call

    def test_an_annotation_after_a_quoted_colon_is_still_read(self) -> None:
        from blast_radius.diff import _param_annotations

        assert _param_annotations('(fmt: str = "%H:%M", n: int = 1)') == ["str", "int"]
