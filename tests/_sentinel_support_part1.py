"""Sentinel support, part 1 of 5 (#122 split).

Functions: _module_tree, _is_zero_float_compare, guard_is_wired_on_the_fast_clock_path, count_comparisons, _comparisons_in, _count_comparison, _enclosing_function, _enclosing_function_node, observed_count_comparisons, _subscript_path, subscript_count_comparisons, retention_subscript_sites_observed, retention_sites_observed, _guard_source_tree, _is_termination_key, _swallows_assertion_error, milestones_tree, _owning_module, _bound_names, _own_imports, _resolved_dotted, _resolves_to, _name_catches_assertion_error, _suppression_names, _suppressed_by_dunder, _store_target_names, _scope_body_nodes, _match_capture_names, _carrier_runtime_kinds, _pattern_is_irrefutable, _pattern_binds, _capture_always_binds, _store_retires, _entry_may_be_an_unrun_capture, _match_capture_names_for
"""

# ruff: noqa: F821
#
# This file is a *fragment* of
# tests/_timed_menu_milestone_sentinel_support, not a module of its own. The
# support module reads its source with `exec` into one shared dict, so that every
# helper stays a bare global -- which is required twice over: the sentinel suite
# asserts one helper *calls* another by parsing the caller's source and matching
# `ast.Name` (`_sentinel_uses`), and callers rebind names on the support module
# itself (`support._module_tree = ...`), which only reaches the caller if there
# is a single globals dict rather than one private copy per file.
#
# A cross-fragment reference is therefore resolved at exec time, not import
# time, and static analysis cannot see it: the names below are defined by
# another fragment. Importing them explicitly would not help -- it would
# reintroduce a second namespace, which is the exact failure this split has to
# avoid -- so the undefined-name rule is disabled for these fragments.

# Constants and shared imports come from the support namespace this file
# is executed into: `tests._timed_menu_milestone_sentinel_support` execs
# the base and then every part, all against one dict, so `ast`, `inspect`,
# `milestones` and the module constants are already globals by the time
# anything below runs. Re-importing them here would only rebind the same
# objects, and importing this file as a module of its own would create a
# second, private namespace -- so these fragments are not importable alone.


def _module_tree():
    """Return the parsed AST of the milestones test module."""
    return ast.parse(inspect.getsource(milestones))


def _is_zero_float_compare(node):
    """True for a comparison of a name against the float ``0.0``."""
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "clock_step"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value == 0.0
    )


def guard_is_wired_on_the_fast_clock_path():
    """Is ``assert_not_deadline_truncated(record)`` still called under
    ``if clock_step == 0.0:`` inside ``run_owner``?

    The guard is only meaningful on the fast-clock path: with ``clock_step=0.0``
    the fake clock never advances, so the run must reach the frame bound rather
    than the deadline. A guard that is present in the file but commented out, or
    nested under a condition that is never true, is not wired and this returns
    ``False``.
    """
    tree = _module_tree()
    for func in tree.body:
        if not (isinstance(func, ast.FunctionDef) and func.name == "run_owner"):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.If) or not _is_zero_float_compare(node.test):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "assert_not_deadline_truncated"
                ):
                    return True
    return False


def count_comparisons():
    """Yield ``(function_name, operator, literal)`` for every ``len(...)`` assert.

    Every comparison operator is recorded, not just ``==``. That is the whole
    point: a check that only *finds* equality assertions would silently pass
    when a site is relaxed to ``>=``, because the relaxed line would simply stop
    being found. Pinning the full observed set means relaxing, deleting, or
    adding a site all change the set and fail.

    Asserts that cannot fail are excluded. ``retention_sites_observed()``
    otherwise reports a wrapped ``try/except AssertionError: pass`` as an intact
    ``== 300`` contract while the owning test has stopped being able to fail at
    all (#280, mutation M7), which is the worse half of that finding: the
    sentinel and the behaviour it protects go unenforced together.
    """
    tree = _module_tree()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        # `assert len(calls) == 271 and calls[-1][...] == 270` is a BoolOp whose
        # operands are the individual comparisons, so compare nodes -- not just
        # the assert's own top-level test.
        for comparison in _comparisons_in(node.test):
            yield from _count_comparison(tree, node, comparison)


def _comparisons_in(expression):
    if isinstance(expression, ast.Compare):
        return [expression]
    if isinstance(expression, ast.BoolOp):
        found = []
        for value in expression.values:
            found.extend(_comparisons_in(value))
        return found
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return _comparisons_in(expression.operand)
    return []


def _count_comparison(tree, node, comparison):
    if len(comparison.ops) != 1:
        return
    op = _OPERATORS.get(type(comparison.ops[0]))
    if op is None:
        return
    left, right = comparison.left, comparison.comparators[0]
    if not (isinstance(left, ast.Call) and getattr(left.func, "id", None) == "len"):
        return
    if isinstance(right, ast.Constant) and isinstance(right.value, int):
        owner = _enclosing_function_node(tree, node)
        if owner is not None and (not _is_enforced(owner, node, tree) or _may_bypass(node.test)):
            return
        yield (_enclosing_function(tree, node), op, right.value)


def _enclosing_function(tree, target):
    for func in tree.body:
        if isinstance(func, ast.FunctionDef) and any(child is target for child in ast.walk(func)):
            return func.name
    return "<module>"


def _enclosing_function_node(tree, target):
    """The ``FunctionDef`` that owns ``target``, or ``None`` at module level."""
    for func in tree.body:
        if isinstance(func, ast.FunctionDef) and any(child is target for child in ast.walk(func)):
            return func
    return None


def observed_count_comparisons():
    """Sorted ``(function, operator, literal)`` triples actually present."""
    return sorted(count_comparisons())


def _subscript_path(node):
    """Return ``(base_name, key_path)`` for a chained subscript, or ``None``.

    ``record["call_log"]["record_count"]`` is two nested ``Subscript`` nodes over
    a ``Name``; every slice must be a ``Constant`` string for the key path to be
    meaningful. The base ``Name`` is returned separately so callers can require
    the comparison to be rooted at ``record`` specifically -- ``state["record_
    count"]`` elsewhere in this module must not be mistaken for the same site.
    """
    keys = []
    current = node
    while isinstance(current, ast.Subscript):
        if not isinstance(current.slice, ast.Constant) or not isinstance(current.slice.value, str):
            return None
        keys.append(current.slice.value)
        current = current.value
    if not isinstance(current, ast.Name) or not keys:
        return None
    return (current.id, tuple(reversed(keys)))


def subscript_count_comparisons():
    """Yield ``(function_name, path, operator, literal)`` for pinned record subscripts.

    The counterpart to :func:`count_comparisons` for ``record[...]`` counts.
    Every operator is recorded, not just ``==``, so a site relaxed to ``>=``
    changes the observed set instead of merely ceasing to be found. Asserts that
    cannot fail are excluded via the shared ``_is_enforced``, the same rule
    #280 introduced for the ``len(...)`` sites.
    """
    tree = _module_tree()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for comparison in _comparisons_in(node.test):
            if len(comparison.ops) != 1:
                continue
            op = _OPERATORS.get(type(comparison.ops[0]))
            if op is None:
                continue
            resolved = _subscript_path(comparison.left)
            if resolved is None:
                continue
            base, path = resolved
            if base != "record":
                continue
            right = comparison.comparators[0]
            if not (isinstance(right, ast.Constant) and isinstance(right.value, int)):
                continue
            owner = _enclosing_function_node(tree, node)
            if owner is not None and (
                not _is_enforced(owner, node, tree) or _may_bypass(node.test)
            ):
                continue
            yield (_enclosing_function(tree, node), path, op, right.value)


def retention_subscript_sites_observed():
    """Sorted ``(function, path, operator, literal)`` for the pinned subscript sites only."""
    expected_paths = {
        function: {path for path, _, _ in sites}
        for function, sites in RETENTION_SUBSCRIPT_COUNT_SITES.items()
    }
    return sorted(
        quad for quad in subscript_count_comparisons() if quad[1] in expected_paths.get(quad[0], ())
    )


def retention_sites_observed():
    """The ``(function, operator, literal)`` triples for the pinned sites only."""
    return sorted(triple for triple in count_comparisons() if triple[0] in RETENTION_COUNT_SITES)


def _guard_source_tree():
    """Return the parsed AST of the module that defines the #261 guard."""
    import importlib

    return ast.parse(inspect.getsource(importlib.import_module(GUARD_MODULE)))


def _is_termination_key(node):
    """True for the ``record["termination"]`` subscript."""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "record"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "termination"
    )


def _swallows_assertion_error(handler):
    """True for a handler that can swallow the failure of an ``assert``.

    ``except AssertionError`` is the direct case, and a bare ``except:`` or
    ``except BaseException:`` swallows it too. ``except Exception`` is counted
    for the same reason: ``AssertionError`` derives from ``Exception``, so a
    handler naming that class also swallows the failure. It is named explicitly
    to keep the rule from depending on the reader knowing the exception
    hierarchy. Handlers for anything else -- ``except ValueError`` and friends
    -- cannot swallow an ``assert`` and are not counted.

    A re-raising handler (``except AssertionError: raise``) is conservatively
    counted as swallowing. The failure does propagate, so the assert is in fact
    enforced; deciding that needs real control-flow analysis, and this is a
    structural sentinel, so it asks only whether a handler *can* swallow. The
    error is toward reporting a live assert as unenforced, never toward missing
    a dead one.
    """
    caught = handler.type
    if caught is None:
        return True
    names = []
    for node in ast.walk(caught):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Tuple):
            names.extend(element.id for element in node.elts if isinstance(element, ast.Name))
    return bool({"AssertionError", "Exception", "BaseException"} & set(names))


def milestones_tree():
    """The parsed milestones module, cached for repeated name resolution."""
    global _MILESTONES_TREE
    if _MILESTONES_TREE is None:
        _MILESTONES_TREE = _module_tree()
    return _MILESTONES_TREE


def _owning_module(function):
    """The parsed module that ``function`` belongs to, for import resolution.

    The bound names must come from the module that actually *contains* the assert
    being judged, not from whichever module the sentinel happens to be inspecting.
    Reading them from the milestones tree alone would make the rule correct for
    that one file and silently wrong for any other, which is how a spelling slips
    through: the aliases that matter are the ones in the file being edited.

    A function from a tree the sentinel has not seen -- a probe built by a test --
    falls back to the milestones module, which is correct for every real call site
    because those asserts live in that file. Probes that need a different set of
    bindings pass their own tree via ``_is_enforced(..., tree=...)``.
    """
    for tree in (milestones_tree(), _module_tree()):
        if any(node is function for node in ast.walk(tree)):
            return tree
    return _module_tree()


def _bound_names(tree, function=None):
    """Map every name bound by an import to the dotted path it binds to.

    ``from contextlib import suppress as sq`` binds ``sq`` to
    ``contextlib.suppress``; ``import contextlib as c`` binds ``c`` to
    ``contextlib``.

    ``function``, when given, contributes its *own* imports as well. A binding
    is only in scope where it appears, so an alias introduced inside the
    function that holds the assert must be resolved from that function -- and
    for correctness it must win over a module-level binding of the same name,
    because the inner ``import`` shadows the outer one. Reading only
    ``tree.body`` left every function-local alias unresolved, which reported a
    disarmed assert as enforced; that gap is measured, not theoretical.
    """
    bound = {}
    nodes = list(getattr(tree, "body", []))
    if function is not None:
        nodes.extend(_own_imports(function))
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def _own_imports(function):
    """Imports belonging to ``function``'s own scope, excluding nested scopes.

    An import inside a nested ``def`` binds a name in *that* scope, so
    attributing it to the enclosing function would let a nested helper's alias
    decide whether the outer function's assert counts as suppressed -- a false
    "unenforced" verdict, which is the damaging direction.
    """
    found = []
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return found


def _resolved_dotted(expression, bound):
    """The dotted path ``expression`` refers to, with every alias expanded."""
    if isinstance(expression, ast.Name):
        return bound.get(expression.id, expression.id)
    if isinstance(expression, ast.Attribute):
        prefix = _resolved_dotted(expression.value, bound)
        return f"{prefix}.{expression.attr}" if prefix else None
    if isinstance(expression, ast.Call):
        # `contextlib.suppress(...)` *is* `contextlib.suppress` for the purpose
        # of asking what callable it is. Without this, an attribute hanging off
        # the result -- `contextlib.suppress(X).__enter__()` -- resolves to
        # None and every dunder-shaped defeat reads as an ordinary method call.
        return _resolved_dotted(expression.func, bound)
    return None


def _resolves_to(expression, dotted, bound):
    """Does ``expression`` name ``dotted``, however this module spelled it?"""
    return _resolved_dotted(expression, bound) == dotted


def _name_catches_assertion_error(name):
    """Does a caught *name* also catch ``AssertionError``?

    Shared by the handler rule and the suppression rule so the two agree on
    what counts. ``ValueError`` and friends are the false case: they cannot
    swallow an ``assert``. A name that is not a known exception type is
    reported as swallowing, because an assert that cannot be proven live must
    not be counted as enforced.
    """
    if name in {"AssertionError", "Exception", "BaseException"}:
        return True
    builtins = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
    candidate = builtins.get(name)
    if isinstance(candidate, type):
        return issubclass(candidate, AssertionError)
    return True


def _suppression_names(call):
    """The exception names passed to a ``suppress(...)`` call.

    A tuple is flattened, because ``suppress((TypeError, ValueError))`` and
    ``pytest.raises((KeyError, RuntimeError))`` are both ordinary spellings that
    name two types. Reading only the tuple node itself would report the
    unreadable case ``BaseException`` for *every* tuple, and since
    ``BaseException`` catches ``AssertionError``, that would report the pinned
    file's four tuple-typed ``pytest.raises`` sites as defeated -- a false alarm
    on real, live asserts. Measured on ``test_timed_menu_milestones.py``:
    ``(TypeError, ValueError)``, ``(KeyError, ValueError, RuntimeError)`` and
    ``(ValueError, RuntimeError)`` all appear.

    An element that is not a plain name is still reported as universal, so an
    argument the check cannot read is never assumed to be harmless.

    The *no positional argument at all* case is the one place where the two
    families must be told apart, and the difference is measured rather than
    assumed:

    * ``contextlib.suppress()`` is legal, and the no-argument reading here is
      ``BaseException`` -- but that is *stricter than the interpreter*, not a
      description of it. Measured: ``contextlib.suppress()`` stores
      ``_exceptions == ()``, and ``issubclass(AssertionError, ())`` is
      ``False``, so it suppresses **nothing** and an assert inside it fails
      loudly. Reading it as universal therefore over-reports.
      That direction is chosen on purpose: the argument the check *can* read
      is absent, and an absent argument is not evidence of a harmless one.
      The cost is a false alarm on a spelling the pinned file does not use; the
      alternative would be to treat "no argument" as "no suppression", which
      cannot be told apart here from a call whose arguments are simply
      unreadable.
    * ``pytest.raises()`` with no expected type raises ``ValueError: You must
      specify at least one parameter`` while the context object is being
      constructed -- before the body is entered at all. The test fails loudly
      and no assert is ever evaluated, so calling it a defeat would report a
      live contract as dead.

    ``pytest.raises(match=...)`` stays universal on purpose; see
    ``_raises_without_an_expected_type`` for why that case is undecidable
    rather than loud.
    """
    names = []
    for argument in call.args:
        if isinstance(argument, ast.Tuple):
            names.extend(
                element.id if isinstance(element, ast.Name) else "BaseException"
                for element in argument.elts
            )
        else:
            names.append(argument.id if isinstance(argument, ast.Name) else "BaseException")
    return names or ["BaseException"]


def _suppressed_by_dunder(call, bound):
    """Is this ``suppressor.__enter__()`` -- the dunder spelling of the defeat?

    ``with contextlib.suppress(AssertionError):`` has an alias that never leaves
    the ``with`` header:

        with contextlib.suppress(AssertionError).__enter__():
            assert 1 == 2

    That reads as a suppression to a human and as an ordinary method call to the
    existing rules: ``_resolves_to`` sees ``contextlib.suppress.__enter__`` and
    matches no entry in ``SUPPRESSING_CONTEXTS``, and ``_unreadable_suppressor``
    only accepts a bare ``Name``. Matching one concrete spelling and leaving the
    family open is exactly the mistake #288 recorded for ``except*``.

    Only the ``__enter__`` dunder is stripped, and only one component, so this
    cannot match a suppressor stored under a different attribute name. A
    non-suppressor that happens to define ``__enter__`` is not matched: the
    underlying call still has to resolve to a real suppressor.

    Note this reports the family as *unable to leave the test green* rather
    than as a silent swallow, and the runtime agrees: ``__enter__`` is
    ``pass``, so entry raises ``TypeError`` before the body runs. The same
    reasoning already covers ``pytest.raises()`` with no expected type in
    :func:`_raises_without_an_expected_type`. "Defeated" here means the owning
    test cannot go green, which is the property the sentinel is asserting.
    """
    func = call.func
    # The whole dunder family is answered uniformly, and the runtime agrees.
    # `contextlib.suppress.__enter__` is `def __enter__(self): pass`, so it
    # returns `None` for *every* instantiation, and
    # `with None:` raises `TypeError: 'NoneType' object does not support the
    # context manager protocol` -- the body is never reached, for every
    # exception argument and for the no-argument form alike. Measured for
    # `AssertionError`, `ValueError`, `RuntimeError`, `Exception`,
    # `BaseException` and `suppress()`.
    #
    # So the exception list is irrelevant here and no argument-reading
    # distinction can be earned: splitting the family by argument would report
    # two shapes with byte-identical runtime differently. This matches the
    # existing treatment of `pytest.raises()` with no expected type, which is
    # also loud *before* the body and is likewise read as unable to leave the
    # test green.
    if not (isinstance(func, ast.Attribute) and func.attr == "__enter__"):
        return []
    if not any(_resolves_to(func.value, dotted, bound) for dotted in SUPPRESSING_CONTEXTS):
        return []
    # Every member of the family is loud before the body, so the whole family is
    # reported as unable to leave the test green. `BaseException` is the
    # caller's "catches anything" marker and is deliberately *not* a claim
    # about this suppressor's arguments -- there are none that matter.
    return ["BaseException"]


def _store_target_names(targets):
    """Every name a single store statement binds.

    An ``ast.Name`` is the easy case, but Python lets a store target be a
    tuple, a list, or a starred expression, and any of those can name a
    variable:

        cs, other = (contextlib.nullcontext(), 2)
        [cs] = [contextlib.nullcontext()]
        cs, *rest = (contextlib.nullcontext(), 2, 3)

    Reading only ``ast.Name`` targets made those stores invisible, so a name
    carried by an earlier walrus was never retired and a live assert under the
    later ``with`` was reported defeated. Unwrapping the nested forms is what
    makes the supersession rule above apply to those binding forms too.

    Two store forms are still not modelled here. ``ast.AugAssign`` (``cs += 1``)
    is not unwrapped, and it cannot retire a carried suppressor. It is benign
    today only because ``contextlib.suppress`` defines no ``__iadd__`` and no
    ``__add__``, so ``cs += 1`` on a carried suppressor raises ``TypeError``
    before any assert can be reached -- the later ``with cs:`` is unreachable
    for the same reason it is after ``del cs``. That safety is incidental to
    the type, not a property this rule guarantees, so do not read the absence
    of an ``AugAssign`` branch as a decision.

    A subscript or attribute target (``obj.cs = ...``) is deliberately not
    followed: it binds an attribute, not a local name, so it cannot retire a
    local binding.

    A ``match`` capture is a store too, and it is not a target node at all --
    ``case [cs]:`` parses to an ``ast.MatchAs`` whose ``name`` is a plain string
    rather than an ``ast`` target. ``MatchStar`` (``case [other, *cs]:``) and
    ``MatchMapping.rest`` (``case {'a': 1, **cs}:``) bind the same way, so
    ``_match_capture_names`` handles them rather than inventing fake targets
    here.
    """
    names = []
    pending = list(targets)
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
    return names


def _scope_body_nodes(function):
    """Walk `function`'s own scope, stopping at nested function boundaries.

    ``ast.walk`` descends into a nested ``def``/``lambda``, whose body binds
    names in a different scope. A rule about which store is live *in this
    function* must not see those. Comprehensions are left in: a walrus inside a
    comprehension binds in the enclosing scope, which is exactly why
    ``_walrus_is_conditional`` has to reason about them at all.

    ``ClassDef`` is a boundary for the same reason, and was missing until
    #350. A class body executes in its own namespace, so a ``match`` capture
    written there binds the *class's* attribute, not ``function``'s local.
    Walking into one retired the enclosing name and certified a genuinely
    swallowed assert as enforced -- the damaging direction.

        def outer(x):
            with (cs := suppress(AssertionError)):
                pass
            class C:
                match nullcontext():
                    case cs:          # binds C.cs, NOT outer()'s cs
                        pass
            with cs:                # outer()'s cs is still the suppressor
                assert x != 1      # swallowed -> correct verdict is False

    Executed on CPython 3.12.14 with ``x=1``: the assert does not fire, because
    ``outer``'s ``cs`` still points at the suppressor, while the analyzer
    reported ``True`` -- a live contract certified where none exists.

    A class body is not a *function* scope, so excluding it here is what makes
    ``_own_imports`` and ``_match_capture_names`` agree: both walk the same
    boundary, and neither now attributes a nested class body's binding to the
    enclosing function. The nested ``def``/``lambda`` case was already correct
    and is unchanged; it is kept as a control row in the shipped test so the
    two boundaries cannot drift apart.
    """
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _match_capture_names(function):
    """Every ``(name, owning statement)`` bound by a ``match`` capture.

    A capture rebinds the name to the value that was matched, so a name
    carrying a suppressor from an earlier walrus must not survive the clause
    that captures it. Left unmodelled, the stale suppressor reached a later
    ``with cs:`` and certified a **live** assert as swallowed.

    These nodes carry a name string instead of a target, and they are not
    reachable from an enclosing statement's target list, so they need their own
    walk. ``MatchAs`` with ``pattern=None`` is the bare ``case _ as cs:`` form;
    a ``MatchAs`` with ``name=None`` is a wildcard or a group and binds nothing.

    The owning ``ast.stmt`` is returned alongside each name because
    ``_binding_order`` positions every store by the top-level statement that
    contains it; a bare pattern node is not in that chain.

    Only captures in `function`'s own scope count. A ``match`` inside a nested
    ``def`` or ``lambda`` binds a name in *that* scope, which has nothing to do
    with the outer binding, and walking into it retired the outer name -- the
    opposite error, turning a swallowed assert into a reported-live one.
    """
    owners = {}
    for statement in _scope_body_nodes(function):
        if not isinstance(statement, ast.Match):
            continue
        for node in ast.walk(statement):
            if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
                owners.setdefault(node.name, statement)
            elif isinstance(node, ast.MatchMapping) and node.rest is not None:
                owners.setdefault(node.rest, statement)
    return owners


def _carrier_runtime_kinds(function):
    """Every ``(name, (owning statement, runtime kind))`` from a string field.

    Three binding forms carry the bound name as a plain ``str`` on a node that
    is not a target at all, so a walk over ``statement.targets`` cannot see
    them and ``_store_bindings`` recorded nothing for the name at all:

        import os as cs                # Import.asname    -> a module
        def cs(): ...                  # FunctionDef.name -> a function
        class cs: ...                  # ClassDef.name    -> a class

    With no store entry, :func:`_entry_is_dead` had no value to read, so a
    later ``with cs:`` was reported *enforced* while the interpreter was
    already raising ``TypeError: 'module'/'type'/'function' object does not
    support the context manager protocol`` on entry. The assert is never
    evaluated, so calling it load-bearing is the **damaging** direction -- a
    dead contract certified as live. #359.

    ``from M import N as cs`` is deliberately **absent**, and that is the
    sharp edge of this function rather than an oversight. ``import os as cs``
    binds the module ``os`` and nothing else, so the syntax decides the value.
    But ``from M import N as cs`` binds whatever attribute ``N`` happens to be
    on ``M``, and one spelling produces several runtime types (measured, #376):

        from os import path as cs           -> os.path   (a module)
        from os import sep as cs            -> os.sep    (a str)
        from decimal import Decimal as cs   -> a class
        from mymod import ctx as cs         -> WHATEVER mymod.ctx is

    A module that exports a real context manager makes the identical statement
    decide the *other* way -- ``with cs:`` succeeds and the assert is live.
    Recording it as ``"module"`` would report that live assert as dead and
    **drop a real pinned contract**, which is the damaging direction this rule
    exists to correct. So the ``ImportFrom`` half is declined here, exactly as
    the module declines every other value it cannot read off the syntax.
    Resolving it would mean reading the attribute off the owning tree, which
    is a different and considerably larger rule.

    The runtime kind is returned rather than a synthetic ``ast.Name`` so the
    value stays attached to the real store: a synthesised node is a child of
    nothing, so containment and ordering both become unanswerable, which is
    the mistake #337's carrier machinery already learned to avoid.

    A ``match`` capture is deliberately *not* here: it binds whatever was
    matched, which is arbitrary and very often a real context manager, and
    #342 owns that decision. Only the forms whose value is fixed by the
    syntax are listed.

    Only bindings in `function`'s own scope count, for the reason
    ``_match_capture_names`` gives: a ``def cs`` in a nested scope binds that
    scope's name, and retiring the outer binding on it would report a
    swallowed assert as live -- the opposite error.
    """
    owners = {}
    for statement in _scope_body_nodes(function):
        carriers = []
        if isinstance(statement, ast.Import):
            # One statement binds several names, and only the `asname`
            # spelling retires a name *different* from the one imported.
            # `import a.b as cs` binds the module `a.b`, and the language
            # gives this one spelling exactly one meaning, so it is decidable.
            carriers.extend((alias.asname, "module") for alias in statement.names if alias.asname)
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            carriers.append((statement.name, "function"))
        elif isinstance(statement, ast.ClassDef):
            carriers.append((statement.name, "type"))
        for name, kind in carriers:
            owners.setdefault(name, (statement, kind))
    return owners


def _pattern_is_irrefutable(pattern):
    """Is ``pattern`` a pattern that matches *every* remaining subject?

    Two spellings qualify, and both are ``ast.MatchAs``:

    * a bare capture (``case name:``) and the wildcard (``case _:``), which
      parse to a ``MatchAs`` whose inner ``pattern`` is ``None``;
    * a capture of an already-irrefutable pattern (``case _ as name:``), which
      parses to a ``MatchAs`` wrapping *another* ``MatchAs`` -- the outer one
      is the capture, the inner one is the pattern it captures. Reading only
      ``pattern is None`` misses this form, which is exactly the irrefutable
      capture the shipped suite exercises.

    Every other pattern -- a value, a sequence, a mapping, a class pattern, a
    starred capture, or a value pattern wrapped in an ``as`` -- can fail to
    match, and a clause built on one of those is only *selected* on the path
    where it does.

    An ``ast.MatchOr`` is the one compound that could be irrefutable, and only
    when every alternative is. CPython rejects a *bare*-capture all-alternatives
    shape -- ``case x | y:`` fails with "name capture 'x' makes remaining
    patterns unreachable" -- but that is not the whole story, and the earlier
    claim here was too broad. ``case [*cs] | [*cs]:`` followed by another
    clause **does** compile on CPython 3.12.14, because neither alternative is
    a bare name capture on its own.

    Such a pattern is still *refutable*: both alternatives are sequence
    patterns, so a non-sequence subject skips the clause entirely and the
    capture never runs. The `all(...)` requirement is what makes that fall on
    the "capture may not have run" side, which keeps a carried suppressor in
    force rather than retiring a binding nothing made -- the damaging
    direction this whole issue is about.
    """
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or _pattern_is_irrefutable(pattern.pattern)
    if isinstance(pattern, ast.MatchOr):
        return all(_pattern_is_irrefutable(alternative) for alternative in pattern.patterns)
    return False


def _pattern_binds(pattern, name):
    """Does ``pattern`` bind ``name`` in any clause it can be selected for?"""
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            return True
        if isinstance(node, ast.MatchMapping) and node.rest == name:
            return True
    return False


def _capture_always_binds(match, name):
    """Is ``name`` bound by ``match`` on *every* path that reaches it?

    A capture is a side effect of its clause being *selected*, and a clause is
    only selected when the patterns before it all fail. So a capture is
    guaranteed to bind only in the one position where nothing can come before
    it and nothing can stop it:

    * that clause's pattern is irrefutable, so the match cannot fall through
      to "no clause matched" and a later clause cannot take it instead.

    The last-clause half of that condition is *checked* rather than assumed,
    but on CPython 3.12.14 it is not independently reachable: a capture in a
    clause that is followed by another is a compile error whenever the
    pattern is irrefutable -- ``case _ as cs:`` followed by anything is
    rejected with "wildcard makes remaining patterns unreachable", a bare
    ``case cs:`` with "name capture 'cs' makes remaining patterns
    unreachable", and a bare-name all-alternatives ``case a | b:`` likewise.
    (A *sequence* capture in every alternative, ``case [*cs] | [*cs]:``,
    compiles -- but that pattern is refutable, so it does not reach this
    branch; see :func:`_pattern_is_irrefutable`.) So an irrefutable capture is
    always the last clause in a program that compiles.
    The check is kept because it is the *reason* the answer is safe, it costs
    one comparison, and dropping it would make the function wrong for an AST
    that ``ast.parse`` accepts even though ``compile`` would not -- which is
    exactly the input this module is handed.

    A **guard** is deliberately not a condition, because the name is bound as
    soon as the pattern matches and is never unbound when the guard turns out
    to be false. That was measured rather than assumed: starting from a
    pre-existing value, ``case _ as cs if False:`` leaves ``cs`` holding the
    *subject*, not the value it had before, so a guard cannot undo a binding
    that has already happened. Treating a guard as a blocker would have retired
    an alias that the capture really did supersede.

    Anything else is a capture that *may* not have run. That distinction is
    what #342 is about: retiring a carried alias for a capture that never
    executed leaves the suppressor in force in reality while the analyzer
    reports the assert live.

    Measuring all six shipped capture forms against CPython 3.12.14 over ten
    subjects each, this predicate separates 38 dangerous verdicts from the
    remainder: without it every refutable shape reported a swallowed assert
    as enforced, because a no-match subject left the carried suppressor bound
    and nothing in the walk could see that the capture had not run.
    """
    if not match.cases:
        return False
    last = match.cases[-1]
    return _pattern_is_irrefutable(last.pattern) and _pattern_binds(last.pattern, name)


def _store_retires(statement, name):
    """Does this store settle ``name``, or may it simply not have run?

    Every store form the walk collects binds its name whenever control reaches
    it, and :func:`_resolve_bindings` already separates the ones that can be
    skipped (``conditional``). A ``match`` capture is the one form that is
    *always* recorded as unconditional yet can still fail to run, because its
    clause may not be selected. Left unfiltered it retires a carried alias on
    a path where the capture never happened, so the stale suppressor reaches
    the next ``with`` -- and the assert under it is really swallowed, while
    the analyzer calls it enforced.
    """
    return not isinstance(statement, ast.Match) or _capture_always_binds(statement, name)


def _entry_may_be_an_unrun_capture(entry):
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
    """
    statement = entry[0]
    if not isinstance(statement, ast.Match):
        return False
    return not all(
        _capture_always_binds(statement, name) for name in _match_capture_names_for(statement)
    )


def _match_capture_names_for(statement):
    """The names a single ``ast.Match`` statement captures."""
    names = []
    for node in ast.walk(statement):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            names.append(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            names.append(node.rest)
    return names

# ---------------------------------------------------------------------------
# Fragment guard. This file is not an importable module: it is one piece of
# tests/_timed_menu_milestone_sentinel_support, which `exec`s it, together with
# the other fragments, into a single shared namespace.
#
# That sharing is what makes the sentinels work, and it cannot survive being
# bypassed. Executed through the entry point, `__name__` is the support module's
# name and this guard is inert. Executed under its OWN name -- which is what
# `import tests._sentinel_support_part1` does, and what the import system does by itself -- the
# file would bind only the names it defines itself: a cross-fragment call would
# raise NameError, and a name imported from here would be a different object
# from the one the support module exports. Worse, that breakage is
# order-dependent and silent, which is a poor property for a module whose entire
# purpose is catching silent structural faults.
#
# Fail loudly instead, and name the supported import.
# ---------------------------------------------------------------------------
if __name__ == "tests._sentinel_support_part1":
    raise ImportError(
        "tests._sentinel_support_part1 is a fragment of "
        "tests._timed_menu_milestone_sentinel_support, not an importable "
        "module. Import the support module instead:\n"
        "    from tests import _timed_menu_milestone_sentinel_support as "
        "support\n"
        "Importing this fragment directly gives it a private copy of the shared "
        "namespace: cross-fragment calls raise NameError, and rebinding a name "
        "on the support module would not reach this code."
    )

