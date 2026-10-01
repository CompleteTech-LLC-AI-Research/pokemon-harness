# Shared-namespace fragment for source-readable match store prefixes.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part25":
    raise ImportError(
        "tests._sentinel_support_part25 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


def _enclosing_match(chain, case):
    """The ``ast.Match`` that owns ``case`` within an ancestor ``chain``."""
    index = chain.index(case)
    for candidate in reversed(chain[:index]):
        if isinstance(candidate, ast.Match):
            return candidate
    return None


def _match_store_prefix_is_safe(function, match):
    """Prove the literal subject and carried managers survive setup unchanged.

    The scalar reader's skipped-With name check is not a purity proof. This
    companion permits only canonical managers with pass bodies and inert
    source setup. Unknown context callbacks/imports never establish a subject.
    """
    if (
        not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        or match not in function.body
    ):
        return False
    module = _module_for_function(function)
    if module is None:
        return False
    for statement in module.body:
        if isinstance(statement, ast.Import) and all(
            a.name == "contextlib" for a in statement.names
        ):
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
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
    values = {
        a.arg: ast.Name(id=a.arg, ctx=ast.Load()) for a in _all_args(function) if a is not None
    }
    for statement in function.body[: function.body.index(match)]:
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Import) and all(
            a.name == "contextlib" for a in statement.names
        ):
            continue
        if isinstance(statement, ast.Assign) and all(
            isinstance(t, ast.Name) for t in statement.targets
        ):
            value = _match_store_safe_value(statement.value, values, function)
            if value is UNREADABLE_VALUE:
                return False
            values.update({t.id: value for t in statement.targets})
            continue
        if isinstance(statement, ast.With) and all(isinstance(n, ast.Pass) for n in statement.body):
            for item in statement.items:
                if item.optional_vars is not None:
                    return False
                value = item.context_expr
                target = None
                if isinstance(value, ast.NamedExpr) and isinstance(value.target, ast.Name):
                    target = value.target.id
                    value = value.value
                value = _match_store_safe_value(value, values, function)
                if not isinstance(value, ast.Call) or not _literal_subject_element_is_safe(
                    value, function
                ):
                    return False
                if target is not None:
                    values[target] = value
            continue
        return False
    for case in match.cases:
        if case.guard is not None or any(
            isinstance(n, (ast.MatchClass, ast.Attribute)) for n in ast.walk(case.pattern)
        ):
            return False
        for statement in case.body:
            if isinstance(statement, ast.Pass):
                continue
            if not isinstance(statement, ast.Assign) or not all(
                isinstance(t, ast.Name) for t in statement.targets
            ):
                return False
            if _match_store_safe_value(statement.value, values, function) is UNREADABLE_VALUE:
                return False
    following = [
        n for n in function.body[function.body.index(match) + 1 :] if not isinstance(n, ast.Pass)
    ]
    if len(following) != 1 or not isinstance(following[0], ast.With):
        return False
    header = following[0]
    return (
        len(header.items) == 1
        and isinstance(header.items[0].context_expr, ast.Name)
        and header.items[0].optional_vars is None
        and all(isinstance(n, (ast.Pass, ast.Assert)) for n in header.body)
        and all(
            n.msg is None or isinstance(n.msg, ast.Constant)
            for n in header.body
            if isinstance(n, ast.Assert)
        )
    )


def _match_store_safe_value(value, values, function):
    if isinstance(value, ast.Name):
        return values.get(value.id, UNREADABLE_VALUE)
    if isinstance(value, ast.Constant):
        return value
    if isinstance(value, ast.Call) and _literal_subject_element_is_safe(value, function):
        return value
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)) and all(
        isinstance(v, ast.Constant) for v in value.elts
    ):
        return value
    if isinstance(value, ast.Dict) and all(
        isinstance(v, ast.Constant) for v in [*value.keys, *value.values]
    ):
        return value
    return UNREADABLE_VALUE
