# Shared-namespace fragment for primitive alias failure witnesses.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part24":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")

_ALIAS_WITNESS_UNKNOWN = object()


def _primitive_alias_failure_path(function, query, owning, *, structured=False, defeated=False):
    """A manager reached by an executed primitive failure path, or no proof.

    #366: an ordinary boolean parameter can leave a previous nullcontext in
    force while a later conditional suppressor store does not execute. Prove
    an EXISTING live path, never infer defeat from a finite parameter sweep.
    Only direct module functions, inert creation/setup, literal/alias stores,
    simple primitive If conditions and one final assertion are modeled.
    Canonical contextlib protocol code is assumed stable during execution.
    """
    if (
        not isinstance(function, ast.FunctionDef)
        or not isinstance(query, ast.Assert)
        or owning is None
        or function not in owning.body
    ):
        return None
    header = function.body[-1]
    if (
        not isinstance(header, ast.With)
        or len(header.items) != 1
        or header.body != [query]
        or header.items[0].optional_vars is not None
        or (query.msg is not None and not isinstance(query.msg, ast.Constant))
    ):
        return None
    expression = header.items[0].context_expr
    destination = None
    if isinstance(expression, ast.NamedExpr) and isinstance(expression.target, ast.Name):
        destination = expression.target.id
        expression = expression.value
    if not isinstance(expression, ast.Name) or not _primitive_alias_setup_is_inert(owning):
        return None
    prefix = [
        statement
        for statement in function.body[:-1]
        if not isinstance(statement, (ast.Import, ast.Pass))
    ]
    if structured:
        if not any(isinstance(statement, (ast.If, ast.For, ast.Try)) for statement in prefix):
            return None
    elif (
        not prefix
        or not isinstance(prefix[0], ast.Assign)
        or not isinstance(prefix[0].value, ast.Call)
        or not _literal_subject_element_is_safe(prefix[0].value, function)
    ):
        return None
    ordinary = {
        a.arg for a in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    }
    if "AssertionError" in ordinary:
        return None
    base = {}
    test = query.test
    if (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id in ordinary
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.NotEq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
    ):
        base[test.left.id] = test.comparators[0].value
    loaded = {
        n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    if defeated and ordinary & loaded:
        return None
    free = sorted((ordinary & loaded) - base.keys())
    if len(free) > 2:
        return None
    bound = _bound_names(owning, function)
    if structured and _structured_alias_retains_ambiguous_suppressors(function, bound):
        return None
    for values in _primitive_alias_environments(free, base):
        block = _structured_alias_block if structured else _primitive_alias_block
        if not block(function.body[:-1], values, function):
            continue
        manager = values.get(expression.id, _ALIAS_WITNESS_UNKNOWN)
        if not isinstance(manager, ast.Call) or not _literal_subject_element_is_safe(
            manager, function
        ):
            continue
        if destination is not None:
            values[destination] = manager
        if _primitive_alias_condition(query.test, values, function) is not False:
            continue
        if _resolves_to(manager.func, "contextlib.nullcontext", bound):
            if not defeated:
                return True
            continue
        if _is_readable_suppressor(manager, bound):
            suppresses = any(
                _name_catches_assertion_error(name)
                for name in _suppression_names(manager, bound, function)
            )
            if suppresses is defeated:
                return True
    return None


def _primitive_alias_setup_is_inert(module):
    for statement in module.body:
        if isinstance(statement, ast.Import) and all(
            a.name == "contextlib" for a in statement.names
        ):
            continue
        if isinstance(statement, ast.FunctionDef):
            args = statement.args
            annotated = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            if (
                statement.decorator_list
                or statement.returns
                or getattr(statement, "type_params", [])
                or any(a is not None and a.annotation is not None for a in annotated)
                or any(
                    not isinstance(v, ast.Constant)
                    for v in [*args.defaults, *args.kw_defaults]
                    if v is not None
                )
            ):
                return False
            continue
        if isinstance(statement, ast.Pass) or (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        return False
    return True


def _primitive_alias_environments(names, base):
    if not names:
        yield dict(base)
        return
    for value in (False, True):
        for environment in _primitive_alias_environments(names[1:], base):
            environment[names[0]] = value
            yield environment


def _primitive_alias_value(node, values, function):
    if isinstance(node, ast.Name):
        return values.get(node.id, _ALIAS_WITNESS_UNKNOWN)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Call) and _literal_subject_element_is_safe(node, function):
        return node
    return _ALIAS_WITNESS_UNKNOWN


def _primitive_alias_condition(node, values, function):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = _primitive_alias_condition(node.operand, values, function)
        return None if value is None else not value
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = _primitive_alias_value(node.left, values, function)
        right = _primitive_alias_value(node.comparators[0], values, function)
        if (
            left is _ALIAS_WITNESS_UNKNOWN
            or right is _ALIAS_WITNESS_UNKNOWN
            or isinstance(left, ast.AST)
            or isinstance(right, ast.AST)
        ):
            return None
        if isinstance(node.ops[0], ast.Eq):
            return left == right
        if isinstance(node.ops[0], ast.NotEq):
            return left != right
        return None
    value = _primitive_alias_value(node, values, function)
    if value is _ALIAS_WITNESS_UNKNOWN or isinstance(value, ast.AST):
        return None
    return bool(value)


def _primitive_alias_block(statements, values, function):
    for statement in statements:
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Import) and all(
            a.name == "contextlib" for a in statement.names
        ):
            continue
        if isinstance(statement, ast.Assign) and all(
            isinstance(t, ast.Name) for t in statement.targets
        ):
            value = _primitive_alias_value(statement.value, values, function)
            if value is _ALIAS_WITNESS_UNKNOWN:
                return False
            values.update({t.id: value for t in statement.targets})
            continue
        if isinstance(statement, ast.If):
            choice = _primitive_alias_condition(statement.test, values, function)
            if choice is None or not _primitive_alias_block(
                statement.body if choice else statement.orelse, values, function
            ):
                return False
            continue
        return False
    return True
