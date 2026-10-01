# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part4":
    raise ImportError(
        "tests._sentinel_support_part4 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


def _aliased_suppressions(node, function, bound, owning=None, target=None):
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
    name from an outer scope is deliberately not followed *by default*:
    assuming an arbitrary call returns a suppressor would report live asserts
    as dead on every context manager this check cannot trace. The one exception
    is a name the function explicitly declares ``nonlocal``, which the language
    guarantees refers to an enclosing function's binding -- see
    :func:`_nonlocal_suppressors` and #355.

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
    by_index, raw_values = _assigned_suppressors(
        function, bound, target if target is not None else node, owning
    )
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
                    header,
                    statement,
                    bound_so_far,
                    by_index.get(index, {}),
                    function,
                    bound,
                    raw_values,
                )
            )
            # #385. A header that reads a loop target *after* the loop has
            # finished is not the in-body question `_bindings_before` asks,
            # and the raw table cannot answer it: `_raw_store_values` records a
            # multi-element loop as nothing at all, so the name has no entry to
            # resolve against and the header reads as an ordinary unbound name.
            # The loop's own binding is layered on for that case only, and
            # `live` is copied rather than mutated so the hand-off to the next
            # top-level statement is unaffected.
            after_loop = _loop_target_bindings_after_loop(function, header, bound, raw_values)
            if after_loop:
                live = {**live, **after_loop}
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
                    elif isinstance(walrus.value, ast.Name):
                        # A `nonlocal` self-alias resolves to nothing locally:
                        #
                        #     def inner():
                        #         nonlocal cs
                        #         with (cs := cs):   # `cs` is the parent's
                        #             assert 1 == 2  # suppressor -> swallowed
                        #
                        # `raw_values` is built from `inner` alone, and
                        # `inner` stores no `cs`, so the dereference above
                        # returns the bare name. The enclosing function's
                        # binding is the real answer (#355).
                        entered.extend(
                            _nonlocal_suppressors(
                                walrus.value.id, function, bound, owning, walrus.value
                            )
                        )
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
                elif isinstance(expression, ast.Name):
                    # The name is not bound by anything this function can see.
                    # A `nonlocal` declaration says it *is* bound in an
                    # enclosing function, and this module walks one function
                    # at a time, so a suppressor sitting in the parent scope is
                    # invisible here even though the header really enters it
                    # (#355).
                    entered.extend(
                        _nonlocal_suppressors(expression.id, function, bound, owning, expression)
                    )
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


def _nonlocal_declared(function):
    """The names ``function`` declares ``nonlocal``, or empty.

    A ``nonlocal`` statement is a declaration, not an assignment: it performs
    no store and binds nothing. It is the one place the source states outright
    that a name belongs to an enclosing function rather than to this one, so
    it is exactly the permission needed to look outward -- and it is what
    keeps the search narrow. A name that is merely inherited by closure, or
    read from a module global, is *not* listed and is still not followed.
    """
    if function is None:
        return frozenset()
    declared = set()
    for node in _scope_body_nodes(function):
        names = getattr(node, "names", None)
        if isinstance(node, ast.Nonlocal) and names:
            declared.update(names)
    return frozenset(declared)


def _nonlocal_suppressors(name, function, bound, owning=None, header=None):
    """Readable enclosing values still in force at direct calls of this function.

    A nonlocal declaration identifies a scope, not a permanently fixed value.
    A store in this function can retire the enclosing suppressor, and parent
    stores must be read at the invocation rather than collected indiscriminately.
    Only direct calls whose binding can be proved readable are followed. A
    returned closure or an opaque intervening call is left undecided.
    """
    if name not in _nonlocal_declared(function):
        return ()
    if _nonlocal_rebound_before(name, function, header, bound):
        return ()
    parent = _nonlocal_parent_function(function, owning)
    if parent is None:
        return ()
    # The ordinary raw table walks nested bodies. Those stores have not run
    # merely because their definitions were executed, so remove the bodies
    # from a private AST before building the parent's invocation snapshots.
    scope = copy.deepcopy(parent)
    for node in _scope_body_nodes(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.body = []
        elif isinstance(node, ast.Lambda):
            node.body = ast.Constant(value=None)
    parent_bound = _nonlocal_parent_imports(parent, owning)
    bindings, _raw = _store_bindings(scope, parent_bound)
    entries = bindings.get(name, ())
    if not entries:
        return ()
    orders = {id(statement): _binding_order(scope, statement) for statement, _, _ in entries}
    calls = [
        index
        for index, statement in enumerate(scope.body)
        if isinstance(statement, (ast.Expr, ast.Return))
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == function.name
    ]
    if not calls:
        return ()
    resolved = []
    for index in calls:
        visible = [entry for entry in entries if orders[id(entry[0])] < index]
        if not visible:
            return ()
        unconditional = [entry for entry in visible if not entry[2]]
        if not unconditional:
            return ()
        latest = max(orders[id(entry[0])] for entry in unconditional)
        if any(entry[2] and orders[id(entry[0])] >= latest for entry in visible):
            # A conditional replacement may have changed the object. The
            # ordinary suppression reader's ambiguity marker is not proof
            # that every possible captured value actually suppresses.
            return ()
        # Another call can mutate the captured cell. Its effects are opaque;
        # a readable earlier assignment is insufficient evidence after it.
        if any(
            any(isinstance(node, ast.Call) for node in _scope_body_nodes(statement))
            or isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and statement.decorator_list
            for statement in scope.body[latest + 1 : index]
        ):
            return ()
        value = _resolve_bindings(visible, parent_bound, orders, index, scope)
        if not _is_readable_suppressor(value, parent_bound):
            return ()
        if not _nonlocal_has_known_exception_arguments(value, bindings, scope):
            return ()
        if not _nonlocal_constructor_is_unshadowed(value, parent, owning):
            return ()
        resolved.append(value)
    return resolved


def _nonlocal_parent_imports(parent, owning):
    """Resolve the constructor using lexical imports, with inner scopes winning."""
    scopes = []
    scope = parent
    while scope is not None:
        scopes.append(scope)
        scope = _nonlocal_parent_function(scope, owning)
    bound = _bound_names(owning) if owning is not None else {}
    for scope in reversed(scopes):
        # _own_imports uses a stack; restore source order for sequential imports.
        imports = sorted(_own_imports(scope), key=lambda node: (node.lineno, node.col_offset))
        bound.update(_bound_names(ast.Module(body=imports, type_ignores=[])))
    return bound


def _nonlocal_constructor_is_unshadowed(value, parent, owning):
    """An import spelling must still identify its constructor in enclosing scopes."""
    root = value.func
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name):
        return False
    scopes = []
    scope = parent
    while scope is not None:
        scopes.append(scope)
        scope = _nonlocal_parent_function(scope, owning)
    if isinstance(owning, ast.Module):
        scopes.append(owning)
    for scope in scopes:
        for node in _scope_body_nodes(scope):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
                and node.id == root.id
            ):
                return False
            if isinstance(node, ast.arg) and node.arg == root.id:
                return False
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == root.id
            ):
                return False
            if isinstance(node, ast.ExceptHandler) and node.name == root.id:
                return False
        if root.id in _match_capture_names(scope):
            return False
    return True


def _nonlocal_has_known_exception_arguments(value, bindings, scope):
    """Follow only unshadowed builtin exception arguments across a scope boundary.

    The ordinary suppression reader treats an unreadable exception as universal.
    That is insufficient evidence for following an outer object: suppress()
    catches nothing, and an alias or a shadowed AssertionError may name a
    harmless exception. Decline those values rather than importing that guess.
    """
    if not value.args:
        return False
    builtins = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
    parameters = {
        argument.arg
        for argument in [*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs]
    }
    parameters.update(
        argument.arg for argument in (scope.args.vararg, scope.args.kwarg) if argument is not None
    )
    for argument in value.args:
        if not isinstance(argument, ast.Name):
            return False
        candidate = builtins.get(argument.id)
        if not isinstance(candidate, type) or not issubclass(candidate, BaseException):
            return False
        if argument.id in parameters or argument.id in bindings:
            return False
    return True


def _nonlocal_rebound_before(name, function, header, bound):
    """Has this function already replaced the enclosing binding at this read?"""
    if header is None:
        return False
    bindings, _raw = _store_bindings(function, bound)
    own_nodes = {id(node) for node in _scope_body_nodes(function)}
    position = (header.lineno, header.col_offset)
    for statement, _value, _conditional in bindings.get(name, ()):
        if id(statement) not in own_nodes:
            continue
        if (statement.lineno, statement.col_offset) > position:
            continue
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            # An as-target binds after its own context expression, and before
            # a later item's expression. Compare that target, not the whole
            # with statement, to the particular read being resolved.
            for item in statement.items:
                target = item.optional_vars
                if (
                    target is not None
                    and name in _store_target_names([target])
                    and (target.lineno, target.col_offset) < position
                ):
                    return True
            continue
        # A self-alias does not replace the object; all other stores retire
        # the parent answer even when their value is not a readable suppressor.
        value = getattr(statement, "value", None)
        if isinstance(value, ast.Name) and value.id == name:
            continue
        return True
    return False


def _nonlocal_parent_function(function, owning=None):
    """The nearest function ancestor of this exact node, not the outermost."""
    trees = [owning] if owning is not None else [milestones_tree(), _module_tree()]
    for tree in trees:
        if tree is None:
            continue
        pending = [(tree, None)]
        while pending:
            node, parent = pending.pop()
            if node is function:
                return parent
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = node
            pending.extend((child, parent) for child in ast.iter_child_nodes(node))
    return None


def _bindings_before(
    header, statement, bound_so_far, own, function=None, bound=None, raw_values=None
):
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
        # A loop's `else` arm is a block of its own and a header written in one
        # is decided by exactly the same rule as one in the body. Reading only
        # `body` never reached the arm at all:
        #
        #     for cs in [contextlib.suppress(AssertionError)]:
        #         pass
        #     else:
        #         with cs:            # `cs` IS bound here
        #             assert x != 1  # executed: swallowed
        #
        # The arm's own earlier stores still win, exactly as they do in `body`:
        # a store before the header in the arm has run, and one after it has
        # not. `seen_store` is shared across both lists, so that falls out of
        # the ordinary rule rather than needing a second path.
        orelse = getattr(block, "orelse", None)
        block_body = body + (orelse if isinstance(orelse, list) else [])
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
        for node in block_body:
            if node is header:
                # Nothing in this block has been stored before the header, so
                # only the bindings carried in from earlier statements apply.
                # #370. A loop body is the one exception: the loop's own target
                # is assigned before the body runs, so a header in the first
                # position already sees it.
                #
                #     for cs in [contextlib.suppress(AssertionError)]:
                #         with cs:            # `cs` IS bound here
                #             assert 1 == 2
                #
                # Executed, that assert is swallowed, so reporting it enforced
                # certifies a disarmed contract as load-bearing. The ordinary
                # same-block case is the opposite and must keep raising loudly:
                #
                #     if flag:
                #         with cs:            # NameError -- not bound yet
                #             assert 1 == 2
                #         cs = contextlib.suppress(AssertionError)
                #
                # so the exception is scoped to the loop that owns the body.
                loop_bindings = _loop_target_bindings(function, header, bound)
                if loop_bindings:
                    # The loop's own target is *one more* store in force at
                    # this point, not a replacement for what the enclosing
                    # blocks already carried in. A name the loop does not
                    # bind still has the value it had on entry, so the
                    # carried bindings are kept and the loop's are layered
                    # on top:
                    #
                    #     base = contextlib.suppress(AssertionError)
                    #     for other in [0]:
                    #         with base:            # still the suppressor
                    #             assert x != 1
                    #
                    # Substituting instead of merging would drop `base` and
                    # report that assert live.
                    # A store *in this very block*, before the header, is the
                    # latest write and supersedes the loop's target:
                    #
                    #     for cs in [contextlib.suppress(AssertionError)]:
                    #         pass
                    #     else:
                    #         cs = contextlib.nullcontext()
                    #         with cs:            # the nullcontext
                    #             assert x != 1
                    #
                    # Reading the loop's element here would report the assert
                    # defeated and drop a contract that fires. The arm's own
                    # store is read from the raw table rather than from `own`,
                    # because `own` is keyed by position in `function.body` and
                    # a store in a loop's `else` arm has no index of its own --
                    # it is filed under the loop, so `own` alone never sees it.
                    in_block = _block_store_bindings(
                        block_body, header, raw_values, bound, function
                    )
                    if in_block:
                        return {**bound_so_far, **in_block}
                    return {**bound_so_far, **loop_bindings}
                return dict(own) if seen_store or captures else bound_so_far
            if (
                isinstance(node, ast.Assign)
                or (isinstance(node, ast.AnnAssign) and node.value is not None)
                or (
                    isinstance(node, (ast.For, ast.AsyncFor))
                    and isinstance(node.iter, (ast.Tuple, ast.List))
                    and bool(node.iter.elts)
                )
            ):
                seen_store = True
    return bound_so_far


def _block_store_bindings(block_body, header, raw_values, bound, function=None):
    """Bindings written by a store that precedes ``header`` in ``block_body``.

    #370b. The ``own`` map this function otherwise reads is keyed by position in
    ``function.body``, so it answers for a header's *enclosing statement* but not
    for a store written in a block that has no index of its own -- a loop's
    ``else`` arm among them. That store is still in force at the header, so the
    raw table is consulted directly and every store appearing before the header
    in the same block contributes.

    Only stores strictly before the header count. A store after it has not run
    when the header is evaluated -- that is the ``UnboundLocalError`` case the
    caller handles by falling through to the carried bindings -- so counting it
    would report a loud failure as a swallowed one.
    """
    if not raw_values or header not in block_body:
        return {}
    cutoff = block_body.index(header)
    resolved = {}
    for entries in raw_values.values():
        for entry_statement, value in entries:
            if entry_statement not in block_body or block_body.index(entry_statement) >= cutoff:
                continue
            if not _is_store_statement(entry_statement, function):
                continue
            targets = (
                entry_statement.targets
                if isinstance(entry_statement, ast.Assign)
                else [entry_statement.target]
            )
            for name in _store_target_names(targets):
                readable = value if _is_readable_suppressor(value, bound) else _NOT_A_SUPPRESSOR
                resolved[name] = readable
    return resolved


def _loop_target_bindings(function, header, bound=None, raw_values=None):
    """Bindings a containing loop's own target puts in force around ``header``.

    #370. A ``for`` target is assigned before its body runs, so it is the one
    same-block store already in force at a header written in the body's first
    position. Executed both ways: the loop shape swallows the assert, while the
    ``if`` shape above raises ``UnboundLocalError`` on entry.

    Only a single-element literal iterable contributes. A multi-element or
    non-literal iterable binds a *different* value per iteration, so no single
    element is the answer and the name stays unreadable -- keeping the header
    ``enforced``, which is the safe direction. #385 measured that choosing an
    index instead moves a damaging cell rather than removing it.

    #413. A multi-element literal is admitted by one further test, and the
    distinction is *agreement* rather than *count*. When every element of the
    literal is a readable suppressor there is no per-iteration disagreement to
    preserve: each iteration binds a value that cannot be entered, so the
    `with` header raises before the body and the assert is unreachable on every
    iteration. The recorded representative is never read as "this element" --
    unanimity is what makes the choice irrelevant.

        for cs in (suppress(AssertionError), suppress(AssertionError)):
            with cs:
                assert 1 == 2        # swallowed on every iteration

    A literal whose elements merely *happen* to include a suppressor still
    fails the test and keeps the decline, which is the point:

        for cs in (suppress(AssertionError), nullcontext()):
            with cs:
                assert 1 == 2        # swallowed once, fires once

    Executed, the first shape never fails and the second shape fails on its
    second iteration, so the first is a disarmed contract and the second is a
    live one. Declining the first reported it ``enforced``, certifying a
    contract that can never fail as load-bearing.

    The enclosing body is found by looking for the *header* rather than for
    the list the caller is iterating, because a single-statement loop body
    puts the ``with`` directly in ``node.body`` while the caller's list is the
    enclosing block. A ``while`` body has no target, and an ``if``/``try`` body
    is not a loop body, so both return ``{}`` and the ordinary rule still
    applies.

    ``bound`` is the module's import bindings, needed to decide whether the
    element is a suppressor at all, and ``raw_values`` is the whole-function
    store table, accepted from the caller when it already has one and rebuilt
    otherwise. Only the entries belonging to this loop are read, so the lookup
    stays narrow regardless of how many stores the function contains.
    """
    if function is None:
        return {}
    if raw_values is None:
        raw_values = _raw_store_values(function)
    resolved = {}
    for node in _enclosing_loops(function, header):
        if (
            _single_loop_element(getattr(node, "iter", None)) is None
            # #413. The multi-element refusal above is a statement about
            # *which* element the target holds, and it holds only while the
            # elements disagree. A literal whose every element is a
            # readable suppressor has no disagreement to preserve: each
            # iteration binds a value that cannot be entered, so `with cs:`
            # raises before the body on every iteration and the assert is
            # unreachable every time. `_unanimous_non_enterable_element`
            # is the same test the recording walk applies, and asking it
            # here keeps the two from disagreeing about the same loop --
            # a loop the table declines to record and this rule declined to
            # consult would simply keep the old false LIVE.
            and _unanimous_non_enterable_element(getattr(node, "iter", None), bound) is None
        ):
            continue
        for name in _store_target_names([node.target]):
            for entry_statement, value in raw_values.get(name, []):
                if entry_statement is node:
                    # The element has to clear the same readability bar every
                    # other store value clears before it can stand in for a
                    # name's binding. Without it a `nullcontext` element is
                    # handed to `_aliased_suppressions` as though it were a
                    # suppressor, and that rule appends a live `live[...]`
                    # value without re-testing it -- so
                    #
                    #     for cs in [contextlib.nullcontext()]:
                    #         with cs:
                    #             assert x != 1
                    #
                    # reports the assert defeated, certifying a contract that
                    # really does fire. `None` and an unreadable value are
                    # different, though: the name IS bound here, so the
                    # "not a suppressor" answer has to be recorded as
                    # `_NOT_A_SUPPRESSOR` rather than dropped, or a suppressor
                    # carried in from an enclosing block is carried past the
                    # loop that just overwrote it.
                    readable = value if _is_readable_suppressor(value, bound) else _NOT_A_SUPPRESSOR
                    # A later loop in this ordering is nested inside this one,
                    # so its target is the LAST write before the header runs
                    # and its element is the binding actually in force. Writing
                    # it over the outer loop's entry is what keeps
                    #
                    #     for cs in [suppress(AssertionError)]:
                    #         for cs in [nullcontext()]:
                    #             with cs:          # nullcontext, not suppress
                    #                 assert x != 1
                    #
                    # reporting the assert live when it is really swallowed.
                    resolved[name] = readable
    return resolved


def _enclosing_loops(function, header):
    """The ``for``/``async for`` loops whose body contains ``header``, outermost first.

    #370b. The binding a header sees comes from the *innermost* loop that binds
    the name, because an inner loop's target is the last write before the header
    runs:

        for cs in [contextlib.suppress(AssertionError)]:
            for cs in [contextlib.nullcontext()]:
                with cs:                     # `nullcontext` is in force
                    assert x != 1

    Matching only the direct body list answered for `for other in [0]:` bodies
    containing a *nested* header as well, so every header one block down still
    reported `enforced` when executed it is swallowed. The body is therefore
    walked, and the walk is bounded by the loop so an unrelated loop elsewhere
    in the function cannot contribute.

    Innermost-last ordering matters: a loop that does not bind the header's name
    contributes nothing, so a name bound only by the outer loop still resolves,
    while a name rebound by the inner loop resolves to the inner element. A
    loop whose iterable is undecidable is *skipped* rather than treated as a
    barrier, so it cannot retire an outer loop's readable binding -- declining
    to read a name is not the same as saying the name is not a suppressor.
    """
    loops = [
        node
        for node in ast.walk(function)
        if isinstance(node, (ast.For, ast.AsyncFor)) and _loop_arms_reach(node, header)
    ]
    if not loops:
        return []
    return _outermost_first(loops, function)


def _loop_arms_reach(loop, header):
    """Does ``header`` sit inside ``loop``'s body or inside its ``else`` arm?

    #370b. Both arms run only after the target has been assigned, so a header in
    either one reads it. The body is the common case; the ``else`` arm is easy to
    miss because it is a sibling list rather than a child:

        for cs in [contextlib.suppress(AssertionError)]:
            pass
        else:
            with cs:                 # `cs` IS bound here
                assert x != 1       # executed: swallowed

    The walk is deliberately scope-blind, the same test the body uses. Descending
    only to the loop's own scope was measured, and it is *worse*: a ``with`` in a
    nested ``def`` reads the loop variable as a free variable, so refusing to
    descend there turns a correct ``False`` into a false LIVE. The shape that
    guard was meant to protect -- a nested ``def`` rebinding the name as its own
    local -- is already correct without it, because the walk finds the nested
    ``def``'s own store first and that store is the later write. A ``while`` loop
    has no target at all, so it is never a candidate here and the ordinary rules
    still decide its headers.
    """
    if not isinstance(loop, (ast.For, ast.AsyncFor)):
        return False
    arms = [*loop.body, *loop.orelse]
    return any(child is header for statement in arms for child in ast.walk(statement))


def _outermost_first(loops, function):
    """``loops`` ordered so an enclosing loop always precedes a nested one.

    ``ast.walk`` is breadth-first, so its order is neither source order nor
    containment order, and the order decides which loop's element a name
    resolves to. Sorting on the parent chain states the intent directly.

    For a nested ``for`` this coincides with sorting on line number -- an
    enclosed statement cannot start before the header that encloses it -- so a
    mutation to line-number order is behaviourally equivalent here and is not
    covered by a test row. The parent chain is kept because it is the property
    that actually matters, and because it stays correct if a future construct
    ever moves a loop body out of source order (``while``/``else``, ``match``
    guards). Two rows do pin the *outcome* of this ordering: the inner target
    superseding the outer one, and the three-deep chain.
    """
    parents = {}
    for parent in ast.walk(function):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def depth(node):
        level = 0
        current = parents.get(id(node))
        while current is not None:
            level += 1
            current = parents.get(id(current))
        return level

    return sorted(loops, key=depth)


def _loop_target_names(block):
    """The names a loop statement binds before its body runs, else empty."""
    if not isinstance(block, (ast.For, ast.AsyncFor)):
        return ()
    return frozenset(_store_target_names([block.target]))


def _is_store_statement(node, function=None):
    """Does reaching this point in a block mean the block's stores have run?

    #359: this list was ``ast.Assign``/``ast.AnnAssign`` only, so a ``for``
    target did not count as a store that had run by the time a ``with`` nested
    inside the loop body was read:

        for cs in (contextlib.suppress(AssertionError),):
            with cs:                 # `cs` IS the loop's live value
                assert x != 1        # swallowed

    The loop's own binding resolved correctly, and then this function threw it
    away because the enclosing statement was not one of the two shapes it
    recognised, so the header saw no binding at all and reported a swallowed
    assert as live. A ``for`` target binds on every iteration that reaches the
    body, which is the guarantee the other two shapes are listed for, so it
    belongs in the same set.

    ``NamedExpr`` is deliberately still absent: it never appears as a direct
    statement, so it cannot be the ``node`` walked here.

    #378: "on every iteration that reaches the body" is a conditional
    guarantee, and a literal empty container gives a loop that reaches the body
    exactly zero times:

        cs = contextlib.nullcontext()
        if True:
            for x in ():                       # never iterates
                cs = contextlib.suppress(AssertionError)   # never executes
            with cs:                          # `cs` is still the nullcontext
                assert x != 1                 # live

    Counting the ``for`` as a store that ran admits the unexecuted assignment
    and answers ``defeated``, dropping a live assert from the sentinel. Master
    answers ``enforced`` here, which is correct, so counting every loop was a
    regression rather than a pre-existing gap.

    The test is `_loop_iterable_is_provably_empty`, the same helper the
    ``for``-defeat rule already uses, so the two agree on what a decidable
    empty iterable is. #450 widened it from the container literals to the
    empty builtin calls and the empty ``range`` shapes, which is what let
    ``for _ in range(0):`` stop counting as a store that ran. A call the
    module still cannot decide -- ``helper.items()``, ``set(items)``,
    ``range(n)`` -- is counted as having run, because guessing wrong here
    drops a live contract, which is the more damaging error.

    ``function`` is the scope the statement appears in, and is needed for the
    same reason :func:`_condition_is_never_true` takes it: ``set()`` is only
    the empty builtin while the name still reaches it, so a function that
    rebinds ``set`` -- or ``range`` -- makes the call a different one whose
    body really is reached. It is optional only so the pre-existing callers
    that have no scope to hand keep working; without it the empty *call*
    shapes are declined and the rule falls back to counting the loop as run,
    which is the conservative direction.
    """
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return not _loop_iterable_is_provably_empty(node.iter, function)
    return isinstance(node, ast.Assign) or (
        isinstance(node, ast.AnnAssign) and node.value is not None
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
    """
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.Name) and test.id in values:
        return bool(values[test.id])
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        value = _elif_failure_predicate(test.operand, values)
        return None if value is None else not value
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
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
        except TypeError:
            # An unorderable pair has no truth value Python would compute, and
            # the assert under test cannot be evaluated against it either.
            return None
    return None
