"""Separate the redundant walk restriction from callee provenance (#463)."""

import ast
import contextlib
import inspect
import sys
import types

import pytest

from tests import _timed_menu_milestone_sentinel_support as support


def _without_walk_module_restriction():
    tree = ast.parse(inspect.getsource(support._literal_match_reaches_header))
    removed = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp):
            retained = []
            for value in node.values:
                if (
                    isinstance(value, ast.Compare)
                    and isinstance(value.left, ast.Attribute)
                    and value.left.attr == "module"
                    and len(value.comparators) == 1
                    and isinstance(value.comparators[0], ast.Constant)
                    and value.comparators[0].value == "contextlib"
                ):
                    removed += 1
                else:
                    retained.append(value)
            node.values = retained
    assert removed == 1, "mutate only the redundant pre-match walk condition"
    namespace = support.__dict__.copy()
    exec(compile(ast.fix_missing_locations(tree), "<463-walk-mutation>", "exec"), namespace)  # noqa: S102
    return namespace["_literal_match_reaches_header"]


@pytest.mark.parametrize("mutated", (False, True))
@pytest.mark.parametrize("module, expected", (("contextlib", True), ("other_context", False)))
def test_walk_module_restriction_is_redundant_with_callee_provenance(
    monkeypatch, mutated, module, expected
):
    other = types.ModuleType("other_context")
    other.nullcontext = contextlib.nullcontext
    monkeypatch.setitem(sys.modules, "other_context", other)
    source = (
        "import contextlib\n"
        "def probe(x):\n"
        f"    from {module} import nullcontext\n"
        "    cs = contextlib.suppress(AssertionError)\n"
        "    match [nullcontext()]:\n"
        "        case [cs]:\n"
        "            pass\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<463-runtime>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    if mutated:
        monkeypatch.setattr(
            support, "_literal_match_reaches_header", _without_walk_module_restriction()
        )
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    # Both sources execute a live assert. Only the canonical import is within
    # this proof's provenance budget; removing the walk guard cannot expand it.
    assert support._is_enforced(function, target, tree) is expected


def test_callee_module_restriction_remains_authoritative():
    canonical = ast.parse("from contextlib import nullcontext").body[0]
    other = ast.parse("from other_context import nullcontext").body[0]
    assert support._binding_is_the_callee_import(canonical, "nullcontext") is True
    assert support._binding_is_the_callee_import(other, "nullcontext") is False


# --- #604: literal and/or folding returns Python's operand value AND type ------


def _fold(expression):
    return support._literal_value(ast.parse(expression, mode="eval").body)


def _python(expression):
    return eval(expression, {"__builtins__": {}})  # noqa: S307 - fixed literal-only test oracle


@pytest.mark.parametrize(
    "expression",
    (
        "2 or 0", "0 and 3", "[] or [1]", "[1] and []", "(2 or 0) == 1", "(2 and 3) == 3",
        "(0 or 2) == 2", "None or 'x'", "'' and 5", "0.0 or None", "1 and 2 and 3",
        "1 and 0 and 3", "0 or '' or []", "0 or '' or [7]", "(1, 2) and {3: 4}", "{} or {5}",
        "not (0 or 2)", "(0 or 2) + 1", "1 and (0 or 2)", "(1 and 0) or 7", "3 or 4 or 5",
        "(1 == 1) and 'ok'", "b'' or b'z'", "0 and 0.5", "1.5 or 0",
        # decisive operand first: the unreached operand is never evaluated by Python either
        "0 and missing", "2 or missing", "[] and call()", "'x' or other.attr", "0 and (1 / 0)",
        "1 or items[0]", "(0 or 2) or missing",
    ),
)
def test_literal_boolop_matches_independent_python_value_and_type(expression):
    expected = _python(expression)
    folded = _fold(expression)
    assert type(folded) is type(expected)
    assert folded == expected


@pytest.mark.parametrize(
    "expression",
    (
        "missing and 1", "missing or 1", "1 and missing", "0 or missing", "[] or call()",
        "1 and other.attr", "x[0] or 2", "(1 and 1) and missing", "0 or (1 / 0)",
        "1 and (1 / 0)", "1 and 'a' + 1", "(missing or 1) and 2",
    ),
)
def test_boolop_with_an_unresolved_evaluated_operand_stays_conservative(expression):
    assert _fold(expression) is support._NOT_LITERAL


def test_boolop_fold_never_executes_runtime_code():
    calls = []

    def probe():
        calls.append(1)
        return 1

    assert _fold("probe() or 1") is support._NOT_LITERAL
    assert calls == []
    tree = ast.parse(inspect.getsource(support._literal_value))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"eval", "exec", "compile", "__import__"}
