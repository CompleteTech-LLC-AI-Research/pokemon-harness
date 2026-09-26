"""Structural sentinels for the #261 retention contracts (#270).

The guard and the exact counts these sentinels protect are *unguarded* in the
behavioural sense, and that is the whole point of this module.  A behavioural
test cannot detect their absence: the owner run is driven by an injected
``FakeClock``, so a run that stops on the frame bound looks identical whether or
not ``assert_not_deadline_truncated`` was called.  Deleting the guard, or
relaxing ``== 300`` to ``>= 1``, both leave the full suite green.

So these checks read the source of ``test_timed_menu_milestones.py`` instead of
its results.  They are deliberately narrow: each one pins a specific contract
that #261 established, and each is paired with a mutation that must turn it red
before it is trusted.

Kept in its own module so that it is not deleted along with the file it guards,
and registered in ``tests/_tier_config.py`` so the production gate can reach it.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MILESTONES_SOURCE = TESTS_DIR / "test_timed_menu_milestones.py"
SUPPORT_SOURCE = TESTS_DIR / "_timed_menu_frame_bound_support.py"

#: (enclosing test function, exact ``len(...)`` count it must assert with ``==``).
#: These are the four sites #270 names.  Pinned by function name rather than by
#: line number so that unrelated edits above them do not make the sentinel lie.
PROTECTED_EXACT_COUNTS = (
    ("test_stream_over_240_calls_keeps_every_record_hash_and_milestone_context", 300),
    ("test_default_retention_keeps_full_calls_and_installs_no_hooks", 300),
    ("test_late_noncompleted_calls_have_exact_counts_and_full_evidence", 271),
    ("test_unknown_actual_progress_is_counted_and_retained_as_interruption", 271),
)

GUARD_FN = "run_owner"
GUARD_CALL = "assert_not_deadline_truncated"
DEADLINE_TERMINATION = "cancelled_or_deadline"


def _parse(path: Path) -> ast.Module:
    if not path.is_file():
        raise AssertionError(f"guarded source is missing: {path}")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(module: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} is missing")


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _single_comparison(node: ast.AST, op: type) -> ast.Compare | None:
    """Return the Compare if ``node`` is a single-operator comparison using ``op``."""
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    return node if isinstance(node.ops[0], op) else None


def _is_termination_key(node: ast.AST) -> bool:
    """True for the ``record["termination"]`` subscript."""
    return (
        isinstance(node, ast.Subscript)
        and _is_name(node.value, "record")
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "termination"
    )


def _constant(node: ast.AST) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _is_len_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and _is_name(node.func, "len")
        and len(node.args) == 1
        and not node.keywords
    )


def _calls(node: ast.AST, name: str) -> bool:
    return any(isinstance(sub, ast.Call) and _is_name(sub.func, name) for sub in ast.walk(node))


def test_deadline_truncation_guard_is_wired_on_the_zero_clock_step_path():
    """The #261 guard must be *called* on the ``clock_step == 0.0`` path.

    Mutation that must fail this: ``if clock_step == 0.0:`` -> ``if False:``,
    or deleting the ``assert_not_deadline_truncated(record)`` call.
    """
    module = _parse(MILESTONES_SOURCE)
    helper = _function(module, GUARD_FN)

    guarded = [
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.If)
        and (comparison := _single_comparison(node.test, ast.Eq)) is not None
        and _is_name(comparison.left, "clock_step")
        and _constant(comparison.comparators[0]) == 0.0
    ]
    assert guarded, (
        f"{GUARD_FN}() no longer branches on `clock_step == 0.0`; the #261 "
        "terminal-state precondition is no longer applied to the frame-bound runs"
    )

    # The call must live in the *body* of that branch. Searching the whole
    # ``if`` would also match a call nested in the condition, which is exactly
    # the "present but never reached" shape this sentinel exists to reject.
    assert any(_calls(statement, GUARD_CALL) for node in guarded for statement in node.body), (
        f"{GUARD_FN}() branches on `clock_step == 0.0` but never calls "
        f"{GUARD_CALL}() inside that branch"
    )


def test_deadline_truncation_guard_still_rejects_the_deadline_terminal_state():
    """The guard must still have teeth, not merely be called.

    Mutation that must fail this: replacing the guard body with ``pass``, or
    flipping ``!=`` to ``==``.
    """
    module = _parse(SUPPORT_SOURCE)
    guard = _function(module, GUARD_CALL)

    asserts = [node for node in ast.walk(guard) if isinstance(node, ast.Assert)]
    assert asserts, f"{GUARD_CALL}() contains no assertion and cannot reject anything"
    assert any(
        (comparison := _single_comparison(node.test, ast.NotEq)) is not None
        and _is_termination_key(comparison.left)
        and _constant(comparison.comparators[0]) == DEADLINE_TERMINATION
        for node in asserts
    ), (
        f"{GUARD_CALL}() no longer rejects termination == {DEADLINE_TERMINATION!r}; "
        "a deadline-truncated run would again read as a retention failure (#261)"
    )


def test_exact_retention_counts_are_pinned_with_equality():
    """The 300/271 contracts must be ``==``, not a range or truthiness check.

    Mutation that must fail this: ``assert len(calls) == 300`` ->
    ``assert len(calls) >= 1``.
    """
    module = _parse(MILESTONES_SOURCE)
    for function_name, expected in PROTECTED_EXACT_COUNTS:
        function = _function(module, function_name)
        exact = [
            node
            for asserted in (node for node in ast.walk(function) if isinstance(node, ast.Assert))
            for node in ast.walk(asserted.test)
            if (comparison := _single_comparison(node, ast.Eq)) is not None
            and _is_len_call(comparison.left)
            and _constant(comparison.comparators[0]) == expected
        ]
        assert exact, (
            f"{function_name}() no longer asserts an exact count of {expected} "
            "with ==; a dropped call would no longer be distinguished from a "
            "deadline-truncated run"
        )


def test_exact_count_assertions_run_on_the_guarded_default_clock_path():
    """#270 asks that every protected count site carry the #261 precondition.

    All four sites obtain it centrally: the shared ``run_owner`` helper applies
    the terminal-state guard whenever ``clock_step`` is the default ``0.0``.  A
    site that opted out by passing its own ``clock_step`` would silently lose
    the precondition, so that is what this pins.

    Mutation that must fail this: adding ``clock_step=1.0`` to any of the four
    ``run_owner(...)`` calls.
    """
    module = _parse(MILESTONES_SOURCE)
    for function_name, _ in PROTECTED_EXACT_COUNTS:
        function = _function(module, function_name)
        calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and _is_name(node.func, GUARD_FN)
        ]
        assert calls, f"{function_name}() no longer drives the owner through {GUARD_FN}()"
        for call in calls:
            overrides = [
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "clock_step"
                and not (isinstance(keyword.value, ast.Constant) and keyword.value.value == 0.0)
            ]
            assert not overrides, (
                f"{function_name}() calls {GUARD_FN}() with clock_step="
                f"{ast.unparse(overrides[0])}, which bypasses the #261 "
                "terminal-state guard that makes an exact count meaningful"
            )
