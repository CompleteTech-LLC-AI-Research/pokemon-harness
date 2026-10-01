# Shared-namespace fragment for completed local instance loops.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part31":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


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
                element = _completed_loop_instance_element(node, scope, before)
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


def _completed_loop_instance_element(loop, function, read):
    """#417: a stable local instance after a source-inert completed loop.

    This widens only the after-loop instance lookup; in-body multi-element
    lookup is unchanged. Source-visible callbacks, protocol/member edits and
    unmodeled setup cannot supply this new proof.
    """
    if not isinstance(function, ast.FunctionDef) or read is None:
        return None
    header = function.body[-1]
    if not isinstance(header, ast.With) or len(header.items) != 1 or not _contains(header, read):
        return None
    if (
        not isinstance(loop.iter, (ast.Tuple, ast.List))
        or not isinstance(loop.target, ast.Name)
        or len(loop.iter.elts) < 2
        or not _loop_completes_before(loop, header, function)
        or _loop_body_rebinds_the_name(loop, loop.target.id)
        or any(not isinstance(statement, ast.Pass) for statement in [*loop.body, *loop.orelse])
    ):
        return None
    module = _module_for_function(function)
    if (
        module is None
        or function not in module.body
        or not _primitive_alias_setup_is_inert(module)
        or not _completed_loop_import_source_is_inert(module, loop)
    ):
        return None
    classes = {}
    roots = set()
    for statement in function.body[:-1]:
        if statement is loop or isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Import) and all(
            alias.name == "contextlib" for alias in statement.names
        ):
            roots.update(alias.asname or alias.name for alias in statement.names)
            continue
        if isinstance(statement, ast.ClassDef) and _stable_loop_instance_class(statement):
            if statement.name in classes or statement.lineno >= loop.lineno:
                return None
            classes[statement.name] = statement
            continue
        return None
    bound = _bound_names(module, function)
    for statement in module.body:
        if isinstance(statement, ast.Import):
            roots.update(alias.asname or alias.name for alias in statement.names)
    if (roots | set(classes)) & _signature_bound_names(function):
        return None
    for element in loop.iter.elts:
        if isinstance(element, ast.Constant):
            continue
        if not isinstance(element, ast.Call) or element.args or element.keywords:
            return None
        if isinstance(element.func, ast.Name) and element.func.id in classes:
            continue
        if not _resolves_to(element.func, "contextlib.nullcontext", bound):
            return None
    last = loop.iter.elts[-1]
    if not (
        isinstance(last, ast.Call) and isinstance(last.func, ast.Name) and last.func.id in classes
    ):
        return None
    return last


def _completed_loop_import_source_is_inert(module, loop):
    """An imported constructor cannot be trusted across source-visible writers."""
    return (
        not _entered_builtin_module_is_mutated(module)
        and not any(isinstance(node, (ast.Global, ast.Nonlocal)) for node in ast.walk(module))
        and not any(
            isinstance(node, ast.Call) and node not in loop.iter.elts for node in ast.walk(module)
        )
    )


def _stable_loop_instance_class(statement):
    if (
        statement.bases
        or statement.keywords
        or statement.decorator_list
        or getattr(statement, "type_params", ())
    ):
        return False
    methods = {}
    for method in statement.body:
        if (
            not isinstance(method, ast.FunctionDef)
            or method.name not in ("__enter__", "__exit__")
            or method.name in methods
            or not _class_protocol_method_creation_is_inert(method)
            or len(method.body) != 1
            or not isinstance(method.body[0], ast.Return)
        ):
            return False
        methods[method.name] = method
    if set(methods) != {"__enter__", "__exit__"}:
        return False
    enter, exit_method = methods["__enter__"], methods["__exit__"]
    enter_args = [*enter.args.posonlyargs, *enter.args.args]
    exit_args = [*exit_method.args.posonlyargs, *exit_method.args.args]
    return (
        len(enter_args) == 1
        and enter.args.vararg is None
        and isinstance(enter.body[0].value, ast.Name)
        and enter.body[0].value.id == enter_args[0].arg
        and (len(exit_args) == 4 or len(exit_args) == 1 and exit_method.args.vararg)
        and isinstance(exit_method.body[0].value, ast.Constant)
    )
