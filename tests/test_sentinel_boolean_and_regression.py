"""Real-Python controls for the leading-literal-True ``and`` correction (#299).

``assert True and x != 1`` evaluates the comparison, so it fails when ``x == 1``;
the scanner must not report it as short-circuited. Real bypasses (``or True``,
and a nested ``or True`` behind a leading literal) pass silently and stay reported.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _may_bypass


def _fires(source, x=1):
    """Run the assert in real Python; True when it raises AssertionError."""
    try:
        exec(compile(source, "<control>", "exec"), {"x": x, "y": [], "flag": True})  # noqa: S102
    except AssertionError:
        return True
    return False


def _reported_as_bypass(source):
    return _may_bypass(ast.parse(source).body[0].test)


LEADING_TRUE_AND = (
    "assert True and x != 1",
    "assert (1 == 1) and x != 1",
    "assert True and True and x != 1",
    "assert len(y) >= 0 and x != 1",
)


@pytest.mark.parametrize("source", LEADING_TRUE_AND)
def test_a_leading_true_and_comparison_fails_in_python_and_is_not_reported_as_a_bypass(source):
    assert _fires(source) is True
    assert _reported_as_bypass(source) is False


@pytest.mark.parametrize("source", LEADING_TRUE_AND)
def test_a_leading_true_and_comparison_passes_only_when_the_comparison_holds(source):
    assert _fires(source, x=0) is False


REAL_BYPASSES = (
    "assert x != 1 or True",
    "assert True or x != 1",
    "assert True and (x != 1 or True)",
)


@pytest.mark.parametrize("source", REAL_BYPASSES)
def test_a_real_bypass_passes_silently_in_python_and_stays_reported(source):
    assert _fires(source) is False
    assert _reported_as_bypass(source) is True


def test_a_falsy_leading_operand_fails_the_assert_so_nothing_is_hidden():
    source = "assert 0 and True and x != 1"
    assert _fires(source, x=0) is True
    assert _reported_as_bypass(source) is False
