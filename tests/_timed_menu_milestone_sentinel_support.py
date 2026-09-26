"""Structural sentinels that keep #261/#270 assertions load-bearing (#270).

``tests/test_timed_menu_milestones.py`` carries the frame-bound retention
contract, but two of its assertions can be deleted or relaxed without any
behavioural test noticing:

* ``assert_not_deadline_truncated(record)`` is the #261 guard. It is *correct*
  and it does fire when the wall clock wins, but nothing checks that the call
  is still wired into ``run_owner``. Rewriting ``if clock_step == 0.0:`` to
  ``if False:`` leaves the whole file green (see #270 Finding 1).
* Four sites assert an exact call count (``== 300`` / ``== 271``). Relaxing any
  of them to ``>=`` turns the retention contract into "at least one call"
  without failing (see #270 Finding 2).

Neither gap is observable from the *return value* of a passing run, so a
behavioural assertion cannot catch it -- the same reasoning that made #271 pin
the clock read by inspecting source. These helpers therefore parse the test
module's AST and assert on structure. They are the backstop for structure; the
behavioural assertions remain the load-bearing contract.
"""

import ast
import inspect

import tests._timed_menu_frame_bound_support as frame_bound_support
import tests.test_timed_menu_milestones as milestones

_OPERATORS = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
}


def _module_tree():
    """Return the parsed AST of the milestones test module."""
    return ast.parse(inspect.getsource(milestones))


def _frame_bound_support_tree():
    """Return the parsed AST of the frame-bound support module.

    The #261 guard itself lives there rather than in the milestones module, so
    the "does it still have teeth" check has to read that file's source.
    """
    return ast.parse(inspect.getsource(frame_bound_support))


def _find_function(tree, name):
    """Return the top-level function definition called ``name``."""
    for func in tree.body:
        if isinstance(func, ast.FunctionDef) and func.name == name:
            return func
    return None


def _is_zero_float_compare(node):
    """True for a comparison of a name against the float ``0.0``."""
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "clock_step"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value == 0.0
    )


def guard_is_wired_on_the_fast_clock_path():
    """Is ``assert_not_deadline_truncated(record)`` still called under
    ``if clock_step == 0.0:`` inside ``run_owner``?

    The guard is only meaningful on the fast-clock path: with ``clock_step=0.0``
    the fake clock never advances, so the run must reach the frame bound rather
    than the deadline. A guard that is present in the file but commented out, or
    nested under a condition that is never true, is not wired and this returns
    ``False``.
    """
    tree = _module_tree()
    for func in tree.body:
        if not (isinstance(func, ast.FunctionDef) and func.name == "run_owner"):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.If) or not _is_zero_float_compare(node.test):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "assert_not_deadline_truncated"
                ):
                    return True
    return False


def count_comparisons():
    """Yield ``(function_name, operator, literal)`` for every ``len(...)`` assert.

    Every comparison operator is recorded, not just ``==``. That is the whole
    point: a check that only *finds* equality assertions would silently pass
    when a site is relaxed to ``>=``, because the relaxed line would simply stop
    being found. Pinning the full observed set means relaxing, deleting, or
    adding a site all change the set and fail.
    """
    tree = _module_tree()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        # `assert len(calls) == 271 and calls[-1][...] == 270` is a BoolOp whose
        # operands are the individual comparisons, so compare nodes -- not just
        # the assert's own top-level test.
        for comparison in _comparisons_in(node.test):
            yield from _count_comparison(tree, node, comparison)


def _comparisons_in(expression):
    if isinstance(expression, ast.Compare):
        return [expression]
    if isinstance(expression, ast.BoolOp):
        found = []
        for value in expression.values:
            found.extend(_comparisons_in(value))
        return found
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return _comparisons_in(expression.operand)
    return []


def _count_comparison(tree, node, comparison):
    if len(comparison.ops) != 1:
        return
    op = _OPERATORS.get(type(comparison.ops[0]))
    if op is None:
        return
    left, right = comparison.left, comparison.comparators[0]
    if not (isinstance(left, ast.Call) and getattr(left.func, "id", None) == "len"):
        return
    if isinstance(right, ast.Constant) and isinstance(right.value, int):
        function = _enclosing_function(tree, node)
        yield (function, op, right.value)


def _enclosing_function(tree, target):
    for func in tree.body:
        if isinstance(func, ast.FunctionDef) and any(child is target for child in ast.walk(func)):
            return func.name
    return "<module>"


def observed_count_comparisons():
    """Sorted ``(function, operator, literal)`` triples actually present."""
    return sorted(count_comparisons())


# The four frame-bound retention counts #270 names. Keyed by the test function
# that owns them; the value is the operator the assertion must keep using.
# ``test_late_noncompleted_calls_*`` is parametrised and so covers the ``== 271``
# site once per status. The 300s are the inline- and stream-retention paths.
RETENTION_COUNT_SITES = {
    "test_stream_over_240_calls_keeps_every_record_hash_and_milestone_context": (300, "=="),
    "test_default_retention_keeps_full_calls_and_installs_no_hooks": (300, "=="),
    "test_late_noncompleted_calls_have_exact_counts_and_full_evidence": (271, "=="),
    "test_unknown_actual_progress_is_counted_and_retained_as_interruption": (271, "=="),
}


def retention_sites_observed():
    """The ``(function, operator, literal)`` triples for the pinned sites only."""
    return sorted(triple for triple in count_comparisons() if triple[0] in RETENTION_COUNT_SITES)


def _is_deadline_termination_compare(comparison):
    """True for ``record["termination"] != "cancelled_or_deadline"``."""
    left, right = comparison.left, comparison.comparators[0]
    return (
        isinstance(left, ast.Subscript)
        and isinstance(left.slice, ast.Constant)
        and left.slice.value == "termination"
        and isinstance(right, ast.Constant)
        and right.value == "cancelled_or_deadline"
    )


def guard_rejects_the_deadline_terminal_state():
    """Does the #261 guard still *reject* a truncated run, not merely get called?

    Being wired is not the same as having teeth. The guard earns its name from
    exactly one assertion, so replacing its body with ``pass`` leaves the whole
    behavioural suite green while the guard stops rejecting anything.
    ``guard_is_wired_on_the_fast_clock_path`` cannot see this: the call is
    still there either way.
    """
    tree = _frame_bound_support_tree()
    guard = _find_function(tree, "assert_not_deadline_truncated")
    if guard is None:
        return False
    return any(
        isinstance(node, ast.Assert)
        and any(
            len(comparison.ops) == 1
            and isinstance(comparison.ops[0], ast.NotEq)
            and _is_deadline_termination_compare(comparison)
            for comparison in _comparisons_in(node.test)
        )
        for node in ast.walk(guard)
    )


def overridden_clock_steps_in_protected_sites():
    """Pinned count sites that pass a ``clock_step`` other than the default ``0.0``.

    ``run_owner`` applies the terminal-state guard only on the default
    ``clock_step == 0.0`` path. A pinned site that supplies its own
    ``clock_step`` opts out of that precondition, so its exact count stops
    being evidence that the run reached the frame bound instead of the wall
    clock. ``retention_sites_observed`` cannot see this: the ``== 300``
    assertion is still present, it has just stopped meaning anything.
    """
    tree = _module_tree()
    offenders = []
    for function_name in sorted(RETENTION_COUNT_SITES):
        func = _find_function(tree, function_name)
        if func is None:
            offenders.append((function_name, "<function missing>"))
            continue
        for call in ast.walk(func):
            if not (isinstance(call, ast.Call) and getattr(call.func, "id", None) == "run_owner"):
                continue
            for keyword in call.keywords:
                if keyword.arg != "clock_step":
                    continue
                is_default = isinstance(keyword.value, ast.Constant) and keyword.value.value == 0.0
                if not is_default:
                    offenders.append((function_name, ast.unparse(keyword.value)))
    return sorted(offenders)
