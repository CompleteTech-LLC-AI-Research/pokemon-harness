"""Shared imports and constants for the sentinel support package.

Fragment 0 of 6, split from _timed_menu_milestone_sentinel_support.py by #122.
This one holds the imports and the module-level constants; the five parts that
follow hold the functions. None of the six is importable on its own --
tests/_timed_menu_milestone_sentinel_support.py execs them, in order, into one
shared dict and is the only entry point.
"""

# `ast` is re-exported: callers reach `support.ast`. `inspect` and `milestones`
# are used by the function fragments that are exec'd after this one into the
# same shared dict, so they are imports-for-others rather than dead code here.
import ast
import inspect

import tests.test_timed_menu_milestones as milestones

# These three are this module's contribution to the shared namespace, not code it
# runs itself: `ast` and `inspect` back `_module_tree()`, and `milestones` is the
# module those helpers parse. The `assert` is not decoration -- without a use in
# this file, `ruff check --fix` deletes exactly these imports, and the breakage
# only surfaces at exec time as a NameError raised from inside an analyzer.
assert ast and inspect and milestones

SUPPRESSING_CONTEXTS = ("contextlib.suppress", "asyncio.suppress")
SUPPRESSOR_SPELLINGS = frozenset(dotted.rsplit(".", 1)[-1] for dotted in SUPPRESSING_CONTEXTS)
AMBIGUOUS_SUPPRESSOR = object()
_NOT_A_SUPPRESSOR = object()
LOUD_DUNDER = object()
_ALIAS_CHAIN_LIMIT = 32
ASSERTION_CAPTURING_CONTEXTS = ("pytest.raises",)
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
PINNED_COUNT_COMPARISONS = (
    ("_authored_cartridge_check", "==", 1),
    ("bounded_child", "<=", 32768),
    ("test_authored_cartridge_actual_helper_is_non_mutating", "==", 1),
    ("test_cap_failure_preserves_unspooled_call_and_incomplete_artifact", "<=", 1200),
    ("test_counter_saturation_fails_explicitly_instead_of_silently_losing_hits", "==", 1),
    ("test_default_retention_keeps_full_calls_and_installs_no_hooks", "==", 300),
    ("test_disabled_touches_no_dependencies_and_still_checks_owner", "==", 1),
    ("test_late_noncompleted_calls_have_exact_counts_and_full_evidence", "==", 271),
    ("test_overflow_is_sticky_counts_continue_without_context_access", "==", 1),
    (
        "test_stream_over_240_calls_keeps_every_record_hash_and_milestone_context",
        "==",
        300,
    ),
    ("test_unknown_actual_progress_is_counted_and_retained_as_interruption", "==", 271),
)
RETENTION_COUNT_SITES = {
    "test_stream_over_240_calls_keeps_every_record_hash_and_milestone_context": (300, "=="),
    "test_default_retention_keeps_full_calls_and_installs_no_hooks": (300, "=="),
    "test_late_noncompleted_calls_have_exact_counts_and_full_evidence": (271, "=="),
    "test_unknown_actual_progress_is_counted_and_retained_as_interruption": (271, "=="),
}
RETENTION_SUBSCRIPT_COUNT_SITES = {
    "test_late_noncompleted_calls_have_exact_counts_and_full_evidence": (
        (("call_log", "record_count"), 271, "=="),
        (("call_counts", "requested_frames"), 271, "=="),
        (("call_counts", "total"), 271, "=="),
    ),
}
GUARD_MODULE = "tests._timed_menu_frame_bound_support"
GUARD_FUNCTION = "assert_not_deadline_truncated"
DEADLINE_TERMINATION = "cancelled_or_deadline"
_MILESTONES_TREE = None
_EXIT_EXCEPTION_PARAMS = frozenset({"exc", "exc_type", "et", "e", "err", "exc_info"})
NON_CONTEXT_MANAGER_TYPES = frozenset(
    {
        "NoneType",
        "bool",
        "int",
        "float",
        "complex",
        "str",
        "bytes",
        "list",
        "tuple",
        "set",
        "dict",
        # #359. A name bound by a string field of a node that is not a target
        # at all holds an object that cannot be entered: a module, a class or
        # a function. Entering one raises `TypeError` *before* the assert under
        # the `with` is evaluated, so the assert is defeated -- and the
        # analyzer, with no store entry to read, was certifying it as
        # load-bearing. The damaging direction.
        "module",
        "type",
        "function",
    }
)
_DECIDING_OPERANDS = (
    ast.Name,
    ast.Attribute,
    ast.Call,
    ast.Subscript,
    ast.Constant,
    ast.List,
    ast.Dict,
    ast.Set,
    ast.Tuple,
    ast.JoinedStr,
    ast.Await,
)
_NOT_LITERAL = object()
_LITERAL_OPERATORS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
    ast.BitOr: lambda a, b: a | b,
    ast.BitAnd: lambda a, b: a & b,
    ast.BitXor: lambda a, b: a ^ b,
    ast.LShift: lambda a, b: a << b,
    ast.RShift: lambda a, b: a >> b,
}
_LITERAL_COMPARISONS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b,
}
_CONTROL_TRANSFERS = (ast.Return, ast.Raise, ast.Break, ast.Continue)
_BLOCK_FIELDS = frozenset({"body", "orelse", "finalbody"})
GUARDED_CLOCK_STEP = 0.0
RUN_OWNER = "run_owner"
