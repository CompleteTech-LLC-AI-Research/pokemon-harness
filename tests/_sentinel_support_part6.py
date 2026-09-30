# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part6":
    raise ImportError(
        "tests._sentinel_support_part6 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


def _module_rebinds_name(function, name, call=None):
    """Does the module holding ``function`` bind ``name`` to something else?

    This is the ``builtins.attr`` form's own question -- "is the qualified name
    still the module itself" -- and it takes the call node so the answer is
    scoped the same way :func:`_module_binds_name` scopes its own. A store
    written *after* a module-scope call has not run when the call is evaluated,
    so the callee still reached the real module:

        import builtins
        cs = builtins.list()          # the builtin; runs first
        builtins = SimpleNamespace(list=nullcontext)

    Counting that later store read the rebound attribute and reported the
    ``TypeError`` CPython raises as LIVE. A function-body call is reached after
    the whole module has run, so for one every store counts and the same order
    cut is not applied.

    The ordinary ``import builtins`` is not a rebinding and is skipped here, so
    the canonical spelling stays trustworthy.
    """
    module = _module_for_function(function)
    if module is None:
        return False
    at_module_scope = _call_runs_at_module_scope(function, call)
    for statement in getattr(module, "body", []):
        if at_module_scope and _module_statement_precedes_call(statement, function, call):
            # The call has been evaluated by the end of this statement, so a
            # binding written from here on is not yet in force where it was.
            break
        if _is_canonical_module_import(statement, name):
            continue
        if any(_names_bound_by_statement(statement, name)):
            return True
        for node in _module_level_bindings(statement):
            if _is_canonical_module_import(node, name):
                continue
            if at_module_scope and _node_precedes_call(statement, node, call):
                continue
            if any(_names_bound_by_statement(node, name)):
                return True
    return False


def _call_runs_at_module_scope(function, call):
    """Is ``call`` evaluated while the module runs, rather than in a body?

    A call written among the module's own statements is reached as the module
    executes, so only the bindings before it are in force. A call inside the
    analysed function -- or inside any other nested scope -- is reached later,
    when the module has finished, so every module binding counts. The question
    is answered by position: a call that is *not* under ``function`` is a
    module-scope one, which is also the safe answer for a caller that supplied
    no call node at all.
    """
    if call is None:
        return True
    return not _contains(function, call)


def _module_binds_name_at_module_scope(module, function, call, name):
    """Module bindings in force where a module-scope ``call`` is evaluated.

    The walk runs the module's statements in source order, descending into
    module-level blocks so that a binding *inside* a block is ordered with
    respect to the call rather than to the block as a whole. That ordering is
    what separates

        if True:
            cs = list()             # the builtin; runs first
            def list(): return None # too late to matter for the line above

    from the same block with the two the other way round, where ``cs`` really
    is the shadow and the header is dead. Stopping at the enclosing block
    instead of at the statement holding the call answered the first as
    shadowed -- a false-live, since CPython raises ``TypeError`` there.

    The walk stops at the first statement at or after the call's own position,
    and the statement holding the call has already been checked, so a store
    written on that same line still counts.
    """
    for statement in getattr(module, "body", []):
        if _module_statement_binds_name_before_call(statement, call, name):
            return True
        if _module_statement_precedes_call(statement, function, call):
            break
    return False


def _module_statement_precedes_call(statement, function, call):
    """Has execution reached ``call`` by the end of ``statement``?"""
    if statement is function:
        # The function's own definition binds its name; the body that follows
        # is a separate scope. A module-scope call is not inside it, so
        # reaching the `def` means the call has already been evaluated.
        return True
    return _contains(statement, call)


def _node_precedes_call(statement, node, call):
    """Has execution reached ``call`` before ``node`` inside ``statement``?

    ``node`` is a binding found *inside* ``statement`` by the block descent,
    so the two are ordered within that one statement rather than across the
    module. Only the path the descent actually took is walked, and a nested
    ``def``/``class`` body -- a separate scope -- stops the walk rather than
    being scanned, because nothing there has run.
    """
    if node is statement:
        return False
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return False
    if not _contains(statement, call):
        # The call is not in this statement at all, so every binding in it
        # runs before the call does.
        return False
    for inner in _module_level_bindings_in_order(statement):
        if inner is node:
            return True
        if _contains(inner, call):
            return False
    return False


def _module_statement_binds_name_before_call(statement, call, name):
    """Does ``statement`` bind ``name``, counting only what runs before ``call``?

    When the call is not inside ``statement`` the whole statement has run, so
    every binding it contains counts. When it is inside, the walk descends in
    order and stops once the call's own statement is reached, so a binding
    written after the call inside the same block is correctly ignored.
    """
    if not _contains(statement, call):
        return any(_names_bound_by_statement(statement, name)) or any(
            _names_bound_by_statement(node, name) for node in _module_level_bindings(statement)
        )
    # The call lives inside this statement, so descend in source order and
    # stop at the first inner statement that has already run it. The call's
    # own statement is checked before stopping, so a store on the same line
    # (`cs = list()`) is still counted.
    for inner in _module_level_bindings_in_order(statement):
        if any(_names_bound_by_statement(inner, name)):
            return True
        if _contains(inner, call):
            break
    return False


def _module_level_bindings_in_order(statement):
    """Like :func:`_module_level_bindings`, but the blocks' own statements too.

    The order matters only for a statement that holds the call, because that is
    the one case where a later binding in the same block must not be counted.
    Each block's children are yielded before the block itself so that the
    reader sees them in the order they run.
    """
    ordered = []
    for node in _module_level_bindings(statement):
        if isinstance(node, _MODULE_LEVEL_BLOCKS):
            ordered.append(node)
            ordered.extend(_module_level_bindings_in_order(node))
        else:
            ordered.append(node)
    return ordered


def _module_level_bindings(statement):
    """Bindings inside one module-level statement, excluding nested scopes.

    Only *blocks* are descended into, because a block runs in the scope that
    encloses it:

        if flag:
            def list(): return None      # still a module binding

    A nested ``def``/``class`` **body** is a separate scope and is never
    entered, which is what keeps

        def unrelated():
            def list(): return None

    from answering for ``list`` at module scope: ``unrelated``'s body binds
    ``list`` only when ``unrelated`` is called, and nothing here has run it.
    """
    found = []
    # The statement's own body list, so a block is entered at its statements
    # rather than at its own child nodes: descending from the block itself
    # would push its `If.test`/body onto the stack unfiltered, which is how a
    # `def` inside a module `if` was skipped. A `def`/`class` is not a block, so
    # this is empty for one and nothing inside it is entered:
    #
    #     def unrelated():
    #         def list(): return None
    #
    # `list` is bound in `unrelated`'s locals, when `unrelated` is called.
    # Descending made the module answer for a name no module store has written,
    # and read the `TypeError` a real call raises as a callable the header
    # enters.
    stack = list(reversed(_block_body(statement)))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.Lambda,)):
            # A bare lambda has no name of its own and its body is a separate
            # scope, so there is nothing here that binds in the module scope.
            continue
        if isinstance(
            current,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Assign,
                ast.AnnAssign,
                ast.AugAssign,
                ast.Import,
                ast.ImportFrom,
                ast.For,
                ast.AsyncFor,
                ast.With,
                ast.AsyncWith,
            ),
        ):
            found.append(current)
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # Recorded for its own name -- `def list()` inside a module
                # `if` really does bind the module name -- then left alone:
                # its body is a separate scope.
                continue
        if isinstance(current, _MODULE_LEVEL_BLOCKS):
            stack.extend(reversed(_block_body(current)))
    return found


def _module_for_function(function):
    """The parsed module ``function`` was defined in, or ``None``.

    The tree is the one the caller is actually analysing, so a module-level
    binding is read from the file the assert lives in rather than from
    whichever module happens to be importable. The result is memoised by node
    identity because the AST is built once per parsed file and outlives the
    call, and because this runs per condition in a large tree.
    """
    if function is None:
        return None
    if id(function) in _MODULE_FOR_FUNCTION:
        return _MODULE_FOR_FUNCTION[id(function)]
    module = None
    for tree in _candidate_module_trees():
        if any(node is function for node in ast.walk(tree)):
            module = tree
            break
    _MODULE_FOR_FUNCTION[id(function)] = module
    return module


def _remember_module_for_function(function, module):
    """Record the module ``function`` was found in, so lookups can reuse it.

    The caller that resolved the module -- :func:`_is_enforced`, which takes
    the tree from its own caller -- knows the answer exactly. Recording it
    keeps :func:`_module_binds_name` from re-deriving it by walking two module
    trees per condition, and it is what makes the module pass answer for a
    probe tree rather than for the importable milestones module.
    """
    if function is not None and module is not None:
        _MODULE_FOR_FUNCTION[id(function)] = module


def _candidate_module_trees():
    """Every parsed module a function could have been defined in.

    Each tree is read through :func:`_owning_module`'s own pair, and a tree
    that cannot be produced at all is skipped rather than raised: this runs
    inside a decision function called once per condition, and a decision
    function must answer for the node it is handed instead of crashing the
    analyzer. The real production path always finds the milestones tree; the
    skip only matters when the source is unavailable.
    """
    candidates = []
    for produce in (_module_tree, milestones_tree):
        try:
            candidates.append(produce())
        except (OSError, TypeError, AttributeError):
            # The source cannot be read back, so the tree is unavailable. The
            # module pass then has nothing to contribute and the answer stays
            # conservative, which is the safe direction. Only the failures
            # `inspect.getsource` actually raises are caught, so a genuine
            # bug in a producer still surfaces instead of being swallowed.
            continue
    for tree in candidates:
        if tree is None:
            continue
        yield tree


def _own_scope_bindings(function):
    """Statements in ``function``'s own scope, excluding nested scopes."""
    found = []
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A `def`/`class` statement *is* a binding in the enclosing scope --
            # that is exactly the shadowing being looked for -- so it is
            # recorded. Its *body* is a separate scope and is not descended
            # into. A bare `Lambda` has no name, so there is nothing to record
            # and its body is skipped with the rest.
            found.append(node)
            continue
        if isinstance(node, ast.Lambda):
            continue
        found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _names_bound_by_statement(statement, name):
    """Does this statement bind ``name`` in the enclosing function's scope?"""
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [statement.name == name]
    if isinstance(statement, ast.ClassDef):
        return [statement.name == name]
    if isinstance(statement, ast.Assign):
        return [any(isinstance(t, ast.Name) and t.id == name for t in statement.targets)]
    if isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        target = statement.target
        return [isinstance(target, ast.Name) and target.id == name]
    if isinstance(statement, ast.Import):
        return [any((a.asname or a.name) == name for a in statement.names)]
    if isinstance(statement, ast.ImportFrom):
        return [any((a.asname or a.name) == name for a in statement.names)]
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return [name in _store_target_names([statement.target])]
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return [
            any(
                a.optional_vars is not None and name in _store_target_names([a.optional_vars])
                for a in statement.items
            )
        ]
    if isinstance(statement, ast.ExceptHandler):
        return [statement.name == name]
    if isinstance(statement, (ast.Global, ast.Nonlocal)):
        return [name in (statement.names or [])]
    return [False]


def _builtin_constructor_arguments_are_ignorable(func, call):
    """Can these arguments turn the constructor call into a different type?

    ``int()``, ``int("3")`` and ``int(b"\\x03")`` all return an ``int``.
    ``list()`` and ``list((1, 2))`` both return a ``list``. But ``range(3)``
    is a ``range`` while ``range()`` is not a range at all, and a *shadowed*
    ``int`` may be any callable, so the question is asked per name rather than
    assumed for the whole set.

    :data:`_BUILTIN_CONSTRUCTOR_TYPES` is consulted with that in mind: an
    entry is only read when its arguments provably cannot select a different
    type, which is decided here from the constructor's own signature-free
    property that the accepted arguments are *inputs to a conversion* rather
    than *selectors of a different class*.
    """
    name = func.id if isinstance(func, ast.Name) else func.attr
    if name == "range":
        # `range(stop)`, `range(start, stop)` and `range(start, stop, step)`
        # are all ranges, but `range()` with no argument is a TypeError, so
        # the call raises and the store never happens, which leaves the carrier
        # in force. That is a decided outcome, not an unreadable one, so the
        # argument-bearing forms still answer `"range"` and the empty form is
        # recognised by the *caller* as a store that cannot happen.
        if not call.args:
            return _RAISING_CONSTRUCTOR
        return _BUILTIN_CONSTRUCTOR_TYPES["range"]
    # An unpacked argument list may carry anything, including a value that
    # makes the call fail, so it is treated as unreadable.
    return not (
        any(isinstance(argument, ast.Starred) for argument in call.args)
        or any(
            isinstance(keyword, ast.keyword) and keyword.arg is None for keyword in call.keywords
        )
    )
    return None


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
    if isinstance(expression, ast.Tuple) and expression.elts:
        # #426. `with (*cs,):` enters a **tuple**, whatever `cs` held. A tuple
        # has no `__enter__`, so the header raises `TypeError` before the body
        # is reached and the assert under it is never evaluated. The
        # analyzer certified it as load-bearing, which is the damaging
        # direction: a disarmed contract called live.
        #
        # A starred element is what decides it, and it is decidable from the
        # header syntax alone. The wrapped value's own kind is irrelevant --
        # `cs = [nullcontext(), suppress(AssertionError)]` reads as "there is
        # a suppressor in there", and believing that is what produced the
        # false-LIVE. (Executed on CPython 3.12.14: `with (*cs,):` raises
        # `TypeError: 'tuple' object does not support the context manager
        # protocol`.)
        #
        # A *plain* tuple header is declined rather than answered, because
        # `with (a, b):` on an unenterable pair and a header that is only
        # sometimes a tuple are not the same question, and guessing would
        # risk the live side.
        return all(isinstance(element, ast.Starred) for element in expression.elts)
    if isinstance(expression, ast.NamedExpr) and isinstance(expression.target, ast.Name):
        # #390. `with (cs := lambda: None):` binds the walrus value directly as
        # the entered object, so the question is the same one the store rules
        # ask about `cs = lambda: None` -- and it has the same answer. Without
        # this the lambda never reached the readable-literal set below (that
        # set is consulted for values reached through an *assignment target*),
        # and a header that raises `TypeError` on entry left the assert under
        # it certified as load-bearing.
        #
        # The name the walrus binds is deliberately not followed afterwards.
        # The value is what is entered here and now; what `cs` holds on a
        # later line is a different store's question, answered by
        # `_stores_of` when that `with` is reached.
        return _literal_entry_is_dead(expression.value)
    if not isinstance(expression, ast.Name):
        return False
    name = expression.id
    stores = _stores_of(name, by_index, index, function)
    # #388. `_stores_of` keeps only unconditional stores, so an *unconditional*
    # carrier survives a *conditional* non-carrier store that follows it. That
    # later store may have run and replaced the carrier with an enterable
    # value, in which case the carrier is stale and reading it would answer
    # `defeated` on an assert the interpreter really does evaluate. The
    # `_module_stores` half of this already declines; without the same decline
    # here the function scope is the only one that answers from a carrier a
    # conditional store may have superseded, which is the damaging direction.
    # #388. `_stores_of` keeps only *unconditional* stores, so an
    # unconditional carrier survives a *conditional* non-carrier store that
    # follows it. That later store may have run and replaced the carrier with
    # an enterable value, in which case the carrier is stale and reading it
    # would answer `defeated` on an assert the interpreter really does
    # evaluate. Declining here is the safe direction.
    #
    # #392's #375 guard in `_stores_of` asks a *wider* question -- "can any
    # later conditional store leave something enterable here?" -- but it only
    # fires when the settled value is one `_store_may_bind_enterable` can read.
    # A carrier is a string kind, and the `with`-header families
    # (`with nullcontext(enter_result=...) as cs:`) settle it through a path
    # that never reaches that guard. Both guards are load-bearing, and both
    # now share one reading of "can this store leave something enterable?",
    # so the two questions cannot answer the same store differently.
    # The guard reads the *raw* binding list rather than `_stores_of`'s
    # filtered one, so it still answers when `_stores_of` declines. That is
    # the nested-scope case: a store inside a nested `def`/`lambda`/`class`
    # is not decidable for this rule, so `_stores_of` returns `None` -- but
    # the carrier written before it is still an unconditional store in this
    # scope, and the header must be read from that carrier rather than
    # reported live. Gating on `stores is not None` skipped exactly that, and
    # `test_a_function_carrier_decline_ignores_non_enterable_stores` read nine
    # unreachable asserts as load-bearing.
    if _carrier_may_have_been_superseded(
        by_index["bindings"].get(name, ()),
        by_index["orders"],
        index,
        bound,
        function,
        name,
    ):
        return False
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
        if _binds_starred_target(statement, name):
            if isinstance(statement, (ast.For, ast.AsyncFor)) and not _starred_store_decides_kind(
                (statement, value, _conditional),
                name,
                by_index["orders"],
                index,
                function,
                by_index.get("header"),
            ):
                return False
            # #418. `*cs, = (...)` and `a, *cs = (...)` collect a run of
            # elements into a **list**, so the *name* is a list whatever the
            # right-hand side held. `list` has no `__enter__`, so entering it
            # raises `TypeError` before the body is reached and the assert
            # never runs: the contract is dead.
            #
            # This is deliberately checked *before* the general
            # destructuring decline below. That decline exists because the
            # type of a plain element is not readable from the container's
            # syntax, so it answers "cannot tell" and keeps the assert live --
            # the safe direction. A starred target is not such a case: the
            # list-wrapping is decided by the target syntax alone, and it is
            # decidable. Reading the wrapped element's kind instead let a
            # usable suppressor vouch for a bare list, certifying a dead
            # assert as enforced.
            #
            # Executed on CPython 3.12.14:
            #   *cs, = (contextlib.suppress(AssertionError),)  -> cs == [suppress]
            #   a, *cs = (contextlib.suppress(...), 2)         -> cs == [2]
            #   b, *c = (1, contextlib.suppress(...))         -> c == [suppress]
            # and in all three `hasattr(cs, "__enter__")` is False.
            kinds.add("list")
            continue
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
                # #420. A *starred* loop target is decidable from the target
                # syntax alone: `for *cs, in (...):` binds the list the
                # unpacking collects, so entering it raises `TypeError`
                # before the body and the assert below never runs. The
                # decline below is for the *plain* loop target, which binds
                # the next element and genuinely cannot be read without
                # running the loop -- `for cs in (nullcontext(),):` is
                # really live (#336). Recording the starred case as a
                # `NoneType` here would be wrong for the same reason the
                # assign path is: the wrapped element's kind is irrelevant
                # to whether the *name* is enterable.
                if _starred_names_in_loop_target(statement, name) and _starred_store_decides_kind(
                    (statement, value, _conditional),
                    name,
                    by_index["orders"],
                    index,
                    function,
                    by_index.get("header"),
                ):
                    kinds.add("list")
                    continue
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
        if _unbound_local_class_constructor(value, function, module):
            # Resolving a local class before its first definition raises
            # UnboundLocalError during construction, before this header can
            # reach its assert. The future class must not vouch for it.
            kinds.add("NoneType")
            continue
        if not isinstance(
            value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.Lambda)
        ):
            # A call is a call: `nullcontext()` returns a real context manager
            # and must not be read as a non-manager here. The *builtin
            # constructors* are the exception, because their result type is
            # fixed by the callee's name: `cs = int()` leaves an `int`, and
            # `with cs:` raises before the assert. Reading every call as
            # unreadable left an unconditional `cs = int()` reported live
            # where CPython raises -- a false-live.
            #
            # #390 adds `ast.Lambda` to the readable set for the same reason a
            # builtin constructor is readable: a lambda's runtime type is
            # fixed by the syntax that built it, exactly as `int()`'s is by
            # its callee name. It stays a *call-free* literal shape, so the
            # `lambda: nullcontext()` case -- which really is enterable --
            # is not what is being read. The kind is produced by
            # `_literal_runtime_type` below like any other literal.
            constructor = _builtin_constructor_kind(value, function)
            if constructor is None or constructor is _RAISING_CONSTRUCTOR:
                # A raising constructor never stores, so it does not settle the
                # name; an ordinary call is genuinely unreadable. Both leave
                # whatever the earlier stores decided, so the set is not
                # narrowed here.
                if constructor is _RAISING_CONSTRUCTOR:
                    continue
                return False
            kinds.add(constructor)
            continue
        if _binds_starred_target(statement, name):
            # #418. `*cs, = (...)` and `a, *cs = (...)` collect a run of
            # elements into a **list**, so the *name* is a list whatever the
            # right-hand side held. `list` has no `__enter__`, so entering it
            # raises `TypeError` before the body is reached and the assert
            # never runs: the contract is dead.
            #
            # This is deliberately checked *before* the general
            # destructuring decline below. That decline exists because the
            # type of a plain element is not readable from the container's
            # syntax, so it answers "cannot tell" and keeps the assert live --
            # the safe direction. A starred target is not such a case: the
            # list-wrapping is decided by the target syntax alone, and it is
            # decidable. Reading the wrapped element's kind instead let a
            # usable suppressor vouch for a bare list, certifying a dead
            # assert as enforced.
            #
            # Executed on CPython 3.12.14:
            #   *cs, = (contextlib.suppress(AssertionError),)  -> cs == [suppress]
            #   a, *cs = (contextlib.suppress(...), 2)         -> cs == [2]
            #   b, *c = (1, contextlib.suppress(...))         -> c == [suppress]
            # and in all three `hasattr(cs, "__enter__")` is False.
            kinds.add("list")
            continue
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
    answers the identical shape the same way, because :func:`_stores_of` is
    what drops conditional stores on *both* sides, and both paths now run the
    same ordered decline through
    :func:`_carrier_may_have_been_superseded`. Lifting the shared
    conservatism -- reading a conditional binding as settled -- would mean
    changing :func:`_stores_of` for every rule at once, and is deliberately
    not folded in here.

    #388 is what made that agreement real rather than aspirational. This
    decline was written for the module path only, and the function path had no
    equivalent, so a function-scope carrier that a later conditional store
    may have superseded went stale and answered ``defeated`` on a live assert.
    The two scopes now call one helper, so a future change to the ordering
    test cannot reach one without reaching the other.

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
    if name_entries and _carrier_may_have_been_superseded(
        name_entries, orders, None, _bound_names(module, module), module, name
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
    if isinstance(value, ast.Lambda):
        # #390. `cs = lambda: None` binds a *function object*, and a function
        # has no `__enter__`. Entering one raises
        # `TypeError: 'function' object does not support the context manager
        # protocol` on the header, before the body is reached, so an assert
        # in that body is unreachable and calling it load-bearing is the
        # damaging direction.
        #
        # This is the same shape `_carrier_runtime_kinds` already records for
        # a `def cs` statement, reached here because a `Lambda` is bound by an
        # ordinary `Assign` target and so *does* get a store entry -- it just
        # had no readable runtime type until now. Reading the value rather
        # than the target syntax is the point: the store map is what makes
        # the answer orderable against the `with` header.
        return "function"
    if isinstance(value, ast.Attribute):
        # #394. `with fake.suppress(AssertionError):` where `fake` is a module
        # that does not export `suppress` raises `AttributeError` while the
        # header is evaluated, so the assert below never runs.
        #
        # The base has to be a *module* that is known not to export the
        # attribute, which is only decidable for a stdlib module read from
        # the real interpreter -- so the check is delegated to
        # `_module_lacks_attribute`, and anything it cannot resolve is
        # declined here. Returning a kind unconditionally would report every
        # `mod.attr` header dead, including `with contextlib.suppress(...)`,
        # which is genuinely live.
        if not isinstance(value.value, ast.Name):
            return None
        kind = _module_lacks_attribute(value.value.id, value.attr)
        return kind
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return {ast.List: "list", ast.Tuple: "tuple", ast.Set: "set"}[type(value)]
    if isinstance(value, ast.Dict):
        return "dict"
    if isinstance(value, ast.Constant):
        return type(value.value).__name__
    return None


def _module_lacks_attribute(base, attribute):
    """Is ``base`` a module that is known *not* to export ``attribute``?

    #394. ``import json as fake`` inside a function body shadows a module-level
    ``import contextlib as fake``, so ``with fake.suppress(AssertionError):``
    is an ``AttributeError`` raised while the header is evaluated -- the
    assert under it never runs, and the analyzer was certifying it as
    load-bearing. The damaging direction.

    Resolution is by the *real* interpreter, not by a list of module names:

    * only a name already bound to a module counts, and
    * only an import that is actually in ``sys.modules`` is consulted.

    Anything else returns ``None`` and the caller declines, because an
    unreadable attribute is not evidence of a missing one. Deciding it the
    other way -- assuming a module lacks an attribute it may well export --
    would report a live ``with contextlib.suppress(...)`` header dead, which
    is the opposite error and the one this rule exists to avoid.
    """
    module = sys.modules.get(base)
    if module is None or not isinstance(module, types.ModuleType):
        return None
    if hasattr(module, attribute):
        return None
    return "module"


def _from_import_kind(module_name, attribute):
    """The runtime kind of ``from <module_name> import <attribute>``, or ``None``.

    #389. ``from os import path as cs`` then ``with cs:`` enters a **module**,
    # which has no ``__enter__``. The header raises ``TypeError`` before the
    # body, so the assert under it is unreachable and the analyzer was
    # certifying it as load-bearing.

    ``_carrier_runtime_kinds`` declines every ``ImportFrom`` on purpose, and
    that decline is *not* lifted here. Measured, one spelling produces several
    runtime types and one of them is genuinely enterable:

        from os import path as cs            -> os.path          (module)
        from os import sep as cs             -> '/'              (str)
        from decimal import Decimal as cs    -> a class          (type)
        from contextlib import nullcontext as cs -> a CM       (enterable)

    So the question is not "is an ``ImportFrom`` a carrier" but "what does
    *this* attribute resolve to", and that is answered by the real
    interpreter. An enterable result returns ``None`` -- the header is live
    and the assert stands -- and only a result that cannot implement the
    protocol returns its type name. An unresolvable module or attribute
    returns ``None`` as well, because an unreadable import is not evidence of
    an unenterable one.
    """
    module = sys.modules.get(module_name)
    if module is None or not isinstance(module, types.ModuleType):
        return None
    try:
        value = getattr(module, attribute)
    except AttributeError:
        return None
    if hasattr(value, "__enter__"):
        # Enterable, so the header succeeds and the assert is live.
        return None
    return type(value).__name__
