# Shared-namespace fragment for source-known generator loop payloads.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part32":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _known_generator_swallows_query(function, query, module):
    """Read plain source-owned yields, never an arbitrary iterable's value.

    Only a generator with a straight-line body of genuine suppressor yields
    supplies this proof. Caller factories, conditional yields, protocol hooks,
    rebinding and evaluated function creation stay outside this budget.
    """
    if not isinstance(module, ast.Module) or function not in module.body:
        return False
    if not _generator_module_creation_is_inert(module):
        return False
    if _entered_builtin_module_is_mutated(module):
        return False
    loops = [n for n in _ancestors(function, query) if isinstance(n, (ast.For, ast.AsyncFor))]
    for loop in loops:
        if not _in_body(loop, query) or not isinstance(loop.target, ast.Name):
            continue
        call = loop.iter
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        if call.args or call.keywords:
            continue
        definitions = [
            n
            for n in [*module.body, *function.body]
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == call.func.id
        ]
        if len(definitions) != 1:
            continue
        gen = definitions[0]
        if isinstance(loop, ast.AsyncFor) != isinstance(gen, ast.AsyncFunctionDef):
            continue
        if gen in function.body and gen.lineno >= loop.lineno:
            continue
        if not _source_generator_definition_is_plain(gen):
            continue
        if not _generator_binding_is_stable(module, function, gen, loop):
            continue
        _remember_module_for_function(gen, module)
        all_yields = [
            s.value.value
            for s in gen.body
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Yield)
        ]
        first_return = next(
            (i for i, s in enumerate(gen.body) if isinstance(s, ast.Return)), len(gen.body)
        )
        yields = [
            s.value.value
            for s in gen.body[:first_return]
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Yield)
        ]
        dead = not yields
        if not all_yields:
            continue
        imports = _bound_names(module, gen)
        if not all(
            _literal_subject_element_is_safe(value, gen)
            and not _entered_builtin_has_enclosing_binding(value, gen)
            and (
                dead
                or (
                    _is_readable_suppressor(value, imports)
                    and bool(value.args)
                    and any(_name_catches_assertion_error(n) for n in _suppression_names(value))
                )
            )
            for value in (all_yields if dead else yields)
        ):
            continue
        headers = [n for n in _ancestors(function, query) if isinstance(n, ast.With)]
        if not headers:
            continue
        header = headers[-1]
        if not _in_body(loop, header) or len(header.items) != 1:
            continue
        item = header.items[0]
        if not isinstance(item.context_expr, ast.Name) or item.context_expr.id != loop.target.id:
            continue
        if item.optional_vars is not None:
            continue
        # A source-visible store in the loop can replace a yielded suppressor.
        if any(
            isinstance(n, ast.Name)
            and n.id == loop.target.id
            and isinstance(n.ctx, (ast.Store, ast.Del))
            for s in loop.body
            for n in ast.walk(s)
        ):
            continue
        if loop.body != [header] or header.body != [query]:
            continue
        if any(
            s is not loop and s is not gen and not isinstance(s, ast.Pass) for s in function.body
        ):
            continue
        allowed = {id(call), *(id(v) for v in all_yields)}
        if any(isinstance(n, (ast.Global, ast.Nonlocal, ast.ClassDef)) for n in ast.walk(module)):
            continue
        if any(isinstance(n, ast.Call) and id(n) not in allowed for n in ast.walk(module)):
            continue
        if any(
            isinstance(n, ast.arg)
            and n.arg in {gen.name, "AssertionError"}
            or isinstance(n, ast.Name)
            and n.id == "AssertionError"
            and isinstance(n.ctx, (ast.Store, ast.Del))
            for n in ast.walk(module)
        ):
            continue
        return True
    return False


def _generator_module_creation_is_inert(module):
    for statement in module.body:
        if isinstance(statement, ast.Import) and all(
            a.name == "contextlib" for a in statement.names
        ):
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = statement.args
            parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            if (
                statement.decorator_list
                or statement.returns
                or getattr(statement, "type_params", [])
                or any(a is not None and a.annotation is not None for a in parameters)
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


def _source_generator_definition_is_plain(gen):
    args = gen.args
    if (
        args.posonlyargs
        or args.args
        or args.kwonlyargs
        or args.vararg
        or args.kwarg
        or gen.decorator_list
        or gen.returns
        or getattr(gen, "type_params", [])
    ):
        return False
    for statement in gen.body:
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Return) and (
            statement.value is None
            or isinstance(statement.value, ast.Constant)
            and statement.value.value is None
        ):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Yield):
            continue
        return False
    return True


def _generator_binding_is_stable(module, function, gen, loop):
    # Resolve the callable's actual lexical name rather than a same-spelled def.
    if gen in module.body:
        scopes = [module, function]
    else:
        scopes = [function]
    for scope in scopes:
        for node in _scope_body_nodes(scope):
            if node is gen:
                continue
            if any(_names_bound_by_statement(node, gen.name)):
                return False
            if isinstance(node, ast.NamedExpr) and gen.name in _store_target_names([node.target]):
                return False
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
                targets = (
                    node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                )
                if gen.name in _store_target_names(targets):
                    return False
    return True
