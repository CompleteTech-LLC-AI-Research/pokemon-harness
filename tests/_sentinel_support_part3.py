# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part3":
    raise ImportError(
        "tests._sentinel_support_part3 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


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


def _single_loop_element(iterable):
    """The one element a ``for`` over ``iterable`` binds, or ``None``.

    #370. Only a literal tuple or list with exactly one element qualifies. That
    is the shape where every iteration binds the same object, so recording the
    element is sound regardless of where the later read sits -- inside the body
    or after the loop.

    Everything else returns ``None`` and the ``for`` target stays unrecorded,
    which leaves the name unreadable rather than misread. In particular a
    multi-element literal is refused even though its last element is the right
    answer for a header *after* the loop, because the same recorded value is
    also what an in-body header would resolve to, and there it is wrong. #385
    measured that trade on a real head: picking an index moves a damaging cell
    rather than removing it.
    """
    if not isinstance(iterable, (ast.Tuple, ast.List)):
        return None
    if len(iterable.elts) != 1:
        return None
    return iterable.elts[0]


def _unanimous_non_enterable_element(iterable, bound):
    """#413. One element standing for a literal whose elements all agree.

    Returns an element of ``iterable`` when the iterable is a literal with at
    least two elements and *every* one of them is a value that cannot be
    entered, and ``None`` in every other case.

    A `for` over a literal binds a different object per iteration, so the
    in-body target is normally unreadable and declining is correct -- see
    :func:`_single_loop_element`. That reasoning rests on the elements
    *disagreeing*: `(suppress(), nullcontext())` is swallowed on the first
    iteration and live on the second, so no single recorded value describes
    both. When the elements agree there is no disagreement to preserve. Each
    iteration binds a value that is itself non-enterable, `with cs:` raises
    before the body every time, and the assert is unreachable on every
    iteration. The verdict does not depend on which iteration is read, so the
    thing that made the multi-element case undecidable is absent.

    Only a readable suppressor counts as non-enterable here. Anything this
    module cannot read as a suppressor -- an arbitrary call, a bare name, a
    starred element -- makes the answer `None`, so a literal containing one
    keeps the existing decline. That is the conservative direction and it
    matches the single-element rule, which only ever records a value it can
    read.

    The first element is returned because the recorded value is never read as
    *this* element: unanimity is what makes the choice irrelevant, and the
    caller only needs a readable value to stand for "all of them".
    """
    if not isinstance(iterable, (ast.Tuple, ast.List)):
        return None
    if len(iterable.elts) < 2:
        return None
    # `bound` is whatever import bindings the caller had. Every caller passes
    # either the real map or a stand-in for "no imports recorded" -- `None` from
    # a defaulted signature, an empty `set()` from `_module_stores`, which
    # passes one deliberately -- and `_resolved_dotted` looks names up with
    # `.get`. A stand-in that is not a mapping has no `.get`, so an empty dict
    # stands in for all of them. That is the conservative answer: with no
    # import recorded, `contextlib.suppress(...)` does not resolve, no element
    # reads as a suppressor, unanimity fails, and the caller keeps the decline.
    if not hasattr(bound, "get"):
        bound = {}
    if not all(_is_readable_suppressor(element, bound) for element in iterable.elts):
        return None
    return iterable.elts[0]


def _loop_element_for_read_after(iterable):
    """The element a ``for`` target holds once the loop has finished, or ``None``.

    #385. :func:`_single_loop_element` refuses a multi-element literal because a
    header *inside* the body sees a different value on each iteration, and no
    one element answers for all of them. A header *after* the loop is the
    opposite case and the refusal is wrong there:

        for cs in (contextlib.nullcontext(),
                   contextlib.suppress(AssertionError)):
            pass
        with cs:                 # `cs` is the LAST element, the suppressor
            assert x == 99       # executed: swallowed

    After the last iteration the target holds the final element, so that one is
    what any later read sees -- and it is the same answer on every path, which
    is the property the in-body case lacks. Executed, the analyzer above
    reported this header ``enforced``: a disarmed contract certified as
    load-bearing (#308 criterion 1).

    Refusing it was the safe direction for the *in-body* question and the
    damaging one here, so the two are separated by position rather than
    averaged. The value is only used by a caller that has already established
    the header is positioned after the loop; :func:`_single_loop_element`
    remains what the in-body path consults, so the multi-element limit the
    sentinel suite pins is untouched.

    A non-literal iterable still returns ``None``: which element survives is
    then a runtime property of the object, and declining leaves the contract
    ``enforced`` rather than guessing.
    """
    if not isinstance(iterable, (ast.Tuple, ast.List)):
        return None
    if not iterable.elts:
        return None
    return iterable.elts[-1]


def _loop_target_bindings_after_loop(function, header, bound=None, raw_values=None):
    """Bindings a *completed* loop leaves in force at a header read after it.

    #385. :func:`_loop_target_bindings` answers for a header written **inside** a
    loop's body, where the target holds a different value on each iteration.
    Its refusal to guess is right there and is left untouched. This function
    answers the different question raised by a header positioned **after** the
    loop has run to exhaustion:

        for cs in (contextlib.nullcontext(),
                   contextlib.suppress(AssertionError)):
            pass
        with cs:                 # `cs` is the LAST element, the suppressor
            assert x == 99       # executed: swallowed

    Once the last iteration is done the target holds the final element, and
    that is the same answer on every path -- the property the in-body case
    lacks. Executed, the analyzer reported this header ``enforced``: a disarmed
    contract certified as load-bearing (#308 criterion 1).

    Three conditions gate the answer, and all three are required:

    * the loop must **precede** the header in the block that sequences them, so
      the target has reached its final value rather than an in-flight one;
    * the loop must be able to run to **exhaustion**. A ``break`` leaves the
      target on whichever element was current, not the last one, so a loop
      containing one is declined;
    * the loop **body must not rebind the name**. The body runs after the
      target on every pass, so a rebound name is whatever the body's own store
      left behind and the iterable's final element says nothing about it.

    A ``return`` in the body is *not* a reason to decline. It stops the loop on
    the header's own path, so the header is only ever reached when the loop
    finished -- which is exactly the case being claimed.

    A non-literal or empty iterable is declined too: which element survives is
    then a property of the object rather than of the syntax, and an empty one
    binds nothing at all (CPython raises ``UnboundLocalError`` at the header
    rather than entering a context). Both leave the contract ``enforced``,
    the safe direction.

    The loop's own binding is offered only while nothing later has overwritten
    the name. :func:`_raw_store_values` declines to record a multi-element loop
    at all, so a store that follows one is absent from the table too; the
    positional check below is therefore made against every recorded store of
    the name rather than against the table's completeness. That is what keeps
    a later ``cs = nullcontext()`` from inheriting the loop's suppressor.
    """
    if function is None:
        return {}
    if raw_values is None:
        raw_values = _raw_store_values(function)
    header_order = _binding_order(function, header)
    resolved = {}
    for node in ast.walk(function):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        if not _loop_completes_before(node, header, function):
            continue
        loop_order = _binding_order(function, node)
        for name in _store_target_names([node.target]):
            if _loop_body_rebinds_the_name(node, name):
                continue
            element = _loop_element_for_read_after(node.iter)
            if element is None:
                continue
            if _name_rebound_after_loop(name, loop_order, header_order, raw_values, function):
                continue
            if _store_precedes_header_in_shared_block(name, node, header, raw_values, function):
                continue
            # A readable element stands in for the name's binding. An
            # unreadable one is recorded as `_NOT_A_SUPPRESSOR` so the loop
            # still retires a suppressor carried in from an enclosing block,
            # exactly as the in-body path does. Dropping it instead would let
            # a stale carried value outlive the loop that overwrote it.
            resolved[name] = (
                element if _is_readable_suppressor(element, bound) else _NOT_A_SUPPRESSOR
            )
    return resolved


def _loop_completes_before(loop, header, function):
    """Is ``header`` read only after ``loop`` has run to exhaustion?

    A header in the loop's own *body* is the in-body question and is answered
    by :func:`_loop_target_bindings`; a loop that merely encloses the header
    has not finished when the header is evaluated, so it is excluded here
    rather than consulted twice.

    The ``else`` arm was excluded alongside the body and that was wrong, and
    measurably so. An ``else`` arm runs *after* the iterable is exhausted --
    that is the only way to reach it without a ``break`` -- so the target
    holds the final element there for the same reason a header after the loop
    does:

        for cs in (nullcontext(), suppress(AssertionError)):
            pass
        else:
            with cs:                 # `cs` IS the last element
                assert x == 99       # executed: swallowed

    Executed, that assert never fires; reported ``enforced`` it certifies a
    disarmed contract as load-bearing. Grouping the arm with the body was a
    reading of :func:`_loop_arms_reach`, which answers "is this header in an
    arm of the loop" and cannot distinguish the two by itself. The arm is
    therefore separated here, where the question is which value the target
    holds, and it is left to :func:`_loop_target_bindings` to decline when the
    element is not a single readable one.

    ``break`` is the other exclusion. It leaves the loop part-way, so the
    target holds the element that was current at the break rather than the
    last one, and reading the last would be a guess about which iteration
    stopped the loop.

        for cs in (nullcontext(), suppress(AssertionError)):
            if flag:
                break
        with cs:                 # the element current at the break, not the last
            assert x != 1

    Executed, the two values of ``flag`` disagree -- swallowed on one, live on
    the other -- so the loop is declined and the header stays ``enforced``,
    which is the safe direction. ``continue`` is deliberately not treated the
    same way: it still ends on the final element, so the answer it leaves
    behind is the one being claimed.
    """
    if _header_in_loop_body(loop, header):
        return False
    # A `break` anywhere in the body means the `else` arm is skipped on that
    # path, but a header *inside* the arm is only ever reached when the loop
    # ran to exhaustion -- so the arm is not declined for the break. The
    # position check below is what places it after the loop.
    # `_breaks_own_loop` answers for a NON-loop node: handed a loop it reads
    # only the `else` arm, because a nested loop's own `break` belongs to that
    # nested loop. So it is asked about each statement of this loop's body,
    # which is the shape it was written for.
    if not _header_in_loop_else(loop, header) and any(
        _breaks_own_loop(child) for child in loop.body
    ):
        return False
    if _header_in_loop_else(loop, header):
        return True
    return _precedes_in_shared_block(loop, header, function)


def _header_in_loop_body(loop, header):
    """Is ``header`` inside ``loop``'s own ``body``?"""
    if not isinstance(loop, (ast.For, ast.AsyncFor)):
        return False
    return any(child is header for statement in loop.body for child in ast.walk(statement))


def _header_in_loop_else(loop, header):
    """Is ``header`` inside ``loop``'s ``else`` arm?"""
    if not isinstance(loop, (ast.For, ast.AsyncFor)):
        return False
    return any(child is header for statement in loop.orelse for child in ast.walk(statement))


def _precedes_in_shared_block(first, second, function):
    """Does ``first`` come before ``second`` in the innermost block holding both?

    Top-level statement index is the module's usual order key, but it is too
    coarse here: a loop and a header written inside one ``if`` share that index
    by construction, which would decline the ordinary in-one-block reading:

        if flag:
            for cs in (contextlib.nullcontext(), suppress(AssertionError)):
                pass
            with cs: ...

    The block that actually sequences the two is the innermost one enclosing
    both, so that is the list the positions are read from. Ordering is compared
    there and nowhere else, which is what makes a loop written *after* the
    header keep declining.

    """
    first_parent = _enclosing_node(function, first)
    second_parent = _enclosing_node(function, second)
    if first_parent is None or first_parent is not second_parent:
        return False
    body = _statements_of_block(first_parent)
    first_position = _position_of_child(body, first)
    second_position = _position_of_child(body, second)
    if first_position is None or second_position is None:
        return False
    return first_position < second_position


def _enclosing_node(function, target):
    """The statement whose body directly holds ``target``, or ``None``."""
    for parent in ast.walk(function):
        for child in ast.iter_child_nodes(parent):
            if child is target:
                return parent
            if any(node is target for node in ast.walk(child)):
                break
    return function if target in getattr(function, "body", []) else None


def _statements_of_block(block):
    """The statement list a node executes, with a function's own body included.

    :func:`_block_body` opens the branch constructs (``if``/``try``/``match``/
    loop) because the rules that use it care which *branch* a store sits in.
    This question is different: it asks what order two siblings are evaluated
    in, and a function's top-level body is the block in which a loop and a
    trailing ``with`` are siblings. That case has no entry in
    :func:`_block_body`, so it is added here rather than widening that helper
    and changing the branch test every other caller depends on.
    """
    if isinstance(block, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return list(block.body)
    return _block_body(block)


def _position_of_child(statements, target):
    """Index of the direct child of ``statements`` that holds ``target``."""
    for index, child in enumerate(statements):
        if child is target or any(grandchild is target for grandchild in ast.walk(child)):
            return index
    return None


def _name_rebound_after_loop(name, loop_order, header_order, raw_values, function):
    """Does a later store of ``name`` supersede ``loop_order`` before the header?

    The check is positional rather than a search for "the last recorded store",
    because :func:`_raw_store_values` records a multi-element loop as *nothing*.
    A store written between the loop and the header is absent from the table
    whenever the loop itself is unrecorded, so a table-completeness argument
    would read the loop as the final write and hand the header a stale
    suppressor:

        for cs in (suppress(AssertionError), nullcontext()):
            pass
        cs = nullcontext()
        with cs:                 # the nullcontext, not the loop's suppressor
            assert x != 1

    The same check has to run over the *statement list* that sequences the
    header, because :func:`_binding_order` cannot separate two stores that
    share a top-level statement -- it maps both to the same index and says so
    in its own docstring. A store written in a loop's ``else`` arm is the
    exact case:

        for cs in [contextlib.suppress(AssertionError)]:
            pass
        else:
            cs = contextlib.nullcontext()   # same top-level index as the loop
            with cs:                        # the nullcontext wins
                assert x != 1

    Read through the order key alone the arm's store compared *equal* to the
    loop, the strict ``<`` never fired, and the loop's suppressor was layered
    over a name the arm had already rebound -- reporting the assert defeated
    and dropping a contract that really fires. :func:`_precedes_in_shared_block`
    is what carries the ordering the order key cannot express.
    """
    for statement, _ in raw_values.get(name, []):
        order = _binding_order(function, statement)
        if loop_order < order <= header_order:
            return True
    return False


def _store_precedes_header_in_shared_block(name, loop, header, raw_values, function):
    """Does a store of ``name`` sit between ``loop`` and ``header`` in one block?

    :func:`_name_rebound_after_loop` asks the same question through
    :func:`_binding_order`, which maps every store inside one top-level
    statement to the same index and explicitly documents that equal keys carry
    no ordering. A loop and a store in its ``else`` arm are exactly that pair,
    so the arm's store is invisible to the order key and the loop's element
    would be layered over a name already rebound:

        for cs in [contextlib.suppress(AssertionError)]:
            pass
        else:
            cs = contextlib.nullcontext()
            with cs:                 # the nullcontext, not the loop's element
                assert x == 99

    Executed, that assert fires; reporting it defeated drops a load-bearing
    contract. The check is the positional one, over the block that actually
    sequences the two, so it holds for an arm and for an ordinary block alike.

    The store is declined whether or not it is *guaranteed* to have run, and
    that is the same safe direction the ambiguity rule takes. A store inside an
    ``if`` between the loop and the header may not have run, in which case the
    loop's element really is in force -- so the loop's value is right on that
    path. But the store's value is right on the other, and the two disagree:

        for cs in (nullcontext(), suppress(AssertionError)):
            pass
        if flag:
            cs = nullcontext()
        with cs:                 # nullcontext when flag, suppressor otherwise
            assert x != 1

    Executed, the assert fires when ``flag`` is true and is swallowed when it
    is false, so no single verdict is right and the loop is declined. Layering
    the loop's element over the store answers ``enforced`` and is a false LIVE
    on the ``flag=True`` path.

    Every recorded store of the name is considered rather than only the last,
    because :func:`_raw_store_values` records a multi-element loop as nothing
    and the table is therefore not a complete account of the name's writes.
    """
    for statement, _ in raw_values.get(name, []):
        if statement is loop:
            continue
        if _precedes_in_shared_block(statement, header, function):
            return True
    return False


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
    if any(isinstance(node, ast.Break) for child in statement.body for node in ast.walk(child)):
        return UNREADABLE_VALUE
    if any(
        _loop_body_rebinds_the_name(statement, name)
        for name in _store_target_names([statement.target])
    ):
        return UNREADABLE_VALUE
    return iterable.elts[-1]


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
