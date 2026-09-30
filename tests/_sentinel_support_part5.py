# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part5":
    raise ImportError(
        "tests._sentinel_support_part5 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


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
    if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name):
        name = expression.func.id
    if name is None:
        return None
    module = tree if tree is not None else _module_tree()
    classes = _locally_defined_classes(module, function, expression)
    node = classes.get(name)
    if isinstance(expression, ast.Call) and node is not None and function is not None:
        scopes = _class_lookup_scopes(module, expression)
        for index in reversed(range(len(scopes))):
            scope = scopes[index]
            cutoff = _class_scope_read_position(scopes, index, expression)
            if cutoff is None and not isinstance(scope, ast.Module):
                return None
            if not isinstance(scope, ast.Module):
                parameters = {
                    argument.arg
                    for argument in ast.walk(scope.args)
                    if isinstance(argument, ast.arg)
                }
                if name in parameters and node not in scope.body:
                    return None
            stores = scope.body if isinstance(scope, ast.Module) else _scope_body_nodes(scope)
            for store in stores:
                names = _store_target_names_of(store)
                if isinstance(store, (ast.Import, ast.ImportFrom)):
                    names = [alias.asname or alias.name.split(".")[0] for alias in store.names]
                elif (
                    isinstance(store, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    or isinstance(store, ast.ExceptHandler)
                    and store.name is not None
                ):
                    names = [store.name]
                if name not in names or store is node:
                    continue
                if cutoff is not None and (store.lineno, store.col_offset) >= (
                    cutoff.lineno,
                    cutoff.col_offset,
                ):
                    continue
                if node not in scope.body or (store.lineno, store.col_offset) > (
                    node.lineno,
                    node.col_offset,
                ):
                    return None
            if node in scope.body:
                break
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
        node = _constructed_class_of(expression, module, function)
        if node is None:
            return None
    for child in node.body:
        if not isinstance(child, ast.FunctionDef) or child.name != "__exit__":
            continue
        if _provably_truthy_exit(child):
            return node
    return None


def _constructed_class_of(expression, tree, function=None):
    """The class an instance-valued ``expression`` was constructed from.

    Reads the ``helper = Suppressor()`` that the ``with`` header names, and
    returns its ``ClassDef`` when that class is one this file defines. Anything
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
    value = None
    for scope in reversed(_class_lookup_scopes(tree, function)):
        before = expression if not isinstance(scope, ast.Module) else None
        value = _assigned_value(expression.id, scope, before)
        if value is not None:
            break
    if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)):
        return None
    # An instance retains the class used at construction, even when the
    # class name is subsequently redefined before the with header.
    return _locally_defined_classes(tree, function, value).get(value.func.id)


def _assigned_value(name, scope, before=None):
    """The right-hand side ``name`` is assigned in ``scope``, or ``None``.

    The last assignment before the read wins in source order, and a
    function-scope lookup is offered before a module one by the caller so an
    inner binding shadows an outer one of the same name.

    ``ast.walk`` is deliberately *not* used on a module: it descends into every
    function body, so a module lookup would silently find a function-local
    assignment and the two scopes would not be distinguishable. The module
    scope is therefore its own top-level statement list, which is what makes
    the function-first lookup in :func:`_constructed_class_of` meaningful.

    A ``for`` target is a store too, and it is the one this used to miss. #370
    taught the module to record a loop target's element for the *suppression*
    question; :func:`_constructed_class_of` asks the same "what does this name
    hold" question, so the same answer has to be readable here:

        for cs in [Suppressor()]:      # `__exit__` returns True for AssertionError
            with cs:
                assert x != 1          # swallowed; was reported enforced

    Read by :func:`_single_loop_element`, so the limit is #370's already-measured
    one rather than a second, looser rule: only a single-element literal
    iterable contributes, and a multi-element or non-literal iterable stays
    ``None`` and keeps the assert ``enforced`` -- the safe direction, since a
    guessed value would read a suppressor that is not the one in force.

    Only an ``ast.Name`` target is followed, matching the ``Assign`` arm: a
    tuple target (``for a, b in ...``) unpacks and is not read here.
    """
    if scope is None:
        return None
    if isinstance(scope, ast.Module):
        nodes = iter(scope.body)
    elif isinstance(scope, ast.AST):
        nodes = _scope_body_nodes(scope)
    else:
        nodes = iter(())
    value = None

    def position(node):
        return node.lineno, node.col_offset

    nodes = [node for node in nodes if isinstance(node, (ast.For, ast.Assign))]
    for node in sorted(nodes, key=position):
        if before is not None and position(node) >= position(before):
            continue
        if isinstance(node, ast.For):
            if not (isinstance(node.target, ast.Name) and node.target.id == name):
                continue
            element = _single_loop_element(node.iter)
            if element is None:
                # Undecidable: leave any earlier `Assign` in force rather than
                # overwrite it with a guess. #385 measured the alternative.
                continue
            value = element
            continue
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


def _locally_defined_classes(tree, function=None, before=None):
    """Classes visible in the nearest lexical scope at this read position.

    Sibling functions and class bodies define separate namespaces. Within a
    function, a later definition replaces an earlier one only after it runs.
    Module definitions are read after module initialization for function-local
    constructor calls; a module-level construction instead uses its own point.
    """
    scopes = _class_lookup_scopes(tree, function)
    # A constructor may have been assigned at module scope before the function
    # was called. Its class is resolved at construction, not at the later use.
    if before is not None:
        scopes = _class_lookup_scopes(tree, before)
    found = {}

    def position(node):
        return node.lineno, node.col_offset

    for index, scope in enumerate(scopes):
        definitions = [node for node in scope.body if isinstance(node, ast.ClassDef)]
        if not isinstance(scope, ast.Module):
            # A local class name shadows a module name even before its first
            # definition, when reading it would raise UnboundLocalError.
            for node in definitions:
                found.pop(node.name, None)
        cutoff = _class_scope_read_position(scopes, index, before)
        for node in definitions:
            if cutoff is None or position(node) < position(cutoff):
                found[node.name] = node
    return found


def _class_scope_read_position(scopes, index, read):
    """An enclosing class cell is read when its child is invoked."""
    scope = scopes[index]
    if isinstance(scope, ast.Module):
        return read if len(scopes) == 1 else None
    if index == len(scopes) - 1:
        return read
    child = scopes[index + 1]
    return next(
        (
            statement.value
            for statement in scope.body
            if isinstance(statement, (ast.Expr, ast.Return))
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == child.name
        ),
        None,
    )


def _class_lookup_scopes(tree, target):
    """Module and enclosing function scopes, excluding unrelated siblings."""
    if target is None or target is tree:
        return [tree]
    path = []

    def find(node):
        path.append(node)
        if node is target:
            return True
        for child in ast.iter_child_nodes(node):
            if find(child):
                return True
        path.pop()
        return False

    if not find(tree):
        return [tree]
    return [
        node
        for node in path
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _unbound_local_class_constructor(value, function, module=None):
    """A local or captured class must have been bound when construction runs."""
    if (
        not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        or not isinstance(value, ast.Call)
        or not isinstance(value.func, ast.Name)
    ):
        return False
    name = value.func.id
    nodes = list(_scope_body_nodes(function))
    if not any(node is value for node in nodes):
        # A module-created instance is not constructed in this function's
        # namespace, even when this function later shadows its class name.
        return False
    tree = module if module is not None else _owning_module(function)
    scopes = _class_lookup_scopes(tree, value)

    def position(node):
        return node.lineno, node.col_offset

    for index in reversed(range(1, len(scopes))):
        scope = scopes[index]
        nodes = list(_scope_body_nodes(scope))
        if not any(isinstance(node, ast.ClassDef) and node.name == name for node in nodes):
            continue
        if _declares(scope, {name}, ast.Global) or _declares(scope, {name}, ast.Nonlocal):
            return False
        if any(
            argument.arg == name
            for argument in ast.walk(scope.args)
            if isinstance(argument, ast.arg)
        ):
            return False
        cutoff = _class_scope_read_position(scopes, index, value)
        if cutoff is None:
            return False
        return not any(
            position(node) < position(cutoff) and any(_names_bound_by_statement(node, name))
            for node in nodes
            if isinstance(node, ast.stmt)
        )
    return False


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


#: The parameter names ``contextlib`` and the stdlib use for the raised
#: exception in ``__exit__``. Any of them means the same thing at runtime, and
#: a user-written manager picks whichever reads best, so all are accepted.
_EXIT_EXCEPTION_PARAMS = frozenset({"exc", "exc_type", "et", "e", "err", "exc_info"})


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


#: Runtime types a store's value is pinned to when the right-hand side is a
#: literal, and which therefore cannot implement the context manager protocol
#: whatever the surrounding code intends. Entering one raises `TypeError` on the
#: header, before the body runs, so an assert in that body is unreachable
#: (#336).
NON_CONTEXT_MANAGER_TYPES = frozenset(
    {
        "NoneType",
        "bool",
        "int",
        "float",
        "complex",
        "str",
        "bytes",
        "list",
        "tuple",
        "set",
        "dict",
        # #359. A name bound by a string field of a node that is not a target
        # at all holds an object that cannot be entered: a module, a class or
        # a function. Entering one raises `TypeError` *before* the assert under
        # the `with` is evaluated, so the assert is defeated -- and the
        # analyzer, with no store entry to read, was certifying it as
        # load-bearing. The damaging direction.
        "module",
        "type",
        "function",
        # #388. The builtin constructors read by `_builtin_constructor_kind`
        # have to be non-enterable *here* as well: a type name the set omits
        # makes the constructor read as possibly-enterable, and the guard then
        # declines a header CPython settles to a value it cannot enter. `range`
        # and `frozenset` are the two a first pass missed, because they are
        # builtins that are not spelled in everyday source as a `with` target
        # and so were not already listed.
        "frozenset",
        "range",
        "bytearray",
    }
)

#: Bare builtin constructors whose result is a value no ``with`` can enter.
#:
#: ``cs = int()`` and ``cs = list()`` are decided by the *name being called*,
#: not by the call being a call. Every entry here is a builtin type object
#: spelled without a module prefix, so a call of it returns a fresh instance of
#: the corresponding :data:`NON_CONTEXT_MANAGER_TYPES` entry and nothing else.
#: Reading these as "a call is a call" reports a dead header as live.
#:
#: Only the *unqualified* spellings are listed. ``builtins.int()`` is
#: recognised separately by :func:`_builtin_constructor_kind`, and a
#: user-shadowed ``int`` is a different binding entirely -- shadowing is not
#: modelled here, so the conservative direction is taken for a name this
#: module cannot prove is the builtin.
_BUILTIN_CONSTRUCTOR_TYPES = {
    "int": "int",
    "float": "float",
    "complex": "complex",
    "str": "str",
    "bytes": "bytes",
    "bool": "bool",
    "list": "list",
    "tuple": "tuple",
    "set": "set",
    "frozenset": "frozenset",
    "dict": "dict",
    "bytearray": "bytearray",
    "range": "range",
}

#: Marker for a constructor call that *raises* for the arguments given, so the
#: store it appears in never happens. ``range()`` with no argument is a
#: `TypeError`; the name keeps whatever was bound before, which for a header
#: following a carrier is the carrier itself.
_RAISING_CONSTRUCTOR = object()

#: Constructors called with no arguments that are **falsy**: `if set():` never
#: enters its body, and `for cs in set():` runs zero times so the loop never
#: binds the name. Both spellings of "empty builtin container" are decided from
#: this one set, so the condition form and the loop form cannot disagree.
_EMPTY_CONSTRUCTOR_TYPES = frozenset({"set", "frozenset", "dict", "list", "tuple", "bytearray"})


def _builtin_constructor_kind(value, function=None):
    """The type name a zero-argument builtin constructor call produces.

    ``int()`` is decided by the callee's name, so this is the *call* form of
    :func:`_literal_runtime_type`: a bare ``ast.Name`` callee drawn from
    :data:`_BUILTIN_CONSTRUCTOR_TYPES`, or the same spelled as ``builtins.int``.

    ``function``, when given, is the scope the call appears in. A name the
    function itself rebinds -- ``def int(): ...``, ``int = ...``, ``import int``
    -- is a *different* binding, and calling it may return anything at all,
    including a real context manager. Reading it as the builtin produces a
    false-dead: the header is reported dead while CPython enters it. A shadowed
    name therefore answers ``None`` here and falls through to the caller's
    ordinary "a call is a call" rule, which is the safe direction.

    Returns ``None`` for every other call, so the caller's existing "a call is
    a call" rule stays in force. That rule is the safe direction: a
    ``nullcontext()`` really does return a context manager, and declining to
    read it keeps a live header live.
    """
    if isinstance(value, ast.Call) and _callee_is_shadowed(value.func, function, value):
        return None
    if not isinstance(value, ast.Call):
        # A constructor called with arguments may return a subclass or a
        # different object entirely (``bool(1)`` is still bool, but
        # ``range(3)`` is a range and ``str(b"")`` is a str, while a future
        # spelling need not be). The constructors below are read through
        # :func:`_builtin_constructor_arguments_are_ignorable` instead, which
        # admits the argument forms that cannot change the result type.
        return None
    func = value.func
    if isinstance(func, ast.Name):
        kind = _BUILTIN_CONSTRUCTOR_TYPES.get(func.id)
    elif (
        isinstance(func, ast.Attribute)
        and func.attr in _BUILTIN_CONSTRUCTOR_TYPES
        and isinstance(func.value, ast.Name)
        and func.value.id == "builtins"
    ):
        kind = _BUILTIN_CONSTRUCTOR_TYPES[func.attr]
    else:
        return None
    if kind is None or not _builtin_constructor_arguments_are_ignorable(func, value):
        return None
    return kind


def _name_is_shadowed_in(func, function):
    """Is the callee a name this function rebinds away from the builtin?

    Only a *binding in the function's own scope* counts, and only for a bare
    ``ast.Name`` callee: ``cs = range(3)`` is only the builtin when nothing
    in scope rebinds ``range``. A ``def``/``class``/``import``/assignment to
    that name anywhere in the function body makes the call a different one.

    Nested scopes bind their own names, so a ``def range(...)`` inside another
    ``def`` inside this function does not shadow the builtin's meaning *here*;
    those are skipped, matching :func:`_own_imports`' scope discipline.
    """
    if not isinstance(func, ast.Name):
        return False
    name = func.id
    for node in _own_scope_bindings(function):
        if node is name:
            continue
        for bound in _names_bound_by_statement(node, name):
            if bound:
                return True
    return False


def _name_is_rebound_away_from_module(name_node, function, call=None):
    """Is ``name_node.id`` rebound to something other than its own module?

    The question is asked only of a name that a qualified ``builtins.attr``
    call is written through, so the answer separates the one import that
    *is* the module from every other way the name can be taken:

    * ``import builtins`` and ``import builtins as builtins`` bring the real
      module in and leave the name meaning that module;
    * a parameter, a local store, a function-local ``def``/``class``, or a
      module-level store of any kind replace it with an arbitrary object, so
      ``builtins.attr`` is then some attribute of *that* object.

    ``from builtins import list`` does not bind the name ``builtins`` at all,
    so it never reaches here; a store that is the module's own canonical import
    is the only shape that keeps the qualified form trustworthy.
    """
    name = name_node.id
    if function is None:
        # No scope to consult, so the name cannot be proved to be the module.
        return True
    if name in _signature_bound_names(function):
        return True
    for node in _own_scope_bindings(function):
        if node is name_node:
            continue
        for bound in _names_bound_by_statement(node, name):
            if bound and not _is_canonical_module_import(node, name):
                return True
    return _module_rebinds_name(function, name, call)


def _is_canonical_module_import(statement, name):
    """Is this binding the ordinary ``import <name>`` of the module itself?

    A ``from M import x`` is a canonical binding of ``x`` in exactly the same
    sense, provided ``x`` really is an attribute of ``M``. It names the
    attribute directly, so the qualified form the callers are about to resolve
    -- ``x`` standing for ``M.x`` -- is trustworthy, and counting the
    from-import as a *rebinding* was the defect #369's filed fixture trips:

        from contextlib import nullcontext

        def outer(...):
            subject = [nullcontext()]      # <- the veto fired here

    :func:`_module_rebinds_name` asks whether ``name`` has been taken over by
    something other than the module, and answered ``True`` for the very
    statement that established the correct binding, so every bare from-import
    alias was refused and a fired assert was certified defeated.

    The attribute check is what keeps this from over-widening. ``from M
    import x`` binds ``x`` to ``M.x`` only if ``M`` exports ``x``; a name the
    module does not export is still an arbitrary object and is deliberately
    left counting as a rebinding. Resolution is asked of the real interpreter
    and declines when it cannot read, so an unreadable answer stays on the
    conservative side.
    """
    if isinstance(statement, ast.Import):
        return any(
            (alias.asname or alias.name) == name and alias.name == name for alias in statement.names
        )
    if isinstance(statement, ast.ImportFrom) and statement.module:
        for alias in statement.names:
            if (alias.asname or alias.name) != name:
                continue
            # `from M import x` -- the name is the attribute `x` of module `M`.
            if not _module_exports_attribute(statement.module, alias.name):
                continue
            return True
    return False


def _module_exports_attribute(module_name, attribute):
    """Does ``module_name`` export ``attribute``? ``False`` when unreadable."""
    import sys

    module = sys.modules.get(module_name)
    if module is None:
        # The module has not been imported, so the attribute cannot be read off
        # it. Importing here would be a side effect this check must not cause.
        return False
    try:
        return hasattr(module, attribute)
    except Exception:  # noqa: BLE001 - a module with a hostile __getattr__
        # An unreadable attribute is declined, which is the safe direction:
        # the name stays a rebinding, so the qualified form stays untrusted.
        return False


def _callee_is_shadowed(func, function, call=None):
    """Is ``func`` a call target that does not reach the real builtin?

    This is the full shadowing question, and it is a strict superset of
    :func:`_name_is_shadowed_in`. That helper only ever looked inside the
    function's *body*, which missed the two bindings that are not written in
    the body at all:

    * a **parameter** -- ``def f(int=None): cs = int()`` binds ``int`` for the
      whole call, so the call is whatever the caller passed. Reading it as the
      builtin ``int`` reports ``cs`` as a zero that cannot be entered;
    * a **module-level** binding -- ``def int(): ...`` at module scope binds
      ``int`` for every function in the file, and a module-level
      ``cs = int()`` is a store that has already run by the time any header
      reads it.

    Both were read as the builtin, so a header CPython enters was reported
    dead. A parameter or a module binding can only make the call *less*
    determinate, so the answer here is always the conservative one: treat the
    callee as shadowed and let the caller's ordinary "a call is a call" rule
    keep the header live.

    ``function`` may be ``None`` -- the condition path is handed no enclosing
    scope by some callers -- and a caller that does not know the scope cannot
    prove the name is the builtin either, so it is answered the same
    conservative way rather than as "not shadowed".

    ``call`` is the ``ast.Call`` whose callee ``func`` is, when the caller has
    it. It only tells the module walk where to stop, so it is optional.
    """
    if isinstance(func, ast.Attribute):
        # `builtins.int` only means the builtin when `builtins` itself is not
        # rebound, so the question moves to the attribute's own base name.
        # Returning `False` for the name outright answered every `builtins.*`
        # call as the real module, including one through a name that some
        # store has replaced:
        #
        #     builtins = SimpleNamespace(list=nullcontext)
        #     cs = builtins.list()      # any callable, not the builtin `list`
        #
        # That read a `TypeError`-raising header as LIVE. `import builtins` is
        # the one binding that must still count as canonical: it is not a
        # *rebinding* but the ordinary import that brings the real module in,
        # and every other `import x` in the file is exactly the spelling that
        # means "this is the module named x". Treating it as a shadow would
        # make `cs = builtins.list()` LIVE where CPython raises.
        if isinstance(func.value, ast.Name) and func.value.id == "builtins":
            return _name_is_rebound_away_from_module(func.value, function, call)
        return _callee_is_shadowed(func.value, function, call)
    if not isinstance(func, ast.Name):
        return False
    if function is None:
        # No enclosing scope to consult. A name the caller could not resolve
        # is not provably the builtin, so it is answered as shadowed.
        return True
    if func.id in _signature_bound_names(function):
        return True
    if _name_is_shadowed_in(func, function):
        return True
    return _module_binds_name(function, func.id, call)


def _signature_bound_names(function):
    """Every name the function's own signature binds when it is called.

    The positional parameters, the keyword-only parameters, ``*args`` and
    ``**kwargs`` all bind for the duration of the call, so each of them
    shadows the builtin of the same name inside the body. ``*args`` and
    ``**kwargs`` are matched through their own attribute, because ``ast``
    records those under ``vararg``/``kwarg`` rather than in the flat ``args``
    list -- a filter reading only ``args`` saw a call whose shadow arrives
    through ``*values`` as an un-shadowed builtin.
    """
    args = getattr(function, "args", None)
    if args is None:
        return frozenset()
    names = {argument.arg for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return frozenset(names)


def _module_binds_name(function, name, call=None):
    """Does the module holding ``function`` bind ``name`` at module scope, and
    does that binding run before ``function`` does?

    A module-level ``def int(): ...``, ``import int`` or ``int = ...`` rebinds
    the name for every function in the file, and a module-level ``cs = int()``
    is a real store that has already run by the time any header reads it.

    Two things have to be true, and asking for either alone was a false-live:

    * the binding must be in the **module** scope. This walk starts at the
      module's own statements, so ``outer``'s own ``def list(): ...`` -- a
      binding one scope *below* the module -- is never consulted here. Before
      this walk existed the question was reached through the module, so a
      function-local definition was answered twice: once correctly by
      :func:`_name_is_shadowed_in` and once, wrongly, from here.
    * the binding must be in force **where the call is evaluated**, and that
      point is not the same for the two kinds of call:

      - a call written at **module scope** is evaluated as the module runs, so
        only the statements before it count:

            cs = list()             # the builtin -- runs first
            def list(): return None # too late to matter for the line above

      - a call inside a **function body** is evaluated only when that function
        is called, which is after the *whole* module has finished executing.
        Every module binding therefore counts, including one written after the
        function's own ``def``:

            def outer(x):
                cs = list()         # `list` is whatever the module ended with
                with cs: ...
            def list(): return nullcontext()

        Reading that ``cs`` as the builtin reported a header CPython enters as
        DEAD. Cutting the walk at the function's ``def`` was the fix for the
        module-scope case above, and it is exactly wrong for this one.

      The two are told apart by whether the call is inside the analysed
      function's own scope, which :func:`_call_runs_at_module_scope` answers.

    The walk descends into module-level blocks -- a binding inside a
    module-level ``if`` still binds the name whenever that branch is taken --
    while a nested ``def``/``class`` *body* stays a separate scope, matching
    :func:`_own_scope_bindings`' discipline.

    ``call`` is the ``ast.Call`` node the question is being asked about, when
    the caller has it. It only decides *where* the walk stops, so omitting it
    is safe -- it just falls back to stopping at the function's own ``def``,
    which is the conservative choice for a function-body call.
    """
    module = _module_for_function(function)
    if module is None:
        return False
    if _call_runs_at_module_scope(function, call):
        return _module_binds_name_at_module_scope(module, function, call, name)
    # The call is inside a function body, so it runs only after the whole
    # module has executed and every module binding is in force. A nested
    # `def`/`class` *body* is still a separate scope and is not counted.
    for statement in getattr(module, "body", []):
        if any(_names_bound_by_statement(statement, name)):
            return True
        for node in _module_level_bindings(statement):
            if any(_names_bound_by_statement(node, name)):
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
