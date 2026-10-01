# Shared-namespace fragment for lexical builtin entry identities.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part28":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _entered_builtin_has_enclosing_binding(value, function):
    """An enclosing cell cannot be read as the ambient builtin of that name."""
    root = value.func if isinstance(value, ast.Call) else value
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name) or not isinstance(
        function, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return False
    nodes = _own_scope_bindings(function)
    if any(isinstance(n, ast.Global) and root.id in n.names for n in nodes):
        return False
    owning = _module_for_function(function)
    parent = _nonlocal_parent_function(function, owning)
    while parent is not None:
        if any(
            isinstance(n, ast.Global) and root.id in n.names for n in _own_scope_bindings(parent)
        ):
            return False
        if root.id in _signature_bound_names(parent) or any(
            any(_names_bound_by_statement(node, root.id)) for node in _own_scope_bindings(parent)
        ):
            return True
        parent = _nonlocal_parent_function(parent, owning)
    return False


def _entered_builtin_setup_is_uncertain(value, function):
    """Require source-inert setup before using a builtin object's fixed kind."""
    if not isinstance(value, (ast.Name, ast.Call)) or not isinstance(
        function, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return False
    module = _module_for_function(function)
    if (
        module is None
        or _walrus_module_may_execute_early(module)
        or _entered_builtin_module_is_mutated(module)
    ):
        return True
    root = value.func if isinstance(value, ast.Call) else value
    while isinstance(root, ast.Attribute):
        root = root.value
    if isinstance(root, ast.Name):
        for scope in ast.walk(module):
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nodes = _own_scope_bindings(scope)
                if any(isinstance(n, ast.Global) and root.id in n.names for n in nodes) and any(
                    not isinstance(n, (ast.Global, ast.Nonlocal))
                    and any(_names_bound_by_statement(n, root.id))
                    for n in nodes
                ):
                    return True
    child = function
    parent = _nonlocal_parent_function(child, module)
    while parent is not None:
        owner = next(
            (stmt for stmt in parent.body if any(n is child for n in ast.walk(stmt))), None
        )
        if owner is None:
            return True
        prefix = [
            stmt
            for stmt in parent.body
            if not isinstance(stmt, (ast.Global, ast.Nonlocal, ast.Pass))
            and not (
                isinstance(stmt, ast.Return)
                and (
                    isinstance(stmt.value, ast.Name)
                    and stmt.value.id == child.name
                    or isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.id == child.name
                    and not stmt.value.args
                    and not stmt.value.keywords
                )
            )
        ]
        if _walrus_module_may_execute_early(ast.Module(body=prefix, type_ignores=[])):
            return True
        child = parent
        parent = _nonlocal_parent_function(child, module)
    # Unknown imported initializers and earlier evaluated statements can change
    # the builtin namespace before the alias is assigned.
    if any(
        isinstance(node, ast.Import)
        and any(alias.name not in ("builtins", "contextlib") for alias in node.names)
        or isinstance(node, ast.ImportFrom)
        and node.module not in ("builtins", "contextlib")
        for node in ast.walk(module)
    ):
        return True
    holder = next(
        (statement for statement in function.body if any(n is value for n in ast.walk(statement))),
        None,
    )
    if holder is None:
        return True
    prefix = [
        statement
        for statement in function.body[: function.body.index(holder)]
        if not isinstance(statement, (ast.Global, ast.Nonlocal, ast.Pass))
    ]
    if _walrus_module_may_execute_early(ast.Module(body=prefix, type_ignores=[])):
        return True
    return any(
        isinstance(node, ast.Import)
        and any(alias.name not in ("builtins", "contextlib") for alias in node.names)
        or isinstance(node, ast.ImportFrom)
        and node.module not in ("builtins", "contextlib")
        for node in prefix
    )


def _entered_builtin_module_is_mutated(module):
    # An unresolved receiver may itself be the builtin namespace. Decline
    # source-visible writes instead of trusting an incomplete alias catalog.
    if any(
        isinstance(node, (ast.AugAssign, ast.Subscript))
        or isinstance(node, ast.Attribute)
        and node.attr == "__builtins__"
        or isinstance(node, (ast.Attribute, ast.Subscript))
        and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in ast.walk(module)
    ):
        return True
    if any(
        isinstance(node, ast.ImportFrom) and node.module == "builtins" for node in ast.walk(module)
    ):
        return True
    aliases = {
        alias.asname or alias.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "builtins"
    }
    changed = True
    mutators = {"setattr", "delattr", "__import__", "eval", "exec", "globals", "locals", "vars"}
    while changed:
        before = (len(aliases), len(mutators))
        for node in ast.walk(module):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                returned = {
                    name.id
                    for statement in _own_scope_bindings(node)
                    if isinstance(statement, ast.Return) and statement.value is not None
                    for name in ast.walk(statement.value)
                    if isinstance(name, ast.Name)
                }
                if returned & aliases:
                    aliases.add(node.name)
                if returned & mutators:
                    mutators.add(node.name)
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
                and node.value is not None
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {
                    n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)
                }
                loaded = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
                if loaded & aliases:
                    aliases.update(names)
                if loaded & mutators:
                    mutators.update(names)
        changed = before != (len(aliases), len(mutators))
    for node in ast.walk(module):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in (
                "update",
                "__setitem__",
                "__delitem__",
                "pop",
                "clear",
                "setdefault",
            ):
                return True
            if any(
                isinstance(n, ast.Name) and n.id in aliases
                for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
                for n in ast.walk(argument)
            ):
                return True
            callee = node.func
            while isinstance(callee, (ast.Attribute, ast.Subscript)):
                callee = callee.value
            if (
                isinstance(callee, ast.Name)
                and callee.id in aliases
                and (
                    not isinstance(node.func, ast.Attribute)
                    or node.func.attr not in _BUILTIN_CONSTRUCTOR_TYPES
                )
            ):
                return True
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            root = node.value
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if isinstance(root, ast.Name) and root.id in aliases:
                return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in mutators
        ):
            return True
    return False


_BUILTIN_CONSTRUCTOR_TYPES = {
    "int": "int",
    "float": "float",
    "complex": "complex",
    "str": "str",
    "bytes": "bytes",
    "bool": "bool",
    "list": "list",
    "tuple": "tuple",
    "set": "set",
    "frozenset": "frozenset",
    "dict": "dict",
    "bytearray": "bytearray",
    "range": "range",
    # #464. `object()` produces the most unenterable value in the table: a
    # plain instance with no `__enter__` at all. It is here so `m = object()`
    # reached through a walrus answers by the same probe path as `m = list()`.
    "object": "object",
}


_PROBE_FOR_RUNTIME_KIND = {
    "function": lambda: None,
    "module": types.ModuleType("probe"),
    "list": [],
    "tuple": (),
    "set": set(),
    "dict": {},
    "int": 0,
    "str": "",
    "float": 0.0,
    "bytes": b"",
    "NoneType": None,
    # The other zero-argument builtin constructors #464 reaches through a
    # walrus: `m = list()` enters a list, and so on. They were already keyed
    # for the literal forms; naming them again would let the two lists drift.
    "complex": 0j,
    "bool": False,
    "frozenset": frozenset(),
    "bytearray": bytearray(),
    "object": object(),
    # `m = len` binds a builtin *function*, not a class object. The kind is
    # whatever `type()` says, so the two function kinds are probed here rather
    # than collapsed into one hand-written label.
    "builtin_function_or_method": len,
    "ellipsis": Ellipsis,
    # #464. `m = int` binds a *bare builtin class object*. Entering one is
    # decided by its metaclass, and every bare builtin class has the builtin
    # `type` as its metaclass -- so `int` is the honest witness. This is a
    # distinct kind from `"type"`, which is what a class *written in the file*
    # records: that one may carry a metaclass defining `__enter__`, and is
    # left unprobed so it keeps the conservative verdict.
    "builtin_class": int,
}


def _entered_global_builtin_is_intact(value, function):
    root = value.func if isinstance(value, ast.Call) else value
    if not isinstance(root, ast.Name) or function is None:
        return False
    if not any(
        isinstance(node, ast.Global) and root.id in node.names
        for node in _own_scope_bindings(function)
    ):
        return False
    module = _module_for_function(function)
    return (
        module is not None
        and not _module_binds_name(function, root.id, value)
        and not any(
            not isinstance(node, (ast.Global, ast.Nonlocal))
            and any(_names_bound_by_statement(node, root.id))
            for node in ast.walk(module)
        )
    )
