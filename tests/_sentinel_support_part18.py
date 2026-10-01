# Shared-namespace fragment for reviewed sentinel helpers.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part18":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _try_only_binds_names_the_witness_ignores(node, function, bound):
    """Admit only caught import/pass work with inert handlers and cleanup."""
    if not isinstance(node, (ast.Try, ast.TryStar)):
        return False
    if any(handler.name is not None for handler in node.handlers):
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
        if not _import_only_binds_the_resolved_root(
            child, function, bound
        ) or not _witness_import_has_readable_failure(child):
            return False
        # This follows the existing transparent-import contract. A readable
        # failed import must be caught; narrower or unreadable handlers decline.
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
    """Only the canonical contextlib import has a source-readable contract.

    Unknown module initializers may mutate the walked manager roots or raise
    outside the handler. Current module availability is not a source proof.
    """
    return (
        isinstance(node, ast.Import)
        and bool(node.names)
        and all(alias.name == "contextlib" for alias in node.names)
    )
