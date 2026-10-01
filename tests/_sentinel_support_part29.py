# Shared-namespace fragment for structured primitive failure witnesses.
# ruff: noqa: F821

if __name__ == "tests._sentinel_support_part29":
    raise ImportError("Import tests._timed_menu_milestone_sentinel_support instead.")

_STRUCTURED_ALIAS_BREAK = object()


def _structured_alias_block(statements, values, function):
    """Execute a source-known inert path; unknown effects provide no witness.

    #361: a later alias snapshots the value after the selected compound arm,
    rather than the last store encountered by an AST walk. This is a positive
    failure witness only. A normal try path can reach else and finally when
    every executed operation is known not to raise or invoke caller code.
    """
    for statement in statements:
        if isinstance(statement, ast.Import) and any(
            alias.name != "contextlib" or alias.asname not in (None, "contextlib")
            for alias in statement.names
        ):
            return False
        if isinstance(statement, (ast.Pass, ast.Import, ast.Assign)):
            if not _primitive_alias_block([statement], values, function):
                return False
            continue
        if isinstance(statement, ast.If):
            choice = _primitive_alias_condition(statement.test, values, function)
            if choice is None:
                return False
            status = _structured_alias_block(
                statement.body if choice else statement.orelse, values, function
            )
            if status is not True:
                return status
            continue
        if isinstance(statement, ast.For):
            if (
                not isinstance(statement.target, ast.Name)
                or not isinstance(statement.iter, (ast.Tuple, ast.List))
                or len(statement.iter.elts) > 8
            ):
                return False
            elements = [
                _primitive_alias_value(element, values, function) for element in statement.iter.elts
            ]
            if any(value is _ALIAS_WITNESS_UNKNOWN for value in elements):
                return False
            for value in elements:
                values[statement.target.id] = value
                status = _structured_alias_block(statement.body, values, function)
                if status is _STRUCTURED_ALIAS_BREAK:
                    break
                if status is not True:
                    return False
            else:
                status = _structured_alias_block(statement.orelse, values, function)
                if status is not True:
                    return status
            continue
        if isinstance(statement, ast.Try):
            # No exception path is inferred: the normal path must be inert.
            status = _structured_alias_block(statement.body, values, function)
            if status is False:
                return False
            if status is True:
                status = _structured_alias_block(statement.orelse, values, function)
                if status is False:
                    return False
            final = _structured_alias_block(statement.finalbody, values, function)
            if final is not True:
                return final
            if status is not True:
                return status
            continue
        if isinstance(statement, ast.Break):
            return _STRUCTURED_ALIAS_BREAK
        return False
    return True


def _contains(statement, target):
    """True if ``target`` is ``statement`` or lies anywhere beneath it."""
    return statement is target or any(node is target for node in ast.walk(statement))


def _structured_alias_retains_ambiguous_suppressors(function, bound):
    """Keep the explicit #308 policy for competing suppressor-only bindings."""
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    if any(_resolves_to(call.func, "contextlib.nullcontext", bound) for call in calls):
        return False
    families = {
        frozenset(_suppression_names(call, bound, function))
        for call in calls
        if _is_readable_suppressor(call, bound)
    }
    return len(families) > 1
