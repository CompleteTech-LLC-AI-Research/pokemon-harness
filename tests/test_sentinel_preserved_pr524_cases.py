"""Preserve PR524's execution cases on the reviewed starred-store implementation.

The rejected helper missed Continue; its failed original head is retained in
external evidence. The original six and subsequent six author cases retain their runtime outcomes
without that duplicate rule; conservative analyzer declines are explicit.
"""

import ast
import sys

import pytest

from tests import _timed_menu_milestone_sentinel_support as support
from tests.test_timed_menu_milestone_sentinels import _assert_entry_contract

AUTHOR_ROWS = [
    (
        "a starred store in a for body binds a list at a later sibling with",
        "    for _ in (1,):\n        *cs, = (contextlib.suppress(AssertionError),)\n    with cs:\n        assert x != 1\n",
        False,
    ),
    (
        "CONTROL a plain store in a for body read by a later sibling with",
        "    for _ in (1,):\n        cs = contextlib.nullcontext()\n    with cs:\n        assert x != 1\n",
        True,
    ),
    (
        "CONTROL a later rebind supersedes the loop's starred store",
        "    for _ in (1,):\n        *cs, = (contextlib.suppress(AssertionError),)\n    cs = contextlib.nullcontext()\n    with cs:\n        assert x != 1\n",
        True,
    ),
]


@pytest.mark.parametrize("label,block,live", AUTHOR_ROWS, ids=[r[0] for r in AUTHOR_ROWS])
def test_preserved_524_later_sibling_cases(label, block, live):
    source = (
        "def outer(x, flag, helper):\n    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n" + block
    )
    _assert_entry_contract(label, source, False, live)
    tree = ast.parse(source)
    function = tree.body[0]
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert support._is_enforced(function, query, tree) is live


@pytest.mark.parametrize(
    "iterable,store_reached",
    [
        ("()", False),
        ("(*(1,),)", True),
        ("range(0)", False),
    ],
)
def test_preserved_524_conservative_iterables(iterable, store_reached):
    """The starred literal does iterate; its completion stays syntactically declined.

    Empty literal and empty range raise UnboundLocalError; the expanded one-item
    literal writes a list and raises TypeError. Their existing analyzer decline
    is pinned separately from those observed runtime outcomes.
    """
    source = (
        "def outer(x, flag, helper):\n    import contextlib\n"
        f"    for _ in {iterable}:\n"
        "        *cs, = (contextlib.suppress(AssertionError),)\n"
        "    with cs:\n        assert x != 1\n"
    )
    tree = ast.parse(source)
    function = tree.body[0]
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    store = next(n for n in ast.walk(function) if isinstance(n, ast.Assign))
    namespace = {}
    filename = "<preserved-524>"
    exec(compile(source, filename, "exec"), namespace)  # noqa: S102
    visited = set()

    def trace(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == filename:
            visited.add(frame.f_lineno)
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        with pytest.raises((TypeError, UnboundLocalError, NameError)):
            namespace["outer"](1, True, None)
    finally:
        sys.settrace(previous)
    assert (store.lineno in visited) is store_reached
    assert query.lineno not in visited
    assert support._is_enforced(function, query, tree) is True


def test_continue_skips_the_store_and_retains_live_manager():
    source = (
        "import contextlib\ndef outer(x):\n    cs=contextlib.nullcontext()\n"
        "    for _ in (1,):\n        continue\n"
        "        *cs,=(contextlib.suppress(AssertionError),)\n"
        "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<524-continue-control>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[1]
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert support._is_enforced(function, query, tree) is True


@pytest.mark.parametrize(
    "before,after,outcome,live",
    [
        ("if x == 99: break", "", TypeError, True),
        ("", "if x == 99: break", TypeError, True),
        ("break", "", UnboundLocalError, True),
        ("continue", "", UnboundLocalError, True),
        ("return 0", "", None, True),
        ("raise ValueError", "", ValueError, False),
    ],
)
def test_preserved_524_control_flow_cases(before, after, outcome, live):
    """Pin runtime truth separately from existing conservative path declines.

    The conditional-break rows raise TypeError for x=1. They remain declined by
    the reviewed implementation. A preceding return returns 0; a preceding
    raise never reaches the header and correctly has verdict False.
    """
    source = (
        "def outer(x, flag, helper):\n    import contextlib\n"
        "    for _ in (1,):\n"
        + (f"        {before}\n" if before else "")
        + "        *cs, = (contextlib.suppress(AssertionError),)\n"
        + (f"        {after}\n" if after else "")
        + "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<524-control-flow>", "exec"), namespace)  # noqa: S102
    if outcome is None:
        assert namespace["outer"](1, True, None) == 0
    else:
        with pytest.raises(outcome):
            namespace["outer"](1, True, None)
    tree = ast.parse(source)
    function = tree.body[0]
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert support._is_enforced(function, query, tree) is live


def test_conditional_break_retains_a_prior_live_manager():
    source = (
        "import contextlib\ndef outer(x, flag):\n"
        "    cs = contextlib.nullcontext()\n    for _ in (1,):\n"
        "        if flag:\n            break\n"
        "        *cs, = (contextlib.suppress(AssertionError),)\n"
        "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<524-break-control>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1, True)
    tree = ast.parse(source)
    function = tree.body[1]
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert support._is_enforced(function, query, tree) is True
