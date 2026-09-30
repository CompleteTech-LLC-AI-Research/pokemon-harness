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
import copy
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

#: A store that binds a name without a right-hand side this module can read
#: (#359). It is a distinct marker rather than ``None`` because ``None`` means
#: "resolved, and the value is not a suppressor" -- a store that *supersedes* a
#: carried one and retires it. Collapsing the two is what made a `for` target
#: invisible and left a swallowed assert reported as live.
UNREADABLE_VALUE = object()

#: Internal "this target does not bind `name`" signal, distinct from a value.
_NO_MATCH = object()

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
    """The *name* of the function that owns ``target`` in ``tree``.

    Name is load-bearing here: :func:`count_comparisons` keys its triples by
    it, and :func:`retention_sites_observed` filters on membership of
    ``RETENTION_COUNT_SITES``. A redefinition of this name with a different
    signature silently returns ``None`` for every lookup and empties the
    observed set, which fails the retention-count pins as "observed []" with
    no other symptom. The #355 work needs an enclosing-function *node* for a
    different purpose and lives in :func:`_nonlocal_parent_function`; do not
    fold the two together.
    """
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


def _assigned_suppressors(function, bound, query=None):
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
    bindings, raw_values = _store_bindings(function, bound, query)
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


def _store_bindings(function, bound, query=None):
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
    raw_values = _raw_store_values(function, query, bound)
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
            value = _loop_value_source(statement, query, bound)
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
            # Order is load-bearing, and it is three steps, not two. The value a
            # destructuring target receives can be reached through a name at
            # either level, and each level needs its own deref:
            #
            #     t = (contextlib.suppress(AssertionError),)
            #     cs, = t                 # the RHS is a Name bound to a container
            #     with cs:
            #         assert x != 1      # swallowed
            #
            #     a, b = b, a             # the *element* is itself a Name
            #     with a:
            #         assert x != 1      # swallowed, after the swap
            #
            # Deref-first alone fixes the first shape and breaks the second: on
            # `(b, a)` the deref is a no-op (a tuple is not a name), so the
            # element `b` is selected but left as a bare `Name`, which is not a
            # readable suppressor and reports the assert live (#381/#382).
            # Select-first alone fixes the second and breaks the first, because
            # the RHS reads as a bare `Name` and no container is ever found
            # (#374). So the right-hand side is dereferenced first, the element
            # is picked out of that resolved container, and the element is
            # dereferenced in turn when it is itself a name.
            resolved = _deref_alias(
                value,
                raw_values,
                orders,
                _binding_order(function, statement),
                name,
            )
            element = _value_bound_by(targets, resolved, name)
            if isinstance(element, ast.Name):
                # The selected element is a name, so it stands for whatever it
                # was bound to rather than for itself. Resolving it here is what
                # keeps a swapped pair -- where both names already hold
                # suppressors -- from reading as a non-suppressor element.
                element = _deref_alias(
                    element,
                    raw_values,
                    orders,
                    _binding_order(function, statement),
                    name,
                )
                if isinstance(element, ast.Name):
                    # The selected element is a name, so it stands for whatever
                    # it was bound to rather than for itself. Resolving it here
                    # is what keeps a swapped pair -- where both names already
                    # hold suppressors -- from reading as a non-suppressor
                    # element.
                    element = _deref_alias(
                        element,
                        raw_values,
                        orders,
                        _binding_order(function, statement),
                        name,
                    )
            bindings.setdefault(name, []).append(
                (
                    statement,
                    element,
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
    statements and ``function`` the scope it sits in; together they are what
    tell a *settled* store from one still waiting on a branch.
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
    latest = max(orders[id(entry[0])] for entry in entries)
    tied = [entry for entry in entries if orders[id(entry[0])] == latest]
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
    last = tied[0][1]
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


def _exclude_for_alias_walk(entries, origin, current, target, revisiting):
    """The store this pass of the alias walk must not resolve to.

    #371. A *self-alias* -- a store whose right-hand side names the very name
    it binds -- is resolved by excluding that store, so the chain falls through
    to the binding that preceded it:

        for cs in (contextlib.nullcontext(),):
            with (cs := cs):        # `cs` still holds the loop's element
                assert x != 1

    The loop target and the walrus sit in one top-level statement, so they
    share a binding order by construction and every position-based test ties.
    The exclusion is what breaks that tie, and it has to name the *entry*, not
    a flag.

    It could not do that before. `origin` is only filled in *after* a pass
    returns, so on the first pass -- which is the only pass a self-alias needs
    -- it is still `None`, and passing it straight through excluded nothing.
    `_last_store_before` then saw a genuine tie, and #367's tie branch, which
    exists to decline a `for`/`else` tie it cannot resolve, declined it:

        >>> _last_store_before(entries, orders, index, exclude=<the walrus>)
        ast.Call                       # the loop's nullcontext()
        >>> _last_store_before(entries, orders, index, exclude=None)
        AMBIGUOUS_SUPPRESSOR           # tie -> declined

    The tie is decidable here, so declining it was wrong. `AMBIGUOUS_SUPPRESSOR`
    then reached `_is_suppressing_with`, which reads the marker as "may be any
    suppressor", and a `nullcontext()` loop target was reported defeated. The
    suppressor spelling of the same row was unaffected -- `False` is the
    expected answer there too -- so only the control row could see it.

    So the entry is located directly: it is the one whose statement is the
    store being resolved. `current.id == target` identifies that walk, and
    `revisiting` keeps the existing behaviour of excluding the entry that
    supplied the value on a later pass.
    """
    if origin is not None and (current.id == target or revisiting):
        return origin
    if current.id == target:
        return next(
            (
                entry
                for entry in entries
                if entry[1] is not None and _is_the_store_being_resolved(entry, target)
            ),
            None,
        )
    return origin if revisiting else None


def _is_the_store_being_resolved(entry, target):
    """Is ``entry`` the self-alias store named by ``target``?"""
    return isinstance(entry[1], ast.Name) and entry[1].id == target


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
            _exclude_for_alias_walk(entries, origin, current, target, revisiting),
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


def _raw_store_values(function, query=None, bound=None):
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

    A store that binds a name *without* recording a value for it is not simply
    absent from this table: it is recorded with :data:`UNREADABLE_VALUE`. The
    distinction is load-bearing and was the whole of #359.

        for cs in (contextlib.suppress(AssertionError),):
            with (cs := cs):        # `cs` is the loop's live value
                assert 1 == 2       # swallowed

    A ``for`` target used to contribute *no* entry at all, so the walrus's
    right-hand side found nothing to adopt, stayed an unreadable ``ast.Name``,
    and the header was judged live -- a swallowed assert certified as
    load-bearing. A missing entry has to mean "this name is not bound to
    anything this table knows", which is a claim the table cannot make: the
    loop *did* bind it. Recording the store with an explicitly unreadable value
    lets :func:`_deref_alias` decline instead of guessing, which is the safe
    direction per #308 criterion 1.

    ``with ... as``, ``except ... as`` and a ``match`` capture bind a name the
    same way, and are handled the same way. A ``del`` is different: it
    *unbinds* the name, which is what #336's reachability question is about,
    and it is deliberately left out of this table so the two do not blur.

    Destructuring is handled per-target rather than per-statement. The old code
    gave every name the whole right-hand side:

        cs, other = (contextlib.suppress(AssertionError), 2)

    so ``cs`` was recorded as the ``Tuple`` rather than as the suppressor
    inside it, ``_is_readable_suppressor`` rejected the container, and a
    swallowed assert was reported live. :func:`_value_bound_by` picks the
    element that actually lands on each name.

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
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            # A loop target binds on every path that reaches the loop, so it
            # retires any carried value. The value is the next *element*, not
            # the iterable, and it is readable when the iterable is a literal.
            targets, value = [statement.target], _loop_value_source(statement, query, bound)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            targets = [
                item.optional_vars for item in statement.items if item.optional_vars is not None
            ]
            value = UNREADABLE_VALUE
        elif isinstance(statement, ast.ExceptHandler):
            if statement.name is None:
                continue
            targets = [ast.Name(id=statement.name, ctx=ast.Store())]
            value = UNREADABLE_VALUE
        else:
            continue
        for name in _store_target_names(targets):
            raw.setdefault(name, []).append((statement, _value_bound_by(targets, value, name)))
    return raw


def _value_bound_by(targets, value, name):
    """The right-hand side that actually lands on ``name``.

    For a plain target the whole right-hand side is the value, so this is the
    identity. Destructuring is the only case that needs real work:

        cs, other = (contextlib.suppress(AssertionError), 2)
        [cs] = [contextlib.suppress(AssertionError)]
        cs, *rest = (suppressor, 2, 3)

    Returning the container itself made every one of those spell the
    suppressor unreadable, and an unreadable value is indistinguishable from
    "not a suppressor", so the assert below it was certified live. Matching the
    target's position against the value's elements recovers the real value.

    A starred target collects the remainder, so its position is not an index;
    that case, and any shape where the correspondence cannot be established
    (a nested target whose parent did not match, a length mismatch), returns
    :data:`UNREADABLE_VALUE` so the caller declines rather than guessing.

    A target that is not itself a container is the identity case and never
    reaches :func:`_element_for_target`:

        cs = contextlib.suppress(AssertionError)
        [cs] = [contextlib.suppress(AssertionError)]

    Both bind ``cs`` from the right-hand side, but only the second picks an
    element out of it. The first has to be returned whole, which is what keeps
    an ordinary store resolving to its own ``suppress`` call.
    """
    if value is UNREADABLE_VALUE or not isinstance(value, (ast.Tuple, ast.List)):
        return value
    for target in targets:
        if not isinstance(target, (ast.Tuple, ast.List)):
            # A bare target takes the whole right-hand side, so this store
            # cannot be the one that binds `name` by position -- unless it is
            # the name itself, which is the identity case handled above.
            if isinstance(target, ast.Name) and target.id == name:
                return value
            continue
        bound = _element_for_target(target, value.elts, name)
        if bound is not _NO_MATCH:
            return bound
    return UNREADABLE_VALUE


def _element_for_target(target, elements, name):
    """The element of ``elements`` that lands on ``name`` via ``target``.

    Targets and value elements are walked in lockstep, because that is how
    Python itself destructures: the n-th target receives the n-th element. A
    nested tuple/list target recurses against the correspondingly nested
    element, so ``(a, (b, c)) = (1, (2, 3))`` resolves ``b`` to ``2``.

    A ``Starred`` target collects a *run* of elements rather than one, so the
    positions after it are not the same indices as the targets. Python binds
    them from the end: in ``a, *rest, cs = (1, 2, 3, 4)`` the star takes
    ``2, 3`` and ``cs`` gets ``4``, the last element. Returning
    :data:`UNREADABLE_VALUE` at the star instead would leave every target
    after it unresolved, so

        a, *rest, cs = (1, 2, 3, contextlib.suppress(AssertionError))
        with cs:
            assert x != 1          # swallowed

    was reported ``enforced``. That is the damaging direction, and it is
    reachable with nothing exotic. The star's own value stays unreadable --
    it is a list, and a name bound to a list cannot be entered -- so the star
    itself still declines.

    A target/value length mismatch is likewise unreadable rather than an
    index error, so a shape this function cannot model degrades to declining.

    The walk is *structural* and must not flatten the target. Flattening
    ``(a, (b, c))`` to three leaves and indexing one flat list of the value's
    elements pairs ``b`` with the second element of the *outer* value rather
    than with the first element of the *nested* one:

        (other, (cs, third)) = (2, (suppress(AssertionError), 3))

    There ``cs` lands on ``3`` that way, so the suppress call was read as
    unreachable and the swallowed assert was reported live. Descending in step
    with the value is what keeps the correspondence Python actually performs.
    """
    star = next(
        (i for i, leaf in enumerate(target.elts) if isinstance(leaf, ast.Starred)),
        None,
    )
    for index, leaf in enumerate(target.elts):
        if index >= len(elements):
            return UNREADABLE_VALUE
        if star is not None and index > star:
            # Targets after a star are bound from the END of the value, so the
            # correspondence is measured from the other end: the last target
            # takes the last element, the one before it the second-last, and so
            # on. In `a, *rest, cs = (1, 2, 3, s)` that is `3 -> s`, which is
            # the whole point -- `cs` really does receive the suppressor.
            element_index = len(elements) - 1 - ((len(target.elts) - 1) - index)
        else:
            element_index = index
        if isinstance(leaf, ast.Name):
            if leaf.id == name:
                return elements[element_index]
            continue
        if isinstance(leaf, ast.Starred):
            if isinstance(leaf.value, ast.Name) and leaf.value.id == name:
                return UNREADABLE_VALUE
            continue
        if isinstance(leaf, (ast.Tuple, ast.List)):
            nested = elements[element_index]
            if not isinstance(nested, (ast.Tuple, ast.List)):
                return UNREADABLE_VALUE
            bound = _element_for_target(leaf, nested.elts, name)
            if bound is not _NO_MATCH:
                return bound
            continue
        return UNREADABLE_VALUE
    return _NO_MATCH


def _loop_value_source(statement, query=None, bound=None):
    """Read the final element only after a literal loop has completed.

    A header inside the body can see every iteration's value. A literal with
    one element is readable there; a mixed multi-element literal has no single
    representative and must stay unreadable. After-loop reads keep the final
    element rule; resolving bindings inside an else clause is separate.
    """
    iterable = statement.iter
    if not isinstance(iterable, (ast.Tuple, ast.List)) or not iterable.elts:
        return UNREADABLE_VALUE
    if len(iterable.elts) == 1:
        return iterable.elts[0]
    if query is not None and any(_contains(child, query) for child in statement.body):
        # A representative is sound only when every iteration suppresses the
        # same failure. Mixed managers can propagate on an earlier iteration.
        imports = bound if hasattr(bound, "get") else {}
        if all(
            _is_readable_suppressor(element, imports)
            and any(_name_catches_assertion_error(name) for name in _suppression_names(element))
            for element in iterable.elts
        ):
            return iterable.elts[0]
        return UNREADABLE_VALUE
    return iterable.elts[-1]


def _aliased_suppressions(node, function, bound, owning=None):
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
    name from an outer scope is deliberately not followed *by default*:
    assuming an arbitrary call returns a suppressor would report live asserts
    as dead on every context manager this check cannot trace. The one exception
    is a name the function explicitly declares ``nonlocal``, which the language
    guarantees refers to an enclosing function's binding -- see
    :func:`_nonlocal_suppressors` and #355.

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
    by_index, raw_values = _assigned_suppressors(function, bound, node)
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
                    header, statement, bound_so_far, by_index.get(index, {}), function
                )
            )
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
                    elif isinstance(walrus.value, ast.Name):
                        # A `nonlocal` self-alias resolves to nothing locally:
                        #
                        #     def inner():
                        #         nonlocal cs
                        #         with (cs := cs):   # `cs` is the parent's
                        #             assert 1 == 2  # suppressor -> swallowed
                        #
                        # `raw_values` is built from `inner` alone, and
                        # `inner` stores no `cs`, so the dereference above
                        # returns the bare name. The enclosing function's
                        # binding is the real answer (#355).
                        entered.extend(
                            _nonlocal_suppressors(
                                walrus.value.id, function, bound, owning, walrus.value
                            )
                        )
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
                elif isinstance(expression, ast.Name):
                    # The name is not bound by anything this function can see.
                    # A `nonlocal` declaration says it *is* bound in an
                    # enclosing function, and this module walks one function
                    # at a time, so a suppressor sitting in the parent scope is
                    # invisible here even though the header really enters it
                    # (#355).
                    entered.extend(
                        _nonlocal_suppressors(expression.id, function, bound, owning, expression)
                    )
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


def _nonlocal_declared(function):
    """The names ``function`` declares ``nonlocal``, or empty.

    A ``nonlocal`` statement is a declaration, not an assignment: it performs
    no store and binds nothing. It is the one place the source states outright
    that a name belongs to an enclosing function rather than to this one, so
    it is exactly the permission needed to look outward -- and it is what
    keeps the search narrow. A name that is merely inherited by closure, or
    read from a module global, is *not* listed and is still not followed.
    """
    if function is None:
        return frozenset()
    declared = set()
    for node in _scope_body_nodes(function):
        names = getattr(node, "names", None)
        if isinstance(node, ast.Nonlocal) and names:
            declared.update(names)
    return frozenset(declared)


def _nonlocal_suppressors(name, function, bound, owning=None, header=None):
    """Readable enclosing values still in force at direct calls of this function.

    A nonlocal declaration identifies a scope, not a permanently fixed value.
    A store in this function can retire the enclosing suppressor, and parent
    stores must be read at the invocation rather than collected indiscriminately.
    Only direct calls whose binding can be proved readable are followed. A
    returned closure or an opaque intervening call is left undecided.
    """
    if name not in _nonlocal_declared(function):
        return ()
    if _nonlocal_rebound_before(name, function, header, bound):
        return ()
    parent = _nonlocal_parent_function(function, owning)
    if parent is None:
        return ()
    # The ordinary raw table walks nested bodies. Those stores have not run
    # merely because their definitions were executed, so remove the bodies
    # from a private AST before building the parent's invocation snapshots.
    scope = copy.deepcopy(parent)
    for node in _scope_body_nodes(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.body = []
        elif isinstance(node, ast.Lambda):
            node.body = ast.Constant(value=None)
    parent_bound = _nonlocal_parent_imports(parent, owning)
    bindings, _raw = _store_bindings(scope, parent_bound)
    entries = bindings.get(name, ())
    if not entries:
        return ()
    orders = {id(statement): _binding_order(scope, statement) for statement, _, _ in entries}
    calls = [
        index
        for index, statement in enumerate(scope.body)
        if isinstance(statement, (ast.Expr, ast.Return))
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == function.name
    ]
    if not calls:
        return ()
    resolved = []
    for index in calls:
        visible = [entry for entry in entries if orders[id(entry[0])] < index]
        if not visible:
            return ()
        unconditional = [entry for entry in visible if not entry[2]]
        if not unconditional:
            return ()
        latest = max(orders[id(entry[0])] for entry in unconditional)
        if any(entry[2] and orders[id(entry[0])] >= latest for entry in visible):
            # A conditional replacement may have changed the object. The
            # ordinary suppression reader's ambiguity marker is not proof
            # that every possible captured value actually suppresses.
            return ()
        # Another call can mutate the captured cell. Its effects are opaque;
        # a readable earlier assignment is insufficient evidence after it.
        if any(
            any(isinstance(node, ast.Call) for node in _scope_body_nodes(statement))
            or isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and statement.decorator_list
            for statement in scope.body[latest + 1 : index]
        ):
            return ()
        value = _resolve_bindings(visible, parent_bound, orders, index, scope)
        if not _is_readable_suppressor(value, parent_bound):
            return ()
        if not _nonlocal_has_known_exception_arguments(value, bindings, scope):
            return ()
        if not _nonlocal_constructor_is_unshadowed(value, parent, owning):
            return ()
        resolved.append(value)
    return resolved


def _nonlocal_parent_imports(parent, owning):
    """Resolve the constructor using lexical imports, with inner scopes winning."""
    scopes = []
    scope = parent
    while scope is not None:
        scopes.append(scope)
        scope = _nonlocal_parent_function(scope, owning)
    bound = _bound_names(owning) if owning is not None else {}
    for scope in reversed(scopes):
        # _own_imports uses a stack; restore source order for sequential imports.
        imports = sorted(_own_imports(scope), key=lambda node: (node.lineno, node.col_offset))
        bound.update(_bound_names(ast.Module(body=imports, type_ignores=[])))
    return bound


def _nonlocal_constructor_is_unshadowed(value, parent, owning):
    """An import spelling must still identify its constructor in enclosing scopes."""
    root = value.func
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name):
        return False
    scopes = []
    scope = parent
    while scope is not None:
        scopes.append(scope)
        scope = _nonlocal_parent_function(scope, owning)
    if isinstance(owning, ast.Module):
        scopes.append(owning)
    for scope in scopes:
        for node in _scope_body_nodes(scope):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
                and node.id == root.id
            ):
                return False
            if isinstance(node, ast.arg) and node.arg == root.id:
                return False
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == root.id
            ):
                return False
            if isinstance(node, ast.ExceptHandler) and node.name == root.id:
                return False
        if root.id in _match_capture_names(scope):
            return False
    return True


def _nonlocal_has_known_exception_arguments(value, bindings, scope):
    """Follow only unshadowed builtin exception arguments across a scope boundary.

    The ordinary suppression reader treats an unreadable exception as universal.
    That is insufficient evidence for following an outer object: suppress()
    catches nothing, and an alias or a shadowed AssertionError may name a
    harmless exception. Decline those values rather than importing that guess.
    """
    if not value.args:
        return False
    builtins = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
    parameters = {
        argument.arg
        for argument in [*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs]
    }
    parameters.update(
        argument.arg for argument in (scope.args.vararg, scope.args.kwarg) if argument is not None
    )
    for argument in value.args:
        if not isinstance(argument, ast.Name):
            return False
        candidate = builtins.get(argument.id)
        if not isinstance(candidate, type) or not issubclass(candidate, BaseException):
            return False
        if argument.id in parameters or argument.id in bindings:
            return False
    return True


def _nonlocal_rebound_before(name, function, header, bound):
    """Has this function already replaced the enclosing binding at this read?"""
    if header is None:
        return False
    bindings, _raw = _store_bindings(function, bound)
    own_nodes = {id(node) for node in _scope_body_nodes(function)}
    position = (header.lineno, header.col_offset)
    for statement, _value, _conditional in bindings.get(name, ()):
        if id(statement) not in own_nodes:
            continue
        if (statement.lineno, statement.col_offset) > position:
            continue
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            # An as-target binds after its own context expression, and before
            # a later item's expression. Compare that target, not the whole
            # with statement, to the particular read being resolved.
            for item in statement.items:
                target = item.optional_vars
                if (
                    target is not None
                    and name in _store_target_names([target])
                    and (target.lineno, target.col_offset) < position
                ):
                    return True
            continue
        # A self-alias does not replace the object; all other stores retire
        # the parent answer even when their value is not a readable suppressor.
        value = getattr(statement, "value", None)
        if isinstance(value, ast.Name) and value.id == name:
            continue
        return True
    return False


def _nonlocal_parent_function(function, owning=None):
    """The nearest function ancestor of this exact node, not the outermost."""
    trees = [owning] if owning is not None else [milestones_tree(), _module_tree()]
    for tree in trees:
        if tree is None:
            continue
        pending = [(tree, None)]
        while pending:
            node, parent = pending.pop()
            if node is function:
                return parent
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = node
            pending.extend((child, parent) for child in ast.iter_child_nodes(node))
    return None


def _bindings_before(header, statement, bound_so_far, own, function=None):
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
        # A loop binds its target before the first iteration's body runs, so a
        # header directly inside that body has already had the target assigned
        # when it is read (#359):
        #
        #     for cs in (contextlib.suppress(AssertionError),):
        #         with cs:             # the loop's live value, a suppressor
        #             assert x != 1    # swallowed
        #
        # The `for` is the *block* here, not an entry in some enclosing body
        # list, so the `seen_store` scan below never sees it. Without this the
        # header reads as carrying nothing, the resolved suppressor is thrown
        # away, and the swallowed assert is reported live.
        #
        # Only the *target* is guaranteed, so only the target's own binding may
        # be pulled in. `own` is the whole statement's store set, and a store
        # written further down the same body has not run yet:
        #
        #     for a in items:
        #         with cs:            # `cs` is not bound at all -> NameError
        #             assert x != 1   # live
        #         cs = contextlib.suppress(AssertionError)
        #
        # Adopting the whole of `own` here would answer that first header with
        # the `cs =` below it and report a live assert as swallowed.
        loop_target = _loop_target_names(block)
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
        for node in body:
            if node is header:
                # Nothing in this block has been stored before the header, so
                # only the bindings carried in from earlier statements apply.
                if seen_store or captures:
                    return dict(own)
                if loop_target:
                    scoped = dict(bound_so_far)
                    scoped.update({n: own[n] for n in loop_target if n in own})
                    return scoped
                return bound_so_far
            if _is_store_statement(node):
                seen_store = True
    return bound_so_far


def _loop_target_names(block):
    """The names a loop statement binds before its body runs, else empty."""
    if not isinstance(block, (ast.For, ast.AsyncFor)):
        return ()
    return frozenset(_store_target_names([block.target]))


def _is_store_statement(node):
    """Does reaching this point in a block mean the block's stores have run?

    #359: this list was ``ast.Assign``/``ast.AnnAssign`` only, so a ``for``
    target did not count as a store that had run by the time a ``with`` nested
    inside the loop body was read:

        for cs in (contextlib.suppress(AssertionError),):
            with cs:                 # `cs` IS the loop's live value
                assert x != 1        # swallowed

    The loop's own binding resolved correctly, and then this function threw it
    away because the enclosing statement was not one of the two shapes it
    recognised, so the header saw no binding at all and reported a swallowed
    assert as live. A ``for`` target binds on every iteration that reaches the
    body, which is the guarantee the other two shapes are listed for, so it
    belongs in the same set.

    ``NamedExpr`` is deliberately still absent: it never appears as a direct
    statement, so it cannot be the ``node`` walked here.

    #378: "on every iteration that reaches the body" is a conditional
    guarantee, and a literal empty container gives a loop that reaches the body
    exactly zero times:

        cs = contextlib.nullcontext()
        if True:
            for x in ():                       # never iterates
                cs = contextlib.suppress(AssertionError)   # never executes
            with cs:                          # `cs` is still the nullcontext
                assert x != 1                 # live

    Counting the ``for`` as a store that ran admits the unexecuted assignment
    and answers ``defeated``, dropping a live assert from the sentinel. Master
    answers ``enforced`` here, which is correct, so counting every loop was a
    regression rather than a pre-existing gap.

    The test is `_is_empty_literal_iterable`, the same helper the ``for``-defeat
    rule already uses, so the two agree on what a decidable empty iterable is.
    A call such as ``range(0)`` is deliberately still counted as having run:
    settling that means reasoning about builtins, and guessing wrong here
    drops a live contract, which is the more damaging error.
    """
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return not _is_empty_literal_iterable(node.iter)
    return isinstance(node, ast.Assign) or (
        isinstance(node, ast.AnnAssign) and node.value is not None
    )


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
    """
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return ast.unparse(expression)
    return None


def _locally_defined_classes(tree):
    """``{name: ClassDef}`` for every class defined at module level in ``tree``.

    A class imported from elsewhere is deliberately absent, which is what keeps
    the rule off ``pytest``'s and the harness's own context managers. Only a
    module-level ``class`` counts. A class *nested* inside another is excluded
    too, and that is a measured limit rather than an oversight: such a class is
    spelled ``Outer.Inner()`` in a ``with`` header, and matching the inner name
    alone would fire on a bare ``Inner`` that is a different object in a
    different scope. Widening this collection to every scope was tried and left
    the suite green, so the narrow rule is the one doing the work.
    """
    found = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            found[node.name] = node
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
    }
)


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
        if _binds_starred_target(statement, name):
            if isinstance(statement, (ast.For, ast.AsyncFor)) and not _starred_store_decides_kind(
                (statement, value, _conditional),
                name,
                by_index["orders"],
                index,
                function,
                by_index.get("header"),
            ):
                return False
            # #418. `*cs, = (...)` and `a, *cs = (...)` collect a run of
            # elements into a **list**, so the *name* is a list whatever the
            # right-hand side held. `list` has no `__enter__`, so entering it
            # raises `TypeError` before the body is reached and the assert
            # never runs: the contract is dead.
            #
            # This is deliberately checked *before* the general
            # destructuring decline below. That decline exists because the
            # type of a plain element is not readable from the container's
            # syntax, so it answers "cannot tell" and keeps the assert live --
            # the safe direction. A starred target is not such a case: the
            # list-wrapping is decided by the target syntax alone, and it is
            # decidable. Reading the wrapped element's kind instead let a
            # usable suppressor vouch for a bare list, certifying a dead
            # assert as enforced.
            #
            # Executed on CPython 3.12.14:
            #   *cs, = (contextlib.suppress(AssertionError),)  -> cs == [suppress]
            #   a, *cs = (contextlib.suppress(...), 2)         -> cs == [2]
            #   b, *c = (1, contextlib.suppress(...))         -> c == [suppress]
            # and in all three `hasattr(cs, "__enter__")` is False.
            kinds.add("list")
            continue
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
                # #420. A *starred* loop target is decidable from the target
                # syntax alone: `for *cs, in (...):` binds the list the
                # unpacking collects, so entering it raises `TypeError`
                # before the body and the assert below never runs. The
                # decline below is for the *plain* loop target, which binds
                # the next element and genuinely cannot be read without
                # running the loop -- `for cs in (nullcontext(),):` is
                # really live (#336). Recording the starred case as a
                # `NoneType` here would be wrong for the same reason the
                # assign path is: the wrapped element's kind is irrelevant
                # to whether the *name* is enterable.
                if _starred_names_in_loop_target(statement, name) and _starred_store_decides_kind(
                    (statement, value, _conditional),
                    name,
                    by_index["orders"],
                    index,
                    function,
                    by_index.get("header"),
                ):
                    kinds.add("list")
                    continue
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
            # and must not be read as a non-manager here.
            return False
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
    already answers the identical shape the same way, because
    :func:`_stores_of` is what drops conditional stores on *both* sides.
    Given

        def outer(...):
            import os as cs
            if flag:
                cs = contextlib.nullcontext()
            else:
                cs = contextlib.nullcontext()
            with cs: ...

    the function path keeps the unconditional carrier and answers ``defeated``
    on master, before this rule existed. The module path now agrees with it
    rather than inventing a stricter policy for one scope only. Lifting the
    shared conservatism -- reading a conditional binding as settled -- would
    mean changing :func:`_stores_of` for every rule at once, and is
    deliberately not folded in here.

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
    if name_entries:
        # #359. `_stores_of` keeps only unconditional stores, so a carrier
        # that is unconditional survives a *conditional* non-carrier store
        # that follows it. That store may have run and replaced the carrier
        # with an enterable value, in which case the carrier is stale and
        # answering from it would drop a live assert. When that happens the
        # value is genuinely undecidable, so decline rather than guess.
        #
        # `except ... as cs:` is excluded because it *unbinds* rather than
        # supersedes: CPython deletes the name when the handler exits, so the
        # earlier carrier is what remains in force. `_stores_of` makes the same
        # exception for the same reason, and without it here a try/except that
        # merely mentions the name would flip a correct `defeated` to
        # `enforced`.
        carrier_orders = [
            orders[id(statement)]
            for statement, value, conditional in name_entries
            if not conditional and isinstance(value, str)
        ]
        if carrier_orders:
            last_carrier = max(carrier_orders)
            if any(
                conditional
                and not isinstance(value, str)
                and not isinstance(statement, ast.ExceptHandler)
                and orders[id(statement)] > last_carrier
                for statement, value, conditional in name_entries
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


def _binds_starred_target(statement, name):
    """Does this store bind ``name`` through an ``ast.Starred`` target?

    ``*cs, = (a,)``, ``a, *cs = (a, b)`` and ``(*cs,) = (a, b)`` all give the
    name the *list* the unpacking collects, so the answer does not depend on
    the right-hand side at all.

    ``_store_target_names`` flattens a ``Starred`` into the name inside it, so
    by the time a name reaches here a starred binding is indistinguishable
    from a positional one. This walks the same target lists again and keeps
    only the names reached *through* a ``Starred``.
    """
    if not isinstance(statement, ast.Assign):
        return _starred_names_in_loop_target(statement, name)
    return name in _starred_target_names(statement.targets)


def _starred_names_in_loop_target(statement, name):
    """#420. Is ``name`` a starred element of a ``for`` target?

    `for *cs, in (...):` binds ``cs`` the same way `*cs, = (...)` does -- to the
    *list* the unpacking collects -- so the target syntax alone decides that the
    name is a list and `with cs:` raises `TypeError` before the body.

    #419 fixed the plain `ast.Assign` spelling. The loop target is reached
    through a different path: it is recorded with no value, so the rule falls
    to the loop-target decline, which exists because a *plain* loop target
    binds the next element and cannot be read without running the loop
    (`#336`). That decline is right for `for cs in (nullcontext(),):`, which is
    genuinely live, and over-broad for the starred sub-case, where nothing
    about the iterable matters.
    """
    if not isinstance(statement, (ast.For, ast.AsyncFor)):
        return False
    return name in _starred_target_names([statement.target])


def _starred_target_names(targets):
    """Names whose final store in these targets collects a starred list.

    CPython assigns targets and nested elements from left to right. A later
    plain target can overwrite the list: ``*cs, cs = (1, nullcontext())``
    leaves ``cs`` a context manager. Track every name store in that order,
    including the separate targets of a chained assignment.
    """
    final_stores = {}
    pending = list(reversed(targets))
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Name):
            final_stores[target.id] = False
        elif isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(reversed(target.elts))
        elif isinstance(target, ast.Starred):
            if isinstance(target.value, ast.Name):
                final_stores[target.value.id] = True
            else:
                # Unpacking the collected list again binds its elements,
                # rather than assigning the list itself to every nested name.
                pending.append(target.value)
    return {name for name, starred in final_stores.items() if starred}


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
    decidable = [
        entry
        for entry in entries
        if not entry[2]
        or isinstance(entry[0], ast.ExceptHandler)
        or _starred_store_decides_kind(entry, name, orders, index, function, by_index.get("header"))
        or _store_is_settled_before(entry, orders, index, function)
    ]
    latest = max((orders[id(entry[0])] for entry in decidable), default=None)
    if latest is None:
        return None
    tied = [entry for entry in decidable if orders[id(entry[0])] == latest]
    if len(tied) == 1:
        return tied
    # Two stores share the top-level statement, so `orders` cannot separate
    # them. Among the tied, the one this rule newly deemed *settled* is the
    # later write in the block: it is written into a `with` body, and by the
    # time a *subsequent* top-level statement is reached it has run, so it is
    # the value in force. Returning it alone keeps the dead-entry rule from
    # reading the earlier carried suppressor as if it were still in play.
    settled = [entry for entry in tied if _store_is_settled_before(entry, orders, index, function)]
    return settled or tied


def _starred_store_decides_kind(entry, name, orders, index, function, header=None):
    """A starred store must have run before this header and remain in force.

    A direct earlier sibling on the header's execution path has run whenever
    the header is reached. A loop target has run when the header is inside
    that loop's body. Future assignments, skipped branches, and loop targets
    read after a possibly empty loop provide no such guarantee.
    """
    statement = entry[0]
    if header is None or not _binds_starred_target(statement, name):
        return False
    prior = _statements_before_header(function, header)
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        reached = any(header is child for node in statement.body for child in ast.walk(node))
        if not reached and isinstance(statement, ast.For) and statement in prior:
            # A completed literal nonempty loop necessarily assigned its
            # target at least once. An empty or dynamically sized iterable
            # can leave the previous usable manager untouched.
            reached = (
                isinstance(statement.iter, (ast.Tuple, ast.List, ast.Set))
                and bool(statement.iter.elts)
                and not any(isinstance(element, ast.Starred) for element in statement.iter.elts)
            )
    else:
        reached = orders[id(statement)] == index and statement in prior
    if not reached:
        return False
    # Even a conditional intervening store can replace the list. Decline it
    # rather than deciding one path's value for every path to the header.
    bindings, _ = _store_bindings(function, {})
    position = lambda node: (node.lineno, node.col_offset)
    return not any(
        other is not statement and position(statement) < position(other) < position(header)
        for other, _, _ in bindings.get(name, ())
    )


def _statements_before_header(function, header):
    """Direct earlier siblings on the syntactic path to this header."""
    prior = []
    node = function
    while node is not header:
        found = None
        for _, value in ast.iter_fields(node):
            children = value if isinstance(value, list) else [value]
            previous = []
            for child in children:
                if not isinstance(child, ast.AST):
                    continue
                if any(descendant is header for descendant in ast.walk(child)):
                    found = child
                    prior.extend(previous)
                    break
                if isinstance(child, ast.stmt):
                    previous.append(child)
            if found is not None:
                break
        if found is None:
            return []
        node = found
    return prior


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
    bindings, _raw_values = _store_bindings(function, bound, header)
    orders = {
        id(statement): _binding_order(function, statement)
        for entries in bindings.values()
        for statement, _, _ in entries
    }
    by_index = {"bindings": bindings, "orders": orders, "header": header}
    for index, statement in enumerate(function.body):
        for candidate in ast.walk(statement):
            if candidate is not header:
                continue
            return any(
                _entry_is_dead(item.context_expr, by_index, index, function, bound, module)
                for item in header.items
            )
    return False


def _is_suppressing_with(node, bound, function=None, owning=None):
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
    for argument in _aliased_suppressions(node, function, bound, owning):
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


def _falsy_literal(node):
    """A condition that is a literal false, so its body can never run."""
    return isinstance(node, ast.Constant) and not node.value


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
            if _is_suppressing_with(ancestor, bound, function, owning):
                return False
            if _is_user_defined_swallowing_with(ancestor, bound, function, owning):
                return False
            if function is not None and _entered_name_is_dead(ancestor, function, bound, owning):
                return False
        elif (
            isinstance(ancestor, (ast.If, ast.While))
            and _falsy_literal(ancestor.test)
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
            for sibling in body:
                if sibling is holder:
                    break
                if _cannot_fall_through(sibling):
                    return True
    return False


def _cannot_fall_through(node):
    """Does a try statement have no path to the following statement?

    Normal completion, exception handling, and finally are separate paths.
    A body ending in return can still raise *before* it returns; an else arm
    runs only when the body completes normally. A falling-through handler
    must therefore keep the following assert live on either spelling.

    The effect model deliberately includes exceptions from opaque expressions.
    It proves absence of normal completion; it does not predict whether a call
    raises or whether a particular handler matches an exception.
    """
    return isinstance(node, (ast.Try, ast.TryStar)) and "normal" not in _statement_exits(node)


def _block_exits(body):
    """Possible exits from a sequential block, including exceptions."""
    exits = {"normal"}
    for statement in body:
        if "normal" not in exits:
            break
        exits = (exits - {"normal"}) | _statement_exits(statement)
    return exits


def _statement_exits(node):
    """A conservative set of normal and control-transfer exits."""
    if isinstance(node, ast.Return):
        # Evaluating a return value can raise before the return is committed.
        return {"return"} if _expression_cannot_raise(node.value) else {"return", "raise"}
    if isinstance(node, ast.Raise):
        return {"raise"}
    if isinstance(node, ast.Break):
        return {"break"}
    if isinstance(node, ast.Continue):
        return {"continue"}
    if isinstance(node, ast.Pass) or (
        isinstance(node, ast.Expr) and _expression_cannot_raise(node.value)
    ):
        return {"normal"}
    if isinstance(node, ast.If):
        exits = _block_exits(node.body) | _block_exits(node.orelse)
        if not _expression_cannot_raise(node.test):
            exits.add("raise")
        return exits
    if not isinstance(node, (ast.Try, ast.TryStar)):
        # Calls, assignments, imports, with headers and loops can all complete
        # or raise. Nested scopes' transfers do not transfer from this block.
        return {"normal", "raise"}

    body = _block_exits(node.body)
    exits = body - {"normal", "raise"}
    if "normal" in body:
        exits |= _block_exits(node.orelse)
    if "raise" in body:
        # Include unhandled exceptions and every possible handler. Matching
        # exception types is intentionally not guessed from their spelling.
        exits.add("raise")
        for handler in node.handlers:
            exits |= _block_exits(handler.body)
    if node.finalbody:
        final = _block_exits(node.finalbody)
        # A finalizer transfer replaces pending return/raise/break/continue;
        # falling through preserves the exit which entered the finalizer.
        exits = (exits if "normal" in final else set()) | (final - {"normal"})
    return exits


def _expression_cannot_raise(node):
    """Only literal values have a statically guaranteed evaluation here."""
    return node is None or isinstance(node, ast.Constant)


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
