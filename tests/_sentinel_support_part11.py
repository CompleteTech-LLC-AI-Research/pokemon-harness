# Shared-namespace fragment for written-out match subjects.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part11":
    raise ImportError(
        "tests._sentinel_support_part11 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


def _entry_may_be_an_unrun_capture(entry, function=None):
    """Is this entry a ``match`` capture that is not guaranteed to have bound?

    The narrow companion to :func:`_store_retires`, asked of a whole
    ``(statement, value, conditional)`` entry rather than of a statement and a
    name. It exists because the name is not carried on the entry itself, and
    the caller that has to decide whether a single competing store is ambiguous
    only has the entry.

    A ``match`` statement can capture several names, so the name is recovered
    by asking which of the captures it owns is the one this entry records --
    a capture that is guaranteed to bind for *some* name is still a capture
    that may not bind for the name in question, so every owned name is
    checked and any one of them being undecidable makes the entry so.

    ``function`` is forwarded so this asks :func:`_store_retires` the same
    question the recording walk asked, and gets the same answer. Delegating
    rather than re-deciding is the point: the two must agree, or a settled
    capture is still counted as a competing one and the name is read as
    ambiguous -- which is a different answer again, and a wrong one.
    """
    statement = entry[0]
    if not isinstance(statement, ast.Match):
        return False
    return not all(
        _store_retires(statement, name, function) for name in _match_capture_names_for(statement)
    )


def _resolve_simple_literal_subject(function, match):
    """Follow a plain ``name = [literal]`` binding to the container it built.

    #369. The subject is a name, but the name was bound a statement or two
    earlier by an assignment whose right-hand side *is* a container literal.
    Only that shape is followed. The three things that make it safe:

    * the binding is at the top level of ``function``'s own body, so it cannot
      be skipped by a branch or by a loop the way a nested store could;
    * it appears **before** the ``match`` in that body, so it is the value in
      effect when the subject is read;
    * every statement between the binding and the ``match`` is inert with
      respect to the subject -- it does not bind the name and cannot reach the
      container -- so mutation, aliases, rebinding, and opaque calls between
      construction and matching are still declined.

    The third condition was originally "immediately precedes, allowing only
    ``pass``". That is *sufficient* but not *necessary*, and it declined the
    exact shape #369 reports: there the carrier

        with (cs := contextlib.suppress(AssertionError)):
            pass

    sits between ``subject = [...]`` and the ``match``. Nothing in it mentions
    ``subject``, so the subject is just as written out as in the accepted
    shape, and CPython enters the clause -- yet the walk gave up and reported a
    live contract as defeated. Narrowing to adjacency fixed no unsoundness and
    cost a true positive, which is the trade this should never make.

    A conditional store, destructuring assignment, loop target, walrus,
    subscript, or parameter still yields ``None``, as does any intervening
    statement that binds the subject or that could reach the container.
    Returning ``None`` is always the safe answer: the pre-existing
    conservative verdict reports a possibly-live assert as defeated, whereas a
    wrong ``1`` here would retire a carried suppressor that is still in force.
    """
    if not isinstance(match.subject, ast.Name):
        return None
    subject_name = match.subject.id
    body = function.body if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
    if body is None:
        return None
    if subject_name in {argument.arg for argument in _all_args(function) if argument is not None}:
        # A parameter is bound by the caller; its value is not in this tree.
        return None
    try:
        match_index = body.index(match)
    except ValueError:
        return None
    for statement in reversed(body[:match_index]):
        # An intervening statement may be skipped only when it is inert here:
        # it must not bind the subject name, and it must not be able to reach
        # the container. A `with` whose own name is unrelated -- which is what
        # #369's own carrier looks like -- qualifies, so the binding is still
        # followed across it. Anything that could have rebound the name or
        # mutated the list in place ends the walk instead.
        if _statement_leaves_subject_alone(statement, subject_name):
            continue
        if not isinstance(statement, ast.Assign):
            return None
        if not _assign_targets_exactly(statement, subject_name):
            return None
        if len(statement.targets) != 1:
            return None
        value = statement.value
        # Only a container literal is followed. A name, a call or a
        # subscript is not a written-out subject, however plainly it is
        # written; `_written_out_subject_elements` re-checks the shape.
        if not isinstance(value, (ast.List, ast.Tuple)):
            return None
        if any(isinstance(element, ast.Starred) for element in value.elts):
            return None
        return value
    return None


def _all_args(function):
    args = function.args
    return [
        *args.posonlyargs,
        *args.args,
        *args.kwonlyargs,
        args.vararg,
        args.kwarg,
    ]


def _assign_targets_exactly(statement, name):
    return any(isinstance(target, ast.Name) and target.id == name for target in statement.targets)


#: The only statements accepted between a subject binding and the `match` that
#: reads it. Each is a statement that cannot bind a name and cannot reach an
#: existing object, so the container built a line earlier is provably still the
#: value in effect when the `match` runs.
#
#: `ast.Pass` is a no-op. A `with`/`async with` is admitted because entering a
#: context manager binds only the names in its `optional_vars`; when that does
#: not name the subject, the `__enter__` side effects are the caller's problem
#: and cannot be read from here, so the conservative reading is to admit it.
#: That is the one genuinely debatable entry, and it is admitted *only* because
#: the alternative -- declining #369's own reproduction -- reports a live
#: contract as defeated.
#
#: Deliberately absent: `if`/`while`/`for`/`try`/`with`-bodies containing a
#: conditional store, every `ast.Assign` (handled by the caller), augmented and
#: annotated assignment, `del`, and anything whose body is not walked here.
_INERT_INTERVENING_TYPES = (ast.Pass, ast.With, ast.AsyncWith)


def _statement_leaves_subject_alone(statement, subject_name):
    """Can ``statement`` sit between the subject's binding and the match?

    Returns ``True`` only for a statement that is provably irrelevant to the
    subject: it never binds ``subject_name`` anywhere beneath it, and it is one
    of the inert statement types. Anything unrecognised answers ``False``, so
    an unknown construct ends the walk rather than silently widening the
    proof.
    """
    if not isinstance(statement, _INERT_INTERVENING_TYPES):
        return False
    return not any(
        isinstance(node, ast.Name)
        and node.id == subject_name
        and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in ast.walk(statement)
    )


def _capture_is_decidable_from_a_literal_subject(function, match, name):
    """Does a written-out subject settle whether this capture ran? (#364, #369)

    The "a refutable pattern may not have been selected" rule answers a
    question about a *runtime* subject. When the subject is written out in the
    source, the answer is already in the tree:

        match [contextlib.nullcontext()]:
            case [cs]:       # a one-element sequence pattern matches this
                pass

    CPython enters that clause and binds ``cs`` to the element -- a real
    context manager. Measured on 3.12.14: ``[nullcontext()]`` and ``[1]`` both
    match and bind, while ``[]``, ``[1, 2]`` and a non-list subject do not, and
    on those the carried binding survives untouched.

    Without this, :func:`_capture_always_binds` answers "may not have run" for
    every sequence capture, the resolution falls back to the carried walrus
    suppressor, and a header CPython really enters is reported **defeated** --
    the damaging direction, and the one #308 exists to prevent.

    Deliberately narrow, and it fails *safe*:

    * Only the **first** clause is consulted, because a later clause is only
      reached when every earlier pattern failed. If the first clause is not
      this capture, the answer is ``None``.
    * Only an exactly-sized sequence pattern with **no** starred element is
      answered. ``case [cs]`` needs length 1 and ``case [cs, other]`` needs
      length 2, both decidable. ``case [cs, *rest]`` is open-ended, so it is
      refused rather than guessed at -- that is a different question.
    * Matching the outer length is not enough on its own. A sequence pattern
      selects only when *every* element matches, so each element is compared
      individually: a bare capture or wildcard matches anything, and a
      singleton patterns require identity, while constant value patterns use
      equality, including cross-type numeric equality. An element that needs a value this
      rule cannot read -- a call, a name, a class pattern -- refuses the whole
      clause.
    * A guard is still not a condition, for the reason the docstring above
      records: a guard cannot undo a binding that already happened.

    ``True`` and ``False`` are the two *decided* answers. ``None`` is not a
    third opinion: it means the question was not asked, and the caller keeps
    the pre-existing conservative verdict. That is the safe direction, since
    it reports a possibly-live assert as defeated rather than retiring a
    suppressor that is genuinely still bound.
    """
    if not match.cases or not _literal_match_reaches_header(function, match):
        return None
    first = match.cases[0]
    # Capturing an element does not prove the following header is reached,
    # or that the case body leaves the capture unchanged.
    if any(not isinstance(statement, ast.Pass) for statement in first.body):
        return None
    if first.guard is not None:
        if not isinstance(first.guard, ast.Constant):
            return None
        if not first.guard.value and len(match.cases) > 1:
            return None
    if not isinstance(first.pattern, ast.MatchSequence):
        return None
    patterns = first.pattern.patterns
    if any(isinstance(pattern, ast.MatchStar) for pattern in patterns):
        return None
    if not any(_pattern_binds(pattern, name) for pattern in patterns):
        return None
    elements = _written_out_subject_elements(function, match)
    if elements is None or not all(
        _literal_subject_element_is_safe(element, function) for element in elements
    ):
        return None
    if len(elements) != len(patterns):
        return False
    for pattern, element in zip(patterns, elements):
        if _sequence_element_matches(pattern, element) is not True:
            return None
    return True


def _written_out_subject_elements(function, match):
    """The subject's own element expressions, or ``None`` if not written out.

    Reads the *outer* container only and deliberately does not try to evaluate
    the elements. ``[contextlib.nullcontext()]`` yields one element node, and
    that is all the length and the position of a capture both need; the
    element's *value* is a separate question that
    :func:`_sequence_element_matches` answers, and answers conservatively.
    """
    for node in (match.subject, *_assigned_literal_subject_value(function, match)):
        if isinstance(node, (ast.List, ast.Tuple)) and not any(
            isinstance(element, ast.Starred) for element in node.elts
        ):
            return list(node.elts)
    return None


def _assigned_literal_subject_value(function, match):
    """The right-hand side of a simple ``name = [literal]`` before ``match``."""
    resolved = _resolve_simple_literal_subject(function, match)
    if resolved is None:
        return ()
    return (resolved,)


def _sequence_element_matches(pattern, element):
    """Does ``pattern`` select ``element``? ``True``, ``False`` or ``None``.

    ``None`` means "not decidable from the two nodes", which is not the same
    as "does not match": the caller refuses the clause in both cases, so the
    distinction is kept only so a future reader can tell a refusal from a
    decision.
    """
    if isinstance(pattern, ast.MatchAs) and pattern.pattern is None:
        # `case [cs]` -- a bare capture, and `case [_, cs]` -- a wildcard. Both
        # accept any element, and a capture accepts it without reading it.
        return True
    if isinstance(pattern, ast.MatchSingleton):
        return element.value is pattern.value if isinstance(element, ast.Constant) else None
    if isinstance(pattern, ast.MatchValue) and isinstance(pattern.value, ast.Constant):
        if not isinstance(element, ast.Constant):
            return None
        # Value patterns use equality; True and 1.0 both match literal 1.
        return bool(pattern.value.value == element.value)
    return None


def _literal_match_capture_value(function, match, name):
    """Read the element bound by a decided bare sequence capture."""
    if _capture_is_decidable_from_a_literal_subject(function, match, name) is not True:
        return None
    elements = _written_out_subject_elements(function, match)
    for pattern, element in zip(match.cases[0].pattern.patterns, elements):
        if isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name == name:
            return element
    return None


def _literal_subject_element_is_safe(element, function):
    """Decline a subject expression that may fail before captures happen."""
    if isinstance(element, ast.Constant):
        return True
    if not isinstance(element, ast.Call) or element.keywords:
        return False
    owning = _module_for_function(function)
    if owning is None:
        return False
    bound = _bound_names(owning, function)
    aliases = {name for name, resolved in bound.items() if resolved == "contextlib"}
    changed = True
    while changed:
        before = len(aliases)
        for node in ast.walk(owning):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
            ):
                aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
        changed = len(aliases) != before
    for node in ast.walk(owning):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in aliases:
                return False
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("setattr", "delattr")
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in aliases
        ):
            return False
    for statement in owning.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if statement.decorator_list:
                return False
            continue
        if any(isinstance(node, ast.Call) for node in ast.walk(statement)):
            return False
    root = element.func
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name) or not _literal_subject_callee_is_intact(
        root, function, element
    ):
        return False
    if _resolves_to(element.func, "contextlib.nullcontext", bound):
        return not element.args
    return _is_readable_suppressor(element, bound) and (
        not element.args or _carried_suppressor_has_unshadowed_arguments(element, function)
    )


def _literal_subject_callee_is_intact(name_node, function, call=None):
    """Is the callee a plain name still naming what its import bound?

    #369. This is the ``from contextlib import nullcontext`` half of the
    shadowing question. The literal-subject proof needs the callee to *be*
    ``contextlib.nullcontext`` or ``contextlib.suppress``, and
    :func:`_name_is_rebound_away_from_module` answers the ``builtins.attr``
    question, which is a different one: it asks whether the name still names
    *the module it was imported as*. For a ``from X import name`` binding the
    name never names the module, so that helper counted the ordinary
    ``from contextlib import nullcontext`` itself as a rebinding and declined
    every element. The decidable-capture path was then unreachable whenever
    the alias was imported that ordinary way, and a header CPython enters
    was reported defeated -- the damaging direction.

    So the question asked here is the narrower one that actually matters: is
    this name bound by something *other* than the import that introduced it?
    Both spellings of a genuine import -- ``from contextlib import
    nullcontext`` and ``import contextlib`` -- are canonical for their own
    name, and everything else that rebinds the name (a parameter, a local
    store, a nested ``def``/``class``, a module-level store) still declines.

    Every decline leaves the capture undecidable, so the caller keeps its
    conservative "a capture may not run" answer rather than certifying a
    swallowed assert as enforced.
    """
    name = name_node.id
    if function is None:
        return False
    if name in _signature_bound_names(function):
        return False
    for statement in _own_scope_bindings(function):
        if statement is name_node:
            continue
        if _binding_is_the_callee_import(statement, name):
            continue
        if any(_names_bound_by_statement(statement, name)):
            return False
    return not _module_rebinds_name_for_any_import(function, name, call)


def _binding_is_the_callee_import(statement, name):
    """Is ``statement`` the ordinary import that brought ``name`` in?

    The ``from`` form binds an attribute of a module, so ``name`` does not
    name the module itself -- that is the whole difference from the
    ``builtins.attr`` spelling, which is why this predicate exists rather than
    reusing :func:`_is_canonical_module_import`.
    """
    if isinstance(statement, ast.ImportFrom):
        return any(
            (alias.asname or alias.name) == name and alias.name == name for alias in statement.names
        )
    return _is_canonical_module_import(statement, name)


def _module_rebinds_name_for_any_import(function, name, call=None):
    """:func:`_module_rebinds_name` with a ``from`` import counted as canonical.

    #369. The shared helper answers the ``builtins.attr`` question, where the
    only trustworthy binding is ``import builtins`` -- so it treats every
    ``from`` import as a rebinding. Here the name is a *member* that
    ``from contextlib import nullcontext`` brings in legitimately, and the
    same rule would decline the very import it is meant to trust.

    The ordering, the module-scope cut, and the per-node scan are the shared
    helper's; only the notion of a canonical import differs, and it is
    supplied by :func:`_binding_is_the_callee_import`.
    """
    module = _module_for_function(function)
    if module is None:
        return False
    at_module_scope = _call_runs_at_module_scope(function, call)
    for statement in getattr(module, "body", []):
        if at_module_scope and _module_statement_precedes_call(statement, function, call):
            break
        if _binding_is_the_callee_import(statement, name):
            continue
        if any(_names_bound_by_statement(statement, name)):
            return True
        for node in _module_level_bindings(statement):
            if _binding_is_the_callee_import(node, name):
                continue
            if at_module_scope and _node_precedes_call(statement, node, call):
                continue
            if any(_names_bound_by_statement(node, name)):
                return True
    return False


def _literal_match_reaches_header(function, match):
    """Keep the selection proof within a straight, readable execution path."""
    if (
        not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        or match not in function.body
    ):
        return False
    index = function.body.index(match)
    for statement in function.body[:index]:
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Import) and all(
            alias.name == "contextlib" for alias in statement.names
        ):
            continue
        if isinstance(statement, ast.Assign) and all(
            isinstance(target, ast.Name) for target in statement.targets
        ):
            value = statement.value
            elements = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
            if all(_literal_subject_element_is_safe(element, function) for element in elements):
                continue
        if isinstance(statement, ast.With) and all(
            isinstance(node, ast.Pass) for node in statement.body
        ):
            values = [item.context_expr for item in statement.items]
            values = [
                value.value
                if isinstance(value, ast.NamedExpr) and isinstance(value.target, ast.Name)
                else value
                for value in values
            ]
            if all(
                isinstance(value, ast.Call) and _literal_subject_element_is_safe(value, function)
                for value in values
            ):
                continue
        return False
    following = [node for node in function.body[index + 1 :] if not isinstance(node, ast.Pass)]
    if len(following) != 1 or not isinstance(following[0], ast.With):
        return False
    header = following[0]
    targets = [node for node in header.body if isinstance(node, ast.Assert)]
    if len(targets) != 1 or not _literal_match_has_failure_witness(function, match, targets[0]):
        return False
    return (
        len(header.items) == 1
        and isinstance(header.items[0].context_expr, ast.Name)
        and sum(isinstance(node, ast.Assert) for node in header.body) == 1
        and all(isinstance(node, (ast.Pass, ast.Assert)) for node in header.body)
    )


def _literal_match_has_failure_witness(function, match, assertion):
    """Require a literal failure or an unchanged parameter/literal contract."""
    test = assertion.test
    if isinstance(test, ast.Constant):
        return not bool(test.value)
    if not (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.NotEq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
    ):
        return False
    name = test.left.id
    if name not in {argument.arg for argument in _all_args(function) if argument is not None}:
        return False
    if name in _match_capture_names_for(match):
        return False
    for statement in function.body[: function.body.index(match)]:
        if any(any(_names_bound_by_statement(node, name)) for node in ast.walk(statement)):
            return False
        if any(
            isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store)
            for node in ast.walk(statement)
        ):
            return False
    return True
