# Shared-namespace fragment for reviewed sentinel helpers.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part18":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _try_only_binds_names_the_witness_ignores(node, function, bound):
    """Admit only caught import/pass work with inert handlers and cleanup."""
    if not isinstance(node, (ast.Try, ast.TryStar)):
        return False
    loaded = {
        n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    if any(handler.name in loaded for handler in node.handlers if handler.name):
        return False
    if any(
        not _witness_try_tail_is_inert(arm, function, bound)
        for arm in [node.orelse, node.finalbody, *(handler.body for handler in node.handlers)]
    ):
        return False
    handlers = [_witness_try_handler_types(handler, function) for handler in node.handlers]
    if any(types is None for types in handlers):
        return False
    for child in node.body:
        if isinstance(child, ast.Pass):
            continue
        if isinstance(child, (ast.Try, ast.TryStar)):
            if not _try_only_binds_names_the_witness_ignores(child, function, bound):
                return False
            continue
        if isinstance(child, ast.Expr) and _witness_source_helper_is_inert(child.value, function):
            continue
        if not _import_only_binds_the_resolved_root(child, function, bound):
            return False
        # Import initializers execute arbitrary code, independent of the names
        # they bind. Only the canonical contextlib contract is readable here.
        if not _witness_import_has_readable_failure(child):
            return False
        if not any(any(issubclass(ImportError, kind) for kind in types) for types in handlers):
            return False
    return True


def _witness_try_tail_is_inert(statements, function, bound):
    """Cleanup/handler paths may pass or execute another fully admitted try."""
    return all(
        isinstance(child, ast.Pass)
        or _try_only_binds_names_the_witness_ignores(child, function, bound)
        for child in statements
    )


def _witness_try_handler_types(handler, function):
    """Read only unshadowed builtin exception spellings, without evaluation."""
    if handler.type is None:
        return (BaseException,)
    values = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    result = []
    module = _module_for_function(function)
    for value in values:
        if not isinstance(value, ast.Name):
            return None
        name = value.id
        if name in _signature_bound_names(function) or any(
            any(_names_bound_by_statement(statement, name))
            for statement in _own_scope_bindings(function)
        ):
            return None
        parent = _nonlocal_parent_function(function, module) if module is not None else None
        while parent is not None:
            if name in _signature_bound_names(parent) or any(
                any(_names_bound_by_statement(statement, name))
                for statement in _own_scope_bindings(parent)
            ):
                return None
            parent = _nonlocal_parent_function(parent, module)
        if module is not None and any(
            any(_names_bound_by_statement(node, name))
            for statement in module.body
            for node in _module_level_bindings(statement)
        ):
            return None
        kind = getattr(__import__("builtins"), name, None)
        if not isinstance(kind, type) or not issubclass(kind, BaseException):
            return None
        result.append(kind)
    return tuple(result)


def _witness_import_has_readable_failure(node):
    """Unknown module initializers may raise outside ImportError or mutate roots."""
    return (
        isinstance(node, ast.Import)
        and bool(node.names)
        and all(alias.name == "contextlib" for alias in node.names)
    )


def _witness_source_helper_is_inert(call, function):
    """Read a zero-argument module helper with only literal returns/pass.

    Caller callbacks and imported helpers remain unknown. This proof assumes
    source function code is stable; it does not execute or inspect live helpers.
    """
    if (
        not isinstance(call, ast.Call)
        or not isinstance(call.func, ast.Name)
        or call.args
        or call.keywords
    ):
        return False
    name = call.func.id
    module = _module_for_function(function)
    if module is None or name in _signature_bound_names(function):
        return False
    if any(any(_names_bound_by_statement(n, name)) for n in _own_scope_bindings(function)):
        return False
    parent = _nonlocal_parent_function(function, module)
    while parent is not None:
        if name in _signature_bound_names(parent) or any(
            any(_names_bound_by_statement(n, name)) for n in _own_scope_bindings(parent)
        ):
            return False
        parent = _nonlocal_parent_function(parent, module)
    bindings = [
        n
        for statement in module.body
        for n in [statement, *_module_level_bindings(statement)]
        if any(_names_bound_by_statement(n, name))
    ]
    if len(bindings) != 1 or not isinstance(bindings[0], ast.FunctionDef):
        return False
    for statement in module.body:
        if isinstance(statement, ast.Import) and all(
            a.name == "contextlib" for a in statement.names
        ):
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = statement.args
            annotated = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            if args.vararg:
                annotated.append(args.vararg)
            if args.kwarg:
                annotated.append(args.kwarg)
            if (
                statement.decorator_list
                or statement.returns
                or getattr(statement, "type_params", [])
                or any(a.annotation is not None for a in annotated)
                or any(
                    not isinstance(v, ast.Constant)
                    for v in [*args.defaults, *args.kw_defaults]
                    if v is not None
                )
            ):
                return False
            continue
        if (
            isinstance(statement, ast.Pass)
            or (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
            or (
                isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Constant)
                and all(isinstance(t, ast.Name) for t in statement.targets)
            )
        ):
            continue
        return False
    helper = bindings[0]
    if (
        helper.decorator_list
        or helper.returns
        or getattr(helper, "type_params", [])
        or _signature_bound_names(helper)
    ):
        return False
    return all(
        isinstance(n, ast.Pass)
        or (isinstance(n, ast.Return) and (n.value is None or isinstance(n.value, ast.Constant)))
        or (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
        for n in helper.body
    )


def _witness_header_root_has_unknown_import(function, target):
    """Decline a nullcontext carrier whose local import resolves another module.

    A failed import leaves the local root unbound; a successful unknown import
    may provide an arbitrary callable. Neither establishes a live assertion.
    """
    for header in _ancestors(function, target):
        if not isinstance(header, ast.With) or len(header.items) != 1:
            continue
        expression = header.items[0].context_expr
        if not isinstance(expression, ast.Name):
            continue
        values = [
            n.value
            for n in _own_scope_bindings(function)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == expression.id for t in n.targets)
        ]
        for value in values:
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "nullcontext"
                and isinstance(value.func.value, ast.Name)
            ):
                continue
            root = value.func.value.id
            bindings = [
                n for n in _own_scope_bindings(function) if any(_names_bound_by_statement(n, root))
            ]
            if len(bindings) == 1 and isinstance(bindings[0], (ast.Import, ast.ImportFrom)):
                n = bindings[0]
                if (
                    n.lineno < value.lineno
                    and not _is_canonical_module_import(n, root)
                    and any(isinstance(a, (ast.Try, ast.TryStar)) for a in _ancestors(function, n))
                ):
                    return True
    return False


def _expression_cannot_raise(node):
    """Only literal values have a statically guaranteed evaluation here."""
    return node is None or isinstance(node, ast.Constant)
