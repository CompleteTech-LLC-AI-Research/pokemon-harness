"""Real-Python controls for the leading-literal-True ``and`` correction (#299).

``assert True and x != 1`` evaluates the comparison, so it fails when ``x == 1``;
the scanner must not report it as short-circuited. Real bypasses (``or True``,
and a nested ``or True`` behind a leading literal) pass silently and stay reported.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import (
    _comparisons_in,
    _count_comparison,
    _may_bypass,
)


def _fires(source, x=1):
    """Run the assert in real Python; True when it raises AssertionError."""
    names = {
        "x": x,
        "y": [],
        "flag": True,
        "calls": [],
        "record": {"termination": "cancelled_or_deadline", "errors": []},
    }
    try:
        exec(compile(source, "<control>", "exec"), names)  # noqa: S102
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


COUNT_PREFIX_NESTED_OR = (
    "assert len(y) > -1 and (len(calls) == 300 or True)",
    "assert len(y) >= 0 and (x != 1 or True)",
    "assert True and len(y) >= 0 and (x != 1 or True)",
    (
        'assert len(record.get("errors", [])) >= 0 and '
        '(record["termination"] != "cancelled_or_deadline" or True)'
    ),
)


@pytest.mark.parametrize("source", COUNT_PREFIX_NESTED_OR)
def test_a_nested_or_bypass_behind_a_count_tautology_passes_silently_and_is_reported(source):
    assert _fires(source) is False
    assert _reported_as_bypass(source) is True


def test_a_count_tautology_before_a_live_comparison_still_fails_and_is_not_reported():
    source = "assert len(y) > -1 and x != 1"
    assert _fires(source) is True
    assert _reported_as_bypass(source) is False


def _counted_sites(source):
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assert))
    return [
        site
        for comparison in _comparisons_in(node.test)
        for site in _count_comparison(tree, node, comparison)
    ]


def test_the_count_walker_skips_a_site_hidden_behind_a_count_tautology():
    hidden = "def probe(y, calls):\n    assert len(y) > -1 and (len(calls) == 300 or True)\n"
    assert _counted_sites(hidden) == []


def test_the_count_walker_still_counts_a_live_site():
    sites = _counted_sites("def probe(calls):\n    assert len(calls) == 300\n")
    assert [(site[0], site[2]) for site in sites] == [("probe", 300)]
