# Shared-namespace fragment for literal entry and header reachability.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part13":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _literal_entry_is_dead(value):
    """Is this *directly entered* literal unenterable? (``#390``)

    A ``with`` header can bind its value without going through a store at all:

        with (cs := lambda: None):

    Here the entered object is the lambda itself, not whatever ``cs`` held
    before. ``_literal_runtime_type`` reads its runtime type from the syntax,
    and a ``lambda`` is a function, which has no ``__enter__``.

    Scoped to the literal shapes whose type the syntax already fixes -- the
    same set the store rules read -- so a header the rule cannot read keeps
    the assert live rather than dropping a real contract. This is the mild
    direction, the same trade :func:`_entry_is_dead` makes for its own shapes.
    """
    if not isinstance(value, (ast.Lambda, ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Constant)):
        return False
    kind = _literal_runtime_type(value)
    if kind is None:
        # Not a literal this rule can read: decline, and keep the assert live.
        return False
    return not _runtime_kind_can_enter(kind)


def _runtime_kind_can_enter(kind):
    """Does a value of this runtime kind implement the context manager protocol?

    Asked of the real interpreter rather than of a list of type names. A
    hand-maintained set of "these are fine" would have to be right about every
    stdlib type that can appear here, and a miss either way is a wrong verdict
    about a real contract. A kind the interpreter cannot produce is refused,
    which keeps the answer on the decline side.
    """
    probe = _PROBE_FOR_RUNTIME_KIND.get(kind)
    if probe is None:
        return False
    return hasattr(probe, "__enter__")


#: One real value per runtime kind :func:`_literal_runtime_type` can report, so
#: the enterability question is answered by the interpreter rather than by a
#: maintained list of type names. `function` is the #390 case: a lambda object
#: has no `__enter__`.
_PROBE_FOR_RUNTIME_KIND = {
    "function": lambda: None,
    "module": types.ModuleType("probe"),
    "list": [],
    "tuple": (),
    "set": set(),
    "dict": {},
    "int": 0,
    "str": "",
    "float": 0.0,
    "bytes": b"",
    "NoneType": None,
}


def _header_expression_raises(header, function, module):
    """Does evaluating this ``with`` header raise before the body is entered?

    #394. ``import contextlib as fake`` at module scope and
    ``import json as fake`` inside the function body: the inner import
    shadows the outer, so ``with fake.suppress(AssertionError):`` evaluates
    ``json.suppress`` -- an attribute that module does not have -- and raises
    ``AttributeError`` while the *header* is being evaluated. The body is
    never entered, the assert under it is never evaluated, and the analyzer
    was certifying it as load-bearing. The damaging direction.

    The same question is asked of the statements that *feed* the header, not
    only of the header itself, because entering a bare name defers the whole
    question to whoever bound it:

        import contextlib.nullcontext as contextlib
        with contextlib.nullcontext():
            assert x != 1

    That import is not a module path at all, so the statement raises
    ``ModuleNotFoundError`` and the ``with`` is never evaluated. The old
    resolver skipped the dotted spelling entirely and fell through to the
    bare ``import contextlib`` further down, resolving the name to the real
    module and certifying the assert load-bearing on a fixture that cannot
    run.

    The check is deliberately narrow and declines in every case it cannot
    read:

    * the callee must be a plain ``Attribute`` on a ``Name`` -- no nested
      attribute, no subscript, no computed callee;
    * the base name must be bound to a module by an ``import`` the analyzer
      can see, in the function body or at module scope; and
    * that module must genuinely lack the attribute.

    An attribute the module *does* export is left alone, because then the
    header evaluates normally and the assert may well be live --
    ``with contextlib.suppress(AssertionError):`` is exactly that shape, and
    answering it dead would drop a real pinned contract. The base is
    resolved from the source's own ``import`` statements rather than from the
    live interpreter's globals, so a name this module never bound is declined
    even if something else in the process happens to bind it.

    A base bound to something that is **not** a module -- a submodule
    attribute, a ``from`` leaf -- is the same question answered the other way,
    and is what makes the two shadow rows above fail. ``_imported_module_name``
    returns ``None`` for those, which by itself means only "not a module I can
    read" and is not enough to answer either way. The distinction that matters
    here is whether an *import this scope can see* bound the name at all: if
    one did, and it did not bind a real module, then the attribute lookup on
    it is what raises, and the assert underneath is unreachable.

    Only the header's own expression and the imports that run before it are
    read. Walking further up the scope -- every earlier attribute call -- was
    tried and reverted: it read 64 pinned asserts that really do fire as dead,
    because a carrier the analyzer has not resolved yet looks exactly like a
    raising one. The header's own expression is the narrow question #394
    settled, and it is the only one with an answer the rest of the file
    already agrees with.
    """
    if isinstance(header, ast.AsyncWith):
        return False
    for item in header.items:
        callee = item.context_expr.func if isinstance(item.context_expr, ast.Call) else None
        if callee is None or not isinstance(callee, ast.Attribute):
            continue
        base = callee.value
        if not isinstance(base, ast.Name):
            continue
        resolved = _imported_module_name(base.id, function, module)
        if resolved is None:
            continue
        # The lookup is against the module the *source* binds the name to, not
        # against the spelling. `import json as fake` binds `json` under the
        # name `fake`, so asking `sys.modules["fake"]` would miss the shadowing
        # entirely -- which is what this rule exists to catch.
        if _module_lacks_attribute(resolved, callee.attr) is not None:
            return True
    return False


def _scope_above_header_raises(function, header):
    """Does an import in this scope raise before the header is evaluated?

    #451. A header that enters a **bare name** hands the whole question to
    whoever bound that name, so the header's own expression says nothing:

        import contextlib.nullcontext as contextlib
        cs = contextlib.nullcontext()
        with cs:                       # a bare `Name` -- nothing to read here
            assert x != 1

    The ``import`` is not a module path at all. It raises
    ``ModuleNotFoundError`` (``contextlib`` is a module, not a package), so
    nothing after it runs, the ``with`` is never entered and the assert is
    never evaluated. The analyzer reported it *enforced* -- the damaging
    direction, on a fixture that cannot even run.

    Two spellings are decidable here, and both are decided from the import
    statement alone, which is what keeps this narrow:

    * a **dotted** ``import a.b`` whose target is not importable raises at the
      statement (``import contextlib.nullcontext as contextlib``);
    * a **``from`` leaf** bound over a name the walk resolves a module root
      through (``from os import sep as contextlib``) leaves a non-module
      there, and the next attribute read on it raises ``AttributeError``.

    A bare ``import contextlib`` is the ordinary filed case and is admitted.
    So is every name no import in this scope rebinds, and a name the witness
    never reads: neither can change what the header enters.

    Deliberately *not* asked here: whether an arbitrary earlier statement
    raises. That was tried and reverted -- an unresolved carrier call
    (``cs = contextlib.nullcontext()`` with a carrier the analyzer has not
    resolved yet) is indistinguishable from a raising one, and reading it as a
    raise reported 64 pinned asserts that really do fire as dead. The import
    statement is the one thing above the header whose failure is decidable
    from the source alone.
    """
    body = getattr(function, "body", None)
    if not body or header not in body:
        return False
    # `importlib` is not in this module's shared namespace -- the fragments
    # share the sentinel support namespace, which imports `ast`, `copy`,
    # `inspect`, `sys` and `types` and nothing else. `part1` imports it
    # locally for the same reason.
    import importlib.util

    for statement in body[: body.index(header)]:
        if not isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        for alias in statement.names:
            bound_name = alias.asname or alias.name
            if bound_name not in _module_roots_read_below(function, header):
                continue
            if isinstance(statement, ast.Import):
                if "." not in alias.name or alias.name in sys.modules:
                    continue
                try:
                    if importlib.util.find_spec(alias.name) is None:
                        return True
                except (ImportError, AttributeError, ValueError):
                    # `ModuleNotFoundError` and "'X' is not a package" both
                    # land here, and both mean the statement raises.
                    return True
                continue
            if statement.level or not statement.module:
                # A relative import resolves against a package this analyzer
                # knows nothing about, so it cannot be shown to be safe.
                return True
            if _from_leaf_is_a_known_value(statement.module, alias.name):
                # The name resolves to something that exists but is not a
                # module. Whether that matters depends entirely on *how* the
                # name is used below, and the two uses are different
                # questions.
                #
                # Entered directly -- `from contextlib import nullcontext as
                # builtins; builtins.int = contextlib.nullcontext; cs =
                # builtins.int(); with cs:` -- the scope decides by assignment
                # whether the value is enterable, and it may well be:
                #
                #     builtins.int()               # a real context manager
                #     with cs:
                #         assert x != 1           # really fires
                #
                # #457 settled that the enterability test is on the metatype,
                # and this scope answers it by assignment. A leaf that is a
                # plain value cannot be shown to be a shadow *and* unenterable
                # at the same time, so the question is declined here rather
                # than guessed -- the direction that keeps a real pinned
                # contract.
                #
                # Read as a *module root* -- `from os import sep as
                # contextlib; cs = contextlib.nullcontext()` -- there is no
                # assignment in between to make it one. A `str` has no
                # `nullcontext`, so the very next attribute read raises and
                # the assert is unreachable. That is decided by the use, not
                # by the value, which is what distinguishes it from the case
                # above.
                if bound_name in _roots_read_as_attribute_paths(
                    function, header
                ) and not _root_is_built_up_in_scope(function, bound_name):
                    return True
                continue
            if _from_leaf_is_a_module(statement.module, alias.name):
                # `from os import path as name` binds a real module, so an
                # attribute read on it is ordinary Python and the raise, if
                # any, is the header's own question -- #394's rule above.
                continue
            # Neither a module nor a value this interpreter can name: the
            # attribute does not exist, so the next read on the bound name
            # raises `AttributeError`.
            return True
    return False


def _from_leaf_is_a_known_value(module_name, attribute):
    """Does ``from <module_name> import <attribute>`` bind an existing value?

    Checked as an *attribute* of the module rather than as a dotted
    ``sys.modules`` key. ``from contextlib import nullcontext`` is
    ``contextlib.nullcontext``, and that leaf is an attribute of the loaded
    ``contextlib`` -- it is not itself an entry in ``sys.modules``, so asking
    for the dotted key reports a perfectly ordinary import as unresolvable and
    produced a false "the scope raises" on a live assert.
    """
    module = sys.modules.get(module_name)
    if module is None or not isinstance(module, types.ModuleType):
        return False
    try:
        getattr(module, attribute)
    except AttributeError:
        return False
    return True


def _from_leaf_is_a_module(module_name, attribute):
    """Does ``from <module_name> import <attribute>`` bind a real module?"""
    module = sys.modules.get(module_name)
    if module is None or not isinstance(module, types.ModuleType):
        return False
    try:
        value = getattr(module, attribute)
    except AttributeError:
        return False
    return isinstance(value, types.ModuleType)


def _module_roots_read_below(function, header):
    """The module roots the code under ``header`` resolves attributes through.

    An import that binds a name nothing here reads cannot change what the
    header enters, so it is not this rule's business -- that is what keeps
    ``import json as _j`` beside the canonical import admitted.
    """
    return _witness_module_roots(function, _bound_names(_owning_module(function), function))


def _roots_read_as_attribute_paths(function, header):
    """Roots below ``header`` that are dereferenced as ``name.something(...)``.

    A root that is only ever passed around as a value (`cs = builtins.int()`)
    is a different question from one the source *walks into* with an attribute
    access. Only the second can be settled from the import alone, because it
    assumes the name is a module -- which is exactly what a `from` leaf
    contradicts.

    The whole scope is read, not just the statements after the header: the
    attribute read that raises is usually the *carrier* -- `cs =
    contextlib.nullcontext()` sits above the `with cs:` that defers to it.

    An attribute on the **callee** of a call is the ordinary spelling of "read
     something off that root and call it" (`contextlib.nullcontext()`), but an
     attribute on the *value* a call returns is a different question entirely
     and is not this one. So a chain rooted at a call is skipped: in
     `builtins.int()` the root `builtins` is read to produce a manager, which
     is a legitimate use of a non-module and must not be read as a raise.
    """
    roots = set()
    for statement in getattr(function, "body", ()) or ():
        for node in ast.walk(statement):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.ctx, ast.Load):
                # `builtins.int = ...` *stores* an attribute; it does not read
                # one, and it is exactly how the scope makes a non-module root
                # usable. Only a read assumes the name is a module.
                continue
            base = node.value
            if isinstance(base, ast.Call):
                # The callee itself is an `ast.Attribute` node and *is*
                # counted -- what is skipped is any further attribute hanging
                # off the call's result.
                continue
            if isinstance(base, ast.Name):
                roots.add(base.id)
    return roots


def _root_is_built_up_in_scope(function, name):
    """Does this scope store an attribute onto ``name`` or rebind it at all?

    ``from contextlib import nullcontext as builtins`` followed by
    ``builtins.int = contextlib.nullcontext`` is a *working* name, not a
    broken one: the scope manufactures the very attribute it then reads. That
    is what separates it from ``from os import sep as contextlib``, where the
    next read of `nullcontext` has nothing behind it.

    So an import that binds a plain value is only a shadow when the scope does
    nothing to it. Any write -- an attribute store, a rebinding, a `del` --
    hands the question back to the rest of the analyzer, which already models
    the entered value; the safe direction is to decline here rather than
    report a live pinned assert dead.
    """
    for statement in getattr(function, "body", ()) or ():
        for node in ast.walk(statement):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                base = node.value
                while isinstance(base, ast.Attribute):
                    base = base.value
                if isinstance(base, ast.Name) and base.id == name:
                    return True
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and node.id == name
            ):
                return True
    return False


def _imported_module_name(name, function, module):
    """The module ``name`` is bound to by an ``import``, or ``None``.

    A function-body import shadows a module-level one of the same spelling,
    so the *last* binding in the innermost scope that mentions the name is
    the one a header in that scope reads. Only the ``asname`` spelling is
    followed for a different name; a bare ``import X`` binds ``X`` itself.

    Every spelling that *binds* ``name`` is read, not just the bare one.
    Matching only ``asname is None and alias.name == name`` left two shadowing
    fixtures resolving to the real ``contextlib`` and being reported live while
    the interpreter raised long before the header:

        from os import sep as contextlib            # AttributeError
        import contextlib.nullcontext as contextlib # ModuleNotFoundError

    In both, ``alias.asname == name`` but ``alias.name`` is something else, so
    the old ``alias.asname is None and alias.name == name`` test simply did not
    match and the resolver walked on to the bare ``import contextlib`` below.
    The rule that consults this also declines when the dotted target is not a
    real module, which is correct -- ``contextlib.nullcontext`` is not one --
    but declining must mean "this scope raises", not "this scope is the real
    ``contextlib``".
    """
    # The *statement* lists are used, not `_scope_body_nodes`: that helper
    # descends into a statement to find nested bindings, so the module-level
    # `import contextlib as fake` would be read from inside the `with` body
    # and the function-body `import json as fake` that actually shadows it
    # would never be reached. Source order inside the real body is what
    # decides the binding.
    # Innermost scope first: a function-body import shadows a module-level one
    # of the same spelling for the whole body, so the module scope is only
    # consulted when the function itself never binds the name.
    for scope in (getattr(function, "body", None), getattr(module, "body", None)):
        if not scope:
            continue
        bound = None
        for statement in scope:
            if not isinstance(statement, (ast.Import, ast.ImportFrom)):
                continue
            for alias in statement.names:
                bound_name = alias.asname or alias.name
                if bound_name != name:
                    continue
                if isinstance(statement, ast.Import):
                    bound = alias.name
                elif statement.module and statement.level == 0:
                    bound = f"{statement.module}.{alias.name}"
                else:
                    # A `from M import a.b` is not valid syntax and a relative
                    # import resolves against a package this analyzer knows
                    # nothing about, so neither yields a module we can name.
                    bound = None
        if bound is not None:
            return bound if isinstance(sys.modules.get(bound), types.ModuleType) else None
    return None


def _binds_element_of(statement, name):
    """Does this store bind ``name`` to an *element* of the right-hand side?

    ``cs, other = (a, b)`` and ``[cs] = [a]`` give the name one element of the
    right-hand side; only a bare ``cs = ...`` target gives it the whole value.
    """
    if not isinstance(statement, ast.Assign):
        return False
    return any(
        isinstance(target, (ast.Tuple, ast.List)) and name in _store_target_names([target])
        for target in statement.targets
    )


def _target_is_bare_name(statement, name):
    """Does ``name`` receive this assignment's whole right-hand side?

    The complement of :func:`_binds_element_of`, asked per target rather than
    per statement. A chained assignment can mix the two spellings in one
    statement:

        cs = (other, x) = (helper, 1)

    Here ``cs`` is a bare target and receives the whole tuple, while ``other``
    and ``x`` receive elements of it. Deciding the statement once -- "this is a
    destructuring assignment" -- is wrong for ``cs`` in both halves of that
    example, and reading the right-hand side's type for ``cs`` is right.

    Only ``ast.Assign`` carries several targets, so any other store form binds
    its name the ordinary way and answers ``True``.
    """
    if not isinstance(statement, ast.Assign):
        return True
    return any(isinstance(target, ast.Name) and target.id == name for target in statement.targets)


def _binds_a_starred_name(statement, name):
    """Is ``name`` a starred target, so unpacking pins it to a list?

    ``*cs, = (helper,)`` and ``first, *cs = pair`` both build a list for ``cs``
    whatever the elements are, so the name cannot hold a context manager
    afterwards. :func:`_literal_runtime_type` reads the ``ast.Starred`` that
    records this as ``"list"`` for the ordinary entry rule; this is the same
    question asked about the target rather than the value, because
    :func:`_store_may_bind_enterable` has to know it *before* it decides
    whether the element type is readable at all.
    """
    if not isinstance(statement, ast.Assign):
        return False

    def starred(node):
        if isinstance(node, ast.Starred):
            return name in _store_target_names([node.value])
        if isinstance(node, (ast.Tuple, ast.List)):
            return any(starred(element) for element in node.elts)
        return False

    return any(starred(target) for target in statement.targets)


def _element_kind_readable_from(statement, value, name):
    """The type ``name`` receives from a destructuring of a literal container.

    The complement of the "element is unreadable" rule in
    :func:`_store_may_bind_enterable`. When the right-hand side is a literal
    container *of literals*, the element this target receives is readable, and
    declining it reports a dead header as live:

        if flag:
            cs, other = (None, 1)

    ``cs`` receives ``None``. ``with cs:`` raises ``TypeError`` on both paths,
    so the assert is dead, but the unreadable-element rule declined and
    reported it live.

    The target is walked in lockstep with the container's elements, because
    that is what Python's unpacking does. A star target breaks the lockstep
    and is answered by :func:`_binds_a_starred_name` before this is reached.

    Returns the element's type name, or ``None`` when the element is not
    statically readable -- an unreadable element falls back to the caller's
    "possibly enterable" rule, which is the safe direction.
    """
    if not isinstance(statement, ast.Assign) or not isinstance(value, (ast.Tuple, ast.List)):
        return None
    elements = list(value.elts)
    for target in statement.targets:
        kind = _pattern_kind(target, elements)
        if kind is not None and _pattern_binds_name(target, name):
            return kind
    return None


def _pattern_binds_name(target, name):
    """Does this assignment target list bind ``name``, however deeply nested?"""
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_pattern_binds_name(element, name) for element in target.elts)
    if isinstance(target, ast.Starred):
        return _pattern_binds_name(target.value, name)
    return False


def _pattern_kind(target, elements):
    """The type ``target`` receives from ``elements``, or ``None`` if unreadable.

    Walks the assignment pattern in lockstep with the right-hand side's
    elements, because that is what Python's unpacking does. A star consumes a
    variable-length run and breaks the lockstep, so it answers ``None`` and is
    left to :func:`_binds_a_starred_name`. A pattern that runs out of elements
    raises at runtime, so the verdict is not readable either.
    """
    if isinstance(target, ast.Starred):
        return None
    if isinstance(target, (ast.Tuple, ast.List)):
        if any(_pattern_kind(element, elements) is None for element in target.elts):
            return None
        consumed = len(target.elts)
        if consumed > len(elements):
            return None
        return "tuple" if isinstance(target, ast.Tuple) else "list"
    if isinstance(target, ast.Name):
        return _literal_runtime_type(elements[0]) if elements else None
    return None


def _statement_never_runs(statement, function):
    """Is this store written inside a branch that provably never executes?

    ``if False: cs = nullcontext()`` never runs its body, so a carrier written
    earlier is still the settled value and the header still raises. Reading the
    store as a possible supersession declines the header and reports a dead
    assert as live, which is the damaging direction.

    The test is the loop spelling and the conjunction spelling of the
    ``if False:`` defeat the module already closes elsewhere:
    :func:`_falsy_literal` for a literal-false condition, and
    :func:`_is_empty_literal_iterable` for a loop over a container that yields
    nothing. A conjunction counts as false when *any* operand is a literal
    false, because ``flag and False`` is false for every value of ``flag``.

    Only shapes whose non-execution is decidable from the syntax are matched.
    A condition this cannot read is treated as possibly-true, which is the
    conservative direction.
    """
    # The recorded statement is the store itself -- the `cs = ...` assignment
    # inside the branch -- not the branch that encloses it, so the enclosing
    # blocks are found by walking *out* to the function and asking which of its
    # bodies contain this statement.
    for enclosing in _enclosing_blocks(statement, function):
        if isinstance(enclosing, ast.If) and _condition_is_never_true(enclosing.test, function):
            return True
        if isinstance(enclosing, ast.While) and _condition_is_never_true(enclosing.test, function):
            # `while False:` is the loop spelling of `if False:` -- the body
            # never runs, so it is never the last element either.
            return True
        if isinstance(enclosing, (ast.For, ast.AsyncFor)) and _loop_iterable_is_provably_empty(
            enclosing.iter, function
        ):
            return True
    for node in ast.walk(statement):
        if isinstance(node, ast.If) and _condition_is_never_true(node.test, function):
            return True
        if isinstance(node, (ast.For, ast.AsyncFor)) and _loop_iterable_is_provably_empty(
            node.iter, function
        ):
            return True
    return False


def _store_retires(statement, name, function=None):
    """Does this store settle ``name``, or may it simply not have run?

    Every store form the walk collects binds its name whenever control reaches
    it, and :func:`_resolve_bindings` already separates the ones that can be
    skipped (``conditional``). A ``match`` capture is the one form that is
    *always* recorded as unconditional yet can still fail to run, because its
    clause may not be selected. Left unfiltered it retires a carried alias on
    a path where the capture never happened, so the stale suppressor reaches
    the next ``with`` -- and the assert under it is really swallowed, while
    the analyzer calls it enforced.

    ``function`` is the scope the statement was written in. It is only needed
    for the #369 subject resolution, which has to look at a preceding
    assignment in the same block, and is optional so the callers that only
    have a statement keep working unchanged.

    There is a second, independent condition and it is not about the clause at
    all: the ``match`` has to be *reached*. A ``match`` written in the body of
    ``for _ in []:`` never runs, so however certainly its clause would bind,
    the binding does not happen. That is #378's question rather than #342's,
    and it is easy to leave out precisely because a refutable clause already
    answers "may not have run" for the ordinary shapes -- the gap only opens
    once some rule starts answering "always binds" for a refutable clause.
    Answered here rather than at one call site so the two callers cannot
    disagree, which is what would let a settled capture still be counted as a
    competing one and the name read as ambiguous.
    """
    if not isinstance(statement, ast.Match):
        return True
    if function is not None and statement not in function.body:
        # Nested in a block, so it runs on some paths only.
        return False
    if _capture_always_binds(statement, name):
        return True
    return bool(_capture_is_decidable_from_a_literal_subject(function, statement, name))


def _is_provably_unreached_store(entry, orders, function):
    """Is this conditional store inside a loop body that can never run?

    #378. A ``for`` over a literal empty iterable has no iterations, so every
    statement in its body is unreachable. A store written there is recorded,
    flagged ``conditional``, and would otherwise compete with the store that
    really is in force -- retiring a live ``nullcontext`` and letting a
    swallowed assert be reported as swallowed when it is in fact live.

    The test is deliberately narrow: only a *provably* empty iterable answers
    yes. ``helper.items()`` may yield nothing, but it may not, so a store in
    that body keeps competing and the ordinary ambiguity handling applies.
    #450 widened the question from the container literals to
    :func:`_loop_iterable_is_provably_empty`, which also settles the empty
    builtin calls and the empty ``range`` shapes. Each of those is decided
    from its own arguments, and each declines when an argument is unreadable
    -- ``set(items)`` and ``range(n)`` keep competing exactly as
    ``helper.items()`` does.

    A ``while`` loop is asked the same question through its test, and only a
    test that is false for every binding answers yes: ``while False:`` is the
    loop spelling of ``if False:``. A ``while True:`` loop, or one whose test
    is merely truthy or unreadable, is not provably empty and so is not
    matched.

    The ``For`` branch is gated on :func:`_in_body`, and that gate is the
    whole difference between a correct answer and a regression. ``For.body``
    and ``For.orelse`` are AST **siblings**: both are attributes of the same
    ``ast.For`` node, and both therefore appear in the ancestor walk, but only
    one of them is skipped when the loop has no iterations. The ``else``
    clause is exactly what runs *because* the loop finished without
    ``break``, so every statement in it executes on every path through a
    zero-iteration loop:

        for y in ():
            pass
        else:
            cs = contextlib.suppress(AssertionError)   # this runs

    Executed on CPython 3.12.14 the assert under the following ``with cs:`` is
    swallowed in all of these -- a plain assign, the same over a list and a
    dict literal, an assign nested in an ``if``, and one nested in a ``with``.
    Unguarded, this helper answered "unreached" for every one of them and the
    assert was reported **live**, which is the damaging direction #378 exists
    to prevent and a regression against ``master`` (``2c81f10``), where all
    five are already correct. The clause is reused rather than a new
    containment test written, because it already answers the same question
    for a ``with`` header: "inside the executed body, not a handler or an
    ``else``".

    This is the same question `_is_store_statement` answers for a header
    *nested inside* the loop; both are needed because the two answer through
    different tables. On ``master`` (``2c81f10``) the nested case was already
    right and the after-loop case still certified three live asserts dead, so
    neither guard covers the other.

    The port originally also filtered ``_resolve_bindings``' competing set and
    added an empty-competing fallback there. Mutation showed both **survived**
    -- reverting either alone leaves the lane green -- because every caller
    either pre-filters the same table (the `_assigned_suppressors` loop above)
    or declines before reaching the ambiguity branch (the ``nonlocal``
    resolver). They were removed rather than left in as untested code.
    """
    statement = entry[0]
    if function is None:
        return False
    # `_ancestors` runs outermost-first and ends with ``target`` itself, so
    # the last element is dropped: the question is which *containers* the
    # store sits inside.
    chain = list(_ancestors(function, statement))[:-1]
    for ancestor in chain:
        # `ast.AsyncFor` is included for symmetry with `_is_store_statement`,
        # but it is deliberately untested here: an `async for` over a *literal*
        # container cannot execute, because a literal is not an async
        # iterable, so no fixture can put the analyzer and CPython in the same
        # room for that shape. Reverting this clause to `ast.For` alone
        # therefore survives mutation, and that is recorded rather than hidden
        # behind a row that cannot run. The clause is kept because the
        # narrowing that survives mutation would be the more surprising change.
        # An outer empty loop's else can contain an inner empty loop body.
        # Keep scanning unless this particular body contains the store.
        if (
            isinstance(ancestor, (ast.For, ast.AsyncFor))
            and _loop_iterable_is_provably_empty(ancestor.iter, function)
            and _in_body(ancestor, statement)
        ):
            return True
        if (
            isinstance(ancestor, ast.While)
            and _condition_is_never_true(ancestor.test, function)
            and _in_body(ancestor, statement)
        ):
            return True
        # #358. A ``match`` clause the literal subject cannot select does not
        # run, so a store in that clause's body is a store that never executes
        # -- the same question this helper answers for an empty loop body and a
        # falsy ``if`` body. The filed row is a sequence pattern against a
        # string subject, which ``match`` deliberately does not select, so the
        # carried suppressor stays in force and the assert under it is
        # swallowed while the analyzer reported it ``enforced``.
        #
        # The gate is the same :func:`_in_body` the clauses above use, and for
        # a ``match_case`` it is exactly "the store is in this clause's body".
        # A later case in the same ``match`` is an AST *sibling*, not a
        # descendant, so a store in a selected sibling clause is not caught by
        # an unselected one: the walk only reaches the case that actually
        # encloses the store.
        if (
            isinstance(ancestor, ast.match_case)
            and _match_case_is_provably_unselected(
                function, _enclosing_match(chain, ancestor), ancestor
            )
            and _in_body(ancestor, statement)
        ):
            return True
    return False


def _enclosing_match(chain, case):
    """The ``ast.Match`` that owns ``case`` within an ancestor ``chain``."""
    index = chain.index(case)
    for candidate in reversed(chain[:index]):
        if isinstance(candidate, ast.Match):
            return candidate
    return None


def _carried_suppressor_has_unshadowed_arguments(value, function):
    """Require builtin exception arguments throughout the enclosing scopes."""
    owning = _module_for_function(function)
    scope = function
    while scope is not None:
        if not _nonlocal_has_known_exception_arguments(value, _raw_store_values(scope), scope):
            return False
        scope = _nonlocal_parent_function(scope, owning)
    if owning is None:
        return False
    for argument in value.args:
        for statement in owning.body:
            nodes = [statement, *_module_level_bindings(statement)]
            if any(any(_names_bound_by_statement(node, argument.id)) for node in nodes):
                return False
    return True


def _literal_value(node):
    """The value of a literal-only expression, or ``_NOT_LITERAL``.

    ``ast.literal_eval`` is the obvious tool here and is the wrong one: it
    refuses every ``Compare`` node, so it cannot answer the question this
    exists to answer. ``1 == 1`` raises ``ValueError`` there, yet it is exactly
    the decisive case -- decidable to true without reading any state, so it
    short-circuits the ``or`` precisely as a bare ``True`` does.

    The fold is therefore done structurally, over the literal expression
    grammar: constants, containers of constants, unary and binary operators,
    and chained comparisons between them. Anything that could read runtime
    state -- ``Name``, ``Call``, ``Attribute``, ``Subscript`` -- makes the walk
    bail and return ``_NOT_LITERAL``. That boundary is what keeps ``x == x``
    enforced: it reads a ``Name``, so it is not decidable, and reporting a live
    assert as dead is the worse error.

    A ``None`` fallback would be wrong here, because ``None`` is itself a
    literal (it parses to a ``Constant``) and so would be indistinguishable
    from a genuine ``None``.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items = [_literal_value(element) for element in node.elts]
        if any(item is _NOT_LITERAL for item in items):
            return _NOT_LITERAL
        try:
            if isinstance(node, ast.Tuple):
                return tuple(items)
            if isinstance(node, ast.List):
                return items
            return set(items)
        except TypeError:
            return _NOT_LITERAL
    if isinstance(node, ast.Dict):
        keys = [_literal_value(key) for key in node.keys]
        values = [_literal_value(value) for value in node.values]
        if any(item is _NOT_LITERAL for item in keys + values):
            return _NOT_LITERAL
        try:
            return dict(zip(keys, values))
        except TypeError:
            return _NOT_LITERAL
    if isinstance(node, ast.UnaryOp):
        operand = _literal_value(node.operand)
        if operand is _NOT_LITERAL:
            return _NOT_LITERAL
        try:
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return +operand
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.Invert):
                return ~operand
        except TypeError:
            return _NOT_LITERAL
        return _NOT_LITERAL
    if isinstance(node, ast.BinOp) and type(node.op) in _LITERAL_OPERATORS:
        left = _literal_value(node.left)
        right = _literal_value(node.right)
        if left is _NOT_LITERAL or right is _NOT_LITERAL:
            return _NOT_LITERAL
        try:
            return _LITERAL_OPERATORS[type(node.op)](left, right)
        except (ArithmeticError, TypeError):
            return _NOT_LITERAL
    if isinstance(node, ast.BoolOp):
        result = isinstance(node.op, ast.And)
        for value in node.values:
            item = _literal_value(value)
            if item is _NOT_LITERAL:
                return _NOT_LITERAL
            if isinstance(node.op, ast.And):
                result = result and bool(item)
            else:
                result = result or bool(item)
        return result
    if isinstance(node, ast.Compare):
        left = _literal_value(node.left)
        if left is _NOT_LITERAL:
            return _NOT_LITERAL
        for operator, comparator in zip(node.ops, node.comparators):
            right = _literal_value(comparator)
            if right is _NOT_LITERAL:
                return _NOT_LITERAL
            handler = _LITERAL_COMPARISONS.get(type(operator))
            if handler is None:
                return _NOT_LITERAL
            try:
                matched = handler(left, right)
            except TypeError:
                return _NOT_LITERAL
            if not matched:
                return False
            left = right
        return True
    return _NOT_LITERAL


def _as_number(value):
    """This value if it is a real number, else ``None``.

    ``bool`` is excluded even though it subclasses ``int``: ``len(y) >= True``
    is a real comparison, not a tautology about a non-negative count.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _numeric_literal(node):
    """The value of a numeric literal node, or ``None``.

    ``-1`` parses as a ``UnaryOp(USub, Constant)`` rather than a negative
    ``Constant``, so a negative bound has to be folded here or every
    lower-bounded form would be missed.
    """
    if isinstance(node, ast.Constant):
        return _as_number(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.USub, ast.UAdd))
        and isinstance(node.operand, ast.Constant)
    ):
        value = _as_number(node.operand.value)
        if value is None:
            return None
        return -value if isinstance(node.op, ast.USub) else value
    return None


def _is_count_like(node):
    """Is this expression a count or length, so it cannot be negative?

    ``len(...)``, ``count(...)`` and ``bit_count(...)`` are the forms that
    appear in these tests. Anything else -- a bare name, an arithmetic
    expression, a record subscript -- is not assumed non-negative, because
    assuming it would turn a real comparison into a false tautology.
    """
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in {"len", "count", "bit_count"}
    if isinstance(node.func, ast.Attribute):
        return node.func.attr in {"len", "count", "bit_count"}
    return False


def _is_tautology(node):
    """Is this expression true regardless of the values it reads?

    Only the decidable forms are recognised: a truthy constant, a comparison
    built purely from literals, and a count compared against a bound a length
    can never cross. A comparison that reads any runtime value
    (``len(calls) == 300``, ``record["termination"] != "cancelled_or_deadline"``,
    ``x == x``) is a real check and is never treated as a tautology.

    A tautology is what makes an ``or`` bypass its sibling: the comparison the
    contract depends on is never evaluated because the other operand is true
    whatever the record says. Verified to survive on ``aef56d6``:

        assert record["termination"] != "cancelled_or_deadline" or \
            len(record.get("errors", [])) >= 0
        -> guard RETURNS on the deadline record; suite exits 0

    The left operand must look count-like before a numeric bound is believed:
    ``x > -1`` is only a tautology when ``x`` cannot be negative, and assuming
    that would report a live assert as dead -- the rule would then cry wolf on
    a real regression.

    The literal-only fold covers the spellings the count heuristic cannot see.
    ``1 == 1`` is true without reading any state, so it short-circuits the ``or``
    exactly as a bare ``True`` does -- but its left operand is a constant, not a
    call, so ``_is_count_like`` rejects it and the operand was previously
    misreported as enforced. Restricting the fold to literals keeps the
    conservative direction: ``x == x`` reads a ``Name``, is not constant, and
    therefore stays enforced.

    Deliberately one-directional. Proving an expression is always *false* is
    not attempted: it needs real evaluation, and a wrong answer there reports a
    live assert as dead. For the same reason ``len(y) < 1`` and ``len(y) <= 0``
    are not tautologies -- they hold only for an empty container.
    """
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if _is_literal_true(node):
        return True
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
        return False
    bound = _numeric_literal(node.comparators[0])
    if bound is None or not _is_count_like(node.left):
        return False
    # A count is >= 0 by construction, so only the lower-bounded forms can be
    # tautologies.
    operator = node.ops[0]
    if isinstance(operator, ast.GtE):
        return bound == 0
    return isinstance(operator, ast.Gt) and bound < 0


def _is_literal_true(node):
    """Is this expression decidable to ``True`` without reading any value?

    An operand built only from literals cannot be influenced by the record, so
    a truthy result means the surrounding ``or`` decides the assert on its own
    and the comparison beside it is never evaluated.

    Only ``True`` counts. A literal that folds to ``False`` -- ``1 == 2`` --
    cannot short-circuit anything, so it is not a bypass.
    """
    value = _literal_value(node)
    return value is not _NOT_LITERAL and bool(value)
