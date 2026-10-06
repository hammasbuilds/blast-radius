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
