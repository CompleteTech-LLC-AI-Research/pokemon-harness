# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part10":
    raise ImportError(
        "tests._sentinel_support_part10 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


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
            if _is_suppressing_with(ancestor, bound, function, owning, target):
                return False
            if _is_user_defined_swallowing_with(ancestor, bound, function, owning):
                return False
            if function is not None and _entered_name_is_dead(ancestor, function, bound, owning):
                return False
            if _header_expression_raises(ancestor, function, owning):
                return False
            if function is not None and _scope_above_header_raises(function, ancestor):
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
            and _loop_iterable_is_provably_empty(ancestor.iter, function)
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


def _elif_link_is_reached(block, function):
    """Is this ``if`` link's body entered on every call, or is it an ``elif``?

    Returns ``True`` for a link that is not part of an ``orelse`` chain: a
    standalone ``if True:`` and the opening link of a chain are both reached
    whenever their test holds, and here the test does hold.

    For a link reached *through* an ``orelse`` -- the ``elif`` shape -- the
    body runs only when every test above it failed, so each of those has to be
    never-true for the link to be entered on every call. One test that can
    hold ends the walk and this returns ``False``, leaving the store
    conditional.
    """
    return _link_is_always_entered(block, function)


def _is_always_true_arm(block, statement, function):
    """Is ``statement`` in an ``else``/``elif`` arm of ``block`` that always runs?

    #403. :func:`_is_always_true_branch` only recognised the **body** of an
    always-true ``if``, so the same rule went unread in the arm that runs
    *because* the test failed:

        from contextlib import nullcontext
        cs = nullcontext()
        if False:
            pass
        elif True:
            cs = list()             # this arm always runs
        def outer(x):
            with cs:               # CPython: `with list():` raises TypeError
                assert x != 1     # unreachable

    The store is written as a conditional one, so it settled nothing and the
    name was left to the ``nullcontext`` above -- reporting a header CPython
    cannot enter as a live one, the damaging direction. The plain ``else:``
    spelling of the same thing was wrong for the same reason:

        if False:
            pass
        else:
            cs = list()

    The arm runs exactly when the test is false **and** every earlier test in
    the chain also failed, so both have to be read: an ``elif`` behind a
    never-true ``if`` always runs, while the same arm behind an always-true
    ``if`` never does. That second case is already handled, and handled
    correctly, by :func:`_statement_in_unreachable_arm` -- which is why this
    helper is consulted alongside it rather than instead of it, and why
    :func:`_statement_always_runs` still subtracts the unreachable-arm answer
    at the end.

    An ``elif`` is an ``else`` whose body is another ``if``, so the chain is
    walked by following ``orelse`` while it holds a single ``ast.If``. A
    trailing plain ``else`` settles the same way once the chain is exhausted.

    This says nothing about a store written *directly* in a loop body, which
    may not run at all; that boundary is #403's AC4 and is preserved by
    leaving :func:`_block_never_runs` the only loop-side test.
    """
    if not isinstance(block, ast.If):
        return False
    # An `if False:` / `elif True:` chain runs the `elif` body on every call,
    # while the `if` link's own body never runs. The *outer* link is claimed
    # here as well as the arm itself, because the per-block `all(...)` in
    # :func:`_statement_always_runs` would otherwise see a never-running block
    # on the path to the store and reject it. That is the right answer for that
    # question: this arm settles the name on every call.
    #
    # The whole rest of the chain still has to be read before claiming it, and
    # `_always_true_arm_within` is what reads it -- an early return here on the
    # strength of this link's test alone is the bug that had the conditional
    # chain below settled on its opening `if False:`.
    if not _condition_is_never_true(block.test, function):
        return False
    return _always_true_arm_within(block, statement, function)


def _always_true_arm_within(block, statement, function):
    """Does an always-run ``orelse`` arm of ``block`` hold ``statement``?

    #403. The chain walk behind :func:`_is_always_true_arm`. ``block``'s own
    # test is already known to be never-true by the caller, so this starts at
    its ``orelse`` and follows the chain downwards:

    * an ``elif`` link is reached only when its own test is also never-true,
      and the claim is made at the first link whose body holds the statement;
      a link whose test is *always*-true settles the name as surely as an
      unconditional store, so it is claimed there too;
    * a trailing plain ``else`` runs when the whole chain above it failed, so
      it settles the name as well;
    * a link whose test can hold ends the walk with ``False`` -- the arm beyond
      it runs on some calls and not others, which is the undecidable case and
      must leave the store conditional.

    Reading every link is what keeps

        if False:
            pass
        elif flag:
            pass
        elif True:
            cs = list()

    from being settled on the strength of the opening ``if False:`` alone. That
    arm is reached only when ``flag`` is false, so the store settles the name
    only sometimes and the header is undecidable.
    """
    arm = block.orelse
    while len(arm) == 1 and isinstance(arm[0], ast.If):
        # An `elif` is reached only when the tests above it failed, so an
        # always-true test here both runs its body on every call *and* is
        # itself the settling store. `_is_always_true_branch` is asked first so
        # the body case is settled by the body rule, keeping the two questions
        # in one place.
        test = arm[0].test
        if _contains_any(arm[0].body, statement):
            if _condition_is_always_true(test, function):
                return _is_always_true_branch(arm[0], statement, function)
            return _condition_is_never_true(test, function)
        if not _condition_is_never_true(test, function):
            return False
        arm = arm[0].orelse
    # A trailing `else` runs when the whole chain above it failed.
    return _contains_any(arm, statement)


def _link_is_always_entered(block, function):
    """Is this ``if`` node's body entered on every call?

    Walks the ``orelse`` chain containing ``block`` from the outermost link
    inward, which is the only direction that can answer the question: a link
    deeper in the chain is reached only when every test above it failed.

    A node that is not inside any chain is a standalone ``if`` or the opening
    link of one, and is entered whenever its own test holds -- the caller's
    question is about the test, so the answer here is ``True`` and the test
    itself is judged by the caller.
    """
    outer_link = _outermost_chain_link(block, function)
    if outer_link is None:
        return True
    link = outer_link
    while True:
        if link is block:
            return True
        if not isinstance(link, ast.If) or len(link.orelse) != 1:
            return False
        nxt = link.orelse[0]
        if not isinstance(nxt, ast.If):
            # A trailing plain `else`. `block` is not in this arm, so it is
            # never reached through this chain.
            return False
        # `nxt` is an `elif`: it is entered only when `link`'s test failed, so
        # `link` must be never-true for the walk to continue inward.
        if not _condition_is_never_true(link.test, function):
            return False
        link = nxt


def _outermost_chain_link(block, function):
    """The opening ``if`` of the ``orelse`` chain containing ``block``.

    ``None`` when ``block`` is not inside such a chain. Only a genuine
    ``elif`` -- a node that is the single element of some ``if``'s
    ``orelse`` -- has a parent link, and the search is by identity through
    the function so an unrelated nested ``if`` cannot be mistaken for one.
    """
    if function is None:
        return None
    for node in ast.walk(function):
        if not isinstance(node, ast.If) or len(node.orelse) != 1:
            continue
        if node.orelse[0] is block:
            return node
    return None


def _elif_skipped_path_carries_defeat(entries, orders, competitor, bound, function, query, owning):
    """Prove the value carried across a skipped arm is the one the ``with`` enters.

    ``#445``. ``#441``/``#452`` answer *live* by showing the failure value skips
    the arm and enters a plain manager carried in from before the chain. This is
    the same walk with the opposite question: does the skipped path enter a
    value that *swallows*? The arm is correctly demoted either way, because an
    ``elif`` runs only when every test above it failed, so it is never the
    binding on the failing call. What differs is the value the failing call
    enters -- the carried one -- and that is the value that decides.

    The gate is what makes the answer symmetric. The carried value must be
    *readable*, which the ``_elif_witness_reaches_header`` scan already
    restricts to ``nullcontext()`` and a named suppression. Reading it here
    through :func:`_entry_suppresses_assertion_errors` is what distinguishes the
    two polarities without widening the witness:

        with (cs := contextlib.suppress(AssertionError)):   # swallows
            pass
        if x: pass
        elif True: cs = contextlib.nullcontext()           # skipped at x=1
        with cs: assert x != 1                             # DEAD -- x=1 enters
                                                          # the carried suppress

    An unreadable carried value answers ``False`` here, which keeps the assert
    live. That is deliberate and it is the safe direction: a factory call the
    reader cannot resolve is not evidence that the assert is defeated.
    """
    return _elif_skipped_path_walks(
        entries, orders, competitor, bound, function, query, owning, carried_suppresses=True
    )


def _carried_suppression_value(entries, orders, competitor, bound):
    """The value in force at the header on the calls that skip the arm.

    The caller has already proven, through
    :func:`_elif_skipped_path_carries_defeat`, that this value swallows
    ``AssertionError``. This returns the *value* rather than a marker so the
    header resolves the way it would have had the arm not been written at all,
    which is exactly what CPython does: the arm's assignment never runs on the
    failing call, so the name still carries the earlier binding there.

    ``None`` is never returned for a proven case -- the witness refuses unless
    the carried entry is a readable suppression call, and that same entry is
    what is returned here. The lookup is by the same identity, order and
    conditionality the witness used, so the two cannot disagree.
    """
    prior = [entry for entry in entries if orders[id(entry[0])] < orders[id(competitor[0])]]
    if not prior:
        return _NOT_A_SUPPRESSOR
    latest_order = max(orders[id(entry[0])] for entry in prior)
    latest = [entry for entry in prior if orders[id(entry[0])] == latest_order]
    if len(latest) != 1 or latest[0][2]:
        return _NOT_A_SUPPRESSOR
    return latest[0][1]


def _carried_value_is_plain_manager(manager, bound):
    """Is the carried value a *readable* manager that lets ``AssertionError`` out?

    #452 originally spelled this as "is it exactly ``contextlib.nullcontext()``".
    # That is narrower than the question the walk is asking, and the gap shows
    # in both polarities. A carried ``suppress(ValueError)`` is just as readable
    and just as enterable as a ``nullcontext()``, and it does not catch
    ``AssertionError`` -- so a call that skips the suppressing arm really does
    let the failure escape, and the assert is live. Restricting the carried
    value to the one spelling reported that live header as DEFEATED.

    The question asked here is deliberately narrow -- *readable*, and provably
    not catching ``AssertionError``. An unreadable call answers False, which
    leaves the resolution untouched, because a factory the reader cannot follow
    is not evidence either way.
    """
    if bound is None or not isinstance(manager, ast.Call) or manager.keywords:
        return False
    if _resolves_to(manager.func, "contextlib.nullcontext", bound) and not manager.args:
        return True
    if not _is_readable_suppressor(manager, bound):
        return False
    return not _entry_suppresses_assertion_errors((None, manager, False), bound)


def _elif_skipped_path_can_fail(entries, orders, competitor, bound, function, query, owning=None):
    """Prove the filed literal-comparison failure reaches a skipped elif arm.

    A non-suppressor is not necessarily enterable. A live classification also
    needs a failure value that takes an earlier arm, rather than the suppressing
    one. Unknown managers, predicates, local rebinding and nested control flow
    retain the existing resolution.
    """
    return _elif_skipped_path_walks(
        entries, orders, competitor, bound, function, query, owning, carried_suppresses=False
    )


def _elif_skipped_path_walks(
    entries, orders, competitor, bound, function, query, owning, carried_suppresses
):
    """Prove the filed literal-comparison failure reaches a skipped arm.

    A non-suppressor is not necessarily enterable. A live classification also
    needs a failure value that takes an earlier arm, rather than the suppressing
    one. Unknown managers, predicates, local rebinding and nested control flow
    retain the existing resolution.

    #452. The walk below reaches the suppressor through the ``orelse`` chain
    and stops when it meets an arm the failure value selects. That covers a
    terminal ``else`` as well as an ``elif``: in both, the suppressor is not
    installed on the failure path, so the manager carried in from before the
    chain is what the ``with`` actually enters. A decided ``if False:`` whose
    ``else`` always runs falls out of the walk with ``False``, which keeps it
    DEFEATED.

    #451 asks a neighbouring question of a loop ``else``, and the answer is the
    # same: the suppressor is not installed on the path that skips its arm, so
    # the manager carried in from before is what the ``with`` actually enters.
    # The difference is *which* path skips the arm -- an earlier test succeeding
    # here, a ``break`` firing there -- so the loop's witness is a separate walk
    # in :func:`_break_skipped_loop_else_can_fail`, dispatched from the same call
    # site so the two guards cannot drift apart.
    """
    if not isinstance(query, ast.Assert) or function is None:
        return False
    prior = [entry for entry in entries if orders[id(entry[0])] < orders[id(competitor[0])]]
    if not prior:
        return False
    latest_order = max(orders[id(entry[0])] for entry in prior)
    latest = [entry for entry in prior if orders[id(entry[0])] == latest_order]
    if len(latest) != 1 or latest[0][2]:
        return False
    manager = latest[0][1]
    if carried_suppresses:
        # #445. The carried value decides the skipped path, so ask what it
        # *is* rather than which spelling it has. #452's witness could hard-code
        # `contextlib.nullcontext()` because it was only ever asking "is the
        # carried value enterable?"; the same question is asked here, and the
        # polarity of the answer -- suppresses or does not -- is decided by the
        # caller. A carried `suppress(AssertionError)` is exactly as readable as
        # a carried `nullcontext()` and just as enterable, and requiring the
        # nullcontext spelling is what made this case read live while the assert
        # really is swallowed.
        if not _entry_suppresses_assertion_errors(latest[0], bound):
            return False
    elif not _carried_value_is_plain_manager(manager, bound):
        return False
    root_name = manager.func
    while isinstance(root_name, ast.Attribute):
        root_name = root_name.value
    if not isinstance(root_name, ast.Name):
        return False
    if any(
        argument.arg == root_name.id
        for argument in ast.walk(function.args)
        if isinstance(argument, ast.arg)
    ):
        return False
    if any(
        root_name.id in _store_target_names_of(statement)
        for statement in _scope_body_nodes(function)
    ):
        return False
    if not isinstance(owning, ast.Module):
        return False
    own_imports = _bound_names(ast.Module(body=[], type_ignores=[]), function)
    if root_name.id not in own_imports:
        for statement in _scope_body_nodes(owning):
            names = _store_target_names_of(statement)
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [statement.name]
            if root_name.id in names:
                return False
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                if any(
                    isinstance(target, (ast.Attribute, ast.Subscript))
                    and any(
                        isinstance(child, ast.Name) and child.id == root_name.id
                        for child in ast.walk(target)
                    )
                    for target in targets
                ):
                    return False
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                return False
    failure = query.test
    values = None
    if (
        isinstance(failure, ast.Compare)
        and len(failure.ops) == 1
        and isinstance(failure.ops[0], ast.NotEq)
    ):
        left, right = failure.left, failure.comparators[0]
        if isinstance(left, ast.Constant) and isinstance(right, ast.Name):
            left, right = right, left
        if isinstance(left, ast.Name) and isinstance(right, ast.Constant):
            values = {left.id: right.value}
    if values is None:
        return False
    parameters = {
        argument.arg
        for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    }
    if not set(values) <= parameters:
        return False
    for statement in _scope_body_nodes(function):
        names = _store_target_names_of(statement)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [statement.name]
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            names = [alias.asname or alias.name.split(".")[0] for alias in statement.names]
        elif isinstance(statement, ast.ExceptHandler) and statement.name is not None:
            names = [statement.name]
        if set(values) & set(names):
            return False
    chain = next(
        (
            node
            for node in function.body
            if isinstance(node, ast.If) and _contains(node, competitor[0])
        ),
        None,
    )
    if chain is None or not _elif_witness_reaches_header(function, chain, query, bound):
        return False
    # #452. A trailing `else` reached through an `elif` link is a weaker
    # question than the one this walk below answers, and the walk below cannot
    # reach it.
    #
    # The walk below needs the *failure value* to decide every link above the
    # arm: for an `elif` link that is the only way to show a call both fails the
    # assert and lands on an arm that does not install the suppressor. A
    # trailing `else` does not need that. It merely has to be *skippable* --
    # some call has to take one of the links above it, leaving the preamble's
    # manager in force. So a link whose test `assert x != 1` says nothing about
    # blocks the walk below (decision is None) but not this one: such a link may
    # simply be the link that is true.
    #
    #     cs = contextlib.nullcontext()
    #     if False:
    #         pass
    #     elif flag:          # nothing in the assert constrains `flag`
    #         pass
    #     else:
    #         cs = contextlib.suppress(AssertionError)
    #     with cs:
    #         assert x != 1
    #
    # At `x == 1, flag == True` the leading link is decided-false and `flag` is
    # true, so *no* arm runs, `cs` is still the `nullcontext`, and the assert
    # fires. The header is live, and the analyzer reported defeated.
    #
    # Soundness, all of which the controls pin: a link that no call can make
    # true does not skip anything, so `if False: ... else: <suppressor>` keeps
    # its existing answer; and no arm above may rebind the name, because then a
    # skipping call would not enter the carried manager. That last one is the
    # both-arms-bind shape, which the caller already resolves on its own.
    #
    # The helper declines when there is no link above the `else`, so the
    # single-`if` form -- which the walk below already decides from the failure
    # value -- keeps that walk's answer and is not re-litigated here.
    if _trailing_else_is_skippable(function, chain, competitor):
        return True
    while isinstance(chain, ast.If):
        if _contains_any(chain.body, competitor[0]):
            return False
        decision = _elif_failure_predicate(chain.test, values)
        if decision is True:
            # A pass-only preceding arm preserves the known manager and failure
            # value. Arbitrary statements cannot establish this witness.
            return bool(chain.body) and all(isinstance(node, ast.Pass) for node in chain.body)
        if (
            decision is not False
            or len(chain.orelse) != 1
            or not isinstance(chain.orelse[0], ast.If)
        ):
            return False
        chain = chain.orelse[0]
    return False


def _break_skipped_loop_else_can_fail(
    entries, orders, competitor, bound, function, query, owning=None
):
    """Prove the assert still fails when a ``break`` skips a loop ``else``.

    #451. The filed shape is

        cs = contextlib.nullcontext()
        for item in (1,):
            break                       # the else never runs
        else:
            cs = contextlib.suppress(AssertionError)
        with cs:                       # `cs` is still the nullcontext
            assert x != 1

    The suppressor is in the loop's ``else``, and the ``break`` means it never
    gets installed, so the header really enters the carried ``nullcontext`` and
    the assert fires. The competitor is unconditional, so
    :func:`_resolve_bindings` resolves the name to the suppressor and the
    assert comes back defeated.

    The witness is *correlated*, not symmetric with the elif one. #441 proves
    that some call's **failure value** selects an arm that skips the
    suppressor; #451 has to prove that there is a call which both

      * reaches the ``break``, so the ``else`` never runs, and
      * still fails the assert.

    Note what this does *not* require. With ``if x: break`` against the filed
    ``assert x != 1`` the break's guard and the assert's condition are the
    same predicate, which looks like the two are exclusive -- but they are not,
    because each call needs only one of them. The ``else`` binds the calls
    where the guard is false, and the assert fails on the calls where it is
    true, so the suppressor is in force on precisely the calls that cannot
    fail and the assert fires on the rest. The exclusive shape is the
    opposite one, ``if x == 0: break``: the guard is false at the failing
    value, the failing call takes the ``else``, and the assert really is
    swallowed. Answering live there would be a new false-LIVE, so the walk
    below requires the failing value to be able to reach the break.

    A ``break`` that is not under any test at all (``for ...: break``) is
    reached on every call, so the failure value is unconstrained and the
    witness is immediate.
    """
    if not isinstance(query, ast.Assert) or function is None:
        return False
    # The same plain `contextlib.nullcontext()` carrier the elif witness
    # requires. A non-suppressor is not necessarily enterable, so seeing one on
    # some path is not on its own enough to call the header live.
    prior = [entry for entry in entries if orders[id(entry[0])] < orders[id(competitor[0])]]
    if not prior:
        return False
    latest_order = max(orders[id(entry[0])] for entry in prior)
    latest = [entry for entry in prior if orders[id(entry[0])] == latest_order]
    if len(latest) != 1 or latest[0][2]:
        return False
    manager = latest[0][1]
    if not (
        isinstance(manager, ast.Call)
        and not manager.args
        and not manager.keywords
        and _resolves_to(manager.func, "contextlib.nullcontext", bound)
    ):
        return False
    root_name = manager.func
    while isinstance(root_name, ast.Attribute):
        root_name = root_name.value
    if not isinstance(root_name, ast.Name):
        return False
    if any(
        argument.arg == root_name.id
        for argument in ast.walk(function.args)
        if isinstance(argument, ast.arg)
    ):
        return False
    if any(
        root_name.id in _store_target_names_of(statement)
        for statement in _scope_body_nodes(function)
    ):
        return False
    if not isinstance(owning, ast.Module):
        return False
    # The failure value, read exactly as `_elif_skipped_path_can_fail` reads
    # it: a single `!=` against a literal pins `x` to the value that makes the
    # assert fail, and `values` maps the parameter name to that value.
    failure = query.test
    values = None
    if (
        isinstance(failure, ast.Compare)
        and len(failure.ops) == 1
        and isinstance(failure.ops[0], ast.NotEq)
    ):
        left, right = failure.left, failure.comparators[0]
        if isinstance(left, ast.Constant) and isinstance(right, ast.Name):
            left, right = right, left
        if isinstance(left, ast.Name) and isinstance(right, ast.Constant):
            values = {left.id: right.value}
    if values is None:
        return False
    parameters = {
        argument.arg
        for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    }
    if not set(values) <= parameters:
        return False
    # A store to the failure parameter anywhere in the scope means its value at
    # the header is not the value the assert was written against, so the
    # correlation proved below would be about the wrong call.
    for statement in _scope_body_nodes(function):
        names = _store_target_names_of(statement)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [statement.name]
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            names = [alias.asname or alias.name.split(".")[0] for alias in statement.names]
        elif isinstance(statement, ast.ExceptHandler) and statement.name is not None:
            names = [statement.name]
        if set(values) & set(names):
            return False
    loop = next(
        (
            node
            for node in function.body
            if isinstance(node, (ast.For, ast.While)) and _contains(node, competitor[0])
        ),
        None,
    )
    if loop is None or not loop.orelse or not _contains_any(loop.orelse, competitor[0]):
        return False
    if not _elif_witness_reaches_header(function, loop, query, bound):
        return False
    return _break_reachable_with_failure(loop, values, function)


def _break_reachable_with_failure(loop, values, function):
    """Can a call at the failing parameter value also reach a ``break``?

    #451. Walks the loop body for a ``break`` and asks what has to be true for
    control to arrive at it. An **unconditional** ``break`` -- one not nested
    inside a test this can read -- is reached on every iteration, so it is
    reached at the failing value too and the witness holds.

    A ``break`` guarded by a condition this *can* read is different: the
    condition has to hold at the failing value as well. ``if x == 0: break``
    against ``assert x != 1`` decides ``False`` there, the ``if`` body is
    skipped, the loop completes normally, the ``else`` runs, and the assert
    really is swallowed on that call. Those rows are declined, which is the
    safe direction.

    The mirror is deliberately *not* declined: ``if x: break`` also holds at
    the failing value, so the break is reachable there and the header is live.
    The two guards are mirror images and the loop distinguishes them, which is
    why the table pins both.

    A condition this cannot read is treated as possibly-true, so a break
    under it is still reachable. That is the conservative direction and matches
    the rest of this module: an unreadable test may hold, and the analyzer must
    not assume the ``else`` ran.
    """
    for node in ast.walk(loop):
        if not isinstance(node, ast.Break):
            continue
        test = _enclosing_conditions(node, loop)
        if all(_condition_can_hold(condition, values) for condition in test):
            return True
    return False


def _condition_can_hold(condition, values):
    """Can this guard be true at ``values`` -- or at least not provably false?

    Two outcomes, and the distinction between them is the whole point:

    * ``False`` -- the guard is decided false, or deciding it would raise. In
      the second case the loop cannot complete normally at all, so the guarded
      ``break`` is unreachable *and* the ``else`` cannot be installed. Either
      way the witness must fail.
    * ``True`` -- decided true, or not decidable, so the break may be reached
      and the witness stands.

    A condition this cannot read is deliberately still "possibly reachable":
    there the ``else`` genuinely may or may not run, and assuming it ran would
    be the unsafe direction. A condition that *raises* is different -- the
    loop cannot complete, so there is nothing left to assume. The two used to
    share a single ``None``, and only one of them is safe to treat as
    "possibly reachable".
    """
    if _condition_raises(condition, values):
        return False
    return _elif_failure_predicate(condition, values) is not False


def _condition_raises(test, values):
    """Would evaluating ``test`` at ``values`` raise, rather than answer?

    Only the comparisons the predicate resolves to concrete operands can raise,
    and only for the four ordering operators: ``==`` and ``!=`` are defined for
    every pair of Python objects, ``1 == "a"`` is ``False`` rather than an
    error. This mirrors the ``try``/``except TypeError`` that
    ``_elif_failure_predicate`` uses to decline the same pairs, so the two
    never disagree about which operands are unorderable.
    """
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return False
    operator = type(test.ops[0])
    if operator not in (ast.Lt, ast.LtE, ast.Gt, ast.GtE):
        return False
    resolved = []
    for operand in (test.left, test.comparators[0]):
        if isinstance(operand, ast.Constant):
            resolved.append(operand.value)
        elif isinstance(operand, ast.Name) and operand.id in values:
            resolved.append(values[operand.id])
        else:
            return False
    left, right = resolved
    evaluates = {
        ast.Lt: lambda: left < right,
        ast.LtE: lambda: left <= right,
        ast.Gt: lambda: left > right,
        ast.GtE: lambda: left >= right,
    }
    try:
        evaluates[operator]()
    except TypeError:
        return True
    return False


def _enclosing_conditions(node, loop):
    """The tests guarding ``node``, outermost first, up to ``loop``'s body.

    A ``break`` runs only when every test between it and the loop body is
    satisfied, so all of them have to be evaluated at the failing value. A
    decision of ``False`` for any one of them is what makes the break
    unreachable on that call, and the caller treats that as a decline.

    ``ast.walk`` does not give the ancestor chain, so it is rebuilt from
    ``loop`` outward through the blocks that contain ``node``.
    """
    conditions = []
    current = node
    while current is not None and current is not loop:
        for block in ast.walk(loop):
            if isinstance(block, (ast.If, ast.While)) and _contains_any(block.body, current):
                conditions.append(block.test)
                current = block
                break
        else:
            break
    conditions.reverse()
    return conditions


def _bindings_after_import(node, bound):
    """The binding table as it stands once ``node`` itself has run.

    The question `_import_only_binds_the_resolved_root` asks is about *this
    statement*: "does the name I bind coincide with a name the witness walks,
    and is it the real module?" Answering that from the caller's merged table
    answers a different question -- what is bound at the *end* of the scope --
    and the two disagree whenever a later import rebinds the same name.

    In

        from json import loads as contextlib
        import contextlib

    the merged table says `contextlib` is the real module, because the second
    import wins. Read that way the first statement looks transparent, and
    admitting it is a false-LIVE: on the first call `contextlib` really is
    `json.loads`, and the walk's `contextlib.suppress` is `json.loads.suppress`.
    Neither import is safe alone, so the correct answer for this statement is
    "no, it rebinds a walked root".

    Overlaying this statement's own aliases on the caller's table is what
    restores the per-statement question without making the caller rebuild a
    scope: later bindings stay visible (they are not this statement's
    business), and the statement under test always sees its own effect.
    """
    own = dict(bound)
    if isinstance(node, ast.Import):
        for alias in node.names:
            own[alias.asname or alias.name] = alias.name
    elif isinstance(node, ast.ImportFrom) and node.module:
        for alias in node.names:
            own[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return own


def _try_only_binds_names_the_witness_ignores(node, function, bound):
    """Is this a ``try``/``except`` that cannot change what the header enters?

    #485. A ``try`` placed above the loop-``else`` witness used to make the
    witness decline, which reported a firing assert as unenforced. The scan
    admits only ``Pass``, an allowed ``Assign``, and a transparent import, and
    an ``ast.Try`` is none of those.

    A ``try`` is not an effect the walk has to model *provided* it binds
    nothing and cannot exit the function before the header. Two things can
    bind inside it:

    * ``except <E> as name`` binds ``name`` until the handler ends, and
    * an assignment, ``for``, ``with``, or walrus in the body or a handler
      binds its targets.

    Rather than decide which of those names are safe to rebind -- which would
    mean resolving them against the carried manager *and* every module root
    the ``contextlib.*`` calls are spelled through -- this declines if the
    ``try`` binds anything at all. The filed fixture binds nothing, so it is
    admitted; every binding shape keeps the pre-#485 decline. A decline
    reports the assert as defeated, which is the false-DEAD direction, but
    never a false-LIVE.

    The body may also raise past its handlers, or transfer control away, and
    either would let the function exit before the header. Rather than reason
    about which exceptions escape which handlers, this admits only a body that
    is itself a shape already known to be transparent: a ``Pass``, an import
    of the resolved root, or a nested admitted ``try``. That covers the filed
    fixture -- whose body is the import -- while every other body keeps the
    previous, safe decline.
    """
    if not isinstance(node, ast.Try) and not isinstance(node, ast.TryStar):
        return False

    # -- What this statement could possibly bind. `except E as name` binds
    #    `name`; an assignment, `for`, `with`, or walrus in the body or a
    #    handler binds its targets; `del` unbinds. Any of these could hand the
    #    header a different object than the one the witness proved, so a `try`
    #    that binds *anything* declines. The filed fixture binds nothing --
    #    its body is a bare import and its handler is a bare `pass` -- which is
    #    exactly the shape that is safe to admit, and refusing everything else
    #    is the pre-#485 behaviour, which cannot produce a false-LIVE.
    bound_names = set()
    for statement in [node.body, node.orelse, node.finalbody]:
        for child in statement:
            bound_names.update(_store_target_names_of(child))
            for sub in ast.walk(child):
                if isinstance(sub, ast.ExceptHandler) and sub.name is not None:
                    bound_names.add(sub.name)
    if bound_names:
        return False

    # -- The body must not be able to exit the function before the header.
    return not _try_body_can_exit_or_raise(node, function, bound)


def _try_body_can_exit_or_raise(node, function, bound):
    """Can this ``try`` body raise past its handlers, or transfer control away?

    Only shapes already transparent to the pre-chain scan are admitted, so the
    filed fixture -- whose body is an import -- passes without this having to
    prove anything about exception flow. Everything else declines, which is
    the pre-#485 behaviour and cannot introduce a false-LIVE.
    """
    for child in node.body:
        if isinstance(child, ast.Pass):
            continue
        if _import_only_binds_the_resolved_root(child, function, bound):
            continue
        if not _try_only_binds_names_the_witness_ignores(child, function, bound):
            return True
    return False


def _import_only_binds_the_resolved_root(node, function, bound):
    """Is this an import that leaves every name the witness reads untouched?

    #451. The filed fixture imports `contextlib` inside the function:

        def outer(x):
            import contextlib
            cs = contextlib.nullcontext()
            for item in (1,):
                break
            else:
                cs = contextlib.suppress(AssertionError)
            with cs:
                assert x != 1

    An import is transparent exactly when it cannot change what the header
    enters, and that is one question with one answer for every spelling: does
    the name this statement binds coincide with a name the witness resolves
    `contextlib.nullcontext` / `contextlib.suppress` through?  If no, the
    statement rebinds something this scope never reads and is inert.  If yes,
    it must resolve to the real `contextlib`, or it has genuinely rebound the
    walked root and the witness would be reasoning about a different object.
    That covers `import a`, `import a.b`, `import a as b`, `from a import b`,
    and `from a import b as c` alike, because the answer comes from the binding
    table rather than from the spelling.

    That rule is what #475 added, because refusing every non-canonical
    spelling was wrong in the damaging direction.  Each of these binds a name
    the fixture does not read, yet all of them really do fire the assert:

    * ``import contextlib as cb`` with ``cb.nullcontext()`` -- `cb` *is* the
      root here, and it resolves to `contextlib`;
    * ``import os.path`` or ``import json`` beside the canonical import;
    * ``import json as cl`` beside the canonical import;
    * ``from json import loads`` (or ``... as cl``) beside the canonical
      import, which binds a leaf of an unrelated module.

    Declining them certified a live assert dead, which is the same false-DEAD
    the residue below describes, one import spelling further out.

    What is still refused is any spelling that shadows a walked root with
    something other than the real `contextlib`:

    * ``import fake as contextlib`` -- `contextlib` resolves to `fake`, and
      admitting it would certify a genuinely swallowed assert enforced;
    * ``import contextlib.nullcontext`` binds the single name `contextlib` as
      a *package*, shadowing the attribute path the witness walks;
    * ``from contextlib import nullcontext`` binds the leaf, not the root, so
      it is only admissible when nothing here reads that name;
    * any binding of a name that is later stored to, or that shadows the root,
      is refused by the checks below;
    * a relative import (``level > 0``) resolves against a package this witness
      knows nothing about.

    Every one of those answers is read off the AST and the binding table rather
    than from a reading of what the import "means", so the admitted and refused
    sets cannot drift apart the way a hand-enumerated list did.
    """
    if not isinstance(node, (ast.Import, ast.ImportFrom)):
        return False
    if isinstance(node, ast.ImportFrom) and node.level:
        # A relative import resolves against a package this witness knows
        # nothing about, so the name it binds cannot be resolved to the real
        # `contextlib` by anything available here.
        return False
    if not node.names:
        return False
    own = _bindings_after_import(node, bound)
    if isinstance(node, ast.ImportFrom):
        # #475. `from a import b` binds `b`, and resolves to `a.b`.  That is
        # the same collision test as every other spelling, read off the binding
        # table: either the bound name is one the witness never reads, so the
        # statement is inert -- `from json import loads as cl` beside the
        # canonical import, which CPython shows still fires -- or it is one it
        # walks, in which case it has to be the real thing.
        for alias in node.names:
            bound_name = alias.asname or alias.name
            if "." in bound_name:
                return False
            if bound_name in _witness_module_roots(function, bound) and not _resolves_to(
                ast.Attribute(
                    value=ast.Name(id=bound_name, ctx=ast.Load()),
                    attr="suppress",
                    ctx=ast.Load(),
                ),
                "contextlib.suppress",
                own,
            ):
                return False
        return True
    for alias in node.names:
        head, dot, _leaf = alias.name.partition(".")
        if not head:
            return False
        if alias.asname is not None:
            # #475. `import x as y` binds `y`, never `x`.  So the only question
            # is whether `y` is a name this witness resolves through.  If it is
            # not, the statement cannot change what the header enters, so it is
            # transparent whatever `y` happens to point at -- `import json as
            # cl` beside the canonical import binds `cl`, which nothing here
            # reads, and the filed assert still fires.
            #
            # When it *is* a walked root, `_resolves_to` already answers it
            # exactly, because that is the same resolved-path test used
            # everywhere else:
            #   * `import contextlib as cb` with `cb.nullcontext(...)` --
            #     `cb` resolves to `contextlib`, so this *is* the filed
            #     spelling under another name and must be admitted;
            #   * `import fake as contextlib` -- `contextlib` resolves to
            #     `fake`, which is not `contextlib.suppress`, so the alias has
            #     genuinely rebound the root the witness walks and must be
            #     refused.  Admitting that one is a false-LIVE: the fixture
            #     installs a real suppressor through `fake` and swallows the
            #     assert, yet the witness would certify it enforced.
            #
            # Nothing here is decided from the author's reading of what the
            # alias "means".
            if alias.asname in _witness_module_roots(function, bound) and not _resolves_to(
                ast.Attribute(
                    value=ast.Name(id=alias.asname, ctx=ast.Load()),
                    attr="suppress",
                    ctx=ast.Load(),
                ),
                "contextlib.suppress",
                own,
            ):
                return False
            continue
        if dot:
            # `import a.b` binds the single name `a` (the leaf is an attribute
            # of it), so it collides with a walked root exactly when `a` is
            # one.  When `a` is unrelated -- `import os.path` next to the
            # canonical `import contextlib` -- the statement rebinds nothing
            # the witness resolves through and is transparent.
            if head in _witness_module_roots(function, bound):
                return False
            continue
        # A plain `import a` binds the single name `a`, which is exactly the
        # same collision test the dotted and aliased spellings above make:
        # `a` has to be either a root this witness walks -- in which case it is
        # the filed spelling and must resolve to the real `contextlib` -- or a
        # name nothing here reads, which cannot change what the header enters.
        # `import json` beside the canonical import is that second case: it
        # rebinds `json`, and `contextlib.nullcontext()` still fires.
        if head in _witness_module_roots(function, bound) and not _resolves_to(
            ast.Attribute(value=ast.Name(id=head, ctx=ast.Load()), attr="suppress", ctx=ast.Load()),
            "contextlib.suppress",
            own,
        ):
            return False
    # The import must not rebind a name the header or the manager path reads,
    # and must not collide with a store anywhere in the scope: that would make
    # the value the witness reasons about a different object at the header.
    #
    # The header's own `context_expr` is the *manager name* (`cs`), not the
    # module root, so it cannot answer this question.  What matters is that
    # the import does not shadow a name the witness reads: the module roots
    # behind `contextlib.nullcontext` / `contextlib.suppress` in this scope.
    # Every spelling above has already been checked against that rule, so
    # reaching here means each name in this statement is bound either to the
    # real `contextlib` the witness resolves through or to a name nothing here
    # reads.  Either way it introduces no value, no branch, and no ordering
    # that could settle the name under test.
    return True


def _witness_module_roots(function, bound):
    """Module roots the witness resolves `contextlib.*` calls through.

    Anything the witness later has to attribute to the real `contextlib`
    module is spelled as an attribute path in this scope, so the roots are
    exactly the `ast.Name` nodes that heads such attribute chains.

    ``bound`` is unused here on purpose, and the parameter is kept only so the
    call sites read the same as the other helpers. What a spelling *is* at a
    given point in the scope is a separate question, asked per statement by
    :func:`_import_only_binds_the_resolved_root` against
    :func:`_bindings_after_import`: in

        import json as contextlib
        import contextlib

    the name `contextlib` heads an attribute path in both, but it is the real
    module only after the second statement. Collapsing that into one merged
    table is what made the first look transparent and turned a live assert
    into a false-DEAD.
    """
    roots = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute):
            head = node
            while isinstance(head, ast.Attribute):
                head = head.value
            if isinstance(head, ast.Name):
                roots.add(head.id)
    return roots


def _elif_witness_is_binding_header(node):
    """A ``with`` header whose only effect is binding the name it enters.

    ``with (cs := ...):`` binds and enters in one statement. It is a safe
    carrier for the witness only when the body is a single ``pass`` and the
    context expression is the assignment expression itself -- a body that
    could rebind the name, or a second item that could bind another one, would
    make the carried value undecidable, so those shapes are declined here
    rather than reasoned about downstream.
    """
    if not isinstance(node, ast.With) or len(node.items) != 1:
        return False
    if not (len(node.body) == 1 and isinstance(node.body[0], ast.Pass)):
        return False
    return isinstance(node.items[0].context_expr, ast.NamedExpr)


def _elif_witness_header_value_is_readable(node, bound):
    """Is the binding header's own value one the witness can reason about?"""
    walrus = node.items[0].context_expr
    if not isinstance(walrus.target, ast.Name):
        return False
    value = walrus.value
    if isinstance(value, ast.Constant):
        return True
    if not isinstance(value, ast.Call) or value.keywords:
        return False
    if _resolves_to(value.func, "contextlib.nullcontext", bound) and not value.args:
        return True
    if not _is_readable_suppressor(value, bound):
        return False
    return all(
        isinstance(argument, ast.Name)
        and argument.id in ("AssertionError", "Exception", "BaseException", "ValueError")
        for argument in value.args
    )


def _elif_witness_reaches_header(function, chain, query, bound):
    """Keep the small witness proof free of earlier exits or opaque effects."""
    header = next(
        (node for node in function.body if isinstance(node, ast.With) and query in node.body), None
    )
    if (
        header is None
        or len(header.items) != 1
        or not isinstance(header.items[0].context_expr, ast.Name)
    ):
        return False
    chain_index, header_index = function.body.index(chain), function.body.index(header)
    if chain_index >= header_index:
        return False
    if any(
        not isinstance(node, ast.Pass) for node in function.body[chain_index + 1 : header_index]
    ):
        return False
    if any(not isinstance(node, ast.Pass) for node in header.body[: header.body.index(query)]):
        return False
    for node in function.body[:chain_index]:
        if isinstance(node, ast.Pass):
            continue
        # #451's filed fixture binds `contextlib` with a *function-local*
        # `import contextlib` rather than a module-level one.  That import is
        # exactly as opaque as the module-level spelling it mirrors -- the
        # witness resolves `contextlib.nullcontext` through the same root
        # either way -- but a bare `ast.Import` is neither a `Pass` nor an
        # `ast.Assign`, so the scan used to reject it and the witness never
        # fired.  The result was the damaging direction: the filed fixture,
        # whose assert really does fire, was reported dead.
        #
        # Accepting it unconditionally would be too permissive, because an
        # import can rebind the very name the header resolves through.  Only an
        # import that binds exactly the module root the witness already reads
        # is transparent, so that is what is admitted here.
        if _import_only_binds_the_resolved_root(node, function, bound):
            continue
        # #485. A `try`/`except` above the loop is transparent for the same
        # reason an import is: it binds names and swallows exceptions, but
        # binding a name the witness never reads is inert, and the swallowed
        # exception does not skip the header.
        #
        #     def outer(x, flag, helper):
        #         try:
        #             import nope_missing_xyz
        #         except ImportError:
        #             pass
        #         cs = contextlib.nullcontext()
        #         for item in (1,):
        #             break
        #         else:
        #             cs = contextlib.suppress(AssertionError)
        #         with cs:
        #             assert x != 1
        #
        # The `ModuleNotFoundError` is caught, execution continues, the loop
        # takes its `break`, the `else` is skipped, and the assert fires. The
        # pre-chain scan rejected the `ast.Try` outright -- it is neither a
        # `Pass` nor an `ast.Assign` -- so the witness never fired.
        #
        # The soundness condition is the same one the import rule uses: the
        # statement must not bind a name the witness resolves through. An
        # `except ... as e` binds `e`, and an assignment inside either arm
        # binds its targets, so those are all checked here rather than
        # assumed. A `try` whose *body* rebinds the carried name or a walked
        # root declines, because then the binding the header enters is not
        # the one the witness proved.
        if _try_only_binds_names_the_witness_ignores(node, function, bound):
            continue
        # #445. The carried binding may arrive through a `with` header's
        # assignment expression rather than a plain assignment:
        #
        #     with (cs := contextlib.suppress(AssertionError)):
        #         pass
        #     if x:
        #         pass
        #     elif True:
        #         cs = contextlib.nullcontext()
        #     with cs:
        #         assert x != 1
        #
        # The header still enters only the object its own expression names, so
        # a `pass` body and a bare-name value are exactly as safe here as they
        # are for the plain-assignment spelling -- the statement runs, binds
        # the name, and leaves the rest of the suite to the value gate below.
        # Requiring an `ast.Assign` made the whole #445 shape unprovable while
        # the analyzer still read it live.
        if _elif_witness_is_binding_header(node):
            if not _elif_witness_header_value_is_readable(node, bound):
                return False
            continue
        if not isinstance(node, ast.Assign) or not all(
            isinstance(target, ast.Name) for target in node.targets
        ):
            return False
        value = node.value
        if isinstance(value, ast.Constant):
            continue
        if not isinstance(value, ast.Call) or value.keywords:
            return False
        if _resolves_to(value.func, "contextlib.nullcontext", bound) and not value.args:
            continue
        if not _is_readable_suppressor(value, bound):
            return False
        if not all(
            isinstance(argument, ast.Name)
            and argument.id in ("AssertionError", "Exception", "BaseException", "ValueError")
            for argument in value.args
        ):
            return False
        argument_names = {argument.id for argument in value.args}
        parameter_names = {
            argument.arg for argument in ast.walk(function.args) if isinstance(argument, ast.arg)
        }
        if argument_names & parameter_names:
            return False
        if any(
            argument_names & set(_store_target_names_of(store))
            for store in _scope_body_nodes(function)
        ):
            return False
    return True


def _trailing_else_is_skippable(function, chain, competitor):
    """Can some call take a link of the chain and so skip the trailing `else`?

    The ``else`` arm holding ``competitor`` runs only when *every* test above it
    failed, so a call skips that arm -- and reaches the header still holding the
    preamble's manager -- as soon as *one* link above it is true. That is the
    whole of the question, and it is the opposite direction from the walk in
    :func:`_elif_failure_predicate`, which has to show that every link above is
    false at the one proven failure value. So a link the assert says nothing
    about does not block this walk; it may simply be the link that is true.

        cs = contextlib.nullcontext()
        if False:
            pass
        elif flag:          # nothing in `assert x != 1` constrains `flag`
            pass
        else:
            cs = contextlib.suppress(AssertionError)
        with cs:
            assert x != 1

    At ``x == 1, flag == True`` the leading link is decided-false, ``flag`` is
    true, so no arm binds and the assert fires. The header is live.

    Two links are rejected as *not* possibly-true, and both are decided by
    syntax rather than by argument:

    * a literal-false test, which no call can make true -- so
      ``if False: ... else: <store>`` runs its ``else`` on every call and the
      store really does settle the name;
    * a test reading a name this scope binds to a falsy constant, which is the
      same thing spelled as a name.

    Everything else is left to the ordinary resolution, because a name the
    caller supplies may be anything and an expression this cannot read may be
    anything. A link that rebinds the competitor's name is refused for the
    separate reason that a skipping call would then enter *that* manager rather
    than the carried one -- the both-arms-bind shape the caller owns.
    """
    if function is None or not isinstance(chain, ast.If):
        return False
    links = _links_above_a_trailing_else(chain, competitor[0])
    if not links:
        return False
    for above in links:
        if _contains_any(above.body, competitor[0]):
            return False
        if _condition_can_be_true(above.test, function):
            return True
    return False


def _links_above_a_trailing_else(chain, store):
    """Every `ast.If` link from ``chain`` down to the arm holding ``store``.

    The returned list includes the final link -- the one whose ``orelse`` *is*
    the trailing ``else`` -- because that link is exactly the one that may be
    true to skip the arm. It is empty when the ``else`` belongs to a link that
    is not reachable as a chain member, and when the store is not in a trailing
    ``else`` at all.
    """
    links = []
    link = chain
    while isinstance(link, ast.If):
        orelse = link.orelse
        if not orelse:
            return []
        # A trailing `else` holds statements, never a single `ast.If`: an
        # `elif` link is exactly an `orelse` whose one element is an `ast.If`,
        # so that is what tells the two apart. Reading "not an `ast.If`" as
        # enough made every `elif` look like a trailing `else` and the descent
        # stopped at the first link.
        is_trailing_else = len(orelse) >= 1 and not (
            len(orelse) == 1 and isinstance(orelse[0], ast.If)
        )
        if is_trailing_else:
            if _contains_any(orelse, store):
                links.append(link)
            return links if links else []
        if len(orelse) != 1 or not isinstance(orelse[0], ast.If):
            return []
        links.append(link)
        link = orelse[0]
    return []


def _condition_can_be_true(node, function=None):
    """May this condition hold on some call of ``function``?

    Only the two shapes that no argument can change are answered False: a
    literal-false test, and a test reading a name this scope pins to a falsy
    constant. Everything else is left as "may be true", which is the
    conservative direction here -- this question is whether a *skipping call
    exists*, so an unreadable test is treated as one the caller can satisfy.
    """
    if _condition_is_never_true(node, function):
        return False
    for child in ast.walk(node):
        if not isinstance(child, ast.Name):
            continue
        pinned = _falsy_constant_pinned_to(child.id, function)
        if pinned:
            return False
    return True


def _falsy_constant_pinned_to(name, function):
    """Is ``name`` bound in ``function``'s scope to a constant that is falsy?

        local = 0
        if False:
            pass
        elif local:            # never true, so the `else` always runs
            pass
        else:
            cs = contextlib.suppress(AssertionError)

    Reading ``elif local`` as possibly-true reported that header live while
    CPython swallows the assert on every call -- a FALSE-LIVE, and the
    damaging direction. The pin is what makes the link decided rather than
    merely unknown.
    """
    for statement in _scope_body_nodes(function):
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = statement.value
        if isinstance(value, ast.Constant):
            return not value.value
        # A non-constant binding is a rebinding, not a pin; keep looking.
    return False
