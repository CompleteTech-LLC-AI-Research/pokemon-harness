# Shared-namespace fragment; imported through the public support entry point.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part16":
    raise ImportError(
        "tests._sentinel_support_part16 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


def _exit_return_is_known_truthy_for_failure(function, owner=None):
    """Prove #338's straight return for the actual AssertionError type.

    Budget: only one unconditional return, actual protocol type parameters,
    immutable identity tests, a genuine builtin bool, or a singleton membership
    test against the genuine AssertionError. No branch, arbitrary expression,
    rebound argument, unknown signature or shadowed builtin is guessed. The new
    proof requires a plain module-owned class with no constructors, protocol
    writes, decorated members or opaque evaluated expressions in the supplied
    module, exactly one assertion and only owned context-manager entries.
    """
    if (
        function.decorator_list
        or owner is not None
        and (owner.decorator_list or owner.keywords or owner.bases)
    ):
        return False
    if _module_for_function(function) is None:
        return False
    module = _module_for_function(function)
    if owner is not None:
        if sum(isinstance(node, ast.Assert) for node in ast.walk(module)) != 1:
            return False

        def inert_setup_value(value):
            return isinstance(value, (ast.Constant, ast.Name)) or (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == owner.name
                and not value.args
                and not value.keywords
            )

        for statement in module.body:
            if statement is owner or isinstance(statement, (ast.FunctionDef, ast.Pass)):
                continue
            if (
                isinstance(statement, ast.Assign)
                and all(isinstance(target, ast.Name) for target in statement.targets)
                and inert_setup_value(statement.value)
            ):
                continue
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                continue
            return False
        if (
            sum(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "__exit__"
                for node in owner.body
            )
            != 1
        ):
            return False
        if (
            owner not in module.body
            or sum(
                isinstance(node, ast.ClassDef) and node.name == owner.name for node in module.body
            )
            != 1
        ):
            return False
        for member in owner.body:
            if isinstance(member, ast.FunctionDef):
                if member.decorator_list:
                    return False
                if member.name == "__enter__" and not (
                    len(member.body) == 1
                    and isinstance(member.body[0], ast.Return)
                    and isinstance(member.body[0].value, ast.Name)
                    and member.args.args
                    and member.body[0].value.id == member.args.args[0].arg
                ):
                    return False
            elif not (
                isinstance(member, ast.Pass)
                or isinstance(member, ast.Expr)
                and isinstance(member.value, ast.Constant)
                and isinstance(member.value.value, str)
            ):
                return False
        instances = {
            target.id
            for statement in module.body
            if isinstance(statement, ast.Assign)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == owner.name
            and not statement.value.args
            and not statement.value.keywords
            for target in statement.targets
            if isinstance(target, ast.Name)
        }
        instance_targets = {
            target
            for statement in module.body
            if isinstance(statement, ast.Assign)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == owner.name
            for target in statement.targets
            if isinstance(target, ast.Name)
        }
        exit_nodes = set(ast.walk(function))
        for node in ast.walk(module):
            if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                return False
            if isinstance(node, ast.Expr) and not (
                isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            ):
                return False
            if isinstance(node, ast.Assign) and not (
                all(isinstance(target, ast.Name) for target in node.targets)
                and inert_setup_value(node.value)
            ):
                return False
            if isinstance(node, ast.arg) and node.arg in instances:
                return False
            if (
                isinstance(node, ast.Name)
                and node.id in instances
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and node not in instance_targets
            ):
                return False
            if isinstance(node, ast.With) and any(
                not (
                    isinstance(item.context_expr, ast.Name)
                    and item.context_expr.id in instances
                    or isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Name)
                    and item.context_expr.func.id == owner.name
                    and not item.context_expr.args
                    and not item.context_expr.keywords
                )
                for item in node.items
            ):
                return False
            if isinstance(node, ast.arg) and node.arg == owner.name:
                return False
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
                node is not owner and node.name == owner.name
            ):
                return False
            if isinstance(node, ast.Attribute):
                return False
            if isinstance(node, ast.Subscript) and node not in exit_nodes:
                return False
            if isinstance(
                node,
                (
                    ast.If,
                    ast.IfExp,
                    ast.For,
                    ast.AsyncFor,
                    ast.While,
                    ast.Match,
                    ast.Try,
                    ast.TryStar,
                    ast.BoolOp,
                    ast.BinOp,
                    ast.UnaryOp,
                ),
            ):
                return False
            if isinstance(node, ast.NamedExpr):
                return False
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.decorator_list
                or node.returns is not None
                or any(
                    argument.annotation is not None
                    for argument in node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                )
                or node.args.vararg is not None
                and node.args.vararg.annotation is not None
                or node.args.kwarg is not None
                and node.args.kwarg.annotation is not None
                or any(
                    value is not None and not isinstance(value, ast.Constant)
                    for value in node.args.defaults + node.args.kw_defaults
                )
                or getattr(node, "type_params", ())
            ):
                return False
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                return False
            if (
                isinstance(node, ast.Name)
                and node.id == owner.name
                and isinstance(node.ctx, (ast.Store, ast.Del))
            ):
                return False
            if isinstance(node, ast.Call) and not (
                isinstance(node.func, ast.Name)
                and (
                    node.func.id == owner.name
                    and not node.args
                    and not node.keywords
                    or node.func.id == "bool"
                    and node in exit_nodes
                    and not _callee_is_shadowed(node.func, function, node)
                )
            ):
                return False
    if owner is not None and any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"__init__", "__new__"}
        for node in owner.body
    ):
        return False
    body = [node for node in function.body if not isinstance(node, ast.Pass)]
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    value = body[0].value
    arguments = function.args.posonlyargs + function.args.args
    if len(arguments) >= 2:
        name, variadic = arguments[1].arg, False
    elif len(arguments) == 1 and function.args.vararg is not None:
        name, variadic = function.args.vararg.arg, True
    else:
        return False

    def is_received_type(node):
        if variadic:
            return (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == name
                and isinstance(node.slice, ast.Constant)
                and type(node.slice.value) is int
                and node.slice.value == 0
            )
        return isinstance(node, ast.Name) and node.id == name

    if isinstance(value, ast.Call):
        return (
            isinstance(value.func, ast.Name)
            and value.func.id == "bool"
            and len(value.args) == 1
            and not value.keywords
            and is_received_type(value.args[0])
            and not _callee_is_shadowed(value.func, function, value)
        )
    if not (
        isinstance(value, ast.Compare)
        and len(value.ops) == len(value.comparators) == 1
        and is_received_type(value.left)
    ):
        return False
    other = value.comparators[0]
    if isinstance(value.ops[0], ast.IsNot):
        return isinstance(other, ast.Constant) and (other.value is None or other.value is False)
    if isinstance(value.ops[0], ast.In) and isinstance(other, ast.Tuple) and len(other.elts) == 1:
        member = other.elts[0]
        return (
            isinstance(member, ast.Name)
            and member.id == "AssertionError"
            and not _callee_is_shadowed(member, function, member)
        )
    return False
