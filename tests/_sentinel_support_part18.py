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
        if not _import_only_binds_the_resolved_root(child, function, bound):
            return False
        # #485. Whether the *import* fails is not a source question, so the
        # earlier `_witness_import_has_readable_failure` gate refused every
        # module it could not prove importable and left the filed
        # missing-import case declined. That refusal is the damaging
        # direction: the witness below is asking "does this ``try`` let control
        # reach the loop-else carrying the manager", and a caught failed import
        # does let it, whether the module exists or not.
        #
        #     try:
        #         import nope_missing_xyz
        #     except ImportError:
        #         pass
        #     cs = contextlib.nullcontext()
        #     for item in (1,):
        #         break                        # the else never runs
        #     else:
        #         cs = contextlib.suppress(AssertionError)
        #     with cs:
        #         assert x != 1
        #
        # `nope_missing_xyz` raises `ModuleNotFoundError`, the handler catches
        # it, and execution continues -- so `cs` is still the `nullcontext`,
        # the header is entered, and the assert **fires** for `x == 0`. The
        # analyzer reported it defeated, which is #308 criterion 1.
        #
        # So the gate is now the same one every other import admits:
        # `_import_only_binds_the_resolved_root` above has already proved the
        # statement binds no name this witness resolves through, which is the
        # only way an import could change what the ``with`` enters. The retired
        # gate instead hard-coded `import contextlib`, so it admitted a
        # *module name* rather than answering the question the witness asks --
        # and "is this module readable" is not a source property at all: the
        # import may fail, the handler may catch it, and control may still
        # reach the header, which is the case this issue is about.
        #
        # The two guards left in place are what make that safe, and neither is
        # about the module: a failed import must be *caught* by a handler whose
        # type is a real, unshadowed builtin, and every handler, ``else`` and
        # ``finally`` arm must be inert. An uncaught failure, a narrowed
        # handler, or an arm that returns or raises still stops control
        # reaching the header, and those rows keep the existing defeated
        # answer. The shadowing spellings are refused by the
        # `_import_only_binds_the_resolved_root` call, not here: those bind a
        # walked root, and admitting them would be a false-LIVE.
        #
        # The import need only be *caught when it fails*, which is why the
        # check below asks the handler set to cover `ImportError` rather than
        # to match one specific exception.
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
