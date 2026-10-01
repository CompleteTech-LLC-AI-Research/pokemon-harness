"""Execution-backed pins for already-correct binding epoch and rebind cases."""

import ast
import sys

import pytest

from tests import _timed_menu_milestone_sentinel_support as support


@pytest.mark.parametrize(
    "later,live",
    [
        ("", False),
        ("cs = contextlib.nullcontext()", True),
        ("cs = contextlib.suppress(AssertionError)", False),
    ],
)
def test_later_loop_store_supersedes_starred_capture(later, live):
    source = (
        "import contextlib\ndef outer(x):\n    for _ in (1,):\n"
        "        *cs, = (contextlib.suppress(AssertionError),)\n"
        + (f"        {later}\n" if later else "")
        + "    with cs:\n        assert x != 1\n"
    )
    tree = ast.parse(source)
    function = tree.body[1]
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    namespace = {}
    filename = "<later-body-store>"
    exec(compile(source, filename, "exec"), namespace)  # noqa: S102
    visits = set()

    def trace(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == filename:
            visits.add(frame.f_lineno)
        return trace

    old = sys.gettrace()
    sys.settrace(trace)
    try:
        if live:
            with pytest.raises(AssertionError):
                namespace["outer"](1)
        elif later:
            namespace["outer"](1)
        else:
            with pytest.raises(TypeError):
                namespace["outer"](1)
    finally:
        sys.settrace(old)
    assert (query.lineno in visits) is bool(later)
    assert support._is_enforced(function, query, tree) is live


def test_unreadable_helper_iterable_reaches_the_assert():
    from tests.test_timed_menu_milestone_sentinels import (
        BINDING_FORM_SHAPES,
        test_a_binding_form_reads_the_value_that_lands_on_the_name,
    )

    row = next(r for r in BINDING_FORM_SHAPES if "unreadable iterable" in r[0])
    visited = []

    def trace(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == f"<{row[0]}>":
            visited.append(frame.f_lineno)
        return trace

    old = sys.gettrace()
    sys.settrace(trace)
    try:
        test_a_binding_form_reads_the_value_that_lands_on_the_name(*row)
    finally:
        sys.settrace(old)
    source = "def outer(x, helper):\n    import contextlib\n" + row[1] + "\n"
    query = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Assert))
    assert query.lineno in visited
