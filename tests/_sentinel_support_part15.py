# Shared-namespace fragment for reviewed sentinel helpers.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part15":
    raise ImportError(
        "tests._sentinel_support_part15 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


def _elif_failure_predicate(test, values):
    """Evaluate only a literal/name predicate at the proven assertion failure.

    #471. This used to answer ``Eq`` and ``NotEq`` and decline every other
    comparison operator, so ``x < 0`` returned ``None`` -- "not readable".

    That was safe for its original caller, which reads ``None`` as *decline the
    claim*. It is not safe for a caller that reads ``None`` as *possibly true*,
    which is what ``_break_reachable_with_failure`` does when it asks whether
    a ``break`` under a guard can be reached at the failing value. A loop whose
    only ``break`` sits under ``if x < 0:`` was then reported as skipping its
    ``else`` at ``x == 1``, and a contract CPython swallows on every call was
    certified enforced.

    So the ordering operators are evaluated here rather than left to the caller's
    interpretation. The operands are already resolved to concrete values above,
    so this is the same comparison Python would perform, on the same values.

    Mixed-type operands stay declined rather than raising: a ``TypeError`` from
    ``1 < "a"`` is not a truth value, and guessing one would be worse than
    declining.

    #479. ``Is`` and ``IsNot`` join the evaluated set here. #471 closed the
    ordering gap but left identity declined, and that is the *same* damaging
    fall-through for the *same* reason: ``_break_reachable_with_failure`` reads
    ``None`` as "possibly true", so a loop whose only ``break`` sits under
    ``if x is None:`` was reported as skipping its ``else`` at every value the
    domain can hold, and a suppressor the loop always installs was certified
    absent. #479 measured 27 such fixtures live on master.

    Identity is defined for every pair, but AST literals and equality-based
    failure witnesses do not preserve runtime object identity. None, unequal
    primitive values, literal booleans, and a name compared with itself can
    settle the question; equal non-singletons remain undecidable.

    #479 residual. ``In`` and ``NotIn`` join for the same reason. Membership is
    #total over a literal container of resolved values -- ``1 in [0]`` is
    #``False`` rather than an error -- so once the left operand and the
    #container's elements are known the answer is decided. It is *not* total
    #for an arbitrary container (``in`` can raise from a custom ``__contains__``
    #or from a missing key on a ``dict``), so only a container the analyzer can
    #read element by element is answered; anything else still declines.

    #479 residual. ``ast.BoolOp`` joins too. ``and``/``or`` are total over
    operands whose truth values are known, and ``bool`` is total on any object,
    so evaluating the operands in Python's own order -- which is what the
    loop body would do -- decides the guard without raising. Short-circuit is
    respected rather than approximated: an operand after the one that already
    decides the result is not needed, and one that cannot be read makes the
    whole operator decline rather than guess.
    """
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.Name) and test.id in values:
        return bool(values[test.id])
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        value = _elif_failure_predicate(test.operand, values)
        return None if value is None else not value
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and type(test.ops[0]) not in (ast.In, ast.NotIn)
    ):
        operands = [test.left, test.comparators[0]]
        resolved = []
        for operand in operands:
            if isinstance(operand, ast.Constant):
                resolved.append(operand.value)
            elif isinstance(operand, ast.Name) and operand.id in values:
                resolved.append(values[operand.id])
            else:
                return None
        left, right = resolved
        operator = type(test.ops[0])
        try:
            if operator is ast.Eq:
                return left == right
            if operator is ast.NotEq:
                return left != right
            if operator is ast.Lt:
                return left < right
            if operator is ast.LtE:
                return left <= right
            if operator is ast.Gt:
                return left > right
            if operator is ast.GtE:
                return left >= right
            if operator in (ast.Is, ast.IsNot):
                # AST literals and equality witnesses do not preserve runtime
                # object identity. Unequal primitive values cannot be identical;
                # None is the one singleton equality fixes without a numeric
                # True/1 ambiguity. Equal non-singletons remain undecidable.
                if left is None or right is None:
                    identical = left is right
                elif left != right:
                    identical = False
                elif (
                    isinstance(operands[0], ast.Name)
                    and isinstance(operands[1], ast.Name)
                    and operands[0].id == operands[1].id
                ):
                    identical = True
                elif all(
                    isinstance(operand, ast.Constant) and type(operand.value) is bool
                    for operand in operands
                ):
                    identical = left is right
                else:
                    return None
                return identical if operator is ast.Is else not identical
        except TypeError:
            # An unorderable pair has no truth value Python would compute, and
            # the assert under test cannot be evaluated against it either.
            return None
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        operator = type(test.ops[0])
        if operator not in (ast.In, ast.NotIn):
            return None
        container = test.comparators[0]
        if isinstance(container, (ast.List, ast.Tuple, ast.Set)):
            elements = container.elts
        else:
            return None
        member = test.left
        if isinstance(member, ast.Constant):
            member_value = member.value
        elif isinstance(member, ast.Name) and member.id in values:
            member_value = values[member.id]
        else:
            return None
        if not all(isinstance(element, ast.Constant) for element in elements):
            return None
        # Element-wise rather than `in`: this evaluates the same question Python
        # would, without inheriting `__eq__` from any operand, and it cannot
        # raise the way a custom `__contains__` can -- which is why an
        # unreadable container still declines above rather than guessing here.
        contained = any(member_value == element.value for element in elements)
        return contained if operator is ast.In else not contained
    if isinstance(test, ast.BoolOp):
        results = []
        for value in test.values:
            results.append(_elif_failure_predicate(value, values))
        if isinstance(test.op, ast.And):
            if any(result is False for result in results):
                # One false operand settles `and` outright. Python short-
                # circuits on it too, so an unreadable operand *after* that
                # point cannot change the answer or raise.
                return False
            if all(result is True for result in results):
                return True
        else:
            if any(result is True for result in results):
                # The mirror: one true operand settles `or`, and Python stops
                # evaluating there.
                return True
            if all(result is False for result in results):
                return False
        # Anything still unreadable leaves the operator genuinely undecided.
        return None
    return None


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


def _store_is_in_a_skipped_else_arm(statement, function=None):
    """Is this store written in a terminal ``else`` arm of an if/elif chain?

    #452. ``_store_is_in_an_elif_link`` deliberately answers ``False`` for an
    ``else`` arm, on the reasoning that an ``else`` and its ``if`` body are
    complementary and so together cover every call -- which is true, and is
    exactly why an ``else`` store is genuinely decisive about whether *some*
    path suppresses. But it misses the direction that matters here: an ``else``
    arm that is skipped still leaves whatever binding the ``if`` *did not*
    override in force, so the assert can fire on the skipped path without ever
    entering the suppressor.

        cs = contextlib.nullcontext()
        if x:
            pass
        else:
            cs = contextlib.suppress(AssertionError)
        with cs:
            assert x != 1

    At ``x == 1`` the ``else`` is skipped and ``cs`` is still the plain
    ``nullcontext``, so the header is live. Recording the arm as a skipped
    branch lets :func:`_elif_skipped_path_can_fail` prove the filed witness
    for the terminal ``else`` exactly as it already does for an ``elif``.

    The arm must be *terminal*: the block it lives in is the single trailing
    ``else`` of some ``if``, not another ``elif`` link. A decided ``if False:``
    whose ``else`` always runs is left alone here and declined by the witness
    walk, which is what keeps that case DEFEATED.
    """
    if function is None:
        return False
    for block in _enclosing_blocks(statement, function):
        if not isinstance(block, ast.If):
            continue
        # A terminal `else` is the trailing `orelse` of a chain, so it is the
        # arm holding this store and nothing else. An `elif` arm is exactly an
        # `orelse` whose single element is an `ast.If`, so excluding that one
        # shape leaves `elif` to `_store_is_in_an_elif_link` while keeping
        # every real trailing `else`, including a multi-statement one and one
        # that itself contains a nested `if`.
        if len(block.orelse) == 1 and isinstance(block.orelse[0], ast.If):
            continue
        if _contains_any(block.orelse, statement):
            return True
    return False


def _store_is_in_a_break_skipped_else_clause(statement, function=None):
    """Is this store written in a loop ``else`` that a ``break`` can skip?

    #451. A ``for``/``else`` or ``while``/``else`` ``else`` clause runs only when
    the loop finishes *without* executing a ``break``. #378 correctly
    established the complementary rule -- a zero-iteration loop's ``else``
    still runs -- but the other side was not modelled, so a suppressor bound in
    an ``else`` that a ``break`` skips retired the carried ``nullcontext``:

        cs = contextlib.nullcontext()
        for item in (1,):
            break                       # the else never runs
        else:
            cs = contextlib.suppress(AssertionError)
        with cs:                       # `cs` is still the nullcontext
            assert x != 1              # LIVE

    Executed on CPython 3.12.14 the assert **fires**, while the analyzer
    reported it defeated. That is #308 criterion 1's damaging direction: a
    contract that really enforces, certified unreachable.

    The loop is the one whose ``orelse`` holds the store, and the ``break`` has
    to be bound to *that* loop. A ``break`` inside a nested loop belongs to the
    inner loop, so ``for ...: for ...: break`` still completes the outer loop
    normally and the outer ``else`` does run. A ``break`` inside a nested
    ``def``/``lambda``/``class`` body is not executed by this loop at all, and
    CPython rejects one written directly in a ``def`` as a ``SyntaxError``
    anyway. :func:`_has_own_break` stops at each of those walls for exactly
    this reason.

    A loop with no reachable ``break`` always completes normally, so its
    ``else`` does run and the store really does settle the name; declining
    there would keep the existing, correct answer rather than invent a new one.
    """
    if function is None:
        return False
    for block in _enclosing_blocks(statement, function):
        if not isinstance(block, (ast.For, ast.AsyncFor, ast.While)):
            continue
        # `orelse` and `body` are AST *siblings*, and only `body` is skipped by
        # a `break`. #378's gate on `_in_body` is the same distinction, asked
        # from the other side: that rule drops stores in a body a zero-iteration
        # loop never runs, and this one keeps stores in an `orelse` a `break`
        # can skip. Neither guard covers the other.
        if _contains_any(block.orelse, statement) and any(
            _has_own_break(node) for node in block.body
        ):
            return True
    return False


#: Nodes that capture their own ``break``, so nothing inside them can break the
#: loop being asked about. A nested loop binds the ``break`` to itself, and a
#: nested scope is a body this loop never executes.
_BREAK_CAPTURING_NODES = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
)


def _has_own_break(node):
    """Does a ``break`` in this subtree bind to the loop that encloses it?

    Walks the subtree but stops at anything that captures its own ``break``. A
    ``break`` reached before such a wall is the enclosing loop's; one found
    only beyond a wall belongs to that wall and does not count.
    """
    if isinstance(node, ast.Break):
        return True
    if isinstance(node, _BREAK_CAPTURING_NODES):
        return False
    return any(_has_own_break(child) for child in ast.iter_child_nodes(node))
