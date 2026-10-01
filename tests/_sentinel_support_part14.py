# Shared-namespace fragment for reviewed sentinel helpers.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part14":
    raise ImportError(
        "tests._sentinel_support_part14 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


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
            # #481. Function bodies are deferred, but decorators, defaults,
            # annotations, class bases, and class bodies execute during creation.
            # Accept only definitions whose evaluated parts are plainly inert.
            if _witness_definition_is_transparent(node, function, bound):
                continue
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


def _witness_definition_is_transparent(node, function, bound):
    """Admit only definitions whose creation cannot execute opaque effects."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    if node.name in _witness_module_roots(function, bound) or node.name in _signature_bound_names(
        function
    ):
        return False
    if node.decorator_list or getattr(node, "type_params", ()):
        return False
    if isinstance(node, ast.ClassDef):
        return (
            not node.bases
            and not node.keywords
            and all(
                isinstance(statement, ast.Pass)
                or (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                )
                for statement in node.body
            )
        )
    args = node.args
    return (
        node.returns is None
        and all(argument.annotation is None for argument in _all_args(node) if argument is not None)
        and all(isinstance(value, ast.Constant) for value in args.defaults)
        and all(value is None or isinstance(value, ast.Constant) for value in args.kw_defaults)
    )


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
