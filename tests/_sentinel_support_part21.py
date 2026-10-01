# Shared-namespace fragment; import the public support entry point.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part21":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


#: A subscript key that is not a literal. Distinct from ``None``, which is a
#: literal key that simply selects nothing out of the container read so far.
_UNREADABLE_KEY = object()


def _literal_subscript_key(node):
    """The literal a subscript selects by, or :data:`_UNREADABLE_KEY`.

    #422. ``holder[0]`` and ``holder["k"]`` write the key as an
    :class:`ast.Constant`, but ``holder[-1]`` writes a negated literal as a
    :class:`ast.UnaryOp` around one -- never ``Constant(-1)`` -- so the
    negative spelling needs its own arm or every negative index would decline.

    Only ``USub``/``UAdd`` over an integer constant is read. ``-x``, ``-len(h)``
    and ``+x`` all depend on a value this analyzer does not evaluate, so they
    decline and leave the assert ``enforced``.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
        operand = node.operand.value
        if isinstance(operand, int) and not isinstance(operand, bool):
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return operand
    return _UNREADABLE_KEY


def _dereferenced_header_value(expression, bound, tree, function):
    """The value a subscripted or attribute-read ``with`` header carries.

    #422. The header does not name a suppressor; it names a *place* one was
    put, and the suppressor is reachable by dereferencing. Two step kinds are
    followed, and each is resolved by reading the same assignment the rest of
    the module already reads:

    * a subscript -- ``holder[0]``, ``holder["k"]`` -- where ``holder`` is
      bound to a list, tuple, set or dict literal, and the key is a literal.
    * an attribute read -- ``Box.ctx`` -- where ``Box`` is a class this file
      defines and ``ctx`` is assigned in its body or directly onto the class.

    The header is an arbitrarily *chained* expression, and the steps nest
    inward-left, so the walk resolves the **base first** and then applies each
    enclosing step in source order:

        holder[0][0]      base ``holder`` -> list -> [0] -> list -> [0] -> call
        Box.b.c           base ``Box``    -> cls  -> .b -> cls  -> .c -> call

    Resolving the outer step first, or recursing into ``expression.value``
    without then applying the outer step, stops one short and leaves exactly
    the false-LIVE the issue files, merely relocated -- the defect is not
    "one dereference", it is "an unfollowed step".

    Only statically readable steps are followed. A computed key, a
    factory-built container, an absent key, an out-of-range index, an
    attribute on a name that is not a locally defined class, and a class
    attribute assigned inside a nested ``def`` all return ``None``, which
    leaves the assert ``enforced``. That is the safe answer and the common
    one: reporting a live contract as defeated requires guessing a value, and
    guessing would drop real contracts. Nothing here decides suppression --
    the caller decides the value that is actually entered, which is what
    keeps a ``nullcontext`` or a wrong-exception suppressor carried through
    either shape correctly ``enforced``.
    """
    steps = []
    node = expression
    # Peel the chain from the outside in, remembering each step in source
    # order. `node` ends at the base: a Name, or None if the chain is rooted
    # in something that is not a readable name at all.
    while True:
        if isinstance(node, ast.Subscript):
            key = _literal_subscript_key(node.slice)
            if key is _UNREADABLE_KEY:
                return None
            steps.append(("item", key))
            node = node.value
            continue
        if isinstance(node, ast.Attribute):
            steps.append(("attr", node.attr))
            node = node.value
            continue
        break
    steps.reverse()
    if not steps or not isinstance(node, ast.Name):
        return None

    module = tree if tree is not None else _module_tree()
    if not _header_reference_setup_is_inert(module, function, bound, expression):
        return None
    value = _header_base_value(node.id, module, function, expression)
    if value is None:
        return None
    for kind, key in steps:
        if value is None:
            return None
        if kind == "item":
            value = _literal_element(value, key, function, bound)
        else:
            value = _read_attribute(value, key, module, function, expression)
    return value


def _header_base_value(name, module, function, header):
    """The value ``name`` holds at ``header``, or ``None``.

    A chain may root in either kind of readable base, so both are tried:

    * an assigned literal -- ``holder = [suppress(AssertionError)]``, whose
      value is the container the first step indexes;
    * a class this file defines -- ``Box``, whose value is the class the
      first attribute step reads from.

    The enclosing function is offered before the module so an inner binding
    shadows an outer one of the same spelling, the same order the rest of
    this module uses. A module-level store is read without a position cutoff
    because a function body is only reached after module initialization.

    An assignment is offered first because a rebinding of the name is what
    the header actually reads at that point; a class definition is the
    fallback for the common case where nothing rebinds it. Neither ordering
    is a guess about a value: both come from a store visible at ``header``,
    and a name that resolves to neither returns ``None``.
    """
    for scope in reversed(_class_lookup_scopes(module, function)):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if name in _signature_bound_names(scope):
                return None
            bindings = [
                node
                for node in _own_scope_bindings(scope)
                if any(_names_bound_by_statement(node, name))
            ]
            if bindings and any(node not in scope.body for node in bindings):
                return None
            if bindings and scope is not function:
                return None
            if bindings and not any(
                (node.lineno, node.col_offset) < (header.lineno, header.col_offset)
                for node in bindings
            ):
                return None
            if bindings and not any(
                isinstance(node, (ast.Assign, ast.ClassDef)) for node in bindings
            ):
                return None
        before = header if not isinstance(scope, ast.Module) else None
        value = _assigned_value(name, scope, before)
        if value is not None:
            return _header_attribute_value(value, function)
    return _locally_defined_classes(module, function, header).get(name)


def _class_from_type_call(value, function=None):
    """A synthetic class body for ``type(name, bases, namespace)``, or ``None``.

    A class built by ``type(...)`` is not a :class:`ast.ClassDef`, but its
    attributes are written out literally in the call:

    .. code-block:: python

        Box = type("Box", (), {"ctx": contextlib.suppress(AssertionError)})
        with Box.ctx:

    The namespace is a literal dict, so its entries are as readable as a
    ``class`` body's assignments. Synthesizing a ``ClassDef`` from it lets one
    resolution path serve both spellings, instead of leaving the dynamic
    spelling as a second, separately-maintained gap.

    Only the fully literal spelling qualifies. A computed namespace, a
    ``**``-merged or omitted namespace, and a non-``type`` factory all return
    ``None``, so a class assembled at runtime is not guessed at -- the header
    then stays ``enforced``, which is the safe direction. The base classes are
    not interpreted: only the attributes of the namespace being built are
    read, because an inherited attribute belongs to a class this rule has not
    established it can see.
    """
    if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)):
        return None
    if value.func.id != "type" or len(value.args) != 3:
        return None
    if value.keywords or function is None or _callee_is_shadowed(value.func, function, value):
        return None
    name, _bases, namespace = value.args
    if not isinstance(_bases, ast.Tuple) or _bases.elts:
        return None
    if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
        return None
    if not isinstance(namespace, ast.Dict):
        return None
    built = ast.ClassDef(
        name=name.value,
        bases=[],
        keywords=[],
        body=[],
        decorator_list=[],
        type_params=[],
    )
    ast.copy_location(built, namespace)
    for key, item in zip(namespace.keys, namespace.values):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            # `**other` contributes attributes this rule cannot see.
            return None
        store = ast.Assign(targets=[ast.Name(id=key.value, ctx=ast.Store())], value=item)
        ast.copy_location(store, item)
        built.body.append(store)
    return built


def _read_attribute(value, attr, module, function, header):
    """The ``attr`` of ``value``, or ``None`` when it is not readable.

    ``value`` is a class this file defines, so there are two readable places
    the attribute can live: the class body, and a direct ``Class.attr = ...``
    store either at module level or in the enclosing function. Only the last
    such store before ``header`` wins, matching :func:`_assigned_value`'s
    last-store-wins rule. Inside the class body the same rule applies, so a
    class that rebinds its own attribute is read at the value in force at
    ``header`` rather than its first one.

    ``AnnAssign`` is included because ``Box.ctx: object = suppress(...)`` is
    the annotated spelling of the same store; a bare annotation with no value
    binds nothing and is correctly skipped.
    """
    if not isinstance(value, ast.ClassDef):
        return None
    in_body = None
    for child in value.body:
        if isinstance(child, ast.Assign):
            if not any(
                isinstance(target, ast.Name) and target.id == attr for target in child.targets
            ):
                continue
            found = child.value
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            if child.target.id != attr or child.value is None:
                continue
            found = child.value
        else:
            continue
        if header is not None and (child.lineno, child.col_offset) >= (
            header.lineno,
            header.col_offset,
        ):
            continue
        in_body = found
    for store in _class_attribute_stores(module, header, function):
        last = store[-1]
        if any(
            isinstance(target, ast.Attribute)
            and target.attr == attr
            and isinstance(target.value, ast.Name)
            and target.value.id == value.name
            for target in getattr(last, "targets", ())
        ):
            # An outside store of the same attribute shadows the class-body
            # one: it is later in the program and is what the header reads.
            return _header_attribute_value(last.value, function)
    return _header_attribute_value(in_body, function)


def _header_attribute_value(value, function=None):
    """``value``, with a ``type(...)`` build widened into a readable class."""
    if value is None:
        return None
    return _class_from_type_call(value, function) or value


def _class_attribute_stores(module, before, function=None):
    """``Class.attr = value`` stores reachable for ``header``, grouped.

    Returned oldest-first per attribute so the caller can take the last store
    before ``before``. Only a scope's *own* top-level statements are read,
    never a nested body: a store inside an inner ``def`` is not visible to the
    function being judged, and walking into one would attribute a local
    binding to the enclosing scope.
    """
    scopes = [
        scope
        for scope in reversed(_class_lookup_scopes(module, function))
        if not isinstance(scope, ast.Module)
    ] + [module]
    for scope in scopes:
        nodes = scope.body if isinstance(scope, ast.Module) else _scope_body_nodes(scope)
        grouped = {}
        for statement in sorted(
            (node for node in nodes if isinstance(node, ast.Assign)),
            key=lambda node: (node.lineno, node.col_offset),
        ):
            if not isinstance(statement, ast.Assign):
                continue
            if before is not None and (statement.lineno, statement.col_offset) >= (
                before.lineno,
                before.col_offset,
            ):
                continue
            for target in statement.targets:
                if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
                    continue
                grouped.setdefault((target.value.id, target.attr), []).append(statement)
        for statements in grouped.values():
            if statements:
                yield statements


def _literal_element(container, key, function=None, bound=None):
    """The element ``key`` selects from a literal ``container``, or ``None``.

    Only a literal list, tuple or dict is indexed, and only because its
    contents are written in the file being judged. A subscript into anything
    else -- a parameter, a comprehension, a call result -- is not statically
    knowable and returns ``None``. A ``set`` literal is deliberately excluded
    even though its elements are equally written out: a set is not
    subscriptable, so ``holder[0]`` over one raises :class:`TypeError` before
    the body runs. Reading an element off it would report a defeat the
    runtime never reaches.

    A negative index is read through its ``UnaryOp`` spelling, because that is
    how ``holder[-1]`` parses: ``UnaryOp(USub, Constant(1))``, never
    ``Constant(-1)``. Only a negated literal integer is read, so ``-x`` and
    ``-len(h)`` still decline.

    ``bool`` is excluded from the integer-index arm because :class:`bool` is a
    subclass of :class:`int`: ``holder[True]`` really is ``holder[1]`` in
    CPython, and accepting it here would let a boolean key quietly stand in
    for an index the author wrote differently. It falls through to the
    ``None`` below, which is the safe direction.
    """
    if not _header_literal_construction_is_safe(container, function, bound):
        return None
    if isinstance(container, (ast.List, ast.Tuple)):
        elements = container.elts
        if (
            isinstance(key, int)
            and not isinstance(key, bool)
            and -len(elements) <= key < len(elements)
        ):
            return elements[key]
        return None
    if isinstance(container, ast.Dict):
        for pair in reversed(list(zip(container.keys, container.values))):
            if isinstance(pair[0], ast.Constant) and pair[0].value == key:
                return pair[1]
    return None


def _header_reference_setup_is_inert(module, function, bound, expression):
    """Refuse opaque construction, mutation, or implicit execution of lookups.

    The proof assumes imported manager implementations remain stable outside
    the supplied source. Unmodelled mutations inside that source decline.
    """
    if function is None:
        return False
    classes = {node.name for node in ast.walk(module) if isinstance(node, ast.ClassDef)}
    classes.update(
        target.id
        for node in ast.walk(module)
        if isinstance(node, ast.Assign) and _class_from_type_call(node.value, function) is not None
        for target in node.targets
        if isinstance(target, ast.Name)
    )
    for node in ast.walk(module):
        if isinstance(node, ast.Expr) and not (
            isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        ):
            return False
        if isinstance(node, ast.With) and any(
            item.optional_vars is not None
            or (
                item.context_expr is not expression
                and not (
                    isinstance(item.context_expr, ast.Call)
                    and _header_literal_construction_is_safe(item.context_expr, function, bound)
                )
            )
            for item in node.items
        ):
            return False
        if isinstance(node, ast.AnnAssign) and not (
            isinstance(node.annotation, ast.Constant)
            or isinstance(node.annotation, ast.Name)
            and node.annotation.id == "object"
            and not _callee_is_shadowed(node.annotation, function, node.annotation)
        ):
            return False
        if isinstance(node, ast.Assign) and not _header_literal_construction_is_safe(
            node.value, function, bound
        ):
            return False
        if (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and not _header_literal_construction_is_safe(node.value, function, bound)
        ):
            return False
        if isinstance(node, (ast.BinOp, ast.BoolOp)):
            return False
        if isinstance(node, ast.Call) and any(
            _resolves_to(node.func, dotted, bound)
            for dotted in ("contextlib.suppress", "contextlib.nullcontext")
        ):
            root = node.func.value if isinstance(node.func, ast.Attribute) else node.func
            if not isinstance(root, ast.Name) or not _literal_subject_callee_is_intact(
                root, function, node
            ):
                return False
        if isinstance(node, (ast.Import, ast.ImportFrom)) and not (
            isinstance(node, ast.Import)
            and all(alias.name == "contextlib" for alias in node.names)
            or isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "contextlib"
        ):
            return False
        if isinstance(node, ast.ClassDef) and (
            node.bases or node.keywords or node.decorator_list or getattr(node, "type_params", ())
        ):
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.decorator_list
            or node.returns is not None
            or getattr(node, "type_params", ())
            or any(arg.annotation is not None for arg in _all_args(node) if arg is not None)
            or any(
                value is not None and not isinstance(value, ast.Constant)
                for value in node.args.defaults + node.args.kw_defaults
            )
        ):
            return False
        if isinstance(
            node,
            (
                ast.AugAssign,
                ast.Delete,
                ast.NamedExpr,
                ast.If,
                ast.For,
                ast.While,
                ast.Try,
                ast.TryStar,
                ast.Match,
                ast.IfExp,
            ),
        ):
            return False
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            return False
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and not (isinstance(node.value, ast.Name) and node.value.id in classes)
        ):
            return False
        if isinstance(node, ast.Call) and not (
            any(
                _resolves_to(node.func, dotted, bound)
                for dotted in ("contextlib.suppress", "contextlib.nullcontext")
            )
            or _class_from_type_call(node, function) is not None
        ):
            return False
    return True


def _header_literal_construction_is_safe(value, function, bound):
    """Every element is evaluated before selection, including unselected ones."""
    if function is None or bound is None:
        return False
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, ast.Name):
        return value.id in {
            "AssertionError",
            "Exception",
            "BaseException",
            "ValueError",
            "TypeError",
            "RuntimeError",
            "KeyError",
        } and not _callee_is_shadowed(value, function, value)
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return all(
            _header_literal_construction_is_safe(element, function, bound) for element in value.elts
        )
    if isinstance(value, ast.Dict):
        return all(
            isinstance(key, ast.Constant)
            and isinstance(key.value, (str, int, float, bool, type(None)))
            and _header_literal_construction_is_safe(item, function, bound)
            for key, item in zip(value.keys, value.values)
        )
    if isinstance(value, ast.Call):
        if _class_from_type_call(value, function) is not None:
            return _header_literal_construction_is_safe(value.args[2], function, bound)
        return (
            not value.keywords
            and any(
                _resolves_to(value.func, dotted, bound)
                for dotted in ("contextlib.suppress", "contextlib.nullcontext")
            )
            and all(
                _header_literal_construction_is_safe(argument, function, bound)
                for argument in value.args
            )
        )
    return False
