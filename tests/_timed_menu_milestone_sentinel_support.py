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

The structural checks ask whether a pinned assertion is *enforced*, not merely
*present*. Presence alone is satisfiable by an assertion wrapped in
``try/except AssertionError: pass``, which leaves the node in the AST, leaves
the comparison in place, and leaves the owning test unable to fail (#280).
``_is_enforced`` is the shared answer to that question and is used by both the
teeth check and the count check.
"""

import ast
import inspect

import tests.test_timed_menu_milestones as milestones

#: Dotted paths whose call is a suppression context. Matched by *resolved*
#: name rather than by spelling, so the qualified, from-import and both alias
#: forms all collapse to the same value before the comparison. Matching one
#: concrete spelling and leaving the family open is what #288 did with
#: ``except*``.
SUPPRESSING_CONTEXTS = ("contextlib.suppress", "asyncio.suppress")

#: The *last component* of each suppressing path -- ``suppress`` for both
#: entries in ``SUPPRESSING_CONTEXTS``. This is the set of bare names that can
#: stand in for a suppressor whose binding the enclosing function cannot see
#: (see ``_unreadable_suppressor``).
#:
#: Taking every component of the dotted path would also admit ``contextlib``
#: and ``asyncio``. Those are module names, not suppressors -- ``with
#: contextlib:`` suppresses nothing -- so including them would report live
#: asserts as dead on any code that uses a module object as a context manager.
#: The suppressing callable is always the final component, so that component is
#: the only spelling that can stand in for one.
SUPPRESSOR_SPELLINGS = frozenset(dotted.rsplit(".", 1)[-1] for dotted in SUPPRESSING_CONTEXTS)

#: Sentinel recorded for a name bound to *more than one* readable suppressor, so
#: that which one applies depends on the runtime path taken.
#:
#: #308 criterion 1 requires such a name to be treated as unreadable and
#: reported as a defeat, rather than resolved by source order. It is a distinct
#: object rather than a boolean so that the ambiguity can be told apart from a
#: known alias when the exception names are inspected -- an ambiguous binding
#: suppresses *any* exception, because the rule cannot know which target was
#: applied.
AMBIGUOUS_SUPPRESSOR = object()

#: Sentinel recording that a name *is* bound at this position and deliberately
#: carries something that is not a suppressor.
#:
#: #367. `_assigned_suppressors` records a name only when it resolves to a
#: suppressor, so "carries a `nullcontext`" and "is not bound here" both fall
#: out as a missing key. Those are different answers: the second inherits the
#: previous position's binding, which resurrects a superseded suppressor. The
#: marker separates them.
_NOT_A_SUPPRESSOR = object()

#: Sentinel for a suppressor reached through ``name.__enter__()``. The dunder
#: is ``pass`` on every suppressor, so the entry raises ``TypeError`` before the
#: body runs whatever exception list it was built with. The argument is
#: therefore irrelevant and cannot be read off the binding, so the shape gets
#: its own marker instead of being classified from the suppressor's arguments.
LOUD_DUNDER = object()

#: How many links of alias chain :func:`_deref_alias` will follow before it
#: gives up and reports the value unreadable. A chain that resolves to a
#: suppressor is short in practice, so the bound is generous; it exists so a
#: cycle (``a = b; b = a``) terminates instead of recursing.
_ALIAS_CHAIN_LIMIT = 32

#: Dotted paths whose call turns a caught exception into a *pass*. This is a
#: different mechanism from ``SUPPRESSING_CONTEXTS`` and the distinction is
#: load-bearing, so the two sets stay separate rather than being merged:
#:
#: * ``suppress`` swallows the failure silently -- the test still passes and
#:   nothing is recorded.
#: * ``raises`` *asserts* the failure happened. ``with
#:   pytest.raises(AssertionError): assert 1 == 2`` therefore raises the
#:   AssertionError, ``pytest.raises`` catches it, finds the expected type, and
#:   the block ends normally. The test goes green on a failing assert, which is
#:   the same present-but-dead contract as the other shapes in this family.
#:
#: Measured on the pinned file, ``pytest.raises`` wraps asserts 5 times and
#: never once for ``AssertionError``: the arguments there are ``RuntimeError``,
#: ``BaseExceptionGroup``, and tuples of ``TypeError``/``ValueError``/
#: ``KeyError``/``RuntimeError``, none of which catch an ``assert``. Adding this
#: set therefore drops 0 of the 143 real asserts.
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


def _module_tree():
    """Return the parsed AST of the milestones test module."""
    return ast.parse(inspect.getsource(milestones))


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

    Asserts that cannot fail are excluded. ``retention_sites_observed()``
    otherwise reports a wrapped ``try/except AssertionError: pass`` as an intact
    ``== 300`` contract while the owning test has stopped being able to fail at
    all (#280, mutation M7), which is the worse half of that finding: the
    sentinel and the behaviour it protects go unenforced together.
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
        owner = _enclosing_function_node(tree, node)
        if owner is not None and (not _is_enforced(owner, node, tree) or _may_bypass(node.test)):
            return
        yield (_enclosing_function(tree, node), op, right.value)


def _enclosing_function(tree, target):
    for func in tree.body:
        if isinstance(func, ast.FunctionDef) and any(child is target for child in ast.walk(func)):
            return func.name
    return "<module>"


def _enclosing_function_node(tree, target):
    """The ``FunctionDef`` that owns ``target``, or ``None`` at module level."""
    for func in tree.body:
        if isinstance(func, ast.FunctionDef) and any(child is target for child in ast.walk(func)):
            return func
    return None


def observed_count_comparisons():
    """Sorted ``(function, operator, literal)`` triples actually present."""
    return sorted(count_comparisons())


#: Every ``len(...) == <int>`` exact-count assertion in the milestones module,
#: written out rather than recomputed.
#:
#: The four ``RETENTION_COUNT_SITES`` are hardcoded for the same reason, but
#: they only cover the #261 retention contract. ``observed_count_comparisons()``
#: is the *whole* set, and it was the wider set that went missing: when #294
#: began applying its bypass rule to ``and`` as well as ``or``, the live
#: contract ``assert len(errors) == 1 and isinstance(errors[0], RuntimeError)``
#: was reported as bypassed and silently dropped from this set -- 11 sites on
#: ``c896da7``, 10 on ``4415452``, with the whole sentinel file green.
#:
#: A backstop that recomputes its expectation from the same code it polices
#: cannot notice its own expectation shrinking, so this list is a literal. A
#: site that is deleted, relaxed, duplicated or neutralised changes the observed
#: set and fails.
#:
#: The literal was first written by dumping the observed set on the *broken*
#: tree, which reproduced the defect exactly: the list then held 10 entries and
#: deleting a pinned site kept the suite green. It is transcribed from the
#: pre-#294 baseline ``c896da7`` instead, and the ``== 1`` site from
#: ``test_disabled_touches_no_dependencies_and_still_checks_owner`` is the one
#: that had gone missing.
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


# --- #270 Finding 2, remaining half: the *subscript* 271 counts ---
#
# ``RETENTION_COUNT_SITES`` is enforced by ``count_comparisons()``, which only
# yields comparisons whose left side is a ``len(...)`` call. The same test also
# pins its counts through record subscripts, which that mechanism never sees:
#
#     assert record["call_log"]["record_count"] == 271
#     assert record["call_counts"]["requested_frames"] == 271
#     assert record["call_counts"]["total"] == 271
#
# Relaxing any of those to ``>= 1`` leaves all four retention sentinels green,
# so the "at least one call was retained" contract could be restored while every
# backstop reported the site intact. These are recorded as (path, operator,
# literal) triples and compared as a set, for the same reason the ``len(...)``
# sites are: relaxing, deleting, duplicating or neutralising a site all change
# the observed set.

#: The record subscripts whose value is a retained-call count, keyed by the test
#: function that owns them. Each entry is ``(key path, literal, operator)``.
RETENTION_SUBSCRIPT_COUNT_SITES = {
    "test_late_noncompleted_calls_have_exact_counts_and_full_evidence": (
        (("call_log", "record_count"), 271, "=="),
        (("call_counts", "requested_frames"), 271, "=="),
        (("call_counts", "total"), 271, "=="),
    ),
}


def _subscript_path(node):
    """Return ``(base_name, key_path)`` for a chained subscript, or ``None``.

    ``record["call_log"]["record_count"]`` is two nested ``Subscript`` nodes over
    a ``Name``; every slice must be a ``Constant`` string for the key path to be
    meaningful. The base ``Name`` is returned separately so callers can require
    the comparison to be rooted at ``record`` specifically -- ``state["record_
    count"]`` elsewhere in this module must not be mistaken for the same site.
    """
    keys = []
    current = node
    while isinstance(current, ast.Subscript):
        if not isinstance(current.slice, ast.Constant) or not isinstance(current.slice.value, str):
            return None
        keys.append(current.slice.value)
        current = current.value
    if not isinstance(current, ast.Name) or not keys:
        return None
    return (current.id, tuple(reversed(keys)))


def subscript_count_comparisons():
    """Yield ``(function_name, path, operator, literal)`` for pinned record subscripts.

    The counterpart to :func:`count_comparisons` for ``record[...]`` counts.
    Every operator is recorded, not just ``==``, so a site relaxed to ``>=``
    changes the observed set instead of merely ceasing to be found. Asserts that
    cannot fail are excluded via the shared ``_is_enforced``, the same rule
    #280 introduced for the ``len(...)`` sites.
    """
    tree = _module_tree()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for comparison in _comparisons_in(node.test):
            if len(comparison.ops) != 1:
                continue
            op = _OPERATORS.get(type(comparison.ops[0]))
            if op is None:
                continue
            resolved = _subscript_path(comparison.left)
            if resolved is None:
                continue
            base, path = resolved
            if base != "record":
                continue
            right = comparison.comparators[0]
            if not (isinstance(right, ast.Constant) and isinstance(right.value, int)):
                continue
            owner = _enclosing_function_node(tree, node)
            if owner is not None and (
                not _is_enforced(owner, node, tree) or _may_bypass(node.test)
            ):
                continue
            yield (_enclosing_function(tree, node), path, op, right.value)


def retention_subscript_sites_observed():
    """Sorted ``(function, path, operator, literal)`` for the pinned subscript sites only."""
    expected_paths = {
        function: {path for path, _, _ in sites}
        for function, sites in RETENTION_SUBSCRIPT_COUNT_SITES.items()
    }
    return sorted(
        quad for quad in subscript_count_comparisons() if quad[1] in expected_paths.get(quad[0], ())
    )


def retention_sites_observed():
    """The ``(function, operator, literal)`` triples for the pinned sites only."""
    return sorted(triple for triple in count_comparisons() if triple[0] in RETENTION_COUNT_SITES)


# --- #270 criterion 1, second half: the guard must have teeth, not just be called ---

#: The module that defines the #261 guard, and the terminal state it exists to reject.
GUARD_MODULE = "tests._timed_menu_frame_bound_support"
GUARD_FUNCTION = "assert_not_deadline_truncated"
DEADLINE_TERMINATION = "cancelled_or_deadline"


def _guard_source_tree():
    """Return the parsed AST of the module that defines the #261 guard."""
    import importlib

    return ast.parse(inspect.getsource(importlib.import_module(GUARD_MODULE)))


def _is_termination_key(node):
    """True for the ``record["termination"]`` subscript."""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "record"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "termination"
    )


def _swallows_assertion_error(handler):
    """True for a handler that can swallow the failure of an ``assert``.

    ``except AssertionError`` is the direct case, and a bare ``except:`` or
    ``except BaseException:`` swallows it too. ``except Exception`` is counted
    for the same reason: ``AssertionError`` derives from ``Exception``, so a
    handler naming that class also swallows the failure. It is named explicitly
    to keep the rule from depending on the reader knowing the exception
    hierarchy. Handlers for anything else -- ``except ValueError`` and friends
    -- cannot swallow an ``assert`` and are not counted.

    A re-raising handler (``except AssertionError: raise``) is conservatively
    counted as swallowing. The failure does propagate, so the assert is in fact
    enforced; deciding that needs real control-flow analysis, and this is a
    structural sentinel, so it asks only whether a handler *can* swallow. The
    error is toward reporting a live assert as unenforced, never toward missing
    a dead one.
    """
    caught = handler.type
    if caught is None:
        return True
    names = []
    for node in ast.walk(caught):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Tuple):
            names.extend(element.id for element in node.elts if isinstance(element, ast.Name))
    return bool({"AssertionError", "Exception", "BaseException"} & set(names))


_MILESTONES_TREE = None

#: ``id(function) -> module tree or None``. Memoises the module a function was
#: defined in, which :func:`_module_binds_name` needs per condition and which
#: costs a full tree walk to answer. Keyed by node identity because the AST is
#: built once per parsed file and outlives the call.
_MODULE_FOR_FUNCTION = {}

#: Statements that open a *block* without opening a new name scope. A binding
#: inside one of these still runs in the module scope, so the module walk has
#: to descend through them; every other child is either a binding itself or the
#: body of a nested scope, and neither is descended into.
_MODULE_LEVEL_BLOCKS = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Match,
)


def _block_body(statement):
    """The statements a block statement runs, in source order.

    ``Try`` and ``Match`` run several bodies, so every one of them is included;
    a binding in any of them is a module binding on the path that runs. A
    ``match`` clause is flattened to its statements rather than returned as a
    ``Case`` node, because ``Case`` is not one of the block types this walk
    knows how to open -- a ``def`` inside a case is still a module binding on
    the path that matches, and reading past it reported a header DEAD that
    CPython enters.

    An ``except`` handler is a body too, and it is a body that has plainly run
    by the time anything below the ``try`` is evaluated:

        try:
            raise ValueError
        except ValueError:
            def list(): return nullcontext()
        cs = list()          # the handler's `list`, not the builtin

    Leaving handler bodies out of the walk skipped the binding entirely and
    reported that ``cs`` as the builtin ``list`` -- a ``TypeError``-raising
    header as LIVE. A handler's ``name`` (``except E as exc``) is a binding too,
    but that one is *deleted* when the handler exits, so the handler body is
    what is walked and the bound name is read from it like any other.
    """
    if isinstance(statement, ast.Try):
        return [
            *statement.body,
            *statement.orelse,
            *statement.finalbody,
            *(nested for handler in statement.handlers for nested in handler.body),
        ]
    if isinstance(statement, ast.Match):
        return [nested for case in statement.cases for nested in case.body]
    if isinstance(statement, (ast.If, ast.While)):
        return [*statement.body, *statement.orelse]
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return [*statement.body, *statement.orelse]
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return list(statement.body)
    return []


def milestones_tree():
    """The parsed milestones module, cached for repeated name resolution."""
    global _MILESTONES_TREE
    if _MILESTONES_TREE is None:
        _MILESTONES_TREE = _module_tree()
    return _MILESTONES_TREE


def _owning_module(function):
    """The parsed module that ``function`` belongs to, for import resolution.

    The bound names must come from the module that actually *contains* the assert
    being judged, not from whichever module the sentinel happens to be inspecting.
    Reading them from the milestones tree alone would make the rule correct for
    that one file and silently wrong for any other, which is how a spelling slips
    through: the aliases that matter are the ones in the file being edited.

    A function from a tree the sentinel has not seen -- a probe built by a test --
    falls back to the milestones module, which is correct for every real call site
    because those asserts live in that file. Probes that need a different set of
    bindings pass their own tree via ``_is_enforced(..., tree=...)``.
    """
    for tree in (milestones_tree(), _module_tree()):
        if any(node is function for node in ast.walk(tree)):
            return tree
    return _module_tree()


def _bound_names(tree, function=None):
    """Map every name bound by an import to the dotted path it binds to.

    ``from contextlib import suppress as sq`` binds ``sq`` to
    ``contextlib.suppress``; ``import contextlib as c`` binds ``c`` to
    ``contextlib``.

    ``function``, when given, contributes its *own* imports as well. A binding
    is only in scope where it appears, so an alias introduced inside the
    function that holds the assert must be resolved from that function -- and
    for correctness it must win over a module-level binding of the same name,
    because the inner ``import`` shadows the outer one. Reading only
    ``tree.body`` left every function-local alias unresolved, which reported a
    disarmed assert as enforced; that gap is measured, not theoretical.
    """
    bound = {}
    nodes = list(getattr(tree, "body", []))
    if function is not None:
        nodes.extend(_own_imports(function))
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def _own_imports(function):
    """Imports belonging to ``function``'s own scope, excluding nested scopes.

    An import inside a nested ``def`` binds a name in *that* scope, so
    attributing it to the enclosing function would let a nested helper's alias
    decide whether the outer function's assert counts as suppressed -- a false
    "unenforced" verdict, which is the damaging direction.
    """
    found = []
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return found


def _resolved_dotted(expression, bound):
    """The dotted path ``expression`` refers to, with every alias expanded."""
    if isinstance(expression, ast.Name):
        return bound.get(expression.id, expression.id)
    if isinstance(expression, ast.Attribute):
        prefix = _resolved_dotted(expression.value, bound)
        return f"{prefix}.{expression.attr}" if prefix else None
    if isinstance(expression, ast.Call):
        # `contextlib.suppress(...)` *is* `contextlib.suppress` for the purpose
        # of asking what callable it is. Without this, an attribute hanging off
        # the result -- `contextlib.suppress(X).__enter__()` -- resolves to
        # None and every dunder-shaped defeat reads as an ordinary method call.
        return _resolved_dotted(expression.func, bound)
    return None


def _resolves_to(expression, dotted, bound):
    """Does ``expression`` name ``dotted``, however this module spelled it?"""
    return _resolved_dotted(expression, bound) == dotted


def _name_catches_assertion_error(name):
    """Does a caught *name* also catch ``AssertionError``?

    Shared by the handler rule and the suppression rule so the two agree on
    what counts. ``ValueError`` and friends are the false case: they cannot
    swallow an ``assert``. A name that is not a known exception type is
    reported as swallowing, because an assert that cannot be proven live must
    not be counted as enforced.
    """
    if name in {"AssertionError", "Exception", "BaseException"}:
        return True
    builtins = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
    candidate = builtins.get(name)
    if isinstance(candidate, type):
        return issubclass(candidate, AssertionError)
    return True


def _suppression_names(call):
    """The exception names passed to a ``suppress(...)`` call.

    A tuple is flattened, because ``suppress((TypeError, ValueError))`` and
    ``pytest.raises((KeyError, RuntimeError))`` are both ordinary spellings that
    name two types. Reading only the tuple node itself would report the
    unreadable case ``BaseException`` for *every* tuple, and since
    ``BaseException`` catches ``AssertionError``, that would report the pinned
    file's four tuple-typed ``pytest.raises`` sites as defeated -- a false alarm
    on real, live asserts. Measured on ``test_timed_menu_milestones.py``:
    ``(TypeError, ValueError)``, ``(KeyError, ValueError, RuntimeError)`` and
    ``(ValueError, RuntimeError)`` all appear.

    An element that is not a plain name is still reported as universal, so an
    argument the check cannot read is never assumed to be harmless.

    The *no positional argument at all* case is the one place where the two
    families must be told apart, and the difference is measured rather than
    assumed:

    * ``contextlib.suppress()`` is legal, and the no-argument reading here is
      ``BaseException`` -- but that is *stricter than the interpreter*, not a
      description of it. Measured: ``contextlib.suppress()`` stores
      ``_exceptions == ()``, and ``issubclass(AssertionError, ())`` is
      ``False``, so it suppresses **nothing** and an assert inside it fails
      loudly. Reading it as universal therefore over-reports.
      That direction is chosen on purpose: the argument the check *can* read
      is absent, and an absent argument is not evidence of a harmless one.
      The cost is a false alarm on a spelling the pinned file does not use; the
      alternative would be to treat "no argument" as "no suppression", which
      cannot be told apart here from a call whose arguments are simply
      unreadable.
    * ``pytest.raises()`` with no expected type raises ``ValueError: You must
      specify at least one parameter`` while the context object is being
      constructed -- before the body is entered at all. The test fails loudly
      and no assert is ever evaluated, so calling it a defeat would report a
      live contract as dead.

    ``pytest.raises(match=...)`` stays universal on purpose; see
    ``_raises_without_an_expected_type`` for why that case is undecidable
    rather than loud.
    """
    names = []
    for argument in call.args:
        if isinstance(argument, ast.Tuple):
            names.extend(
                element.id if isinstance(element, ast.Name) else "BaseException"
                for element in argument.elts
            )
        else:
            names.append(argument.id if isinstance(argument, ast.Name) else "BaseException")
    return names or ["BaseException"]


def _suppressed_by_dunder(call, bound):
    """Is this ``suppressor.__enter__()`` -- the dunder spelling of the defeat?

    ``with contextlib.suppress(AssertionError):`` has an alias that never leaves
    the ``with`` header:

        with contextlib.suppress(AssertionError).__enter__():
            assert 1 == 2

    That reads as a suppression to a human and as an ordinary method call to the
    existing rules: ``_resolves_to`` sees ``contextlib.suppress.__enter__`` and
    matches no entry in ``SUPPRESSING_CONTEXTS``, and ``_unreadable_suppressor``
    only accepts a bare ``Name``. Matching one concrete spelling and leaving the
    family open is exactly the mistake #288 recorded for ``except*``.

    Only the ``__enter__`` dunder is stripped, and only one component, so this
    cannot match a suppressor stored under a different attribute name. A
    non-suppressor that happens to define ``__enter__`` is not matched: the
    underlying call still has to resolve to a real suppressor.

    Note this reports the family as *unable to leave the test green* rather
    than as a silent swallow, and the runtime agrees: ``__enter__`` is
    ``pass``, so entry raises ``TypeError`` before the body runs. The same
    reasoning already covers ``pytest.raises()`` with no expected type in
    :func:`_raises_without_an_expected_type`. "Defeated" here means the owning
    test cannot go green, which is the property the sentinel is asserting.
    """
    func = call.func
    # The whole dunder family is answered uniformly, and the runtime agrees.
    # `contextlib.suppress.__enter__` is `def __enter__(self): pass`, so it
    # returns `None` for *every* instantiation, and
    # `with None:` raises `TypeError: 'NoneType' object does not support the
    # context manager protocol` -- the body is never reached, for every
    # exception argument and for the no-argument form alike. Measured for
    # `AssertionError`, `ValueError`, `RuntimeError`, `Exception`,
    # `BaseException` and `suppress()`.
    #
    # So the exception list is irrelevant here and no argument-reading
    # distinction can be earned: splitting the family by argument would report
    # two shapes with byte-identical runtime differently. This matches the
    # existing treatment of `pytest.raises()` with no expected type, which is
    # also loud *before* the body and is likewise read as unable to leave the
    # test green.
    if not (isinstance(func, ast.Attribute) and func.attr == "__enter__"):
        return []
    if not any(_resolves_to(func.value, dotted, bound) for dotted in SUPPRESSING_CONTEXTS):
        return []
    # Every member of the family is loud before the body, so the whole family is
    # reported as unable to leave the test green. `BaseException` is the
    # caller's "catches anything" marker and is deliberately *not* a claim
    # about this suppressor's arguments -- there are none that matter.
    return ["BaseException"]


def _store_target_names(targets):
    """Every name a single store statement binds.

    An ``ast.Name`` is the easy case, but Python lets a store target be a
    tuple, a list, or a starred expression, and any of those can name a
    variable:

        cs, other = (contextlib.nullcontext(), 2)
        [cs] = [contextlib.nullcontext()]
        cs, *rest = (contextlib.nullcontext(), 2, 3)

    Reading only ``ast.Name`` targets made those stores invisible, so a name
    carried by an earlier walrus was never retired and a live assert under the
    later ``with`` was reported defeated. Unwrapping the nested forms is what
    makes the supersession rule above apply to those binding forms too.

    Two store forms are still not modelled here. ``ast.AugAssign`` (``cs += 1``)
    is not unwrapped, and it cannot retire a carried suppressor. It is benign
    today only because ``contextlib.suppress`` defines no ``__iadd__`` and no
    ``__add__``, so ``cs += 1`` on a carried suppressor raises ``TypeError``
    before any assert can be reached -- the later ``with cs:`` is unreachable
    for the same reason it is after ``del cs``. That safety is incidental to
    the type, not a property this rule guarantees, so do not read the absence
    of an ``AugAssign`` branch as a decision.

    A subscript or attribute target (``obj.cs = ...``) is deliberately not
    followed: it binds an attribute, not a local name, so it cannot retire a
    local binding.

    A ``match`` capture is a store too, and it is not a target node at all --
    ``case [cs]:`` parses to an ``ast.MatchAs`` whose ``name`` is a plain string
    rather than an ``ast`` target. ``MatchStar`` (``case [other, *cs]:``) and
    ``MatchMapping.rest`` (``case {'a': 1, **cs}:``) bind the same way, so
    ``_match_capture_names`` handles them rather than inventing fake targets
    here.
    """
    names = []
    pending = list(targets)
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
    return names


def _scope_body_nodes(function):
    """Walk `function`'s own scope, stopping at nested function boundaries.

    ``ast.walk`` descends into a nested ``def``/``lambda``, whose body binds
    names in a different scope. A rule about which store is live *in this
    function* must not see those. Comprehensions are left in: a walrus inside a
    comprehension binds in the enclosing scope, which is exactly why
    ``_walrus_is_conditional`` has to reason about them at all.

    ``ClassDef`` is a boundary for the same reason, and was missing until
    #350. A class body executes in its own namespace, so a ``match`` capture
    written there binds the *class's* attribute, not ``function``'s local.
    Walking into one retired the enclosing name and certified a genuinely
    swallowed assert as enforced -- the damaging direction.

        def outer(x):
            with (cs := suppress(AssertionError)):
                pass
            class C:
                match nullcontext():
                    case cs:          # binds C.cs, NOT outer()'s cs
                        pass
            with cs:                # outer()'s cs is still the suppressor
                assert x != 1      # swallowed -> correct verdict is False

    Executed on CPython 3.12.14 with ``x=1``: the assert does not fire, because
    ``outer``'s ``cs`` still points at the suppressor, while the analyzer
    reported ``True`` -- a live contract certified where none exists.

    A class body is not a *function* scope, so excluding it here is what makes
    ``_own_imports`` and ``_match_capture_names`` agree: both walk the same
    boundary, and neither now attributes a nested class body's binding to the
    enclosing function. The nested ``def``/``lambda`` case was already correct
    and is unchanged; it is kept as a control row in the shipped test so the
    two boundaries cannot drift apart.
    """
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _match_capture_names(function):
    """Every ``(name, owning statement)`` bound by a ``match`` capture.

    A capture rebinds the name to the value that was matched, so a name
    carrying a suppressor from an earlier walrus must not survive the clause
    that captures it. Left unmodelled, the stale suppressor reached a later
    ``with cs:`` and certified a **live** assert as swallowed.

    These nodes carry a name string instead of a target, and they are not
    reachable from an enclosing statement's target list, so they need their own
    walk. ``MatchAs`` with ``pattern=None`` is the bare ``case _ as cs:`` form;
    a ``MatchAs`` with ``name=None`` is a wildcard or a group and binds nothing.

    The owning ``ast.stmt`` is returned alongside each name because
    ``_binding_order`` positions every store by the top-level statement that
    contains it; a bare pattern node is not in that chain.

    Only captures in `function`'s own scope count. A ``match`` inside a nested
    ``def`` or ``lambda`` binds a name in *that* scope, which has nothing to do
    with the outer binding, and walking into it retired the outer name -- the
    opposite error, turning a swallowed assert into a reported-live one.
    """
    owners = {}
    for statement in _scope_body_nodes(function):
        if not isinstance(statement, ast.Match):
            continue
        for node in ast.walk(statement):
            if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
                owners.setdefault(node.name, statement)
            elif isinstance(node, ast.MatchMapping) and node.rest is not None:
                owners.setdefault(node.rest, statement)
    return owners


def _carrier_runtime_kinds(function):
    """Every ``(name, (owning statement, runtime kind))`` from a string field.

    Three binding forms carry the bound name as a plain ``str`` on a node that
    is not a target at all, so a walk over ``statement.targets`` cannot see
    them and ``_store_bindings`` recorded nothing for the name at all:

        import os as cs                # Import.asname    -> a module
        def cs(): ...                  # FunctionDef.name -> a function
        class cs: ...                  # ClassDef.name    -> a class

    With no store entry, :func:`_entry_is_dead` had no value to read, so a
    later ``with cs:`` was reported *enforced* while the interpreter was
    already raising ``TypeError: 'module'/'type'/'function' object does not
    support the context manager protocol`` on entry. The assert is never
    evaluated, so calling it load-bearing is the **damaging** direction -- a
    dead contract certified as live. #359.

    ``from M import N as cs`` is deliberately **absent**, and that is the
    sharp edge of this function rather than an oversight. ``import os as cs``
    binds the module ``os`` and nothing else, so the syntax decides the value.
    But ``from M import N as cs`` binds whatever attribute ``N`` happens to be
    on ``M``, and one spelling produces several runtime types (measured, #376):

        from os import path as cs           -> os.path   (a module)
        from os import sep as cs            -> os.sep    (a str)
        from decimal import Decimal as cs   -> a class
        from mymod import ctx as cs         -> WHATEVER mymod.ctx is

    A module that exports a real context manager makes the identical statement
    decide the *other* way -- ``with cs:`` succeeds and the assert is live.
    Recording it as ``"module"`` would report that live assert as dead and
    **drop a real pinned contract**, which is the damaging direction this rule
    exists to correct. So the ``ImportFrom`` half is declined here, exactly as
    the module declines every other value it cannot read off the syntax.
    Resolving it would mean reading the attribute off the owning tree, which
    is a different and considerably larger rule.

    The runtime kind is returned rather than a synthetic ``ast.Name`` so the
    value stays attached to the real store: a synthesised node is a child of
    nothing, so containment and ordering both become unanswerable, which is
    the mistake #337's carrier machinery already learned to avoid.

    A ``match`` capture is deliberately *not* here: it binds whatever was
    matched, which is arbitrary and very often a real context manager, and
    #342 owns that decision. Only the forms whose value is fixed by the
    syntax are listed.

    Only bindings in `function`'s own scope count, for the reason
    ``_match_capture_names`` gives: a ``def cs`` in a nested scope binds that
    scope's name, and retiring the outer binding on it would report a
    swallowed assert as live -- the opposite error.
    """
    owners = {}
    for statement in _scope_body_nodes(function):
        carriers = []
        if isinstance(statement, ast.Import):
            # One statement binds several names, and only the `asname`
            # spelling retires a name *different* from the one imported.
            # `import a.b as cs` binds the module `a.b`, and the language
            # gives this one spelling exactly one meaning, so it is decidable.
            carriers.extend((alias.asname, "module") for alias in statement.names if alias.asname)
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            carriers.append((statement.name, "function"))
        elif isinstance(statement, ast.ClassDef):
            carriers.append((statement.name, "type"))
        for name, kind in carriers:
            owners.setdefault(name, (statement, kind))
    return owners


def _pattern_is_irrefutable(pattern):
    """Is ``pattern`` a pattern that matches *every* remaining subject?

    Two spellings qualify, and both are ``ast.MatchAs``:

    * a bare capture (``case name:``) and the wildcard (``case _:``), which
      parse to a ``MatchAs`` whose inner ``pattern`` is ``None``;
    * a capture of an already-irrefutable pattern (``case _ as name:``), which
      parses to a ``MatchAs`` wrapping *another* ``MatchAs`` -- the outer one
      is the capture, the inner one is the pattern it captures. Reading only
      ``pattern is None`` misses this form, which is exactly the irrefutable
      capture the shipped suite exercises.

    Every other pattern -- a value, a sequence, a mapping, a class pattern, a
    starred capture, or a value pattern wrapped in an ``as`` -- can fail to
    match, and a clause built on one of those is only *selected* on the path
    where it does.

    An ``ast.MatchOr`` is the one compound that could be irrefutable, and only
    when every alternative is. CPython rejects a *bare*-capture all-alternatives
    shape -- ``case x | y:`` fails with "name capture 'x' makes remaining
    patterns unreachable" -- but that is not the whole story, and the earlier
    claim here was too broad. ``case [*cs] | [*cs]:`` followed by another
    clause **does** compile on CPython 3.12.14, because neither alternative is
    a bare name capture on its own.

    Such a pattern is still *refutable*: both alternatives are sequence
    patterns, so a non-sequence subject skips the clause entirely and the
    capture never runs. The `all(...)` requirement is what makes that fall on
    the "capture may not have run" side, which keeps a carried suppressor in
    force rather than retiring a binding nothing made -- the damaging
    direction this whole issue is about.
    """
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or _pattern_is_irrefutable(pattern.pattern)
    if isinstance(pattern, ast.MatchOr):
        return all(_pattern_is_irrefutable(alternative) for alternative in pattern.patterns)
    return False


def _pattern_binds(pattern, name):
    """Does ``pattern`` bind ``name`` in any clause it can be selected for?"""
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            return True
        if isinstance(node, ast.MatchMapping) and node.rest == name:
            return True
    return False


def _capture_always_binds(match, name):
    """Is ``name`` bound by ``match`` on *every* path that reaches it?

    A capture is a side effect of its clause being *selected*, and a clause is
    only selected when the patterns before it all fail. So a capture is
    guaranteed to bind only in the one position where nothing can come before
    it and nothing can stop it:

    * that clause's pattern is irrefutable, so the match cannot fall through
      to "no clause matched" and a later clause cannot take it instead.

    The last-clause half of that condition is *checked* rather than assumed,
    but on CPython 3.12.14 it is not independently reachable: a capture in a
    clause that is followed by another is a compile error whenever the
    pattern is irrefutable -- ``case _ as cs:`` followed by anything is
    rejected with "wildcard makes remaining patterns unreachable", a bare
    ``case cs:`` with "name capture 'cs' makes remaining patterns
    unreachable", and a bare-name all-alternatives ``case a | b:`` likewise.
    (A *sequence* capture in every alternative, ``case [*cs] | [*cs]:``,
    compiles -- but that pattern is refutable, so it does not reach this
    branch; see :func:`_pattern_is_irrefutable`.) So an irrefutable capture is
    always the last clause in a program that compiles.
    The check is kept because it is the *reason* the answer is safe, it costs
    one comparison, and dropping it would make the function wrong for an AST
    that ``ast.parse`` accepts even though ``compile`` would not -- which is
    exactly the input this module is handed.

    A **guard** is deliberately not a condition, because the name is bound as
    soon as the pattern matches and is never unbound when the guard turns out
    to be false. That was measured rather than assumed: starting from a
    pre-existing value, ``case _ as cs if False:`` leaves ``cs`` holding the
    *subject*, not the value it had before, so a guard cannot undo a binding
    that has already happened. Treating a guard as a blocker would have retired
    an alias that the capture really did supersede.

    Anything else is a capture that *may* not have run. That distinction is
    what #342 is about: retiring a carried alias for a capture that never
    executed leaves the suppressor in force in reality while the analyzer
    reports the assert live.

    Measuring all six shipped capture forms against CPython 3.12.14 over ten
    subjects each, this predicate separates 38 dangerous verdicts from the
    remainder: without it every refutable shape reported a swallowed assert
    as enforced, because a no-match subject left the carried suppressor bound
    and nothing in the walk could see that the capture had not run.
    """
    if not match.cases:
        return False
    last = match.cases[-1]
    return _pattern_is_irrefutable(last.pattern) and _pattern_binds(last.pattern, name)


def _store_retires(statement, name):
    """Does this store settle ``name``, or may it simply not have run?

    Every store form the walk collects binds its name whenever control reaches
    it, and :func:`_resolve_bindings` already separates the ones that can be
    skipped (``conditional``). A ``match`` capture is the one form that is
    *always* recorded as unconditional yet can still fail to run, because its
    clause may not be selected. Left unfiltered it retires a carried alias on
    a path where the capture never happened, so the stale suppressor reaches
    the next ``with`` -- and the assert under it is really swallowed, while
    the analyzer calls it enforced.
    """
    return not isinstance(statement, ast.Match) or _capture_always_binds(statement, name)


def _entry_may_be_an_unrun_capture(entry):
    """Is this entry a ``match`` capture that is not guaranteed to have bound?

    The narrow companion to :func:`_store_retires`, asked of a whole
    ``(statement, value, conditional)`` entry rather than of a statement and a
    name. It exists because the name is not carried on the entry itself, and
    the caller that has to decide whether a single competing store is ambiguous
    only has the entry.

    A ``match`` statement can capture several names, so the name is recovered
    by asking which of the captures it owns is the one this entry records --
    a capture that is guaranteed to bind for *some* name is still a capture
    that may not bind for the name in question, so every owned name is
    checked and any one of them being undecidable makes the entry so.
    """
    statement = entry[0]
    if not isinstance(statement, ast.Match):
        return False
    return not all(
        _capture_always_binds(statement, name) for name in _match_capture_names_for(statement)
    )


def _match_capture_names_for(statement):
    """The names a single ``ast.Match`` statement captures."""
    names = []
    for node in ast.walk(statement):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            names.append(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            names.append(node.rest)
    return names


def _assigned_suppressors(function, bound):
    """Map each statement index to the suppressor names bound *by* it.

    ``contextlib.suppress(AssertionError)`` is an expression, and an expression
    can be given a name and entered later:

        cs = contextlib.suppress(AssertionError)
        with cs:
            assert 1 == 2

    By the time the ``with`` header is read, the call that built ``cs`` is gone,
    so the header holds a bare ``Name`` and the resolution rule has nothing to
    resolve. Only an assignment whose right-hand side is itself a readable
    suppressor is recorded, so a name bound to an ordinary call is never
    assumed to suppress. ``isinstance`` on a class attribute is deliberately
    not matched: that is an ``Attribute`` chain, not a bare name, and reaching
    through a class to a descriptor needs a runtime this check does not have.

    The result is keyed by statement index rather than merged into one dict,
    because *order* decides whether the alias is bound yet. ``with cs:`` placed
    **before** ``cs = suppress(...)`` raises ``NameError`` on entry -- the test
    fails loudly rather than passing quietly -- so it is not a defeat, and a
    single merged dict would have called it one. Only the assignments that
    precede the ``with`` are visible to it.

    Assignments are collected from the whole function body rather than from
    ``function.body`` alone, so a suppressor bound inside an ``if``, a loop, a
    ``try`` or a nested ``with`` is still found. Restricting the walk to the
    top level is the escape recorded in #308: a suppressor's reach does not
    depend on the indentation it was written at, and reading only the
    outermost statements reports a swallowed assert as *enforced*.

    Bindings are split by whether two of them can be reached on the same path.
    Only *competing* bindings make a name ambiguous:

    * **Unconditional** -- sitting directly in the function body. These run in
      source order, so at any ``with`` only the last one can be live, and it
      supersedes the rest rather than competing with them.
    * **Conditional** -- nested in an ``if``, a loop, a ``try`` or a ``with``.
      Two of these genuinely can reach the same ``with`` on different paths, so
      only these make a name ambiguous.

    The distinction is load-bearing in both directions. Reading two
    *sequential* suppressor bindings as competing would report a live assert
    as dead -- the opposite error from the one #308 criterion 1 is about, and
    the more damaging one here, because it drops a real contract out of the
    sentinel's view. Reading two *competing* bindings as sequential would pick
    a winner by source order and certify a disarmed assert as load-bearing.
    Per #308 criterion 1 a competing name is *unreadable*, and the safe
    direction is to treat it as a defeat.

    EVERY binding of a name is recorded, not only the suppressor ones. That is
    what makes a superseding store visible at all: a name rebound to an
    ordinary call is recorded and supersedes like any other value, so

        cs = contextlib.suppress(AssertionError)
        cs = helper.make()

    resolves to "not an alias" instead of leaving the first binding as the sole
    known one. Recording only the suppressor bindings would make that
    sequence look like a lone alias and report the live assert as dead.

    An assignment *expression* is a binding too, and is collected here for the
    same reason:

        with (cs := contextlib.suppress(AssertionError)):
            assert 1 == 2
        with cs:
            assert 1 == 2          # also swallowed -- `cs` is still bound

    A ``NamedExpr`` is not an ``ast.Assign``, so before #324 it was invisible
    to this walk entirely and that second assert was reported enforced -- the
    damaging direction. Recording it here rather than in the header logic is
    what makes the supersession above apply to it unchanged: a later
    ``cs = nullcontext()`` is an ordinary store, it wins on order, and the
    carried suppressor does not outlive it. Recording the binding *only* where
    the header is read -- the approach #323 took -- gets the re-entry case and
    loses the supersession case, because nothing ever retires the value.
    """
    # Every binding of every name, tagged with whether that store can compete
    # with another, so that supersession and ambiguity stay told apart.
    bindings, raw_values = _store_bindings(function, bound)
    orders = {
        id(statement): _binding_order(function, statement)
        for entries in bindings.values()
        for statement, _, _ in entries
    }
    assigned = {}
    for index in range(len(function.body)):
        for name, entries in bindings.items():
            seen = [entry for entry in entries if orders[id(entry[0])] <= index]
            if not seen:
                continue
            value = _resolve_bindings(seen, bound, orders, index, function)
            if value is not None:
                assigned.setdefault(index, {})[name] = value
            elif index and name in assigned.get(index - 1, {}):
                # #367. The name is still bound here, and what it carries is
                # deliberately *not* a suppressor. Recording that explicitly
                # is what stops `_aliased_suppressions`' `bound_so_far` from
                # carrying the previous index's suppressor forward:
                #
                #     with (cs := contextlib.suppress(AssertionError)):
                #         cs = contextlib.nullcontext()
                #         assert x != 1
                #     with cs:                  # `cs` is the nullcontext
                #         assert x != 2
                #
                # At the index of the *first* `with` the name really does carry
                # the suppressor, so that index records it. The next index
                # resolves the tie to the `nullcontext`, which is not a
                # suppressor, and without this line the walk would find no
                # entry for it and read the stale suppressor from the previous
                # index -- reporting a live assert defeated. The marker is
                # `_NOT_A_SUPPRESSOR` rather than a dropped key precisely
                # because "not a suppressor" and "not bound" are different
                # answers here.
                assigned.setdefault(index, {})[name] = _NOT_A_SUPPRESSOR
    # #367. A store in a statement's *body* runs before the next statement, so
    # a header in that next statement must not read the value this statement's
    # own header entered:
    #
    #     with (cs := contextlib.suppress(AssertionError)):
    #         cs = contextlib.nullcontext()
    #         assert x != 1
    #     with cs:                  # `cs` is the nullcontext
    #         assert x != 2
    #
    # At the index of the first `with`, `_resolve_bindings` answers with the
    # `suppress` -- correct, because that header is evaluated before the body
    # store happens. But the body store *has* run by the time the next
    # statement is reached, so the value that statement's headers read is the
    # `nullcontext`. Without this pass the next index records
    # `_NOT_A_SUPPRESSOR` for `cs`, and `_aliased_suppressions`, which advances
    # its carried set from `assigned[index]` at the end of each statement,
    # hands the *first* `with`'s suppressor forward instead.
    #
    # The pass is deliberately narrow: only a name whose latest store *at or
    # before* `index` is settled past `index` -- a store in a `with` body that
    # has already run by the next statement -- is downgraded. Everything else
    # keeps the value this statement's own header entered, which is what a
    # header written *before* the body store must see.
    for index in range(len(function.body) - 1):
        following = index + 1
        for name, entries in bindings.items():
            value = assigned.get(index, {}).get(name)
            if value is None or value is _NOT_A_SUPPRESSOR:
                continue
            seen = [entry for entry in entries if orders[id(entry[0])] <= index]
            if not seen:
                continue
            latest = max(orders[id(entry[0])] for entry in seen)
            if not any(
                _store_is_settled_before(entry, orders, following, function)
                for entry in seen
                if orders[id(entry[0])] == latest
            ):
                continue
            resolved = _resolve_bindings(
                [entry for entry in entries if orders[id(entry[0])] <= following],
                bound,
                orders,
                following,
                function,
            )
            if resolved is None:
                # Recorded against THIS index, not the next one.
                # `_aliased_suppressions` advances its carried set from
                # `assigned[index]` at the end of each statement, so this is
                # the entry the *next* statement's headers read. Recording it
                # against `following` would land one statement too late and
                # leave the stale suppressor in force for exactly one header.
                assigned.setdefault(index, {})[name] = _NOT_A_SUPPRESSOR
    # The raw table has to be built for the whole function, not assembled as
    # the recording walk proceeds. #333: an assignment expression in a `with`
    # header can alias a name whose own store `ast.walk` has not reached yet,
    # because `ast.walk` is breadth-first and visits the header before stores
    # that precede it in source. `_store_bindings` applies the dereference to
    # every recorded store and hands back the table it used, so the map and
    # the table can never disagree about what a name carries.
    return assigned, raw_values


def _store_bindings(function, bound):
    """Map every name ``function`` binds to its ``(stmt, value, cond)`` stores.

    Returns ``(bindings, raw_values)``. ``raw_values`` is the undereferenced
    right-hand side of every value-bearing store, keyed the same way; the
    recorded ``value`` has already had any alias followed through it. The
    caller that resolves a *header* needs the raw side to follow a chain of
    its own, which is why both are handed back rather than only the map.

    The raw per-name store lists, before any resolution to a suppressor. Rules
    that need to know *what value* a name received -- rather than whether that
    value is a known suppressor -- read this instead, because
    :func:`_assigned_suppressors` resolves a non-suppressor to ``None`` and the
    two meanings of that ``None`` are exactly what :func:`_entry_is_dead`
    needs to tell apart.

    The recorded ``value`` is the store's right-hand side with any alias
    already followed, so a name bound to a suppressor -- directly or through a
    chain -- is recorded as the suppressor it carries. See :func:`_deref_alias`
    for why the chain is resolved against the whole function at once.
    """
    bindings = {}
    # #333: the right-hand side of every store, keyed by the name it binds,
    # gathered BEFORE the recording walk so an assignment expression can be
    # resolved against a name whose own store has not been visited yet.
    # `ast.walk` is breadth-first, so a `with` header is visited before stores
    # that precede it in source; resolving against the table as it fills would
    # leave a two-hop chain (`first` -> `second` -> `(cs := second)`) stuck at
    # an unrecorded name and report a swallowed assert as live.
    #
    # The table covers ALL stores, not just walruses, and the dereference below
    # is applied to every one of them. The walrus path needs the table resolved
    # at a `with` HEADER; the ordinary `with alias:` path resolves at the STORE
    # SITE, where the table only has to be filled up to that point. Both need
    # the same whole-function view to follow a name to its binding, so they
    # share the table rather than keeping two in step by hand.
    #
    # "Whichever binding ran last" is not the same as "whichever binding is
    # written last". A read only sees the stores that precede it, so both
    # resolution sites pass the position they are resolving at and let
    # :func:`_last_store_before` pick the entry that had actually run.
    raw_values = _raw_store_values(function)
    orders = {
        id(statement): _binding_order(function, statement)
        for entries in raw_values.values()
        for statement, _ in entries
    }
    for statement in ast.walk(function):
        targets = []
        value = None
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
            value = statement.value
        elif isinstance(statement, ast.NamedExpr):
            # #324: an assignment expression is a binding like any other store.
            # It is not an `ast.Assign`, so before this it was invisible here
            # and a `with (cs := suppress(...)):` header left `cs` bound for
            # the rest of the scope without any later `with cs:` seeing it --
            # a swallowed assert reported live. Recording it through the same
            # machinery means the per-index resolution, the supersession by a
            # later store, and the non-suppressor rule below all apply to it
            # unchanged, which is what keeps a later `cs = nullcontext()` from
            # inheriting the carried suppressor (#308).
            targets = [statement.target]
            value = statement.value
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            # #324: a loop target is a store like any other. `for cs in ():`
            # rebinds the name on every path that reaches the loop, so a
            # carried suppressor must not survive it. Recording only
            # `ast.Name` targets left this invisible and let a stale
            # suppressor outrank the loop's own binding.
            targets = [statement.target]
            # #370. `value = None` above made the binding *retire* a carried
            # suppressor, which is what #324 needed, but it also left the name
            # carrying nothing at all -- so a `for` that binds a suppressor was
            # invisible in the other direction too:
            #
            #     for cs in [contextlib.suppress(AssertionError)]:
            #         with cs:
            #             assert 1 == 2      # reported enforced; swallowed
            #
            # A loop target does receive a value, and the single-element case
            # records it so `_resolve_bindings` can see the suppressor. The
            # `None` is kept for every other iterable, which is where the
            # retirement behaviour is all that is sound.
            value = _single_loop_element(getattr(statement, "iter", None))
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            # #324: `with ... as cs:` is a store too, and the item expression
            # is what the `with` would evaluate. A walrus carried in from
            # earlier must be retired by it.
            for item in statement.items:
                if item.optional_vars is not None:
                    targets.append(item.optional_vars)
                    value = None
        elif isinstance(statement, ast.ExceptHandler):
            # #324: `except E as cs:` binds `cs`, and CPython deletes the name
            # when the handler exits, so a carried suppressor must not outlive
            # it either.
            if statement.name is not None:
                targets = [ast.Name(id=statement.name, ctx=ast.Store())]
                value = None
        elif isinstance(statement, ast.Delete):
            # #324: `del cs` unbinds the name outright. Recording it as a store
            # of `None` is what makes the existing supersession rule retire any
            # earlier binding of that name.
            targets = list(statement.targets)
            value = None
        else:
            continue
        # A store written directly in the function body runs on every path;
        # one nested inside a block runs on some paths only.
        # A `NamedExpr` never sits directly in the function body, so for it the
        # question is whether the *top-level statement that contains it* is
        # written directly in the body. A walrus in a top-level `with` header
        # therefore runs on every path (it is evaluated whenever that `with`
        # is reached), while a walrus inside an `if` or a loop still runs on
        # some paths only.
        if isinstance(statement, ast.NamedExpr):
            conditional = _walrus_is_conditional(function, statement)
        else:
            conditional = statement not in function.body
        for name in _store_target_names(targets):
            bindings.setdefault(name, []).append(
                (
                    statement,
                    _deref_alias(
                        value,
                        raw_values,
                        orders,
                        _binding_order(function, statement),
                        name,
                        function,
                    ),
                    conditional,
                )
            )
    # A `match` capture is the one store form that is not reachable from a
    # statement's target list, so it cannot ride along in the loop above.
    #
    # `conditional` is False only when the capture is *guaranteed* to run --
    # a last, irrefutable, unguarded clause (`_capture_always_binds`). For the
    # refutable shapes the capture is marked conditional instead, because a
    # clause that is not selected never binds anything. Marking those
    # unconditional (the original #324 behaviour) made the capture supersede
    # the earlier walrus on every path, so a subject that matched no clause at
    # all still retired the carried suppressor and reported the assert below it
    # as live while it was really swallowed. See #342.
    for name, statement in _match_capture_names(function).items():
        conditional = not _store_retires(statement, name)
        bindings.setdefault(name, []).append((statement, None, conditional))
    # #359. The four string-field carriers (`import os as cs`, `def cs`,
    # `class cs`, ...) bind a name the loop above never sees, because their
    # name is a string field of a node rather than a target. They are recorded
    # here with the runtime kind their value is pinned to, so that
    # `_entry_is_dead` can rule on them the way it rules on a literal: a module,
    # a class and a function are not context managers, so entering one raises
    # before the assert runs.
    #
    # `conditional` is the same test the target-list loop uses -- a binding
    # nested in a block may not have run -- so a carrier inside an `if` cannot
    # be read as having retired the name on a path where it never executed.
    for name, (statement, kind) in _carrier_runtime_kinds(function).items():
        conditional = statement not in function.body
        bindings.setdefault(name, []).append((statement, kind, conditional))
    # A name bound by exactly one readable suppressor is a known alias. A name
    # bound by several is ambiguous -- see the docstring (#308 criterion 1) --
    # and must NOT be resolved by source order. It is recorded as an
    # `AMBIGUOUS` marker instead of being dropped: dropping it would fall back
    # to "this name is not a known suppressor", which reports the assert as
    # *enforced*, and that is the damaging direction. On the ambiguous path the
    # rule genuinely cannot tell which target was applied, so it treats the
    # name as a suppression and says so (#308 criterion 1: over-reporting is the
    # safe direction here).
    #
    # Ambiguity is judged over the whole function: a nested binding and a
    # top-level one are the same name, and a rule that let the source order
    # pick between them would report the verdict for one path only.
    # Resolution is per top-level statement rather than once for the whole
    # function, because "which store is live" is a question about one `with`
    # and not about the function. A `with` placed *between* two stores sees
    # the earlier one:
    #
    #     cs = contextlib.suppress(AssertionError)
    #     with cs:              # <- this assert is swallowed
    #         assert 1 == 2
    #     cs = helper.make()
    #
    # Resolving once for the function would answer that `with` with the *last*
    # store and certify a disarmed assert as load-bearing. So each index
    # resolves from the bindings that precede it alone.
    return bindings, raw_values


def _resolve_bindings(entries, bound, orders, index=None, function=None):
    """Resolve one name from the bindings in effect at a single ``with``.

    ``entries`` are ``(statement, value, conditional)`` triples, already
    filtered to the stores that run at or before the ``with`` in question.
    ``index`` is the position of that ``with`` among the function's top-level
    statements and ``function`` the scope it sits in. ``index`` and
    ``function`` together are what tell a *settled* store from one still
    waiting on a branch; ``function`` is also what tells a real builtin from
    a name the function rebinds.
    """
    # An unconditional store runs on *every* path, so the last one of those is
    # the value in force unless some conditional store comes after it. Only the
    # conditional stores that are ordered later can still compete with it;
    # earlier ones were overwritten by it:
    #
    #     if p:
    #         cs = contextlib.suppress(AssertionError)   # competing
    #     else:
    #         cs = contextlib.suppress(ValueError)        # competing
    #     cs = helper.make()                              # runs last, everywhere
    #     with cs:                                        # `cs` is not a suppressor
    #
    # Treating the two branches above as still ambiguous here would report a
    # live assert as swallowed. Counting a conditional store written *after* an
    # unconditional one as competing is the other half: it genuinely can be the
    # last store to run.
    unconditional = [entry for entry in entries if not entry[2]]
    latest = max((orders[id(entry[0])] for entry in unconditional), default=None)
    competing = [
        entry for entry in entries if entry[2] and (latest is None or orders[id(entry[0])] > latest)
    ]
    # A `for cs in ...:` target and an assignment to `cs` in that loop's own
    # body are not two competing bindings. The body runs *after* the target on
    # every pass, so whichever it stores is the value left behind and the
    # target's element is gone. Counting both made
    #
    #     import os as cs
    #     if flag:
    #         for cs in (None,):
    #             cs = nullcontext()
    #
    # ambiguous and reported a live header dead: the name really is a context
    # manager whenever the loop runs. The target is dropped from the competing
    # set when its own body rebinds the name, leaving the body's store as the
    # single conditional binding -- which supersedes the carried carrier.
    #
    # The collapse is only sound while the body's own store is what actually
    # answers the header, and that store is not always readable. Dropping the
    # target while the body binds `contextlib.suppress(AssertionError)` and
    # then answering from the *target* retired a suppressor that is really in
    # force on the path where the loop runs:
    #
    #     cs = None
    #     if flag:
    #         for cs in (None,):
    #             cs = contextlib.suppress(AssertionError)
    #
    # Once the target is out of the competing set, the body's store is the one
    # answer, so the collapse is kept only when the body's last rebind is a
    # store this can read. An unreadable one leaves the target in place, and
    # the two competing bindings are then reported ambiguous -- which is the
    # safe direction, because the name is genuinely a suppressor on the path
    # where the loop runs.
    collapsed_entries, collapsed = _collapse_loop_targets_into_bodies(competing)
    if collapsed:
        competing = collapsed_entries
    if len(competing) > 1 or any(_entry_may_be_an_unrun_capture(entry) for entry in competing):
        # More than one conditional binding can reach this `with` on different
        # paths, so which suppressor is live is undecidable. Recorded as an
        # `AMBIGUOUS` marker rather than dropped: dropping it would fall back
        # to "this name is not a known suppressor", which reports the assert as
        # *enforced* -- the damaging direction.
        #
        # A *single* `match` capture is the #342 case and it is decided the
        # same way, because the question is identical: the clause may simply
        # not be selected, so the name at this header is either the captured
        # value or the one it supersedes. Which is in force is not decidable
        # from the source, and picking the later store would retire a
        # suppressor that is still bound on the path that skipped it.
        # `AMBIGUOUS` keeps the answer on the safe side: the name is treated
        # as a possible suppressor, so the assert is reported defeated even
        # though one of the two readings is a live contract.
        #
        # A single *non-capture* conditional store is deliberately NOT
        # ambiguous. A plain `if flag: cs = nullcontext()` supersedes a
        # carried walrus on the path that runs, and the shipped rows pin the
        # assert under the following `with` as live. Widening the rule to
        # every single competing store regressed two of those rows, so the
        # widening is scoped to captures.
        return AMBIGUOUS_SUPPRESSOR
    # Otherwise nothing competes with anything: the stores that can be last are
    # a single one, so the highest-ordered entry is what the `with` enters.
    # `max` runs over every entry, not just the unconditional ones: a
    # conditional store ordered *after* the latest unconditional store is
    # exactly the supersession #323 models, and dropping it would resurrect the
    # stale suppressor.
    #
    # #367: `max` returns the FIRST maximal entry, and two stores inside one
    # top-level statement share an order by construction. So where the maximum
    # is attained more than once, `max` resolves the tie by walk position --
    # a question the source does not answer. The sharp case is a `for`/`else`,
    # where exactly one branch runs and which one is an input:
    #
    #     for i in items:
    #         first = contextlib.suppress(AssertionError)
    #     else:
    #         first = contextlib.nullcontext()
    #     with (cs := first):
    #         assert x != 1
    #
    # `max` returned the *first* maximal entry, so a `for`/`else` pair resolved
    # to whichever branch the walk reached first -- fixed regardless of which
    # branch runs. The `for`/`else` above is the sharp case: on `items == []`
    # the `else` runs and the name is a `nullcontext`, so the assert is LIVE,
    # while on `items == [1]` the body runs and the name is a `suppress`, so
    # the assert is swallowed. The interpreter disagrees with itself across
    # the two inputs, so no single verdict is right, and walk order is not the
    # thing that decides it.
    #
    # A tie of that kind -- every member conditional -- is already declined by
    # the `competing` branch above, and cannot reach here: with no
    # unconditional entry at the maximum order, two or more conditional
    # entries at that order are exactly two or more *competing* stores, so
    # that branch returns first. An earlier cut of this repair therefore
    # carried a second, all-conditional tie test here; it was unreachable in
    # every enumerated shape (630 order/conditional combinations, zero
    # reachable) and has been removed rather than left as a branch no mutation
    # can kill.
    # The tie-break below runs over the *surviving* candidates, not over every
    # entry. A loop target that its own body rebinds shares the body's order --
    # both are ordered by the top-level `for` that contains them -- so reading
    # `entries` here reinstated the very binding `_collapse_loop_targets_into_bodies`
    # had just retired, and the #367 tie-break then declined a name the collapse
    # had made decidable:
    #
    #     def outer(x, flag):
    #         import os as cs
    #         if flag:
    #             for cs in (None,):
    #                 cs = nullcontext()
    #         with cs:
    #             assert x != 1
    #
    # The collapse reduces the two conditional stores to the body's alone, but
    # the maximal order over *all* entries is still 1 with both the `for`
    # target and the body store tied at it. `_latest_write_in_block` cannot
    # separate stores inside one loop (neither has run in a way that settles
    # the other), so it returns `None` and the resolution became
    # `AMBIGUOUS` -- read downstream as a *possible suppressor*, which retired
    # a header CPython really enters. The maximum is therefore taken over the
    # same set the collapse produced, matching the no-tie branch below.
    candidates = competing if competing else entries
    latest = max(orders[id(entry[0])] for entry in candidates)
    tied = [entry for entry in candidates if orders[id(entry[0])] == latest]
    if len(tied) > 1:
        # Mixed tie: an unconditional store and a conditional one share the
        # order. `_binding_order` cannot separate them -- it keys a store by the
        # top-level statement containing it, so two stores in one block compare
        # equal by construction -- but source position can, and a genuine
        # supersession *is* written later:
        #
        # Treating this as ambiguous instead would re-open #323:
        #
        #     with (cs := suppress(AssertionError)):
        #         cs = nullcontext()      # same top-level statement, so the
        #         assert x != 1           # same order as the walrus above
        #     with cs:                    # `cs` is the nullcontext
        #         assert x != 2
        #
        # The unconditional walrus runs on every path; the assignment runs only
        # when the body is entered, and once it has, the name is the
        # `nullcontext`. Reporting the tie as ambiguous would call that live
        # assert defeated.
        # #367: "written later" is necessary but not sufficient. A store inside
        # a *branch* of the block may never run, so it cannot be called the
        # value in force:
        #
        #     with (cs := contextlib.suppress(AssertionError)):
        #         assert x != 1
        #         if flag:
        #             cs = contextlib.nullcontext()
        #     with cs:
        #         assert x != 2
        #
        # Measured on CPython 3.12.14, `cs` is a `suppress` for both values of
        # `flag`: the `with` statement's own item expression is what enters,
        # and the body's rebinding of the name does not change the manager the
        # `with` already holds. The second assert is swallowed either way, so
        # it must be reported defeated. Treating the branch as the later write
        # reads the `nullcontext`, retires the suppressor, and reports that
        # swallowed assert live -- the damaging direction.
        #
        # So the last write counts only when it is one that has *run*: either
        # it is unconditional, or it is the settled in-body store that
        # `_store_is_settled_before` recognises. Anything else leaves the tie
        # undecided, which is declined.
        latest_write = _latest_write_in_block(tied, orders, index, function)
        if latest_write is None:
            return AMBIGUOUS_SUPPRESSOR
        return _readable_store_value(latest_write[1], bound)
    else:
        # No tie, so the single highest-ordered candidate is the value in
        # force.
        #
        # The sort runs over `candidates`, the *surviving* set the collapse
        # above produced -- for the same reason the tie-break does. A loop
        # target that its own body rebinds shares the body's source order, so
        # taking the maximum over every entry let the retired target win a tie
        # it should not have been in, and the suppressor stored by the body was
        # dropped in favour of the element the target yielded:
        #
        #     cs = None
        #     if flag:
        #         for cs in (None,):
        #             cs = contextlib.suppress(AssertionError)
        #
        # The collapse above is what makes the body's store the only
        # candidate, so the candidate set has to be the same one the collapse
        # produced. Reading `entries` here reinstated the very binding that
        # was just retired.
        last = max(candidates, key=lambda entry: orders[id(entry[0])])[1]
        return last if _is_readable_suppressor(last, bound) else None


def _latest_write_in_block(tied, orders=None, index=None, function=None):
    """The store written last among entries that share one top-level statement.

    #367. `_binding_order` keys a store by the top-level statement containing
    it, so two stores written in the same block compare equal *by construction*
    and the tie has to be broken some other way. Source position is the one
    that answers the question the tie poses: of the stores in this block, which
    ran last? A later write to a name overwrites an earlier one, so the value
    in force afterwards is the last write's -- not the first, and not the one
    that happens to be walked first.

    That is the whole correction. `ast.walk` is breadth-first, so it reaches a
    `with` header's named expression *after* the statements in that header's
    own body, and it reaches a body's `if` branch before the statements that
    precede it. Reading the first maximal entry therefore picked whichever
    store the traversal happened to visit first, which is a property of the
    walk and not of the program:

        with (cs := contextlib.suppress(AssertionError)):
            cs = contextlib.nullcontext()
            assert x != 1
        with cs:
            assert x != 2

    `cs = nullcontext()` is written *after* the walrus, so `cs` is the
    `nullcontext` and the second assert is live. Resolving the tie by walk
    position reads the walrus instead, resurrects the stale suppressor, and
    reports that live assert defeated.

    A write only counts as *last* if it has run. An unconditional store has.
    A conditional one has only if it is the settled in-body store
    :func:`_store_is_settled_before` recognises -- written in a `with` body
    that has already completed by the time the queried header is read. A
    conditional store inside an `if`, a loop or a `try` may never run, so it
    cannot be called the value in force and the tie stays undecided.

    Returning ``None`` -- no single last write, or the last write is one that
    may not have run -- declines the name rather than guessing. The caller
    reports that as a defeat, the safe side (#308 criterion 1).
    """

    def position(node):
        return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))

    def is_conditional(entry):
        # The binding table records `(statement, value, conditional)` triples;
        # `_assigned_suppressors`' raw table records `(statement, value)` pairs
        # and carries no flag. A pair is treated as *conditional* here, which
        # is the conservative reading in the one direction that matters: it
        # cannot be called a store that provably ran, so it can neither win a
        # tie outright nor license the fallback to an earlier write. A raw tie
        # is therefore declined, which is the right answer -- the raw table is
        # consulted for *which* right-hand side a read sees, and two stores in
        # one block give it nothing to choose between.
        return True if len(entry) <= 2 else entry[2]

    latest = max(position(entry[0]) for entry in tied)
    winners = [entry for entry in tied if position(entry[0]) == latest]
    if len(winners) != 1:
        return None
    winner = winners[0]
    if not is_conditional(winner):
        return winner
    if (
        orders is not None
        and index is not None
        and function is not None
        and _store_is_settled_before(winner, orders, index, function)
    ):
        return winner
    # The last write has not run at this position -- the header being resolved
    # is the very statement whose body contains it:
    #
    #     with (cs := contextlib.suppress(AssertionError)):
    #         cs = contextlib.nullcontext()   # has not run yet
    #         assert x != 1
    #
    # The value at THIS header is still the walrus. So the tie falls back to
    # the last write that *has* run, which is what the read actually sees.
    #
    # The fallback is only sound when an unconditional store is in the tie --
    # something that provably ran on every path. With no such store, every
    # member may not have run and the name genuinely is one of several values:
    #
    #     for item in items:
    #         first = contextlib.suppress(AssertionError)
    #     else:
    #         first = contextlib.nullcontext()
    #     with (cs := first):
    #         assert x != 1
    #
    # Exactly one of the two bodies runs, and which one is an input. Measured on
    # CPython 3.12.14 the assert is swallowed when the `for` body ran and fires
    # when the `else` did, so the interpreter disagrees with itself and no
    # single verdict is right. The name is declined, which reports a defeat --
    # the safe side (#308 criterion 1). Falling through to whichever store is
    # written later would pick the `nullcontext` by source position and report
    # that live assert enforced, which is the damaging direction.
    if not any(not is_conditional(entry) for entry in tied):
        return None
    ran = [
        entry
        for entry in tied
        if not is_conditional(entry)
        or (
            orders is not None
            and index is not None
            and function is not None
            and _store_is_settled_before(entry, orders, index, function)
        )
    ]
    if not ran:
        return None
    ran_latest = max(position(entry[0]) for entry in ran)
    survivors = [entry for entry in ran if position(entry[0]) == ran_latest]
    return survivors[0] if len(survivors) == 1 else None


def _readable_store_value(value, bound):
    """The store's value, if it is a *suppressor candidate* this function owns.

    This resolver answers "is the name in force a readable suppressor?", so it
    must only ever hand back a value the suppressor machinery can read. That is
    not every store value, and #367's tie branch is where the difference bites:

    #359 records a carrier (`import ... as cs`, `def cs`, `class cs`) as a
    plain ``str`` runtime kind -- "module"/"function"/"type" -- deliberately, so
    the value stays attached to the real statement and containment and
    ordering stay answerable. A carrier is *not* a suppressor and not a
    readable right-hand side either, and `_entry_is_dead` is the rule that
    answers it, by reading the ``str``. Returning one from here would put a
    bare ``"module"`` in front of the alias machinery as though it were a call.

    The failure that produced is measured. A carrier inside the header's own
    scope has already run by the time the name is entered:

        with (cs := suppress(AssertionError)):
            import os as cs
        with cs:
            assert x != 1        # TypeError: 'module' object ...

    Both stores sit in one top-level statement, so they share a binding order
    and this tie branch answers with the conditional one. Returning the raw
    ``"module"`` skipped the entry for *any* readable suppressor -- the tie had
    already retired the `suppress` -- and reported the assert `enforced`,
    certifying an unreachable contract as load-bearing. Base answers `False`
    here, so the tie branch is what introduced it.

    So: anything that is not a value the suppressor rules can read answers
    ``None``, which is this function's existing "not a suppressor" answer, and
    leaves the carrier to ``_entry_is_dead``. An unreadable right-hand side and
    a carrier are different things that happen to share an answer here, which
    is the safe one -- neither can be claimed harmless.
    """
    if isinstance(value, str):
        return None
    return value if _is_readable_suppressor(value, bound) else None


def _binding_order(function, statement):
    """Where ``statement`` sits among the function's top-level statements.

    This is the sort key for "which store runs last". A statement written
    directly in the body has its own index. A nested one has none, so it is
    ordered by the top-level statement that contains it -- the earliest point
    at which it can possibly have run, and the only position that is true on
    every path. Two stores inside one top-level statement therefore compare
    equal, which is fine: they are in the same block, and if both are
    conditional the name is already ambiguous by the caller. Callers that
    need to break the tie use ``max`` over this key -- see
    :func:`_last_store_before` -- and must not read it as source order, since
    equal keys carry no ordering information at all.
    """
    for index, top in enumerate(function.body):
        if top is statement:
            return index
    return next(
        index
        for index, top in enumerate(function.body)
        if any(child is statement for child in ast.walk(top))
    )


def _walrus_is_conditional(function, walrus):
    """Is this assignment expression reachable on only some paths?

    An ``ast.NamedExpr`` never appears directly in the function body, so
    ``statement not in function.body`` would classify every walrus as
    conditional. That is right for ``if flag: (cs := suppress(...))`` and
    wrong for a walrus written in a top-level ``with`` header, which is
    evaluated on every path that reaches that ``with``.

    The question is therefore asked of the blocks *inside* the top-level
    statement that contains the walrus. Only the blocks that can *skip* the
    walrus count: ``if``, the loops, ``try`` and a conditional expression. A
    ``with`` header is not one of them -- its item expressions are evaluated
    to build the context manager before the body is entered, so a walrus in
    one runs on every path that reaches the header.

    That keeps two successive top-level ``with`` headers from looking like two
    *competing* bindings of one name, which would make the second header's
    value ambiguous (#308) instead of letting it supersede the first.
    """
    for top in function.body:
        if top is walrus or any(child is walrus for child in ast.walk(top)):
            return _walrus_skipped_by_a_branch(top, walrus)
    return True


def _walrus_skipped_by_a_branch(statement, walrus):
    """Is ``walrus`` inside a block of ``statement`` that can skip it?"""
    for block in ast.walk(statement):
        if not isinstance(block, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.IfExp)):
            continue
        if any(child is walrus for child in ast.walk(block)):
            return True
    return False


def _is_readable_suppressor(value, bound):
    """Is ``value`` a right-hand side this check can read as a suppressor?

    A non-``Call`` right-hand side -- a constant, a subscript, another name --
    is never a readable suppressor, and neither is a call that fails the
    suppressor predicate. Both are recorded as *not* a suppressor so that the
    store still supersedes an earlier binding of the same name.
    """
    return isinstance(value, ast.Call) and _is_suppression_call(value, bound)


def _deref_alias(value, raw_values, orders=None, index=None, target=None, function=None):
    """The value an assignment expression's right-hand side stands for.

    A binding can take its value from another name rather than from a call:

        base = contextlib.suppress(AssertionError)
        y = (cs := base)         # `cs` is the same suppressor as `base`
        with cs:
            assert 1 == 2        # swallowed, because `cs` *is* `base`

    Recording the bare ``base`` node leaves the binding unreadable, because
    :func:`_is_readable_suppressor` accepts only an ``ast.Call``. The header
    then reported a swallowed assert as *enforced* -- the damaging direction,
    a disarmed contract certified as load-bearing (#333).

    Resolution is bounded so the walk is total. An alias with no recorded
    binding resolves to itself and stays unreadable; a cycle (``a = b; b = a``)
    is cut by the bound instead of recursing; and a name that resolves to a
    non-suppressor keeps that value, so the store still supersedes whatever it
    replaced. Only a chain that ends at a readable suppressor is reported as
    one.

    A name can be bound more than once, and the *last* binding is the one a
    later read sees:

        a = helper.make()
        a = contextlib.suppress(AssertionError)
        with (cs := a):          # `a` is the suppressor, not the first store

    Taking the first binding would resolve that to the ordinary call, leave the
    header unreadable, and report a swallowed assert as live. A name bound
    twice is a plain supersession this module already models, so the chain has
    to follow it the same way.

    The table is whole-function rather than position-filtered, which is what
    lets a two-hop chain resolve at the binding that performs it. Most of the
    shapes that over-approximates fail loudly rather than silently: binding an
    alias *after* the header that reads it raises ``NameError`` on entry, and
    a binding overwritten with a non-context manager raises ``TypeError``.
    Neither is a swallowed assert, so neither is the damage this direction
    causes.

    "Most" is doing real work, and the exception is ``del``. A ``del`` is a
    value-less store, so :func:`_raw_store_values` records nothing for it and
    this walk never sees it -- but the name's *earlier* store is still in the
    table, so a name that was a suppressor before its ``del`` is still
    resolved to that suppressor. #336's :func:`_entered_name_is_dead` reads
    the ``del`` and reports the same entry unreachable, so both rules fire and
    the surviving verdict depends on whether a later store exists. The
    answers are correct either way, but the route differs, and nothing here
    says which rule decided. Filed as #344; it predates this change and is not
    fixed by it.
    """
    seen = set()
    # A store whose right-hand side names the name it binds is a *self-alias*:
    #
    #     cs = contextlib.suppress(AssertionError)
    #     with (cs := cs):       # `cs` still holds the suppressor
    #         assert x != 1     # swallowed
    #
    # Following the name from here lands on the very store being resolved, so
    # the walk would hand back the same name and the header would read as
    # unreadable. The binding in force *before* this statement is the answer,
    # which is the same resolution with the store being resolved excluded.
    # That exclusion is what `origin` carries below.
    current = value
    origin = None
    for _ in range(_ALIAS_CHAIN_LIMIT):
        if not isinstance(current, ast.Name):
            return current
        entries = raw_values.get(current.id)
        if not entries:
            return current
        # When the walk is standing on the very store it is resolving, that
        # store is excluded so the chain falls through to the binding that
        # preceded it. Otherwise the entry that supplied the current value is
        # remembered, so a later return to this name excludes the right one.
        revisiting = current.id in seen
        if not revisiting:
            seen.add(current.id)
        chosen = _last_store_before(
            entries,
            orders,
            index,
            origin if current.id == target or revisiting else None,
            function,
        )
        if origin is None:
            origin = next((e for e in entries if e[1] is chosen), None)
        current = chosen
        if revisiting:
            return current
    return current


def _last_store_before(entries, orders, index, exclude=None, function=None):
    """The right-hand side a read at ``index`` would actually see.

    ``raw_values`` is deliberately whole-function, so ``entries[-1]`` is the
    *last* store of the name anywhere in the function. That is the wrong
    answer for a read that happens before that store:

        first = contextlib.suppress(AssertionError)
        with (cs := first):        # here `first` IS the suppressor
            assert x != 1          # swallowed
        first = contextlib.nullcontext()

    The interpreter swallows the assert, because at the moment the header
    reads ``first`` the rebind has not run. Taking ``entries[-1]`` resolves
    the header to the ``nullcontext()`` store instead, which is not a
    suppressor, so the header looks unreadable and a swallowed assert is
    reported live -- the damaging direction (#348).

    The same mistake makes a self-alias resolve to itself:

        cs = contextlib.suppress(AssertionError)
        with (cs := cs):           # `cs` is the suppressor it already holds
            assert x != 1

    Here the read and the store share a name, so the only store that
    *precedes* the header is the ``cs = contextlib.suppress(...)`` above it.
    Reading ``entries[-1]`` instead finds the walrus's own store, and the
    walk becomes self-referential.

    Ordering is by :func:`_binding_order`, so a store nested inside another
    top-level statement sorts at that statement's position -- the earliest
    point at which it can have run. Among the stores that are visible, the
    one with the *greatest* order is the one that has actually run last, and
    that is the entry returned.

    The pick is ``max`` over ``orders`` rather than the last entry in
    ``raw_values``, and the two are not the same list. :func:`_raw_store_values`
    accumulates with ``ast.walk``, which is *breadth*-first, so a store
    written directly in the body is recorded before a store nested in an
    earlier top-level statement. Taking the last accumulated entry therefore
    returns the nested one -- a value that is stale the moment the enclosing
    statement is not the one that ran. That is the damaging direction, and it
    is silent:

        if flag:
            first = contextlib.suppress(AssertionError)
        first = contextlib.nullcontext()      # always runs
        second = first
        with (cs := second):
            assert 1 == 2

    The interpreter is LIVE here: ``first`` is the ``nullcontext()``, which is
    a working context manager, so the assert runs and any failure escapes.
    Resolving to the nested ``suppress(...)`` instead makes the chain look
    swallowable, and the header reports a live contract as defeated.

    #367: when two stores share an order they are inside one top-level
    statement, so neither provably precedes the other and the value is
    genuinely ambiguous. Picking the first -- which is what ``max`` does --
    resolves that ambiguity by walk position, which the source does not
    justify. A ``for``/``else`` pair is the sharpest case, because exactly one
    branch runs and the walk order is fixed regardless of which:

        for i in items:
            first = contextlib.suppress(AssertionError)
        else:
            first = contextlib.nullcontext()
        with (cs := first):
            assert x != 1

    With ``items == []`` the ``else`` runs and the assert is LIVE. With
    ``items == [1]`` the body runs, the assert is swallowed. The interpreter
    disagrees with itself across the two, so no single verdict is right, and
    returning the walk-first entry -- the ``suppress(...)`` -- reports the
    ``items == []`` case as defeated. That is the damaging direction: a live
    contract certified as disarmed.

    A tie is not always ambiguous, though. A later write to a name overwrites
    an earlier one, so where a single store in the block is the last write
    that *ran*, that store is what a read at ``index`` sees:

        with (cs := contextlib.suppress(AssertionError)):
            cs = contextlib.nullcontext()      # written after the walrus
            assert x != 1
        with cs:                              # `cs` is the nullcontext
            assert x != 2

    Resolving that tie to the ``suppress(...)`` instead resurrects a stale
    suppressor and reports a live assert defeated. Only a store that *ran*
    qualifies -- an unconditional one, or the settled in-body store
    :func:`_store_is_settled_before` recognises. A conditional store inside an
    ``if`` may never run, so the tie stays undecided there.

    An undecided tie is declined with :data:`AMBIGUOUS_SUPPRESSOR` -- the same
    marker :func:`_resolve_bindings` returns for a name several conditional
    stores can reach, and the one the suppression rules already read as "may be
    any suppressor, so report a defeat" (#308 criterion 1). Returning a plain
    ``None`` would be wrong: ``None`` means "this store's right-hand side is
    not readable", and callers treat it as *not a suppressor*, which reports
    the assert live. A tie is not an unreadable right-hand side.

    Without ``orders``/``index`` -- the call sites that genuinely have no
    position to reason from -- this falls back to the whole-function last
    binding, which is the behaviour #333 originally shipped.
    """
    if orders is None or index is None:
        return entries[-1][1]
    visible = [
        entry
        for entry in entries
        if orders.get(id(entry[0])) is not None
        and orders[id(entry[0])] <= index
        and entry is not exclude
    ]
    if not visible:
        return entries[-1][1]
    latest = max(orders[id(entry[0])] for entry in visible)
    tied = [entry for entry in visible if orders[id(entry[0])] == latest]
    if len(tied) > 1:
        # #367. The same tie :func:`_resolve_bindings` handles, and the same
        # answer: a later write to a name overwrites an earlier one, so the
        # value a read at ``index`` sees is the last write that *ran*. The
        # two functions cannot disagree about it, so both call
        # :func:`_latest_write_in_block`; where that cannot name a single
        # store, the name is declined rather than picked by walk position.
        winner = _latest_write_in_block(tied, orders, index, function)
        return AMBIGUOUS_SUPPRESSOR if winner is None else winner[1]
    return tied[0][1]


def _raw_store_values(function):
    """Every store's right-hand side, keyed by the name it binds.

    The point is to have the whole table available *before* any binding is
    resolved, so an assignment expression can follow a name whose own store
    ``ast.walk`` has not reached yet. ``ast.walk`` is breadth-first, so a
    ``with`` header is visited before stores that precede it in source:

        first = contextlib.suppress(AssertionError)
        second = first
        with (cs := second):        # `second` is not recorded at this point

    Resolving against the table as it fills would leave that stuck at an
    unrecorded name, which is unreadable, and the assert it guards would be
    reported live while the interpreter swallows it.

    Only the forms that carry a *value* are listed. A ``with ... as``, an
    ``except ... as``, a ``del`` and a ``match`` capture bind a name without
    one, and :func:`_deref_alias` stops on a missing entry exactly as it does
    for an unbound name -- which is right, because a later store of that name
    is what retires the carried value anyway.

    #370. A ``for`` target was in that list too, and it should not have been.
    A loop target does receive a value -- the current element of the iterable --
    so recording nothing left the name with no entry to resolve against and
    every ``for``-bound suppressor invisible:

        for cs in [contextlib.suppress(AssertionError)]:
            with cs:
                assert 1 == 2        # reported enforced; swallowed

    The value recorded is the iterable's element only when the iterable is a
    literal with **exactly one** element, which is the only case where every
    iteration binds the same thing and the read is unambiguous.

    A multi-element literal is deliberately left unrecorded, and that limit is
    the point rather than an oversight. Inside the body the target holds a
    *different* value per iteration, so for

        for cs in (suppress(AssertionError), nullcontext()):
            with cs:
                assert 1 == 2

    the first iteration swallows the assert and the second does not. One static
    answer cannot be right for both, and guessing either way is a coin flip
    that lands on a false verdict half the time. Leaving it unreadable keeps
    the assert *enforced*, which is the safe direction: an over-cautious
    sentinel still reports the contract it was asked to protect. #385 measured
    this exact trade -- indexing to one element or the other simply moves a
    damaging cell rather than removing it -- and its after-loop variant, where
    the *last* element is the right answer, needs the in-body/after-loop
    distinction designed rather than guessed at. Neither is settled here.

    Each entry is the ``(statement, right-hand side)`` pair rather than the
    right-hand side alone. The statement is what :func:`_last_store_before`
    orders by, so a read resolves against the stores that precede it and not
    against stores the interpreter has not executed yet (#348).
    """
    raw = {}
    for statement in ast.walk(function):
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        # `AnnAssign` without a value (`cs: contextlib.suppress`) binds
        # nothing, so it is excluded here exactly as it is in the recording
        # walk. The explicit `is not None` keeps `and`/`or` precedence obvious
        # rather than relying on how the two combine.
        elif (
            isinstance(statement, ast.AnnAssign)
            and statement.value is not None
            or (isinstance(statement, ast.NamedExpr))
        ):
            targets, value = [statement.target], statement.value
        # #370. A `for` target binds the current element of the iterable, so it
        # does carry a value. Recording it is only sound when the iterable is a
        # literal with a single element: then every iteration binds the same
        # object and a later read is unambiguous. A multi-element literal is
        # left unrecorded, because inside the body the target differs per
        # iteration and no single element is the answer -- see the docstring.
        elif isinstance(statement, ast.For):
            element = _single_loop_element(statement.iter)
            if element is None:
                continue
            # `ast.For.target` is a single node, not a list of them.
            targets, value = [statement.target], element
        else:
            continue
        for name in _store_target_names(targets):
            raw.setdefault(name, []).append((statement, value))
    return raw


def _single_loop_element(iterable):
    """The one element a ``for`` over ``iterable`` binds, or ``None``.

    #370. Only a literal tuple or list with exactly one element qualifies. That
    is the shape where every iteration binds the same object, so recording the
    element is sound regardless of where the later read sits -- inside the body
    or after the loop.

    Everything else returns ``None`` and the ``for`` target stays unrecorded,
    which leaves the name unreadable rather than misread. In particular a
    multi-element literal is refused even though its last element is the right
    answer for a header *after* the loop, because the same recorded value is
    also what an in-body header would resolve to, and there it is wrong. #385
    measured that trade on a real head: picking an index moves a damaging cell
    rather than removing it.
    """
    if not isinstance(iterable, (ast.Tuple, ast.List)):
        return None
    if len(iterable.elts) != 1:
        return None
    return iterable.elts[0]


def _loop_element_for_read_after(iterable):
    """The element a ``for`` target holds once the loop has finished, or ``None``.

    #385. :func:`_single_loop_element` refuses a multi-element literal because a
    header *inside* the body sees a different value on each iteration, and no
    one element answers for all of them. A header *after* the loop is the
    opposite case and the refusal is wrong there:

        for cs in (contextlib.nullcontext(),
                   contextlib.suppress(AssertionError)):
            pass
        with cs:                 # `cs` is the LAST element, the suppressor
            assert x == 99       # executed: swallowed

    After the last iteration the target holds the final element, so that one is
    what any later read sees -- and it is the same answer on every path, which
    is the property the in-body case lacks. Executed, the analyzer above
    reported this header ``enforced``: a disarmed contract certified as
    load-bearing (#308 criterion 1).

    Refusing it was the safe direction for the *in-body* question and the
    damaging one here, so the two are separated by position rather than
    averaged. The value is only used by a caller that has already established
    the header is positioned after the loop; :func:`_single_loop_element`
    remains what the in-body path consults, so the multi-element limit the
    sentinel suite pins is untouched.

    A non-literal iterable still returns ``None``: which element survives is
    then a runtime property of the object, and declining leaves the contract
    ``enforced`` rather than guessing.
    """
    if not isinstance(iterable, (ast.Tuple, ast.List)):
        return None
    if not iterable.elts:
        return None
    return iterable.elts[-1]


def _loop_target_bindings_after_loop(function, header, bound=None, raw_values=None):
    """Bindings a *completed* loop leaves in force at a header read after it.

    #385. :func:`_loop_target_bindings` answers for a header written **inside** a
    loop's body, where the target holds a different value on each iteration.
    Its refusal to guess is right there and is left untouched. This function
    answers the different question raised by a header positioned **after** the
    loop has run to exhaustion:

        for cs in (contextlib.nullcontext(),
                   contextlib.suppress(AssertionError)):
            pass
        with cs:                 # `cs` is the LAST element, the suppressor
            assert x == 99       # executed: swallowed

    Once the last iteration is done the target holds the final element, and
    that is the same answer on every path -- the property the in-body case
    lacks. Executed, the analyzer reported this header ``enforced``: a disarmed
    contract certified as load-bearing (#308 criterion 1).

    Three conditions gate the answer, and all three are required:

    * the loop must **precede** the header in the block that sequences them, so
      the target has reached its final value rather than an in-flight one;
    * the loop must be able to run to **exhaustion**. A ``break`` leaves the
      target on whichever element was current, not the last one, so a loop
      containing one is declined;
    * the loop **body must not rebind the name**. The body runs after the
      target on every pass, so a rebound name is whatever the body's own store
      left behind and the iterable's final element says nothing about it.

    A ``return`` in the body is *not* a reason to decline. It stops the loop on
    the header's own path, so the header is only ever reached when the loop
    finished -- which is exactly the case being claimed.

    A non-literal or empty iterable is declined too: which element survives is
    then a property of the object rather than of the syntax, and an empty one
    binds nothing at all (CPython raises ``UnboundLocalError`` at the header
    rather than entering a context). Both leave the contract ``enforced``,
    the safe direction.

    The loop's own binding is offered only while nothing later has overwritten
    the name. :func:`_raw_store_values` declines to record a multi-element loop
    at all, so a store that follows one is absent from the table too; the
    positional check below is therefore made against every recorded store of
    the name rather than against the table's completeness. That is what keeps
    a later ``cs = nullcontext()`` from inheriting the loop's suppressor.
    """
    if function is None:
        return {}
    if raw_values is None:
        raw_values = _raw_store_values(function)
    header_order = _binding_order(function, header)
    resolved = {}
    for node in ast.walk(function):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        if not _loop_completes_before(node, header, function):
            continue
        loop_order = _binding_order(function, node)
        for name in _store_target_names([node.target]):
            if _loop_body_rebinds_the_name(node, name):
                continue
            element = _loop_element_for_read_after(node.iter)
            if element is None:
                continue
            if _name_rebound_after_loop(name, loop_order, header_order, raw_values, function):
                continue
            if _store_precedes_header_in_shared_block(name, node, header, raw_values, function):
                continue
            # A readable element stands in for the name's binding. An
            # unreadable one is recorded as `_NOT_A_SUPPRESSOR` so the loop
            # still retires a suppressor carried in from an enclosing block,
            # exactly as the in-body path does. Dropping it instead would let
            # a stale carried value outlive the loop that overwrote it.
            resolved[name] = (
                element if _is_readable_suppressor(element, bound) else _NOT_A_SUPPRESSOR
            )
    return resolved


def _loop_completes_before(loop, header, function):
    """Is ``header`` read only after ``loop`` has run to exhaustion?

    A header in the loop's own *body* is the in-body question and is answered
    by :func:`_loop_target_bindings`; a loop that merely encloses the header
    has not finished when the header is evaluated, so it is excluded here
    rather than consulted twice.

    The ``else`` arm was excluded alongside the body and that was wrong, and
    measurably so. An ``else`` arm runs *after* the iterable is exhausted --
    that is the only way to reach it without a ``break`` -- so the target
    holds the final element there for the same reason a header after the loop
    does:

        for cs in (nullcontext(), suppress(AssertionError)):
            pass
        else:
            with cs:                 # `cs` IS the last element
                assert x == 99       # executed: swallowed

    Executed, that assert never fires; reported ``enforced`` it certifies a
    disarmed contract as load-bearing. Grouping the arm with the body was a
    reading of :func:`_loop_arms_reach`, which answers "is this header in an
    arm of the loop" and cannot distinguish the two by itself. The arm is
    therefore separated here, where the question is which value the target
    holds, and it is left to :func:`_loop_target_bindings` to decline when the
    element is not a single readable one.

    ``break`` is the other exclusion. It leaves the loop part-way, so the
    target holds the element that was current at the break rather than the
    last one, and reading the last would be a guess about which iteration
    stopped the loop.

        for cs in (nullcontext(), suppress(AssertionError)):
            if flag:
                break
        with cs:                 # the element current at the break, not the last
            assert x != 1

    Executed, the two values of ``flag`` disagree -- swallowed on one, live on
    the other -- so the loop is declined and the header stays ``enforced``,
    which is the safe direction. ``continue`` is deliberately not treated the
    same way: it still ends on the final element, so the answer it leaves
    behind is the one being claimed.
    """
    if _header_in_loop_body(loop, header):
        return False
    # A `break` anywhere in the body means the `else` arm is skipped on that
    # path, but a header *inside* the arm is only ever reached when the loop
    # ran to exhaustion -- so the arm is not declined for the break. The
    # position check below is what places it after the loop.
    # `_breaks_own_loop` answers for a NON-loop node: handed a loop it reads
    # only the `else` arm, because a nested loop's own `break` belongs to that
    # nested loop. So it is asked about each statement of this loop's body,
    # which is the shape it was written for.
    if not _header_in_loop_else(loop, header) and any(
        _breaks_own_loop(child) for child in loop.body
    ):
        return False
    if _header_in_loop_else(loop, header):
        return True
    return _precedes_in_shared_block(loop, header, function)


def _header_in_loop_body(loop, header):
    """Is ``header`` inside ``loop``'s own ``body``?"""
    if not isinstance(loop, (ast.For, ast.AsyncFor)):
        return False
    return any(child is header for statement in loop.body for child in ast.walk(statement))


def _header_in_loop_else(loop, header):
    """Is ``header`` inside ``loop``'s ``else`` arm?"""
    if not isinstance(loop, (ast.For, ast.AsyncFor)):
        return False
    return any(child is header for statement in loop.orelse for child in ast.walk(statement))


def _precedes_in_shared_block(first, second, function):
    """Does ``first`` come before ``second`` in the innermost block holding both?

    Top-level statement index is the module's usual order key, but it is too
    coarse here: a loop and a header written inside one ``if`` share that index
    by construction, which would decline the ordinary in-one-block reading:

        if flag:
            for cs in (contextlib.nullcontext(), suppress(AssertionError)):
                pass
            with cs: ...

    The block that actually sequences the two is the innermost one enclosing
    both, so that is the list the positions are read from. Ordering is compared
    there and nowhere else, which is what makes a loop written *after* the
    header keep declining.

    """
    first_parent = _enclosing_node(function, first)
    second_parent = _enclosing_node(function, second)
    if first_parent is None or first_parent is not second_parent:
        return False
    body = _statements_of_block(first_parent)
    first_position = _position_of_child(body, first)
    second_position = _position_of_child(body, second)
    if first_position is None or second_position is None:
        return False
    return first_position < second_position


def _enclosing_node(function, target):
    """The statement whose body directly holds ``target``, or ``None``."""
    for parent in ast.walk(function):
        for child in ast.iter_child_nodes(parent):
            if child is target:
                return parent
            if any(node is target for node in ast.walk(child)):
                break
    return function if target in getattr(function, "body", []) else None


def _statements_of_block(block):
    """The statement list a node executes, with a function's own body included.

    :func:`_block_body` opens the branch constructs (``if``/``try``/``match``/
    loop) because the rules that use it care which *branch* a store sits in.
    This question is different: it asks what order two siblings are evaluated
    in, and a function's top-level body is the block in which a loop and a
    trailing ``with`` are siblings. That case has no entry in
    :func:`_block_body`, so it is added here rather than widening that helper
    and changing the branch test every other caller depends on.
    """
    if isinstance(block, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return list(block.body)
    return _block_body(block)


def _position_of_child(statements, target):
    """Index of the direct child of ``statements`` that holds ``target``."""
    for index, child in enumerate(statements):
        if child is target or any(grandchild is target for grandchild in ast.walk(child)):
            return index
    return None


def _name_rebound_after_loop(name, loop_order, header_order, raw_values, function):
    """Does a later store of ``name`` supersede ``loop_order`` before the header?

    The check is positional rather than a search for "the last recorded store",
    because :func:`_raw_store_values` records a multi-element loop as *nothing*.
    A store written between the loop and the header is absent from the table
    whenever the loop itself is unrecorded, so a table-completeness argument
    would read the loop as the final write and hand the header a stale
    suppressor:

        for cs in (suppress(AssertionError), nullcontext()):
            pass
        cs = nullcontext()
        with cs:                 # the nullcontext, not the loop's suppressor
            assert x != 1

    The same check has to run over the *statement list* that sequences the
    header, because :func:`_binding_order` cannot separate two stores that
    share a top-level statement -- it maps both to the same index and says so
    in its own docstring. A store written in a loop's ``else`` arm is the
    exact case:

        for cs in [contextlib.suppress(AssertionError)]:
            pass
        else:
            cs = contextlib.nullcontext()   # same top-level index as the loop
            with cs:                        # the nullcontext wins
                assert x != 1

    Read through the order key alone the arm's store compared *equal* to the
    loop, the strict ``<`` never fired, and the loop's suppressor was layered
    over a name the arm had already rebound -- reporting the assert defeated
    and dropping a contract that really fires. :func:`_precedes_in_shared_block`
    is what carries the ordering the order key cannot express.
    """
    for statement, _ in raw_values.get(name, []):
        order = _binding_order(function, statement)
        if loop_order < order <= header_order:
            return True
    return False


def _store_precedes_header_in_shared_block(name, loop, header, raw_values, function):
    """Does a store of ``name`` sit between ``loop`` and ``header`` in one block?

    :func:`_name_rebound_after_loop` asks the same question through
    :func:`_binding_order`, which maps every store inside one top-level
    statement to the same index and explicitly documents that equal keys carry
    no ordering. A loop and a store in its ``else`` arm are exactly that pair,
    so the arm's store is invisible to the order key and the loop's element
    would be layered over a name already rebound:

        for cs in [contextlib.suppress(AssertionError)]:
            pass
        else:
            cs = contextlib.nullcontext()
            with cs:                 # the nullcontext, not the loop's element
                assert x == 99

    Executed, that assert fires; reporting it defeated drops a load-bearing
    contract. The check is the positional one, over the block that actually
    sequences the two, so it holds for an arm and for an ordinary block alike.

    The store is declined whether or not it is *guaranteed* to have run, and
    that is the same safe direction the ambiguity rule takes. A store inside an
    ``if`` between the loop and the header may not have run, in which case the
    loop's element really is in force -- so the loop's value is right on that
    path. But the store's value is right on the other, and the two disagree:

        for cs in (nullcontext(), suppress(AssertionError)):
            pass
        if flag:
            cs = nullcontext()
        with cs:                 # nullcontext when flag, suppressor otherwise
            assert x != 1

    Executed, the assert fires when ``flag`` is true and is swallowed when it
    is false, so no single verdict is right and the loop is declined. Layering
    the loop's element over the store answers ``enforced`` and is a false LIVE
    on the ``flag=True`` path.

    Every recorded store of the name is considered rather than only the last,
    because :func:`_raw_store_values` records a multi-element loop as nothing
    and the table is therefore not a complete account of the name's writes.
    """
    for statement, _ in raw_values.get(name, []):
        if statement is loop:
            continue
        if _precedes_in_shared_block(statement, header, function):
            return True
    return False


def _aliased_suppressions(node, function, bound):
    """Suppressors reached through a bare ``Name`` in a ``with`` header.

    ``contextlib.suppress(AssertionError)`` is an expression, and an expression
    can be given a name and entered later:

        cs = contextlib.suppress(AssertionError)
        with cs:
            assert 1 == 2

    This is the *silent* form of the defeat. Unlike the ``__enter__()``
    dunder -- which returns ``None`` and so raises ``TypeError`` on entry,
    failing loudly -- entering the alias object directly really does swallow the
    assertion failure and the test stays green. It is therefore the more
    damaging of the two, and matching only the dunder would have left the worse
    spelling open.

    Only names bound by an assignment that *precedes* the ``with`` qualify, and
    only when that assignment's right-hand side is a *readable* suppressor. A
    name from an outer scope is deliberately not followed: assuming an
    arbitrary call returns a suppressor would report live asserts as dead on
    every context manager this check cannot trace.

    An assignment expression in the header is the same defeat with the binding
    folded into the ``with`` itself:

        with (cs := contextlib.suppress(AssertionError)):
            assert 1 == 2

    A ``NamedExpr`` is not an ``ast.Assign``, so ``_assigned_suppressors`` used
    to skip it entirely, and the header no longer holds a bare ``Name`` for the
    resolution rule to read -- the suppressor sat in a node shape nothing else
    inspected. Executed, the assertion failure really is swallowed, so this
    was the damaging direction: a live defeat reported as enforced. #324
    records the ``NamedExpr`` in :func:`_assigned_suppressors` so the *later*
    header that re-enters the name resolves it too; the inline classification
    below is what handles the header that performs the binding.

    The right-hand side is still gated on :func:`_is_suppression_call`, and that
    guard is load-bearing rather than a redundancy. A zero-argument call such as
    ``nullcontext()`` or ``helper.make()`` reads as the unreadable case
    ``BaseException`` in :func:`_suppression_names`, and ``BaseException`` *does*
    catch ``AssertionError`` -- so appending every walrus unconditionally would
    report both of those live context managers as defeats. The mutation matrix
    in the sentinel suite pins that difference.
    """
    by_index, raw_values = _assigned_suppressors(function, bound)
    # The raw table is keyed by name, so the statement that owns each entry is
    # what positions it. #348: the header must resolve against the stores that
    # ran before it, not against whichever store of that name is written last
    # somewhere else in the function.
    orders = {
        id(statement): _binding_order(function, statement)
        for entries in raw_values.values()
        for statement, _ in entries
    }
    # A name is bound only by the assignments that run *before* the `with`.
    # Each `with` header is therefore read against the bindings in force at
    # ITS OWN position, not against the bindings that were live when the
    # enclosing top-level statement began.
    #
    # The distinction is the same-block defeat:
    #
    #     for a in rows:
    #         cs = contextlib.suppress(AssertionError)
    #         with cs:                  # really does swallow
    #             assert 1 == 2
    #
    # The store and the `with` share one top-level statement, so reading the
    # header before applying the statement's bindings reported that assert as
    # load-bearing. Executed, the failure is swallowed and the test stays
    # green -- the one direction this module must never get wrong.
    #
    # The converse must keep working, and it is the reason the position is
    # compared rather than just "after the statement":
    #
    #     if flag:
    #         with cs:                  # NameError: `cs` is not bound yet
    #             assert 1 == 2
    #         cs = contextlib.suppress(AssertionError)
    #
    # That block raises on entry, so it is a live contract.
    bound_so_far = {}
    entered = []
    for index, statement in enumerate(function.body):
        for header in ast.walk(statement):
            if not isinstance(header, (ast.With, ast.AsyncWith)):
                continue
            if not _encloses(header, node):
                # A `with` that does not enclose the queried assert says
                # nothing about the context that assert runs under. Collecting
                # every header in the function would answer for a later `with`
                # while being asked about an earlier one, and two asserts in
                # the same block -- one before the store and one after it --
                # would both be reported defeated.
                continue
            live = (
                bound_so_far
                if header is statement
                else _bindings_before(
                    header,
                    statement,
                    bound_so_far,
                    by_index.get(index, {}),
                    function,
                    bound,
                    raw_values,
                )
            )
            # #385. A header that reads a loop target *after* the loop has
            # finished is not the in-body question `_bindings_before` asks,
            # and the raw table cannot answer it: `_raw_store_values` records a
            # multi-element loop as nothing at all, so the name has no entry to
            # resolve against and the header reads as an ordinary unbound name.
            # The loop's own binding is layered on for that case only, and
            # `live` is copied rather than mutated so the hand-off to the next
            # top-level statement is unaffected.
            after_loop = _loop_target_bindings_after_loop(function, header, bound, raw_values)
            if after_loop:
                live = {**live, **after_loop}
            for item in header.items:
                expression = item.context_expr
                # `with (cs := contextlib.suppress(AssertionError)):` binds the
                # name and enters it in one node, so the alias is classified
                # from its right-hand side rather than from `bound_so_far`. The
                # right-hand side need not be a call at all -- `with (cs := 1):`
                # is legal and fails loudly on entry with `TypeError` -- so the
                # node type is checked before the suppressor predicate, which
                # reads `call.func` and would otherwise raise.
                walrus = expression if isinstance(expression, ast.NamedExpr) else None
                if walrus is not None:
                    # #333: the right-hand side may be a NAME bound to a
                    # suppressor rather than a call:
                    #
                    #     base = contextlib.suppress(AssertionError)
                    #     with (cs := base):
                    #         assert 1 == 2      # swallowed by `base`
                    #
                    # Reading the bare `base` node finds no `ast.Call` and the
                    # header is reported live. Resolving the name the same way
                    # the re-entering path does means the two halves cannot
                    # disagree about what `cs` carries.
                    #
                    # `live` is deliberately not used here: it holds only names
                    # that already resolved to a suppressor, so an alias whose
                    # own name is not itself a resolved suppressor is invisible
                    # there. The raw table is the complete set of right-hand
                    # sides, which is what makes a two-hop chain resolvable at
                    # the header performing the binding.
                    resolved = _deref_alias(
                        walrus.value,
                        raw_values,
                        orders,
                        _binding_order(function, walrus),
                        walrus.target.id if isinstance(walrus.target, ast.Name) else None,
                        function,
                    )
                    # #367: the alias walk can land on `AMBIGUOUS_SUPPRESSOR`
                    # when the name it followed is bound twice in one top-level
                    # statement, so the source does not say which store the
                    # read sees. `_is_readable_suppressor` accepts only an
                    # `ast.Call`, so the marker is tested before it -- otherwise
                    # it reads as "not a suppressor", the header looks live, and
                    # a `for`/`else` tie certifies a live assert as defeated.
                    if resolved is AMBIGUOUS_SUPPRESSOR:
                        entered.append(AMBIGUOUS_SUPPRESSOR)
                    if _is_readable_suppressor(resolved, bound):
                        entered.append(resolved)
                # #367: `_NOT_A_SUPPRESSOR` is a *record*, not a value. It says
                # the name is bound here and carries nothing this module can
                # call a suppressor, which is what keeps the previous
                # position's suppressor from being read forward. Appending it
                # would hand `_suppression_names` an object with no `args`.
                if (
                    isinstance(expression, ast.Name)
                    and expression.id in live
                    and live[expression.id] is not _NOT_A_SUPPRESSOR
                ):
                    entered.append(live[expression.id])
                if not isinstance(expression, ast.Call) or not isinstance(
                    expression.func, ast.Attribute
                ):
                    continue
                if expression.func.attr != "__enter__":
                    continue
                value = expression.func.value
                if isinstance(value, ast.Name) and value.id in live:
                    entered.append(LOUD_DUNDER)
        # `assigned[index]` is already the COMPLETE set in force *at* that
        # index: `_assigned_suppressors` filters stores to `order <= index`, so
        # it includes everything written by every earlier statement, and it
        # records a store that deliberately resolves to a non-suppressor
        # (#367's `_NOT_A_SUPPRESSOR`). Advancing to that set here rather than
        # to the previous index's is what stops a superseded suppressor from
        # being read forward:
        #
        #     with (cs := contextlib.suppress(AssertionError)):
        #         cs = contextlib.nullcontext()
        #         assert x != 1
        #     with cs:                  # `cs` is the nullcontext
        #         assert x != 2
        #
        # At the index of the second `with`, `assigned` records `cs` as bound
        # and not a suppressor. The earlier index recorded the `suppress`, so
        # reading the previous index's set forward would resurrect it and
        # report a live assert defeated. The set is copied rather than
        # aliased because it is replaced, never mutated.
        #
        # The index advanced to is the *next* statement's, not this one's. A
        # header written inside this statement has not been read yet when the
        # loop reaches the end of the iteration, and a header in a later
        # statement reads the set in force once this one is done -- which is
        # exactly `assigned[index + 1]`. Taking `assigned[index]` here instead
        # would apply this statement's own stores to a header written *before*
        # them:
        #
        #     if p:
        #         with cs:                 # NameError: `cs` is not bound yet
        #             assert x != 1
        #         cs = contextlib.suppress(AssertionError)
        #         with cs:
        #             assert x != 1
        #
        # Both headers belong to the same top-level statement, so they are both
        # read while `by_index` still holds only that statement's own entry.
        # `_bindings_before` is what separates the two, and it needs
        # `bound_so_far` to be the set carried in from *earlier* statements.
        # That is why the update happens at all, and why advancing to
        # `index + 1` rather than `index` is the only reading under which
        # `bound_so_far` still means "before this statement" when the next
        # iteration's headers are read.
        # #367. The set a *later* statement's headers read is the one resolved
        # at that later index, not this one's. Advancing to the next index that
        # has an entry is what lets a store that retires a suppressor in this
        # statement's body be seen by the next statement:
        #
        #     with (cs := contextlib.suppress(AssertionError)):
        #         cs = contextlib.nullcontext()
        #         assert x != 1
        #     with cs:                  # `cs` is the nullcontext
        #         assert x != 2
        #
        # At the index of the first `with` the name really does carry the
        # suppressor, so that index records it. The next index resolves the
        # tie to the `nullcontext` and records `_NOT_A_SUPPRESSOR` for `cs`.
        # Advancing to the next *recorded* index hands that marker over, so
        # the second header does not read the stale suppressor and report a
        # live assert defeated.
        #
        # Taking the very next index unconditionally would be wrong in the
        # other direction: `assigned[i]` includes statement `i`'s own body
        # stores, and a header written *before* those must not see them.
        # `_bindings_before` is what separates headers inside one statement;
        # this hand-off is only for the statement-to-statement case.
        bound_so_far = dict(by_index.get(index, {}))
    return entered


def _encloses(header, node):
    """Is ``node`` the header itself, or somewhere inside its body?"""
    if header is node:
        return True
    return any(child is node for child in ast.walk(header))


def _bindings_before(
    header, statement, bound_so_far, own, function=None, bound=None, raw_values=None
):
    """The bindings in force at a nested ``with`` inside ``statement``.

    A store that appears *before* the nested header in the same block has run
    by the time the header is read, so this statement's bindings apply. A store
    that appears *after* it has not, and ``with cs:`` there raises `NameError`
    on entry -- loudly, not silently -- so the outer bindings stand.

    The "has a store run here" test is deliberately not a list of statement
    types. A ``match`` capture binds the name as a side effect of its clause
    matching, and the capture is not itself a statement in the block, so a
    ``with cs:`` written inside a ``case`` body has to see it:

        with (cs := suppress(AssertionError)): ...
        match flag:
            case nullcontext() as cs:   # retires the carried suppressor
                with cs:               # must read the capture, not the walrus
                    assert x != 1      # live
    """
    for block in ast.walk(statement):
        body = getattr(block, "body", None)
        if not isinstance(body, list):
            continue
        # A loop's `else` arm is a block of its own and a header written in one
        # is decided by exactly the same rule as one in the body. Reading only
        # `body` never reached the arm at all:
        #
        #     for cs in [contextlib.suppress(AssertionError)]:
        #         pass
        #     else:
        #         with cs:            # `cs` IS bound here
        #             assert x != 1  # executed: swallowed
        #
        # The arm's own earlier stores still win, exactly as they do in `body`:
        # a store before the header in the arm has run, and one after it has
        # not. `seen_store` is shared across both lists, so that falls out of
        # the ordinary rule rather than needing a second path.
        orelse = getattr(block, "orelse", None)
        block_body = body + (orelse if isinstance(orelse, list) else [])
        seen_store = False
        # Captures owned by this block have run by the time a header inside the
        # same clause body is reached, even though no store *statement* does.
        # The owner is the `ast.Match`; the block holding the header is the
        # `match_case` nested under it, so the test runs the other way round --
        # does the capturing `match` enclose this block?
        captures_scope = function if function is not None else statement
        captures = {
            name
            for name, owner in _match_capture_names(captures_scope).items()
            if any(child is block for child in ast.walk(owner))
        }
        for node in block_body:
            if node is header:
                # Nothing in this block has been stored before the header, so
                # only the bindings carried in from earlier statements apply.
                # #370. A loop body is the one exception: the loop's own target
                # is assigned before the body runs, so a header in the first
                # position already sees it.
                #
                #     for cs in [contextlib.suppress(AssertionError)]:
                #         with cs:            # `cs` IS bound here
                #             assert 1 == 2
                #
                # Executed, that assert is swallowed, so reporting it enforced
                # certifies a disarmed contract as load-bearing. The ordinary
                # same-block case is the opposite and must keep raising loudly:
                #
                #     if flag:
                #         with cs:            # NameError -- not bound yet
                #             assert 1 == 2
                #         cs = contextlib.suppress(AssertionError)
                #
                # so the exception is scoped to the loop that owns the body.
                loop_bindings = _loop_target_bindings(function, header, bound)
                if loop_bindings:
                    # The loop's own target is *one more* store in force at
                    # this point, not a replacement for what the enclosing
                    # blocks already carried in. A name the loop does not
                    # bind still has the value it had on entry, so the
                    # carried bindings are kept and the loop's are layered
                    # on top:
                    #
                    #     base = contextlib.suppress(AssertionError)
                    #     for other in [0]:
                    #         with base:            # still the suppressor
                    #             assert x != 1
                    #
                    # Substituting instead of merging would drop `base` and
                    # report that assert live.
                    # A store *in this very block*, before the header, is the
                    # latest write and supersedes the loop's target:
                    #
                    #     for cs in [contextlib.suppress(AssertionError)]:
                    #         pass
                    #     else:
                    #         cs = contextlib.nullcontext()
                    #         with cs:            # the nullcontext
                    #             assert x != 1
                    #
                    # Reading the loop's element here would report the assert
                    # defeated and drop a contract that fires. The arm's own
                    # store is read from the raw table rather than from `own`,
                    # because `own` is keyed by position in `function.body` and
                    # a store in a loop's `else` arm has no index of its own --
                    # it is filed under the loop, so `own` alone never sees it.
                    in_block = _block_store_bindings(block_body, header, raw_values, bound)
                    if in_block:
                        return {**bound_so_far, **in_block}
                    return {**bound_so_far, **loop_bindings}
                return dict(own) if seen_store or captures else bound_so_far
            if isinstance(node, ast.Assign) or (
                isinstance(node, ast.AnnAssign) and node.value is not None
            ):
                seen_store = True
    return bound_so_far


def _block_store_bindings(block_body, header, raw_values, bound):
    """Bindings written by a store that precedes ``header`` in ``block_body``.

    #370b. The ``own`` map this function otherwise reads is keyed by position in
    ``function.body``, so it answers for a header's *enclosing statement* but not
    for a store written in a block that has no index of its own -- a loop's
    ``else`` arm among them. That store is still in force at the header, so the
    raw table is consulted directly and every store appearing before the header
    in the same block contributes.

    Only stores strictly before the header count. A store after it has not run
    when the header is evaluated -- that is the ``UnboundLocalError`` case the
    caller handles by falling through to the carried bindings -- so counting it
    would report a loud failure as a swallowed one.
    """
    if not raw_values or header not in block_body:
        return {}
    cutoff = block_body.index(header)
    resolved = {}
    for entries in raw_values.values():
        for entry_statement, value in entries:
            if entry_statement not in block_body or block_body.index(entry_statement) >= cutoff:
                continue
            if not _is_store_statement(entry_statement):
                continue
            targets = (
                entry_statement.targets
                if isinstance(entry_statement, ast.Assign)
                else [entry_statement.target]
            )
            for name in _store_target_names(targets):
                readable = value if _is_readable_suppressor(value, bound) else _NOT_A_SUPPRESSOR
                resolved[name] = readable
    return resolved


def _is_store_statement(statement):
    """Is ``statement`` a binding form whose value is known?"""
    return isinstance(statement, (ast.Assign, ast.For, ast.AsyncFor)) or (
        isinstance(statement, ast.AnnAssign) and statement.value is not None
    )


def _loop_target_bindings(function, header, bound=None, raw_values=None):
    """Bindings a containing loop's own target puts in force around ``header``.

    #370. A ``for`` target is assigned before its body runs, so it is the one
    same-block store already in force at a header written in the body's first
    position. Executed both ways: the loop shape swallows the assert, while the
    ``if`` shape above raises ``UnboundLocalError`` on entry.

    Only a single-element literal iterable contributes. A multi-element or
    non-literal iterable binds a *different* value per iteration, so no single
    element is the answer and the name stays unreadable -- keeping the header
    ``enforced``, which is the safe direction. #385 measured that choosing an
    index instead moves a damaging cell rather than removing it.

    The enclosing body is found by looking for the *header* rather than for
    the list the caller is iterating, because a single-statement loop body
    puts the ``with`` directly in ``node.body`` while the caller's list is the
    enclosing block. A ``while`` body has no target, and an ``if``/``try`` body
    is not a loop body, so both return ``{}`` and the ordinary rule still
    applies.

    ``bound`` is the module's import bindings, needed to decide whether the
    element is a suppressor at all, and ``raw_values`` is the whole-function
    store table, accepted from the caller when it already has one and rebuilt
    otherwise. Only the entries belonging to this loop are read, so the lookup
    stays narrow regardless of how many stores the function contains.
    """
    if function is None:
        return {}
    if raw_values is None:
        raw_values = _raw_store_values(function)
    resolved = {}
    for node in _enclosing_loops(function, header):
        if _single_loop_element(getattr(node, "iter", None)) is None:
            continue
        for name in _store_target_names([node.target]):
            for entry_statement, value in raw_values.get(name, []):
                if entry_statement is node:
                    # The element has to clear the same readability bar every
                    # other store value clears before it can stand in for a
                    # name's binding. Without it a `nullcontext` element is
                    # handed to `_aliased_suppressions` as though it were a
                    # suppressor, and that rule appends a live `live[...]`
                    # value without re-testing it -- so
                    #
                    #     for cs in [contextlib.nullcontext()]:
                    #         with cs:
                    #             assert x != 1
                    #
                    # reports the assert defeated, certifying a contract that
                    # really does fire. `None` and an unreadable value are
                    # different, though: the name IS bound here, so the
                    # "not a suppressor" answer has to be recorded as
                    # `_NOT_A_SUPPRESSOR` rather than dropped, or a suppressor
                    # carried in from an enclosing block is carried past the
                    # loop that just overwrote it.
                    readable = value if _is_readable_suppressor(value, bound) else _NOT_A_SUPPRESSOR
                    # A later loop in this ordering is nested inside this one,
                    # so its target is the LAST write before the header runs
                    # and its element is the binding actually in force. Writing
                    # it over the outer loop's entry is what keeps
                    #
                    #     for cs in [suppress(AssertionError)]:
                    #         for cs in [nullcontext()]:
                    #             with cs:          # nullcontext, not suppress
                    #                 assert x != 1
                    #
                    # reporting the assert live when it is really swallowed.
                    resolved[name] = readable
    return resolved


def _enclosing_loops(function, header):
    """The ``for``/``async for`` loops whose body contains ``header``, outermost first.

    #370b. The binding a header sees comes from the *innermost* loop that binds
    the name, because an inner loop's target is the last write before the header
    runs:

        for cs in [contextlib.suppress(AssertionError)]:
            for cs in [contextlib.nullcontext()]:
                with cs:                     # `nullcontext` is in force
                    assert x != 1

    Matching only the direct body list answered for `for other in [0]:` bodies
    containing a *nested* header as well, so every header one block down still
    reported `enforced` when executed it is swallowed. The body is therefore
    walked, and the walk is bounded by the loop so an unrelated loop elsewhere
    in the function cannot contribute.

    Innermost-last ordering matters: a loop that does not bind the header's name
    contributes nothing, so a name bound only by the outer loop still resolves,
    while a name rebound by the inner loop resolves to the inner element. A
    loop whose iterable is undecidable is *skipped* rather than treated as a
    barrier, so it cannot retire an outer loop's readable binding -- declining
    to read a name is not the same as saying the name is not a suppressor.
    """
    loops = [
        node
        for node in ast.walk(function)
        if isinstance(node, (ast.For, ast.AsyncFor)) and _loop_arms_reach(node, header)
    ]
    if not loops:
        return []
    return _outermost_first(loops, function)


def _loop_arms_reach(loop, header):
    """Does ``header`` sit inside ``loop``'s body or inside its ``else`` arm?

    #370b. Both arms run only after the target has been assigned, so a header in
    either one reads it. The body is the common case; the ``else`` arm is easy to
    miss because it is a sibling list rather than a child:

        for cs in [contextlib.suppress(AssertionError)]:
            pass
        else:
            with cs:                 # `cs` IS bound here
                assert x != 1       # executed: swallowed

    The walk is deliberately scope-blind, the same test the body uses. Descending
    only to the loop's own scope was measured, and it is *worse*: a ``with`` in a
    nested ``def`` reads the loop variable as a free variable, so refusing to
    descend there turns a correct ``False`` into a false LIVE. The shape that
    guard was meant to protect -- a nested ``def`` rebinding the name as its own
    local -- is already correct without it, because the walk finds the nested
    ``def``'s own store first and that store is the later write. A ``while`` loop
    has no target at all, so it is never a candidate here and the ordinary rules
    still decide its headers.
    """
    if not isinstance(loop, (ast.For, ast.AsyncFor)):
        return False
    arms = [*loop.body, *loop.orelse]
    return any(child is header for statement in arms for child in ast.walk(statement))


def _outermost_first(loops, function):
    """``loops`` ordered so an enclosing loop always precedes a nested one.

    ``ast.walk`` is breadth-first, so its order is neither source order nor
    containment order, and the order decides which loop's element a name
    resolves to. Sorting on the parent chain states the intent directly.

    For a nested ``for`` this coincides with sorting on line number -- an
    enclosed statement cannot start before the header that encloses it -- so a
    mutation to line-number order is behaviourally equivalent here and is not
    covered by a test row. The parent chain is kept because it is the property
    that actually matters, and because it stays correct if a future construct
    ever moves a loop body out of source order (``while``/``else``, ``match``
    guards). Two rows do pin the *outcome* of this ordering: the inner target
    superseding the outer one, and the three-deep chain.
    """
    parents = {}
    for parent in ast.walk(function):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def depth(node):
        level = 0
        current = parents.get(id(node))
        while current is not None:
            level += 1
            current = parents.get(id(current))
        return level

    return sorted(loops, key=depth)


def _unreadable_suppressor(call, bound):
    """Is this a suppression this function cannot trace back to an import?

    ``_resolves_to`` answers "does this name *resolve* to ``contextlib.suppress``",
    which is a question about a binding that exists. A function parameter never
    has one:

        def f(suppress):
            with suppress(AssertionError):
                assert 1 == 2

    There is no import to resolve, so the dotted path is the bare name ``suppress``
    and the equality simply fails. That is a defeat by the same argument
    ``_name_catches_assertion_error`` already accepts -- an exception this check
    cannot prove harmless is treated as harmful -- applied one level up to the
    *suppressor* rather than the exception it is handed.

    Scoped deliberately to a bare ``Name``, and only when that name is one of the
    documented suppressor spellings. Every context manager wrapping a real pinned
    assert in ``test_timed_menu_milestones.py`` is called on an ``Attribute``
    (``pytest.raises(...)``, ``harness.observe()``, ...) -- measured: 40 of the
    40 ``with``-items in that file, with no other shape present. A bare name is
    therefore a shape the real file never uses, so widening the check to it
    cannot manufacture a false "unenforced" verdict on pinned sites. An
    ``Attribute`` this function cannot resolve is left alone, because
    ``pytest.raises(...)`` legitimately wraps many of the real asserts.
    """
    func = call.func
    if not isinstance(func, ast.Name):
        return False
    if func.id in bound:
        # A binding exists; `_is_suppressing_with` already decided it properly.
        return False
    return func.id in SUPPRESSOR_SPELLINGS


def _swallowing_exit_class(expression, bound, tree=None, function=None):
    """The class of ``expression`` whose ``__exit__`` provably eats the failure.

    #316. A user-defined context manager swallows an ``assert`` exactly when
    its ``__exit__`` returns truthy for the ``AssertionError``, and that is a
    different mechanism from every rule above: none of them reads a return
    value, because none of them can. ``contextlib.suppress`` and
    ``pytest.raises`` are known by name; this is recognised by what its own
    ``__exit__`` *does*.

    Executed, the shape in the issue really is silent::

        >>> probe(1)          # with helper: inside
        >>> # returns normally, rc=0

    so reporting the assert as ``enforced`` certifies a disarmed contract as
    load-bearing. That is the damaging direction, which is the one this module
    must never get wrong.

    **Scope is the whole difficulty.** Matching any class that merely *has* an
    ``__exit__`` would fire on every legitimate context manager -- ``pytest``'s,
    ``harness``'s -- and report all 147 real pinned asserts as defeated. The
    rule is therefore gated on three things at once, and the mutation matrix in
    the sentinel suite pins each:

    1. the name must resolve to a class **defined in the file being judged**,
       so ``pytest.raises`` and every imported context manager is unreachable;
    2. that class must define ``__exit__`` in the same file, rather than
       inheriting one;
    3. the body must **provably** return truthy -- a bare ``return`` of a
       constant, or a ``return <bool expr>`` this can evaluate statically.

    An ``__exit__`` that returns ``None`` or ``False``, or that raises, does not
    swallow anything and must stay ``enforced``; criterion 3 is what keeps
    ordinary managers on the live side of the line.
    """
    name = _dotted_class_name(expression)
    if name is None:
        return None
    module = tree if tree is not None else _module_tree()
    classes = _locally_defined_classes(module)
    node = classes.get(name)
    if node is None:
        # The header usually names an *instance*, not the class:
        #
        #     helper = Suppressor()
        #     with helper:
        #
        # so a name that is not itself a class is followed one step through the
        # binding to the class it was constructed from. One step only -- a name
        # built by an unreadable call (a factory, a parameter) is not followed,
        # because assuming an arbitrary constructor returns a swallowing class
        # is the over-breadth this rule exists to avoid.
        constructor = _constructed_class_of(expression, module, classes, function)
        if constructor is None:
            return None
        node = classes.get(constructor)
        if node is None:
            return None
    for child in node.body:
        if not isinstance(child, ast.FunctionDef) or child.name != "__exit__":
            continue
        if _provably_truthy_exit(child):
            return node
    return None


def _constructed_class_of(expression, tree, classes, function=None):
    """The class an instance-valued ``expression`` was constructed from.

    Reads the ``helper = Suppressor()`` that the ``with`` header names, and
    returns ``"Suppressor"`` when that class is one this file defines. Anything
    else -- a factory call, a parameter, a subscript -- is ``None``, which
    leaves the assert ``enforced`` rather than guessing.

    The assignment is read straight off the tree rather than from ``bound``,
    because that mapping is deliberately import-only -- it answers "which
    dotted path does this name resolve to", a question about ``import``
    statements -- and ``helper = Suppressor()`` is an ``Assign``, not an
    import.

    Both the module and the enclosing function are read, because #316's
    criterion 4 requires the local-alias spelling

    .. code-block:: python

        def probe(x):
            cs = Suppressor()
            with cs:

    and the module-level one. The *function's own* bindings are consulted
    first, for the same reason :func:`_bound_names` prefers a function-local
    import: an inner name shadows an outer one of the same spelling. The
    search is still one step -- only a direct ``Name(...)`` constructor call
    is followed, never a factory that returns an instance indirectly.
    """
    if not isinstance(expression, ast.Name):
        return None
    value = _assigned_value(expression.id, function)
    if value is None:
        value = _assigned_value(expression.id, tree)
    if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)):
        return None
    if value.func.id not in classes:
        return None
    return value.func.id


def _assigned_value(name, scope):
    """The right-hand side ``name`` is assigned in ``scope``, or ``None``.

    The *last* assignment wins, matching what a reader meets last, and a
    function-scope lookup is offered before a module one by the caller so an
    inner binding shadows an outer one of the same name.

    ``ast.walk`` is deliberately *not* used on a module: it descends into every
    function body, so a module lookup would silently find a function-local
    assignment and the two scopes would not be distinguishable. The module
    scope is therefore its own top-level statement list, which is what makes
    the function-first lookup in :func:`_constructed_class_of` meaningful.

    A ``for`` target is a store too, and it is the one this used to miss. #370
    taught the module to record a loop target's element for the *suppression*
    question; :func:`_constructed_class_of` asks the same "what does this name
    hold" question, so the same answer has to be readable here:

        for cs in [Suppressor()]:      # `__exit__` returns True for AssertionError
            with cs:
                assert x != 1          # swallowed; was reported enforced

    Read by :func:`_single_loop_element`, so the limit is #370's already-measured
    one rather than a second, looser rule: only a single-element literal
    iterable contributes, and a multi-element or non-literal iterable stays
    ``None`` and keeps the assert ``enforced`` -- the safe direction, since a
    guessed value would read a suppressor that is not the one in force.

    Only an ``ast.Name`` target is followed, matching the ``Assign`` arm: a
    tuple target (``for a, b in ...``) unpacks and is not read here.
    """
    if scope is None:
        return None
    if isinstance(scope, ast.Module):
        nodes = iter(scope.body)
    elif isinstance(scope, ast.AST):
        nodes = ast.walk(scope)
    else:
        nodes = iter(())
    value = None
    for node in nodes:
        if isinstance(node, ast.For):
            if not (isinstance(node.target, ast.Name) and node.target.id == name):
                continue
            element = _single_loop_element(node.iter)
            if element is None:
                # Undecidable: leave any earlier `Assign` in force rather than
                # overwrite it with a guess. #385 measured the alternative.
                continue
            value = element
            continue
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        value = node.value
    return value


def _dotted_class_name(expression):
    """The dotted name of a class reference, or ``None`` if it is not one.

    Only a plain ``Name`` or an ``Attribute`` chain qualifies. A subscript or a
    call is not statically readable as a class, and guessing would put the
    over-breadth risk back.

    #330/#339. One call shape is an exception, and it is the shape the
    # ``with`` header itself writes:

        with Suppressor():        # constructed inline, never bound to a name
            assert 1 == 2

    The user-defined-``__exit__`` rule reached its class only through a *name
    binding* (``helper = Suppressor()``), so the inline spelling returned
    ``None`` here and the assert stayed ``enforced`` while the interpreter
    swallowed it. Executed, on this tree and on master:

        checker=LIVE  cpython=DEAD        # with Suppressor():
        checker=DEAD  cpython=DEAD        # helper = Suppressor(); with helper:

    The alias spelling already worked; only the inline one was missed, which is
    why the three issues read as separate but are one hole.

    The unwrapping is deliberately one step and only for a *bare constructor*:
    a ``Name`` called directly, or an ``Attribute`` chain called directly. A
    factory (``make_suppressor()``) is still unreadable, because an arbitrary
    call is not known to return an instance of the class it is named after.
    Every downstream gate is unchanged -- the class must still be defined in
    the file under judgement, must define ``__exit__`` there, and that
    ``__exit__`` must still provably return truthy -- so criterion 3 keeps an
    ordinary manager on the live side of the line.
    """
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return ast.unparse(expression)
    # #330/#339. The header constructs the suppressor where it uses it.
    if isinstance(expression, ast.Call):
        return _dotted_class_name(expression.func)
    return None


def _locally_defined_classes(tree):
    """``{name: ClassDef}`` for every class defined in ``tree`` at module level
    or inside a function.

    A class imported from elsewhere is deliberately absent, which is what keeps
    the rule off ``pytest``'s and the harness's own context managers.

    A class defined *inside a function* counts too. #410: executed, a
    function-local manager swallows the assert exactly as a module-level one
    does, and it was reported ``enforced`` -- a disarmed contract certified as
    load-bearing:

        def outer(x):
            class Suppressor:
                def __exit__(self, *exc): return exc[0] is AssertionError

            helper = Suppressor()
            with helper:
                assert x == 99

    It is reached through the *instance* spelling, exactly as a module-level one
    is: ``_constructed_class_of`` asks which class the bound value was
    constructed from, so the class is registered under the name it is
    constructed by.

    A class inside another class is still excluded, and
    :func:`_class_defs_in_scope_order` explains why -- the two cases look alike
    in a tree walk but are reached by different spellings.

    **Over-breadth, measured rather than assumed.** #316's criterion is that a
    rule firing on every ``__exit__`` does not count. The real pinned file was
    walked with this widened: it holds 145 asserts and four module-level classes
    (``Harness``, ``Symbols``, ``ReadOnly``, ``WriteFault``), all 40 of its
    ``with``-items are ``Call`` expressions, and **zero** of them name a
    file-defined class. Nothing actually pinned can reach this rule.

    Only classes this module's own text defines are collected. An imported
    class is still absent, which is what keeps ``pytest``'s and the harness's
    real context managers out.
    """
    found = {}
    for node in _class_defs_in_scope_order(tree):
        if node.name in found:
            # A same-named class in a later scope does not displace the one
            # already registered; the first is the one a bare `Inner()` in the
            # same function constructs.
            continue
        found[node.name] = node
    return found


def _class_defs_in_scope_order(tree):
    """Every ``ClassDef`` defined in a *function* body in ``tree``, in order.

    A **class body is deliberately not descended into.** A class inside another
    class is spelled ``Outer.Inner()`` at its use site, so registering it under
    the bare name ``Inner`` would let a bare ``Inner`` -- a different object in a
    different scope -- be reported as a defeat. That limit is deliberate and is
    pinned by ``test_only_a_module_level_class_counts``. #410 widens only the
    *function* case, where the class is reached through an instance binding
    (``helper = Suppressor()``) rather than through its own name.

    This flag is defence in depth, and it is worth being exact about which
    guard actually does the work. Removing it and descending into class bodies
    as well leaves ``test_only_a_module_level_class_counts`` **passing** --
    because the header there is ``Outer.Inner()``, a ``Call``, and
    :func:`_dotted_class_name` rejects a ``Call`` outright, so the header never
    reaches the table by that route either. The mutation is therefore not
    killed by the suite, and is documented here rather than claimed as pinned.
    What the flag does guarantee is that the *name* ``Inner`` never enters the
    table, so no other rule that consults it can be widened by accident later.

    Nested functions *are* descended into, so a class defined two function
    levels down is still found. Order is document order and the first
    registration of a name wins, so a bare ``Inner()`` resolves to the class its
    own scope defines rather than a same-named one from an unrelated function.
    """
    found = []
    seen = set()

    def visit(node, inside_class):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                if not inside_class and id(child) not in seen:
                    seen.add(id(child))
                    found.append(child)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, inside_class)

    visit(tree, False)
    return found


def _provably_truthy_exit(exit_function):
    """Does this ``__exit__`` provably return a truthy value?

    Three shapes are decided, and nothing else:

    * ``return True`` / ``return 1`` -- a truthy constant.
    * ``return False`` / ``return None`` / ``return 0`` -- falsy, so the assert
      propagates and stays a live contract.
    * ``return <expression>`` where the expression is a name, attribute, or call
      already bound to a truthy constant *in this file*.

    A ``return`` whose value cannot be decided statically is treated as **not**
    swallowing. That is the conservative choice for a *silent* defeat only in
    the sense that it avoids false alarms on the 147 real pinned asserts; the
    issue's over-breadth criterion is explicit that a rule firing on every
    ``__exit__`` does not count, so an undecidable return is left alone rather
    than guessed at.
    """
    for node in ast.walk(exit_function):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        verdict = _statically_truthy(node.value)
        if verdict is True:
            return True
        if verdict is False:
            return False
    # No `return` at all means the function falls off the end and yields None,
    # which does not swallow.
    return False


def _statically_truthy(node):
    """``True``/``False`` when ``node``'s truth value is decidable here.

    A ``Compare`` is decided by reading its operands as literals, so the shape
    in #316 -- ``return exc[0] is AssertionError`` -- is decided by
    :func:`_exit_swallows_assertion_error`, which knows what the second
    argument to ``__exit__`` actually is at runtime.

    Nothing here evaluates a general expression. A ``Compare`` is only
    decidable when it is *literally* the "is this the AssertionError?" test
    that ``__exit__`` receives, and everything else returns ``None`` so the
    assert stays ``enforced`` rather than being reported on a guess.
    """
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.NameConstant):  # pragma: no cover - py<3.8 shape
        return bool(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return bool(getattr(node, "elts", None) or getattr(node, "keys", None))
    if _exit_swallows_assertion_error(node):
        return True
    return None


#: The parameter names ``contextlib`` and the stdlib use for the raised
#: exception in ``__exit__``. Any of them means the same thing at runtime, and
#: a user-written manager picks whichever reads best, so all are accepted.
_EXIT_EXCEPTION_PARAMS = frozenset({"exc", "exc_type", "et", "e", "err", "exc_info"})


def _exit_swallows_assertion_error(node):
    """Is ``node`` the "the exception being handled is AssertionError" test?

    This is the shape #316 is filed against, verbatim::

        def __exit__(self, *exc):
            return exc[0] is AssertionError

    At runtime ``__exit__`` receives the exception type as its first argument
    after ``self``, so ``exc[0]`` is the raised type and comparing it to
    ``AssertionError`` is true exactly when an assert was swallowed. The three
    spellings accepted are the ones that say that and nothing more:

    * ``<param>[0] is AssertionError``  -- the filed shape
    * ``<param> is AssertionError``     -- a named first parameter
    * ``<param>[0] == AssertionError``  -- the same test spelled ``==``
    * ``issubclass(<param>[0], AssertionError)`` -- the ``except*`` style

    Anything else -- a bare name, a call, a different exception, a negated
    test -- is not this shape and is declined, which leaves the assert
    ``enforced``. That is the direction the issue's over-breadth criterion
    requires: an ordinary manager whose ``__exit__`` inspects the exception in
    some other way must not have its asserts reported.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id != "issubclass" or len(node.args) != 2:
            return False
        return _is_exception_argument(node.args[0]) and _is_assertion_error_ref(node.args[1])
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    if not isinstance(node.ops[0], (ast.Is, ast.Eq)):
        return False
    if not _is_exception_argument(node.left):
        return False
    return len(node.comparators) == 1 and _is_assertion_error_ref(node.comparators[0])


def _is_exception_argument(node):
    """Is ``node`` a read of ``__exit__``'s raised-exception argument?"""
    if isinstance(node, ast.Subscript):
        index = node.slice
        if not (isinstance(index, ast.Constant) and index.value == 0):
            return False
        return _is_exception_name(node.value)
    return _is_exception_name(node)


def _is_exception_name(node):
    return isinstance(node, ast.Name) and node.id in _EXIT_EXCEPTION_PARAMS


def _is_assertion_error_ref(node):
    """Is ``node`` a reference to the builtin ``AssertionError``?

    A bare ``Name`` is enough. The name is not resolved against a binding, so
    a local that *shadows* ``AssertionError`` would be misread -- which is the
    safe direction here, because a shadowed name almost certainly is not the
    builtin and reporting the assert as swallowed then costs a false alarm
    rather than certifying a disarmed contract. The cost is a rare false
    positive in a file that rebinds the builtin, which the mutation matrix
    keeps visible.
    """
    return isinstance(node, ast.Name) and node.id == "AssertionError"


def _is_user_defined_swallowing_with(node, bound, function, tree=None):
    """Does this ``with`` enter a user class that provably eats the failure?"""
    for item in node.items:
        found = _swallowing_exit_class(item.context_expr, bound, tree, function)
        if found is not None:
            return True
    return False


def _is_suppression_call(call, bound):
    """Is this call a suppression context that can eat an assertion failure?"""
    if any(_resolves_to(call.func, dotted, bound) for dotted in SUPPRESSING_CONTEXTS):
        return True
    if any(_resolves_to(call.func, dotted, bound) for dotted in ASSERTION_CAPTURING_CONTEXTS):
        return not _raises_without_an_expected_type(call)
    return _unreadable_suppressor(call, bound)


def _raises_without_an_expected_type(call):
    """Is this ``pytest.raises`` call one that cannot leave the test green?

    ``pytest.raises`` requires the expected exception type to be given
    positionally. With *no* argument at all it raises ``ValueError: You must
    specify at least one parameter`` while building the context object, before
    the body is ever entered -- so no assert is evaluated and the test fails
    loudly for an unrelated reason. That case is decidable and excluded.

    ``match=`` is deliberately *not* excluded, and the reason is that it is
    undecidable rather than loud. Whether the block ends green depends on the
    assertion's own message at runtime: with ``pytest.raises(AssertionError,
    match="1 == 2")`` the empty message of a bare ``assert`` fails the regex and
    pytest re-raises -- loud -- but with ``match=""`` or ``match=".*"`` the
    failure is caught, matches, and the test passes green. Measured across
    all three. A static check cannot know which, so the two defensible answers
    are "always a defeat" and "never a defeat", and this picks the first: an
    assert that is *sometimes* unenforceable is not a contract that can be
    relied on, whereas calling it enforced would certify a shape that has
    already been measured to go green.

    The no-argument case is different in kind: there the failure is raised
    before the body runs, so the assert is never evaluated and *no* runtime
    value can rescue it.
    """
    return not call.args


def _entered_suppressions(node, bound):
    """Suppressions installed by ``stack.enter_context(...)`` inside this ``with``.

    ``ExitStack`` defers the suppression past the ``with`` header, so the shape is

        with ExitStack() as stack:
            stack.enter_context(suppress(AssertionError))
            assert 1 == 2

    where the assert is enclosed by the ``with`` but the suppressor is a statement
    in its body rather than an item of its header. Reading only the header misses
    it. A resolved suppressor already answers True in ``_is_suppression_call``;
    this narrows the *unresolved* case to ``enter_context``, because a bare-name
    rule over every call in the body would also fire on unrelated statements.
    """
    entered = []
    for statement in node.body:
        for child in ast.walk(statement):
            if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
                continue
            if child.func.attr != "enter_context" or not child.args:
                continue
            entered.append(child.args[0])
    return entered


#: Runtime types a store's value is pinned to when the right-hand side is a
#: literal, and which therefore cannot implement the context manager protocol
#: whatever the surrounding code intends. Entering one raises `TypeError` on the
#: header, before the body runs, so an assert in that body is unreachable
#: (#336).
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
        # #388. The builtin constructors read by `_builtin_constructor_kind`
        # have to be non-enterable *here* as well: a type name the set omits
        # makes the constructor read as possibly-enterable, and the guard then
        # declines a header CPython settles to a value it cannot enter. `range`
        # and `frozenset` are the two a first pass missed, because they are
        # builtins that are not spelled in everyday source as a `with` target
        # and so were not already listed.
        "frozenset",
        "range",
        "bytearray",
    }
)

#: Bare builtin constructors whose result is a value no ``with`` can enter.
#:
#: ``cs = int()`` and ``cs = list()`` are decided by the *name being called*,
#: not by the call being a call. Every entry here is a builtin type object
#: spelled without a module prefix, so a call of it returns a fresh instance of
#: the corresponding :data:`NON_CONTEXT_MANAGER_TYPES` entry and nothing else.
#: Reading these as "a call is a call" reports a dead header as live.
#:
#: Only the *unqualified* spellings are listed. ``builtins.int()`` is
#: recognised separately by :func:`_builtin_constructor_kind`, and a
#: user-shadowed ``int`` is a different binding entirely -- shadowing is not
#: modelled here, so the conservative direction is taken for a name this
#: module cannot prove is the builtin.
_BUILTIN_CONSTRUCTOR_TYPES = {
    "int": "int",
    "float": "float",
    "complex": "complex",
    "str": "str",
    "bytes": "bytes",
    "bool": "bool",
    "list": "list",
    "tuple": "tuple",
    "set": "set",
    "frozenset": "frozenset",
    "dict": "dict",
    "bytearray": "bytearray",
    "range": "range",
}

#: Marker for a constructor call that *raises* for the arguments given, so the
#: store it appears in never happens. ``range()`` with no argument is a
#: `TypeError`; the name keeps whatever was bound before, which for a header
#: following a carrier is the carrier itself.
_RAISING_CONSTRUCTOR = object()

#: Constructors called with no arguments that are **falsy**: `if set():` never
#: enters its body, and `for cs in set():` runs zero times so the loop never
#: binds the name. Both spellings of "empty builtin container" are decided from
#: this one set, so the condition form and the loop form cannot disagree.
_EMPTY_CONSTRUCTOR_TYPES = frozenset({"set", "frozenset", "dict", "list", "tuple", "bytearray"})


def _builtin_constructor_kind(value, function=None):
    """The type name a zero-argument builtin constructor call produces.

    ``int()`` is decided by the callee's name, so this is the *call* form of
    :func:`_literal_runtime_type`: a bare ``ast.Name`` callee drawn from
    :data:`_BUILTIN_CONSTRUCTOR_TYPES`, or the same spelled as ``builtins.int``.

    ``function``, when given, is the scope the call appears in. A name the
    function itself rebinds -- ``def int(): ...``, ``int = ...``, ``import int``
    -- is a *different* binding, and calling it may return anything at all,
    including a real context manager. Reading it as the builtin produces a
    false-dead: the header is reported dead while CPython enters it. A shadowed
    name therefore answers ``None`` here and falls through to the caller's
    ordinary "a call is a call" rule, which is the safe direction.

    Returns ``None`` for every other call, so the caller's existing "a call is
    a call" rule stays in force. That rule is the safe direction: a
    ``nullcontext()`` really does return a context manager, and declining to
    read it keeps a live header live.
    """
    if isinstance(value, ast.Call) and _callee_is_shadowed(value.func, function, value):
        return None
    if not isinstance(value, ast.Call):
        # A constructor called with arguments may return a subclass or a
        # different object entirely (``bool(1)`` is still bool, but
        # ``range(3)`` is a range and ``str(b"")`` is a str, while a future
        # spelling need not be). The constructors below are read through
        # :func:`_builtin_constructor_arguments_are_ignorable` instead, which
        # admits the argument forms that cannot change the result type.
        return None
    func = value.func
    if isinstance(func, ast.Name):
        kind = _BUILTIN_CONSTRUCTOR_TYPES.get(func.id)
    elif (
        isinstance(func, ast.Attribute)
        and func.attr in _BUILTIN_CONSTRUCTOR_TYPES
        and isinstance(func.value, ast.Name)
        and func.value.id == "builtins"
    ):
        kind = _BUILTIN_CONSTRUCTOR_TYPES[func.attr]
    else:
        return None
    if kind is None or not _builtin_constructor_arguments_are_ignorable(func, value):
        return None
    return kind


def _name_is_shadowed_in(func, function):
    """Is the callee a name this function rebinds away from the builtin?

    Only a *binding in the function's own scope* counts, and only for a bare
    ``ast.Name`` callee: ``cs = range(3)`` is only the builtin when nothing
    in scope rebinds ``range``. A ``def``/``class``/``import``/assignment to
    that name anywhere in the function body makes the call a different one.

    Nested scopes bind their own names, so a ``def range(...)`` inside another
    ``def`` inside this function does not shadow the builtin's meaning *here*;
    those are skipped, matching :func:`_own_imports`' scope discipline.
    """
    if not isinstance(func, ast.Name):
        return False
    name = func.id
    for node in _own_scope_bindings(function):
        if node is name:
            continue
        for bound in _names_bound_by_statement(node, name):
            if bound:
                return True
    return False


def _name_is_rebound_away_from_module(name_node, function, call=None):
    """Is ``name_node.id`` rebound to something other than its own module?

    The question is asked only of a name that a qualified ``builtins.attr``
    call is written through, so the answer separates the one import that
    *is* the module from every other way the name can be taken:

    * ``import builtins`` and ``import builtins as builtins`` bring the real
      module in and leave the name meaning that module;
    * a parameter, a local store, a function-local ``def``/``class``, or a
      module-level store of any kind replace it with an arbitrary object, so
      ``builtins.attr`` is then some attribute of *that* object.

    ``from builtins import list`` does not bind the name ``builtins`` at all,
    so it never reaches here; a store that is the module's own canonical import
    is the only shape that keeps the qualified form trustworthy.
    """
    name = name_node.id
    if function is None:
        # No scope to consult, so the name cannot be proved to be the module.
        return True
    if name in _signature_bound_names(function):
        return True
    for node in _own_scope_bindings(function):
        if node is name_node:
            continue
        for bound in _names_bound_by_statement(node, name):
            if bound and not _is_canonical_module_import(node, name):
                return True
    return _module_rebinds_name(function, name, call)


def _is_canonical_module_import(statement, name):
    """Is this binding the ordinary ``import <name>`` of the module itself?"""
    if not isinstance(statement, ast.Import):
        return False
    return any(
        (alias.asname or alias.name) == name and alias.name == name for alias in statement.names
    )


def _callee_is_shadowed(func, function, call=None):
    """Is ``func`` a call target that does not reach the real builtin?

    This is the full shadowing question, and it is a strict superset of
    :func:`_name_is_shadowed_in`. That helper only ever looked inside the
    function's *body*, which missed the two bindings that are not written in
    the body at all:

    * a **parameter** -- ``def f(int=None): cs = int()`` binds ``int`` for the
      whole call, so the call is whatever the caller passed. Reading it as the
      builtin ``int`` reports ``cs`` as a zero that cannot be entered;
    * a **module-level** binding -- ``def int(): ...`` at module scope binds
      ``int`` for every function in the file, and a module-level
      ``cs = int()`` is a store that has already run by the time any header
      reads it.

    Both were read as the builtin, so a header CPython enters was reported
    dead. A parameter or a module binding can only make the call *less*
    determinate, so the answer here is always the conservative one: treat the
    callee as shadowed and let the caller's ordinary "a call is a call" rule
    keep the header live.

    ``function`` may be ``None`` -- the condition path is handed no enclosing
    scope by some callers -- and a caller that does not know the scope cannot
    prove the name is the builtin either, so it is answered the same
    conservative way rather than as "not shadowed".

    ``call`` is the ``ast.Call`` whose callee ``func`` is, when the caller has
    it. It only tells the module walk where to stop, so it is optional.
    """
    if isinstance(func, ast.Attribute):
        # `builtins.int` only means the builtin when `builtins` itself is not
        # rebound, so the question moves to the attribute's own base name.
        # Returning `False` for the name outright answered every `builtins.*`
        # call as the real module, including one through a name that some
        # store has replaced:
        #
        #     builtins = SimpleNamespace(list=nullcontext)
        #     cs = builtins.list()      # any callable, not the builtin `list`
        #
        # That read a `TypeError`-raising header as LIVE. `import builtins` is
        # the one binding that must still count as canonical: it is not a
        # *rebinding* but the ordinary import that brings the real module in,
        # and every other `import x` in the file is exactly the spelling that
        # means "this is the module named x". Treating it as a shadow would
        # make `cs = builtins.list()` LIVE where CPython raises.
        if isinstance(func.value, ast.Name) and func.value.id == "builtins":
            return _name_is_rebound_away_from_module(func.value, function, call)
        return _callee_is_shadowed(func.value, function, call)
    if not isinstance(func, ast.Name):
        return False
    if function is None:
        # No enclosing scope to consult. A name the caller could not resolve
        # is not provably the builtin, so it is answered as shadowed.
        return True
    if func.id in _signature_bound_names(function):
        return True
    if _name_is_shadowed_in(func, function):
        return True
    return _module_binds_name(function, func.id, call)


def _signature_bound_names(function):
    """Every name the function's own signature binds when it is called.

    The positional parameters, the keyword-only parameters, ``*args`` and
    ``**kwargs`` all bind for the duration of the call, so each of them
    shadows the builtin of the same name inside the body. ``*args`` and
    ``**kwargs`` are matched through their own attribute, because ``ast``
    records those under ``vararg``/``kwarg`` rather than in the flat ``args``
    list -- a filter reading only ``args`` saw a call whose shadow arrives
    through ``*values`` as an un-shadowed builtin.
    """
    args = getattr(function, "args", None)
    if args is None:
        return frozenset()
    names = {argument.arg for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return frozenset(names)


def _module_binds_name(function, name, call=None):
    """Does the module holding ``function`` bind ``name`` at module scope, and
    does that binding run before ``function`` does?

    A module-level ``def int(): ...``, ``import int`` or ``int = ...`` rebinds
    the name for every function in the file, and a module-level ``cs = int()``
    is a real store that has already run by the time any header reads it.

    Two things have to be true, and asking for either alone was a false-live:

    * the binding must be in the **module** scope. This walk starts at the
      module's own statements, so ``outer``'s own ``def list(): ...`` -- a
      binding one scope *below* the module -- is never consulted here. Before
      this walk existed the question was reached through the module, so a
      function-local definition was answered twice: once correctly by
      :func:`_name_is_shadowed_in` and once, wrongly, from here.
    * the binding must be in force **where the call is evaluated**, and that
      point is not the same for the two kinds of call:

      - a call written at **module scope** is evaluated as the module runs, so
        only the statements before it count:

            cs = list()             # the builtin -- runs first
            def list(): return None # too late to matter for the line above

      - a call inside a **function body** is evaluated only when that function
        is called, which is after the *whole* module has finished executing.
        Every module binding therefore counts, including one written after the
        function's own ``def``:

            def outer(x):
                cs = list()         # `list` is whatever the module ended with
                with cs: ...
            def list(): return nullcontext()

        Reading that ``cs`` as the builtin reported a header CPython enters as
        DEAD. Cutting the walk at the function's ``def`` was the fix for the
        module-scope case above, and it is exactly wrong for this one.

      The two are told apart by whether the call is inside the analysed
      function's own scope, which :func:`_call_runs_at_module_scope` answers.

    The walk descends into module-level blocks -- a binding inside a
    module-level ``if`` still binds the name whenever that branch is taken --
    while a nested ``def``/``class`` *body* stays a separate scope, matching
    :func:`_own_scope_bindings`' discipline.

    ``call`` is the ``ast.Call`` node the question is being asked about, when
    the caller has it. It only decides *where* the walk stops, so omitting it
    is safe -- it just falls back to stopping at the function's own ``def``,
    which is the conservative choice for a function-body call.
    """
    module = _module_for_function(function)
    if module is None:
        return False
    if _call_runs_at_module_scope(function, call):
        return _module_binds_name_at_module_scope(module, function, call, name)
    # The call is inside a function body, so it runs only after the whole
    # module has executed and every module binding is in force. A nested
    # `def`/`class` *body* is still a separate scope and is not counted.
    for statement in getattr(module, "body", []):
        if any(_names_bound_by_statement(statement, name)):
            return True
        for node in _module_level_bindings(statement):
            if any(_names_bound_by_statement(node, name)):
                return True
    return False


def _module_rebinds_name(function, name, call=None):
    """Does the module holding ``function`` bind ``name`` to something else?

    This is the ``builtins.attr`` form's own question -- "is the qualified name
    still the module itself" -- and it takes the call node so the answer is
    scoped the same way :func:`_module_binds_name` scopes its own. A store
    written *after* a module-scope call has not run when the call is evaluated,
    so the callee still reached the real module:

        import builtins
        cs = builtins.list()          # the builtin; runs first
        builtins = SimpleNamespace(list=nullcontext)

    Counting that later store read the rebound attribute and reported the
    ``TypeError`` CPython raises as LIVE. A function-body call is reached after
    the whole module has run, so for one every store counts and the same order
    cut is not applied.

    The ordinary ``import builtins`` is not a rebinding and is skipped here, so
    the canonical spelling stays trustworthy.
    """
    module = _module_for_function(function)
    if module is None:
        return False
    at_module_scope = _call_runs_at_module_scope(function, call)
    for statement in getattr(module, "body", []):
        if at_module_scope and _module_statement_precedes_call(statement, function, call):
            # The call has been evaluated by the end of this statement, so a
            # binding written from here on is not yet in force where it was.
            break
        if _is_canonical_module_import(statement, name):
            continue
        if any(_names_bound_by_statement(statement, name)):
            return True
        for node in _module_level_bindings(statement):
            if _is_canonical_module_import(node, name):
                continue
            if at_module_scope and _node_precedes_call(statement, node, call):
                continue
            if any(_names_bound_by_statement(node, name)):
                return True
    return False


def _call_runs_at_module_scope(function, call):
    """Is ``call`` evaluated while the module runs, rather than in a body?

    A call written among the module's own statements is reached as the module
    executes, so only the bindings before it are in force. A call inside the
    analysed function -- or inside any other nested scope -- is reached later,
    when the module has finished, so every module binding counts. The question
    is answered by position: a call that is *not* under ``function`` is a
    module-scope one, which is also the safe answer for a caller that supplied
    no call node at all.
    """
    if call is None:
        return True
    return not _contains(function, call)


def _module_binds_name_at_module_scope(module, function, call, name):
    """Module bindings in force where a module-scope ``call`` is evaluated.

    The walk runs the module's statements in source order, descending into
    module-level blocks so that a binding *inside* a block is ordered with
    respect to the call rather than to the block as a whole. That ordering is
    what separates

        if True:
            cs = list()             # the builtin; runs first
            def list(): return None # too late to matter for the line above

    from the same block with the two the other way round, where ``cs`` really
    is the shadow and the header is dead. Stopping at the enclosing block
    instead of at the statement holding the call answered the first as
    shadowed -- a false-live, since CPython raises ``TypeError`` there.

    The walk stops at the first statement at or after the call's own position,
    and the statement holding the call has already been checked, so a store
    written on that same line still counts.
    """
    for statement in getattr(module, "body", []):
        if _module_statement_binds_name_before_call(statement, call, name):
            return True
        if _module_statement_precedes_call(statement, function, call):
            break
    return False


def _module_statement_precedes_call(statement, function, call):
    """Has execution reached ``call`` by the end of ``statement``?"""
    if statement is function:
        # The function's own definition binds its name; the body that follows
        # is a separate scope. A module-scope call is not inside it, so
        # reaching the `def` means the call has already been evaluated.
        return True
    return _contains(statement, call)


def _node_precedes_call(statement, node, call):
    """Has execution reached ``call`` before ``node`` inside ``statement``?

    ``node`` is a binding found *inside* ``statement`` by the block descent,
    so the two are ordered within that one statement rather than across the
    module. Only the path the descent actually took is walked, and a nested
    ``def``/``class`` body -- a separate scope -- stops the walk rather than
    being scanned, because nothing there has run.
    """
    if node is statement:
        return False
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return False
    if not _contains(statement, call):
        # The call is not in this statement at all, so every binding in it
        # runs before the call does.
        return False
    for inner in _module_level_bindings_in_order(statement):
        if inner is node:
            return True
        if _contains(inner, call):
            return False
    return False


def _module_statement_binds_name_before_call(statement, call, name):
    """Does ``statement`` bind ``name``, counting only what runs before ``call``?

    When the call is not inside ``statement`` the whole statement has run, so
    every binding it contains counts. When it is inside, the walk descends in
    order and stops once the call's own statement is reached, so a binding
    written after the call inside the same block is correctly ignored.
    """
    if not _contains(statement, call):
        return any(_names_bound_by_statement(statement, name)) or any(
            _names_bound_by_statement(node, name) for node in _module_level_bindings(statement)
        )
    # The call lives inside this statement, so descend in source order and
    # stop at the first inner statement that has already run it. The call's
    # own statement is checked before stopping, so a store on the same line
    # (`cs = list()`) is still counted.
    for inner in _module_level_bindings_in_order(statement):
        if any(_names_bound_by_statement(inner, name)):
            return True
        if _contains(inner, call):
            break
    return False


def _module_level_bindings_in_order(statement):
    """Like :func:`_module_level_bindings`, but the blocks' own statements too.

    The order matters only for a statement that holds the call, because that is
    the one case where a later binding in the same block must not be counted.
    Each block's children are yielded before the block itself so that the
    reader sees them in the order they run.
    """
    ordered = []
    for node in _module_level_bindings(statement):
        if isinstance(node, _MODULE_LEVEL_BLOCKS):
            ordered.append(node)
            ordered.extend(_module_level_bindings_in_order(node))
        else:
            ordered.append(node)
    return ordered


def _module_level_bindings(statement):
    """Bindings inside one module-level statement, excluding nested scopes.

    Only *blocks* are descended into, because a block runs in the scope that
    encloses it:

        if flag:
            def list(): return None      # still a module binding

    A nested ``def``/``class`` **body** is a separate scope and is never
    entered, which is what keeps

        def unrelated():
            def list(): return None

    from answering for ``list`` at module scope: ``unrelated``'s body binds
    ``list`` only when ``unrelated`` is called, and nothing here has run it.
    """
    found = []
    # The statement's own body list, so a block is entered at its statements
    # rather than at its own child nodes: descending from the block itself
    # would push its `If.test`/body onto the stack unfiltered, which is how a
    # `def` inside a module `if` was skipped. A `def`/`class` is not a block, so
    # this is empty for one and nothing inside it is entered:
    #
    #     def unrelated():
    #         def list(): return None
    #
    # `list` is bound in `unrelated`'s locals, when `unrelated` is called.
    # Descending made the module answer for a name no module store has written,
    # and read the `TypeError` a real call raises as a callable the header
    # enters.
    stack = list(reversed(_block_body(statement)))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.Lambda,)):
            # A bare lambda has no name of its own and its body is a separate
            # scope, so there is nothing here that binds in the module scope.
            continue
        if isinstance(
            current,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Assign,
                ast.AnnAssign,
                ast.AugAssign,
                ast.Import,
                ast.ImportFrom,
                ast.For,
                ast.AsyncFor,
                ast.With,
                ast.AsyncWith,
            ),
        ):
            found.append(current)
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # Recorded for its own name -- `def list()` inside a module
                # `if` really does bind the module name -- then left alone:
                # its body is a separate scope.
                continue
        if isinstance(current, _MODULE_LEVEL_BLOCKS):
            stack.extend(reversed(_block_body(current)))
    return found


def _module_for_function(function):
    """The parsed module ``function`` was defined in, or ``None``.

    The tree is the one the caller is actually analysing, so a module-level
    binding is read from the file the assert lives in rather than from
    whichever module happens to be importable. The result is memoised by node
    identity because the AST is built once per parsed file and outlives the
    call, and because this runs per condition in a large tree.
    """
    if function is None:
        return None
    if id(function) in _MODULE_FOR_FUNCTION:
        return _MODULE_FOR_FUNCTION[id(function)]
    module = None
    for tree in _candidate_module_trees():
        if any(node is function for node in ast.walk(tree)):
            module = tree
            break
    _MODULE_FOR_FUNCTION[id(function)] = module
    return module


def _remember_module_for_function(function, module):
    """Record the module ``function`` was found in, so lookups can reuse it.

    The caller that resolved the module -- :func:`_is_enforced`, which takes
    the tree from its own caller -- knows the answer exactly. Recording it
    keeps :func:`_module_binds_name` from re-deriving it by walking two module
    trees per condition, and it is what makes the module pass answer for a
    probe tree rather than for the importable milestones module.
    """
    if function is not None and module is not None:
        _MODULE_FOR_FUNCTION[id(function)] = module


def _candidate_module_trees():
    """Every parsed module a function could have been defined in.

    Each tree is read through :func:`_owning_module`'s own pair, and a tree
    that cannot be produced at all is skipped rather than raised: this runs
    inside a decision function called once per condition, and a decision
    function must answer for the node it is handed instead of crashing the
    analyzer. The real production path always finds the milestones tree; the
    skip only matters when the source is unavailable.
    """
    candidates = []
    for produce in (_module_tree, milestones_tree):
        try:
            candidates.append(produce())
        except (OSError, TypeError, AttributeError):
            # The source cannot be read back, so the tree is unavailable. The
            # module pass then has nothing to contribute and the answer stays
            # conservative, which is the safe direction. Only the failures
            # `inspect.getsource` actually raises are caught, so a genuine
            # bug in a producer still surfaces instead of being swallowed.
            continue
    for tree in candidates:
        if tree is None:
            continue
        yield tree


def _own_scope_bindings(function):
    """Statements in ``function``'s own scope, excluding nested scopes."""
    found = []
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A `def`/`class` statement *is* a binding in the enclosing scope --
            # that is exactly the shadowing being looked for -- so it is
            # recorded. Its *body* is a separate scope and is not descended
            # into. A bare `Lambda` has no name, so there is nothing to record
            # and its body is skipped with the rest.
            found.append(node)
            continue
        if isinstance(node, ast.Lambda):
            continue
        found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _names_bound_by_statement(statement, name):
    """Does this statement bind ``name`` in the enclosing function's scope?"""
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [statement.name == name]
    if isinstance(statement, ast.ClassDef):
        return [statement.name == name]
    if isinstance(statement, ast.Assign):
        return [any(isinstance(t, ast.Name) and t.id == name for t in statement.targets)]
    if isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        target = statement.target
        return [isinstance(target, ast.Name) and target.id == name]
    if isinstance(statement, ast.Import):
        return [any((a.asname or a.name) == name for a in statement.names)]
    if isinstance(statement, ast.ImportFrom):
        return [any((a.asname or a.name) == name for a in statement.names)]
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return [name in _store_target_names([statement.target])]
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return [
            any(
                a.optional_vars is not None and name in _store_target_names([a.optional_vars])
                for a in statement.items
            )
        ]
    if isinstance(statement, ast.ExceptHandler):
        return [statement.name == name]
    if isinstance(statement, (ast.Global, ast.Nonlocal)):
        return [name in (statement.names or [])]
    return [False]


def _builtin_constructor_arguments_are_ignorable(func, call):
    """Can these arguments turn the constructor call into a different type?

    ``int()``, ``int("3")`` and ``int(b"\\x03")`` all return an ``int``.
    ``list()`` and ``list((1, 2))`` both return a ``list``. But ``range(3)``
    is a ``range`` while ``range()`` is not a range at all, and a *shadowed*
    ``int`` may be any callable, so the question is asked per name rather than
    assumed for the whole set.

    :data:`_BUILTIN_CONSTRUCTOR_TYPES` is consulted with that in mind: an
    entry is only read when its arguments provably cannot select a different
    type, which is decided here from the constructor's own signature-free
    property that the accepted arguments are *inputs to a conversion* rather
    than *selectors of a different class*.
    """
    name = func.id if isinstance(func, ast.Name) else func.attr
    if name == "range":
        # `range(stop)`, `range(start, stop)` and `range(start, stop, step)`
        # are all ranges, but `range()` with no argument is a TypeError, so
        # the call raises and the store never happens, which leaves the carrier
        # in force. That is a decided outcome, not an unreadable one, so the
        # argument-bearing forms still answer `"range"` and the empty form is
        # recognised by the *caller* as a store that cannot happen.
        if not call.args:
            return _RAISING_CONSTRUCTOR
        return _BUILTIN_CONSTRUCTOR_TYPES["range"]
    # An unpacked argument list may carry anything, including a value that
    # makes the call fail, so it is treated as unreadable.
    return not (
        any(isinstance(argument, ast.Starred) for argument in call.args)
        or any(
            isinstance(keyword, ast.keyword) and keyword.arg is None for keyword in call.keywords
        )
    )
    return None


def _entry_is_dead(expression, by_index, index, function, bound, module=None):
    """Is the value this ``with`` header binds to ``expression`` unenterable?

    #336. Every other rule in this module answers "does this context *suppress*
    the assertion". This one answers the question those rules silently assume
    the answer to: is the entered value a context manager *at all*.

    ``with contextlib.nullcontext() as cs:`` binds ``cs`` to ``None``, because
    an ``as`` target receives ``__enter__``'s return value and ``nullcontext``
    returns ``None``. A later ``with cs:`` then raises ``TypeError: 'NoneType'
    object does not support the context manager protocol`` while entering, so
    the assert under it never runs. The store rules correctly retire the carried
    suppressor at that rebind; what they cannot see is that the name now holds
    something that cannot be entered at all, so the assert is unreachable and is
    reported *enforced*.

    The same holds for the other shapes #336 lists: ``except E as cs:`` unbinds
    the name when the handler exits, ``*cs, = (...)`` binds a list, and ``del
    cs`` removes it outright. Each is a store that cannot produce an enterable
    value.

    Direction: this reports something that looks live as dead. That is the mild
    direction -- the opposite of certifying a swallowed assert as load-bearing
    -- and it is why the rule is scoped as tightly as it is. It fires only when
    the rebinding statement is read directly in the function body (so it really
    does run) and the value is a *literal* whose type is fixed by the syntax. A
    call, an attribute, a parameter, or any store the rule cannot read is left
    alone, because a wrong answer here drops a real pinned assert.
    """
    if not isinstance(expression, ast.Name):
        return False
    name = expression.id
    stores = _stores_of(name, by_index, index, function)
    # #388. `_stores_of` keeps only unconditional stores, so an *unconditional*
    # carrier survives a *conditional* non-carrier store that follows it. That
    # later store may have run and replaced the carrier with an enterable
    # value, in which case the carrier is stale and reading it would answer
    # `defeated` on an assert the interpreter really does evaluate. The
    # `_module_stores` half of this already declines; without the same decline
    # here the function scope is the only one that answers from a carrier a
    # conditional store may have superseded, which is the damaging direction.
    # #388. `_stores_of` keeps only *unconditional* stores, so an
    # unconditional carrier survives a *conditional* non-carrier store that
    # follows it. That later store may have run and replaced the carrier with
    # an enterable value, in which case the carrier is stale and reading it
    # would answer `defeated` on an assert the interpreter really does
    # evaluate. Declining here is the safe direction.
    #
    # #392's #375 guard in `_stores_of` asks a *wider* question -- "can any
    # later conditional store leave something enterable here?" -- but it only
    # fires when the settled value is one `_store_may_bind_enterable` can read.
    # A carrier is a string kind, and the `with`-header families
    # (`with nullcontext(enter_result=...) as cs:`) settle it through a path
    # that never reaches that guard. Both guards are load-bearing, and both
    # now share one reading of "can this store leave something enterable?",
    # so the two questions cannot answer the same store differently.
    # The guard reads the *raw* binding list rather than `_stores_of`'s
    # filtered one, so it still answers when `_stores_of` declines. That is
    # the nested-scope case: a store inside a nested `def`/`lambda`/`class`
    # is not decidable for this rule, so `_stores_of` returns `None` -- but
    # the carrier written before it is still an unconditional store in this
    # scope, and the header must be read from that carrier rather than
    # reported live. Gating on `stores is not None` skipped exactly that, and
    # `test_a_function_carrier_decline_ignores_non_enterable_stores` read nine
    # unreachable asserts as load-bearing.
    if _carrier_may_have_been_superseded(
        by_index["bindings"].get(name, ()),
        by_index["orders"],
        index,
        bound,
        function,
        name,
    ):
        return False
    if stores is None and module is not None:
        # #359. A carrier bound at module scope leaves no store in the
        # function's own table, so `_stores_of` declines and the header reads
        # as enforced. The module body is consulted next, because a
        # module-level binding is a real store that runs at import time,
        # before any function is entered.
        #
        # It is consulted *only* when the function's own table is silent: a
        # store inside the function shadows the module's, and the rule that
        # the inner store supersedes the outer one is already settled by the
        # branch above.
        stores = _module_stores(name, module)
    if stores is None:
        return False
    kinds = set()
    for statement, value, _conditional in stores:
        if value is None:
            # A store with no readable right-hand side. Which store it is
            # decides the answer, and each of these cannot leave a usable
            # context manager bound to the name:
            #
            #   `with nullcontext() as cs:`  -> binds `__enter__`'s return
            #                                   value, i.e. `None`
            #   `del cs`                     -> unbinds the name outright
            #   `except E as cs:`            -> unbinds it when the handler
            #                                   exits
            #
            # A loop target is NOT one of these. It binds the next element, and
            # the rule cannot read that element without running the loop, so
            # it is declined below rather than assumed.
            #
            # A `match` capture is excluded outright. It is recorded with no
            # value because the capture is not reachable from a target list at
            # all, but the name it binds is whatever was *matched* -- arbitrary,
            # and very often a real context manager. Reading its missing value
            # as `None` would claim the later `with cs:` always raises, when the
            # shipped tests pin the opposite (a capture retires the carried
            # suppressor and leaves the assert live). That direction is the
            # safe one, so the rule declines to touch captures.
            if isinstance(statement, ast.Match):
                # #367. ... unless the subject itself pins the captured
                # value, which `match [1]: case [cs]:` does. The exclusion
                # above is about an *arbitrary* matched value; a literal
                # subject is not arbitrary, and reading it is what keeps the
                # in-header capture row from reporting a `TypeError`-raising
                # header as enforced.
                pinned = _match_capture_pins_value(statement, name)
                if pinned is None:
                    return False
                kinds.add(pinned)
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor)):
                # A loop target binds the *next element* of the iterable, and
                # the rule cannot read that element without running the loop.
                # `#336` measured `for cs in (contextlib.nullcontext(),):` and
                # found the second assert genuinely **live** -- the element
                # is a real context manager. Claiming otherwise would drop a
                # live assert, so a loop target is declined outright rather
                # than guessed at. This is a *narrower* rule than the issue
                # filed, and deliberately so: the shipped tests below execute
                # every row to pin the direction.
                return False
            kinds.add("NoneType")
            continue
        if isinstance(value, str):
            # #359. A carrier recorded by `_carrier_runtime_kinds`
            # (function scope) or `_store_bindings` over the module body
            # (module scope): the name
            # holds a module, a class or a function, each pinned by the syntax
            # that bound it. None of them has `__enter__`, so entering one
            # raises `TypeError` before the assert is evaluated. The kind is a
            # plain string rather than a synthesised AST node, so the store
            # stays attached to the real statement it came from.
            kinds.add(value)
            continue
        if not isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)):
            # A call is a call: `nullcontext()` returns a real context manager
            # and must not be read as a non-manager here. The *builtin
            # constructors* are the exception, because their result type is
            # fixed by the callee's name: `cs = int()` leaves an `int`, and
            # `with cs:` raises before the assert. Reading every call as
            # unreadable left an unconditional `cs = int()` reported live
            # where CPython raises -- a false-live.
            constructor = _builtin_constructor_kind(value, function)
            if constructor is None or constructor is _RAISING_CONSTRUCTOR:
                # A raising constructor never stores, so it does not settle the
                # name; an ordinary call is genuinely unreadable. Both leave
                # whatever the earlier stores decided, so the set is not
                # narrowed here.
                if constructor is _RAISING_CONSTRUCTOR:
                    continue
                return False
            kinds.add(constructor)
            continue
        if _binds_element_of(statement, name):
            # `cs, other = (contextlib.nullcontext(), 2)` binds `cs` to the
            # tuple's *first element*, not to the tuple. Reading the right-hand
            # side's type would call the name a tuple and report a live assert
            # as unreachable -- the damaging direction, and exactly the error
            # `#336` was filed to prevent. The element's type is not readable
            # from the container's syntax, so a destructuring target is
            # declined. (Measured: `cs, other = (contextlib.nullcontext(), 2)`
            # leaves the second assert genuinely **live**.)
            return False
        kind = _literal_runtime_type(value)
        if kind is None:
            return False
        kinds.add(kind)
    kinds.discard("element")
    return bool(kinds) and kinds <= NON_CONTEXT_MANAGER_TYPES


def _module_stores(name, module):
    """The module-level stores binding ``name``, resolved the ordinary way.

    #359. The first cut of this rule read the module body looking for
    *carriers* only, and answered from whichever carrier it found last. That
    is the wrong invariant. A name's value is settled by the last **binding**
    of that name, not the last carrier: given

        import os as cs
        import contextlib
        cs = contextlib.nullcontext()

    the carrier runs first and is then superseded by a store of a real context
    manager, so ``with cs:`` succeeds and the assert under it is live. Reading
    only carriers left the ``import os as cs`` entry standing, reported the
    name as a module, and answered ``defeated`` -- dropping a real pinned
    contract. That is the damaging direction, and it is the same one #359 was
    filed to correct, so a first cut that fixes the carrier rows while
    regressing these is not a partial success.

    The fix is to resolve the module body through the machinery already used
    for a function scope -- :func:`_store_bindings`, :func:`_binding_order`
    and :func:`_stores_of` -- rather than a parallel walk. The carrier needs
    no special case anywhere in that path: :func:`_store_bindings` already
    records a carrier as a plain ``str`` value attached to the real statement,
    and :func:`_entry_is_dead` already reads a ``str`` value as a runtime kind.
    Keeping the value attached to a real statement is what makes containment
    and ordering answerable, the mistake #337's carrier machinery had already
    learned to avoid.

    The header is always evaluated *after* every module-level statement has
    run -- the name is read when a function is called, long after the import
    completed -- so the index is one past the last module store and every
    module binding is in scope. What ``_stores_of`` then does is the same
    thing it does for a function: it keeps only unconditional stores, because
    a store nested in a block may not have run.

    That is why a carrier inside a module-level ``if`` is still declined, and
    why a rebinding inside a module-level ``if``/``else`` is declined too --
    in both cases the only *unconditional* binding is a value the rule cannot
    read, and declining is the conservative answer rather than a guess. See
    :func:`_carrier_runtime_kinds` for what the direct, decidable case buys:
    a carrier written straight into the module body has run by the time any
    function is entered, and reading it is what lets the header be judged
    rather than declined.

    That conservatism is not new to the module path. The function-scope walk
    answers the identical shape the same way, because :func:`_stores_of` is
    what drops conditional stores on *both* sides, and both paths now run the
    same ordered decline through
    :func:`_carrier_may_have_been_superseded`. Lifting the shared
    conservatism -- reading a conditional binding as settled -- would mean
    changing :func:`_stores_of` for every rule at once, and is deliberately
    not folded in here.

    #388 is what made that agreement real rather than aspirational. This
    decline was written for the module path only, and the function path had no
    equivalent, so a function-scope carrier that a later conditional store
    may have superseded went stale and answered ``defeated`` on a live assert.
    The two scopes now call one helper, so a future change to the ordering
    test cannot reach one without reaching the other.

    A *conditional non-carrier* store is the one case where dropping it is
    not safe here, and it is handled separately. A store nested in a block may
    not have run -- but it may have, and if it did it can have replaced the
    carrier with an enterable value:

        import os as cs
        import contextlib
        def _rebind():
            global cs
            cs = contextlib.nullcontext()
        _rebind()
        def outer(x, flag, helper):
            with cs:                # succeeds: cs is a real nullcontext()
                assert x != 1       # live

    The carrier is unconditional, so :func:`_stores_of` keeps it and answers
    ``defeated`` -- dropping a live assert. Measured on ``8f4395b``, and wrong
    where ``master`` is right. So a conditional non-carrier store that comes
    *after* the last unconditional carrier leaves the value undecidable, and
    this returns ``None`` (decline) instead.

    The ordering test is what keeps this narrow. A conditional store *before*
    the last carrier really is superseded by it and needs no special case:

        def _rebind():
            global cs
            cs = contextlib.nullcontext()
        _rebind()
        import os as cs            # runs last; the name is a module again

    That answers ``defeated`` correctly, because the later carrier wins.

    Returns ``None`` when the module body says nothing usable about ``name``,
    which is the same "decline" the function path returns and the same answer
    master gave.
    """
    if not isinstance(module, ast.Module):
        return None
    bindings, _raw_values = _store_bindings(module, set())
    if not bindings:
        return None
    orders = {
        id(statement): _binding_order(module, statement)
        for entries in bindings.values()
        for statement, _, _ in entries
    }
    name_entries = bindings.get(name)
    if name_entries and _carrier_may_have_been_superseded(
        name_entries, orders, None, _bound_names(module, module), module, name
    ):
        return None
    by_index = {"bindings": bindings, "orders": orders}
    index = max(orders.values(), default=-1) + 1
    return _stores_of(name, by_index, index, module)


def _literal_runtime_type(value):
    """The runtime type name a literal expression is pinned to, or ``None``."""
    if isinstance(value, ast.Starred):
        # `cs, *rest = ...` and `*cs, = ...` build a list. `#336` measured
        # `*cs, = [...]`: the name is bound to a list, entering it raises
        # `TypeError`, so the assert below is unreachable. A starred target is
        # recorded as an `ast.Starred` inside the target list, so the runtime
        # type of the *name* is the list the unpacking produces.
        return "list"
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return {ast.List: "list", ast.Tuple: "tuple", ast.Set: "set"}[type(value)]
    if isinstance(value, ast.Dict):
        return "dict"
    if isinstance(value, ast.Constant):
        return type(value.value).__name__
    return None


def _binds_element_of(statement, name):
    """Does this store bind ``name`` to an *element* of the right-hand side?

    ``cs, other = (a, b)`` and ``[cs] = [a]`` give the name one element of the
    right-hand side; only a bare ``cs = ...`` target gives it the whole value.
    """
    if not isinstance(statement, ast.Assign):
        return False
    return any(
        isinstance(target, (ast.Tuple, ast.List)) and name in _store_target_names([target])
        for target in statement.targets
    )


def _target_is_bare_name(statement, name):
    """Does ``name`` receive this assignment's whole right-hand side?

    The complement of :func:`_binds_element_of`, asked per target rather than
    per statement. A chained assignment can mix the two spellings in one
    statement:

        cs = (other, x) = (helper, 1)

    Here ``cs`` is a bare target and receives the whole tuple, while ``other``
    and ``x`` receive elements of it. Deciding the statement once -- "this is a
    destructuring assignment" -- is wrong for ``cs`` in both halves of that
    example, and reading the right-hand side's type for ``cs`` is right.

    Only ``ast.Assign`` carries several targets, so any other store form binds
    its name the ordinary way and answers ``True``.
    """
    if not isinstance(statement, ast.Assign):
        return True
    return any(isinstance(target, ast.Name) and target.id == name for target in statement.targets)


def _binds_a_starred_name(statement, name):
    """Is ``name`` a starred target, so unpacking pins it to a list?

    ``*cs, = (helper,)`` and ``first, *cs = pair`` both build a list for ``cs``
    whatever the elements are, so the name cannot hold a context manager
    afterwards. :func:`_literal_runtime_type` reads the ``ast.Starred`` that
    records this as ``"list"`` for the ordinary entry rule; this is the same
    question asked about the target rather than the value, because
    :func:`_store_may_bind_enterable` has to know it *before* it decides
    whether the element type is readable at all.
    """
    if not isinstance(statement, ast.Assign):
        return False

    def starred(node):
        if isinstance(node, ast.Starred):
            return name in _store_target_names([node.value])
        if isinstance(node, (ast.Tuple, ast.List)):
            return any(starred(element) for element in node.elts)
        return False

    return any(starred(target) for target in statement.targets)


def _element_kind_readable_from(statement, value, name):
    """The type ``name`` receives from a destructuring of a literal container.

    The complement of the "element is unreadable" rule in
    :func:`_store_may_bind_enterable`. When the right-hand side is a literal
    container *of literals*, the element this target receives is readable, and
    declining it reports a dead header as live:

        if flag:
            cs, other = (None, 1)

    ``cs`` receives ``None``. ``with cs:`` raises ``TypeError`` on both paths,
    so the assert is dead, but the unreadable-element rule declined and
    reported it live.

    The target is walked in lockstep with the container's elements, because
    that is what Python's unpacking does. A star target breaks the lockstep
    and is answered by :func:`_binds_a_starred_name` before this is reached.

    Returns the element's type name, or ``None`` when the element is not
    statically readable -- an unreadable element falls back to the caller's
    "possibly enterable" rule, which is the safe direction.
    """
    if not isinstance(statement, ast.Assign) or not isinstance(value, (ast.Tuple, ast.List)):
        return None
    elements = list(value.elts)
    for target in statement.targets:
        kind = _pattern_kind(target, elements)
        if kind is not None and _pattern_binds_name(target, name):
            return kind
    return None


def _pattern_binds_name(target, name):
    """Does this assignment target list bind ``name``, however deeply nested?"""
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_pattern_binds_name(element, name) for element in target.elts)
    if isinstance(target, ast.Starred):
        return _pattern_binds_name(target.value, name)
    return False


def _pattern_kind(target, elements):
    """The type ``target`` receives from ``elements``, or ``None`` if unreadable.

    Walks the assignment pattern in lockstep with the right-hand side's
    elements, because that is what Python's unpacking does. A star consumes a
    variable-length run and breaks the lockstep, so it answers ``None`` and is
    left to :func:`_binds_a_starred_name`. A pattern that runs out of elements
    raises at runtime, so the verdict is not readable either.
    """
    if isinstance(target, ast.Starred):
        return None
    if isinstance(target, (ast.Tuple, ast.List)):
        if any(_pattern_kind(element, elements) is None for element in target.elts):
            return None
        consumed = len(target.elts)
        if consumed > len(elements):
            return None
        return "tuple" if isinstance(target, ast.Tuple) else "list"
    if isinstance(target, ast.Name):
        return _literal_runtime_type(elements[0]) if elements else None
    return None


def _statement_never_runs(statement, function):
    """Is this store written inside a branch that provably never executes?

    ``if False: cs = nullcontext()`` never runs its body, so a carrier written
    earlier is still the settled value and the header still raises. Reading the
    store as a possible supersession declines the header and reports a dead
    assert as live, which is the damaging direction.

    The test is the loop spelling and the conjunction spelling of the
    ``if False:`` defeat the module already closes elsewhere:
    :func:`_falsy_literal` for a literal-false condition, and
    :func:`_is_empty_literal_iterable` for a loop over a container that yields
    nothing. A conjunction counts as false when *any* operand is a literal
    false, because ``flag and False`` is false for every value of ``flag``.

    Only shapes whose non-execution is decidable from the syntax are matched.
    A condition this cannot read is treated as possibly-true, which is the
    conservative direction.
    """
    # The recorded statement is the store itself -- the `cs = ...` assignment
    # inside the branch -- not the branch that encloses it, so the enclosing
    # blocks are found by walking *out* to the function and asking which of its
    # bodies contain this statement.
    for enclosing in _enclosing_blocks(statement, function):
        if isinstance(enclosing, ast.If) and _condition_is_never_true(enclosing.test, function):
            return True
        if isinstance(enclosing, ast.While) and _condition_is_never_true(enclosing.test, function):
            # `while False:` is the loop spelling of `if False:` -- the body
            # never runs, so it is never the last element either.
            return True
        if isinstance(enclosing, (ast.For, ast.AsyncFor)) and _is_empty_literal_iterable(
            enclosing.iter
        ):
            return True
    for node in ast.walk(statement):
        if isinstance(node, ast.If) and _condition_is_never_true(node.test, function):
            return True
        if isinstance(node, (ast.For, ast.AsyncFor)) and _is_empty_literal_iterable(node.iter):
            return True
    return False


def _enclosing_blocks(statement, function):
    """Every block node between ``statement`` and the top of ``function``."""
    if function is None:
        return ()
    found = []
    for node in ast.walk(function):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            for child in ast.iter_child_nodes(node):
                if child is statement or _contains(child, statement):
                    found.append(node)
                    break
    return found


def _condition_is_never_true(node, function=None):
    """A condition that is false for every binding, so its body never runs.

    ``function`` is the scope the condition appears in. It is not optional
    decoration: the ``set()``/``list()`` spelling of a falsy condition is only
    falsy while the name reaches the *builtin* constructor, so a function that
    rebinds the name locally -- ``def set(): return nullcontext()`` -- makes
    ``if set():`` a condition this cannot decide. Without the scope threaded
    in, that call was read as the builtin and a body CPython enters was
    reported as never running.
    """
    if _falsy_literal(node, function):
        return True
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        # `a and b` is false when *any* operand is false, so one literal-false
        # operand settles it. `a or b` is the opposite: `flag or False` is
        # true whenever `flag` is, so a false operand there says nothing and
        # the body can still run. Reading both the same way made
        # `if flag or False: cs = nullcontext()` report a live header dead.
        return any(_condition_is_never_true(value, function) for value in node.values)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        # `a or b` is true when *any* operand is true, so it is never-true
        # only when **every** operand is false. `flag or False` has a live
        # operand and stays reachable; `(False or ())` has none and never
        # runs its body. Reading an `or` like an `and` missed the second case
        # and reported a genuinely dead header live.
        return all(_condition_is_never_true(value, function) for value in node.values)
    return False


def _condition_is_always_true(node, function=None):
    """A condition that is true for every binding, so its body always runs.

    This is the mirror of :func:`_condition_is_never_true`, and it matters for
    the same reason. A store inside ``if True:`` is written as a conditional
    one, but the body runs on every import, so the store settles the name just
    as an unconditional store would:

        if True:
            cs = list()             # runs on every import; `cs` is a `list`
            def list(): return nullcontext()

    Read as genuinely conditional, the ``cs = list()`` above settled nothing
        and the name was left to a later store, which reported the `TypeError`
    CPython raises as a live header. Reading it as conditional is only right
        when the condition may or may not hold, so the always-true case has to
        be told apart rather than left to the conservative default.

    Only shapes decidable from the syntax are matched, exactly as in the
    never-true direction. A condition this cannot read is *not* always-true,
    which keeps the caller's existing treatment of it.
    """
    if _is_literal_true(node):
        return True
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        # `a or b` is true whenever *any* operand is, so one literal-true
        # operand settles it. The `and` case is the opposite: `flag and True`
        # is false whenever `flag` is, so a true operand there says nothing.
        return any(_condition_is_always_true(value, function) for value in node.values)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        return all(_condition_is_always_true(value, function) for value in node.values)
    return False


def _statement_always_runs(statement, function):
    """Is this store written inside a branch that provably always executes?

    The counterpart to :func:`_statement_never_runs`, used for the same
    purpose from the other side: a store that always runs settles the name, so
    it belongs with the unconditional ones in :func:`_stores_of` rather than
    being left to a later conditional store that may never have written.

    *Every* block between the store and the top of the function has to run
    every time, not just the innermost one. A literal-true branch nested inside
    a loop that never iterates never runs either:

        cs = nullcontext()
        for _ in ():
            if True:
                cs = list()          # the `if` is true, but the loop never runs

    Reading only the `if` settled the name on the `cs = list()` above, and the
    `nullcontext` that was in force was never reached -- a `TypeError`-raising
    header reported DEAD where CPython enters it. The same applies to a
    `while False:` body and to an `else` arm, which runs exactly when its `if`
    does *not*.
    """
    if function is None:
        return False
    blocks = _enclosing_blocks(statement, function)
    if not any(_is_always_true_branch(block, statement, function) for block in blocks):
        # The store is not inside an always-true branch at all, so there is
        # nothing to claim. This is the ordinary case and the cheap exit.
        return False
    # It is inside one, so every other block on the path has to run every time
    # as well. A never-true `if`, a loop over an empty literal, a `while False`
    # and an `else` beside a literal-true test are all read by the same
    # never-runs question, which is the safe direction: the store is left
    # conditional, so whatever the enclosing scope really does still decides.
    return all(
        _is_always_true_branch(block, statement, function) or not _block_never_runs(block, function)
        for block in blocks
    ) and not any(_statement_in_unreachable_arm(block, statement, function) for block in blocks)


def _is_always_true_branch(block, statement, function):
    """Is ``block`` an always-true ``if`` whose *body* holds ``statement``?"""
    return (
        isinstance(block, ast.If)
        and _condition_is_always_true(block.test, function)
        and _contains_any(block.body, statement)
    )


def _block_never_runs(block, function):
    """Is ``block`` a construct that provably never reaches its body?"""
    if isinstance(block, ast.If):
        return _condition_is_never_true(block.test, function)
    if isinstance(block, ast.While):
        return _condition_is_never_true(block.test, function)
    if isinstance(block, (ast.For, ast.AsyncFor)):
        return (
            _is_empty_literal_iterable(block.iter)
            or _is_empty_literal_string(block.iter)
            or _is_zero_argument_empty_container(block.iter, function)
        )
    return False


def _statement_in_unreachable_arm(block, statement, function=None):
    """Is ``statement`` in an arm of ``block`` that can never be taken?

    An ``else`` arm runs exactly when its ``if`` does **not**, so a store
    written in the ``else`` of an always-true test never runs:

        if True:
            if True:
                pass
            else:
                cs = list()          # the inner `if` is true, so this is dead

    The condition itself is true, which is what made the enclosing block look
    like an always-true branch, and the dead arm was then read as a store that
    always runs. `cs` stayed the `nullcontext` it was before the branch, so a
    header CPython enters came back DEAD.

    ``function`` is the scope this condition appears in, for the same reason
    :func:`_condition_is_always_true` takes it: the call spelling of a
    literal is only that literal while the name reaches the *builtin*, so a
    scope that rebinds it keeps the arm undecided.

    An ``elif`` needs no walk of its own. It is an ``else`` whose body is
    another ``if``, so a chain of always-true tests leaves every ``elif``
    dead, and the check below already covers everything under it:

        if True:
            pass
        elif True:
            cs = list()          # reached only when the test above is false

    An earlier version recursed into that nested ``if`` to read its own test,
    which was dead code: the direct check subsumes every ``elif`` body. The
    recursion was also unsound, because it fell back on the *parent's* test
    and so labelled a reachable `elif False:` arm dead.

    The always-true test changes the verdict, though not in a way any current
    row can pin. An ``else`` whose own test cannot be decided sits inside an
    always-true branch:

        cs = nullcontext()
        if True:
            if os.name:
                pass
            else:
                cs = list()

    Dropping the test reports that arm dead, which leaves the store
    undecided; keeping it settles the store. **Neither answer is correct** --
    the arm is not provably dead, and the module-carrier limit means the tool
    reports a header CPython enters as DEAD either way. The test is kept
    because "an undecidable test is not a dead arm" is the true statement, and
    because answering `True` for *any* ``if`` would additionally claim the arm
    of a never-true test is dead, which is wrong for a different reason.
    """
    if not isinstance(block, ast.If) or not _condition_is_always_true(block.test, function):
        # A non-`if`, or an `if` that can fail: its whole `else` arm is a
        # runnable one and there is nothing dead to report. See above: an
        # undecidable test lands here too, and has to.
        return False
    return _contains_any(block.orelse, statement)


def _is_empty_literal_string(node):
    """Is this iterable a string literal that is empty, so the loop never runs?

    ``for _ in "":`` is the same never-run body as ``for _ in ():`` -- the
    string has no elements to yield -- but it is an ``ast.Constant``, not one
    of the container literals :func:`_is_empty_literal_iterable` matches, so
    that helper answered `False` and the body was read as running. ``"a"`` has
    one element, so it is *not* empty and its body does run.
    """
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and not node.value


def _is_bare_name(target, name):
    """Is this loop target the plain name ``name``, with no unpacking?"""
    return isinstance(target, ast.Name) and target.id == name


def _loop_last_element_kind(iterable):
    """The type of the last element a literal loop iterable yields, or ``None``.

    ``for cs in (None,):`` yields ``None``, so the name holds ``None`` and
    cannot be entered. The **last** element is the one in force once the loop
    finishes, because the body may reassign the name on every pass: reading the
    first element instead gets ``for cs in (nullcontext(), None):`` backwards,
    and that error is in the damaging direction -- it reports a live assert
    dead. So the last element decides, and ``for cs in (None, nullcontext()):``
    correctly stays enterable.

    An empty or non-literal iterable answers ``None``, which leaves the
    caller's "possibly enterable" rule in force -- the safe direction.
    """
    if not isinstance(iterable, (ast.Tuple, ast.List)) or not iterable.elts:
        # A set literal is left out on purpose: `{nullcontext(), None}` has no
        # readable order, so which element the loop leaves bound is not
        # decidable from the syntax. Answering the first element would report
        # `for cs in {nullcontext(), None}:` dead, which is the damaging
        # direction. `for cs in set():` yields nothing and is already covered
        # by `_is_empty_literal_iterable`'s sibling case here.
        return None
    return _literal_runtime_type(iterable.elts[-1])


def _loop_body_rebinds_the_name(statement, name):
    """Does the loop body assign ``name``, so the yield is not what survives?

    A ``for`` target is re-bound on every pass, and the *body* runs after the
    assignment. If the body assigns the name again, the value left in force
    once the loop finishes is whatever the last pass's body stored, and the
    iterable's final element says nothing about it.

    Only the loop's own body counts, and nested scopes are skipped for the
    same reason :func:`_own_scope_bindings` skips them: a name bound inside a
    nested ``def`` is not the enclosing function's variable.
    """
    for node in statement.body:
        if _names_bound_in_scope(node, name):
            return True
        if _nested_rebinds(node, name):
            return True
    return False


def _collapse_loop_targets_into_bodies(entries):
    """Drop a loop target when its own body is the store that answers.

    A ``for cs in ...:`` target and an assignment to ``cs`` in that loop's
    body are not two competing bindings: the body runs *after* the target on
    every pass, so whatever the body stores is the value left behind and the
    target's element is gone. Returning the target as one of two competitors
    made

        import os as cs
        if flag:
            for cs in (None,):
                cs = nullcontext()

    ambiguous and reported a live header dead, where the name really is a
    context manager whenever the loop runs.

    The collapse is only sound when the body's store is what the caller will
    then answer from, and that requires the body's **last** rebind to be one
    this can read. A body whose final rebind is itself unreadable -- a second
    loop over something undecidable, a ``match`` capture -- leaves the value
    genuinely undecided, so the target stays and the two competitors make the
    answer ambiguous, which is the safe direction.

    Readable here means the same thing it means everywhere else: the body ends
    in a value this module can pin down by type (including a carrier and the
    builtin constructors, both of which :func:`_store_may_bind_enterable`
    reads). A suppressor call is the important case -- it *is* readable, so
    the collapse is kept and the suppressor survives to answer the header,
    which is exactly what a version that collapsed unconditionally and then
    answered from the retired target got wrong.

    A call that is *neither* a pinned type nor a resolved suppressor also
    counts as readable, and it has to: ``for cs in (None,): cs =
    nullcontext()`` leaves the name holding a real context manager on the path
    where the loop runs, so the target is definitively gone and the body is
    the only binding. Refusing to collapse such a store -- because a call has
    no pinned *type* -- left the target and the body competing, made the name
    ambiguous, and read the ambiguity as a possible suppressor, which retires
    a live header. That is the original #388 regression, so "has no readable
    type" cannot mean "not readable" here.
    """
    rebinding_targets = {
        id(entry[0].target): entry
        for entry in entries
        if isinstance(entry[0], (ast.For, ast.AsyncFor))
        and isinstance(entry[0].target, ast.Name)
        and _loop_body_rebinds_the_name(entry[0], entry[0].target.id)
    }
    if not rebinding_targets:
        return entries, False
    kept = []
    for entry in entries:
        statement = entry[0]
        if not (
            isinstance(statement, (ast.For, ast.AsyncFor))
            and id(statement.target) in rebinding_targets
        ):
            kept.append(entry)
            continue
        name = statement.target.id
        if _loop_body_last_store_is_readable(statement, name):
            continue
        kept.append(entry)
    return kept, len(kept) != len(entries)


def _loop_body_last_store_is_readable(loop, name):
    """Is the final store this loop's body makes to ``name`` one we can read?

    The body's statements are scanned in source order and the last binding of
    ``name`` wins, matching the way the binding table itself resolves "which
    store ran last".

    Only two shapes are *not* readable, and both are shapes whose surviving
    value depends on a decision this function would have to make twice:

    * a ``match`` capture, whose value is only bound when the clause is
      selected at runtime;
    * a nested ``for`` target, which recurses through this same question and
      so defers rather than answering.

    Everything else -- a literal, a carrier, a builtin constructor, an
    arbitrary call, even a ``del`` -- leaves the target definitively gone,
    because the body ran after it on every pass and rebinding is what remains.
    Restricting this to stores whose *type* can be pinned was the mistake that
    regressed #388: ``cs = nullcontext()`` has no pinned type, so refusing to
    collapse it kept the loop target in the competing set, made the name
    ambiguous, and read that ambiguity as a possible suppressor.
    """
    last = None
    for statement in _statements_binding_name(loop.body, name):
        last = statement
    if last is None:
        return False
    if isinstance(last, (ast.For, ast.AsyncFor)):
        # A nested loop rebinds the name under the same question this function
        # answers, so it is deferred rather than answered here. The outer
        # target stays in the competing set, which is the safe direction: two
        # candidates read as ambiguous, and ambiguous keeps the name a
        # possible suppressor instead of retiring one.
        return _loop_body_last_store_is_readable(last, name)
    # A `match` capture is the one shape whose value is only bound when the
    # clause is selected at runtime, so it is the one body store that is not
    # readable here.
    return not isinstance(last, (ast.Match, ast.MatchAs, ast.MatchStar))


def _statements_binding_name(body, name):
    """Statements in ``body`` that bind ``name``, in source order.

    Nested scopes are skipped for the same reason
    :func:`_names_bound_in_scope` skips them: a ``def`` inside the loop body
    binds its own names, not the enclosing function's. Blocks *within* the body
    are still walked, because a store inside an ``if`` in the body is a
    rebind that happens on some pass and the last one written is the one that
    decides the question.
    """
    found = []
    stack = list(reversed(body))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if any(_names_bound_by_statement(current, name)):
            found.append(current)
        stack.extend(reversed(list(ast.iter_child_nodes(current))))
    return found


def _names_bound_in_scope(node, name):
    """Every statement under ``node`` that binds ``name``, nested scopes aside."""
    found = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if any(_names_bound_by_statement(current, name)):
            found.append(current)
        stack.extend(ast.iter_child_nodes(current))
    return found


def _nested_rebinds(node, name):
    """Does ``node`` contain a binding of ``name`` in a nested block of its own?"""
    for statement in getattr(node, "body", []) or []:
        if _names_bound_in_scope(statement, name):
            return True
    for handler in getattr(node, "handlers", []) or []:
        if _names_bound_in_scope(handler, name):
            return True
    for case in getattr(node, "cases", []) or []:
        if _names_bound_in_scope(case, name):
            return True
    return False


def _store_may_bind_enterable(entry, name, function=None, starred_asked=False):
    """Could this store leave ``name`` holding something a ``with`` accepts?

    The narrow companion to the stale-carrier guard in :func:`_stores_of`.
    That guard asks whether a later conditional store makes the header's value
    undecidable, and the honest answer is only "yes" when the store could have
    put something *enterable* there. A store pinned to a type that cannot be
    entered settles the name just as surely as leaving it alone, because every
    path through the header then raises before the assert is evaluated.

    ``name`` is the name the header under test reads, because whether a store
    pins that name is a question about *that name's own target*. A statement
    can bind several names at once and, in a chained assignment, can mix a bare
    target with a destructured one; the answer differs per target.

    The same reading the literal-type rule already uses is applied, so the two
    cannot disagree about a value:

    * a *carrier* (``import os as cs``, ``def cs``, ``class cs``) is a module,
      a function or a type -- none of which can be entered;
    * a value-less store is `with ... as cs:`, `del cs` or `except E as cs:`,
      none of which leaves a usable manager bound;
    * a literal is read through :func:`_literal_runtime_type`; a literal whose
      type is in :data:`NON_CONTEXT_MANAGER_TYPES` cannot be entered;
    * a **starred** target binds a list whatever the elements are. This is a
      property of the target, not of the value, so it is decided first and
      without reading the right-hand side at all;
    * any other non-bare target -- a tuple or list target -- binds an *element*
      of the right-hand side, and the element's type is not readable from the
      container's syntax;
    * a call, an attribute, a subscript, a loop target or a `match` capture is
      **not** read. The value is whatever the call returns or the loop yields,
      which the syntax does not fix -- `nullcontext()` is a real context
      manager and the other calls may be too. Reading those as unenterable
      would drop a live assert, so they are reported as possibly-enterable and
      the caller declines, which is the safe direction.

    Direction: reporting a store as *not* possibly-enterable keeps a correct
    dead verdict; reporting it as possibly-enterable makes the caller decline.
    Only the second is the damaging one, so an unreadable value is always
    resolved toward "possibly enterable".
    """
    statement, value, _conditional = entry
    if isinstance(value, str):
        # A carrier: a module, a function or a class, none enterable.
        return False
    constructor = _builtin_constructor_kind(value, function)
    if constructor is not None:
        # `cs = int()` is decided by the callee, not by the call. Reading every
        # call as possibly-enterable is right for `nullcontext()` and wrong for
        # the builtin constructors, so a header the head reported live where
        # CPython raises `TypeError` on both paths is recovered here.
        if constructor is _RAISING_CONSTRUCTOR:
            # `range()` raises before binding, so this store never happens and
            # the carrier stays in force. Reporting it possibly-enterable
            # would decline a header CPython settles on the carrier.
            return False
        return constructor not in NON_CONTEXT_MANAGER_TYPES
    if not starred_asked and _binds_a_starred_name(statement, name):
        # A starred target pins the name to a **list** whatever the elements
        # are, so the right-hand side does not have to be readable at all:
        # `*cs, = (helper,)`, `*cs, = helper` and `first, *cs = pair` all leave
        # `cs` holding a list, which cannot be entered.
        #
        # This has to be asked *before* the readable-literal checks below.
        # Only the first of those is a tuple literal, so a version that tested
        # "is this a literal?" first answered "no" for `*cs, = helper` and for
        # every `= pair` form, reported the store as possibly-enterable, and
        # made the caller decline -- four more false-live regressions against a
        # master that answers them correctly. The type is a property of the
        # target, not of the value.
        return False
    if value is None:
        # `with ... as cs:`, `del cs`, `except E as cs:` and a loop target.
        # A loop target binds the next element of an iterable this rule cannot
        # read, so it is declined as unreadable below rather than assumed.
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            # A loop over a *literal* container yields a literal element, so
            # `for cs in (None,):` binds `None` and `with cs:` raises before
            # the assert. Declining every loop target left this one reported
            # live where CPython raises on both paths. The **last** element
            # decides, not the first: the body may rebind the name on each
            # pass, so the one in force afterwards is the final yield, and
            # reading the first would report `for cs in (nullcontext(), None):`
            # live.
            # An empty *builtin* container yields nothing, so the name is never
            # bound at all and the header raises `UnboundLocalError`; the
            # carrier never gets the chance to be superseded either way.
            #
            # Only the *no-argument* form is empty, for the same reason
            # `_falsy_literal` requires it: `for cs in set([1]):` binds the
            # element `1` and the loop runs once, so the name is very much
            # bound. Reading the argument-bearing call as empty here reported
            # a live header dead.
            empty_builtin = _is_zero_argument_empty_container(statement.iter, function)
            if _is_bare_name(statement.target, name) and empty_builtin:
                # `for cs in set():` and `for cs in ():` iterate zero times, so
                # the loop never binds the name. The carrier is untouched and
                # the header still raises on entry.
                return False
            if _is_bare_name(statement.target, name):
                if _loop_body_rebinds_the_name(statement, name):
                    # The body assigns the name, so whatever it leaves on the
                    # last pass is in force afterwards -- not the final yield.
                    # `for cs in (None,): cs = nullcontext()` binds a real
                    # context manager, so reading the iterable's last element
                    # would report a live header dead. The body's store may be
                    # any value, so the loop is declined as unreadable, which
                    # keeps the header live.
                    return True
                element = _loop_last_element_kind(statement.iter)
                if element is not None:
                    return element not in NON_CONTEXT_MANAGER_TYPES
            return True
        return bool(isinstance(statement, ast.Match))
    if not isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)):
        # A call is a call. `nullcontext()` returns a real context manager and
        # must not be read as unenterable here.
        return True
    if isinstance(statement, ast.Assign) and not _target_is_bare_name(statement, name):
        # A destructuring target: `cs, other = (...)` binds `cs` to one
        # *element* of the right-hand side, and the element's type is not
        # readable from the container's syntax. Reading the whole right-hand
        # side's type would call the name a tuple and drop a live assert, so
        # this is reported as possibly enterable and the caller declines.
        #
        # This is about *this name's* own target, not about the statement.
        # A chained assignment mixes the two, and the two spellings are not
        # equivalent:
        #
        #     *cs, = (helper,)              # `cs` is a list
        #     cs = (other, x) = (helper, 1)  # `cs` is the whole RHS tuple
        #
        # The second is pinned non-enterable too, so declining it would replace
        # a correct dead verdict with a false-live one; the direct `cs` target
        # in a chain is a bare name and falls through to the literal reading
        # below, which is right for it. Reading the statement as "destructuring
        # happened" and returning early for both got that wrong in six
        # fixtures. The starred half is answered above, before the literal
        # checks, because it does not depend on the right-hand side at all.
        element = _element_kind_readable_from(statement, value, name)
        if element is not None:
            return element not in NON_CONTEXT_MANAGER_TYPES
        return True
    kind = _literal_runtime_type(value)
    if kind is None:
        return True
    return kind not in NON_CONTEXT_MANAGER_TYPES


def _carrier_may_have_been_superseded(
    entries, orders, index=None, bound=None, function=None, name=None
):
    """May a store a conditional binding performed have replaced the carrier?

    #388. :func:`_stores_of` keeps only *unconditional* stores, because a
    store nested in a block may not have run. That is the right default, but
    it has one damaging consequence for a name that also carries a carrier:
    an unconditional carrier survives a *conditional* non-carrier store that
    follows it, so the stale carrier wins and the rule answers ``defeated``
    from a value the name may no longer hold.

    Given

        def outer(x, flag, helper):
            import os as cs                 # carrier: pins cs to module os
            if flag:
                cs = contextlib.nullcontext()   # may supersede the carrier
            with cs:
                assert x != 1               # FIRES: cs is a nullcontext

    ``with cs:`` succeeds and the assert runs, so calling it defeated drops a
    live pinned contract. When that happens the value is genuinely
    undecidable -- the carrier runs first, the later store *may* replace it --
    so the answer is to decline rather than guess.

    The ordering test is what keeps this narrow. A conditional store *before*
    the last carrier really is superseded by it and needs no special case, and
    a carrier followed only by further carriers is decided by the last one.

    `except ... as cs:` is excluded because it *unbinds* rather than
    supersedes: CPython deletes the name when the handler exits, so the
    earlier carrier is what remains in force. :func:`_stores_of` makes the same
    exception for the same reason, and without it here a try/except that
    merely mentions the name would flip a correct ``defeated`` to
    ``enforced``.

    ``del cs`` is excluded for the same reason, and the exclusion is what keeps
    this rule from inventing a dead assert. A delete *unbinds* the name; it
    never installs an enterable value in its place:

        def outer(flag):
            import os as cs
            if flag:
                del cs
            with cs:            # never runs the body, on either path
                assert False

    With ``flag=False`` the carrier is still in force and the header raises
    ``TypeError``; with ``flag=True`` CPython has deleted the name and the
    header raises ``UnboundLocalError``. The assert is unreachable either way.
    Counting the delete as a superseding store declines the header, and a
    decline reports ``enforced`` -- so the delete would turn a *dead* assert
    into a purportedly load-bearing one, the opposite error from the one this
    rule exists to prevent. Reviewed as a blocking finding on #388.

    The same reasoning settles the other forms that cannot leave an enterable
    value, and settling them by *category* rather than one at a time is the
    point. The question this predicate has to answer is not "is this store
    conditional?" but **"can this store leave something enterable bound to
    ``name``?"** Anything that cannot is excluded, because counting it would
    decline the header and manufacture a dead assert.

    ``bound`` and ``function`` are what let :func:`_may_bind_something_enterable`
    answer the one category that is not decidable from the store node alone. A
    ``with ... as cs:`` binds ``__enter__``'s return value, so *which* value
    depends on the context expression, and that expression is only readable
    with the import-alias map. ``function`` is the owning scope, needed for
    the same reason ``_scope_body_nodes`` takes it: to tell a store that binds
    *this* name from one that binds a nested scope's local of the same name.

    ``index`` restricts the question to the stores that precede the queried
    ``with`` header, so a later store cannot be read backwards into an earlier
    one. It is ``None`` for the module path, where the header is evaluated
    only after every module-level statement has run.
    """
    carriers = [
        orders[id(statement)]
        for statement, value, conditional in entries
        if not conditional
        and isinstance(value, str)
        and (index is None or orders[id(statement)] <= index)
    ]
    if not carriers:
        return False
    last_carrier = max(carriers)
    return any(
        conditional
        and _may_bind_something_enterable(statement, value, name, bound, function)
        and not _statement_never_runs(statement, function)
        and orders[id(statement)] > last_carrier
        and (index is None or orders[id(statement)] <= index)
        for statement, value, conditional in entries
    )


def _may_bind_something_enterable(statement, value, name=None, bound=None, function=None):
    """Can this store leave a name bound to something ``with`` can enter?

    #388. The decline in :func:`_carrier_may_have_been_superseded` is only
    safe when the competing store genuinely might have replaced the carrier
    with an *enterable* value. Counting a store that cannot would decline the
    header, and a decline reports ``enforced`` -- so it would manufacture a
    dead assert, the opposite error from the one the decline exists to
    prevent.

    Four families cannot, and each was measured against CPython 3.12.14 by
    executing the header rather than by reasoning about it:

    - ``except ... as cs:`` and ``del cs`` **unbind**. CPython deletes the
      name, so the earlier carrier is what remains in force.
    - A store inside a nested ``def``/``lambda``/``class`` body binds *that*
      scope's local, not this function's name. This is the boundary
      :func:`_scope_body_nodes` already draws elsewhere in this module.
    - A ``with ... as cs:`` whose context expression is a **known
      ``None``-returning** context manager. See
      :func:`_with_binds_a_known_non_enterable`.
    - ``*cs, = (...)`` builds a **list**, which cannot be entered. The
      starred target is not visible on the recorded value, so it is read off
      the statement's target list.

    Everything else -- a call, a walrus, a loop target, a capture, a
    destructuring element, and a ``with`` on an arbitrary expression -- *may*
    bind something enterable, so it counts and the header is declined.
    """
    if isinstance(value, str):
        # A carrier recorded by `_carrier_runtime_kinds`: a module, a class
        # or a function. None of those has `__enter__`.
        return False
    if isinstance(statement, (ast.ExceptHandler, ast.Delete)):
        # Both *unbind*. `del cs` removes the name outright, and CPython
        # deletes an `except ... as cs` name when the handler exits, so
        # neither can install an enterable value over the carrier.
        return False
    if function is not None and not _store_is_in_scope(statement, function):
        # A nested `def`/`lambda`/`class` body has its own locals. A store
        # there binds *that* scope's `cs`, so the carrier in the enclosing
        # scope is untouched and still in force. Measured:
        #
        #     def outer(x, flag):
        #         import os as cs
        #         if flag:
        #             def inner():
        #                 cs = contextlib.nullcontext()
        #         with cs:          # cs is still the module -> TypeError
        #             assert x != 1
        #
        # The assert never runs on either path, so the carrier -- not the
        # nested store -- is what decides the header.
        return False
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return not _with_binds_a_known_non_enterable(statement, name, bound)
    # `*cs, = (...)` binds a list, which cannot be entered, so the header
    # cannot run the assert. The recorded `value` is the whole right-hand side
    # rather than the element, so the starred *target* has to be read off the
    # statement itself.
    #
    # This branch owns that question for the carrier path rather than sharing
    # it with :func:`_store_may_bind_enterable`, which answers the same thing
    # through `_binds_a_starred_name`. The two must not both answer it: while
    # both did, neutralising `_target_is_starred` changed nothing, so the
    # exclusion it names was not the one keeping the store out of the decline
    # -- and `test_each_non_enterable_exclusion_is_load_bearing` says so
    # explicitly. The value is read from `_target_is_starred` below, so the
    # helper that names the exclusion stays load-bearing.
    if name is not None and _target_is_starred(statement, name):
        return False
    # #388. The four families above are decidable from the store node alone.
    # Everything else is decided by :func:`_store_may_bind_enterable`, which
    # reads the *value* as well: it knows the builtin constructors
    # (``cs = int()`` leaves an ``int``), distinguishes a bare target from a
    # destructured one, and reads a loop's last element or its body's rebind.
    #
    # Delegating is what keeps the two readings of the same store from
    # disagreeing. An earlier version answered every remaining store as
    # "possibly enterable" on its own, which is the safe direction in
    # isolation -- but the stale-carrier guard in
    # :func:`_carrier_may_have_been_superseded` calls this for a *conditional*
    # store, and a store already pinned to a non-enterable type cannot
    # supersede the carrier. Answering `True` there declines the header, and a
    # decline reports ``enforced`` -- so 47 rows of
    # `test_a_nonenterable_conditional_store_does_not_revive_a_stale_carrier`
    # read a genuinely unreachable assert as load-bearing. Reading the value
    # through the shared helper settles all three suites at once.
    # The starred question is answered above, from `_target_is_starred`, so the
    # shared helper is told to skip its own `_binds_a_starred_name` branch.
    # The two disagree on purpose: `_binds_a_starred_name` also matches
    # `first, *cs = pair`, where `cs` holds an *element* of the right-hand
    # side rather than a list, and the carrier path must not exclude that
    # case. Letting the shared helper answer as well would make both branches
    # decide the row, and neutralising `_target_is_starred` alone would then
    # change nothing -- which is the dead-exclusion that
    # `test_each_non_enterable_exclusion_is_load_bearing` exists to catch.
    return _store_may_bind_enterable((statement, value, True), name, function, starred_asked=True)


def _store_is_in_scope(statement, function):
    """Is ``statement`` part of ``function``'s own scope?

    ``_store_bindings`` records a store nested in a nested ``def``/``lambda``/
    ``class`` as an ordinary conditional binding of the same *spelling* of the
    name, because the name it binds really is called ``cs``. What differs is
    the *scope* it lands in: a class body is its own namespace and a nested
    ``def`` owns its locals, so neither rebinds the enclosing function's
    ``cs``. Measured:

        def outer(x, flag):
            import os as cs
            if flag:
                def inner():
                    cs = contextlib.nullcontext()
            with cs:          # cs is still the module -> TypeError, dead
                assert x != 1

    The boundary is the same one :func:`_scope_body_nodes` draws, and it is
    asked here by membership in that set rather than by re-walking, so the two
    rules cannot drift apart on what counts as a nested scope.

    A ``global`` (or ``nonlocal``) declaration moves the store *out* of the
    nested scope. At module level the ``global`` spelling is the whole of
    #375's `_rebind` shape:

        def _rebind():
            global cs
            cs = contextlib.nullcontext()

    writes the *module's* ``cs``, so the carrier really is superseded and the
    header really is live. Reading it as a nested local -- which is what
    membership alone would do -- would exclude the only store that decides the
    question and answer `defeated` on an assert CPython evaluates. So the
    nested scope is only a real boundary for a name that is *not* declared in
    an enclosing scope from within it.

    A declaration only moves a store out of the nested scope when it targets
    the scope that actually *owns* the queried carrier. ``global cs`` names the
    module namespace, so it supersedes a module-level ``cs`` and nothing else:

        def outer(x, flag):
            import os as cs
            if flag:
                def inner():
                    global cs        # writes the MODULE's cs
                    cs = contextlib.nullcontext()
            with cs:                # `outer`'s own local cs -> TypeError
                assert x != 1

    Here ``outer``'s ``cs`` is a function local, the nested store lands in the
    module, and the header is entered with the module -- so it raises on both
    paths and the assert is dead. Reading the ``global`` as if it governed
    ``outer``'s binding made the checker report that dead assert as live.
    ``nonlocal`` is the mirror image: it names a *function* scope, so it
    supersedes a function-scope carrier and says nothing about a module one.
    The two are told apart by the scope being queried, which is why this test
    reads ``function`` and not just the store.

    Only the declarations that *govern* the owning scope count. ``ast.walk``
    descends into scopes of its own, and a declaration in a grandchild says
    nothing about the store beside it:

        def outer(x, flag):
            import os as cs
            if flag:
                def inner():
                    cs = contextlib.nullcontext()      # inner's local
                    def grandchild():
                        global cs                      # no effect on inner
            with cs:                                  # still the module
                assert x != 1

    :func:`_scope_body_nodes` draws the boundary, and it is asked here by
    iteration rather than by re-walking, so the two rules cannot drift apart
    on what counts as a nested scope.
    """
    if any(node is statement for node in _scope_body_nodes(function)):
        return True
    owner = _owning_scope_of(statement, function)
    if owner is None:
        return False
    # `global` and `nonlocal` are told apart by the namespace they name: a
    # module-level carrier is the one a `global` can supersede, and a
    # function-scope carrier is the one a `nonlocal` can. Asking the
    # declaration alone would have each of them claim the other's case.
    declaration = ast.Global if isinstance(function, ast.Module) else ast.Nonlocal
    return _declares(owner, _store_target_names_of(statement), declaration)


def _target_is_starred(statement, name):
    """Does this store bind the name through an ``ast.Starred`` target?

    ``*cs, = (...)`` builds a list, and ``cs, *rest = (...)`` does not bind
    the starred name at all. Only the first shape binds a non-enterable value,
    so the two are told apart by asking whether ``name`` is *the* starred
    target -- which is why the name is a parameter. Matching any ``Starred``
    in the target list would exclude the second shape too, and there the
    starred element is discarded while ``cs`` keeps a perfectly enterable
    value, so excluding it would be wrong in the damaging direction.

    The recorded ``value`` is the whole right-hand side, so the starred
    *target* has to be read off the statement; :func:`_literal_runtime_type`
    sees the ``ast.Starred`` from the opposite direction, on the value, and
    reads the same list type for the non-carrier path.
    """
    targets = []
    if isinstance(statement, (ast.AnnAssign, ast.NamedExpr)):
        targets = [statement.target]
    elif isinstance(statement, ast.Assign):
        targets = statement.targets
    return any(
        isinstance(node, ast.Starred) and isinstance(node.value, ast.Name) and node.value.id == name
        for target in targets
        for node in ast.walk(target)
    )


def _store_target_names_of(statement):
    """The names a single store statement binds, or ``[]`` for a non-store.

    Thin wrapper over :func:`_store_target_names` so the scope test can ask
    the question in terms of one statement. A node that is not one of the
    store forms binds nothing here and is reported as binding nothing, which
    keeps the caller from having to enumerate the forms a second time.
    """
    if isinstance(statement, ast.Assign):
        return _store_target_names(statement.targets)
    if isinstance(statement, ast.AnnAssign):
        return _store_target_names([statement.target])
    if isinstance(statement, ast.NamedExpr):
        return _store_target_names([statement.target])
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return _store_target_names([statement.target])
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _store_target_names(
            [item.optional_vars for item in statement.items if item.optional_vars is not None]
        )
    if isinstance(statement, ast.Delete):
        return _store_target_names(statement.targets)
    if isinstance(statement, ast.ExceptHandler):
        return [statement.name] if statement.name is not None else []
    return []


def _owning_scope_of(statement, function):
    """The nested ``def``/``lambda``/``class`` that ``statement`` sits inside.

    ``None`` when the statement is part of ``function``'s own body, or when
    no single nested scope contains it. Only the *nearest* enclosing scope is
    returned, because a ``global`` declaration in an inner scope says nothing
    about an intermediate one, and a doubly-nested store writes to whichever
    scope declared its name.
    """
    return _nearest_scope_containing(function, statement)


def _nearest_scope_containing(scope, statement):
    """The innermost scope strictly inside ``scope`` that contains ``statement``."""
    for node in ast.iter_child_nodes(scope):
        if any(child is statement for child in ast.walk(node)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                # An inner scope wins: descend and prefer whatever is closest.
                return _nearest_scope_containing(node, statement) or node
            # `node` is a plain statement that contains the store, so the
            # scope boundary is further out; keep looking among the siblings
            # that also contain it.
            deeper = _nearest_scope_containing(node, statement)
            if deeper is not None:
                return deeper
    return None


def _declares(scope, names, kind):
    """Does ``scope`` govern its own stores with a ``kind`` declaration?

    ``global cs`` is the declaration that makes a nested store write to the
    *module's* binding, and it is what separates #375's ``_rebind`` -- whose
    store really does supersede a module carrier -- from a nested ``def`` that
    merely shadows the name. ``nonlocal cs`` does the same for the nearest
    *enclosing function* instead. The caller picks between them by the scope it
    is reasoning about, because the two name different namespaces.

    Only declarations that govern ``scope`` are read. A ``global`` in a nested
    ``def`` binds that ``def``'s stores, so it has no say over a store sitting
    in ``scope`` itself -- and ``ast.walk`` cannot draw that line, since it
    descends straight through the nested scope. :func:`_scope_body_nodes` is
    the boundary this module draws everywhere else, so it is what asks, and
    the nodes it yields are read *directly*.

    Reading them directly is what makes the boundary hold at any depth. A
    second walk over each yielded node re-enters a nested scope reached
    through a wrapper, so a ``nonlocal`` in a grandchild written under an
    ``if`` was still attributed to its grandparent:

        def inner():
            cs = contextlib.nullcontext()
            if True:
                def grandchild():
                    nonlocal cs

    ``_scope_body_nodes`` yields the ``FunctionDef`` and stops, and the check
    is a type test on the node itself, so the declaration inside it is never
    reached. A wrapper changes nothing, because ``_scope_body_nodes``
    descends through statements and yields the nested scope wherever it sits.

    Neither declaration takes effect unless the name is actually bound
    somewhere in the scope, so the answer here is the necessary half of the
    test and the target list is what supplies the other.
    """
    wanted = set(names)
    if not wanted:
        return False
    for node in _scope_body_nodes(scope):
        if isinstance(node, kind) and wanted.intersection(node.names):
            return True
    return False


#: Dotted paths whose ``__enter__`` provably returns ``None``.
#:
#: ``with EXPR as cs:`` binds ``EXPR.__enter__()``, so whether the header
#: still holds an *enterable* value is a question about the manager's
#: ``__enter__``, not about the syntax of the ``with``. It is decidable only
#: for a closed set of callables whose ``__enter__`` is known to return
#: ``None``:
#:
#: * ``contextlib.suppress.__enter__`` is a bare ``pass``, so the call returns
#:   ``None`` implicitly however the manager was built.
#: * ``contextlib.nullcontext.__enter__`` is ``return None`` -- it is
#:   documented to return None and that is what makes it a *null* context --
#:   but *only* when the manager was built with no ``enter_result``. Given one
#:   it returns that argument, so the spelling alone does not settle the
#:   bound value. :func:`_binds_a_null_returning_context` therefore asks about
#:   the call rather than the name.
#:
#: The ``asyncio`` spelling from :data:`SUPPRESSING_CONTEXTS` is deliberately
#: **not** inherited here, and the reason is a measured one. ``asyncio`` has no
#: ``suppress`` in CPython 3.12.14, so the name is only ever whatever the
#: program put there:
#:
#:     asyncio.suppress = CM       # `CM.__enter__` returns self
#:     with asyncio.suppress() as cs:
#:         pass                    # `cs` is a CM -- `with cs:` enters
#
#: A module attribute is rebindable, and reading the spelling as a fixed
#: ``None``-returning callable excluded a store that really does bind an
#: enterable value. ``contextlib.suppress`` has no such spelling problem: the
#: name resolves to the library object.
#:
#: Nothing else belongs here. A user-defined ``CM()`` whose ``__enter__``
#: returns ``self`` is syntactically identical to one that returns ``None``,
#: so treating *any* ``with`` as non-enterable would report the header dead
#: on an assert CPython really evaluates. Measured on CPython 3.12.14:
#
#:     def outer(x, flag):
#:         import os as cs
#:         if flag:
#:             with CM() as cs:      # flag -> cs is the CM, `with cs:` enters
#:                 pass
#:         with cs:                  # and `assert x != 1` FIRES
#:             assert x != 1
#
#: An unrecognised manager therefore *counts* as a superseding store and the
#: header is declined, which is the direction this module takes when a value
#: cannot be read: a decline reports the assert live, and calling a live
#: assert dead is the damaging direction.
NULL_RETURNING_CONTEXTS = ("contextlib.nullcontext", "contextlib.suppress")


def _with_binds_a_known_non_enterable(statement, name, bound=None):
    """Does this ``with`` bind a name to a provably non-enterable value?

    ``with EXPR as cs:`` binds ``EXPR.__enter__()``. When ``EXPR`` resolves to
    one of :data:`NULL_RETURNING_CONTEXTS` -- and binds no ``enter_result`` --
    the bound value is ``None``, cannot be entered, and so cannot supersede a
    carrier with anything enterable; the carrier stays in force. Every other
    manager may return an enterable object and is reported as a genuine
    superseder.

    Only the item that binds ``name`` is asked about. A ``with`` binds each of
    its items independently, so a non-enterable *sibling* says nothing about
    the item the queried name came from:

        with CM() as cs, contextlib.nullcontext() as other:
            pass

    ``cs`` receives ``CM().__enter__()`` -- a real object -- and ``other``'s
    ``None`` is not what the header goes on to enter. Reading the whole
    statement instead of the one item excluded a store that *can* bind an
    enterable value, which is the damaging direction: a decline reports the
    assert live, so a dead assert was certified as load-bearing.

    ``contextlib.nullcontext(CM())`` is the same argument one level in. The
    argument is the value its ``__enter__`` returns, so this call binds
    ``CM()`` and the header is entered with an object. Only the no-argument
    spelling returns ``None``, and requiring an empty argument list is what
    keeps the two apart.

    The alias map resolves every spelling: ``contextlib.nullcontext()``,
    ``from contextlib import nullcontext as nc`` and a re-exported attribute
    all collapse to the same dotted path before the comparison.

    A ``with`` whose *unbound* value is the queried name (``with cs:`` with no
    ``as`` clause) binds nothing, so no item here matches the name and the
    answer is ``False`` -- the store is not a rebind of the name at all, and
    :func:`_store_bindings` only records items that carry an ``optional_vars``.

    When *several* items bind the same name, the **last** one is what the
    header goes on to enter, because CPython processes the items in order:

        with contextlib.nullcontext() as cs, CM() as cs:
            pass

    leaves ``cs`` holding ``CM().__enter__()``, not ``None``. Reading any
    qualifying item as the answer excluded the whole statement, so a name an
    enterable item re-bound last was reported as a ``None``. Only the last
    binder is asked about, and a ``with`` that rebinds the name more than once
    is not claimed by the first item to match.

    Reading the *last* binder still does not make this rule reach the end-to-end
    verdict on such a statement. A ``with`` that binds the same name twice
    leaves ``_store_bindings`` with two entries for it, and #308 reads a name
    bound by several stores as ambiguous and reports the assert as swallowed
    before this rule is consulted at all. That is the pre-existing answer on
    ``ed9d9b0`` for this shape, it is pinned at
    ``FUNCTION_CARRIER_SUPERSESSION_LIMIT_SHAPES``, and it is not introduced
    here. What this rule controls is narrower and is what the decline asks:
    whether *the* store can be a superseding one, and it now answers from the
    binder whose value survives.
    """
    binding = None
    for item in statement.items:
        if item.optional_vars is not None and name in _store_target_names([item.optional_vars]):
            binding = item
    if binding is None:
        return False
    return _binds_a_null_returning_context(binding.context_expr, bound or {})


def _binds_a_null_returning_context(expression, bound):
    """Does ``expression`` call a known ``None``-returning manager outright?

    A call in :data:`NULL_RETURNING_CONTEXTS` binds its manager's
    ``__enter__`` result, and the manager is only *known* to return ``None``
    where that is decided by the call itself rather than by an argument:

    * ``contextlib.suppress`` has a ``__enter__`` that is a bare ``pass``, so
      it returns ``None`` however it is called. That is why its arguments are
      not inspected: it takes its exceptions as arguments, and excluding the
      call for carrying one would give up the ``suppress(AssertionError)``
      spelling.
    * ``contextlib.nullcontext`` passes its ``enter_result`` straight
      through, so the bound value is whatever the argument evaluates to.

    The second member is why a truthiness or emptiness test is not enough.
    Measured on CPython 3.12.14 across the whole ``enter_result`` space, only
    a *knowably enterable* argument leaves a value a later ``with cs:`` can
    enter:

        nullcontext()                      -> None   (not enterable)
        nullcontext(enter_result=0)       -> 0      (not enterable)
        nullcontext(enter_result=())      -> ()     (not enterable)
        nullcontext(enter_result=(CM(),)) -> tuple  (not enterable)
        nullcontext(enter_result=[CM()])  -> list   (not enterable)
        nullcontext(enter_result=CM())    -> CM     (ENTERABLE)
        nullcontext(CM())                 -> CM     (ENTERABLE)
        nullcontext(*[CM()])              -> CM     (ENTERABLE)

    The tuple and list rows are the ones a truthiness test gets wrong: the
    container holds a context manager and is still not one. So the argument
    has to be *typed*, not merely evaluated -- see
    :func:`_null_context_binds_an_enterable`.

    A call that binds its parameter twice, or names one that does not exist,
    is a ``TypeError`` at the call: the statement raises before it can bind
    anything, so there is no store to exclude. That is
    :func:`_null_context_call_raises`.
    """
    if not isinstance(expression, ast.Call):
        return False
    dotted = _resolved_dotted(expression, bound)
    if dotted not in NULL_RETURNING_CONTEXTS:
        return False
    if dotted == "contextlib.suppress":
        return True
    if _null_context_call_raises(expression):
        # `enter_result` is the *only* parameter, so a second value for it is
        # a `TypeError` at the call. The whole `with` statement raises before
        # the header is entered, so there is no store here to exclude and the
        # assert under it is unreachable. Counting the call as a superseding
        # store would decline the header and report a dead assert as
        # load-bearing, so it keeps the exclusion.
        return True
    return not _null_context_binds_an_enterable(expression, bound)


#: A ``with`` binds ``EXPR.__enter__()``, so what decides whether the name
#: still holds something enterable is the *type* of the value
#: ``enter_result`` evaluates to -- not whether it is ``None``, and not how it
#: is spelled. Measured on CPython 3.12.14 over the ``enter_result`` space:
#:
#:     nullcontext()                      -> None   (not enterable)
#:     nullcontext(enter_result=0)       -> 0      (not enterable)
#:     nullcontext(enter_result=())      -> ()     (not enterable)
#:     nullcontext(enter_result=(CM(),)) -> tuple  (not enterable)
#:     nullcontext(enter_result=[CM()])  -> list   (not enterable)
#:     nullcontext(enter_result=CM())    -> CM     (ENTERABLE)
#:     nullcontext(CM())                 -> CM     (ENTERABLE)
#:
#: A literal *container* holding a manager is still not one, so the rule reads
#: the value's type. It cannot read a runtime type off an expression, and the
#: two ways of guessing each get one of these rows wrong:
#:
#: * "any ``ast.Call`` binds an enterable" is wrong for ``list()``, ``int()``,
#:   ``dict()``, ``set()`` and every other builtin, which bind objects with no
#:   ``__enter__``. :data:`_NON_ENTERABLE_BUILTIN_CALLS` is that list.
#: * "only a bare ``ast.Call`` binds an enterable" is wrong for every other
#:   *spelling* of the same value -- a walrus, an ``IfExp``, a ``BoolOp``, a
#:   subscript, a local name, a computed ``*`` or ``**`` -- and excluding those
#:   declares a live contract dead, which is the damaging direction.
#:
#: So the classifier is an **allowlist of known-non-enterable types** and
#: everything else answers "may be enterable". See
#: :func:`_value_may_be_enterable`.
_ENTERABLE_CLASS_NAMES = (ast.Name,)

#: Builtin containers and scalars whose instances have no ``__enter__``.
#:
#: ``frozenset``, ``set``, ``dict``, ``list``, ``tuple``, ``range``,
#: ``enumerate``, ``zip``, ``map``, ``filter`` and the numeric/``bytes``
#: builtins are all *calls* that bind something a ``with`` cannot enter, so a
#: rule reading "any call is enterable" would declare every one of them live.
#: Measured on CPython 3.12.14 by entering each result. This is the call
#: arm of the allowlist; the display arm is the container literals below.
#:
#: The rule is by *name*, not by arity or by category: ``bool()``,
#: ``object()``, ``complex()`` and ``bytearray()`` are as fixed as ``list()``
#: even though none of them is a container, and leaving them out made
#: ``nullcontext(enter_result=bool())`` a live superseder on a header that
#: raises. Every entry below was measured by entering its zero-argument result
#: on CPython 3.12.14. Adding a name is a claim that ``NAME()`` has no
#: ``__enter__``, so it is checked, not assumed.
_NON_ENTERABLE_BUILTIN_CALLS = frozenset(
    {
        "bool",
        "bytearray",
        "bytes",
        "complex",
        "dict",
        "enumerate",
        "filter",
        "float",
        "frozenset",
        "int",
        "list",
        "map",
        "object",
        "range",
        "set",
        "str",
        "tuple",
        "zip",
    }
)

#: Values whose *type the language pins* to something with no ``__enter__``.
#:
#: This is an allowlist, so membership is the only thing that may decline a
#: store. Each entry is a type CPython decides, not a guess about the program:
#:
#: * a ``None`` constant is the value the no-argument form produces;
#: * a numeric / ``bytes`` / ``str`` constant is a literal of that type;
#: * a list, tuple, set or dict *display* builds a container, and a container
#:   is not a context manager however it is filled -- ``[CM()]`` holds a
#:   manager and is still a list.
#:
#: A call to a builtin in :data:`_NON_ENTERABLE_BUILTIN_CALLS` is the same
#: decision made through a call rather than a display, and is kept in
#: :func:`_value_may_be_enterable` so the builtin list and the display rule
#: read as one allowlist.
_NON_ENTERABLE_LITERAL_TYPES = (type(None), int, float, complex, str, bytes, bool)


def _null_context_binds_an_enterable(expression, bound):
    """Does this ``nullcontext`` call bind something a ``with`` could enter?

    ``nullcontext.__enter__`` is ``return self.enter_result``, so the bound
    value *is* the argument, and the question is whether its type has
    ``__enter__``. The answer comes from a known-non-enterable allowlist: only
    a value whose type the source pins may answer "no", and everything else
    answers "maybe", which is what declines the header.

    The "maybe" default is the load-bearing decision, and it is forced by the
    two errors not being symmetric. Declining reports the assert **live**, so
    reading an unreadable value as non-enterable calls a live contract dead --
    the damaging direction. :func:`_value_may_be_enterable` documents the
    choice and the rows that hold it to CPython.
    """
    for value in _null_context_enter_results(expression, bound):
        if _value_may_be_enterable(value, bound):
            return True
    return False


def _value_may_be_enterable(node, bound):
    """May ``node`` evaluate to something a ``with`` header can be entered on?

    The answer is ``False`` only for a value whose type is *pinned* to a
    non-enterable one, and ``True`` for everything else. A ``None`` constant, a
    scalar constant, a container display, and a call to a known non-enterable
    builtin are the pinned cases.

    Every other expression answers ``True``, and the reason is the direction of
    each error rather than a preference. ``True`` declines the header, so the
    assert is reported **live**; a live assert reported dead is the damaging
    error, and a dead assert reported live is recoverable. The two are
    therefore not symmetric, and the unreadable cases have to land on the
    recoverable one:

        with contextlib.nullcontext(enter_result=[CM()][0]) as cs:
            pass

    binds a ``CM`` -- the subscript is computed, so the source does not pin its
    type -- and the assert fires. The same value reached as a walrus, a
    conditional, a ``BoolOp``, a local name, or a computed ``*``/``**`` is the
    same live header, and no spelling of it may be read as non-enterable.

    The recursion is over the *values* a container display holds, never over a
    call's arguments: ``nullcontext(enter_result=(CM(),))`` binds a **tuple**,
    and the tuple is decided by the display rule. An earlier version recursed
    into a one-element tuple with the *call* classifier, which read ``.args``
    off an ``ast.Tuple`` and raised ``AttributeError`` on
    ``nullcontext(enter_result=((CM(),),))`` -- a crash where CPython simply
    binds a tuple and raises ``TypeError`` entering it.
    """
    if isinstance(node, ast.Constant):
        return not isinstance(node.value, _NON_ENTERABLE_LITERAL_TYPES)
    if isinstance(node, (ast.JoinedStr, ast.FormattedValue)):
        # An f-string is a `str` whatever it interpolates, and `ast` gives it
        # its own node type rather than folding it into `ast.Constant` -- so
        # `nullcontext(enter_result=f"{x}")` binds a string and cannot be
        # entered. Left to the fall-through it answered "may be enterable" and
        # declared a dead header live. Measured on CPython 3.12.14 by entering
        # the result.
        return False
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        # A container *display* builds a container, whatever fills it.
        return False
    if isinstance(node, ast.Dict):
        # Same for a dict display. A `**` of a dict *display* is different --
        # that one names parameters -- and is handled by the argument model.
        return False
    if isinstance(node, ast.Call):
        return not _is_a_call_to_a_non_enterable_builtin(node)
    if isinstance(node, ast.Starred):
        # A starred expression only appears where the argument model has
        # already expanded it; reaching one here means an unexpanded container
        # is being read, and its type is not pinned.
        return True
    return True


def _is_a_call_to_a_non_enterable_builtin(node):
    """Is this a call to a builtin whose result type has no ``__enter__``?

    ``nullcontext(enter_result=list())`` binds a list and
    ``nullcontext(enter_result=int())`` binds an ``int``; neither can be
    entered, so counting the call as a live superseder declared a dead assert
    load-bearing. The call is only asked about when the callee is a bare
    name, because that is the spelling that names a builtin -- an attribute
    (``mod.list()``) or a subscript is a value this module cannot read, and it
    answers as unreadable rather than assuming.
    """
    if not isinstance(node.func, _ENTERABLE_CLASS_NAMES):
        return False
    return node.func.id in _NON_ENTERABLE_BUILTIN_CALLS


def _null_context_enter_results(expression, bound):
    """The ``enter_result`` values this ``nullcontext`` call is given.

    A ``nullcontext`` signature is ``__init__(self, enter_result=None)``, so
    at most one argument can be effective, and it is the *first* positional
    value, or else the sole keyword named ``enter_result``. Every spelling is
    normalised to the value that would actually reach the parameter, in the
    order CPython applies them -- positional arguments first, then keywords:

    * the first positional node, which binds the parameter;
    * a ``*`` of a *literal* list/tuple/set, whose elements are the positional
      arguments -- so the *first element* is the one that binds it, and an
      empty star supplies nothing at all;
    * a ``*`` of anything else, which pins no value and so may supply any;
    * a ``**`` of a *literal dict*, whose ``"enter_result"`` key is the
      keyword, with the **last** duplicate winning as CPython's dict display
      does;
    * a ``**`` of a computed mapping, whose keys are unreadable -- it may
      carry ``enter_result`` and it may carry anything else, so its value is
      reported as unreadable rather than as absent.

    Returning the *effective* value rather than every syntactic node is what
    keeps the cardinality question separate from the type question:
    ``nullcontext(*[], enter_result=CM())`` unpacks to nothing, so its
    ``enter_result`` is the keyword, while ``nullcontext(*(), CM())`` supplies
    ``CM()`` positionally and the empty star contributes nothing.

    Only *one* value is returned, because only one can bind the parameter: the
    extra values of an over-supplied call are what make it raise, and that is
    :func:`_null_context_call_raises`'s question rather than this one's.
    """
    positional = _first_positional_value(expression)
    if positional is not None:
        return [positional]
    for keyword in expression.keywords:
        if keyword.arg == "enter_result":
            return [keyword.value]
    for keyword in expression.keywords:
        if keyword.arg is not None:
            continue
        literal = _dict_literal_value(keyword.value, "enter_result")
        if literal is not None:
            return [literal]
        # A `**` of a mapping the source does not pin may carry the key. A
        # `**` of a literal that simply has no such key cannot, so those are
        # skipped rather than reported.
        if _literal_dict_keys(keyword.value) is None:
            return [_UNREADABLE_VALUE]
    return []


def _first_positional_value(expression):
    """The value the first positional argument binds, or ``None`` if there is none.

    A starred argument contributes its elements, so an empty literal star
    supplies nothing and the argument after it becomes the first
    (``nullcontext(*(), CM())`` binds ``CM()``). An unreadable star pins
    nothing, so it is reported as the value and left for the type question.
    """
    for argument in expression.args:
        if not isinstance(argument, ast.Starred):
            return argument
        elements = _literal_star_elements(argument.value)
        if elements is None:
            return _UNREADABLE_VALUE
        if elements:
            return elements[0]
        # An empty literal star unpacks to nothing; the next argument is first.
    return None


#: Sentinel for "a value the source does not pin, so it may be anything".
_UNREADABLE_VALUE = ast.Name(id="__unreadable__", ctx=ast.Load())


def _literal_star_elements(node):
    """The elements a starred literal unpacks to, or ``None`` if unreadable.

    A dict display is included because it is a readable *iterable* here: it
    unpacks to its **keys**, and the checker does not need to guess what they
    are to know how many values reach the call. ``nullcontext(*{})`` unpacks
    to nothing, so it supplies no argument at all -- which is the same
    no-argument form as ``nullcontext()`` and binds ``None``. Treating it as
    unreadable made it supply a "maybe enterable" value and declared a dead
    header live. A dict display with *computed* keys is still unreadable, so
    only a fully literal one counts.
    """
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return list(node.elts)
    if isinstance(node, ast.Dict) and _literal_dict_keys(node) is not None:
        return [key for key in _literal_dict_keys(node) or ()]
    return None


def _dict_literal_value(node, key):
    """The value a literal dict stores under ``key``, or ``None`` if unreadable.

    Python keeps the **last** of several duplicate keys, so this walks to the
    end rather than returning the first match:

        nullcontext(**{"enter_result": CM(), "enter_result": None})

    binds ``None`` -- the second value wins, the header raises entering it --
    while the same dict with the values swapped binds ``CM()`` and the header
    is live. Reading the first key reversed both.
    """
    if not isinstance(node, ast.Dict):
        return None
    value = None
    for literal_key, literal_value in zip(node.keys, node.values):
        if isinstance(literal_key, ast.Constant) and literal_key.value == key:
            value = literal_value
    return value


def _null_context_call_raises(expression):
    """Would this ``nullcontext`` call raise before it binds anything?

    ``nullcontext.__init__(self, enter_result=None)`` is the whole signature, so
    the call raises at bind time when it supplies **more than one** effective
    argument, or when it names a parameter that does not exist. The ``with``
    then fails before it can bind ``cs``, so there is no store to exclude and
    the assert under it is unreachable.

    The count is over *effective* arguments, not syntax nodes, so the starred
    spellings are measured by what they unpack to:

    * ``nullcontext(*[CM(), CM()])`` supplies two and raises;
    * ``nullcontext(*[], enter_result=CM())`` supplies one -- the empty star
      unpacks to nothing -- and does not;
    * ``nullcontext(*(), CM())`` likewise supplies one.

    A keyword is unexpected when it is not ``enter_result``. An explicit
    ``foo=1`` is read directly; a ``**`` mapping is read for the keys it
    carries, with the last duplicate winning, so
    ``nullcontext(CM(), **{"foo": 1})`` is seen to raise the same way
    ``nullcontext(CM(), foo=1)`` does. A ``**`` of a mapping the source does
    not pin is treated as possibly carrying any key, so it may raise; the
    store then is not one that can be excluded, which is the recoverable
    direction.
    """
    if not isinstance(expression, ast.Call):
        return False
    positional = 0
    unknown = False
    for argument in expression.args:
        if isinstance(argument, ast.Starred):
            elements = _literal_star_elements(argument.value)
            if elements is None:
                # An unreadable star could hold any *number* of values, so
                # this cannot say the call raises -- and it must not say it
                # does not, either, because `nullcontext(*[CM(), CM()])`
                # really does raise while `nullcontext(*values)` with
                # `values = [CM()]` does not. Both spellings are therefore
                # left to the *value* question, which reports the unreadable
                # one as maybe-enterable. Claiming a raise here would
                # exclude a live store; this is the recoverable direction.
                unknown = True
                continue
            positional += len(elements)
        else:
            positional += 1
    if unknown:
        return False
    keywords = []
    for keyword in expression.keywords:
        if keyword.arg is not None:
            keywords.append(keyword.arg)
            continue
        state = _dict_literal_key_state(keyword.value)
        if state is None:
            # Not a dict display at all, so nothing about it is pinned.
            return _mapping_may_add_a_key(expression, positional)
        keys, has_computed, has_spread = state
        if has_computed and "enter_result" in keys:
            # A computed key is a *distinct* entry beside a readable
            # `enter_result`, and `nullcontext` takes one parameter, so the
            # call raises whatever the computed key evaluates to:
            #
            #     nullcontext(**{("enter_" + "r"): 1, "enter_result": CM()})
            #     -> TypeError: unexpected keyword argument 'enter_r'
            return True
        if has_spread:
            # A `**` spread may *overwrite* a key rather than add one, so it
            # can leave the mapping with exactly the keys already read:
            #
            #     nullcontext(**{"enter_result": CM(), **other})
            #
            # binds `CM()` when `other` carries only `enter_result`, and
            # raises when `other` adds a name. Which one it is depends on a
            # value the source does not pin, so this claims neither and the
            # value question takes it from there.
            continue
        keywords.extend(keys)
    if positional > 1:
        return True
    if keywords.count("enter_result") > 1:
        return True
    if positional and "enter_result" in keywords:
        return True
    return any(name != "enter_result" for name in keywords)


def _literal_dict_keys(node):
    """The keys a literal dict carries, last duplicate winning, else ``None``."""
    if not isinstance(node, ast.Dict):
        return None
    keys = []
    for literal_key in node.keys:
        if literal_key is None:
            # `**` inside a dict display (`{**other}`) is itself unreadable.
            return None
        if not isinstance(literal_key, ast.Constant):
            # A computed key -- `("enter_" + "result")` -- pins no name, so
            # the mapping it builds is not one the checker can read.
            return None
        if literal_key.value in keys:
            keys.remove(literal_key.value)
        keys.append(literal_key.value)
    return keys


def _dict_literal_key_state(node):
    """``(keys, has_computed_key, has_spread)`` for a dict display, else ``None``.

    The three answers are needed apart because they are not the same claim. A
    readable key names a parameter. A **computed** key (``("enter_" + "r")``)
    names one the source does not pin, and it is always a *separate* entry --
    it cannot overwrite its neighbours, because a dict display evaluates keys
    left to right and a computed key lands at its own position. A **spread**
    (``{**other}``) is the opposite: it can overwrite whatever came before it,
    so it cannot be counted as an extra key at all.

    Returning ``None`` for a non-display means "this is not a mapping the
    source shows", which the caller treats differently from "a mapping with
    computed keys" -- the first is a ``Name`` like ``**values`` and pins
    nothing; the second pins a readable ``enter_result`` *and* hides a key.
    """
    if not isinstance(node, ast.Dict):
        return None
    keys = []
    has_computed = False
    has_spread = False
    for literal_key in node.keys:
        if literal_key is None:
            has_spread = True
            continue
        if not isinstance(literal_key, ast.Constant):
            has_computed = True
            continue
        if literal_key.value in keys:
            keys.remove(literal_key.value)
        keys.append(literal_key.value)
    return keys, has_computed, has_spread


def _mapping_may_add_a_key(expression, positional):
    """Could this unreadable ``**`` mapping add a key to the call?

    ``nullcontext`` takes exactly one parameter, so *any* key beyond the one
    it already supplies is unexpected and raises. The mapping itself may
    carry nothing, though, so this answers only where the source has already
    pinned a key elsewhere:

    * ``nullcontext(CM(), **values)`` has a positional already, so a key in
      ``values`` would be a second value for the same parameter.
    * ``nullcontext(enter_result=CM(), **values)`` is the same in the keyword
      position.
    * ``nullcontext(**values)`` on its own pins nothing: the call may raise or
      not, so it is left to the value question.

    The *empty* mapping is the boundary and it is measured, not assumed:
    ``nullcontext(CM(), **{})`` and ``nullcontext(enter_result=CM(), **{})``
    both build a manager without raising, because an empty mapping adds
    nothing. The name is unreadable, so this cannot tell it from the populated
    case, and it does not try -- the two disagree about whether the *call*
    raises while neither can be decided from the source, so both are left to
    the value question.
    """
    if positional:
        return True
    return any(keyword.arg == "enter_result" for keyword in expression.keywords)


def _stores_of(name, by_index, index, function):
    """The ``(statement, value, conditional)`` stores binding ``name`` here.

    Only the index that *is* the queried ``with`` is consulted, so a later
    store cannot be read backwards into an earlier header. Returns ``None``
    when the name is not bound at this point at all, which is a `NameError` on
    entry and a different defect (#334's family), not this one.
    """
    orders = by_index["orders"]
    entries = [
        entry for entry in by_index["bindings"].get(name, ()) if orders[id(entry[0])] <= index
    ]
    if not entries:
        return None
    # Only an unconditional store settles the value. A store nested in a block
    # may not have run, so a literal type read off one of those would claim a
    # certainty the source does not have. An `except ... as cs:` handler is
    # marked conditional for exactly that reason, yet it is still decidable
    # here: whether the handler runs or not, the name it binds cannot hold a
    # usable context manager afterwards -- if the handler ran, CPython deleted
    # the name when the handler exited, and if it did not run, the previous
    # binding is still in force and is settled by an unconditional store.
    # A store inside an always-true branch joins them from the other side:
    # `if True:` runs its body on every import, so the store settles the name
    # exactly as an unconditional one does. Left out, it settled nothing and a
    # later conditional store was read as the one in force -- which reported
    # the `TypeError` CPython raises for
    #
    #     if True:
    #         cs = list()
    #         def list(): return nullcontext()
    #
    # as a live header. Only a branch that is true for every binding counts, so
    # a condition this cannot read keeps its existing treatment.
    decidable = [
        entry
        for entry in entries
        if not entry[2]
        or isinstance(entry[0], ast.ExceptHandler)
        or _store_is_settled_before(entry, orders, index, function)
        or _statement_always_runs(entry[0], function)
    ]
    latest = max((orders[id(entry[0])] for entry in decidable), default=None)
    if latest is None:
        return None
    # #375. A conditional store that may *supersede* a carrier leaves the
    # name's final value undecidable, so the carrier must not be read as
    # still in force. This is checked BEFORE the #367 tie-break below,
    # because the tie-break's premise -- that `latest` identifies the value
    # actually in force -- is exactly what a superseding conditional store
    # denies. Declining here is the safe direction: `_entry_is_dead` then
    # treats the header as live rather than certifying a reachable assert as
    # swallowed.
    #
    #     def outer(flag, x):
    #         import os as cs
    #         if flag:
    #             cs = nullcontext()
    #         with cs:
    #             assert x != 1
    #
    # When the branch runs the name holds a real context manager and the
    # assert is **live**; when it does not, the module is in force and entry
    # raises `TypeError`. Either way the carrier alone does not settle the
    # header, and answering "dead entry" from it reports a live contract as
    # unreachable.
    #
    # Three restrictions keep the guard from over-claiming, each pinned by a
    # row that master already gets right:
    #
    # * It fires only when the settled store is itself a carrier. An
    #   unconditional store after the carrier has already settled the name and
    #   a later conditional store cannot unsettle it.
    # * A later conditional store that is itself pinned non-enterable, or is
    #   itself a carrier, cannot rescue the header on the path where it runs,
    #   so the carrier still decides every path:
    #
    #       def outer(flag, x):
    #           import os as cs
    #           if flag:
    #               cs = None
    #           with cs:
    #               assert x != 1
    #
    #   CPython raises `TypeError` for `flag=False` (the module) *and* for
    #   `flag=True` (`None`), so the assert is unreachable on both paths.
    # * `except ... as cs:` is excluded because the handler *deletes* the name
    #   on exit rather than superseding it, and the list above already keeps
    #   that shape decidable. Excluding it here stops a try/except that merely
    #   mentions the name from flipping a correct `defeated` to `enforced`.
    tied = [entry for entry in decidable if orders[id(entry[0])] == latest]
    # The guard fires when the *settled* store is a value the header could not
    # otherwise have entered -- a carrier, a pinned unenterable literal, `None`,
    # and so on -- and a *later conditional* store might make the header
    # enterable by superseding it with a real context manager. It must not be
    # restricted to carriers: with
    #
    #     def outer(flag, x):
    #         import os as cs
    #         cs = None
    #         if flag:
    #             cs = nullcontext()
    #
    # the settled value is `None` (not a carrier) yet a later conditional store
    # *does* make the header live for `flag=True`. Restricting the guard to
    # carriers read the stale `None` and reported that live header dead. The
    # "later store must itself be enterable" restriction below still keeps the
    # `cs = None; if False: cs = nullcontext()` family correctly dead, because
    # a conditional store that is itself pinned unenterable cannot rescue the
    # header on the path where it runs.
    settled_unenterable = any(
        orders[id(entry[0])] == latest and not _store_may_bind_enterable(entry, name, function)
        for entry in decidable
    )
    if settled_unenterable and any(
        entry[2]
        and not isinstance(entry[0], ast.ExceptHandler)
        and orders[id(entry[0])] > latest
        and not _statement_never_runs(entry[0], function)
        # A store inside a nested `def`/`lambda`/`class` body binds *that*
        # scope's local, so it cannot supersede this scope's carrier. Without
        # this the row below declined on a store the name cannot hold, which
        # reports `enforced` -- turning a genuinely unreachable assert into a
        # purportedly load-bearing one, the exact error the decline exists to
        # prevent. `global`/`nonlocal` are handled inside the helper: they
        # name the scope that actually owns the settled value, so a
        # `nonlocal` store here *does* supersede and a `global` one does not.
        and _store_is_in_scope(entry[0], function)
        and _store_may_bind_enterable(entry, name, function)
        for entry in entries
    ):
        return None
    if len(tied) == 1:
        return tied
    # #367. Two stores share the top-level statement, so `orders` cannot
    # separate them. Among the tied, the one this rule newly deemed *settled*
    # is the later write in the block: it is written into a `with` body, and
    # by the time a *subsequent* top-level statement is reached it has run, so
    # it is the value in force. Returning it alone keeps the dead-entry rule
    # from reading the earlier carried suppressor as if it were still in play.
    settled = [entry for entry in tied if _store_is_settled_before(entry, orders, index, function)]
    return settled or tied


def _store_is_settled_before(entry, orders, index, function):
    """Has this conditional store run by the time top-level statement ``index``?

    #367. A store recorded as ``conditional`` is one nested *somewhere* inside
    a top-level statement, but "nested" and "may not have run" are not the same
    question. The two shapes this rule separates are the ones the anchor test
    pins side by side:

        with (cs := contextlib.suppress(AssertionError)):
            import os as cs          # <-- nested, yet certainly run
        with cs:                      # a LATER top-level statement
            assert x != 1

    The ``import`` sits in the *body* of a ``with``. A ``with`` body is not a
    branch: it either runs to completion -- and then the store has happened --
    or it propagates an exception, in which case the later top-level ``with``
    is never reached at all. So by the time statement ``index`` is executing,
    a store in an *earlier* top-level statement's body has run. `conditional`
    is still the right flag for the *binding resolver*, which has to reason
    about a ``with`` header that is entered *before* the body store happens
    (that is the #323 supersession the flag encodes). It is the wrong flag for
    the dead-entry rule, which is only ever asked about a header that comes
    strictly after the statement containing the store.

    That strictness is what keeps this from over-claiming. The store's order
    must be *less than* the queried index: an order equal to ``index`` means
    the store is nested in the very statement whose header is being read, and
    that header is evaluated *before* the body runs:

        with cs = nullcontext():
            ...

    is not a shape, but ``with (cs := nullcontext()):`` is -- and there the
    store has not happened when ``cs`` is read. The `strictly earlier` test
    declines it, which is the safe direction.

    Branching containers are excluded outright, because a store inside one of
    their *bodies* really can be skipped:

        with helper.manage():
            if flag:
                cs = nullcontext()
        with cs:                      # `cs` may not be bound at all
            assert x != 1

    The walk therefore requires the store to be nested under a ``with``/
    ``async with`` *body* and not under any ``if``/loop/``try``/``with`` *body*
    that could skip it. ``try`` is included because its ``body`` runs once but
    an exception can still divert control; keeping it out means a store
    directly in a ``try`` body is left to the ordinary ``conditional`` answer,
    which is the conservative one.
    """
    statement = entry[0]
    if orders[id(statement)] >= index:
        # Same top-level statement as the queried header, or later: the header
        # is read before that body runs, so nothing is settled.
        return False
    order = orders[id(statement)]
    if 0 <= order < len(function.body):
        return _runs_before_end_of(function.body[order], statement)
    return False


def _runs_before_end_of(top, statement):
    """Does ``statement`` run on every path that completes ``top``?

    ``top`` is the top-level statement the store is nested in, and ``statement``
    is the store. A store written directly in ``top``'s body runs whenever
    ``top`` completes. A store nested inside a *branch* of ``top`` may be
    skipped, so it does not. The only container whose body is guaranteed to run
    is a ``with``/``async with``: its body is not conditional, so anything
    written directly in it has run by the time ``top`` returns.

    Every hop on the way down has to be guaranteed, not just the innermost one.
    A store under an ``if``/loop/``try`` *inside* a ``with`` body may still be
    skipped, even though the ``with`` body itself always runs:

        with contextlib.nullcontext():
            if flag:
                with contextlib.nullcontext():
                    cs = contextlib.nullcontext()

    With ``flag`` false the rebind never happens, the name still holds the
    ``suppress`` an earlier statement bound, and the ``with cs:`` below it
    swallows the assert. Judging only the innermost hop called that store
    settled, retired the ``suppress``, and reported the swallowed assert
    ENFORCED -- the damaging direction (#308 criterion 1). So the walk carries
    whether *every* edge crossed so far was guaranteed, and no descendant of an
    unguaranteed edge can be settled.
    """
    if statement is top:
        return False
    current = [(top, True)]
    while current:
        nxt = []
        for node, guaranteed_so_far in current:
            for field in ("body", "orelse", "finalbody", "handlers", "items"):
                children = getattr(node, field, None) or []
                if isinstance(children, ast.AST):
                    children = [children]
                edge = _edge_is_guaranteed(node, field)
                for child in children:
                    if child is statement:
                        # The store is a child of `node` through `field`, so
                        # this last hop is judged like every other one.
                        return guaranteed_so_far and edge
                    nxt.append((child, guaranteed_so_far and edge))
        current = nxt
    return False


def _edge_is_guaranteed(node, field):
    """Does ``node``'s ``field`` child always run when ``node`` runs?"""
    if field == "orelse" and isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return _loop_else_always_runs(node)
    # `orelse`, `finalbody`, `handlers` and `items` are branches or
    # alternatives: a `with`'s own `orelse`, a `try`'s handler, and a `with`
    # item's `vars` all may not run. Only a `with` body is unconditional.
    return field == "body" and isinstance(node, (ast.With, ast.AsyncWith))


def _loop_else_always_runs(loop):
    """Can control leave ``loop``'s body without running its ``else``?

    #395. A ``for``/``while`` ``else`` runs when the loop finishes without a
    ``break``, so the store written there settles the name *iff* no path can
    break out. ``return``/``raise``/``continue`` are not disqualifying: they
    leave the whole enclosing statement rather than falling through to the
    ``else``, and a store that never ran cannot have settled a name that a
    later statement reads.

    Only a ``break`` targeting this loop suppresses the ``else``. A ``break``
    in a *nested* loop's body belongs to that inner loop and does not count,
    but a ``break`` in a nested loop's ``else`` is a plain block statement: it
    binds to this loop and does suppress this ``else``. See
    ``_breaks_own_loop``.
    """
    return not any(_breaks_own_loop(child) for child in loop.body)


def _breaks_own_loop(node):
    """Is there a ``break`` in ``node``'s subtree that exits the enclosing loop?"""
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        # A nested loop has its own `break` target, so a `break` in its *body*
        # exits the nested loop, not the one we are asking about. Its `else` is
        # a different story: a loop `else` is a plain block, not a loop, so a
        # `break` written there binds to the *enclosing* loop and really does
        # suppress the enclosing `else`:
        #
        #     for i in range(n):        # the loop we are asking about
        #         first = contextlib.suppress(AssertionError)
        #         for j in range(1):
        #             pass
        #         else:
        #             break            # exits the OUTER loop
        #     else:
        #         first = contextlib.nullcontext()
        #
        # With `n == 1` that `break` skips the outer `else`, so `first` keeps
        # the suppressor and the assert is swallowed. Returning a flat `False`
        # here called the outer `else` guaranteed and reported the swallowed
        # assert ENFORCED -- head-worse-than-base (#308 criterion 1).
        return any(_breaks_own_loop(child) for child in node.orelse)
    if isinstance(node, ast.Break):
        return True
    for child in ast.iter_child_nodes(node):
        if _breaks_own_loop(child):
            return True
    return False


def _match_capture_pins_value(statement, name):
    """The literal a ``match`` capture binds, when the subject fixes it.

    #367. `_entry_is_dead` declines every `ast.Match` store, because a capture
    binds whatever was *matched* and that is very often a real context manager.
    The exclusion is right in general and wrong for the one shape the anchor
    test pins: when the ``match`` subject is a literal and the capture takes
    one of its elements, the value is fixed by the source, exactly as `import
    os as cs` is fixed by its syntax.

        match [1]:
            case [cs]:        # `cs` is `1`, an int
                pass

    Only a sequence or mapping subject is considered, only a capture that
    binds a *whole element* of it (a ``MatchAs`` with no sub-pattern, or a
    ``MatchStar``), and only when the element the capture receives is itself a
    literal this module can type. Anything else -- a class pattern, a nested
    sequence, a starred tail, a computed subject -- returns ``None`` and the
    capture keeps its exclusion, so a real context manager bound by a capture
    is never called dead.
    """
    subject = statement.subject
    if isinstance(subject, (ast.List, ast.Tuple)):
        elements = list(subject.elts)
    elif isinstance(subject, ast.Dict):
        return None
    else:
        return None
    for case in statement.cases:
        pattern = case.pattern
        # A single whole-subject sequence pattern maps element-wise onto the
        # subject, so the capture's index in the pattern fixes its element.
        if not isinstance(pattern, (ast.MatchSequence, ast.MatchStar)):
            continue
        for index, element in enumerate(elements):
            kind = _capture_element_kind(pattern, index, name)
            if kind is not None:
                return _literal_runtime_type(element)
    return None


def _capture_element_kind(pattern, index, name):
    """Does the sequence ``pattern`` bind ``name`` to its ``index``-th element?"""
    if isinstance(pattern, ast.MatchSequence):
        patterns = list(pattern.patterns)
        if index >= len(patterns):
            return None
        return name if _pattern_binds_element(patterns[index], name) else None
    if isinstance(pattern, ast.MatchStar):
        # A bare `[*cs]` collects the *remaining* elements as a list, which is
        # a real list, not an element of the subject.
        return "star" if isinstance(pattern.name, str) and pattern.name == name else None
    return None


def _pattern_binds_element(pattern, name):
    """Does ``pattern`` bind ``name`` to the whole element it matches?"""
    if isinstance(pattern, ast.MatchAs) and pattern.pattern is None:
        return pattern.name == name
    if isinstance(pattern, ast.MatchStar):
        return pattern.name == name
    return False


def _entered_name_is_dead(header, function, bound, module=None):
    """Does this ``with`` enter a name whose value cannot be a context manager?

    Locates the ``with`` among the function's top-level statements, rebuilds the
    per-index store map exactly as :func:`_aliased_suppressions` does, and asks
    :func:`_entry_is_dead` about each bare-``Name`` item in the header. The map
    is rebuilt per call rather than cached because it is cheap next to the AST
    walk that produced it, and a cache here would have to be keyed on the
    function object as well as the index.

    An ``async with`` is excluded for the same reason
    :func:`_is_suppressing_with` excludes it: the async protocol is a different
    question, and every async entry already raises on a sync-shaped value, so
    the rule would fire on shapes it cannot reason about.
    """
    if isinstance(header, ast.AsyncWith):
        return False
    # `_assigned_suppressors` resolves each name to a *suppressor* or `None`,
    # which is exactly the information this rule needs discarded: the value
    # `None` here means "not a known suppressor", not "the value is None". So
    # the raw per-name store lists and their orderings are rebuilt instead.
    bindings, _raw_values = _store_bindings(function, bound)
    orders = {
        id(statement): _binding_order(function, statement)
        for entries in bindings.values()
        for statement, _, _ in entries
    }
    by_index = {"bindings": bindings, "orders": orders}
    for index, statement in enumerate(function.body):
        for candidate in ast.walk(statement):
            if candidate is not header:
                continue
            return any(
                _entry_is_dead(item.context_expr, by_index, index, function, bound, module)
                for item in header.items
            )
    return False


def _is_suppressing_with(node, bound, function=None):
    """Is this ``with`` a suppression context that can eat an assertion failure?"""
    if isinstance(node, ast.AsyncWith):
        # `async with` demands an *asynchronous* context manager. Neither
        # `contextlib.suppress` nor `pytest.raises` provides one -- each returns
        # `None` from `__enter__` and has no `__aenter__` at all -- so
        # `async with suppress(AssertionError):` raises `TypeError: 'suppress'
        # object does not support the asynchronous context manager protocol`
        # while entering. The body never runs and the test fails loudly, so this
        # is a live contract, not a defeat. Measured on the pinned interpreter.
        # Reading it as a suppression would drop a real assert from the
        # sentinel's view, so the async form is left alone entirely.
        return False
    for item in node.items:
        call = item.context_expr
        if not isinstance(call, ast.Call):
            continue
        dunder = _suppressed_by_dunder(call, bound)
        if dunder and any(_name_catches_assertion_error(name) for name in dunder):
            return True
        if not _is_suppression_call(call, bound):
            continue
        if any(_name_catches_assertion_error(name) for name in _suppression_names(call)):
            return True
    for argument in _entered_suppressions(node, bound):
        if not isinstance(argument, ast.Call):
            continue
        if not _is_suppression_call(argument, bound):
            continue
        if any(_name_catches_assertion_error(name) for name in _suppression_names(argument)):
            return True
    if function is None:
        return False
    for argument in _aliased_suppressions(node, function, bound):
        # An ambiguous binding may be *any* suppressor, so the rule cannot claim
        # the exception is harmless and reports the assert as defeated (#308).
        if argument is AMBIGUOUS_SUPPRESSOR:
            return True
        if argument is LOUD_DUNDER:
            # `cs.__enter__()` raises before the body whatever `cs` holds, so
            # the argument is not what decides this and must not be read.
            return True
        # #367: no `_is_suppression_call` gate is needed here, and adding one
        # would be a re-test of a property the producer already guarantees.
        # Every value `_aliased_suppressions` appends is either one of the two
        # markers handled above or a value that already passed
        # `_is_readable_suppressor` at its source:
        #
        # * the walrus branch appends `resolved` only under
        #   `if _is_readable_suppressor(resolved, bound)`, and that predicate
        #   accepts only an `ast.Call` that `_is_suppression_call` accepts;
        # * `live[expression.id]` comes from `_assigned_suppressors`, which
        #   records a name only when `_resolve_bindings` returned a non-`None`
        #   value -- and every non-`None` branch of `_resolve_bindings` returns
        #   either `AMBIGUOUS_SUPPRESSOR` or `_is_readable_suppressor`'s result.
        #
        # An earlier cut of this repair gated the loop on `_is_suppression_call`
        # to stop a bare `nullcontext()` from reading as `["BaseException"]`
        # and eating the verdict. That was treating the symptom at the
        # consumer: the real source of the bad value was `_resolve_bindings`
        # returning an unreadable right-hand side, which `_readable_store_value`
        # now declines at the producer. A mutation removing such a gate is
        # therefore not a surviving defect, and shipping an unpinned
        # re-test would be exactly the kind of change this lane rejects.
        # #390 tracks the related own-body `lambda` case.
        if any(_name_catches_assertion_error(name) for name in _suppression_names(argument)):
            return True
    return False


def _ancestors(function, target):
    """The chain of nodes from ``function`` down to ``target``, outermost first."""
    chain = []
    current = function
    while True:
        for child in ast.iter_child_nodes(current):
            if child is target:
                return [*chain, child]
            if any(node is target for node in ast.walk(child)):
                chain.append(child)
                current = child
                break
        else:
            return chain


def _in_body(branch, target):
    """Is ``target`` inside ``branch``'s executed body, not a handler or ``else``?"""
    return any(_contains(statement, target) for statement in branch.body)


def _falsy_literal(node, function=None):
    """A condition that is a literal false, so its body can never run."""
    if isinstance(node, ast.Constant) and not node.value:
        return True
    # An empty literal container is false for the same reason `False` is, and
    # `if flag and ():` never enters its body. CPython evaluates the tuple's
    # truth value at runtime; the empty form is decidable from the syntax, so
    # it belongs to the same rule rather than to the loop-emptiness check,
    # which is about a *container of values* rather than a condition.
    #
    # `ast.Dict` has to be asked about its own emptiness, not `.elts`: a dict
    # node carries `keys`/`values`, so reading `.elts` on it raised
    # `AttributeError` inside the decision function and crashed the analyzer on
    # `if {}: cs = nullcontext()`. A decision function must answer for every
    # node the caller can hand it, not raise.
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    # A zero-argument call that builds an *empty* container is falsy for the
    # same runtime reason `()` is: `if set():` never enters its body, exactly
    # like `if ():`.
    #
    # **Only** the no-argument form is decided. An argument-bearing call of the
    # same constructor is truthy whenever the argument produces an element:
    # `if set([1]):`, `if list((1,)):` and `if bytearray(b"x"):` all enter
    # their bodies in CPython, and `if dict(a=1):` does too. Reading them as
    # empty reported a live header dead -- the damaging direction -- because
    # :func:`_builtin_constructor_kind` answers the *type* a call produces and
    # the emptiness question was asked of that type rather than of the
    # argument list. The check belongs here, next to the container literals it
    # sits beside, and it is a property of the call's own argument list.
    return _is_zero_argument_empty_container(node, function)


def _is_zero_argument_empty_container(node, function=None):
    """Is ``node`` a ``set()``-shaped call that provably builds an empty one?

    Two properties have to hold together, and both were needed for the answer
    to be right:

    * the call must carry **no arguments**. `set([1])`, `list((1,))`,
      `dict(a=1)` and `bytearray(b"x")` are all non-empty, and reading them as
      empty reported a live header dead -- in a *condition* and in a *loop
      iterable* alike, because both call sites asked the type question through
      :func:`_builtin_constructor_kind` and never looked at the argument list;
    * the callee must be the real **builtin**, which
      :func:`_callee_is_shadowed` decides from the enclosing scope. A local
      ``def set(): return nullcontext()`` makes the call a different one, and
      a false "empty" there retires a header CPython enters.

    Both spellings of "empty builtin container" are decided from this one
    function, so the condition form and the loop form cannot disagree -- which
    is the property :data:`_EMPTY_CONSTRUCTOR_TYPES` was introduced to
    guarantee and an argument-blind helper would have broken.
    """
    if not isinstance(node, ast.Call) or node.args or node.keywords:
        return False
    return _builtin_constructor_kind(node, function) in _EMPTY_CONSTRUCTOR_TYPES


def _is_empty_literal_iterable(node):
    """Is this iterable a literal container that provably yields nothing?

    ``for _ in []:`` keeps the assert in the AST and never runs it, which is the
    loop spelling of the ``if False:`` defeat already closed above. The literal
    must be *readable* rather than merely a literal: ``[]``, ``()``, ``{}``,
    ``(0,)`` and ``(False, True)`` all have a decidable value, and the
    emptiness test is then exact.

    A call such as ``range(0)`` or ``dict()`` is deliberately *not* matched
    even though it too yields nothing. Deciding those means reasoning about
    builtins rather than reading a literal, and a wrong answer there drops a
    live contract from the sentinel's view -- the more damaging error. The rule
    answers only the question a literal settles on its own.
    """
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return False
    return not _literal_value(node)


def _is_uncalled_nested_def(function, node):
    """Is this nested ``def`` referenced nowhere in the owning function?

    A nested definition is only a defeat if nothing can reach it. The common
    pattern is a callback handed to the code under test, which *is* called even
    though nothing in the function body calls it, so the rule treats a nested
    def as live whenever its name is loaded anywhere in the enclosing function.

    The asymmetry is deliberate. A missed defeat leaves one pinned assert
    defensible-but-weak; a wrong "defeated" verdict *removes* a live contract
    from the sentinel's view, which is the more damaging error and the one
    #280/#287 exist to prevent.
    """
    name = node.name
    for candidate in ast.walk(function):
        if candidate is node:
            continue
        if (
            isinstance(candidate, ast.Name)
            and candidate.id == name
            and isinstance(candidate.ctx, ast.Load)
        ):
            return False
    return True


#: Operand kinds whose truthiness can decide an ``or`` on its own. A nested
#: ``Compare`` is deliberately absent: it is not a decision on its own, and
#: treating it as one would flag a legitimate ``and`` of two comparisons.
#:
#: This applies to ``or`` only. In ``A or B`` a truthy ``A`` means ``B`` is
#: never evaluated, so the comparison in ``B`` goes unchecked. In ``A and B``
#: the opposite holds: ``B`` is skipped when ``A`` is *falsy*, and a truthy
#: runtime value in ``A`` -- a ``Name``, ``Call`` or ``Subscript`` -- carries no
#: information about whether ``B`` runs. Applying this tuple to ``and`` too
#: reported the live contract
#: ``assert len(errors) == 1 and isinstance(errors[0], RuntimeError)`` as
#: bypassed, silently dropping it from the pinned count set.
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


#: Distinct from ``None``, which is itself a literal and so cannot double as a
#: bail-out signal.
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


def _literal_value(node):
    """The value of a literal-only expression, or ``_NOT_LITERAL``.

    ``ast.literal_eval`` is the obvious tool here and is the wrong one: it
    refuses every ``Compare`` node, so it cannot answer the question this
    exists to answer. ``1 == 1`` raises ``ValueError`` there, yet it is exactly
    the decisive case -- decidable to true without reading any state, so it
    short-circuits the ``or`` precisely as a bare ``True`` does.

    The fold is therefore done structurally, over the literal expression
    grammar: constants, containers of constants, unary and binary operators,
    and chained comparisons between them. Anything that could read runtime
    state -- ``Name``, ``Call``, ``Attribute``, ``Subscript`` -- makes the walk
    bail and return ``_NOT_LITERAL``. That boundary is what keeps ``x == x``
    enforced: it reads a ``Name``, so it is not decidable, and reporting a live
    assert as dead is the worse error.

    A ``None`` fallback would be wrong here, because ``None`` is itself a
    literal (it parses to a ``Constant``) and so would be indistinguishable
    from a genuine ``None``.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items = [_literal_value(element) for element in node.elts]
        if any(item is _NOT_LITERAL for item in items):
            return _NOT_LITERAL
        try:
            if isinstance(node, ast.Tuple):
                return tuple(items)
            if isinstance(node, ast.List):
                return items
            return set(items)
        except TypeError:
            return _NOT_LITERAL
    if isinstance(node, ast.Dict):
        keys = [_literal_value(key) for key in node.keys]
        values = [_literal_value(value) for value in node.values]
        if any(item is _NOT_LITERAL for item in keys + values):
            return _NOT_LITERAL
        try:
            return dict(zip(keys, values))
        except TypeError:
            return _NOT_LITERAL
    if isinstance(node, ast.UnaryOp):
        operand = _literal_value(node.operand)
        if operand is _NOT_LITERAL:
            return _NOT_LITERAL
        try:
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return +operand
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.Invert):
                return ~operand
        except TypeError:
            return _NOT_LITERAL
        return _NOT_LITERAL
    if isinstance(node, ast.BinOp) and type(node.op) in _LITERAL_OPERATORS:
        left = _literal_value(node.left)
        right = _literal_value(node.right)
        if left is _NOT_LITERAL or right is _NOT_LITERAL:
            return _NOT_LITERAL
        try:
            return _LITERAL_OPERATORS[type(node.op)](left, right)
        except (ArithmeticError, TypeError):
            return _NOT_LITERAL
    if isinstance(node, ast.BoolOp):
        result = isinstance(node.op, ast.And)
        for value in node.values:
            item = _literal_value(value)
            if item is _NOT_LITERAL:
                return _NOT_LITERAL
            if isinstance(node.op, ast.And):
                result = result and bool(item)
            else:
                result = result or bool(item)
        return result
    if isinstance(node, ast.Compare):
        left = _literal_value(node.left)
        if left is _NOT_LITERAL:
            return _NOT_LITERAL
        for operator, comparator in zip(node.ops, node.comparators):
            right = _literal_value(comparator)
            if right is _NOT_LITERAL:
                return _NOT_LITERAL
            handler = _LITERAL_COMPARISONS.get(type(operator))
            if handler is None:
                return _NOT_LITERAL
            try:
                matched = handler(left, right)
            except TypeError:
                return _NOT_LITERAL
            if not matched:
                return False
            left = right
        return True
    return _NOT_LITERAL


def _as_number(value):
    """This value if it is a real number, else ``None``.

    ``bool`` is excluded even though it subclasses ``int``: ``len(y) >= True``
    is a real comparison, not a tautology about a non-negative count.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _numeric_literal(node):
    """The value of a numeric literal node, or ``None``.

    ``-1`` parses as a ``UnaryOp(USub, Constant)`` rather than a negative
    ``Constant``, so a negative bound has to be folded here or every
    lower-bounded form would be missed.
    """
    if isinstance(node, ast.Constant):
        return _as_number(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.USub, ast.UAdd))
        and isinstance(node.operand, ast.Constant)
    ):
        value = _as_number(node.operand.value)
        if value is None:
            return None
        return -value if isinstance(node.op, ast.USub) else value
    return None


def _is_count_like(node):
    """Is this expression a count or length, so it cannot be negative?

    ``len(...)``, ``count(...)`` and ``bit_count(...)`` are the forms that
    appear in these tests. Anything else -- a bare name, an arithmetic
    expression, a record subscript -- is not assumed non-negative, because
    assuming it would turn a real comparison into a false tautology.
    """
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in {"len", "count", "bit_count"}
    if isinstance(node.func, ast.Attribute):
        return node.func.attr in {"len", "count", "bit_count"}
    return False


def _is_tautology(node):
    """Is this expression true regardless of the values it reads?

    Only the decidable forms are recognised: a truthy constant, a comparison
    built purely from literals, and a count compared against a bound a length
    can never cross. A comparison that reads any runtime value
    (``len(calls) == 300``, ``record["termination"] != "cancelled_or_deadline"``,
    ``x == x``) is a real check and is never treated as a tautology.

    A tautology is what makes an ``or`` bypass its sibling: the comparison the
    contract depends on is never evaluated because the other operand is true
    whatever the record says. Verified to survive on ``aef56d6``:

        assert record["termination"] != "cancelled_or_deadline" or \
            len(record.get("errors", [])) >= 0
        -> guard RETURNS on the deadline record; suite exits 0

    The left operand must look count-like before a numeric bound is believed:
    ``x > -1`` is only a tautology when ``x`` cannot be negative, and assuming
    that would report a live assert as dead -- the rule would then cry wolf on
    a real regression.

    The literal-only fold covers the spellings the count heuristic cannot see.
    ``1 == 1`` is true without reading any state, so it short-circuits the ``or``
    exactly as a bare ``True`` does -- but its left operand is a constant, not a
    call, so ``_is_count_like`` rejects it and the operand was previously
    misreported as enforced. Restricting the fold to literals keeps the
    conservative direction: ``x == x`` reads a ``Name``, is not constant, and
    therefore stays enforced.

    Deliberately one-directional. Proving an expression is always *false* is
    not attempted: it needs real evaluation, and a wrong answer there reports a
    live assert as dead. For the same reason ``len(y) < 1`` and ``len(y) <= 0``
    are not tautologies -- they hold only for an empty container.
    """
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if _is_literal_true(node):
        return True
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
        return False
    bound = _numeric_literal(node.comparators[0])
    if bound is None or not _is_count_like(node.left):
        return False
    # A count is >= 0 by construction, so only the lower-bounded forms can be
    # tautologies.
    operator = node.ops[0]
    if isinstance(operator, ast.GtE):
        return bound == 0
    return isinstance(operator, ast.Gt) and bound < 0


def _is_literal_true(node):
    """Is this expression decidable to ``True`` without reading any value?

    An operand built only from literals cannot be influenced by the record, so
    a truthy result means the surrounding ``or`` decides the assert on its own
    and the comparison beside it is never evaluated.

    Only ``True`` counts. A literal that folds to ``False`` -- ``1 == 2`` --
    cannot short-circuit anything, so it is not a bypass.
    """
    value = _literal_value(node)
    return value is not _NOT_LITERAL and bool(value)


def _may_bypass(expression):
    """Can a comparison nested in this expression still go unchecked?

    ``assert x != y or True`` and ``assert True or x != y`` both parse to a
    ``BoolOp``, and both leave the comparison unchecked: the ``or`` decides the
    assert on its own when the other operand is truthy. The comparison node is
    still present, so a presence-only check -- and ``_is_enforced``, which only
    inspects ``try`` -- reports the contract as intact.

    A tautological operand decides it just as effectively as a bare ``True``,
    which is why the spelling of the bypass does not matter. Recursion covers
    every operand shape for the same reason: an earlier version inspected only
    ``Name/Attribute/Call/Subscript/Constant`` and missed a ``Compare`` operand
    that was trivially true.

    The two operators are handled separately, because they bypass in opposite
    conditions. Under ``or`` any deciding operand skips its sibling when
    truthy, so every operand is a candidate. Under ``and`` a sibling is skipped
    only when the other side is *falsy*, which no decidable-true operand can
    establish, so a runtime value is not a bypass there. Only a tautology
    remains a bypass under ``and``, and only in the operand positions that
    decide the result: a leading tautology short-circuits to true and the rest
    never runs.

    A bare ``Compare`` operand does not count as a decision by itself, so
    ``assert x != 1 and y != 2`` -- the shape the real retention sites use --
    stays enforced. That distinction is pinned by table rows, because an
    over-broad rule here would report a live assert as dead.
    """
    if not isinstance(expression, ast.BoolOp):
        return False
    operands = expression.values
    tautologies = [_is_tautology(value) for value in operands]
    if any(tautologies):
        if isinstance(expression.op, ast.Or):
            return True
        # Under `and`, a tautology only decides the result when it is the
        # first operand: `True and <comparison>` never evaluates the
        # comparison. In any later position it is only decisive when every
        # operand before it is itself truthy -- `a and True and <comparison>`
        # still short-circuits to true without reaching the comparison, while
        # `<comparison> and True` evaluates the comparison first and so is not
        # a bypass. An earlier operand that could be falsy leaves the
        # comparison reachable, so the tautology is not the deciding one.
        return any(
            tautology and all(_is_literal_true(operand) for operand in operands[:position])
            for position, tautology in enumerate(tautologies)
        )
    if isinstance(expression.op, ast.And):
        return False
    if any(isinstance(value, _DECIDING_OPERANDS) for value in operands):
        return True
    return any(_may_bypass(value) for value in operands)


def _is_enforced(function, target, tree=None):
    """Is ``target`` an assert that can actually fail?

    An ``assert`` is defeated, without being removed, if it sits inside a
    ``try`` whose handler swallows ``AssertionError`` (#280). ``ast.walk`` is
    scope-blind -- it descends into ``try`` bodies -- so presence-based checks
    report such an assert as intact while the owning test can no longer fail.
    Both ``ast.Try`` and ``ast.TryStar`` (``except*``) are matched: they are
    distinct node types, and matching only the former left ``except*`` an
    unguarded spelling of the same defeat.

    Scoped to a ``try`` whose body actually contains the assert, not to any
    handler in the function: a swallowing ``try`` around a *sibling* statement
    does not disarm an assert outside it, and treating it as though it did
    would drop real pinned sites. ``_contains`` is what draws that line.

    The remaining defeats are all reachability, and all are decided here rather
    than behaviourally (#287). That is a measured choice, not a preference: an
    assert moved into an uncalled nested ``def`` or hidden in an ``if False:``
    branch leaves the *owning test green* -- the assert is never evaluated, so
    nothing about running the test can observe it. #291's behavioural guard
    closes the dead-code arm for the #261 guard only because that guard is
    *called*; the retention counts are not called from anywhere, so for them
    the structural walk is the only place the defeat is visible.

    Suppression is matched by *resolved* name, so the qualified, from-import
    and both alias spellings are covered alongside ``contextlib.suppress``. The
    three ways a suppressor gets past that rule are each closed separately,
    because each is a different reason the resolution has nothing to match:
    a bare ``Name`` with no binding at all (a parameter, read by
    ``_unreadable_suppressor``), a suppressor installed *inside* the body by
    ``stack.enter_context`` rather than in the header, and a suppressor bound
    to a name that the header then enters (``_aliased_suppressions``). The
    ``.__enter__()`` dunder is a fourth spelling, matched by
    ``_suppressed_by_dunder``. All of them lean on the same "cannot prove it
    harmless, so do not assume it" principle the exception argument already
    uses, and all are scoped to *readable* suppressors so they cannot fire on
    the pinned file's own contexts.

    ``pytest.raises(AssertionError)`` is a different mechanism -- it asserts
    that the failure happened -- but the same defeat: the failure is caught,
    matches, and the test goes green on an assert that could not have failed.
    It is matched by ``ASSERTION_CAPTURING_CONTEXTS`` and kept separate from
    ``SUPPRESSING_CONTEXTS`` so the two stay independently checkable.

    Reachability is decided the same way. A literal container that is empty
    never enters its body, so ``for _ in []:`` is the loop spelling of the
    ``if False:`` defeat; a *call* like ``range(0)`` is deliberately not
    matched, because deciding it means reasoning about builtins rather than
    reading a literal, and a wrong answer there drops a live contract.

    ``tree`` supplies the module whose import bindings to resolve, so the
    bindings always come from the file the assert actually lives in.
    """
    owning = tree if tree is not None else _owning_module(function)
    bound = _bound_names(owning, function)
    if _is_after_control_transfer(function, target):
        # #400. A statement that follows `return` / `raise` / `break` /
        # `continue` in the SAME block is dead -- control can never reach it --
        # yet it is still a lexically present `assert`, so the reachability walk
        # above had no reason to look at it and answered "enforced":
        #
        #     def outer(x):
        #         return
        #         assert x != 1        # never evaluated
        #
        # That certifies a defeated contract as load-bearing, which is #308
        # criterion 1 in the self-direction. The branch-shaped defeats above
        # (`if False:`, `for _ in []:`) do not cover it: those are about a
        # *container* that may not be entered, while this is a statement
        # positioned after one that can never fall through.
        return False
    # The shadowing rules below have to answer "is this name the real builtin",
    # and that question includes bindings written at *module* scope. The module
    # the caller just resolved is exactly that answer, so it is registered
    # here rather than rediscovered per callee -- which also means a probe
    # built by a test is classified against the tree the caller passed, not
    # against whichever module happens to be importable.
    _remember_module_for_function(function, owning)
    for ancestor in _ancestors(function, target):
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if ancestor is not function and _is_uncalled_nested_def(function, ancestor):
                # The assert moved into a nested definition nothing calls.
                return False
        elif isinstance(ancestor, (ast.Try, ast.TryStar)):
            if not _in_body(ancestor, target):
                continue
            if any(_swallows_assertion_error(handler) for handler in ancestor.handlers):
                return False
        elif isinstance(ancestor, (ast.With, ast.AsyncWith)):
            if not _in_body(ancestor, target):
                continue
            if _is_suppressing_with(ancestor, bound, function):
                return False
            if _is_user_defined_swallowing_with(ancestor, bound, function, owning):
                return False
            if function is not None and _entered_name_is_dead(ancestor, function, bound, owning):
                return False
        elif (
            isinstance(ancestor, (ast.If, ast.While))
            and _falsy_literal(ancestor.test, function)
            and _in_body(ancestor, target)
        ):
            # `if False:` / `while False:` -- the body never runs. Only the `if
            # False:` case matters: an `else` branch of a falsy `if` is
            # precisely the one that *does* run, hence the `_in_body` guard.
            return False
        elif (
            isinstance(ancestor, (ast.For, ast.AsyncFor))
            and _is_empty_literal_iterable(ancestor.iter)
            and _in_body(ancestor, target)
        ):
            # `for _ in []:` -- the loop body never runs, so an assert inside it
            # is never evaluated. The loop spelling of the `if False:` defeat.
            return False
    return True


def _contains(statement, target):
    """True if ``target`` is ``statement`` or lies anywhere beneath it."""
    return statement is target or any(node is target for node in ast.walk(statement))


_CONTROL_TRANSFERS = (ast.Return, ast.Raise, ast.Break, ast.Continue)

#: The AST fields that hold statements executed as one sequential block. Only
#: these are walked by the #400 reachability rule; `with` also has an `items`
#: list and a `for` an `orelse`, and neither is interleaved with statements.
_BLOCK_FIELDS = frozenset({"body", "orelse", "finalbody"})


def _is_after_control_transfer(function, target):
    """Is ``target`` dead because an earlier sibling can never fall through?

    #400. A ``return`` / ``raise`` / ``break`` / ``continue`` that is
    unconditional *within its own block* makes every later statement in that
    block -- and everything nested under a later statement's body --
    unreachable:

        def outer(x):
            return
            assert x != 1        # dead

    A transfer that terminates the *whole function* (`return` / `raise`) also
    kills a later `with`, `try` or `if` wholesale, because the interpreter never
    begins evaluating that statement at all:

        def outer(x):
            return
            with open("f") as fh:  # never entered
                assert x != 1      # dead

    The chain therefore continues through the *bodies* of every statement
    after the transfer, not just through the statements themselves. A transfer
    nested inside an earlier statement's own body belongs to *that* body, so it
    says nothing about the statements that follow:

        for i in items:
            if i:
                break            # leaves the loop, not this block
            assert x != 1
        assert x != 2              # still reached after the loop

    A guarded transfer likewise leaves the block reachable on the other path:

        for i in items:
            if i:
                continue
            assert x != 1        # still reached when `i` is falsy

    `break` and `continue` are scoped to the nearest enclosing loop, so they
    only kill what follows them in *that* loop's body. A `break` after the loop
    ends cannot suppress an assert placed after the loop, and one inside a
    nested `if` is guarded.

    So the rule is a forward walk down the chain of blocks that hold ``target``:
    at each level, the entry that *is* (or contains) ``target`` is the holder,
    and only the siblings *before* the holder are candidates. A sibling that
    comes after the holder says nothing about the holder's own reachability,
    which is why the walk stops at the holder rather than at ``target``.
    """
    ancestors = _ancestors(function, target)
    for node, holder in zip([function, *ancestors], [*ancestors, target]):
        for body in _statement_lists_holding(node, holder):
            if _is_terminated_before(body, holder):
                return True
    return False


def _statement_lists_holding(node, target):
    """Yield each statement list in ``node`` that directly contains ``target``.

    Only the fields that are *executed as a block* are considered. A `with`
    also carries a list of `items`, and a `for` an `orelse`; neither is
    interleaved with the statements, so a transfer sitting in a preceding
    sibling says nothing about them.
    """
    for field, value in ast.iter_fields(node):
        if (
            field in _BLOCK_FIELDS
            and isinstance(value, list)
            and any(item is target for item in value)
        ):
            yield value


def _is_terminated_before(body, target):
    """Does ``body`` stop being reachable at some point strictly before ``target``?

    The walk is positional but transitive: once the block is terminated, every
    later statement is unreachable, *including* the bodies of compound
    statements that follow, because the interpreter never starts them.
    """
    for node in body:
        if node is target:
            return False
        if _is_bare_transfer(node):
            return True
    return False


def _is_bare_transfer(node):
    """Is ``node`` itself an unconditional ``return`` / ``raise`` / ``break`` / ``continue``?

    A transfer hidden inside a compound statement's body is conditional with
    respect to the enclosing block, so it is not a bare transfer here. An
    ``assert`` is a statement and can be a transfer target, so it is skipped
    rather than ending the search.
    """
    return isinstance(node, _CONTROL_TRANSFERS) and not isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    )


def _contains_any(statements, target):
    """True if ``target`` is one of ``statements`` or lies beneath any of them.

    :func:`_contains` answers the same question for a single node, but the
    ``if``/``else`` split is a pair of *statement lists*, and asking a list for
    its ``_fields`` raised inside a decision function. A branch membership test
    is the question several rules ask of one of those lists.
    """
    return any(_contains(statement, target) for statement in statements or ())


def guard_rejects_the_deadline_terminal_state():
    """Does the guard still *reject* ``termination == "cancelled_or_deadline"``?

    ``guard_is_wired_on_the_fast_clock_path`` only proves the guard is *called*.
    That is not sufficient: a guard whose body asserts nothing is wired to
    nothing, and the exact-count assertions it exists to qualify would then read
    a deadline-truncated run as a retention result again -- the confusion #261
    was filed to remove. This asks for the ``!=`` comparison that does the
    rejecting, so a gutted guard body fails.

    The comparison must also be *enforced*. Wrapping it in
    ``try/except AssertionError: pass`` keeps the node, keeps the operator, and
    leaves the guard unable to fail -- while satisfying a presence-only check
    (#280, mutation M6). ``_is_enforced`` is what closes that.
    """
    tree = _guard_source_tree()
    for func in tree.body:
        if not (isinstance(func, ast.FunctionDef) and func.name == GUARD_FUNCTION):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Assert):
                continue
            for comparison in _comparisons_in(node.test):
                if not (len(comparison.ops) == 1 and isinstance(comparison.ops[0], ast.NotEq)):
                    continue
                right = comparison.comparators[0]
                if (
                    _is_termination_key(comparison.left)
                    and isinstance(right, ast.Constant)
                    and right.value == DEADLINE_TERMINATION
                    and _is_enforced(func, node, tree)
                    and not _may_bypass(node.test)
                ):
                    return True
    return False


# --- #270 criterion 3: the pinned counts must be reached WITH the #261 precondition ---

#: ``run_owner`` applies the guard whenever ``clock_step`` is this value.
GUARDED_CLOCK_STEP = 0.0
RUN_OWNER = "run_owner"


def count_sites_that_bypass_the_guard():
    """Pinned retention sites whose ``run_owner`` call overrides ``clock_step``.

    All four sites get the #261 terminal-state precondition centrally, from
    ``run_owner()``, which applies the guard whenever ``clock_step`` is the
    default ``0.0``. A site that passed its own ``clock_step`` would silently
    opt out of the precondition, leaving its exact count asserting something the
    guard never qualified. Returns the offending ``(function, clock_step)``
    pairs; empty is correct.

    A ``**`` unpacking is read as well as a named keyword. ``ast`` gives it
    ``arg=None``, so a filter on ``keyword.arg == "clock_step"`` skips it
    entirely -- yet ``run_owner(m, p, **{"clock_step": 0.5})`` reaches exactly
    the same unguarded path as the named spelling. Reading only the named form
    is the same mistake #288 corrected for ``except*``: matching one concrete
    node shape instead of the family of spellings that reach it.

    An unpacking that cannot be read statically (a computed dict, a name) is
    reported rather than assumed safe, because it might carry ``clock_step``
    and silently opt the site out. Over-reporting here is the safe direction:
    it is a false alarm on a pinned site, not a missed bypass.
    """
    return [spelling for _name, spelling in _bypassing_sites(_module_tree())]


def _bypassing_sites(milestones_tree):
    """``(function, spelling)`` pairs for the bypasses in this milestones tree.

    ``milestones_tree`` is a parameter so a spellings table can be classified
    against a synthetic ``run_owner`` call without going through
    :func:`_module_tree`, which only ever returns the real module. The table
    test drives this with one mutated call, so it covers the call path
    production uses and not just the per-call classifier.
    """
    offenders = []
    for func_name in RETENTION_COUNT_SITES:
        for func in milestones_tree.body:
            if not (isinstance(func, ast.FunctionDef) and func.name == func_name):
                continue
            for node in ast.walk(func):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == RUN_OWNER
                ):
                    continue
                offenders.extend((func_name, spelling) for spelling in clock_step_overrides(node))
    return offenders


def clock_step_overrides(call):
    """Spellings in this ``run_owner`` call that override the guarded value.

    A single source of truth for the bypass rule, so the caller that reports
    offenders and the table that pins the classification cannot drift apart.
    Reads named keywords and ``**`` unpackings alike: ``ast`` reports an
    unpacking with ``arg=None``, and ``run_owner(m, p, **{"clock_step": 0.5})``
    reaches the same unguarded path as the named spelling.

    An unpacking that cannot be read statically (a computed dict, a bare name)
    is returned as an offender: it might carry ``clock_step`` and silently opt
    the site out, so it is reported rather than assumed safe. Over-reporting is
    the safe direction -- a false alarm on a pinned site, not a missed bypass.
    """
    overrides = []
    for keyword in call.keywords:
        if keyword.arg is not None:
            continue
        for name, value in _unpacked_keywords(keyword.value):
            if name == "<unreadable>" or (name == "clock_step" and value != GUARDED_CLOCK_STEP):
                overrides.append(f"**{{{name}: {value}}}")
    for keyword in call.keywords:
        if keyword.arg != "clock_step":
            continue
        guarded = (
            isinstance(keyword.value, ast.Constant) and keyword.value.value == GUARDED_CLOCK_STEP
        )
        if not guarded:
            overrides.append(ast.unparse(keyword.value))
    return overrides


def _unpacked_keywords(node):
    """``(name, value)`` pairs from a ``**`` unpacking of a literal dict.

    Yields ``("<unreadable>", source)`` for an unpacking whose keys cannot be
    determined statically, so the caller can report it instead of treating an
    unreadable dict as harmless. Values are the literal objects, so the caller
    compares them against ``GUARDED_CLOCK_STEP`` the same way it compares a
    named keyword rather than re-parsing source text.
    """
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        yield ("<unreadable>", ast.unparse(node))
        return
    if not isinstance(value, dict):
        yield ("<unreadable>", ast.unparse(node))
        return
    for key, item in value.items():
        yield (key if isinstance(key, str) else "<unreadable>", item)
