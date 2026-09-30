# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part2":
    raise ImportError(
        "tests._sentinel_support_part2 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


def _store_is_in_an_elif_link(statement, function=None):
    """Is this store written in an ``elif`` arm rather than a first-link ``if``?

    An ``elif`` is not a branch of its own. It is a continuation of the chain
    above it and runs only when *every* test before it failed, so a store in an
    ``elif`` arm cannot be read as the value in force on every call:

        cs = contextlib.nullcontext()
        if x:
            pass
        elif True:
            cs = contextlib.suppress(AssertionError)

    At ``x == 1`` the first arm is taken and ``cs`` still holds the
    ``nullcontext``; at ``x == 0`` the ``elif`` runs. Which store a later
    ``with cs:`` enters therefore depends on the call, and no single verdict
    about the header is right.

    The test is structural: an enclosing ``ast.If`` whose ``orelse`` contains
    the store's own block. A first-link ``if``, an ``else`` arm and an
    ``if True:`` body all fail it, which is what keeps them on their existing
    answers -- an ``else`` arm genuinely does run whenever its ``if`` does not,
    so together the two cover every call and the later store really is
    decisive there.
    """
    if function is None:
        return False
    for block in _enclosing_blocks(statement, function):
        if not isinstance(block, ast.If):
            continue
        for outer in _enclosing_blocks(block, function):
            if isinstance(outer, ast.If) and any(child is block for child in outer.orelse):
                return True
    return False


def _entry_suppresses_assertion_errors(entry, bound=None):
    """Does this store's recorded value provably swallow ``AssertionError``?

    The ambiguity rule treats a name bound on two paths as a possible
    suppressor, because it cannot tell which one the call took. That is only
    the safe reading when *every* candidate actually suppresses: if any of
    them holds a plain context manager, there is a call on which the assert
    fires, and reporting the header defeated would be a false-DEAD.

    So this asks the narrow question the ambiguity rule needs: is this value a
    *readable* suppression call that names ``AssertionError``? The
    ``_is_readable_suppressor`` gate is load-bearing rather than a re-test,
    because :func:`_suppression_names` deliberately answers ``["BaseException"]``
    for any call it cannot read -- and ``BaseException`` does catch
    ``AssertionError``, so dropping the gate would classify ``nullcontext()``
    as swallowing. A zero-argument manager, an unreadable call, or a name with
    no recorded value all answer False, which is the direction that keeps the
    assert load-bearing.
    """
    value = entry[1] if isinstance(entry, tuple) else None
    if value is None or not isinstance(value, ast.Call):
        return False
    if bound is None:
        # Without the scope's bindings the call cannot be resolved, so it is
        # not a *readable* suppressor. Answering False keeps the assert live,
        # which is the direction #441 needs when the reader cannot prove the
        # contract is disarmed.
        return False
    if not _is_readable_suppressor(value, bound):
        return False
    names = _suppression_names(value)
    return bool(names) and any(_name_catches_assertion_error(name) for name in names)


def _superseded_entry_suppresses_assertion_errors(entries, orders, competing, bound=None):
    """Does every store the competitor supersede also swallow ``AssertionError``?

    An `elif` arm only runs on the calls where every test above it failed. The
    calls that skipped it keep whatever the name held before, so the header is
    only *partly* defeated: it is defeated exactly when that earlier binding
    is itself a suppressor -- or when there was no earlier binding at all, in
    which case the skipped calls raise `NameError` on entry instead of
    running the body.

    Both bindings suppressing is therefore the fully-defeated case and must
    stay reported as defeated -- `cs = suppress(...)` followed by an `elif` arm
    storing another suppressor really does swallow the assert on every call,
    and treating it as partly live would report a disarmed contract as
    load-bearing.

    *Latest* is the operative word, and reading "was in force at some point
    during the skipped call" as the answer would get the rows backwards. A
    skipped call walks straight past every store, so the name ends on whatever
    the **last** one set -- not on whichever one is a suppressor:

        cs = contextlib.suppress(AssertionError)     # x=0: still the suppressor
        cs = contextlib.nullcontext()                # x=1: the nullcontext
        if x:
            pass
        elif True:
            cs = contextlib.suppress(AssertionError)  # x=0 only
        with cs:
            assert x != 1

    At ``x=1`` the header enters the ``nullcontext`` and the assert fires, so
    the contract is live. Asking "did *any* superseded store suppress?" would
    answer yes on the strength of the first line and report this as defeated
    -- the same false-DEAD #441 is about, reached by a different route.
    """
    competing_orders = {id(entry[0]) for entry in competing}
    newest = max(competing_orders)
    prior = [
        entry
        for entry in entries
        if id(entry[0]) not in competing_orders and orders[id(entry[0])] < newest
    ]
    if not prior:
        # Nothing was in force before the arm, so the arm's own store decides
        # every call: it runs when the arm is reached, and on the calls where
        # it is not reached the name is unbound, so `with cs:` raises rather
        # than running the body. The header is defeated outright.
        return True
    latest_prior = max(prior, key=lambda entry: orders[id(entry[0])])
    return _entry_suppresses_assertion_errors(latest_prior, bound)


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


def _match_capture_names_for(statement):
    """The names a single ``ast.Match`` statement captures."""
    names = []
    for node in ast.walk(statement):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            names.append(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            names.append(node.rest)
    return names


def _assigned_suppressors(function, bound, query=None):
    """Map each statement index to the suppressor names bound *by* it.

    ``contextlib.suppress(AssertionError)`` is an expression, and an expression
    can be given a name and entered later:

        cs = contextlib.suppress(AssertionError)
        with cs:
            assert 1 == 2

    By the time the ``with`` header is read, the call that built ``cs`` is gone,
    so the header holds a bare ``Name`` and the resolution rule has nothing to
    resolve. Only an assignment whose right-hand side is itself a readable
    suppressor is recorded, so a name bound to an ordinary call is never
    assumed to suppress. ``isinstance`` on a class attribute is deliberately
    not matched: that is an ``Attribute`` chain, not a bare name, and reaching
    through a class to a descriptor needs a runtime this check does not have.

    The result is keyed by statement index rather than merged into one dict,
    because *order* decides whether the alias is bound yet. ``with cs:`` placed
    **before** ``cs = suppress(...)`` raises ``NameError`` on entry -- the test
    fails loudly rather than passing quietly -- so it is not a defeat, and a
    single merged dict would have called it one. Only the assignments that
    precede the ``with`` are visible to it.

    Assignments are collected from the whole function body rather than from
    ``function.body`` alone, so a suppressor bound inside an ``if``, a loop, a
    ``try`` or a nested ``with`` is still found. Restricting the walk to the
    top level is the escape recorded in #308: a suppressor's reach does not
    depend on the indentation it was written at, and reading only the
    outermost statements reports a swallowed assert as *enforced*.

    Bindings are split by whether two of them can be reached on the same path.
    Only *competing* bindings make a name ambiguous:

    * **Unconditional** -- sitting directly in the function body. These run in
      source order, so at any ``with`` only the last one can be live, and it
      supersedes the rest rather than competing with them.
    * **Conditional** -- nested in an ``if``, a loop, a ``try`` or a ``with``.
      Two of these genuinely can reach the same ``with`` on different paths, so
      only these make a name ambiguous.

    The distinction is load-bearing in both directions. Reading two
    *sequential* suppressor bindings as competing would report a live assert
    as dead -- the opposite error from the one #308 criterion 1 is about, and
    the more damaging one here, because it drops a real contract out of the
    sentinel's view. Reading two *competing* bindings as sequential would pick
    a winner by source order and certify a disarmed assert as load-bearing.
    Per #308 criterion 1 a competing name is *unreadable*, and the safe
    direction is to treat it as a defeat.

    EVERY binding of a name is recorded, not only the suppressor ones. That is
    what makes a superseding store visible at all: a name rebound to an
    ordinary call is recorded and supersedes like any other value, so

        cs = contextlib.suppress(AssertionError)
        cs = helper.make()

    resolves to "not an alias" instead of leaving the first binding as the sole
    known one. Recording only the suppressor bindings would make that
    sequence look like a lone alias and report the live assert as dead.

    An assignment *expression* is a binding too, and is collected here for the
    same reason:

        with (cs := contextlib.suppress(AssertionError)):
            assert 1 == 2
        with cs:
            assert 1 == 2          # also swallowed -- `cs` is still bound

    A ``NamedExpr`` is not an ``ast.Assign``, so before #324 it was invisible
    to this walk entirely and that second assert was reported enforced -- the
    damaging direction. Recording it here rather than in the header logic is
    what makes the supersession above apply to it unchanged: a later
    ``cs = nullcontext()`` is an ordinary store, it wins on order, and the
    carried suppressor does not outlive it. Recording the binding *only* where
    the header is read -- the approach #323 took -- gets the re-entry case and
    loses the supersession case, because nothing ever retires the value.
    """
    # Every binding of every name, tagged with whether that store can compete
    # with another, so that supersession and ambiguity stay told apart.
    bindings, raw_values = _store_bindings(function, bound, query)
    orders = {
        id(statement): _binding_order(function, statement)
        for entries in bindings.values()
        for statement, _, _ in entries
    }
    assigned = {}
    for index in range(len(function.body)):
        for name, entries in bindings.items():
            seen = [entry for entry in entries if orders[id(entry[0])] <= index]
            if not seen:
                continue
            value = _resolve_bindings(seen, bound, orders, index, function)
            # #441. `_NOT_A_SUPPRESSOR` is a *record* that the name is bound
            # and what it carries is not a suppressor, not a value to hand
            # downstream. It has always been dropped here, which was correct
            # when only `None` reached this point; `_resolve_bindings` can now
            # return the marker itself to say "bound on two paths, and the one
            # reached without a suppressor provably does not swallow
            # `AssertionError`" -- a live header. Recording it would carry the
            # marker forward, and the two consumers of this table would each
            # misread it: `_aliased_suppressions` looks names up to append
            # them to a header's argument list, where a marker is not a
            # suppression, and `_bindings_before` advances a carried set from
            # it, where a stale suppressor must not survive. Both already
            # handle the marker correctly when it is absent -- that is what
            # #367's `_NOT_A_SUPPRESSOR` is for -- so the value is simply not
            # recorded, and the name reads as carrying nothing here.
            if value is not None and value is not _NOT_A_SUPPRESSOR:
                assigned.setdefault(index, {})[name] = value
            elif index and name in assigned.get(index - 1, {}):
                # #367. The name is still bound here, and what it carries is
                # deliberately *not* a suppressor. Recording that explicitly
                # is what stops `_aliased_suppressions`' `bound_so_far` from
                # carrying the previous index's suppressor forward:
                #
                #     with (cs := contextlib.suppress(AssertionError)):
                #         cs = contextlib.nullcontext()
                #         assert x != 1
                #     with cs:                  # `cs` is the nullcontext
                #         assert x != 2
                #
                # At the index of the *first* `with` the name really does carry
                # the suppressor, so that index records it. The next index
                # resolves the tie to the `nullcontext`, which is not a
                # suppressor, and without this line the walk would find no
                # entry for it and read the stale suppressor from the previous
                # index -- reporting a live assert defeated. The marker is
                # `_NOT_A_SUPPRESSOR` rather than a dropped key precisely
                # because "not a suppressor" and "not bound" are different
                # answers here.
                assigned.setdefault(index, {})[name] = _NOT_A_SUPPRESSOR
    # #367. A store in a statement's *body* runs before the next statement, so
    # a header in that next statement must not read the value this statement's
    # own header entered:
    #
    #     with (cs := contextlib.suppress(AssertionError)):
    #         cs = contextlib.nullcontext()
    #         assert x != 1
    #     with cs:                  # `cs` is the nullcontext
    #         assert x != 2
    #
    # At the index of the first `with`, `_resolve_bindings` answers with the
    # `suppress` -- correct, because that header is evaluated before the body
    # store happens. But the body store *has* run by the time the next
    # statement is reached, so the value that statement's headers read is the
    # `nullcontext`. Without this pass the next index records
    # `_NOT_A_SUPPRESSOR` for `cs`, and `_aliased_suppressions`, which advances
    # its carried set from `assigned[index]` at the end of each statement,
    # hands the *first* `with`'s suppressor forward instead.
    #
    # The pass is deliberately narrow: only a name whose latest store *at or
    # before* `index` is settled past `index` -- a store in a `with` body that
    # has already run by the next statement -- is downgraded. Everything else
    # keeps the value this statement's own header entered, which is what a
    # header written *before* the body store must see.
    for index in range(len(function.body) - 1):
        following = index + 1
        for name, entries in bindings.items():
            value = assigned.get(index, {}).get(name)
            if value is None or value is _NOT_A_SUPPRESSOR:
                continue
            seen = [entry for entry in entries if orders[id(entry[0])] <= index]
            if not seen:
                continue
            latest = max(orders[id(entry[0])] for entry in seen)
            if not any(
                _store_is_settled_before(entry, orders, following, function)
                for entry in seen
                if orders[id(entry[0])] == latest
            ):
                continue
            resolved = _resolve_bindings(
                [entry for entry in entries if orders[id(entry[0])] <= following],
                bound,
                orders,
                following,
                function,
            )
            if resolved is None:
                # Recorded against THIS index, not the next one.
                # `_aliased_suppressions` advances its carried set from
                # `assigned[index]` at the end of each statement, so this is
                # the entry the *next* statement's headers read. Recording it
                # against `following` would land one statement too late and
                # leave the stale suppressor in force for exactly one header.
                assigned.setdefault(index, {})[name] = _NOT_A_SUPPRESSOR
    # The raw table has to be built for the whole function, not assembled as
    # the recording walk proceeds. #333: an assignment expression in a `with`
    # header can alias a name whose own store `ast.walk` has not reached yet,
    # because `ast.walk` is breadth-first and visits the header before stores
    # that precede it in source. `_store_bindings` applies the dereference to
    # every recorded store and hands back the table it used, so the map and
    # the table can never disagree about what a name carries.
    return assigned, raw_values


def _store_bindings(function, bound, query=None):
    """Map every name ``function`` binds to its ``(stmt, value, cond)`` stores.

    Returns ``(bindings, raw_values)``. ``raw_values`` is the undereferenced
    right-hand side of every value-bearing store, keyed the same way; the
    recorded ``value`` has already had any alias followed through it. The
    caller that resolves a *header* needs the raw side to follow a chain of
    its own, which is why both are handed back rather than only the map.

    The raw per-name store lists, before any resolution to a suppressor. Rules
    that need to know *what value* a name received -- rather than whether that
    value is a known suppressor -- read this instead, because
    :func:`_assigned_suppressors` resolves a non-suppressor to ``None`` and the
    two meanings of that ``None`` are exactly what :func:`_entry_is_dead`
    needs to tell apart.

    The recorded ``value`` is the store's right-hand side with any alias
    already followed, so a name bound to a suppressor -- directly or through a
    chain -- is recorded as the suppressor it carries. See :func:`_deref_alias`
    for why the chain is resolved against the whole function at once.
    """
    bindings = {}
    # #333: the right-hand side of every store, keyed by the name it binds,
    # gathered BEFORE the recording walk so an assignment expression can be
    # resolved against a name whose own store has not been visited yet.
    # `ast.walk` is breadth-first, so a `with` header is visited before stores
    # that precede it in source; resolving against the table as it fills would
    # leave a two-hop chain (`first` -> `second` -> `(cs := second)`) stuck at
    # an unrecorded name and report a swallowed assert as live.
    #
    # The table covers ALL stores, not just walruses, and the dereference below
    # is applied to every one of them. The walrus path needs the table resolved
    # at a `with` HEADER; the ordinary `with alias:` path resolves at the STORE
    # SITE, where the table only has to be filled up to that point. Both need
    # the same whole-function view to follow a name to its binding, so they
    # share the table rather than keeping two in step by hand.
    #
    # "Whichever binding ran last" is not the same as "whichever binding is
    # written last". A read only sees the stores that precede it, so both
    # resolution sites pass the position they are resolving at and let
    # :func:`_last_store_before` pick the entry that had actually run.
    raw_values = _raw_store_values(function, query, bound)
    orders = {
        id(statement): _binding_order(function, statement)
        for entries in raw_values.values()
        for statement, _ in entries
    }
    for statement in ast.walk(function):
        targets = []
        value = None
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
            value = statement.value
        elif isinstance(statement, ast.NamedExpr):
            # #324: an assignment expression is a binding like any other store.
            # It is not an `ast.Assign`, so before this it was invisible here
            # and a `with (cs := suppress(...)):` header left `cs` bound for
            # the rest of the scope without any later `with cs:` seeing it --
            # a swallowed assert reported live. Recording it through the same
            # machinery means the per-index resolution, the supersession by a
            # later store, and the non-suppressor rule below all apply to it
            # unchanged, which is what keeps a later `cs = nullcontext()` from
            # inheriting the carried suppressor (#308).
            targets = [statement.target]
            value = statement.value
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            # #324: a loop target is a store like any other. `for cs in ():`
            # rebinds the name on every path that reaches the loop, so a
            # carried suppressor must not survive it. Recording only
            # `ast.Name` targets left this invisible and let a stale
            # suppressor outrank the loop's own binding.
            targets = [statement.target]
            value = _loop_value_source(statement, query, bound)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            # #324: `with ... as cs:` is a store too, and the item expression
            # is what the `with` would evaluate. A walrus carried in from
            # earlier must be retired by it.
            for item in statement.items:
                if item.optional_vars is not None:
                    targets.append(item.optional_vars)
                    value = None
        elif isinstance(statement, ast.ExceptHandler):
            # #324: `except E as cs:` binds `cs`, and CPython deletes the name
            # when the handler exits, so a carried suppressor must not outlive
            # it either.
            if statement.name is not None:
                targets = [ast.Name(id=statement.name, ctx=ast.Store())]
                value = None
        elif isinstance(statement, ast.Delete):
            # #324: `del cs` unbinds the name outright. Recording it as a store
            # of `None` is what makes the existing supersession rule retire any
            # earlier binding of that name.
            targets = list(statement.targets)
            value = None
        else:
            continue
        # A store written directly in the function body runs on every path;
        # one nested inside a block runs on some paths only.
        # A `NamedExpr` never sits directly in the function body, so for it the
        # question is whether the *top-level statement that contains it* is
        # written directly in the body. A walrus in a top-level `with` header
        # therefore runs on every path (it is evaluated whenever that `with`
        # is reached), while a walrus inside an `if` or a loop still runs on
        # some paths only.
        if isinstance(statement, ast.NamedExpr):
            conditional = _walrus_is_conditional(function, statement)
        else:
            conditional = statement not in function.body
        for name in _store_target_names(targets):
            # Order is load-bearing, and it is three steps, not two. The value a
            # destructuring target receives can be reached through a name at
            # either level, and each level needs its own deref:
            #
            #     t = (contextlib.suppress(AssertionError),)
            #     cs, = t                 # the RHS is a Name bound to a container
            #     with cs:
            #         assert x != 1      # swallowed
            #
            #     a, b = b, a             # the *element* is itself a Name
            #     with a:
            #         assert x != 1      # swallowed, after the swap
            #
            # Deref-first alone fixes the first shape and breaks the second: on
            # `(b, a)` the deref is a no-op (a tuple is not a name), so the
            # element `b` is selected but left as a bare `Name`, which is not a
            # readable suppressor and reports the assert live (#381/#382).
            # Select-first alone fixes the second and breaks the first, because
            # the RHS reads as a bare `Name` and no container is ever found
            # (#374). So the right-hand side is dereferenced first, the element
            # is picked out of that resolved container, and the element is
            # dereferenced in turn when it is itself a name.
            resolved = _deref_alias(
                value,
                raw_values,
                orders,
                _binding_order(function, statement),
                name,
            )
            element = _value_bound_by(targets, resolved, name)
            if isinstance(element, ast.Name):
                # The selected element is a name, so it stands for whatever it
                # was bound to rather than for itself. Resolving it here is what
                # keeps a swapped pair -- where both names already hold
                # suppressors -- from reading as a non-suppressor element.
                element = _deref_alias(
                    element,
                    raw_values,
                    orders,
                    _binding_order(function, statement),
                    name,
                )
                if isinstance(element, ast.Name):
                    # The selected element is a name, so it stands for whatever
                    # it was bound to rather than for itself. Resolving it here
                    # is what keeps a swapped pair -- where both names already
                    # hold suppressors -- from reading as a non-suppressor
                    # element.
                    element = _deref_alias(
                        element,
                        raw_values,
                        orders,
                        _binding_order(function, statement),
                        name,
                    )
            bindings.setdefault(name, []).append(
                (
                    statement,
                    element,
                    conditional,
                )
            )
    # A `match` capture is the one store form that is not reachable from a
    # statement's target list, so it cannot ride along in the loop above.
    #
    # `conditional` is False only when the capture is *guaranteed* to run --
    # a last, irrefutable, unguarded clause (`_capture_always_binds`). For the
    # refutable shapes the capture is marked conditional instead, because a
    # clause that is not selected never binds anything. Marking those
    # unconditional (the original #324 behaviour) made the capture supersede
    # the earlier walrus on every path, so a subject that matched no clause at
    # all still retired the carried suppressor and reported the assert below it
    # as live while it was really swallowed. See #342.
    for name, statement in _match_capture_names(function).items():
        conditional = not _store_retires(statement, name)
        bindings.setdefault(name, []).append((statement, None, conditional))
    # #359. The four string-field carriers (`import os as cs`, `def cs`,
    # `class cs`, ...) bind a name the loop above never sees, because their
    # name is a string field of a node rather than a target. They are recorded
    # here with the runtime kind their value is pinned to, so that
    # `_entry_is_dead` can rule on them the way it rules on a literal: a module,
    # a class and a function are not context managers, so entering one raises
    # before the assert runs.
    #
    # `conditional` is the same test the target-list loop uses -- a binding
    # nested in a block may not have run -- so a carrier inside an `if` cannot
    # be read as having retired the name on a path where it never executed.
    for name, (statement, kind) in _carrier_runtime_kinds(function).items():
        conditional = statement not in function.body
        bindings.setdefault(name, []).append((statement, kind, conditional))
    # A name bound by exactly one readable suppressor is a known alias. A name
    # bound by several is ambiguous -- see the docstring (#308 criterion 1) --
    # and must NOT be resolved by source order. It is recorded as an
    # `AMBIGUOUS` marker instead of being dropped: dropping it would fall back
    # to "this name is not a known suppressor", which reports the assert as
    # *enforced*, and that is the damaging direction. On the ambiguous path the
    # rule genuinely cannot tell which target was applied, so it treats the
    # name as a suppression and says so (#308 criterion 1: over-reporting is the
    # safe direction here).
    #
    # Ambiguity is judged over the whole function: a nested binding and a
    # top-level one are the same name, and a rule that let the source order
    # pick between them would report the verdict for one path only.
    # Resolution is per top-level statement rather than once for the whole
    # function, because "which store is live" is a question about one `with`
    # and not about the function. A `with` placed *between* two stores sees
    # the earlier one:
    #
    #     cs = contextlib.suppress(AssertionError)
    #     with cs:              # <- this assert is swallowed
    #         assert 1 == 2
    #     cs = helper.make()
    #
    # Resolving once for the function would answer that `with` with the *last*
    # store and certify a disarmed assert as load-bearing. So each index
    # resolves from the bindings that precede it alone.
    return bindings, raw_values


def _resolve_bindings(entries, bound, orders, index=None, function=None):
    """Resolve one name from the bindings in effect at a single ``with``.

    ``entries`` are ``(statement, value, conditional)`` triples, already
    filtered to the stores that run at or before the ``with`` in question.
    ``index`` is the position of that ``with`` among the function's top-level
    statements and ``function`` the scope it sits in. ``index`` and
    ``function`` together are what tell a *settled* store from one still
    waiting on a branch; ``function`` is also what tells a real builtin from
    a name the function rebinds.
    """
    # An unconditional store runs on *every* path, so the last one of those is
    # the value in force unless some conditional store comes after it. Only the
    # conditional stores that are ordered later can still compete with it;
    # earlier ones were overwritten by it:
    #
    #     if p:
    #         cs = contextlib.suppress(AssertionError)   # competing
    #     else:
    #         cs = contextlib.suppress(ValueError)        # competing
    #     cs = helper.make()                              # runs last, everywhere
    #     with cs:                                        # `cs` is not a suppressor
    #
    # Treating the two branches above as still ambiguous here would report a
    # live assert as swallowed. Counting a conditional store written *after* an
    # unconditional one as competing is the other half: it genuinely can be the
    # last store to run.
    unconditional = [entry for entry in entries if not entry[2]]
    latest = max((orders[id(entry[0])] for entry in unconditional), default=None)
    competing = [
        entry for entry in entries if entry[2] and (latest is None or orders[id(entry[0])] > latest)
    ]
    # A `for cs in ...:` target and an assignment to `cs` in that loop's own
    # body are not two competing bindings. The body runs *after* the target on
    # every pass, so whichever it stores is the value left behind and the
    # target's element is gone. Counting both made
    #
    #     import os as cs
    #     if flag:
    #         for cs in (None,):
    #             cs = nullcontext()
    #
    # ambiguous and reported a live header dead: the name really is a context
    # manager whenever the loop runs. The target is dropped from the competing
    # set when its own body rebinds the name, leaving the body's store as the
    # single conditional binding -- which supersedes the carried carrier.
    #
    # The collapse is only sound while the body's own store is what actually
    # answers the header, and that store is not always readable. Dropping the
    # target while the body binds `contextlib.suppress(AssertionError)` and
    # then answering from the *target* retired a suppressor that is really in
    # force on the path where the loop runs:
    #
    #     cs = None
    #     if flag:
    #         for cs in (None,):
    #             cs = contextlib.suppress(AssertionError)
    #
    # Once the target is out of the competing set, the body's store is the one
    # answer, so the collapse is kept only when the body's last rebind is a
    # store this can read. An unreadable one leaves the target in place, and
    # the two competing bindings are then reported ambiguous -- which is the
    # safe direction, because the name is genuinely a suppressor on the path
    # where the loop runs.
    collapsed_entries, collapsed = _collapse_loop_targets_into_bodies(competing)
    if collapsed:
        competing = collapsed_entries
    # #441. A single competing store is normally decisive, and deliberately so:
    # a plain `if flag: cs = nullcontext()` really does supersede whatever the
    # name held before it, on the one path that matters, and the shipped rows
    # pin that assert as live. An `elif` link is a different question, because
    # the arm runs only when *every* test above it failed:
    #
    #     cs = contextlib.nullcontext()
    #     if x:
    #         pass
    #     elif True:
    #         cs = contextlib.suppress(AssertionError)
    #     with cs:
    #         assert x != 1
    #
    # At `x == 1` the `if` arm is taken, the `elif` never runs, `cs` is still
    # the `nullcontext`, and the assert really fires. So the name holds the
    # suppressor on *some* calls and a live manager on others: which store is
    # in force is not decidable from the source. Letting the later store win
    # outright reads the header as swallowed on *every* call, which is a
    # false-DEAD -- #308 criterion 1's damaging direction -- because the path
    # that skipped the arm really does enter the plain manager.
    #
    # `AMBIGUOUS_SUPPRESSOR` is not the answer either: it means "possibly any
    # suppressor", and the rule that reads it reports the assert defeated.
    # That is sound only while every candidate really does swallow
    # `AssertionError`. Here the *other* path provably does not, so the header
    # has to stay live, and `_NOT_A_SUPPRESSOR` records exactly that: the name
    # *is* bound, but not in a way that disarms the contract on every call.
    # #367's marker is the one to reuse rather than a new sentinel. It already
    # separates "bound and carries no suppressor" from "not bound at all", and
    # `_aliased_suppressions` already filters it out of the header walk, so a
    # second marker would be a second spelling of the same record.
    #
    # Scoped strictly to an `elif` link. A first-link `if`, an `else` arm and
    # an `if True:` body all keep the existing single-competitor answer, and
    # #403's `if False: ... elif True:` arm is settled upstream by
    # `_statement_always_runs` and so never reaches this as a competitor at all.
    if (
        len(competing) == 1
        and not any(_entry_may_be_an_unrun_capture(entry) for entry in competing)
        and _store_is_in_an_elif_link(competing[0][0], function)
        and _entry_suppresses_assertion_errors(competing[0], bound)
        and not _superseded_entry_suppresses_assertion_errors(entries, orders, competing, bound)
    ):
        # The arm itself swallows `AssertionError`, so the call that takes
        # it is defeated; the call that skips it keeps the earlier binding
        # and still fires. Both outcomes occur, so the contract is not
        # enforced on every call and the header has to stay live.
        return _NOT_A_SUPPRESSOR
    if len(competing) > 1 or any(_entry_may_be_an_unrun_capture(entry) for entry in competing):
        # More than one conditional binding can reach this `with` on different
        # paths, so which suppressor is live is undecidable. Recorded as an
        # `AMBIGUOUS` marker rather than dropped: dropping it would fall back
        # to "this name is not a known suppressor", which reports the assert as
        # *enforced* -- the damaging direction.
        #
        # A *single* `match` capture is the #342 case and it is decided the
        # same way, because the question is identical: the clause may simply
        # not be selected, so the name at this header is either the captured
        # value or the one it supersedes. Which is in force is not decidable
        # from the source, and picking the later store would retire a
        # suppressor that is still bound on the path that skipped it.
        # `AMBIGUOUS` keeps the answer on the safe side: the name is treated
        # as a possible suppressor, so the assert is reported defeated even
        # though one of the two readings is a live contract.
        #
        # A single *non-capture* conditional store is deliberately NOT
        # ambiguous. A plain `if flag: cs = nullcontext()` supersedes a
        # carried walrus on the path that runs, and the shipped rows pin the
        # assert under the following `with` as live. Widening the rule to
        # every single competing store regressed two of those rows, so the
        # widening is scoped to captures.
        return AMBIGUOUS_SUPPRESSOR
    # Otherwise nothing competes with anything: the stores that can be last are
    # a single one, so the highest-ordered entry is what the `with` enters.
    # `max` runs over every entry, not just the unconditional ones: a
    # conditional store ordered *after* the latest unconditional store is
    # exactly the supersession #323 models, and dropping it would resurrect the
    # stale suppressor.
    #
    # #367: `max` returns the FIRST maximal entry, and two stores inside one
    # top-level statement share an order by construction. So where the maximum
    # is attained more than once, `max` resolves the tie by walk position --
    # a question the source does not answer. The sharp case is a `for`/`else`,
    # where exactly one branch runs and which one is an input:
    #
    #     for i in items:
    #         first = contextlib.suppress(AssertionError)
    #     else:
    #         first = contextlib.nullcontext()
    #     with (cs := first):
    #         assert x != 1
    #
    # `max` returned the *first* maximal entry, so a `for`/`else` pair resolved
    # to whichever branch the walk reached first -- fixed regardless of which
    # branch runs. The `for`/`else` above is the sharp case: on `items == []`
    # the `else` runs and the name is a `nullcontext`, so the assert is LIVE,
    # while on `items == [1]` the body runs and the name is a `suppress`, so
    # the assert is swallowed. The interpreter disagrees with itself across
    # the two inputs, so no single verdict is right, and walk order is not the
    # thing that decides it.
    #
    # A tie of that kind -- every member conditional -- is already declined by
    # the `competing` branch above, and cannot reach here: with no
    # unconditional entry at the maximum order, two or more conditional
    # entries at that order are exactly two or more *competing* stores, so
    # that branch returns first. An earlier cut of this repair therefore
    # carried a second, all-conditional tie test here; it was unreachable in
    # every enumerated shape (630 order/conditional combinations, zero
    # reachable) and has been removed rather than left as a branch no mutation
    # can kill.
    # The tie-break below runs over the *surviving* candidates, not over every
    # entry. A loop target that its own body rebinds shares the body's order --
    # both are ordered by the top-level `for` that contains them -- so reading
    # `entries` here reinstated the very binding `_collapse_loop_targets_into_bodies`
    # had just retired, and the #367 tie-break then declined a name the collapse
    # had made decidable:
    #
    #     def outer(x, flag):
    #         import os as cs
    #         if flag:
    #             for cs in (None,):
    #                 cs = nullcontext()
    #         with cs:
    #             assert x != 1
    #
    # The collapse reduces the two conditional stores to the body's alone, but
    # the maximal order over *all* entries is still 1 with both the `for`
    # target and the body store tied at it. `_latest_write_in_block` cannot
    # separate stores inside one loop (neither has run in a way that settles
    # the other), so it returns `None` and the resolution became
    # `AMBIGUOUS` -- read downstream as a *possible suppressor*, which retired
    # a header CPython really enters. The maximum is therefore taken over the
    # same set the collapse produced, matching the no-tie branch below.
    candidates = competing if competing else entries
    latest = max(orders[id(entry[0])] for entry in candidates)
    tied = [entry for entry in candidates if orders[id(entry[0])] == latest]
    if len(tied) > 1:
        # Mixed tie: an unconditional store and a conditional one share the
        # order. `_binding_order` cannot separate them -- it keys a store by the
        # top-level statement containing it, so two stores in one block compare
        # equal by construction -- but source position can, and a genuine
        # supersession *is* written later:
        #
        # Treating this as ambiguous instead would re-open #323:
        #
        #     with (cs := suppress(AssertionError)):
        #         cs = nullcontext()      # same top-level statement, so the
        #         assert x != 1           # same order as the walrus above
        #     with cs:                    # `cs` is the nullcontext
        #         assert x != 2
        #
        # The unconditional walrus runs on every path; the assignment runs only
        # when the body is entered, and once it has, the name is the
        # `nullcontext`. Reporting the tie as ambiguous would call that live
        # assert defeated.
        # #367: "written later" is necessary but not sufficient. A store inside
        # a *branch* of the block may never run, so it cannot be called the
        # value in force:
        #
        #     with (cs := contextlib.suppress(AssertionError)):
        #         assert x != 1
        #         if flag:
        #             cs = contextlib.nullcontext()
        #     with cs:
        #         assert x != 2
        #
        # Measured on CPython 3.12.14, `cs` is a `suppress` for both values of
        # `flag`: the `with` statement's own item expression is what enters,
        # and the body's rebinding of the name does not change the manager the
        # `with` already holds. The second assert is swallowed either way, so
        # it must be reported defeated. Treating the branch as the later write
        # reads the `nullcontext`, retires the suppressor, and reports that
        # swallowed assert live -- the damaging direction.
        #
        # So the last write counts only when it is one that has *run*: either
        # it is unconditional, or it is the settled in-body store that
        # `_store_is_settled_before` recognises. Anything else leaves the tie
        # undecided, which is declined.
        latest_write = _latest_write_in_block(tied, orders, index, function)
        if latest_write is None:
            return AMBIGUOUS_SUPPRESSOR
        return _readable_store_value(latest_write[1], bound)
    else:
        # No tie, so the single highest-ordered candidate is the value in
        # force.
        #
        # The sort runs over `candidates`, the *surviving* set the collapse
        # above produced -- for the same reason the tie-break does. A loop
        # target that its own body rebinds shares the body's source order, so
        # taking the maximum over every entry let the retired target win a tie
        # it should not have been in, and the suppressor stored by the body was
        # dropped in favour of the element the target yielded:
        #
        #     cs = None
        #     if flag:
        #         for cs in (None,):
        #             cs = contextlib.suppress(AssertionError)
        #
        # The collapse above is what makes the body's store the only
        # candidate, so the candidate set has to be the same one the collapse
        # produced. Reading `entries` here reinstated the very binding that
        # was just retired.
        last = max(candidates, key=lambda entry: orders[id(entry[0])])[1]
        return last if _is_readable_suppressor(last, bound) else None


def _latest_write_in_block(tied, orders=None, index=None, function=None):
    """The store written last among entries that share one top-level statement.

    #367. `_binding_order` keys a store by the top-level statement containing
    it, so two stores written in the same block compare equal *by construction*
    and the tie has to be broken some other way. Source position is the one
    that answers the question the tie poses: of the stores in this block, which
    ran last? A later write to a name overwrites an earlier one, so the value
    in force afterwards is the last write's -- not the first, and not the one
    that happens to be walked first.

    That is the whole correction. `ast.walk` is breadth-first, so it reaches a
    `with` header's named expression *after* the statements in that header's
    own body, and it reaches a body's `if` branch before the statements that
    precede it. Reading the first maximal entry therefore picked whichever
    store the traversal happened to visit first, which is a property of the
    walk and not of the program:

        with (cs := contextlib.suppress(AssertionError)):
            cs = contextlib.nullcontext()
            assert x != 1
        with cs:
            assert x != 2

    `cs = nullcontext()` is written *after* the walrus, so `cs` is the
    `nullcontext` and the second assert is live. Resolving the tie by walk
    position reads the walrus instead, resurrects the stale suppressor, and
    reports that live assert defeated.

    A write only counts as *last* if it has run. An unconditional store has.
    A conditional one has only if it is the settled in-body store
    :func:`_store_is_settled_before` recognises -- written in a `with` body
    that has already completed by the time the queried header is read. A
    conditional store inside an `if`, a loop or a `try` may never run, so it
    cannot be called the value in force and the tie stays undecided.

    Returning ``None`` -- no single last write, or the last write is one that
    may not have run -- declines the name rather than guessing. The caller
    reports that as a defeat, the safe side (#308 criterion 1).
    """

    def position(node):
        return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))

    def is_conditional(entry):
        # The binding table records `(statement, value, conditional)` triples;
        # `_assigned_suppressors`' raw table records `(statement, value)` pairs
        # and carries no flag. A pair is treated as *conditional* here, which
        # is the conservative reading in the one direction that matters: it
        # cannot be called a store that provably ran, so it can neither win a
        # tie outright nor license the fallback to an earlier write. A raw tie
        # is therefore declined, which is the right answer -- the raw table is
        # consulted for *which* right-hand side a read sees, and two stores in
        # one block give it nothing to choose between.
        return True if len(entry) <= 2 else entry[2]

    latest = max(position(entry[0]) for entry in tied)
    winners = [entry for entry in tied if position(entry[0]) == latest]
    if len(winners) != 1:
        return None
    winner = winners[0]
    if not is_conditional(winner):
        return winner
    if (
        orders is not None
        and index is not None
        and function is not None
        and _store_is_settled_before(winner, orders, index, function)
    ):
        return winner
    # The last write has not run at this position -- the header being resolved
    # is the very statement whose body contains it:
    #
    #     with (cs := contextlib.suppress(AssertionError)):
    #         cs = contextlib.nullcontext()   # has not run yet
    #         assert x != 1
    #
    # The value at THIS header is still the walrus. So the tie falls back to
    # the last write that *has* run, which is what the read actually sees.
    #
    # The fallback is only sound when an unconditional store is in the tie --
    # something that provably ran on every path. With no such store, every
    # member may not have run and the name genuinely is one of several values:
    #
    #     for item in items:
    #         first = contextlib.suppress(AssertionError)
    #     else:
    #         first = contextlib.nullcontext()
    #     with (cs := first):
    #         assert x != 1
    #
    # Exactly one of the two bodies runs, and which one is an input. Measured on
    # CPython 3.12.14 the assert is swallowed when the `for` body ran and fires
    # when the `else` did, so the interpreter disagrees with itself and no
    # single verdict is right. The name is declined, which reports a defeat --
    # the safe side (#308 criterion 1). Falling through to whichever store is
    # written later would pick the `nullcontext` by source position and report
    # that live assert enforced, which is the damaging direction.
    if not any(not is_conditional(entry) for entry in tied):
        return None
    ran = [
        entry
        for entry in tied
        if not is_conditional(entry)
        or (
            orders is not None
            and index is not None
            and function is not None
            and _store_is_settled_before(entry, orders, index, function)
        )
    ]
    if not ran:
        return None
    ran_latest = max(position(entry[0]) for entry in ran)
    survivors = [entry for entry in ran if position(entry[0]) == ran_latest]
    return survivors[0] if len(survivors) == 1 else None


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
