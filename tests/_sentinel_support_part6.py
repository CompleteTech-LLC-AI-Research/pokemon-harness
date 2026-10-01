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


def _literal_runtime_type(value, function=None):
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
    # #464 residual. A zero-argument builtin constructor call produces a
    # value whose type the callee already fixes, so the same question
    # `_literal_runtime_type` answers for a literal is decidable here too:
    #
    #     m = list()
    #     with (cs := m):      # enters a list -> TypeError before the body
    #         assert x != 1    # unreachable, but reported `enforced`
    #
    # `_builtin_constructor_kind` is the existing rule for this and already
    # declines a shadowed callee, so a `def list(): ...` or a rebound `list`
    # keeps its ordinary "a call is a call" answer -- which is the safe
    # direction, because a shadowing `list()` may well return a real context
    # manager. Delegating rather than re-deciding is the point: a hand-kept
    # list of constructor names would have to agree with the one that already
    # answers the emptiness and type questions elsewhere.
    kind = _builtin_constructor_kind(value, function)
    if kind is not None:
        return kind
    # A bare builtin *name* is the same object without the call: `m = int`
    # binds the type itself and `m = len` binds a builtin function, and neither
    # can be entered. Only the function's *own* rebound names are excluded --
    # `m = int` after `def int(): ...` is that function instead, and it may
    # well be a real context manager.
    #
    # The question is asked of the real ``builtins`` module rather than of a
    # kept list of names, for the reason the probe table is: a list would have
    # to be right about every exported name, and a miss is a wrong verdict
    # about a real contract. Nothing here enumerates them; the interpreter
    # answers "what is this object's type, and can *that* be entered" and the
    # name is recorded under the *type of the object it names*, so a kind the
    # probe table has never seen still declines through `_runtime_kind_can_enter`.
    if isinstance(value, ast.Name) and not _callee_is_shadowed(value, function):
        return _bare_builtin_object_kind(value.id)
    return None


def _bare_builtin_object_kind(name):
    """The runtime kind a bare builtin *name* binds, or ``None`` to decline.

    ``m = int`` binds a class object and ``m = len`` a builtin function; both
    are entered with a ``TypeError`` before the body runs, so an assert under
    the header is unreachable while the analyzer was certifying it. The kind
    reported is the type of the object the name resolves to, which keeps the
    answer on the probe path: ``builtin_class`` for a class object, the
    function's own type name for a function, and whatever a builtin constant
    happens to be.

    A name the ``builtins`` module does not export declines, as does one it
    exports but that resolves to nothing usable. The caller has already
    established that the name is not rebound in this scope, so an unresolved
    name is genuinely the builtin and this is the only remaining question.
    """
    try:
        import builtins

        obj = getattr(builtins, name)
    except (AttributeError, ImportError):
        return None
    if isinstance(obj, type):
        # A *class object* is reported under a kind of its own rather than as
        # ``type``. Whether a class can be entered is decided by its
        # **metaclass**, not by the class: every bare builtin class has the
        # builtin ``type`` as its metaclass and so cannot be entered, while a
        # class written in the file may have a metaclass that defines
        # ``__enter__`` and is genuinely enterable. `_classdef_runtime_kind`
        # already proves the locally-defined half separately, and collapsing
        # both onto one ``type`` kind would let a probed builtin class answer
        # for a local class that has a live metaclass protocol.
        #
        # So the enterability question is asked of ``type(obj)`` and not of
        # ``obj``. Asking it of ``obj`` was wrong in the damaging direction:
        # ``memoryview`` is the one exported builtin whose *class object*
        # carries ``__enter__``, because ``memoryview.__enter__`` is the
        # instance-level context-manager method. CPython entered the *class*
        # and raised before the body --
        #
        #     >>> with memoryview as v: ...
        #     TypeError: 'type' object does not support the context manager protocol
        #
        # -- so the assert under the header is unreachable, while the
        # instance spelling ``m = memoryview(b"xy")`` really is enterable and
        # must stay live. The old ``hasattr(obj, "__enter__")`` gate could not
        # tell those two apart and declined both, reporting the unreachable
        # one as enforced. Every bare builtin class has the builtin ``type``
        # as its metaclass, so asking ``type`` settles all of them at once --
        # no count is written here because the exported set is interpreter
        # specific, and a hardcoded one goes stale without changing the claim.
        if hasattr(type(obj), "__enter__"):
            return None
        return "builtin_class"
    # A non-class builtin is entered as itself, so this object *is* the one
    # ``__enter__`` is looked up on. Nothing currently exported answers this
    # branch, and asking the interpreter rather than keeping a list is what
    # keeps that from being an assumption: a future singleton that does
    # implement the protocol still declines, and its header stays live.
    if hasattr(obj, "__enter__"):
        return None
    return type(obj).__name__


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
        from decimal import Decimal as cs    -> a class          (unenterable)
        from contextlib import nullcontext as cs -> a CM *class* (unenterable)

    So the question is not "is an ``ImportFrom`` a carrier" but "what does
    *this* attribute resolve to", and that is answered by the real
    interpreter. An enterable result returns ``None`` -- the header is live
    and the assert stands -- and only a result that cannot implement the
    protocol returns its type name. An unresolvable module or attribute
    returns ``None`` as well, because an unreadable import is not evidence of
    an unenterable one.

    #457. The enterability test is on the *metatype*, not on the value. ``with``
    performs the special-method lookup on ``type(value)``, so a class is
    enterable only when its metatype implements the protocol. Measured, the
    naive ``hasattr(value, "__enter__")`` inverts the verdict for every
    context-manager *class*:

        contextlib.suppress      hasattr -> True    enterable -> TypeError
        contextlib.nullcontext   hasattr -> True    enterable -> TypeError
        contextlib.suppress(...)  hasattr -> True    enterable -> yes

    A class exposes ``__enter__`` as the *unbound function* that its instances
    will use, which is exactly the attribute the class itself is not entitled
    to. Entering a class therefore raises ``TypeError: 'ABCMeta' object does
    not support the context manager protocol``, and it raises during the
    protocol check -- the metatype is consulted and found wanting, so no
    ``__enter__`` is ever called. Answering "is this value enterable" by
    asking the value therefore certified a dead assert as live -- the
    damaging direction. The fix is to look the dunder up where the interpreter
    looks it up.
    """
    module = sys.modules.get(module_name)
    if module is None or not isinstance(module, types.ModuleType):
        return None
    try:
        value = getattr(module, attribute)
    except AttributeError:
        return None
    if hasattr(type(value), "__enter__"):
        # Enterable, so the header succeeds and the assert is live.
        #
        # #457. The lookup is on the metatype because that is the object the
        # interpreter searches: `with cs:` evaluates `type(cs).__enter__`. A
        # context-manager *class* exposes `__enter__` on itself as the unbound
        # method its instances use, so asking the value would call a CM class
        # enterable and pin a false LIVE; asking `type(value)` asks the
        # metatype, which is what actually has to define the protocol, and
        # keeps a CM *instance* -- whose own type does define it -- enterable.
        #
        # Reading the metatype rather than testing `isinstance(value, type)`
        # is what makes the class case correct in *both* directions. A class
        # with an enterable metaclass really does support the protocol --
        # `class Meta(type)` defining `__enter__`/`__exit__` makes
        # `with CM:` enter and bind the class itself -- so declaring every
        # class unenterable would pin a false DEAD there. Asking the metatype
        # answers that case the way the interpreter does.
        return None
    if isinstance(value, type):
        # #457. The value is a class and its metatype cannot be entered, so it
        # is reported as the kind the rest of the module already uses for a
        # class: `"type"`. The bare `type(value).__name__` would instead
        # return the *metatype's* name, and for an abstract base such as
        # `contextlib.suppress` that is `"ABCMeta"` -- a name
        # `NON_CONTEXT_MANAGER_TYPES` does not list, so the caller would treat
        # the unenterable header as unreadable and report the assert live
        # again, the same false LIVE one line above. `_carrier_runtime_kinds`
        # already records a `ClassDef` as `"type"`, so this keeps the two
        # producers of that kind in agreement.
        return "type"
    return type(value).__name__
