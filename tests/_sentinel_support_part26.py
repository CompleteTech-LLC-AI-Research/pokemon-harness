# Shared-namespace fragment for evaluated definition-time alias stores.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part26":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _assigned_alias_store_is_in_scope(statement, function, name, owning=None):
    """Distinguish evaluated definition metadata from the nested body (#334).

    Readable inert defaults, decorators, bases and eager3.11-3.13 annotations
    execute in the containing scope. Future/3.14 annotations are deferred.
    A nested body keeps the declaration-aware
    boundary. This does not prove that an arbitrary callback was invoked.
    """
    if _is_after_control_transfer(function, statement):
        return False
    path = _alias_store_body_scopes(function, statement, ())
    if path is None:
        return False
    if path and path[0] is function:
        path = path[1:]
    if owning is not None and _alias_annotation_is_deferred(statement, owning):
        return False
    if not _alias_definition_metadata_is_safe(statement, function):
        return False
    if not path:
        return True
    owner = path[-1]
    if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_uncalled_nested_def(
        function, owner
    ):
        return False
    kind = (
        ast.Global
        if isinstance(function, ast.Module) or _declares(function, (name,), ast.Global)
        else ast.Nonlocal
    )
    if not _declares(owner, (name,), kind):
        return False
    if kind is ast.Nonlocal:
        for scope in reversed(path[:-1]):
            if isinstance(
                scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            ) and _alias_scope_binds_locally(scope, name):
                return False
    return True


def _alias_annotation_is_deferred(statement, module):
    deferred = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in module.body
    )
    if not deferred and sys.version_info < (3, 14):
        return False
    annotations = [
        node.annotation
        for node in ast.walk(module)
        if isinstance(node, ast.arg) and node.annotation is not None
    ]
    annotations.extend(
        node.returns
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None
    )
    return any(
        any(node is statement for node in ast.walk(annotation)) for annotation in annotations
    )


def _alias_scope_binds_locally(scope, name):
    if _declares(scope, (name,), ast.Global) or _declares(scope, (name,), ast.Nonlocal):
        return False
    args = scope.args
    if any(
        argument is not None and argument.arg == name
        for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg)
    ):
        return True
    for node in ast.walk(scope):
        path = _alias_store_body_scopes(scope, node, ())
        if path and path[0] is scope:
            path = path[1:]
        if not path and any(_names_bound_by_statement(node, name)):
            return True
    return False


def _alias_store_body_scopes(node, target, owners):
    """Nested scope bodies entered on the AST path to one exact store."""
    if node is target:
        return owners
    for field, value in ast.iter_fields(node):
        scoped = owners
        if field == "body" and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            scoped = (*owners, node)
        for child in value if isinstance(value, list) else (value,):
            if not isinstance(child, ast.AST):
                continue
            found = _alias_store_body_scopes(child, target, scoped)
            if found is not None:
                return found
    return None


def _alias_definition_metadata_is_safe(statement, function):
    for definition in ast.walk(function):
        if definition is function or not isinstance(
            definition, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        expressions = _alias_definition_expressions(definition)
        if not any(
            any(node is statement for node in ast.walk(expression)) for expression in expressions
        ):
            continue
        if not _alias_definition_prefix_is_safe(function):
            return False
        decorators = getattr(definition, "decorator_list", ())
        if getattr(definition, "type_params", ()):
            return False
        if any(
            not _alias_metadata_expression_is_inert(expression, function)
            for expression in expressions
            if expression not in decorators
        ):
            return False
        if any(
            not _alias_metadata_decorator_is_inert(expression, definition, function)
            for expression in decorators
        ):
            return False
        if isinstance(definition, ast.ClassDef):
            if definition.keywords or len(definition.bases) > 1:
                return False
            if any(
                not isinstance(node, ast.Pass)
                and not (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                )
                for node in definition.body
            ):
                return False
    return True


def _alias_definition_expressions(definition):
    expressions = list(getattr(definition, "decorator_list", ()))
    if isinstance(definition, ast.ClassDef):
        return expressions + definition.bases + [keyword.value for keyword in definition.keywords]
    args = definition.args
    expressions.extend(value for value in (*args.defaults, *args.kw_defaults) if value is not None)
    expressions.extend(
        argument.annotation
        for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg)
        if argument is not None and argument.annotation is not None
    )
    if getattr(definition, "returns", None) is not None:
        expressions.append(definition.returns)
    return expressions


def _alias_metadata_expression_is_inert(node, function):
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Lambda):
        return all(
            _alias_metadata_expression_is_inert(value, function)
            for value in _alias_definition_expressions(node)
        )
    if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
        return _alias_metadata_expression_is_inert(node.value, function)
    if isinstance(node, ast.Call):
        return _literal_subject_element_is_safe(node, function)
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__class__"
        and isinstance(node.value, ast.NamedExpr)
        and isinstance(node.value.value, ast.Call)
    ):
        bound = _bound_names(_module_for_function(function), function)
        return _resolves_to(
            node.value.value.func, "contextlib.nullcontext", bound
        ) and _literal_subject_element_is_safe(node.value.value, function)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_alias_metadata_expression_is_inert(value, function) for value in node.elts)
    return False


def _alias_metadata_decorator_is_inert(expression, definition, function):
    if (
        isinstance(expression, ast.BoolOp)
        and isinstance(expression.op, ast.And)
        and len(expression.values) == 2
    ):
        first, expression = expression.values
        bound = _bound_names(_module_for_function(function), function)
        if (
            not isinstance(first, ast.NamedExpr)
            or not isinstance(first.value, ast.Call)
            or not _resolves_to(first.value.func, "contextlib.nullcontext", bound)
            or not _literal_subject_element_is_safe(first.value, function)
        ):
            return False
    if not isinstance(expression, ast.Name):
        return False
    definitions = [
        node
        for node in function.body
        if isinstance(node, ast.FunctionDef)
        and node.name == expression.id
        and node.lineno < definition.lineno
    ]
    if len(definitions) != 1:
        return False
    identity = definitions[0]
    args = identity.args
    arguments = [*args.posonlyargs, *args.args]
    if (
        identity.decorator_list
        or identity.returns
        or getattr(identity, "type_params", ())
        or len(arguments) != 1
        or args.vararg
        or args.kwarg
        or args.kwonlyargs
        or args.defaults
        or arguments[0].annotation
    ):
        return False
    if any(
        node is not identity and any(_names_bound_by_statement(node, expression.id))
        for node in _own_scope_bindings(function)
    ):
        return False
    if any(
        argument.arg == expression.id
        for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    ):
        return False
    return (
        len(identity.body) == 1
        and isinstance(identity.body[0], ast.Return)
        and isinstance(identity.body[0].value, ast.Name)
        and identity.body[0].value.id == arguments[0].arg
    )


def _alias_definition_prefix_is_safe(function):
    for statement in function.body[:-1]:
        if isinstance(statement, (ast.Pass, ast.Global, ast.Nonlocal)):
            continue
        if isinstance(statement, ast.Import) and all(
            alias.name == "contextlib" for alias in statement.names
        ):
            continue
        if (
            isinstance(statement, ast.ImportFrom)
            and statement.level == 0
            and statement.module == "contextlib"
            and all(alias.name in ("suppress", "nullcontext") for alias in statement.names)
        ):
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorators = statement.decorator_list
            if (
                getattr(statement, "type_params", ())
                or any(
                    not _alias_metadata_expression_is_inert(value, function)
                    for value in _alias_definition_expressions(statement)
                    if value not in decorators
                )
                or any(
                    not _alias_metadata_decorator_is_inert(value, statement, function)
                    for value in decorators
                )
            ):
                return False
            continue
        if (
            isinstance(statement, ast.Assign)
            and all(isinstance(target, ast.Name) for target in statement.targets)
            and _alias_metadata_expression_is_inert(statement.value, function)
        ):
            continue
        if (
            isinstance(statement, ast.With)
            and len(statement.items) == 1
            and statement.items[0].optional_vars is None
            and all(isinstance(node, ast.Pass) for node in statement.body)
            and _alias_metadata_expression_is_inert(statement.items[0].context_expr, function)
        ):
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and _alias_metadata_setup_call_is_inert(statement.value, function)
        ):
            continue
        if isinstance(statement, ast.ClassDef):
            if (
                statement.keywords
                or any(
                    not _alias_metadata_decorator_is_inert(value, statement, function)
                    for value in statement.decorator_list
                )
                or len(statement.bases) > 1
                or any(
                    not _alias_metadata_expression_is_inert(base, function)
                    for base in statement.bases
                )
                or any(not isinstance(node, ast.Pass) for node in statement.body)
            ):
                return False
            continue
        return False
    return True


def _alias_metadata_setup_call_is_inert(call, function):
    if not isinstance(call.func, ast.Name) or call.args or call.keywords:
        return False
    candidates = [
        node
        for node in function.body
        if isinstance(node, ast.FunctionDef)
        and node.name == call.func.id
        and node.lineno < call.lineno
    ]
    if len(candidates) != 1:
        return False
    definition = candidates[0]
    args = definition.args
    if (
        args.posonlyargs
        or args.args
        or args.kwonlyargs
        or args.vararg
        or args.kwarg
        or definition.decorator_list
        or definition.returns
        or getattr(definition, "type_params", ())
    ):
        return False
    if any(
        node is not definition and any(_names_bound_by_statement(node, call.func.id))
        for node in _own_scope_bindings(function)
    ):
        return False
    if any(
        argument.arg == call.func.id
        for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    ):
        return False
    for statement in definition.body:
        if isinstance(statement, (ast.Pass, ast.Nonlocal)):
            continue
        if (
            isinstance(statement, ast.Assign)
            and all(isinstance(target, ast.Name) for target in statement.targets)
            and _alias_metadata_expression_is_inert(statement.value, function)
        ):
            continue
        if isinstance(statement, ast.Return) and (
            statement.value is None or isinstance(statement.value, ast.Constant)
        ):
            continue
        if (
            isinstance(statement, ast.FunctionDef)
            and not statement.decorator_list
            and not getattr(statement, "type_params", ())
            and all(
                _alias_metadata_expression_is_inert(value, function)
                for value in _alias_definition_expressions(statement)
            )
        ):
            continue
        return False
    return True
