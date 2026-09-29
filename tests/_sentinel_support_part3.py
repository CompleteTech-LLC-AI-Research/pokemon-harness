"""Sentinel support, part 3 of 5 (#122 split).

Functions: _last_store_before, _raw_store_values, _aliased_suppressions, _encloses, _bindings_before, _unreadable_suppressor, _swallowing_exit_class, _constructed_class_of, _assigned_value, _dotted_class_name, _locally_defined_classes, _provably_truthy_exit, _statically_truthy, _exit_swallows_assertion_error, _is_exception_argument, _is_exception_name, _is_assertion_error_ref, _is_user_defined_swallowing_with, _is_suppression_call, _raises_without_an_expected_type, _entered_suppressions
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

    Only the forms that carry a *value* are listed. A ``for`` target, a
    ``with ... as``, an ``except ... as``, a ``del`` and a ``match`` capture
    bind a name without one, and :func:`_deref_alias` stops on a missing entry
    exactly as it does for an unbound name -- which is right, because a later
    store of that name is what retires the carried value anyway.

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
        else:
            continue
        for name in _store_target_names(targets):
            raw.setdefault(name, []).append((statement, value))
    return raw


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

# ---------------------------------------------------------------------------
# Fragment guard. This file is not an importable module: it is one piece of
# tests/_timed_menu_milestone_sentinel_support, which `exec`s it, together with
# the other fragments, into a single shared namespace.
#
# That sharing is what makes the sentinels work, and it cannot survive being
# bypassed. Executed through the entry point, `__name__` is the support module's
# name and this guard is inert. Executed under its OWN name -- which is what
# `import tests._sentinel_support_part3` does, and what the import system does by itself -- the
# file would bind only the names it defines itself: a cross-fragment call would
# raise NameError, and a name imported from here would be a different object
# from the one the support module exports. Worse, that breakage is
# order-dependent and silent, which is a poor property for a module whose entire
# purpose is catching silent structural faults.
#
# Fail loudly instead, and name the supported import.
# ---------------------------------------------------------------------------
if __name__ == "tests._sentinel_support_part3":
    raise ImportError(
        "tests._sentinel_support_part3 is a fragment of "
        "tests._timed_menu_milestone_sentinel_support, not an importable "
        "module. Import the support module instead:\n"
        "    from tests import _timed_menu_milestone_sentinel_support as "
        "support\n"
        "Importing this fragment directly gives it a private copy of the shared "
        "namespace: cross-fragment calls raise NameError, and rebinding a name "
        "on the support module would not reach this code."
    )

