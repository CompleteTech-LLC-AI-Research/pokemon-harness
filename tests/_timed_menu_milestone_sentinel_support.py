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

A third gap is that a structural check is itself only as good as the shape it
inspects. #280 showed that both sentinels above asked "is this comparison
*present*?" and answered a shape that is present but unable to fail:

    try:
        assert record["termination"] != "cancelled_or_deadline", ...
    except AssertionError:
        pass

leaves every node in place, so ``ast.walk`` still finds the comparison and
still calls the guard a rejection, while the guard returns normally. The same
holds for ``assert ... or True`` and for a statically dead ``if False:``
branch. ``_is_enforced`` and ``_may_bypass`` are the shared answer: an assert
counts only when its failure actually propagates.
"""

import ast
import builtins
import inspect

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

#: Handlers that catch ``AssertionError`` without naming it. ``Exception`` and
#: ``BaseException`` are included because they are ordinary Python and either
#: one defuses every assert it encloses.
UNIVERSAL_HANDLERS = frozenset({"BaseException", "Exception"})


def _guard_globals():
    """The namespace the #261 guard is defined in, for local-class lookup."""
    import importlib

    return vars(importlib.import_module(GUARD_MODULE))


def _exception_names(exception):
    """The names an ``except`` clause catches, or ``None`` if unrecognised.

    A bare ``except:`` has no type node and catches everything, so it resolves
    to the universal base. A tuple is flattened. Anything else -- a subscript,
    a call, a starred expression -- returns ``None`` so the caller can treat the
    clause as hostile instead of silently assuming it is harmless.
    """
    if exception is None:
        return ("BaseException",)
    if isinstance(exception, ast.Name):
        return (exception.id,)
    if isinstance(exception, ast.Attribute):
        return (ast.unparse(exception),)
    if isinstance(exception, ast.Tuple):
        names = []
        for element in exception.elts:
            resolved = _exception_names(element)
            if resolved is None:
                return None
            names.extend(resolved)
        return tuple(names)
    return None


def _name_catches_assertion_error(name):
    """Is this caught name one that also catches ``AssertionError``?

    Resolution is attempted against builtins and the defining module's
    globals, so a locally declared ``class Truncated(AssertionError)`` is
    recognised. A name that resolves to nothing is reported as swallowing: an
    assert that cannot be proven live must not be counted as enforced.
    """
    if name in UNIVERSAL_HANDLERS or name == "AssertionError":
        return True
    for namespace in (vars(builtins), _guard_globals()):
        candidate = namespace.get(name.rsplit(".", 1)[-1])
        if isinstance(candidate, type):
            return issubclass(candidate, AssertionError)
    return True


def _handler_catches_assertion_error(handler):
    """Could this handler let an ``AssertionError`` pass without propagating?"""
    names = _exception_names(handler.type)
    if names is None:
        return True
    return any(_name_catches_assertion_error(name) for name in names)


def _encloses(outer, node):
    """Is ``node`` a strict descendant of ``outer``?"""
    return any(child is node for child in ast.walk(outer) if child is not outer)


#: ``ast.Try`` and its ``try/except*`` counterpart, which catch the same way.
_TRY_NODES = (ast.Try, ast.TryStar)


def _suppressed_by(entered, name):
    """Is this statement inside a ``with`` that suppresses assertion failure?"""
    for item in entered:
        if not isinstance(item, (ast.With, ast.AsyncWith)):
            continue
        for with_item in item.items:
            call = with_item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == name
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "contextlib"
                and any(
                    _name_catches_assertion_error(name_)
                    for name_ in _exception_names_of_suppress(call)
                )
            ):
                return True
    return False


def _exception_names_of_suppress(call):
    """The exception names passed to ``contextlib.suppress(...)``."""
    names = []
    for argument in call.args:
        if isinstance(argument, ast.Name):
            names.append(argument.id)
        else:
            # Anything that is not a plain name is reported as universal, so an
            # unrecognised argument is never assumed to be harmless.
            names.append("BaseException")
    return names or ["BaseException"]


def _always_false_branch(node):
    """Is this statement under a branch the interpreter can statically skip?

    Only literal conditions are considered, so a guard nested under a genuine
    runtime condition is never mistaken for a dead one. ``if False:`` is the
    shape that unwired the #261 guard in the first place, and it is the same
    "present in the source but unreachable" defect as a swallowed assert.
    """
    return any(
        isinstance(entered, ast.If)
        and isinstance(entered.test, ast.Constant)
        and not entered.test.value
        for entered in node
    )


def _swallowing_handlers(function, target):
    """Scopes inside ``function`` that can eat ``target``'s assertion failure.

    A ``try`` body is covered by that same ``try``'s handlers, which is a
    sibling relationship rather than an ancestor one -- the shape that made an
    earlier version of this helper return a false negative. ``orelse`` is not
    covered (it runs only when nothing was raised), and one handler body is
    never covered by a sibling handler in the same chain. ``with
    contextlib.suppress(...)`` and statically dead ``if False:`` branches are
    handled here too, because both make an assert present but unfailable.
    """
    found = []

    def enclosing_names(entered):
        names = []
        for item in entered:
            if isinstance(item, ast.ExceptHandler):
                names.extend(_exception_names(item.type) or ("BaseException",))
        if _suppressed_by(entered, "suppress"):
            names.append("BaseException")
        return names

    def visit(node, entered):
        if node is target:
            if any(_name_catches_assertion_error(name) for name in enclosing_names(entered)):
                found.append(node)
            if _always_false_branch(entered):
                found.append(node)
            return True
        if isinstance(node, _TRY_NODES):
            handlers = [item for item in node.handlers if _handler_catches_assertion_error(item)]
            for child in node.body:
                if visit(child, [*entered, *handlers]):
                    return True
            for child in node.orelse + node.finalbody:
                if visit(child, entered):
                    return True
            for handler in node.handlers:
                # `elif`-style chains are sibling handlers, but they attach to
                # the same orelse. In a chain, handler N+1 sees the exceptions
                # that reached it, which excludes what handler N already caught.
                siblings = [
                    item
                    for item in node.handlers[node.handlers.index(handler) + 1 :]
                    if _handler_catches_assertion_error(item)
                ]
                for child in handler.body:
                    # Not `handler` itself: an AssertionError raised inside a
                    # handler body propagates out of the whole try, it is not
                    # re-caught by the clause that caught the first one.
                    if visit(child, [*entered, *siblings]):
                        return True
            return False
        if isinstance(node, ast.If) and isinstance(node.test, ast.Constant) and not node.test.value:
            if any(visit(child, [*entered, node]) for child in node.body):
                return True
            return any(visit(child, entered) for child in node.orelse)
        if isinstance(node, (ast.With, ast.AsyncWith)) and _suppressed_by((node,), "suppress"):
            return any(visit(child, [*entered, node]) for child in node.body)
        for child in ast.iter_child_nodes(node):
            if visit(child, entered):
                return True
        return False

    for statement in function.body:
        visit(statement, [])
    return found


def _is_enforced(function, node):
    """Can this assert actually fail, or is it present but inert?"""
    return not _swallowing_handlers(function, node)


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
        owner = _enclosing_function_def(tree, node)
        if owner is None or not _is_enforced(owner, node) or _may_bypass(node.test):
            continue
        # `assert len(calls) == 271 and calls[-1][...] == 270` is a BoolOp whose
        # operands are the individual comparisons, so compare nodes -- not just
        # the assert's own top-level test.
        for comparison in _comparisons_in(node.test):
            yield from _count_comparison(tree, node, comparison)


def _enclosing_function_def(tree, target):
    """The ``FunctionDef`` that contains ``target``, or ``None`` at module scope."""
    for func in tree.body:
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) and _encloses(func, target):
            return func
    return None


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


def _may_bypass(expression):
    """Can a comparison nested in this expression still go unchecked?

    ``assert x != y or flag`` and ``assert flag or x != y`` both parse to a
    ``BoolOp``, and both leave the comparison unchecked. That is a third shape
    of the same "present in the source but unable to fail" defect #280 is
    about, and it defeats a check that only looks for the comparison node.

    Only value positions matter. Under ``or`` any operand that decides the
    result on its own decides the whole assert, and under ``and`` the same
    holds of the operands' combined truth, so one traversal covers both. A bare
    ``Compare`` has no bypass.
    """
    if not isinstance(expression, ast.BoolOp):
        return False
    return any(
        isinstance(value, (ast.Name, ast.Attribute, ast.Call, ast.Subscript, ast.Constant))
        or _may_bypass(value)
        for value in expression.values
    )


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
        if owner is not None and not _is_enforced(owner, node):
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


def guard_rejects_the_deadline_terminal_state():
    """Does the guard still *reject* ``termination == "cancelled_or_deadline"``?

    ``guard_is_wired_on_the_fast_clock_path`` only proves the guard is *called*.
    That is not sufficient: a guard whose body asserts nothing is wired to
    nothing, and the exact-count assertions it exists to qualify would then read
    a deadline-truncated run as a retention result again -- the confusion #261
    was filed to remove. This asks for the ``!=`` comparison that does the
    rejecting, so a gutted guard body fails.

    A comparison that is merely *present* is not enough either. Wrapping the
    assert in ``try: ... except AssertionError: pass`` leaves the node in the
    tree but makes the guard return normally, which would restore the exact
    confusion above while every structural check stayed green. An assert only
    counts when no enclosing handler swallows its failure, and not when it sits
    in a branch the interpreter can statically skip (#280, #285).
    ``_is_enforced`` is what closes that.
    """
    tree = _guard_source_tree()
    for func in tree.body:
        if not (isinstance(func, ast.FunctionDef) and func.name == GUARD_FUNCTION):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Assert):
                continue
            if not _is_enforced(func, node) or _may_bypass(node.test):
                continue
            for comparison in _comparisons_in(node.test):
                if not (len(comparison.ops) == 1 and isinstance(comparison.ops[0], ast.NotEq)):
                    continue
                right = comparison.comparators[0]
                if (
                    _is_termination_key(comparison.left)
                    and isinstance(right, ast.Constant)
                    and right.value == DEADLINE_TERMINATION
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
    """
    tree = _module_tree()
    offenders = []
    for func_name in RETENTION_COUNT_SITES:
        for func in tree.body:
            if not (isinstance(func, ast.FunctionDef) and func.name == func_name):
                continue
            for node in ast.walk(func):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == RUN_OWNER
                ):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "clock_step":
                        continue
                    guarded = (
                        isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == GUARDED_CLOCK_STEP
                    )
                    if not guarded:
                        offenders.append((func_name, ast.unparse(keyword.value)))
    return offenders
