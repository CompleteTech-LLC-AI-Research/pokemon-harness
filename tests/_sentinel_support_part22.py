# Shared-namespace fragment; import the public support entry point.
# ruff: noqa: F821
if __name__ == "tests._sentinel_support_part22":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _nonlocal_match_captures(function, query=None, global_names=None):
    """Reached plain nested bodies with proved capture values; opaque code declines.

    Function definitions alone do not execute their bodies. Only a direct zero-arg
    invocation in the owning function is read. Unknown callbacks, conditional
    execution and unproved subjects preserve the earlier binding rather than
    inventing a live value. This is a scoped proof, not general nonlocal evaluation.
    """
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    module = _module_for_function(function)
    if module is None or not _nonlocal_capture_setup_is_inert(module):
        return []
    captured = []
    for nested in function.body:
        if not isinstance(nested, (ast.ClassDef, ast.FunctionDef)):
            continue
        if nested.decorator_list or getattr(nested, "type_params", ()):
            continue
        owner = nested
        if isinstance(nested, ast.ClassDef):
            if nested.bases or nested.keywords:
                continue
        else:
            if _signature_bound_names(nested) or nested.returns is not None:
                continue
            calls = [
                statement
                for statement in function.body
                if isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
                and statement.value.func.id == nested.name
                and not statement.value.args
                and not statement.value.keywords
                and statement.lineno > nested.lineno
                and (query is None or statement.lineno < query.lineno)
            ]
            if len(calls) != 1:
                continue
            owner = calls[0]
            stores = [
                node
                for node in _own_scope_bindings(function)
                if any(_names_bound_by_statement(node, nested.name))
            ]
            if stores != [nested]:
                continue
        collectors = {
            argument.arg
            for argument in (function.args.vararg, function.args.kwarg)
            if argument is not None
        }
        if any(
            isinstance(node, ast.Name) and node.id in collectors
            for statement in function.body[function.body.index(owner) + 1 :]
            for node in ast.walk(statement)
        ):
            continue
        if any(
            isinstance(node, ast.Assert)
            and node.msg is not None
            and not isinstance(node.msg, ast.Constant)
            for statement in function.body[function.body.index(owner) + 1 :]
            for node in ast.walk(statement)
        ):
            continue
        declared = _nonlocal_declared(nested)
        declaration_type = ast.Nonlocal
        if global_names is not None:
            declaration_type = ast.Global
            declared = {
                name
                for statement in nested.body
                if isinstance(statement, ast.Global)
                for name in statement.names
            } & global_names
        if not declared or any(
            not isinstance(statement, (declaration_type, ast.Match, ast.Pass))
            for statement in nested.body
        ):
            continue
        matches = [statement for statement in nested.body if isinstance(statement, ast.Match)]
        if len(matches) != 1:
            continue
        match = matches[0]
        if len(match.cases) != 1 or match.cases[0].guard is not None:
            continue
        if any(not isinstance(statement, ast.Pass) for statement in match.cases[0].body):
            continue
        original_subject = match.subject
        if isinstance(original_subject, (ast.List, ast.Tuple)):
            replacements = [
                _source_known_manager_value(element, module, function) or element
                for element in original_subject.elts
            ]
            match.subject = ast.copy_location(
                type(original_subject)(elts=replacements, ctx=ast.Load()), original_subject
            )
        original_body = function.body
        # Preserve the definition's binding when checking callee and witness
        # shadowing. This placeholder models a store, not the runtime value.
        binding = ast.Assign(
            targets=[ast.Name(id=nested.name, ctx=ast.Store())], value=ast.Constant(None)
        )
        ast.copy_location(binding, nested)
        projected = [
            binding if statement is nested else statement
            for statement in original_body
            if global_names is None or not isinstance(statement, ast.Global)
        ]
        if owner is nested:
            projected.insert(projected.index(binding) + 1, match)
        else:
            projected[projected.index(owner)] = match
        had_module_managers = hasattr(function, "_allow_literal_module_managers")
        previous_module_managers = getattr(function, "_allow_literal_module_managers", None)
        if global_names is not None:
            function._allow_literal_module_managers = True
        function.body = projected
        try:
            values = {
                name: _literal_match_capture_value(function, match, name)
                for name in _match_capture_names_for(match)
                if name in declared
            }
        finally:
            function.body = original_body
            match.subject = original_subject
            if not had_module_managers:
                if hasattr(function, "_allow_literal_module_managers"):
                    del function._allow_literal_module_managers
            else:
                function._allow_literal_module_managers = previous_module_managers
        for name in _match_capture_names_for(match):
            if name not in declared:
                continue
            value = values.get(name)
            if value is not None:
                captured.append((name, owner, value))
    return captured


def _nonlocal_capture_setup_is_inert(module):
    """Reject opaque imports and definition-time callbacks before projecting scopes."""
    for node in ast.walk(module):
        if isinstance(node, ast.Import) and any(alias.name != "contextlib" for alias in node.names):
            return False
        if isinstance(node, ast.ImportFrom) and (node.level or node.module != "contextlib"):
            return False
        if isinstance(node, ast.ClassDef) and (
            node.bases or node.keywords or node.decorator_list or getattr(node, "type_params", ())
        ):
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.decorator_list
            or node.returns is not None
            or getattr(node, "type_params", ())
            or any(
                argument.annotation is not None
                for argument in _all_args(node)
                if argument is not None
            )
            or any(
                value is not None and not isinstance(value, ast.Constant)
                for value in node.args.defaults + node.args.kw_defaults
            )
        ):
            return False
    return True
