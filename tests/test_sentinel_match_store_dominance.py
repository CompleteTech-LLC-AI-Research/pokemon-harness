"""Execution-first controls for match capture retirement (#399)."""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

ROWS = (
    ("unconditional store", "cs = contextlib.nullcontext()\nwith cs:\n    assert x != 1\n", True),
    (
        "unrun nullcontext store",
        "if False:\n    cs = contextlib.nullcontext()\nwith cs:\n    assert x != 1\n",
        False,
    ),
    (
        "unrun unrelated exception store",
        "if False:\n    cs = contextlib.suppress(ValueError)\nwith cs:\n    assert x != 1\n",
        False,
    ),
    (
        "store after query",
        "with cs:\n    assert x != 1\ncs = contextlib.nullcontext()\n",
        False,
    ),
    (
        "store and query in selected arm",
        "if x == 1:\n    cs = contextlib.nullcontext()\n    with cs:\n        assert x != 1\n",
        True,
    ),
    (
        "store in unentered handler",
        "try:\n    pass\nexcept ValueError:\n    cs = contextlib.nullcontext()\nwith cs:\n    assert x != 1\n",
        False,
    ),
    (
        "store in uncalled function",
        "def later():\n    nonlocal cs\n    cs = contextlib.nullcontext()\nwith cs:\n    assert x != 1\n",
        False,
    ),
)


@pytest.mark.parametrize("label, body, expected", ROWS, ids=[row[0] for row in ROWS])
def test_only_a_store_that_precedes_the_header_on_its_path_retires_capture(label, body, expected):
    source = (
        "import contextlib\ndef outer(x):\n"
        "    match [contextlib.suppress(AssertionError)]:\n"
        "        case [cs]:\n" + "".join("            " + line for line in body.splitlines(True))
    )
    namespace = {}
    exec(compile(source, "<399-dominance-runtime>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1)
    except AssertionError:
        actual = True
    else:
        actual = False
    assert actual is expected, label
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is actual, label


PARAMETER_ROWS = (
    (
        "controllable parameter",
        "",
        "if flag:\n    cs = contextlib.nullcontext()\nwith cs:\n    assert x != 1\n",
        "AssertionError",
        True,
    ),
    (
        "flag overwritten before conditional",
        "",
        "flag = False\nif flag:\n    cs = contextlib.nullcontext()\nwith cs:\n    assert x != 1\n",
        "TypeError",
        False,
    ),
    (
        "assert parameter overwritten before capture",
        "    x = 0\n",
        "if flag:\n    cs = contextlib.nullcontext()\nwith cs:\n    assert x != 1\n",
        "returned",
        False,
    ),
    (
        "assert message raises",
        "",
        "if flag:\n    cs = contextlib.nullcontext()\nwith cs:\n    assert x != 1, missing\n",
        "NameError",
        False,
    ),
    (
        "selected store raises",
        "",
        "if flag:\n    cs = contextlib.nullcontext(1 / 0)\nwith cs:\n    assert x != 1\n",
        "ZeroDivisionError",
        False,
    ),
    (
        "selected arm raises after store",
        "",
        "if flag:\n    cs = contextlib.nullcontext()\n    raise ValueError\nwith cs:\n    assert x != 1\n",
        "ValueError",
        False,
    ),
)


@pytest.mark.parametrize(
    "label, prefix, body, outcome, expected", PARAMETER_ROWS, ids=[row[0] for row in PARAMETER_ROWS]
)
def test_conditional_retirement_has_an_actual_parameter_failure_path(
    label, prefix, body, outcome, expected
):
    source = (
        "def outer(x, flag):\n    import contextlib\n"
        + prefix
        + "    match [1]:\n        case [cs]:\n"
        + "".join("            " + line for line in body.splitlines(True))
    )
    namespace = {}
    exec(compile(source, "<399-parameter-runtime>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1, True)
    except Exception as exc:  # noqa: BLE001
        actual = type(exc).__name__
    else:
        actual = "returned"
    assert actual == outcome, label
    tree = ast.parse(source)
    function = tree.body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is expected, label


def test_untyped_parameter_has_a_truthy_failure_witness_even_for_false_literal():
    source = (
        "def outer(flag):\n    import contextlib\n"
        "    match [1]:\n        case [cs]:\n"
        "            if flag:\n                cs = contextlib.nullcontext()\n"
        "            with cs:\n                assert flag != False\n"
    )

    class TruthyEqualFalse:
        def __bool__(self):
            return True

        def __eq__(self, other):
            return other is False

    namespace = {}
    exec(compile(source, "<399-truthy-failure-runtime>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](TruthyEqualFalse())
    tree = ast.parse(source)
    function = tree.body[0]
    query = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, query, tree) is True
