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


def _elif_skipped_path_can_fail(entries, orders, competitor, bound, function, query, owning=None):
    """Prove the filed literal-comparison failure reaches a skipped elif arm.

    A non-suppressor is not necessarily enterable. A live classification also
    needs a failure value that takes an earlier arm, rather than the suppressing
    one. Unknown managers, predicates, local rebinding and nested control flow
    retain the existing resolution.
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


def _is_provably_unreached_store(entry, orders, function):
    """Is this conditional store inside a loop body that can never run?

    #378. A ``for`` over a literal empty iterable has no iterations, so every
    statement in its body is unreachable. A store written there is recorded,
    flagged ``conditional``, and would otherwise compete with the store that
    really is in force -- retiring a live ``nullcontext`` and letting a
    swallowed assert be reported as swallowed when it is in fact live.

    The test is deliberately narrow: only a *provably* empty literal iterable
    answers yes. ``helper.items()`` may yield nothing, but it may not, so a
    store in that body keeps competing and the ordinary ambiguity handling
    applies. ``range(0)`` and ``set()`` are excluded for the same reason
    `_is_empty_literal_iterable` excludes them -- deciding them means
    reasoning about builtins rather than reading a literal.

    A ``while True:`` loop, or one whose test is merely truthy, is not
    provably empty and so is not matched.

    The ``For`` branch is gated on :func:`_in_body`, and that gate is the
    whole difference between a correct answer and a regression. ``For.body``
    and ``For.orelse`` are AST **siblings**: both are attributes of the same
    ``ast.For`` node, and both therefore appear in the ancestor walk, but only
    one of them is skipped when the loop has no iterations. The ``else``
    clause is exactly what runs *because* the loop finished without
    ``break``, so every statement in it executes on every path through a
    zero-iteration loop:

        for y in ():
            pass
        else:
            cs = contextlib.suppress(AssertionError)   # this runs

    Executed on CPython 3.12.14 the assert under the following ``with cs:`` is
    swallowed in all of these -- a plain assign, the same over a list and a
    dict literal, an assign nested in an ``if``, and one nested in a ``with``.
    Unguarded, this helper answered "unreached" for every one of them and the
    assert was reported **live**, which is the damaging direction #378 exists
    to prevent and a regression against ``master`` (``2c81f10``), where all
    five are already correct. The clause is reused rather than a new
    containment test written, because it already answers the same question
    for a ``with`` header: "inside the executed body, not a handler or an
    ``else``".

    This is the same question `_is_store_statement` answers for a header
    *nested inside* the loop; both are needed because the two answer through
    different tables. On ``master`` (``2c81f10``) the nested case was already
    right and the after-loop case still certified three live asserts dead, so
    neither guard covers the other.

    The port originally also filtered ``_resolve_bindings``' competing set and
    added an empty-competing fallback there. Mutation showed both **survived**
    -- reverting either alone leaves the lane green -- because every caller
    either pre-filters the same table (the `_assigned_suppressors` loop above)
    or declines before reaching the ambiguity branch (the ``nonlocal``
    resolver). They were removed rather than left in as untested code.
    """
    statement = entry[0]
    if function is None:
        return False
    # `_ancestors` runs outermost-first and ends with ``target`` itself, so
    # the last element is dropped: the question is which *containers* the
    # store sits inside.
    for ancestor in list(_ancestors(function, statement))[:-1]:
        # `ast.AsyncFor` is included for symmetry with `_is_store_statement`,
        # but it is deliberately untested here: an `async for` over a *literal*
        # container cannot execute, because a literal is not an async
        # iterable, so no fixture can put the analyzer and CPython in the same
        # room for that shape. Reverting this clause to `ast.For` alone
        # therefore survives mutation, and that is recorded rather than hidden
        # behind a row that cannot run. The clause is kept because the
        # narrowing that survives mutation would be the more surprising change.
        # An outer empty loop's else can contain an inner empty loop body.
        # Keep scanning unless this particular body contains the store.
        if (
            isinstance(ancestor, (ast.For, ast.AsyncFor))
            and _is_empty_literal_iterable(ancestor.iter)
            and _in_body(ancestor, statement)
        ):
            return True
    return False


def _carried_suppressor_has_unshadowed_arguments(value, function):
    """Require builtin exception arguments throughout the enclosing scopes."""
    owning = _module_for_function(function)
    scope = function
    while scope is not None:
        if not _nonlocal_has_known_exception_arguments(value, _raw_store_values(scope), scope):
            return False
        scope = _nonlocal_parent_function(scope, owning)
    if owning is None:
        return False
    for argument in value.args:
        for statement in owning.body:
            nodes = [statement, *_module_level_bindings(statement)]
            if any(any(_names_bound_by_statement(node, argument.id)) for node in nodes):
                return False
    return True
