# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part8":
    raise ImportError(
        "tests._sentinel_support_part8 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


def _store_is_in_scope(statement, function):
    """Is ``statement`` part of ``function``'s own scope?

    ``_store_bindings`` records a store nested in a nested ``def``/``lambda``/
    ``class`` as an ordinary conditional binding of the same *spelling* of the
    name, because the name it binds really is called ``cs``. What differs is
    the *scope* it lands in: a class body is its own namespace and a nested
    ``def`` owns its locals, so neither rebinds the enclosing function's
    ``cs``. Measured:

        def outer(x, flag):
            import os as cs
            if flag:
                def inner():
                    cs = contextlib.nullcontext()
            with cs:          # cs is still the module -> TypeError, dead
                assert x != 1

    The boundary is the same one :func:`_scope_body_nodes` draws, and it is
    asked here by membership in that set rather than by re-walking, so the two
    rules cannot drift apart on what counts as a nested scope.

    A ``global`` (or ``nonlocal``) declaration moves the store *out* of the
    nested scope. At module level the ``global`` spelling is the whole of
    #375's `_rebind` shape:

        def _rebind():
            global cs
            cs = contextlib.nullcontext()

    writes the *module's* ``cs``, so the carrier really is superseded and the
    header really is live. Reading it as a nested local -- which is what
    membership alone would do -- would exclude the only store that decides the
    question and answer `defeated` on an assert CPython evaluates. So the
    nested scope is only a real boundary for a name that is *not* declared in
    an enclosing scope from within it.

    A declaration only moves a store out of the nested scope when it targets
    the scope that actually *owns* the queried carrier. ``global cs`` names the
    module namespace, so it supersedes a module-level ``cs`` and nothing else:

        def outer(x, flag):
            import os as cs
            if flag:
                def inner():
                    global cs        # writes the MODULE's cs
                    cs = contextlib.nullcontext()
            with cs:                # `outer`'s own local cs -> TypeError
                assert x != 1

    Here ``outer``'s ``cs`` is a function local, the nested store lands in the
    module, and the header is entered with the module -- so it raises on both
    paths and the assert is dead. Reading the ``global`` as if it governed
    ``outer``'s binding made the checker report that dead assert as live.
    ``nonlocal`` is the mirror image: it names a *function* scope, so it
    supersedes a function-scope carrier and says nothing about a module one.
    The two are told apart by the scope being queried, which is why this test
    reads ``function`` and not just the store.

    Only the declarations that *govern* the owning scope count. ``ast.walk``
    descends into scopes of its own, and a declaration in a grandchild says
    nothing about the store beside it:

        def outer(x, flag):
            import os as cs
            if flag:
                def inner():
                    cs = contextlib.nullcontext()      # inner's local
                    def grandchild():
                        global cs                      # no effect on inner
            with cs:                                  # still the module
                assert x != 1

    :func:`_scope_body_nodes` draws the boundary, and it is asked here by
    iteration rather than by re-walking, so the two rules cannot drift apart
    on what counts as a nested scope.
    """
    if any(node is statement for node in _scope_body_nodes(function)):
        return True
    owner = _owning_scope_of(statement, function)
    if owner is None:
        return False
    # `global` and `nonlocal` are told apart by the namespace they name: a
    # module-level carrier is the one a `global` can supersede, and a
    # function-scope carrier is the one a `nonlocal` can. Asking the
    # declaration alone would have each of them claim the other's case.
    declaration = ast.Global if isinstance(function, ast.Module) else ast.Nonlocal
    return _declares(owner, _store_target_names_of(statement), declaration)


def _target_is_starred(statement, name):
    """Does this store bind the name through an ``ast.Starred`` target?

    ``*cs, = (...)`` builds a list, and ``cs, *rest = (...)`` does not bind
    the starred name at all. Only the first shape binds a non-enterable value,
    so the two are told apart by asking whether ``name`` is *the* starred
    target -- which is why the name is a parameter. Matching any ``Starred``
    in the target list would exclude the second shape too, and there the
    starred element is discarded while ``cs`` keeps a perfectly enterable
    value, so excluding it would be wrong in the damaging direction.

    The recorded ``value`` is the whole right-hand side, so the starred
    *target* has to be read off the statement; :func:`_literal_runtime_type`
    sees the ``ast.Starred`` from the opposite direction, on the value, and
    reads the same list type for the non-carrier path.
    """
    targets = []
    if isinstance(statement, (ast.AnnAssign, ast.NamedExpr)):
        targets = [statement.target]
    elif isinstance(statement, ast.Assign):
        targets = statement.targets
    return any(
        isinstance(node, ast.Starred) and isinstance(node.value, ast.Name) and node.value.id == name
        for target in targets
        for node in ast.walk(target)
    )


def _store_target_names_of(statement):
    """The names a single store statement binds, or ``[]`` for a non-store.

    Thin wrapper over :func:`_store_target_names` so the scope test can ask
    the question in terms of one statement. A node that is not one of the
    store forms binds nothing here and is reported as binding nothing, which
    keeps the caller from having to enumerate the forms a second time.
    """
    if isinstance(statement, ast.Assign):
        return _store_target_names(statement.targets)
    if isinstance(statement, ast.AnnAssign):
        return _store_target_names([statement.target])
    if isinstance(statement, ast.NamedExpr):
        return _store_target_names([statement.target])
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return _store_target_names([statement.target])
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _store_target_names(
            [item.optional_vars for item in statement.items if item.optional_vars is not None]
        )
    if isinstance(statement, ast.Delete):
        return _store_target_names(statement.targets)
    if isinstance(statement, ast.ExceptHandler):
        return [statement.name] if statement.name is not None else []
    return []


def _owning_scope_of(statement, function):
    """The nested ``def``/``lambda``/``class`` that ``statement`` sits inside.

    ``None`` when the statement is part of ``function``'s own body, or when
    no single nested scope contains it. Only the *nearest* enclosing scope is
    returned, because a ``global`` declaration in an inner scope says nothing
    about an intermediate one, and a doubly-nested store writes to whichever
    scope declared its name.
    """
    return _nearest_scope_containing(function, statement)


def _nearest_scope_containing(scope, statement):
    """The innermost scope strictly inside ``scope`` that contains ``statement``."""
    for node in ast.iter_child_nodes(scope):
        if any(child is statement for child in ast.walk(node)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                # An inner scope wins: descend and prefer whatever is closest.
                return _nearest_scope_containing(node, statement) or node
            # `node` is a plain statement that contains the store, so the
            # scope boundary is further out; keep looking among the siblings
            # that also contain it.
            deeper = _nearest_scope_containing(node, statement)
            if deeper is not None:
                return deeper
    return None


def _declares(scope, names, kind):
    """Does ``scope`` govern its own stores with a ``kind`` declaration?

    ``global cs`` is the declaration that makes a nested store write to the
    *module's* binding, and it is what separates #375's ``_rebind`` -- whose
    store really does supersede a module carrier -- from a nested ``def`` that
    merely shadows the name. ``nonlocal cs`` does the same for the nearest
    *enclosing function* instead. The caller picks between them by the scope it
    is reasoning about, because the two name different namespaces.

    Only declarations that govern ``scope`` are read. A ``global`` in a nested
    ``def`` binds that ``def``'s stores, so it has no say over a store sitting
    in ``scope`` itself -- and ``ast.walk`` cannot draw that line, since it
    descends straight through the nested scope. :func:`_scope_body_nodes` is
    the boundary this module draws everywhere else, so it is what asks, and
    the nodes it yields are read *directly*.

    Reading them directly is what makes the boundary hold at any depth. A
    second walk over each yielded node re-enters a nested scope reached
    through a wrapper, so a ``nonlocal`` in a grandchild written under an
    ``if`` was still attributed to its grandparent:

        def inner():
            cs = contextlib.nullcontext()
            if True:
                def grandchild():
                    nonlocal cs

    ``_scope_body_nodes`` yields the ``FunctionDef`` and stops, and the check
    is a type test on the node itself, so the declaration inside it is never
    reached. A wrapper changes nothing, because ``_scope_body_nodes``
    descends through statements and yields the nested scope wherever it sits.

    Neither declaration takes effect unless the name is actually bound
    somewhere in the scope, so the answer here is the necessary half of the
    test and the target list is what supplies the other.
    """
    wanted = set(names)
    if not wanted:
        return False
    for node in _scope_body_nodes(scope):
        if isinstance(node, kind) and wanted.intersection(node.names):
            return True
    return False


#: Dotted paths whose ``__enter__`` provably returns ``None``.
#:
#: ``with EXPR as cs:`` binds ``EXPR.__enter__()``, so whether the header
#: still holds an *enterable* value is a question about the manager's
#: ``__enter__``, not about the syntax of the ``with``. It is decidable only
#: for a closed set of callables whose ``__enter__`` is known to return
#: ``None``:
#:
#: * ``contextlib.suppress.__enter__`` is a bare ``pass``, so the call returns
#:   ``None`` implicitly however the manager was built.
#: * ``contextlib.nullcontext.__enter__`` is ``return None`` -- it is
#:   documented to return None and that is what makes it a *null* context --
#:   but *only* when the manager was built with no ``enter_result``. Given one
#:   it returns that argument, so the spelling alone does not settle the
#:   bound value. :func:`_binds_a_null_returning_context` therefore asks about
#:   the call rather than the name.
#:
#: The ``asyncio`` spelling from :data:`SUPPRESSING_CONTEXTS` is deliberately
#: **not** inherited here, and the reason is a measured one. ``asyncio`` has no
#: ``suppress`` in CPython 3.12.14, so the name is only ever whatever the
#: program put there:
#:
#:     asyncio.suppress = CM       # `CM.__enter__` returns self
#:     with asyncio.suppress() as cs:
#:         pass                    # `cs` is a CM -- `with cs:` enters
#
#: A module attribute is rebindable, and reading the spelling as a fixed
#: ``None``-returning callable excluded a store that really does bind an
#: enterable value. ``contextlib.suppress`` has no such spelling problem: the
#: name resolves to the library object.
#:
#: Nothing else belongs here. A user-defined ``CM()`` whose ``__enter__``
#: returns ``self`` is syntactically identical to one that returns ``None``,
#: so treating *any* ``with`` as non-enterable would report the header dead
#: on an assert CPython really evaluates. Measured on CPython 3.12.14:
#
#:     def outer(x, flag):
#:         import os as cs
#:         if flag:
#:             with CM() as cs:      # flag -> cs is the CM, `with cs:` enters
#:                 pass
#:         with cs:                  # and `assert x != 1` FIRES
#:             assert x != 1
#
#: An unrecognised manager therefore *counts* as a superseding store and the
#: header is declined, which is the direction this module takes when a value
#: cannot be read: a decline reports the assert live, and calling a live
#: assert dead is the damaging direction.
NULL_RETURNING_CONTEXTS = ("contextlib.nullcontext", "contextlib.suppress")


def _with_binds_a_known_non_enterable(statement, name, bound=None):
    """Does this ``with`` bind a name to a provably non-enterable value?

    ``with EXPR as cs:`` binds ``EXPR.__enter__()``. When ``EXPR`` resolves to
    one of :data:`NULL_RETURNING_CONTEXTS` -- and binds no ``enter_result`` --
    the bound value is ``None``, cannot be entered, and so cannot supersede a
    carrier with anything enterable; the carrier stays in force. Every other
    manager may return an enterable object and is reported as a genuine
    superseder.

    Only the item that binds ``name`` is asked about. A ``with`` binds each of
    its items independently, so a non-enterable *sibling* says nothing about
    the item the queried name came from:

        with CM() as cs, contextlib.nullcontext() as other:
            pass

    ``cs`` receives ``CM().__enter__()`` -- a real object -- and ``other``'s
    ``None`` is not what the header goes on to enter. Reading the whole
    statement instead of the one item excluded a store that *can* bind an
    enterable value, which is the damaging direction: a decline reports the
    assert live, so a dead assert was certified as load-bearing.

    ``contextlib.nullcontext(CM())`` is the same argument one level in. The
    argument is the value its ``__enter__`` returns, so this call binds
    ``CM()`` and the header is entered with an object. Only the no-argument
    spelling returns ``None``, and requiring an empty argument list is what
    keeps the two apart.

    The alias map resolves every spelling: ``contextlib.nullcontext()``,
    ``from contextlib import nullcontext as nc`` and a re-exported attribute
    all collapse to the same dotted path before the comparison.

    A ``with`` whose *unbound* value is the queried name (``with cs:`` with no
    ``as`` clause) binds nothing, so no item here matches the name and the
    answer is ``False`` -- the store is not a rebind of the name at all, and
    :func:`_store_bindings` only records items that carry an ``optional_vars``.

    When *several* items bind the same name, the **last** one is what the
    header goes on to enter, because CPython processes the items in order:

        with contextlib.nullcontext() as cs, CM() as cs:
            pass

    leaves ``cs`` holding ``CM().__enter__()``, not ``None``. Reading any
    qualifying item as the answer excluded the whole statement, so a name an
    enterable item re-bound last was reported as a ``None``. Only the last
    binder is asked about, and a ``with`` that rebinds the name more than once
    is not claimed by the first item to match.

    Reading the *last* binder still does not make this rule reach the end-to-end
    verdict on such a statement. A ``with`` that binds the same name twice
    leaves ``_store_bindings`` with two entries for it, and #308 reads a name
    bound by several stores as ambiguous and reports the assert as swallowed
    before this rule is consulted at all. That is the pre-existing answer on
    ``ed9d9b0`` for this shape, it is pinned at
    ``FUNCTION_CARRIER_SUPERSESSION_LIMIT_SHAPES``, and it is not introduced
    here. What this rule controls is narrower and is what the decline asks:
    whether *the* store can be a superseding one, and it now answers from the
    binder whose value survives.
    """
    binding = None
    for item in statement.items:
        if item.optional_vars is not None and name in _store_target_names([item.optional_vars]):
            binding = item
    if binding is None:
        return False
    return _binds_a_null_returning_context(binding.context_expr, bound or {})


def _binds_a_null_returning_context(expression, bound):
    """Does ``expression`` call a known ``None``-returning manager outright?

    A call in :data:`NULL_RETURNING_CONTEXTS` binds its manager's
    ``__enter__`` result, and the manager is only *known* to return ``None``
    where that is decided by the call itself rather than by an argument:

    * ``contextlib.suppress`` has a ``__enter__`` that is a bare ``pass``, so
      it returns ``None`` however it is called. That is why its arguments are
      not inspected: it takes its exceptions as arguments, and excluding the
      call for carrying one would give up the ``suppress(AssertionError)``
      spelling.
    * ``contextlib.nullcontext`` passes its ``enter_result`` straight
      through, so the bound value is whatever the argument evaluates to.

    The second member is why a truthiness or emptiness test is not enough.
    Measured on CPython 3.12.14 across the whole ``enter_result`` space, only
    a *knowably enterable* argument leaves a value a later ``with cs:`` can
    enter:

        nullcontext()                      -> None   (not enterable)
        nullcontext(enter_result=0)       -> 0      (not enterable)
        nullcontext(enter_result=())      -> ()     (not enterable)
        nullcontext(enter_result=(CM(),)) -> tuple  (not enterable)
        nullcontext(enter_result=[CM()])  -> list   (not enterable)
        nullcontext(enter_result=CM())    -> CM     (ENTERABLE)
        nullcontext(CM())                 -> CM     (ENTERABLE)
        nullcontext(*[CM()])              -> CM     (ENTERABLE)

    The tuple and list rows are the ones a truthiness test gets wrong: the
    container holds a context manager and is still not one. So the argument
    has to be *typed*, not merely evaluated -- see
    :func:`_null_context_binds_an_enterable`.

    A call that binds its parameter twice, or names one that does not exist,
    is a ``TypeError`` at the call: the statement raises before it can bind
    anything, so there is no store to exclude. That is
    :func:`_null_context_call_raises`.
    """
    if not isinstance(expression, ast.Call):
        return False
    dotted = _resolved_dotted(expression, bound)
    if dotted not in NULL_RETURNING_CONTEXTS:
        return False
    if dotted == "contextlib.suppress":
        return True
    if _null_context_call_raises(expression):
        # `enter_result` is the *only* parameter, so a second value for it is
        # a `TypeError` at the call. The whole `with` statement raises before
        # the header is entered, so there is no store here to exclude and the
        # assert under it is unreachable. Counting the call as a superseding
        # store would decline the header and report a dead assert as
        # load-bearing, so it keeps the exclusion.
        return True
    return not _null_context_binds_an_enterable(expression, bound)


#: A ``with`` binds ``EXPR.__enter__()``, so what decides whether the name
#: still holds something enterable is the *type* of the value
#: ``enter_result`` evaluates to -- not whether it is ``None``, and not how it
#: is spelled. Measured on CPython 3.12.14 over the ``enter_result`` space:
#:
#:     nullcontext()                      -> None   (not enterable)
#:     nullcontext(enter_result=0)       -> 0      (not enterable)
#:     nullcontext(enter_result=())      -> ()     (not enterable)
#:     nullcontext(enter_result=(CM(),)) -> tuple  (not enterable)
#:     nullcontext(enter_result=[CM()])  -> list   (not enterable)
#:     nullcontext(enter_result=CM())    -> CM     (ENTERABLE)
#:     nullcontext(CM())                 -> CM     (ENTERABLE)
#:
#: A literal *container* holding a manager is still not one, so the rule reads
#: the value's type. It cannot read a runtime type off an expression, and the
#: two ways of guessing each get one of these rows wrong:
#:
#: * "any ``ast.Call`` binds an enterable" is wrong for ``list()``, ``int()``,
#:   ``dict()``, ``set()`` and every other builtin, which bind objects with no
#:   ``__enter__``. :data:`_NON_ENTERABLE_BUILTIN_CALLS` is that list.
#: * "only a bare ``ast.Call`` binds an enterable" is wrong for every other
#:   *spelling* of the same value -- a walrus, an ``IfExp``, a ``BoolOp``, a
#:   subscript, a local name, a computed ``*`` or ``**`` -- and excluding those
#:   declares a live contract dead, which is the damaging direction.
#:
#: So the classifier is an **allowlist of known-non-enterable types** and
#: everything else answers "may be enterable". See
#: :func:`_value_may_be_enterable`.
_ENTERABLE_CLASS_NAMES = (ast.Name,)

#: Builtin containers and scalars whose instances have no ``__enter__``.
#:
#: ``frozenset``, ``set``, ``dict``, ``list``, ``tuple``, ``range``,
#: ``enumerate``, ``zip``, ``map``, ``filter`` and the numeric/``bytes``
#: builtins are all *calls* that bind something a ``with`` cannot enter, so a
#: rule reading "any call is enterable" would declare every one of them live.
#: Measured on CPython 3.12.14 by entering each result. This is the call
#: arm of the allowlist; the display arm is the container literals below.
#:
#: The rule is by *name*, not by arity or by category: ``bool()``,
#: ``object()``, ``complex()`` and ``bytearray()`` are as fixed as ``list()``
#: even though none of them is a container, and leaving them out made
#: ``nullcontext(enter_result=bool())`` a live superseder on a header that
#: raises. Every entry below was measured by entering its zero-argument result
#: on CPython 3.12.14. Adding a name is a claim that ``NAME()`` has no
#: ``__enter__``, so it is checked, not assumed.
_NON_ENTERABLE_BUILTIN_CALLS = frozenset(
    {
        "bool",
        "bytearray",
        "bytes",
        "complex",
        "dict",
        "enumerate",
        "filter",
        "float",
        "frozenset",
        "int",
        "list",
        "map",
        "object",
        "range",
        "set",
        "str",
        "tuple",
        "zip",
    }
)

#: Values whose *type the language pins* to something with no ``__enter__``.
#:
#: This is an allowlist, so membership is the only thing that may decline a
#: store. Each entry is a type CPython decides, not a guess about the program:
#:
#: * a ``None`` constant is the value the no-argument form produces;
#: * a numeric / ``bytes`` / ``str`` constant is a literal of that type;
#: * a list, tuple, set or dict *display* builds a container, and a container
#:   is not a context manager however it is filled -- ``[CM()]`` holds a
#:   manager and is still a list.
#:
#: A call to a builtin in :data:`_NON_ENTERABLE_BUILTIN_CALLS` is the same
#: decision made through a call rather than a display, and is kept in
#: :func:`_value_may_be_enterable` so the builtin list and the display rule
#: read as one allowlist.
_NON_ENTERABLE_LITERAL_TYPES = (type(None), int, float, complex, str, bytes, bool)


def _null_context_binds_an_enterable(expression, bound):
    """Does this ``nullcontext`` call bind something a ``with`` could enter?

    ``nullcontext.__enter__`` is ``return self.enter_result``, so the bound
    value *is* the argument, and the question is whether its type has
    ``__enter__``. The answer comes from a known-non-enterable allowlist: only
    a value whose type the source pins may answer "no", and everything else
    answers "maybe", which is what declines the header.

    The "maybe" default is the load-bearing decision, and it is forced by the
    two errors not being symmetric. Declining reports the assert **live**, so
    reading an unreadable value as non-enterable calls a live contract dead --
    the damaging direction. :func:`_value_may_be_enterable` documents the
    choice and the rows that hold it to CPython.
    """
    for value in _null_context_enter_results(expression, bound):
        if _value_may_be_enterable(value, bound):
            return True
    return False


def _value_may_be_enterable(node, bound):
    """May ``node`` evaluate to something a ``with`` header can be entered on?

    The answer is ``False`` only for a value whose type is *pinned* to a
    non-enterable one, and ``True`` for everything else. A ``None`` constant, a
    scalar constant, a container display, and a call to a known non-enterable
    builtin are the pinned cases.

    Every other expression answers ``True``, and the reason is the direction of
    each error rather than a preference. ``True`` declines the header, so the
    assert is reported **live**; a live assert reported dead is the damaging
    error, and a dead assert reported live is recoverable. The two are
    therefore not symmetric, and the unreadable cases have to land on the
    recoverable one:

        with contextlib.nullcontext(enter_result=[CM()][0]) as cs:
            pass

    binds a ``CM`` -- the subscript is computed, so the source does not pin its
    type -- and the assert fires. The same value reached as a walrus, a
    conditional, a ``BoolOp``, a local name, or a computed ``*``/``**`` is the
    same live header, and no spelling of it may be read as non-enterable.

    The recursion is over the *values* a container display holds, never over a
    call's arguments: ``nullcontext(enter_result=(CM(),))`` binds a **tuple**,
    and the tuple is decided by the display rule. An earlier version recursed
    into a one-element tuple with the *call* classifier, which read ``.args``
    off an ``ast.Tuple`` and raised ``AttributeError`` on
    ``nullcontext(enter_result=((CM(),),))`` -- a crash where CPython simply
    binds a tuple and raises ``TypeError`` entering it.
    """
    if isinstance(node, ast.Constant):
        return not isinstance(node.value, _NON_ENTERABLE_LITERAL_TYPES)
    if isinstance(node, (ast.JoinedStr, ast.FormattedValue)):
        # An f-string is a `str` whatever it interpolates, and `ast` gives it
        # its own node type rather than folding it into `ast.Constant` -- so
        # `nullcontext(enter_result=f"{x}")` binds a string and cannot be
        # entered. Left to the fall-through it answered "may be enterable" and
        # declared a dead header live. Measured on CPython 3.12.14 by entering
        # the result.
        return False
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        # A container *display* builds a container, whatever fills it.
        return False
    if isinstance(node, ast.Dict):
        # Same for a dict display. A `**` of a dict *display* is different --
        # that one names parameters -- and is handled by the argument model.
        return False
    if isinstance(node, ast.Call):
        return not _is_a_call_to_a_non_enterable_builtin(node)
    if isinstance(node, ast.Starred):
        # A starred expression only appears where the argument model has
        # already expanded it; reaching one here means an unexpanded container
        # is being read, and its type is not pinned.
        return True
    return True


def _is_a_call_to_a_non_enterable_builtin(node):
    """Is this a call to a builtin whose result type has no ``__enter__``?

    ``nullcontext(enter_result=list())`` binds a list and
    ``nullcontext(enter_result=int())`` binds an ``int``; neither can be
    entered, so counting the call as a live superseder declared a dead assert
    load-bearing. The call is only asked about when the callee is a bare
    name, because that is the spelling that names a builtin -- an attribute
    (``mod.list()``) or a subscript is a value this module cannot read, and it
    answers as unreadable rather than assuming.
    """
    if not isinstance(node.func, _ENTERABLE_CLASS_NAMES):
        return False
    return node.func.id in _NON_ENTERABLE_BUILTIN_CALLS


def _null_context_enter_results(expression, bound):
    """The ``enter_result`` values this ``nullcontext`` call is given.

    A ``nullcontext`` signature is ``__init__(self, enter_result=None)``, so
    at most one argument can be effective, and it is the *first* positional
    value, or else the sole keyword named ``enter_result``. Every spelling is
    normalised to the value that would actually reach the parameter, in the
    order CPython applies them -- positional arguments first, then keywords:

    * the first positional node, which binds the parameter;
    * a ``*`` of a *literal* list/tuple/set, whose elements are the positional
      arguments -- so the *first element* is the one that binds it, and an
      empty star supplies nothing at all;
    * a ``*`` of anything else, which pins no value and so may supply any;
    * a ``**`` of a *literal dict*, whose ``"enter_result"`` key is the
      keyword, with the **last** duplicate winning as CPython's dict display
      does;
    * a ``**`` of a computed mapping, whose keys are unreadable -- it may
      carry ``enter_result`` and it may carry anything else, so its value is
      reported as unreadable rather than as absent.

    Returning the *effective* value rather than every syntactic node is what
    keeps the cardinality question separate from the type question:
    ``nullcontext(*[], enter_result=CM())`` unpacks to nothing, so its
    ``enter_result`` is the keyword, while ``nullcontext(*(), CM())`` supplies
    ``CM()`` positionally and the empty star contributes nothing.

    Only *one* value is returned, because only one can bind the parameter: the
    extra values of an over-supplied call are what make it raise, and that is
    :func:`_null_context_call_raises`'s question rather than this one's.
    """
    positional = _first_positional_value(expression)
    if positional is not None:
        return [positional]
    for keyword in expression.keywords:
        if keyword.arg == "enter_result":
            return [keyword.value]
    for keyword in expression.keywords:
        if keyword.arg is not None:
            continue
        literal = _dict_literal_value(keyword.value, "enter_result")
        if literal is not None:
            return [literal]
        # A `**` of a mapping the source does not pin may carry the key. A
        # `**` of a literal that simply has no such key cannot, so those are
        # skipped rather than reported.
        if _literal_dict_keys(keyword.value) is None:
            return [_UNREADABLE_VALUE]
    return []


def _first_positional_value(expression):
    """The value the first positional argument binds, or ``None`` if there is none.

    A starred argument contributes its elements, so an empty literal star
    supplies nothing and the argument after it becomes the first
    (``nullcontext(*(), CM())`` binds ``CM()``). An unreadable star pins
    nothing, so it is reported as the value and left for the type question.
    """
    for argument in expression.args:
        if not isinstance(argument, ast.Starred):
            return argument
        elements = _literal_star_elements(argument.value)
        if elements is None:
            return _UNREADABLE_VALUE
        if elements:
            return elements[0]
        # An empty literal star unpacks to nothing; the next argument is first.
    return None


#: Sentinel for "a value the source does not pin, so it may be anything".
_UNREADABLE_VALUE = ast.Name(id="__unreadable__", ctx=ast.Load())


def _literal_star_elements(node):
    """The elements a starred literal unpacks to, or ``None`` if unreadable.

    A dict display is included because it is a readable *iterable* here: it
    unpacks to its **keys**, and the checker does not need to guess what they
    are to know how many values reach the call. ``nullcontext(*{})`` unpacks
    to nothing, so it supplies no argument at all -- which is the same
    no-argument form as ``nullcontext()`` and binds ``None``. Treating it as
    unreadable made it supply a "maybe enterable" value and declared a dead
    header live. A dict display with *computed* keys is still unreadable, so
    only a fully literal one counts.
    """
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return list(node.elts)
    if isinstance(node, ast.Dict) and _literal_dict_keys(node) is not None:
        return [key for key in _literal_dict_keys(node) or ()]
    return None


def _dict_literal_value(node, key):
    """The value a literal dict stores under ``key``, or ``None`` if unreadable.

    Python keeps the **last** of several duplicate keys, so this walks to the
    end rather than returning the first match:

        nullcontext(**{"enter_result": CM(), "enter_result": None})

    binds ``None`` -- the second value wins, the header raises entering it --
    while the same dict with the values swapped binds ``CM()`` and the header
    is live. Reading the first key reversed both.
    """
    if not isinstance(node, ast.Dict):
        return None
    value = None
    for literal_key, literal_value in zip(node.keys, node.values):
        if isinstance(literal_key, ast.Constant) and literal_key.value == key:
            value = literal_value
    return value


def _null_context_call_raises(expression):
    """Would this ``nullcontext`` call raise before it binds anything?

    ``nullcontext.__init__(self, enter_result=None)`` is the whole signature, so
    the call raises at bind time when it supplies **more than one** effective
    argument, or when it names a parameter that does not exist. The ``with``
    then fails before it can bind ``cs``, so there is no store to exclude and
    the assert under it is unreachable.

    The count is over *effective* arguments, not syntax nodes, so the starred
    spellings are measured by what they unpack to:

    * ``nullcontext(*[CM(), CM()])`` supplies two and raises;
    * ``nullcontext(*[], enter_result=CM())`` supplies one -- the empty star
      unpacks to nothing -- and does not;
    * ``nullcontext(*(), CM())`` likewise supplies one.

    A keyword is unexpected when it is not ``enter_result``. An explicit
    ``foo=1`` is read directly; a ``**`` mapping is read for the keys it
    carries, with the last duplicate winning, so
    ``nullcontext(CM(), **{"foo": 1})`` is seen to raise the same way
    ``nullcontext(CM(), foo=1)`` does. A ``**`` of a mapping the source does
    not pin is treated as possibly carrying any key, so it may raise; the
    store then is not one that can be excluded, which is the recoverable
    direction.
    """
    if not isinstance(expression, ast.Call):
        return False
    positional = 0
    unknown = False
    for argument in expression.args:
        if isinstance(argument, ast.Starred):
            elements = _literal_star_elements(argument.value)
            if elements is None:
                # An unreadable star could hold any *number* of values, so
                # this cannot say the call raises -- and it must not say it
                # does not, either, because `nullcontext(*[CM(), CM()])`
                # really does raise while `nullcontext(*values)` with
                # `values = [CM()]` does not. Both spellings are therefore
                # left to the *value* question, which reports the unreadable
                # one as maybe-enterable. Claiming a raise here would
                # exclude a live store; this is the recoverable direction.
                unknown = True
                continue
            positional += len(elements)
        else:
            positional += 1
    if unknown:
        return False
    keywords = []
    for keyword in expression.keywords:
        if keyword.arg is not None:
            keywords.append(keyword.arg)
            continue
        state = _dict_literal_key_state(keyword.value)
        if state is None:
            # Not a dict display at all, so nothing about it is pinned.
            return _mapping_may_add_a_key(expression, positional)
        keys, has_computed, has_spread = state
        if has_computed and "enter_result" in keys:
            # A computed key is a *distinct* entry beside a readable
            # `enter_result`, and `nullcontext` takes one parameter, so the
            # call raises whatever the computed key evaluates to:
            #
            #     nullcontext(**{("enter_" + "r"): 1, "enter_result": CM()})
            #     -> TypeError: unexpected keyword argument 'enter_r'
            return True
        if has_spread:
            # A `**` spread may *overwrite* a key rather than add one, so it
            # can leave the mapping with exactly the keys already read:
            #
            #     nullcontext(**{"enter_result": CM(), **other})
            #
            # binds `CM()` when `other` carries only `enter_result`, and
            # raises when `other` adds a name. Which one it is depends on a
            # value the source does not pin, so this claims neither and the
            # value question takes it from there.
            continue
        keywords.extend(keys)
    if positional > 1:
        return True
    if keywords.count("enter_result") > 1:
        return True
    if positional and "enter_result" in keywords:
        return True
    return any(name != "enter_result" for name in keywords)


def _literal_dict_keys(node):
    """The keys a literal dict carries, last duplicate winning, else ``None``."""
    if not isinstance(node, ast.Dict):
        return None
    keys = []
    for literal_key in node.keys:
        if literal_key is None:
            # `**` inside a dict display (`{**other}`) is itself unreadable.
            return None
        if not isinstance(literal_key, ast.Constant):
            # A computed key -- `("enter_" + "result")` -- pins no name, so
            # the mapping it builds is not one the checker can read.
            return None
        if literal_key.value in keys:
            keys.remove(literal_key.value)
        keys.append(literal_key.value)
    return keys


def _dict_literal_key_state(node):
    """``(keys, has_computed_key, has_spread)`` for a dict display, else ``None``.

    The three answers are needed apart because they are not the same claim. A
    readable key names a parameter. A **computed** key (``("enter_" + "r")``)
    names one the source does not pin, and it is always a *separate* entry --
    it cannot overwrite its neighbours, because a dict display evaluates keys
    left to right and a computed key lands at its own position. A **spread**
    (``{**other}``) is the opposite: it can overwrite whatever came before it,
    so it cannot be counted as an extra key at all.

    Returning ``None`` for a non-display means "this is not a mapping the
    source shows", which the caller treats differently from "a mapping with
    computed keys" -- the first is a ``Name`` like ``**values`` and pins
    nothing; the second pins a readable ``enter_result`` *and* hides a key.
    """
    if not isinstance(node, ast.Dict):
        return None
    keys = []
    has_computed = False
    has_spread = False
    for literal_key in node.keys:
        if literal_key is None:
            has_spread = True
            continue
        if not isinstance(literal_key, ast.Constant):
            has_computed = True
            continue
        if literal_key.value in keys:
            keys.remove(literal_key.value)
        keys.append(literal_key.value)
    return keys, has_computed, has_spread


def _mapping_may_add_a_key(expression, positional):
    """Could this unreadable ``**`` mapping add a key to the call?

    ``nullcontext`` takes exactly one parameter, so *any* key beyond the one
    it already supplies is unexpected and raises. The mapping itself may
    carry nothing, though, so this answers only where the source has already
    pinned a key elsewhere:

    * ``nullcontext(CM(), **values)`` has a positional already, so a key in
      ``values`` would be a second value for the same parameter.
    * ``nullcontext(enter_result=CM(), **values)`` is the same in the keyword
      position.
    * ``nullcontext(**values)`` on its own pins nothing: the call may raise or
      not, so it is left to the value question.

    The *empty* mapping is the boundary and it is measured, not assumed:
    ``nullcontext(CM(), **{})`` and ``nullcontext(enter_result=CM(), **{})``
    both build a manager without raising, because an empty mapping adds
    nothing. The name is unreadable, so this cannot tell it from the populated
    case, and it does not try -- the two disagree about whether the *call*
    raises while neither can be decided from the source, so both are left to
    the value question.
    """
    if positional:
        return True
    return any(keyword.arg == "enter_result" for keyword in expression.keywords)


def _binds_starred_target(statement, name):
    """Does this store bind ``name`` through an ``ast.Starred`` target?

    ``*cs, = (a,)``, ``a, *cs = (a, b)`` and ``(*cs,) = (a, b)`` all give the
    name the *list* the unpacking collects, so the answer does not depend on
    the right-hand side at all.

    ``_store_target_names`` flattens a ``Starred`` into the name inside it, so
    by the time a name reaches here a starred binding is indistinguishable
    from a positional one. This walks the same target lists again and keeps
    only the names reached *through* a ``Starred``.
    """
    if not isinstance(statement, ast.Assign):
        return _starred_names_in_loop_target(statement, name)
    return name in _starred_target_names(statement.targets)


def _starred_names_in_loop_target(statement, name):
    """#420. Is ``name`` a starred element of a ``for`` target?

    `for *cs, in (...):` binds ``cs`` the same way `*cs, = (...)` does -- to the
    *list* the unpacking collects -- so the target syntax alone decides that the
    name is a list and `with cs:` raises `TypeError` before the body.

    #419 fixed the plain `ast.Assign` spelling. The loop target is reached
    through a different path: it is recorded with no value, so the rule falls
    to the loop-target decline, which exists because a *plain* loop target
    binds the next element and cannot be read without running the loop
    (`#336`). That decline is right for `for cs in (nullcontext(),):`, which is
    genuinely live, and over-broad for the starred sub-case, where nothing
    about the iterable matters.
    """
    if not isinstance(statement, (ast.For, ast.AsyncFor)):
        return False
    return name in _starred_target_names([statement.target])


def _starred_target_names(targets):
    """Names whose final store in these targets collects a starred list.

    CPython assigns targets and nested elements from left to right. A later
    plain target can overwrite the list: ``*cs, cs = (1, nullcontext())``
    leaves ``cs`` a context manager. Track every name store in that order,
    including the separate targets of a chained assignment.
    """
    final_stores = {}
    pending = list(reversed(targets))
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Name):
            final_stores[target.id] = False
        elif isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(reversed(target.elts))
        elif isinstance(target, ast.Starred):
            if isinstance(target.value, ast.Name):
                final_stores[target.value.id] = True
            else:
                # Unpacking the collected list again binds its elements,
                # rather than assigning the list itself to every nested name.
                pending.append(target.value)
    return {name for name, starred in final_stores.items() if starred}
