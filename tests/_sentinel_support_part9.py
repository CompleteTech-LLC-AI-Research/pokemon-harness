# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part9":
    raise ImportError(
        "tests._sentinel_support_part9 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
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
    # A store inside an always-true branch joins them from the other side:
    # `if True:` runs its body on every import, so the store settles the name
    # exactly as an unconditional one does. Left out, it settled nothing and a
    # later conditional store was read as the one in force -- which reported
    # the `TypeError` CPython raises for
    #
    #     if True:
    #         cs = list()
    #         def list(): return nullcontext()
    #
    # as a live header. Only a branch that is true for every binding counts, so
    # a condition this cannot read keeps its existing treatment.
    decidable = [
        entry
        for entry in entries
        if not entry[2]
        or isinstance(entry[0], ast.ExceptHandler)
        or _starred_store_decides_kind(entry, name, orders, index, function, by_index.get("header"))
        or _store_is_settled_before(entry, orders, index, function)
        or _statement_always_runs(entry[0], function)
    ]
    latest = max((orders[id(entry[0])] for entry in decidable), default=None)
    if latest is None:
        return None
    # #375. A conditional store that may *supersede* a carrier leaves the
    # name's final value undecidable, so the carrier must not be read as
    # still in force. This is checked BEFORE the #367 tie-break below,
    # because the tie-break's premise -- that `latest` identifies the value
    # actually in force -- is exactly what a superseding conditional store
    # denies. Declining here is the safe direction: `_entry_is_dead` then
    # treats the header as live rather than certifying a reachable assert as
    # swallowed.
    #
    #     def outer(flag, x):
    #         import os as cs
    #         if flag:
    #             cs = nullcontext()
    #         with cs:
    #             assert x != 1
    #
    # When the branch runs the name holds a real context manager and the
    # assert is **live**; when it does not, the module is in force and entry
    # raises `TypeError`. Either way the carrier alone does not settle the
    # header, and answering "dead entry" from it reports a live contract as
    # unreachable.
    #
    # Three restrictions keep the guard from over-claiming, each pinned by a
    # row that master already gets right:
    #
    # * It fires only when the settled store is itself a carrier. An
    #   unconditional store after the carrier has already settled the name and
    #   a later conditional store cannot unsettle it.
    # * A later conditional store that is itself pinned non-enterable, or is
    #   itself a carrier, cannot rescue the header on the path where it runs,
    #   so the carrier still decides every path:
    #
    #       def outer(flag, x):
    #           import os as cs
    #           if flag:
    #               cs = None
    #           with cs:
    #               assert x != 1
    #
    #   CPython raises `TypeError` for `flag=False` (the module) *and* for
    #   `flag=True` (`None`), so the assert is unreachable on both paths.
    # * `except ... as cs:` is excluded because the handler *deletes* the name
    #   on exit rather than superseding it, and the list above already keeps
    #   that shape decidable. Excluding it here stops a try/except that merely
    #   mentions the name from flipping a correct `defeated` to `enforced`.
    tied = [entry for entry in decidable if orders[id(entry[0])] == latest]
    # The guard fires when the *settled* store is a value the header could not
    # otherwise have entered -- a carrier, a pinned unenterable literal, `None`,
    # and so on -- and a *later conditional* store might make the header
    # enterable by superseding it with a real context manager. It must not be
    # restricted to carriers: with
    #
    #     def outer(flag, x):
    #         import os as cs
    #         cs = None
    #         if flag:
    #             cs = nullcontext()
    #
    # the settled value is `None` (not a carrier) yet a later conditional store
    # *does* make the header live for `flag=True`. Restricting the guard to
    # carriers read the stale `None` and reported that live header dead. The
    # "later store must itself be enterable" restriction below still keeps the
    # `cs = None; if False: cs = nullcontext()` family correctly dead, because
    # a conditional store that is itself pinned unenterable cannot rescue the
    # header on the path where it runs.
    settled_unenterable = any(
        orders[id(entry[0])] == latest and not _store_may_bind_enterable(entry, name, function)
        for entry in decidable
    )
    if settled_unenterable and any(
        entry[2]
        and not isinstance(entry[0], ast.ExceptHandler)
        and orders[id(entry[0])] > latest
        and not _statement_never_runs(entry[0], function)
        # A store inside a nested `def`/`lambda`/`class` body binds *that*
        # scope's local, so it cannot supersede this scope's carrier. Without
        # this the row below declined on a store the name cannot hold, which
        # reports `enforced` -- turning a genuinely unreachable assert into a
        # purportedly load-bearing one, the exact error the decline exists to
        # prevent. `global`/`nonlocal` are handled inside the helper: they
        # name the scope that actually owns the settled value, so a
        # `nonlocal` store here *does* supersede and a `global` one does not.
        and _store_is_in_scope(entry[0], function)
        and _store_may_bind_enterable(entry, name, function)
        for entry in entries
    ):
        return None
    if len(tied) == 1:
        return tied
    # #367. Two stores share the top-level statement, so `orders` cannot
    # separate them. Among the tied, the one this rule newly deemed *settled*
    # is the later write in the block: it is written into a `with` body, and
    # by the time a *subsequent* top-level statement is reached it has run, so
    # it is the value in force. Returning it alone keeps the dead-entry rule
    # from reading the earlier carried suppressor as if it were still in play.
    settled = [entry for entry in tied if _store_is_settled_before(entry, orders, index, function)]
    return settled or tied


def _starred_store_decides_kind(entry, name, orders, index, function, header=None):
    """A starred store must have run before this header and remain in force.

    A direct earlier sibling on the header's execution path has run whenever
    the header is reached. A loop target has run when the header is inside
    that loop's body. Future assignments, skipped branches, and loop targets
    read after a possibly empty loop provide no such guarantee.
    """
    statement = entry[0]
    if header is None or not _binds_starred_target(statement, name):
        return False
    prior = _statements_before_header(function, header)
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        reached = any(header is child for node in statement.body for child in ast.walk(node))
        if not reached and isinstance(statement, ast.For) and statement in prior:
            # A completed literal nonempty loop necessarily assigned its
            # target at least once. An empty or dynamically sized iterable
            # can leave the previous usable manager untouched.
            reached = (
                isinstance(statement.iter, (ast.Tuple, ast.List, ast.Set))
                and bool(statement.iter.elts)
                and not any(isinstance(element, ast.Starred) for element in statement.iter.elts)
            )
    else:
        reached = orders[id(statement)] == index and statement in prior
    if not reached:
        return False
    # Even a conditional intervening store can replace the list. Decline it
    # rather than deciding one path's value for every path to the header.
    bindings, _ = _store_bindings(function, {})

    def position(node):
        return node.lineno, node.col_offset

    return not any(
        other is not statement and position(statement) < position(other) < position(header)
        for other, _, _ in bindings.get(name, ())
    )


def _statements_before_header(function, header):
    """Direct earlier siblings on the syntactic path to this header."""
    prior = []
    node = function
    while node is not header:
        found = None
        for _, value in ast.iter_fields(node):
            children = value if isinstance(value, list) else [value]
            previous = []
            for child in children:
                if not isinstance(child, ast.AST):
                    continue
                if any(descendant is header for descendant in ast.walk(child)):
                    found = child
                    prior.extend(previous)
                    break
                if isinstance(child, ast.stmt):
                    previous.append(child)
            if found is not None:
                break
        if found is None:
            return []
        node = found
    return prior


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
    bindings, _raw_values = _store_bindings(function, bound, header)
    orders = {
        id(statement): _binding_order(function, statement)
        for entries in bindings.values()
        for statement, _, _ in entries
    }
    by_index = {"bindings": bindings, "orders": orders, "header": header}
    for index, statement in enumerate(function.body):
        for candidate in ast.walk(statement):
            if candidate is not header:
                continue
            return any(
                _entry_is_dead(item.context_expr, by_index, index, function, bound, module)
                for item in header.items
            )
    return False


def _is_suppressing_with(node, bound, function=None, owning=None, target=None):
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
        if any(
            _name_catches_assertion_error(name)
            for name in _suppression_names(call, bound, function)
        ):
            return True
    for argument in _entered_suppressions(node, bound):
        if not isinstance(argument, ast.Call):
            continue
        if not _is_suppression_call(argument, bound):
            continue
        if any(
            _name_catches_assertion_error(name)
            for name in _suppression_names(argument, bound, function)
        ):
            return True
    if function is None:
        return False
    for argument in _aliased_suppressions(node, function, bound, owning, target):
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
        if any(
            _name_catches_assertion_error(name)
            for name in _suppression_names(argument, bound, function)
        ):
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


def _falsy_literal(node, function=None):
    """A condition that is a literal false, so its body can never run."""
    if isinstance(node, ast.Constant) and not node.value:
        return True
    # An empty literal container is false for the same reason `False` is, and
    # `if flag and ():` never enters its body. CPython evaluates the tuple's
    # truth value at runtime; the empty form is decidable from the syntax, so
    # it belongs to the same rule rather than to the loop-emptiness check,
    # which is about a *container of values* rather than a condition.
    #
    # `ast.Dict` has to be asked about its own emptiness, not `.elts`: a dict
    # node carries `keys`/`values`, so reading `.elts` on it raised
    # `AttributeError` inside the decision function and crashed the analyzer on
    # `if {}: cs = nullcontext()`. A decision function must answer for every
    # node the caller can hand it, not raise.
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    # A zero-argument call that builds an *empty* container is falsy for the
    # same runtime reason `()` is: `if set():` never enters its body, exactly
    # like `if ():`.
    #
    # **Only** the no-argument form is decided. An argument-bearing call of the
    # same constructor is truthy whenever the argument produces an element:
    # `if set([1]):`, `if list((1,)):` and `if bytearray(b"x"):` all enter
    # their bodies in CPython, and `if dict(a=1):` does too. Reading them as
    # empty reported a live header dead -- the damaging direction -- because
    # :func:`_builtin_constructor_kind` answers the *type* a call produces and
    # the emptiness question was asked of that type rather than of the
    # argument list. The check belongs here, next to the container literals it
    # sits beside, and it is a property of the call's own argument list.
    return _is_zero_argument_empty_container(node, function)


def _is_zero_argument_empty_container(node, function=None):
    """Is ``node`` a ``set()``-shaped call that provably builds an empty one?

    Two properties have to hold together, and both were needed for the answer
    to be right:

    * the call must carry **no arguments**. `set([1])`, `list((1,))`,
      `dict(a=1)` and `bytearray(b"x")` are all non-empty, and reading them as
      empty reported a live header dead -- in a *condition* and in a *loop
      iterable* alike, because both call sites asked the type question through
      :func:`_builtin_constructor_kind` and never looked at the argument list;
    * the callee must be the real **builtin**, which
      :func:`_callee_is_shadowed` decides from the enclosing scope. A local
      ``def set(): return nullcontext()`` makes the call a different one, and
      a false "empty" there retires a header CPython enters.

    Both spellings of "empty builtin container" are decided from this one
    function, so the condition form and the loop form cannot disagree -- which
    is the property :data:`_EMPTY_CONSTRUCTOR_TYPES` was introduced to
    guarantee and an argument-blind helper would have broken.
    """
    if not isinstance(node, ast.Call) or node.args or node.keywords:
        return False
    return _builtin_constructor_kind(node, function) in _EMPTY_CONSTRUCTOR_TYPES


#: The ``range`` call shapes that are provably empty, keyed by argument count.
#: Only the shapes whose emptiness follows from the arguments themselves are
#: listed; a ``range`` whose bounds are not literals is left to the
#: conservative "a call is a call" rule, because a wrong answer here drops a
#: live contract from the sentinel's view.
_EMPTY_RANGE_ARGUMENT_COUNTS = frozenset({1, 2, 3})


def _is_empty_range_call(node, function=None):
    """Is ``node`` a ``range()`` call that provably yields nothing?

    ``for _ in range(0):`` has no iterations, so every statement in its body
    is unreachable -- the same property ``for _ in []:`` already has, and the
    reason :func:`_is_empty_literal_iterable` reads container literals. A
    zero-argument ``range()`` raises ``TypeError`` and a zero ``step`` raises
    ``ValueError``; both raise *before* the loop body, so the body is still
    unreachable, but they are not this helper's question and are declined so
    the raise is not silently answered as "empty".

    Emptiness is decided from the arguments with the ``range`` length rule
    rather than from a table of spellings, because the three shapes that are
    empty are not the three that read as zero: ``range(5, 0)`` and
    ``range(0, 5, -1)`` are both empty while ``range(0, 5)`` is not. Reading
    only the first argument answers "empty" for ``range(5, 0)`` by accident
    and for the wrong reason, so the bounds and the step are folded here:

        start = 0 / stop / step = 1        (one-argument form)
        start / stop                       (two-argument form, step 1)
        start / stop / step                (three-argument form)

    ``len(range(start, stop, step))`` is zero exactly when the step does not
    carry ``start`` to ``stop``, which is what the fold below computes. Every
    argument must be a readable literal integer: ``range(n)`` with a name for
    ``n`` is not decidable, and ``range(0.0)`` raises rather than yielding
    nothing, so a non-integer or unreadable argument answers "not provably
    empty" and leaves the loop's body counted as reached.

    The callee must be the real builtin, decided by
    :func:`_callee_is_shadowed` from the enclosing scope. A local
    ``def range(): return [1]`` makes the call a different one, and answering
    "empty" there retires a store CPython really performs.
    """
    if not isinstance(node, ast.Call) or _callee_is_shadowed(node.func, function, node):
        return False
    if not isinstance(node.func, ast.Name) or node.func.id != "range" or node.keywords:
        return False
    arguments = [_range_bound_is_integer(argument) for argument in node.args]
    if len(arguments) not in _EMPTY_RANGE_ARGUMENT_COUNTS or None in arguments:
        return False
    if len(arguments) == 1:
        # The one-argument form is `range(stop)`, which starts at 0 and steps
        # by 1 -- `len(range(stop))` is zero exactly when `stop <= 0`. Reading
        # it as `not stop` answered "not empty" for every negative bound, so
        # `for _ in range(-5):` -- which yields nothing, leaving the loop body
        # unreachable -- retired no store and the assert below was certified
        # defeated while CPython still evaluated it.
        #
        # This is the same fold the two- and three-argument forms already use,
        # and it has to stay the same *polarity*: `_range_reaches` answers
        # "does it yield", this helper answers "is it empty", so the result is
        # negated. Passing it through un-negated would answer "empty" for
        # `range(5)` and leave `range(0)` -- the row #456 exists to fix --
        # wrong.
        return not _range_reaches(0, arguments[0], 1)
    start, stop = arguments[0], arguments[1]
    step = arguments[2] if len(arguments) == 3 else 1
    if step == 0:
        # `range(0, 0, 0)` raises ValueError before the body, but "raises" is
        # not "yields nothing", and conflating the two would make this helper
        # answer a question it was not asked. Declining leaves the caller's
        # conservative rule in force.
        return False
    return not _range_reaches(start, stop, step)


def _range_bound_is_integer(node):
    """The integer value of a literal ``range`` bound, or ``None``.

    ``bool`` is excluded on purpose even though it is an ``int`` subclass:
    ``range(True)`` is a one-element range, and treating the literal as the
    integer ``1`` would make the rule answer a question it never documents.
    A negative bound is written as a ``UnaryOp`` and folded through
    :func:`_literal_value`, so ``range(0, -1)`` is decided rather than
    declined.
    """
    value = _literal_value(node)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _range_reaches(start, stop, step):
    """Does ``range(start, stop, step)`` yield at least one value?

    A step pointing up reaches ``stop`` exactly when ``start < stop``, and a
    step pointing down exactly when ``start > stop``; ``step == 0`` is
    declined by the caller. The comparison is made on the integers directly
    rather than through a division, so a very large bound cannot introduce a
    float rounding error and the answer is exactly the one CPython computes.
    """
    if step > 0:
        return start < stop
    return start > stop


def _is_empty_literal_iterable(node):
    """Is this iterable a literal container that provably yields nothing?

    ``for _ in []:`` keeps the assert in the AST and never runs it, which is the
    loop spelling of the ``if False:`` defeat already closed above. The literal
    must be *readable* rather than merely a literal: ``[]``, ``()``, ``{}``,
    ``(0,)``, ``(False, True)`` and ``''`` all have a decidable value, and the
    emptiness test is then exact.

    A call is deliberately *not* matched here even when it too yields nothing:
    this function answers only the question a literal settles on its own, and
    a wrong answer about a builtin drops a live contract from the sentinel's
    view -- the more damaging error. The empty builtin containers (``set()``,
    ``dict()``) and the empty ``range`` shapes are decided by their own
    helpers, which is why :func:`_loop_iterable_is_provably_empty` -- the
    predicate every loop call site asks -- ORs the four together. A call the
    module cannot decide, such as ``range(n)`` for an unreadable ``n`` or
    ``set(items)``, is left to the conservative "a call is a call" rule and
    keeps the loop's body counted as reached.

    A string literal belongs here for the same reason the containers do: ``''``
    is a ``Constant`` whose value ``_literal_value`` already reads exactly, and
    it iterates zero times just as ``[]`` does. Excluding it left ``for _ in
    '':`` as the one spelling where a loop whose body provably cannot run still
    reported its assert live (#439) -- a false-live, the same damaging
    direction as the defeat above, and inconsistent with the three sibling
    containers already handled here.

    ``bytes`` is admitted by the same test rather than by a separate rule: an
    empty ``bytes`` literal also yields nothing, and one test keeps the branch
    about "a literal sequence with a readable length" instead of about a
    specific type.

    An f-string with no replacement fields is the same empty string reached
    through a different node: ``f''`` parses to a ``JoinedStr`` with an empty
    ``values`` list rather than to a ``Constant``, so the branch above cannot
    see it (#449). It is still a *literal* with a decidable value, which is the
    boundary this function draws -- ``range(0)`` is out of scope because
    deciding it means reasoning about a builtin, not because it is a call.

    The guard is emptiness, not mere presence: only the no-``values`` form is
    decided. An f-string carrying replacement fields is left alone, since
    deciding it means reasoning about the expressions substituted into it --
    the ``range(0)`` problem again.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return not node.value
    if isinstance(node, ast.JoinedStr) and not node.values:
        return True
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return False
    return not _literal_value(node)


def _loop_iterable_is_provably_empty(node, function=None):
    """Does a loop over ``node`` reach its body exactly zero times?

    One predicate for the whole question, so the ``for`` call sites cannot
    disagree with each other about which spellings are decidable. Four
    sources are ORed, and each is narrow on its own terms:

    * :func:`_is_empty_literal_iterable` -- ``[]``, ``()``, ``{}``, ``''``,
      ``f''`` and the other literals whose value is readable off the syntax;
    * :func:`_is_empty_literal_string` -- the empty ``str`` literal, which
      :func:`_is_empty_literal_iterable` also reaches but which is asked
      separately at some call sites;
    * :func:`_is_zero_argument_empty_container` -- ``set()``, ``dict()``,
      ``list()`` and the other bare builtin constructors;
    * :func:`_is_empty_range_call` -- the ``range`` shapes whose emptiness
      follows from their own literal bounds.

    Anything else -- ``helper.items()``, ``set(items)``, ``range(n)`` -- may or
    may not be empty, so this answers ``False`` and the loop's body keeps
    counting as reached. That is the conservative direction: treating a
    possibly-empty loop as definitely-empty would report a live assert dead.
    """
    return (
        _is_empty_literal_iterable(node)
        or _is_empty_literal_string(node)
        or _is_zero_argument_empty_container(node, function)
        or _is_empty_range_call(node, function)
    )


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
