# Shared-namespace fragment for reviewed sentinel helpers.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part17":
    raise ImportError(
        "tests._sentinel_support_part17 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


def _walrus_name_entry_is_dead(value, by_index, index, function, bound, module):
    """Is a walrus value that is a bare ``Name`` bound to something unenterable?

    #464. ``with (cs := helper):`` enters whatever ``helper`` holds. When that
    is a function object the header raises ``TypeError`` while it is being
    evaluated -- before the body is entered -- so the assert under it never
    runs. #390 fixed exactly this for the ``ast.Lambda`` spelling by reading
    the entered value's own runtime type, but a bare ``ast.Name`` declines in
    ``_literal_runtime_type`` and so kept the assert live. The entered object
    is the same function object either way; only the spelling differs.

    The name is resolved through the machinery this module already trusts for
    every other store -- ``_stores_of`` for the function scope and
    ``_module_stores`` for the module scope -- so a *local alias* and a *module constant* answer from a settled
    lexical binding. Unknown caller parameters and closure cells decline. Each is then read for its runtime kind, which
    is what decides entry.

    The conservative direction is preserved in two places, and both matter:

    * a name with no settled store here answers ``False``, keeping the assert
      live. ``_stores_of`` also returns ``None`` for a name bound only
      conditionally, and for one a later conditional store may have
      superseded; those are all genuinely undecidable and stay live.
    * a kind the interpreter cannot produce is refused by
      :func:`_runtime_kind_can_enter`, which also declines.

    Inverting either would trade this false-LIVE for false-DEADs, which is the
    more damaging direction under #308 criterion 1 -- an unreadable name is
    not evidence of an unenterable one.
    """
    if not isinstance(value, ast.Name):
        return False
    kind = _name_runtime_kind(value.id, by_index, index, function, module)
    if kind is None:
        # Unresolved, conditionally settled, or a spelling this cannot read.
        # Declining keeps the assert live, which is the safe direction.
        return False
    return kind in _PROBE_FOR_RUNTIME_KIND and not _runtime_kind_can_enter(kind)


def _name_runtime_kind(name, by_index, index, function, module, depth=0):
    """Resolve the nearest lexical binding; unknown locals never fall back."""
    if depth > 4 or _is_parameter_of(function, name):
        return None
    if name in by_index["bindings"]:
        return _name_kind_in_scope(name, by_index, index, function, depth)
    if module is None or _walrus_module_may_execute_early(module):
        return None
    # A closure cell shadows the module even if its value is unreadable here.
    parent = _nonlocal_parent_function(function, module)
    while parent is not None:
        if name in _signature_bound_names(parent) or any(
            any(_names_bound_by_statement(statement, name))
            for statement in _own_scope_bindings(parent)
        ):
            return None
        parent = _nonlocal_parent_function(parent, module)
    bindings, _raw = _store_bindings(module, _bound_names(module))
    module_index = {
        "bindings": bindings,
        "orders": {
            id(entry[0]): _binding_order(module, entry[0])
            for entries in bindings.values()
            for entry in entries
        },
    }
    return _name_kind_in_scope(name, module_index, len(module.body), module, depth)


def _name_kind_in_scope(name, by_index, index, scope, depth):
    """Follow a settled alias at its assignment time, rather than header time."""
    if depth > 4:
        return None
    stores = _stores_of(name, by_index, index, scope)
    kind = _entry_kind_of_stores(stores)
    if kind is not None:
        return kind
    if not stores or len(stores) != 1:
        return None
    statement, value, conditional = stores[0]
    if conditional or not isinstance(value, ast.Name):
        return None
    before = by_index["orders"][id(statement)] - 1
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _name_runtime_kind(
            value.id, by_index, before, scope, _module_for_function(scope), depth + 1
        )
    return _name_kind_in_scope(value.id, by_index, before, scope, depth + 1)


def _settled_stores_of(name, by_index, index, function):
    """The settled store entries binding ``name``, possibly empty."""
    stores = _stores_of(name, by_index, index, function)
    if not stores:
        return []
    entries = [stores] if isinstance(stores[0], ast.AST) else list(stores)
    return [entry for entry in entries if isinstance(entry, tuple) and len(entry) == 3]


def _settled_module_stores_of(name, module):
    """The settled module-scope store entries binding ``name``."""
    stores = _module_stores(name, module)
    if not stores:
        return []
    entries = [stores] if isinstance(stores[0], ast.AST) else list(stores)
    return [entry for entry in entries if isinstance(entry, tuple) and len(entry) == 3]


def _is_parameter_of(function, name):
    """Is ``name`` one of ``function``'s own parameters?"""
    arguments = function.args
    candidates = [
        *getattr(arguments, "posonlyargs", []),
        *arguments.args,
        *arguments.kwonlyargs,
    ]
    if arguments.vararg is not None:
        candidates.append(arguments.vararg)
    if arguments.kwarg is not None:
        candidates.append(arguments.kwarg)
    return any(isinstance(candidate, ast.arg) and candidate.arg == name for candidate in candidates)


def _entry_kind_of_stores(stores):
    """The runtime kind the settled ``stores`` bind, or ``None`` to decline.

    ``_stores_of``/``_module_stores`` answer with ``(statement, value,
    conditional)`` entries whose value may be an expression, a recorded kind
    string, or a marker. Anything that is not a single settled entry with a
    readable runtime kind declines, which keeps the caller on the safe side.
    """
    if not stores:
        return None
    entries = [stores] if isinstance(stores[0], ast.AST) else list(stores)
    if len(entries) != 1:
        return None
    entry = entries[0]
    if not (isinstance(entry, tuple) and len(entry) == 3):
        return None
    statement, value, conditional = entry
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.decorator_list:
        return None
    if conditional or value is _NOT_A_SUPPRESSOR or value is _RAISING_CONSTRUCTOR:
        return None
    if isinstance(value, str):
        # A carrier: `_store_bindings` records the kind directly.
        return value
    if not isinstance(value, (ast.Constant, ast.Lambda, ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return None
    return _literal_runtime_type(value)


def _walrus_module_may_execute_early(module):
    """Require inert module setup before trusting a module-end snapshot."""
    function_defined = False
    for statement in module.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            if function_defined or isinstance(statement, ast.ImportFrom) and statement.level:
                return True
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_defined = True
            arguments = [
                *statement.args.posonlyargs,
                *statement.args.args,
                *statement.args.kwonlyargs,
            ]
            arguments.extend(arg for arg in (statement.args.vararg, statement.args.kwarg) if arg)
            if (
                statement.decorator_list
                or statement.returns is not None
                or any(arg.annotation is not None for arg in arguments)
                or getattr(statement, "type_params", ())
                or any(not isinstance(value, ast.Constant) for value in statement.args.defaults)
                or any(
                    value is not None and not isinstance(value, ast.Constant)
                    for value in statement.args.kw_defaults
                )
            ):
                return True
            continue
        if isinstance(statement, ast.Assign):
            if not all(isinstance(target, ast.Name) for target in statement.targets):
                return True
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            if not isinstance(statement.annotation, (ast.Name, ast.Constant)):
                return True
            value = statement.value
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
        else:
            return True
        if value is not None and not _walrus_inert_module_value(value, module):
            return True
    return False


def _walrus_inert_module_value(value, module):
    """Allow references/literals and the validated zero-argument constructor."""
    if isinstance(value, (ast.Name, ast.Constant)):
        return True
    if isinstance(value, (ast.List, ast.Tuple)):
        return all(_walrus_inert_module_value(element, module) for element in value.elts)
    return isinstance(value, ast.Call) and _walrus_inert_module_constructor(value, module)


def _walrus_inert_module_constructor(call, module):
    """Allow only an untouched absolute contextlib nullcontext construction."""
    callee = call.func
    if not (
        isinstance(callee, ast.Attribute)
        and isinstance(callee.value, ast.Name)
        and callee.attr == "nullcontext"
        and not call.args
        and not call.keywords
    ):
        return False
    name = callee.value.id
    imports = [
        node
        for node in module.body
        if isinstance(node, ast.Import)
        and any(
            alias.name == "contextlib" and (alias.asname or alias.name) == name
            for alias in node.names
        )
    ]
    if len(imports) != 1:
        return False
    for statement in module.body:
        if statement is imports[0]:
            continue
        if any(_names_bound_by_statement(statement, name)):
            return False
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes = [statement.args, *statement.decorator_list]
            if statement.name == name:
                return False
        else:
            nodes = [statement]
        for root in nodes:
            for node in ast.walk(root):
                if (
                    isinstance(node, ast.Name)
                    and node.id == name
                    and isinstance(node.ctx, (ast.Store, ast.Del))
                ):
                    return False
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    return False
    return imports[0].lineno < call.lineno
