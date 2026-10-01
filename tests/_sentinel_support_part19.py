# Shared-namespace fragment for reviewed sentinel helpers.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part19":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")

# A source-proven non-suppressing metaclass protocol is the only widened case.
_ENTERABLE_CLASS_CARRIER = "class-live-protocol"


def _classdef_runtime_kind(statement, function):
    """Widen the existing class gate only for a settled live metaclass proof.

    Unknown/decorated/inherited shapes retain the prior conservative class
    verdict. This does not claim their entered runtime value is unenterable.
    """
    return (
        _ENTERABLE_CLASS_CARRIER
        if _class_carrier_has_live_protocol(statement, function)
        else "type"
    )


def _class_carrier_has_live_protocol(statement, function):
    if (
        statement.bases
        or statement.decorator_list
        or getattr(statement, "type_params", ())
        or len(statement.keywords) != 1
    ):
        return False
    keyword = statement.keywords[0]
    if keyword.arg != "metaclass" or not isinstance(keyword.value, ast.Name):
        return False
    name = keyword.value.id
    if {name, statement.name} & _signature_bound_names(function):
        return False
    definitions = [
        node for node in _own_scope_bindings(function) if any(_names_bound_by_statement(node, name))
    ]
    if len(definitions) != 1 or not isinstance(definitions[0], ast.ClassDef):
        return False
    meta = definitions[0]
    if (
        meta.lineno >= statement.lineno
        or meta.decorator_list
        or meta.keywords
        or len(meta.bases) != 1
    ):
        return False
    if not isinstance(meta.bases[0], ast.Name) or meta.bases[0].id != "type":
        return False
    if not _class_protocol_builtin_type_is_unshadowed(function):
        return False
    if any(
        not isinstance(node, ast.Pass)
        and not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        for node in statement.body
    ):
        return False
    methods = {}
    for node in meta.body:
        if (
            isinstance(node, ast.Pass)
            or isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if not isinstance(node, ast.FunctionDef) or node.name in methods:
            return False
        if node.name not in (
            "__enter__",
            "__exit__",
        ) or not _class_protocol_method_creation_is_inert(node):
            return False
        methods[node.name] = node
    if set(methods) != {"__enter__", "__exit__"}:
        return False
    enter, exit_function = methods["__enter__"], methods["__exit__"]
    enter_args = [*enter.args.posonlyargs, *enter.args.args]
    exit_args = [*exit_function.args.posonlyargs, *exit_function.args.args]
    if len(enter_args) != 1 or enter.args.vararg or len(enter.body) != 1:
        return False
    if (
        not isinstance(enter.body[0], ast.Return)
        or not isinstance(enter.body[0].value, ast.Name)
        or enter.body[0].value.id != enter_args[0].arg
    ):
        return False
    if not (len(exit_args) == 4 or len(exit_args) == 1 and exit_function.args.vararg):
        return False
    if len(exit_function.body) != 1 or not isinstance(exit_function.body[0], ast.Return):
        return False
    value = exit_function.body[0].value
    if not isinstance(value, ast.Constant) or value.value is not False:
        return False
    if not _class_protocol_setup_is_inert(function, meta, statement):
        return False
    module = _module_for_function(function)
    scopes = [function] if module is None else [module]
    # No opaque callbacks or member edits can change the proven protocol.
    return not any(
        isinstance(node, (ast.Call, ast.NamedExpr))
        or isinstance(node, ast.Attribute)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        for scope in scopes
        for node in ast.walk(scope)
    )


def _class_protocol_setup_is_inert(function, meta, carrier):
    """Keep implicit execution and competing stores outside this proof."""
    if not _class_protocol_method_creation_is_inert(function):
        return False
    parameters = {
        arg.arg
        for arg in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    }
    protected = {meta.name, carrier.name, *_signature_bound_names(function)}
    headers = 0
    for node in function.body:
        if node is meta or node is carrier or isinstance(node, ast.Pass):
            continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and all(
                isinstance(target, ast.Name) and target.id not in protected
                for target in node.targets
            )
        ):
            continue
        if isinstance(node, ast.With) and len(node.items) == 1:
            item = node.items[0]
            queries = [child for child in node.body if isinstance(child, ast.Assert)]
            if not (
                isinstance(item.context_expr, ast.Name)
                and item.context_expr.id == carrier.name
                and item.optional_vars is None
                and len(queries) == 1
                and all(isinstance(child, (ast.Assert, ast.Pass)) for child in node.body)
                and _class_protocol_has_literal_failure(queries[0], parameters)
            ):
                return False
            headers += 1
            continue
        return False
    if headers != 1:
        return False
    module = _module_for_function(function)
    if module is None:
        return True
    for node in module.body:
        if node is function or isinstance(node, ast.Pass):
            continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and all(isinstance(target, ast.Name) for target in node.targets)
        ):
            continue
        return False
    return True


def _class_protocol_has_literal_failure(query, parameters):
    """The filed parameter/literal comparison has an inert failing witness."""
    test = query.test
    return (
        (query.msg is None or isinstance(query.msg, ast.Constant))
        and isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id in parameters
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.NotEq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and isinstance(test.comparators[0].value, (str, bytes, int, float, bool, type(None)))
    )


def _class_protocol_method_creation_is_inert(method):
    arguments = [*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs]
    arguments.extend(arg for arg in (method.args.vararg, method.args.kwarg) if arg)
    return not (
        method.decorator_list
        or method.returns is not None
        or getattr(method, "type_params", ())
        or method.args.defaults
        or method.args.kwonlyargs
        or method.args.kwarg
        or any(arg.annotation is not None for arg in arguments)
    )


def _class_protocol_builtin_type_is_unshadowed(function):
    module = _module_for_function(function)
    scope = function
    while scope is not None:
        if "type" in _signature_bound_names(scope) or any(
            any(_names_bound_by_statement(node, "type")) for node in _own_scope_bindings(scope)
        ):
            return False
        scope = _nonlocal_parent_function(scope, module) if module is not None else None
    return module is None or not any(
        any(_names_bound_by_statement(node, "type"))
        for statement in module.body
        for node in _module_level_bindings(statement)
    )


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
        return _literal_entry_is_dead(expression.value) or _walrus_name_entry_is_dead(
            expression.value, by_index, index, function, bound, module
        )
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
            #
            # #468 widens only a proven non-suppressing metaclass protocol.
            if value is _ENTERABLE_CLASS_CARRIER:
                return False
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
