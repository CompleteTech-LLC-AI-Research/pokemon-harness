# Shared-namespace fragment; import the public support entry point.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part20":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _suppression_names(call, bound=None, function=None):
    """The exception names passed to a ``suppress(...)`` call.

    A tuple is flattened, because ``suppress((TypeError, ValueError))`` and
    ``pytest.raises((KeyError, RuntimeError))`` are both ordinary spellings that
    name two types. Reading only the tuple node itself would report the
    unreadable case ``BaseException`` for *every* tuple, and since
    ``BaseException`` catches ``AssertionError``, that would report the pinned
    file's four tuple-typed ``pytest.raises`` sites as defeated -- a false alarm
    on real, live asserts. Measured on ``test_timed_menu_milestones.py``:
    ``(TypeError, ValueError)``, ``(KeyError, ValueError, RuntimeError)`` and
    ``(ValueError, RuntimeError)`` all appear.

    An element that is not a plain name is still reported as universal, so an
    argument the check cannot read is never assumed to be harmless.

    A genuinely imported, intact zero-argument ``contextlib.suppress``
    suppresses nothing. Unknown callees and keyword/starred arguments retain
    the conservative fallback. ``pytest.raises`` remains separately gated.
    """
    if _empty_suppress_call_is_genuine(call, bound, function):
        return []
    names = []
    for argument in call.args:
        if isinstance(argument, ast.Tuple):
            names.extend(
                element.id if isinstance(element, ast.Name) else "BaseException"
                for element in argument.elts
            )
        else:
            names.append(argument.id if isinstance(argument, ast.Name) else "BaseException")
    return names or ["BaseException"]


def _empty_suppress_call_is_genuine(call, bound, function):
    """Read only stable absolute imports, never a similarly named callable."""
    if call.args or call.keywords or function is None or bound is None:
        return False
    if not _resolves_to(call.func, "contextlib.suppress", bound):
        return False
    root = call.func.value if isinstance(call.func, ast.Attribute) else call.func
    if not isinstance(root, ast.Name):
        return False
    name = root.id
    path = "contextlib" if isinstance(call.func, ast.Attribute) else "contextlib.suppress"
    module = _module_for_function(function)
    if module is None or name in _signature_bound_names(function):
        return False

    def canonical(statement):
        if isinstance(statement, ast.Import):
            return path == "contextlib" and any(
                alias.name == "contextlib" and (alias.asname or alias.name) == name
                for alias in statement.names
            )
        return (
            path == "contextlib.suppress"
            and isinstance(statement, ast.ImportFrom)
            and statement.level == 0
            and statement.module == "contextlib"
            and any(
                alias.name == "suppress" and (alias.asname or alias.name) == name
                for alias in statement.names
            )
        )

    parent = _nonlocal_parent_function(function, module)
    while parent is not None:
        if name in _signature_bound_names(parent) or any(
            any(_names_bound_by_statement(node, name)) for node in _own_scope_bindings(parent)
        ):
            return False
        parent = _nonlocal_parent_function(parent, module)
    for node in _own_scope_bindings(function):
        if not any(_names_bound_by_statement(node, name)):
            continue
        if not canonical(node) or node not in function.body or node.lineno >= call.lineno:
            return False
    for statement in module.body:
        for node in [statement, *_module_level_bindings(statement)]:
            if any(_names_bound_by_statement(node, name)) and not canonical(node):
                return False
    # Any member write can replace the imported implementation or its protocol.
    # The proof assumes no external mutation of the imported callable.
    for node in ast.walk(module):
        if isinstance(node, ast.Assign) and (
            not all(isinstance(target, ast.Name) for target in node.targets)
            or not _empty_suppress_setup_value_is_safe(node.value, bound, function)
        ):
            return False
        if isinstance(node, ast.AnnAssign):
            return False
        if isinstance(node, ast.Expr) and not _empty_suppress_setup_value_is_safe(
            node.value, bound, function
        ):
            return False
        if isinstance(node, ast.ClassDef) and (node.bases or node.keywords):
            return False
        if isinstance(
            node,
            (
                ast.AugAssign,
                ast.Delete,
                ast.If,
                ast.While,
                ast.For,
                ast.AsyncFor,
                ast.Try,
                ast.TryStar,
                ast.Match,
                ast.IfExp,
            ),
        ):
            return False
        if isinstance(node, ast.With):
            for item in node.items:
                expression = item.context_expr
                if isinstance(expression, ast.NamedExpr):
                    expression = expression.value
                if isinstance(expression, ast.Name):
                    if expression.id in _signature_bound_names(function):
                        return False
                    expression = _assigned_value(expression.id, function, node) or _assigned_value(
                        expression.id, module
                    )
                if isinstance(expression, (ast.Subscript, ast.Attribute)):
                    dereference = globals().get("_dereferenced_header_value")
                    expression = (
                        dereference(expression, bound, module, function) if dereference else None
                    )
                if not isinstance(expression, ast.Call) or not any(
                    _resolves_to(expression.func, path, bound)
                    for path in ("contextlib.suppress", "contextlib.nullcontext")
                ):
                    return False
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            return False
        if isinstance(node, (ast.Import, ast.ImportFrom)) and not (
            isinstance(node, ast.Import)
            and all(alias.name == "contextlib" for alias in node.names)
            or isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "contextlib"
        ):
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
            node.decorator_list
            or getattr(node, "type_params", ())
            or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (
                node.returns is not None
                or any(arg.annotation is not None for arg in _all_args(node) if arg is not None)
                or any(
                    value is not None and not isinstance(value, ast.Constant)
                    for value in node.args.defaults + node.args.kw_defaults
                )
            )
        ):
            return False
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            return False
        if isinstance(node, ast.Call) and not any(
            _resolves_to(node.func, path, bound)
            for path in ("contextlib.suppress", "contextlib.nullcontext")
        ):
            return False
    return True


def _empty_suppress_setup_value_is_safe(value, bound, function):
    """No arbitrary lookup or construction can run ahead of the empty header."""
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, ast.Name):
        return (
            value.id in _signature_bound_names(function)
            or (
                value.id
                in {"AssertionError", "Exception", "BaseException", "ValueError", "TypeError"}
                and not _callee_is_shadowed(value, function, value)
            )
            or _resolved_dotted(value, bound) in {"contextlib.suppress", "contextlib.nullcontext"}
        )
    if isinstance(value, ast.Attribute):
        return _resolved_dotted(value, bound) in {"contextlib.suppress", "contextlib.nullcontext"}
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return all(
            _empty_suppress_setup_value_is_safe(element, bound, function) for element in value.elts
        )
    if isinstance(value, ast.Dict):
        return all(
            isinstance(key, ast.Constant)
            and isinstance(key.value, (str, int, float, bool, type(None)))
            and _empty_suppress_setup_value_is_safe(item, bound, function)
            for key, item in zip(value.keys, value.values)
        )
    if isinstance(value, ast.Call) and not value.keywords:
        return (
            _resolved_dotted(value.func, bound) in {"contextlib.suppress", "contextlib.nullcontext"}
            and (
                not _resolves_to(value.func, "contextlib.nullcontext", bound)
                or len(value.args) <= 1
            )
            and all(
                _empty_suppress_setup_value_is_safe(argument, bound, function)
                for argument in value.args
            )
        )
    return False
