# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part7":
    raise ImportError(
        "tests._sentinel_support_part7 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


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

    #403. The `else`/`elif` case is subtler than the `while False:` one,
    because an arm that always runs is the *settling* direction rather than the
    never-runs one, and it was the shape that stayed wrong after the plain
    `if True:` spelling was fixed:

        if False:
            pass
        elif True:
            cs = list()          # the arm always runs, so this settles `cs`

    `_is_always_true_arm` reads that, and the unreachable-arm check at the end
    still subtracts the mirror-image shape (`if True: ... else: <store>`,
    which never runs), so the two directions stay distinct.
    """
    if function is None:
        return False
    blocks = _enclosing_blocks(statement, function)
    # #403. An always-true **arm** counts from this side as well as an
    # always-true body: `if False: ... elif True: <store>` runs the store on
    # every call, exactly as `if True: <store>` does. Both spellings settle the
    # name, so both belong with the unconditional stores in `_stores_of`.
    if not any(
        _is_always_true_branch(block, statement, function)
        or _is_always_true_arm(block, statement, function)
        for block in blocks
    ):
        # The store is not inside an always-true branch at all, so there is
        # nothing to claim. This is the ordinary case and the cheap exit.
        return False
    # It is inside one, so every other block on the path has to run every time
    # as well. A never-true `if`, a loop over an empty literal, a `while False`
    # and an `else` beside a literal-true test are all read by the same
    # never-runs question, which is the safe direction: the store is left
    # conditional, so whatever the enclosing scope really does still decides.
    # #403. `_is_always_true_arm` counts here for the same reason it counts in
    # the `any` above: an always-run arm settles the name just as an
    # always-true body does, so the `if False:` link that makes the arm
    # reachable must not veto it.
    return all(
        (
            _is_always_true_branch(block, statement, function)
            or _is_always_true_arm(block, statement, function)
            if isinstance(block, ast.If)
            else _block_always_reaches_store(block, statement, function)
        )
        for block in blocks
    ) and not any(_statement_in_unreachable_arm(block, statement, function) for block in blocks)


def _block_always_reaches_store(block, statement, function):
    """Does ``block`` reach ``statement`` on every call, or only on some?

    #435. The always-true-branch rule has to survive two very different
    enclosings, and both of them are *conditional* in the same way: the arm
    holding the store is entered only when some enclosing test comes out a
    particular way, and that is decided per call.

    The first is a literal-true `elif` link (#429). The `elif True:` has a
    literal-true test of its own, so the enclosing block reads as
    always-true, but the link is entered only when every test above it failed.
    The second is a plain conditional `if` with a literal-true arm nested
    inside it:

        cs = contextlib.nullcontext()
        if not x:
            if True:
                cs = list()

    Here the `if True:` really does run whenever it is entered -- but it is
    entered only when `not x` is truthy. With `x = 1` the outer arm never
    runs, `cs` is still the `nullcontext`, and `with cs:` enters and the
    assert fires.

    Both were reported DEAD by the same mistake, and it is worth naming
    precisely: the rule had only two answers -- "this block always runs" and
    "this block never runs" -- and treated *anything that is not never-runs*
    as always-runs. A branch taken on some calls and skipped on others is
    neither, and it fell into the gap. Answering False for a block whose
    reachability depends on a per-call test is the safe direction: the store
    stays conditional, the earlier store remains in force, and whatever the
    enclosing scope really does still decides the header.

    A `while True:` and a loop over a non-empty literal are read as always
    running, which is what lets a store inside one settle the name. That is
    the pre-existing treatment and it is left alone here; only the
    conditional-`if` gap is closed.
    """
    if _is_always_true_branch(block, statement, function):
        return True
    if isinstance(block, ast.If) and _condition_is_never_true(block.test, function):
        # Runs on no call, so it cannot be the reason a store always runs.
        return False
    if _elif_link_is_conditional(block, statement, function):
        # #429. Entered only when every test above the link failed.
        return False
    if (
        isinstance(block, ast.If)
        and _contains_any(block.body, statement)
        and not _condition_is_always_true(block.test, function)
    ):
        # #435. A conditional `if` holding the store in its own body: taken
        # when its test is truthy and skipped when it is not, so the store
        # settles the name on some calls only.
        return False
    return not _block_never_runs(block, function)


def _is_always_true_branch(block, statement, function):
    """Is ``block`` an always-true ``if`` whose *body* holds ``statement``?

    "Always-true" is about the test alone, which is enough only when the body
    is reached on every call. That is true of a standalone ``if True:`` and
    false of an ``elif`` link: an ``elif`` body runs only when every test above
    it in the chain failed, so its own always-true test says nothing about
    whether it is ever entered (#416):

        from contextlib import nullcontext

        def outer(x):
            cs = nullcontext()
            if x:
                pass
            elif True:
                cs = list()      # reached only when x is FALSE
            with cs:
                assert x != 1    # CPython, x=1: fires

    The ``elif True:`` link satisfies the test on its own, so the store was
    promoted to an unconditional one and the header answered ``defeated`` --
    a live contract dropped. The store settles the name only when ``x`` is
    false, so the header is undecidable and must not be settled.

    :func:`_elif_link_is_reached` supplies that missing question, and it is
    the same reachability the ``_always_true_arm_within`` chain walk already
    models for the arm case.
    """
    return (
        isinstance(block, ast.If)
        and _condition_is_always_true(block.test, function)
        and _contains_any(block.body, statement)
        and _elif_link_is_reached(block, function)
    )


def _elif_link_is_conditional(block, statement, function):
    """Is ``block`` an ``elif`` link, reached only when every test above failed?

    #429. An ``elif`` is not its own top-level statement: it is an ``ast.If``
    nested inside the *previous* ``if``'s ``orelse``. So

        cs = contextlib.nullcontext()
        if x:
            pass
        elif True:
            cs = list()

    hands the store to two enclosing blocks, and the inner one has a
    literal-true test. :func:`_is_always_true_branch` reads that inner block
    on its own and answers "yes, the store always runs" -- but the link is
    entered only when ``if x:`` is *false*, and ``x`` is a parameter. The
    store therefore settles the name on some calls only, while the rule
    treated it as settling it on all of them, dropped the earlier
    ``nullcontext`` from :func:`_stores_of`, and reported the ``with cs:``
    header DEAD. CPython enters that header: with ``x = 1`` the ``if x:``
    arm runs, the ``elif`` body never does, ``cs`` is still the
    ``nullcontext``, and the assert fires.

    A literal-true *first* ``if`` really is unconditional, and must keep being
    read that way -- that is the ``if True:`` row this rule exists to serve.
    The distinction is exactly whether the literal-true test is the first link
    in the chain or a later one.
    """
    if function is None or not isinstance(block, ast.If):
        return False
    # The store has to sit somewhere inside this link for the question to mean
    # anything: either its own body, or its `orelse` for the `else` tail.
    if not _contains_any(block.body, statement) and not _contains_any(block.orelse, statement):
        return False
    # An `elif` link is an `ast.If` that is an entry of an enclosing
    # `If.orelse`. Its own test being literal-true says nothing about whether
    # the link is *reached*: that is decided by every test above it.
    #
    # #435. This still only reads the *immediate* parent, so it misses a
    # store nested a further level down:
    #
    #     cs = contextlib.nullcontext()
    #     if x:
    #         pass
    #     elif True:
    #         if True:
    #             cs = list()
    #
    # The nested `if True:` sees the literal-true `elif True:` as its only
    # link parent and concludes the store is unconditional. The `elif True:`
    # link is itself entered only when the outer `if x:` is false, so the
    # store is still conditional. Widening this walk to the whole chain was
    # measured and changed no verdict in 2368 generated shapes, because the
    # enclosing `if x:` is caught by the conditional-branch rule in
    # :func:`_block_always_reaches_store` either way -- so the one-level walk
    # is kept and the gap is closed there instead.
    for parent in _enclosing_blocks(block, function):
        if not isinstance(parent, ast.If):
            continue
        if not any(child is block for child in parent.orelse):
            continue
        if not _condition_is_always_true(parent.test, function):
            return True
    return False


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
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        value = statement.value
    elif isinstance(statement, (ast.For, ast.AsyncFor)) and value is UNREADABLE_VALUE:
        value = None
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
