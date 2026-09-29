"""Sentinel support, part 4 of 5 (#122 split).

Functions: _entry_is_dead, _module_stores, _literal_runtime_type, _binds_element_of, _stores_of, _store_is_settled_before, _runs_before_end_of, _edge_is_guaranteed, _loop_else_always_runs, _breaks_own_loop, _match_capture_pins_value, _capture_element_kind, _pattern_binds_element, _entered_name_is_dead, _is_suppressing_with, _ancestors, _in_body, _falsy_literal, _is_empty_literal_iterable, _is_uncalled_nested_def, _literal_value, _as_number, _numeric_literal, _is_count_like
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

# ---------------------------------------------------------------------------
# Fragment guard. This file is not an importable module: it is one piece of
# tests/_timed_menu_milestone_sentinel_support, which `exec`s it, together with
# the other fragments, into a single shared namespace.
#
# That sharing is what makes the sentinels work, and it cannot survive being
# bypassed. Executed through the entry point, `__name__` is the support module's
# name and this guard is inert. Executed under its OWN name -- which is what
# `import tests._sentinel_support_part4` does, and what the import system does by itself -- the
# file would bind only the names it defines itself: a cross-fragment call would
# raise NameError, and a name imported from here would be a different object
# from the one the support module exports. Worse, that breakage is
# order-dependent and silent, which is a poor property for a module whose entire
# purpose is catching silent structural faults.
#
# Fail loudly instead, and name the supported import.
# ---------------------------------------------------------------------------
if __name__ == "tests._sentinel_support_part4":
    raise ImportError(
        "tests._sentinel_support_part4 is a fragment of "
        "tests._timed_menu_milestone_sentinel_support, not an importable "
        "module. Import the support module instead:\n"
        "    from tests import _timed_menu_milestone_sentinel_support as "
        "support\n"
        "Importing this fragment directly gives it a private copy of the shared "
        "namespace: cross-fragment calls raise NameError, and rebinding a name "
        "on the support module would not reach this code."
    )

