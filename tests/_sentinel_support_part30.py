# Shared-namespace fragment for reached starred bindings and dead headers.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part30":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _known_starred_binding_blocks_query(function, query, owning):
    """A reached starred binding holds a builtin list until a later write.

    #423/#432/#430: successful unpacking always creates a list. Only reached
    stores and source-inert intervening operations establish this proof; opaque
    effects, possibly empty completed loops, and competing writes decline it.
    """
    if owning is None or not _primitive_alias_setup_is_inert(owning):
        return False
    bindings, _ = _store_bindings(function, {})
    prior = _statements_before_header(function, query)
    for header in ast.walk(function):
        if not isinstance(header, ast.With) or len(header.items) != 1:
            continue
        if not (_in_body(header, query) or header in prior):
            continue
        expression = header.items[0].context_expr
        if isinstance(expression, ast.NamedExpr):
            expression = expression.value
        if not isinstance(expression, ast.Name):
            continue
        name = expression.id
        for statement, _, _ in bindings.get(name, ()):
            if not _binds_starred_target(statement, name):
                continue
            if not _starred_binding_reaches_header(statement, header, function):
                continue
            position = lambda node: (node.lineno, node.col_offset)
            if any(
                other is not statement and position(statement) < position(other) < position(header)
                for other, _, _ in bindings.get(name, ())
            ):
                continue
            if _starred_header_interval_is_inert(statement, header, function, owning):
                return True
    return False


def _starred_binding_reaches_header(statement, header, function):
    prior = _statements_before_header(function, header)
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        if _in_body(statement, header):
            return True
        return statement in prior and _starred_completed_loop_has_element(statement)
    if statement in prior:
        return True
    for loop in prior:
        if not isinstance(loop, ast.For) or statement not in loop.body:
            continue
        if not _starred_completed_loop_has_element(loop):
            continue
        prefix = loop.body[: loop.body.index(statement)]
        if not any(
            isinstance(node, (ast.Return, ast.Break, ast.Continue))
            for sibling in prefix
            for node in ast.walk(sibling)
        ):
            return True
    return False


def _starred_completed_loop_has_element(loop):
    return (
        isinstance(loop, ast.For)
        and isinstance(loop.iter, (ast.Tuple, ast.List))
        and bool(loop.iter.elts)
        and not any(isinstance(element, ast.Starred) for element in loop.iter.elts)
    )


def _starred_header_interval_is_inert(statement, header, function, owning):
    position = lambda node: (node.lineno, node.col_offset)
    nodes = [
        node
        for node in ast.walk(function)
        if hasattr(node, "lineno") and position(statement) < position(node) < position(header)
    ]
    safe_calls = {
        id(node)
        for call in nodes
        if isinstance(call, ast.Call) and _starred_witness_call_is_inert(call, function)
        for node in ast.walk(call)
    }
    for node in nodes:
        if id(node) in safe_calls:
            continue
        if isinstance(
            node, (ast.Call, ast.Attribute, ast.Subscript, ast.Await, ast.Yield, ast.YieldFrom)
        ):
            return False
        if isinstance(node, (ast.Compare, ast.BinOp, ast.BoolOp, ast.UnaryOp)):
            try:
                ast.literal_eval(node)
            except (ValueError, TypeError):
                return False
        if isinstance(node, (ast.If, ast.While, ast.Assert)):
            test = node.test
            if any(
                not isinstance(child, (ast.Constant, ast.Load, ast.UnaryOp, ast.Not))
                for child in ast.walk(test)
            ):
                return False
        if isinstance(node, (ast.For, ast.AsyncFor)) and not _starred_completed_loop_has_element(
            node
        ):
            return False
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Try, ast.TryStar, ast.With),
        ):
            return False
    return True


def _contains_any(statements, target):
    """True if ``target`` is one of ``statements`` or lies beneath any of them.

    :func:`_contains` answers the same question for a single node, but the
    ``if``/``else`` split is a pair of *statement lists*, and asking a list for
    its ``_fields`` raised inside a decision function. A branch membership test
    is the question several rules ask of one of those lists.
    """
    return any(_contains(statement, target) for statement in statements or ())


_STARRED_PATH_LIST = object()
_STARRED_PATH_BREAK = object()


def _starred_binding_has_failure_witness(function, query, owning):
    """A real selected rebind can replace a list with a genuine live manager."""
    if owning is None or not isinstance(function, ast.FunctionDef) or function not in owning.body:
        return False
    if not _primitive_alias_setup_is_inert(owning):
        return False
    header = function.body[-1]
    if not isinstance(header, ast.With) or header.body != [query] or len(header.items) != 1:
        return False
    item = header.items[0]
    if item.optional_vars is not None or not isinstance(item.context_expr, ast.Name):
        return False
    if not any(isinstance(node, ast.Starred) for node in ast.walk(function)):
        return False
    if query.msg is not None and not isinstance(query.msg, ast.Constant):
        return False
    ordinary = {
        argument.arg
        for argument in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    }
    test = query.test
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.NotEq)
        and len(test.comparators) == 1
        and isinstance(test.left, ast.Name)
        and test.left.id in ordinary
        and isinstance(test.comparators[0], ast.Constant)
    ):
        return False
    base = {test.left.id: test.comparators[0].value}
    loaded = {
        n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    free = sorted((ordinary & loaded) - base.keys())
    if len(free) > 2:
        return False
    bound = _bound_names(owning, function)
    for values in _primitive_alias_environments(free, base):
        if _starred_witness_block(function.body[:-1], values, function, {}) is not True:
            continue
        manager = values.get(item.context_expr.id)
        if (
            isinstance(manager, ast.Call)
            and _starred_witness_call_is_inert(manager, function)
            and _resolves_to(manager.func, "contextlib.nullcontext", bound)
            and _primitive_alias_condition(test, values, function) is False
        ):
            return True
    return False


def _starred_witness_block(statements, values, function, helpers):
    for statement in statements:
        if isinstance(statement, (ast.Pass, ast.Nonlocal)):
            continue
        if isinstance(statement, ast.Import) and all(
            a.name == "contextlib" and a.asname in (None, "contextlib") for a in statement.names
        ):
            continue
        if isinstance(statement, ast.Assign):
            names = _starred_target_names(statement.targets)
            if names:
                if not all(
                    isinstance(target, (ast.Tuple, ast.List))
                    and len(target.elts) == 1
                    and isinstance(target.elts[0], ast.Starred)
                    and isinstance(target.elts[0].value, ast.Name)
                    for target in statement.targets
                ):
                    return False
                if not isinstance(statement.value, (ast.Tuple, ast.List)) or any(
                    _primitive_alias_value(element, values, function) is _ALIAS_WITNESS_UNKNOWN
                    for element in statement.value.elts
                ):
                    return False
                values.update({name: _STARRED_PATH_LIST for name in names})
                continue
            if not all(isinstance(t, ast.Name) for t in statement.targets):
                return False
            value = _primitive_alias_value(statement.value, values, function)
            if isinstance(statement.value, ast.Call) and not _starred_witness_call_is_inert(
                statement.value, function
            ):
                return False
            if value is _ALIAS_WITNESS_UNKNOWN:
                return False
            values.update({target.id: value for target in statement.targets})
            continue
        if isinstance(statement, ast.If):
            if any(
                isinstance(node, ast.Name) and values.get(node.id) is _STARRED_PATH_LIST
                for node in ast.walk(statement.test)
            ):
                return False
            choice = _primitive_alias_condition(statement.test, values, function)
            if choice is None:
                return False
            status = _starred_witness_block(
                statement.body if choice else statement.orelse, values, function, helpers
            )
            if status is not True:
                return status
            continue
        if isinstance(statement, ast.For):
            if (
                not isinstance(statement.iter, (ast.Tuple, ast.List))
                or len(statement.iter.elts) > 8
            ):
                return False
            if any(
                isinstance(node, ast.Call) and not _starred_witness_call_is_inert(node, function)
                for node in statement.iter.elts
            ):
                return False
            elements = [_primitive_alias_value(n, values, function) for n in statement.iter.elts]
            if any(v is _ALIAS_WITNESS_UNKNOWN for v in elements):
                return False
            for value in elements:
                names = _starred_target_names([statement.target])
                if names:
                    if not (
                        isinstance(statement.target, (ast.Tuple, ast.List))
                        and len(statement.target.elts) == 1
                        and isinstance(statement.target.elts[0], ast.Starred)
                        and isinstance(statement.target.elts[0].value, ast.Name)
                        and type(value) in (tuple, list, str, bytes, dict, set)
                    ):
                        return False
                    values.update({name: _STARRED_PATH_LIST for name in names})
                elif isinstance(statement.target, ast.Name):
                    values[statement.target.id] = value
                else:
                    return False
                status = _starred_witness_block(statement.body, values, function, helpers)
                if status is _STARRED_PATH_BREAK:
                    break
                if status is not True:
                    return False
            else:
                if _starred_witness_block(statement.orelse, values, function, helpers) is not True:
                    return False
            continue
        if isinstance(statement, ast.Break):
            return _STARRED_PATH_BREAK
        if isinstance(statement, ast.FunctionDef):
            if (
                statement.decorator_list
                or _signature_bound_names(statement)
                or statement.returns
                or getattr(statement, "type_params", ())
            ):
                return False
            if any(not isinstance(n, (ast.Nonlocal, ast.Assign, ast.Pass)) for n in statement.body):
                return False
            declared = {
                name
                for node in statement.body
                if isinstance(node, ast.Nonlocal)
                for name in node.names
            }
            if any(
                not all(
                    isinstance(target, ast.Name) and target.id in declared
                    for target in node.targets
                )
                for node in statement.body
                if isinstance(node, ast.Assign)
            ):
                return False
            helpers[statement.name] = statement
            values[statement.name] = statement
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            helper = helpers.get(call.func.id) if isinstance(call.func, ast.Name) else None
            if (
                helper is None
                or values.get(helper.name) is not helper
                or call.args
                or call.keywords
            ):
                return False
            if _starred_witness_block(helper.body, values, function, {}) is not True:
                return False
            continue
        return False
    return True


def _starred_witness_call_is_inert(call, function):
    if not _literal_subject_element_is_safe(call, function):
        return False
    module = _module_for_function(function)
    if module is None or _entered_builtin_module_is_mutated(module):
        return False
    if _entered_builtin_has_enclosing_binding(call, function):
        return False
    root = call.func
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name):
        return False
    for scope in ast.walk(module):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = _own_scope_bindings(scope)
        if any(isinstance(node, ast.Global) and root.id in node.names for node in nodes) and any(
            not isinstance(node, (ast.Global, ast.Nonlocal))
            and any(_names_bound_by_statement(node, root.id))
            for node in nodes
        ):
            return False
    return True
