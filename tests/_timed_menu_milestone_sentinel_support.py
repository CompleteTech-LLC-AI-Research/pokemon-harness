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

#: Sentinel for a suppressor reached through ``name.__enter__()``. The dunder
#: is ``pass`` on every suppressor, so the entry raises ``TypeError`` before the
#: body runs whatever exception list it was built with. The argument is
#: therefore irrelevant and cannot be read off the binding, so the shape gets
#: its own marker instead of being classified from the suppressor's arguments.
LOUD_DUNDER = object()

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
    """
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
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
    bindings = {}
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
            value = None
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
            bindings.setdefault(name, []).append((statement, value, conditional))
    # A `match` capture is the one store form that is not reachable from a
    # statement's target list, so it cannot ride along in the loop above.
    #
    # `conditional` is False, not True. A `match` clause that matches always
    # binds the captured name, and a clause that does not match leaves the
    # previous binding in force -- so from the point of view of "which store
    # runs last and retires the carried suppressor", a capture is exactly the
    # unconditional store the supersession rule is built around. Marking it
    # conditional made it merely *compete* with the earlier walrus instead of
    # superseding it, so `_resolve_bindings` still resolved the name to the
    # carried suppressor and a live assert was reported as swallowed.
    for name, statement in _match_capture_names(function).items():
        bindings.setdefault(name, []).append((statement, None, False))
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
            value = _resolve_bindings(seen, bound, orders)
            if value is not None:
                assigned.setdefault(index, {})[name] = value
    return assigned


def _resolve_bindings(entries, bound, orders):
    """Resolve one name from the bindings in effect at a single ``with``.

    ``entries`` are ``(statement, value, conditional)`` triples, already
    filtered to the stores that run at or before the ``with`` in question.
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
    if len(competing) > 1:
        # More than one conditional binding can reach this `with` on different
        # paths, so which suppressor is live is undecidable. Recorded as an
        # `AMBIGUOUS` marker rather than dropped: dropping it would fall back
        # to "this name is not a known suppressor", which reports the assert as
        # *enforced* -- the damaging direction.
        return AMBIGUOUS_SUPPRESSOR
    # Otherwise nothing competes with anything: the stores that can be last are
    # a single one, so the highest-ordered entry is what the `with` enters.
    last = max(entries, key=lambda entry: orders[id(entry[0])])[1]
    return last if _is_readable_suppressor(last, bound) else None


def _binding_order(function, statement):
    """Where ``statement`` sits among the function's top-level statements.

    This is the sort key for "which store runs last". A statement written
    directly in the body has its own index. A nested one has none, so it is
    ordered by the top-level statement that contains it -- the earliest point
    at which it can possibly have run, and the only position that is true on
    every path. Two stores inside one top-level statement would therefore
    compare equal, which is fine: they are in the same block, and if both are
    conditional the name is already ambiguous by the caller.
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
    by_index = _assigned_suppressors(function, bound)
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
                if (
                    walrus is not None
                    and isinstance(walrus.value, ast.Call)
                    and _is_suppression_call(walrus.value, bound)
                ):
                    entered.append(walrus.value)
                if isinstance(expression, ast.Name) and expression.id in live:
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
        # Bindings take effect only *after* the statement that makes them, and
        # each index is the COMPLETE set in force there rather than a delta.
        # Merging would leave a superseded alias live: a suppressor bound in an
        # `if` at index 0 followed by `cs = helper.make()` at index 1 would
        # still resolve `cs` to the suppressor at index 2, reporting a live
        # assert as swallowed.
        bound_so_far = dict(by_index.get(index, {}))
    return entered


def _encloses(header, node):
    """Is ``node`` the header itself, or somewhere inside its body?"""
    if header is node:
        return True
    return any(child is node for child in ast.walk(header))


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
                return dict(own) if seen_store or captures else bound_so_far
            if isinstance(node, ast.Assign) or (
                isinstance(node, ast.AnnAssign) and node.value is not None
            ):
                seen_store = True
    return bound_so_far


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
