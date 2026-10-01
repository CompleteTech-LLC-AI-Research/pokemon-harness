# Shared-namespace fragment for source-known with-target unpacking.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part27":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")


def _known_with_target_cannot_unpack(function, query, owning):
    """#431: a known entry value cannot bind the target before the body starts.

    Only direct headers after inert setup are proved. Unknown constructors,
    callbacks and module definition metadata decline. Contextlib protocol code
    is assumed stable during modeled execution, as in the existing witnesses.
    """
    if any(
        isinstance(header, ast.With)
        and _in_body(header, query)
        and any(isinstance(item.context_expr, (ast.Tuple, ast.List)) for item in header.items)
        for header in _ancestors(function, query)
    ):
        return True
    if owning is None or not _primitive_alias_setup_is_inert(owning):
        return False
    bound = _bound_names(owning, function)
    for header in function.body:
        if (
            not isinstance(header, ast.With)
            or len(header.items) != 1
            or not _in_body(header, query)
        ):
            continue
        prefix = function.body[: function.body.index(header)]
        if any(
            not (
                isinstance(statement, ast.Pass)
                or (
                    isinstance(statement, ast.Import)
                    and all(alias.name == "contextlib" for alias in statement.names)
                )
            )
            for statement in prefix
        ):
            return False
        for item in header.items:
            target = item.optional_vars
            if not isinstance(target, (ast.Tuple, ast.List)):
                continue
            call = item.context_expr
            if not isinstance(call, ast.Call) or call.keywords:
                continue
            root = call.func
            while isinstance(root, ast.Attribute):
                root = root.value
            if not isinstance(root, ast.Name) or not _literal_subject_callee_is_intact(
                root, function, call
            ):
                continue
            if _resolves_to(call.func, "contextlib.suppress", bound):
                if not _literal_subject_element_is_safe(call, function):
                    continue
                value = None
            elif _resolves_to(call.func, "contextlib.nullcontext", bound):
                identity_call = ast.copy_location(
                    ast.Call(func=call.func, args=[], keywords=[]), call
                )
                if not _literal_subject_element_is_safe(identity_call, function):
                    continue
                if len(call.args) > 1:
                    continue
                if call.args:
                    try:
                        value = ast.literal_eval(call.args[0])
                    except (ValueError, TypeError):
                        continue
                else:
                    value = None
            else:
                continue
            if _known_target_unpack_fails(target, value):
                return True
    return False


def _known_target_unpack_fails(target, value):
    if isinstance(target, (ast.Name, ast.Starred)):
        return False
    if not isinstance(target, (ast.Tuple, ast.List)):
        return False
    if type(value) not in (tuple, list, str, bytes, dict, set):
        return True
    # Literal dict/set iteration ordering is immaterial only when no nested
    # target needs a particular element; decline those rather than guess.
    if type(value) in (dict, set):
        return False
    values = list(value)
    stars = [i for i, element in enumerate(target.elts) if isinstance(element, ast.Starred)]
    if not stars:
        if len(values) != len(target.elts):
            return True
        return any(_known_target_unpack_fails(t, v) for t, v in zip(target.elts, values))
    if len(values) < len(target.elts) - 1:
        return True
    star = stars[0]
    tail = len(target.elts) - star - 1
    pairs = list(zip(target.elts[:star], values[:star]))
    if tail:
        pairs.extend(zip(target.elts[-tail:], values[-tail:]))
    return any(_known_target_unpack_fails(t, v) for t, v in pairs)
