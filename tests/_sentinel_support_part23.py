# Shared-namespace fragment; import the public support entry point.
# ruff: noqa: F821
if __name__ == "tests._sentinel_support_part23":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _source_known_manager_value(value, module, function):
    """Canonical constructor, or a zero-arg source helper returning only one."""
    if not isinstance(value, ast.Call) or value.keywords:
        return None
    bound = _bound_names(module, function)
    if any(
        _resolves_to(value.func, path, bound)
        for path in ("contextlib.suppress", "contextlib.nullcontext")
    ):
        root = value.func.value if isinstance(value.func, ast.Attribute) else value.func
        if not isinstance(root, ast.Name) or not _literal_subject_callee_is_intact(
            root, function, value
        ):
            return None
        if _resolves_to(value.func, "contextlib.nullcontext", bound):
            return value if not value.args else None
        return (
            value
            if not value.args or _carried_suppressor_has_unshadowed_arguments(value, function)
            else None
        )
    if value.args or not isinstance(value.func, ast.Name):
        return None
    for scope in reversed(_class_lookup_scopes(module, function)):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            value.func.id in _signature_bound_names(scope)
            or any(
                any(_names_bound_by_statement(statement, value.func.id)) for statement in scope.body
            )
        ):
            return None
    candidates = [
        statement
        for statement in module.body
        if any(_names_bound_by_statement(statement, value.func.id))
    ]
    if not _contains(function, value):
        candidates = [
            statement
            for statement in candidates
            if (statement.lineno, statement.col_offset) < (value.lineno, value.col_offset)
        ]
    if not candidates or not isinstance(candidates[-1], ast.FunctionDef):
        return None
    helper = candidates[-1]
    if _signature_bound_names(helper) or helper.decorator_list or helper.returns is not None:
        return None
    if len(helper.body) != 1 or not isinstance(helper.body[0], ast.Return):
        return None
    _remember_module_for_function(helper, module)
    returned = helper.body[0].value
    if not isinstance(returned, ast.Call) or not isinstance(returned.func, ast.Attribute):
        return None
    return _source_known_manager_value(returned, module, helper)


def _global_module_capture_defeats_assertion(header, function, module, target):
    """Read a plain module's global capture after complete inert initialization.

    Only an unchanged ordinary parameter or literal-false assertion is modeled.
    Opaque module execution, unknown constructors and function-local bindings
    decline. This does not interpret arbitrary imported callbacks or external
    mutations of genuine contextlib implementations.
    """
    if not (
        isinstance(module, ast.Module)
        and isinstance(function, ast.FunctionDef)
        and isinstance(target, ast.Assert)
        and isinstance(header, ast.With)
    ):
        return False
    # Module initialization alone does not establish the effects of enclosing
    # callers before a nested function reads a global.
    if function not in module.body:
        return False
    if target.msg is not None and not isinstance(target.msg, ast.Constant):
        return False
    if len(header.items) != 1 or not isinstance(header.items[0].context_expr, ast.Name):
        return False
    name = header.items[0].context_expr.id
    if name in _signature_bound_names(function):
        return False
    if any(any(_names_bound_by_statement(statement, name)) for statement in function.body):
        return False
    if sum(isinstance(statement, ast.With) for statement in function.body) != 1:
        return False
    if any(
        not isinstance(statement, (ast.With, ast.Pass, ast.Global)) for statement in function.body
    ):
        return False
    if (
        any(not isinstance(statement, ast.Assert) for statement in header.body)
        or len(header.body) != 1
    ):
        return False
    if any(item.optional_vars is not None for item in header.items):
        return False
    ordinary = {
        argument.arg
        for argument in function.args.posonlyargs + function.args.args + function.args.kwonlyargs
    }
    test = target.test
    witness = (isinstance(test, ast.Constant) and not bool(test.value)) or (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id in ordinary
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.NotEq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
    )
    if not witness or not _nonlocal_capture_setup_is_inert(module):
        return False
    selected = None
    for statement in module.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(statement, ast.Assign) and all(
            isinstance(item, ast.Name) for item in statement.targets
        ):
            value = _source_known_manager_value(statement.value, module, function)
            if value is None and not isinstance(statement.value, ast.Constant):
                return False
            if any(item.id == name for item in statement.targets):
                selected = value
            continue
        if not isinstance(statement, ast.ClassDef):
            return False
        if any(not isinstance(item, (ast.Global, ast.Match, ast.Pass)) for item in statement.body):
            return False
        declared = {
            item
            for declaration in statement.body
            if isinstance(declaration, ast.Global)
            for item in declaration.names
        }
        if declared != {name}:
            return False
        matches = [item for item in statement.body if isinstance(item, ast.Match)]
        if len(matches) != 1:
            return False
        match = matches[0]
        if not isinstance(match.subject, (ast.List, ast.Tuple)) or len(match.cases) != 1:
            return False
        case = match.cases[0]
        if case.guard is not None or any(not isinstance(item, ast.Pass) for item in case.body):
            return False
        if not isinstance(case.pattern, ast.MatchSequence) or any(
            isinstance(item, ast.MatchStar) for item in case.pattern.patterns
        ):
            return False
        elements = []
        for item in match.subject.elts:
            value = _source_known_manager_value(item, module, function)
            if value is None and not isinstance(item, ast.Constant):
                return False
            elements.append(item if value is None else value)
        if len(elements) != len(case.pattern.patterns):
            continue
        decisions = [
            _sequence_element_matches(pattern, element)
            for pattern, element in zip(case.pattern.patterns, elements)
        ]
        if any(decision is None for decision in decisions):
            return False
        if not all(decisions):
            continue
        for pattern, element in zip(case.pattern.patterns, elements):
            if (
                isinstance(pattern, ast.MatchAs)
                and pattern.pattern is None
                and pattern.name == name
                and name in declared
            ):
                selected = element
    if not isinstance(selected, ast.Call) or not selected.args:
        return False
    bound = _bound_names(module, function)
    return _is_readable_suppressor(selected, bound) and any(
        _name_catches_assertion_error(item)
        for item in _suppression_names(selected, bound, function)
    )


def _global_function_capture_effects(function, query=None):
    """Global captures read by this function, excluding its own local names."""
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or query is None:
        return []
    headers = [
        node
        for node in _scope_body_nodes(function)
        if isinstance(node, ast.With) and _contains(node, query)
    ]
    if len(headers) != 1 or len(headers[0].items) != 1:
        return []
    expression = headers[0].items[0].context_expr
    if not isinstance(expression, ast.Name):
        return []
    name = expression.id
    declared = {
        item
        for statement in function.body
        if isinstance(statement, ast.Global)
        for item in statement.names
    }
    if name in _signature_bound_names(function):
        return []
    if name not in declared and name in _raw_store_values(function):
        return []
    module = _module_for_function(function)
    if module is None:
        return []
    if name not in declared:
        for scope in _class_lookup_scopes(module, function):
            if (
                isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
                and scope is not function
                and (name in _signature_bound_names(scope) or name in _raw_store_values(scope))
            ):
                return []
    module = _module_for_function(function)
    if module is None:
        return []
    for statement in module.body:
        if isinstance(
            statement, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.Pass)
        ):
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if (
            isinstance(statement, ast.Assign)
            and all(isinstance(target, ast.Name) for target in statement.targets)
            and (
                isinstance(statement.value, ast.Constant)
                or _source_known_manager_value(statement.value, module, function) is not None
            )
        ):
            continue
        return []
    return _nonlocal_match_captures(function, query, {name})
