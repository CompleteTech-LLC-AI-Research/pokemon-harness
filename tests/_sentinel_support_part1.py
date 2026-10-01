# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F821


# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_part1":
    raise ImportError(
        "tests._sentinel_support_part1 is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


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
    """The *name* of the function that owns ``target`` in ``tree``.

    Name is load-bearing here: :func:`count_comparisons` keys its triples by
    it, and :func:`retention_sites_observed` filters on membership of
    ``RETENTION_COUNT_SITES``. A redefinition of this name with a different
    signature silently returns ``None`` for every lookup and empties the
    observed set, which fails the retention-count pins as "observed []" with
    no other symptom. The #355 work needs an enclosing-function *node* for a
    different purpose and lives in :func:`_nonlocal_parent_function`; do not
    fold the two together.
    """
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


#: Every ``len(...) == <int>`` exact-count assertion in the milestones module,
#: written out rather than recomputed.
#:
#: The four ``RETENTION_COUNT_SITES`` are hardcoded for the same reason, but
#: they only cover the #261 retention contract. ``observed_count_comparisons()``
#: is the *whole* set, and it was the wider set that went missing: when #294
#: began applying its bypass rule to ``and`` as well as ``or``, the live
#: contract ``assert len(errors) == 1 and isinstance(errors[0], RuntimeError)``
#: was reported as bypassed and silently dropped from this set -- 11 sites on
#: ``c896da7``, 10 on ``4415452``, with the whole sentinel file green.
#:
#: A backstop that recomputes its expectation from the same code it polices
#: cannot notice its own expectation shrinking, so this list is a literal. A
#: site that is deleted, relaxed, duplicated or neutralised changes the observed
#: set and fails.
#:
#: The literal was first written by dumping the observed set on the *broken*
#: tree, which reproduced the defect exactly: the list then held 10 entries and
#: deleting a pinned site kept the suite green. It is transcribed from the
#: pre-#294 baseline ``c896da7`` instead, and the ``== 1`` site from
#: ``test_disabled_touches_no_dependencies_and_still_checks_owner`` is the one
#: that had gone missing.
PINNED_COUNT_COMPARISONS = (
    ("_authored_cartridge_check", "==", 1),
    ("bounded_child", "<=", 32768),
    ("test_authored_cartridge_actual_helper_is_non_mutating", "==", 1),
    ("test_cap_failure_preserves_unspooled_call_and_incomplete_artifact", "<=", 1200),
    ("test_counter_saturation_fails_explicitly_instead_of_silently_losing_hits", "==", 1),
    ("test_default_retention_keeps_full_calls_and_installs_no_hooks", "==", 300),
    ("test_disabled_touches_no_dependencies_and_still_checks_owner", "==", 1),
    ("test_late_noncompleted_calls_have_exact_counts_and_full_evidence", "==", 271),
    ("test_overflow_is_sticky_counts_continue_without_context_access", "==", 1),
    (
        "test_stream_over_240_calls_keeps_every_record_hash_and_milestone_context",
        "==",
        300,
    ),
    ("test_unknown_actual_progress_is_counted_and_retained_as_interruption", "==", 271),
)


# The four frame-bound retention counts #270 names. Keyed by the test function
# that owns them; the value is the operator the assertion must keep using.
# ``test_late_noncompleted_calls_*`` is parametrised and so covers the ``== 271``
# site once per status. The 300s are the inline- and stream-retention paths.
RETENTION_COUNT_SITES = {
    "test_stream_over_240_calls_keeps_every_record_hash_and_milestone_context": (300, "=="),
    "test_default_retention_keeps_full_calls_and_installs_no_hooks": (300, "=="),
    "test_late_noncompleted_calls_have_exact_counts_and_full_evidence": (271, "=="),
    "test_unknown_actual_progress_is_counted_and_retained_as_interruption": (271, "=="),
}


# --- #270 Finding 2, remaining half: the *subscript* 271 counts ---
#
# ``RETENTION_COUNT_SITES`` is enforced by ``count_comparisons()``, which only
# yields comparisons whose left side is a ``len(...)`` call. The same test also
# pins its counts through record subscripts, which that mechanism never sees:
#
#     assert record["call_log"]["record_count"] == 271
#     assert record["call_counts"]["requested_frames"] == 271
#     assert record["call_counts"]["total"] == 271
#
# Relaxing any of those to ``>= 1`` leaves all four retention sentinels green,
# so the "at least one call was retained" contract could be restored while every
# backstop reported the site intact. These are recorded as (path, operator,
# literal) triples and compared as a set, for the same reason the ``len(...)``
# sites are: relaxing, deleting, duplicating or neutralising a site all change
# the observed set.

#: The record subscripts whose value is a retained-call count, keyed by the test
#: function that owns them. Each entry is ``(key path, literal, operator)``.
RETENTION_SUBSCRIPT_COUNT_SITES = {
    "test_late_noncompleted_calls_have_exact_counts_and_full_evidence": (
        (("call_log", "record_count"), 271, "=="),
        (("call_counts", "requested_frames"), 271, "=="),
        (("call_counts", "total"), 271, "=="),
    ),
}


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


# --- #270 criterion 1, second half: the guard must have teeth, not just be called ---

#: The module that defines the #261 guard, and the terminal state it exists to reject.
GUARD_MODULE = "tests._timed_menu_frame_bound_support"
GUARD_FUNCTION = "assert_not_deadline_truncated"
DEADLINE_TERMINATION = "cancelled_or_deadline"


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


_MILESTONES_TREE = None

#: ``id(function) -> module tree or None``. Memoises the module a function was
#: defined in, which :func:`_module_binds_name` needs per condition and which
#: costs a full tree walk to answer. Keyed by node identity because the AST is
#: built once per parsed file and outlives the call.
_MODULE_FOR_FUNCTION = {}

#: Statements that open a *block* without opening a new name scope. A binding
#: inside one of these still runs in the module scope, so the module walk has
#: to descend through them; every other child is either a binding itself or the
#: body of a nested scope, and neither is descended into.
_MODULE_LEVEL_BLOCKS = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Match,
)


def _block_body(statement):
    """The statements a block statement runs, in source order.

    ``Try`` and ``Match`` run several bodies, so every one of them is included;
    a binding in any of them is a module binding on the path that runs. A
    ``match`` clause is flattened to its statements rather than returned as a
    ``Case`` node, because ``Case`` is not one of the block types this walk
    knows how to open -- a ``def`` inside a case is still a module binding on
    the path that matches, and reading past it reported a header DEAD that
    CPython enters.

    An ``except`` handler is a body too, and it is a body that has plainly run
    by the time anything below the ``try`` is evaluated:

        try:
            raise ValueError
        except ValueError:
            def list(): return nullcontext()
        cs = list()          # the handler's `list`, not the builtin

    Leaving handler bodies out of the walk skipped the binding entirely and
    reported that ``cs`` as the builtin ``list`` -- a ``TypeError``-raising
    header as LIVE. A handler's ``name`` (``except E as exc``) is a binding too,
    but that one is *deleted* when the handler exits, so the handler body is
    what is walked and the bound name is read from it like any other.
    """
    if isinstance(statement, ast.Try):
        return [
            *statement.body,
            *statement.orelse,
            *statement.finalbody,
            *(nested for handler in statement.handlers for nested in handler.body),
        ]
    if isinstance(statement, ast.Match):
        return [nested for case in statement.cases for nested in case.body]
    if isinstance(statement, (ast.If, ast.While)):
        return [*statement.body, *statement.orelse]
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return [*statement.body, *statement.orelse]
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return list(statement.body)
    return []


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

    The result is in whatever order the LIFO walk produces, and that is
    deliberate. Two callers merge it last-wins, and they want opposite
    answers, so the order is not a shared fact:

    * :func:`_bound_names` merges into one table, where the *earlier*
      import currently wins -- which is how a `from`-leaf shadow is caught at
      all, and why `_import_only_binds_the_resolved_root` asks each statement
      about its own bindings rather than the merged ones.
    * :func:`_nonlocal_parent_imports` re-sorts into source order, because an
      enclosing scope's *last* binding is the one in force.

    Sorting here was tried and reverted. It made `_bound_names` answer with
    the true final binding, which is more accurate in the abstract and less
    accurate in practice: three live asserts in the adversarial sweep
    (`import json as contextlib` followed by `import contextlib`, and the
    multi-alias spellings of it) stopped being certified, because with the
    shadow no longer visible the shadowing import looked transparent. Those
    are false-DEADs -- the safe direction, and still wrong.
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
        elif isinstance(statement, ast.ImportFrom):
            # #389. `from os import path as cs` binds a *name decided by the
            # module's own attribute*, which is why the docstring above
            # declines this form outright: one spelling produces several
            # runtime types and one of them is genuinely enterable
            # (`from contextlib import nullcontext as cs`).
            #
            # The decline is lifted only for attributes the real interpreter
            # can resolve to something that cannot implement the context
            # manager protocol. `_from_import_kind` answers exactly that and
            # returns `None` for an enterable or unresolvable attribute, so
            # a live header keeps its assert. `from M import N` without an
            # `asname` binds `N` itself, not a different name, so it is left
            # to the ordinary store rules exactly as `import M` is.
            carriers.extend(
                (alias.asname, kind)
                for alias in statement.names
                if alias.asname
                and (kind := _from_import_kind(statement.module, alias.name)) is not None
            )
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
