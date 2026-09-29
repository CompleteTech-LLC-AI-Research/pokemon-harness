"""Sentinel support, part 5 of 5 (#122 split).

Functions: _is_tautology, _is_literal_true, _may_bypass, _is_enforced, _contains, _is_after_control_transfer, _statement_lists_holding, _is_terminated_before, _is_bare_transfer, guard_rejects_the_deadline_terminal_state, count_sites_that_bypass_the_guard, _bypassing_sites, clock_step_overrides, _unpacked_keywords
"""

# ruff: noqa: F821
#
# This file is a *fragment* of
# tests/_timed_menu_milestone_sentinel_support, not a module of its own. The
# support module reads its source with `exec` into one shared dict, so that every
# helper stays a bare global -- which is required twice over: the sentinel suite
# asserts one helper *calls* another by parsing the caller's source and matching
# `ast.Name` (`_sentinel_uses`), and callers rebind names on the support module
# itself (`support._module_tree = ...`), which only reaches the caller if there
# is a single globals dict rather than one private copy per file.
#
# A cross-fragment reference is therefore resolved at exec time, not import
# time, and static analysis cannot see it: the names below are defined by
# another fragment. Importing them explicitly would not help -- it would
# reintroduce a second namespace, which is the exact failure this split has to
# avoid -- so the undefined-name rule is disabled for these fragments.

# Constants and shared imports come from the support namespace this file
# is executed into: `tests._timed_menu_milestone_sentinel_support` execs
# the base and then every part, all against one dict, so `ast`, `inspect`,
# `milestones` and the module constants are already globals by the time
# anything below runs. Re-importing them here would only rebind the same
# objects, and importing this file as a module of its own would create a
# second, private namespace -- so these fragments are not importable alone.


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
            if _is_suppressing_with(ancestor, bound, function):
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

# ---------------------------------------------------------------------------
# Fragment guard. This file is not an importable module: it is one piece of
# tests/_timed_menu_milestone_sentinel_support, which `exec`s it, together with
# the other fragments, into a single shared namespace.
#
# That sharing is what makes the sentinels work, and it cannot survive being
# bypassed. Executed through the entry point, `__name__` is the support module's
# name and this guard is inert. Executed under its OWN name -- which is what
# `import tests._sentinel_support_part5` does, and what the import system does by itself -- the
# file would bind only the names it defines itself: a cross-fragment call would
# raise NameError, and a name imported from here would be a different object
# from the one the support module exports. Worse, that breakage is
# order-dependent and silent, which is a poor property for a module whose entire
# purpose is catching silent structural faults.
#
# Fail loudly instead, and name the supported import.
# ---------------------------------------------------------------------------
if __name__ == "tests._sentinel_support_part5":
    raise ImportError(
        "tests._sentinel_support_part5 is a fragment of "
        "tests._timed_menu_milestone_sentinel_support, not an importable "
        "module. Import the support module instead:\n"
        "    from tests import _timed_menu_milestone_sentinel_support as "
        "support\n"
        "Importing this fragment directly gives it a private copy of the shared "
        "namespace: cross-fragment calls raise NameError, and rebinding a name "
        "on the support module would not reach this code."
    )

