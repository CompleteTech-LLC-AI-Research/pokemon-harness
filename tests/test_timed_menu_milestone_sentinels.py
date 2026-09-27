"""Sentinels that keep the #261 guard and the retention counts load-bearing (#270).

``tests/test_timed_menu_milestones.py`` holds the frame-bound retention
contract, but two of its assertions can be removed or relaxed with every
behavioural test still green. This module pins them structurally.

Both gaps were confirmed on ``master`` before this file existed:

* ``if clock_step == 0.0:`` -> ``if False:`` leaves the milestones + frame-bound
  files at **83 passed, 0 failed** (Finding 1: the #261 guard is unwired).
* additionally relaxing ``assert len(calls) == 300`` to ``>= 1`` also leaves
  **83 passed, 0 failed** (Finding 2: the exact counts are unpinned).

These checks are structural -- AST inspection -- because that is the only way
to catch a *deletion*. The #261 guard is proven non-vacuous behaviourally (it
fires with 8 failures when production is mutated so the wall clock wins), so
what is missing is only the proof that it is still wired. The behavioural
assertions in the milestones module remain the contract; this file is the
backstop that keeps them from being quietly removed.
"""

import ast
import inspect
import textwrap

import pytest

from tests import _timed_menu_milestone_sentinel_support as support
from tests._timed_menu_milestone_sentinel_support import (
    DEADLINE_TERMINATION,
    GUARD_FUNCTION,
    PINNED_COUNT_COMPARISONS,
    RETENTION_COUNT_SITES,
    RETENTION_SUBSCRIPT_COUNT_SITES,
    RUN_OWNER,
    _bypassing_sites,
    _is_enforced,
    _is_tautology,
    _may_bypass,
    _names_bound_by,
    count_sites_that_bypass_the_guard,
    guard_is_wired_on_the_fast_clock_path,
    guard_rejects_the_deadline_terminal_state,
    observed_count_comparisons,
    retention_sites_observed,
    retention_subscript_sites_observed,
)

#: PEP 695's node type, read through the module rather than imported by name.
#: Importing it directly would make this file uncollectable against any
#: support module that predates the definition-carrier repair, which is exactly
#: the configuration the "do these new rows actually fail before the fix?"
#: check runs in. Reading it through `getattr` keeps that check a *test
#: failure* rather than a collection error, so the count of failing rows stays
#: readable instead of collapsing to a single error.
_TYPE_ALIAS = getattr(support, "_TYPE_ALIAS", None)


def _sentinel_uses(name, owner_name):
    """True if ``owner_name`` in the support module actually calls ``name``."""
    source = inspect.getsource(getattr(support, owner_name))
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        for node in ast.walk(ast.parse(source))
    )


@pytest.mark.parametrize(
    ("predicate", "caller", "defect"),
    (
        (
            "_may_bypass",
            "_count_comparison",
            (
                "pinned count sites would be reported as exact equalities again "
                "even when a BoolOp short-circuits the comparison away, which is "
                "the #280 hole these sentinels exist to close"
            ),
        ),
        (
            "_may_bypass",
            "guard_rejects_the_deadline_terminal_state",
            (
                f"{GUARD_FUNCTION}() would count as having teeth again once its "
                "assert is short-circuited by a tautological `or` operand, so a "
                "deadline-truncated run would again be reported as retention"
            ),
        ),
        (
            "_is_enforced",
            "_count_comparison",
            (
                "pinned count sites would count again when wrapped in a "
                "swallowing try/except, which is the #280 handler half"
            ),
        ),
        (
            "_is_enforced",
            "guard_rejects_the_deadline_terminal_state",
            (
                "the teeth check would certify a guard that can no longer "
                "fail: wrapping its assert in try/except AssertionError or "
                "contextlib.suppress keeps the node and the operator, so only "
                "_is_enforced closes the #280 defect this function exists to "
                "catch"
            ),
        ),
        (
            "_may_bypass",
            "subscript_count_comparisons",
            (
                "pinned record-subscript counts would be reported as exact "
                "equalities again once a tautological operand short-circuits "
                "them, undoing the counts this branch just added"
            ),
        ),
    ),
    ids=(
        "count-uses-bypass",
        "guard-uses-bypass",
        "count-uses-enforced",
        "guard-uses-enforced",
        "subscript-counts-use-bypass",
    ),
)
def test_the_short_circuit_rule_is_wired_into_every_enforcement_call_site(
    predicate, caller, defect
):
    """``_may_bypass`` must be *called* wherever enforcement is decided.

    The bypass rule is only load-bearing if the helpers that decide whether a
    pinned assert is live actually consult it. The table-driven rows above
    exercise ``_may_bypass`` *directly*, so they keep passing even when these
    three callers stop calling it. Measured on this branch, each deletion left
    all 43 rows green and unmasks exactly its own attack:

    ==================  ==============================  ==========================
    deletion             attack that then survives       caught when wiring intact
    ==================  ==============================  ==========================
    ``_count_comparison``  ``or True`` on a ``== 271``   yes (1 failed / 42 passed)
    guard check            ``or True`` on the #261      yes (1 failed / 42 passed)
                            guard's own assert
    subscript counts       ``or True`` on                yes (1 failed / 42 passed)
                            ``record["call_counts"]``
    ==================  ==============================  ==========================

    Pinning the wiring here means a future edit that drops one of these calls
    fails loudly, instead of silently disarming that call site and nothing else.
    """
    assert _sentinel_uses(predicate, caller), (
        f"{caller}() no longer consults {predicate}(), so {defect}"
    )


def test_the_261_guard_is_still_wired_to_the_fast_clock_path():
    """The deadline guard must remain called under ``if clock_step == 0.0:``.

    This is the check #270 asks for: it fails when the guard is *unwired*,
    not merely when it misfires. #262's guard is behaviourally proven to fire,
    so a behavioural test alone cannot detect its removal.
    """
    assert guard_is_wired_on_the_fast_clock_path(), (
        "assert_not_deadline_truncated is no longer called on the "
        "clock_step == 0.0 path in run_owner, so the #261 guard is unwired: "
        "a deadline-truncated run would report as a retention pass"
    )


def test_the_four_retention_counts_are_still_exact_equalities():
    """The pinned retention counts must keep using ``==`` against their literals.

    ``RETENTION_COUNT_SITES`` is the expected ``(function -> (literal, op))``
    mapping; ``retention_sites_observed()`` is what the module actually
    contains. Comparing the two means a site that is relaxed to ``>=``,
    deleted, or duplicated all produce a different observed set and fail.
    """
    expected = sorted(
        (function, op, literal) for function, (literal, op) in RETENTION_COUNT_SITES.items()
    )
    observed = retention_sites_observed()
    assert observed == expected, (
        "the frame-bound retention counts are no longer exact equality "
        f"assertions at the pinned sites; expected {expected}, observed {observed}"
    )


def test_the_full_set_of_pinned_count_comparisons_has_not_shrunk():
    """No exact-count assertion may leave the observed set without failing.

    ``test_the_four_retention_counts_are_still_exact_equalities`` covers the
    #261 retention contract. This covers the *wider* set, and it is pinned as a
    literal rather than recomputed, because a backstop that derives its
    expectation from the code it polices cannot notice that expectation
    shrinking. Measured: #294 began applying its bypass rule to ``and`` as well
    as ``or``, the live contract at ``test_timed_menu_milestones.py:155``
    (``assert len(errors) == 1 and isinstance(errors[0], RuntimeError)``) was
    reported as bypassed, and the observed count went 11 -> 10 with this file
    entirely green.
    """
    observed = observed_count_comparisons()
    expected = sorted(PINNED_COUNT_COMPARISONS)
    assert observed == expected, (
        "the set of pinned exact-count comparisons changed; a site was "
        f"deleted, relaxed, duplicated or neutralised. missing="
        f"{sorted(set(expected) - set(observed))} unexpected="
        f"{sorted(set(observed) - set(expected))}"
    )


def test_the_261_guard_still_rejects_the_deadline_terminal_state():
    """The guard must have teeth, not merely be called.

    ``test_the_261_guard_is_still_wired_to_the_fast_clock_path`` proves the
    guard is invoked, but not that invoking it rejects anything. Reducing the
    guard body to ``pass`` leaves the wiring check green while the #261 contract
    no longer exists -- and the exact-count assertions it exists to qualify would
    then read a deadline-truncated run as a retention result, which is the exact
    confusion #261 was filed to remove.
    """
    assert guard_rejects_the_deadline_terminal_state(), (
        f"{GUARD_FUNCTION}() no longer rejects termination == "
        f"{DEADLINE_TERMINATION!r}; it is wired but toothless, so a "
        "deadline-truncated run would again be reported as a retention failure"
    )


def test_the_pinned_counts_are_reached_with_the_261_precondition_active():
    """No pinned count site may opt out of the #261 terminal-state guard.

    Criterion 3 of #270 asks about the sites at ``:746``, ``:755`` and ``:971``,
    which do not each call the guard themselves. They get the precondition
    centrally, from ``run_owner()``, which applies it whenever ``clock_step`` is
    the default ``0.0``. Duplicating four guard calls would add noise without
    adding coverage; what matters is that no site can pass its own
    ``clock_step`` and quietly lose the precondition its exact count depends on.
    """
    offenders = count_sites_that_bypass_the_guard()
    assert not offenders, (
        f"these pinned retention sites call {RUN_OWNER}() with a clock_step "
        f"that bypasses the #261 terminal-state guard: {offenders}"
    )


#: ``(label, call arguments, bypasses?)`` -- does this spelling of the pinned
#: site's ``run_owner`` call opt the site out of the #261 precondition? A ``**``
#: unpacking arrives from ``ast`` with ``arg=None``, so a filter on
#: ``keyword.arg == "clock_step"`` skips it even though it reaches the same
#: unguarded path.
CLOCK_STEP_SPELLINGS = (
    ("guarded literal", "clock_step=0.0", False),
    ("named override", "clock_step=0.5", True),
    ("absent", "", False),
    ("unpacked override", '**{"clock_step": 0.5}', True),
    ("unpacked guarded", '**{"clock_step": 0.0}', False),
    ("unpacked with sibling", '**{"clock_step": 0.5, "x": 1}', True),
    ("unpacked without clock_step", '**{"x": 1}', False),
    ("unrelated keyword", "other=1", False),
    # An unpacking that cannot be read statically might still carry
    # clock_step, so it is reported rather than assumed safe.
    ("unreadable unpacking", "**options", True),
)


@pytest.mark.parametrize(
    ("label", "arguments", "bypasses"),
    CLOCK_STEP_SPELLINGS,
    ids=[spelling[0] for spelling in CLOCK_STEP_SPELLINGS],
)
def test_every_clock_step_spelling_is_classified(label, arguments, bypasses):
    """The guard-bypass check must read ``**`` as well as named keywords.

    ``run_owner(m, p, **{"clock_step": 0.5})`` reaches the same unguarded path
    as the named spelling, but ``ast`` reports it with ``arg=None``. Matching
    only the named form is the mistake #288 corrected for ``except*``: one
    concrete node shape instead of the family that reaches it.

    Each row is mounted inside a real pinned retention function and run through
    the same site walk :func:`count_sites_that_bypass_the_guard` uses, so a row
    cannot pass while the production call path has stopped consulting the
    classifier at all.
    """
    site = next(iter(RETENTION_COUNT_SITES))
    source = f"def {site}():\n    return run_owner(m, p, {arguments})\n"
    offenders = _bypassing_sites(ast.parse(source))
    assert bool(offenders) is bypasses, f"{label}: reported {offenders}"


def test_a_bypass_in_any_pinned_site_is_reported_not_just_the_first():
    """The site walk must cover every pinned function, not only the first.

    Every row in the spellings table mounts a single-function tree, so a walk
    narrowed to one function -- ``milestones_tree.body[:1]`` -- still reports
    every row correctly and leaves the suite green. Measured on this head with
    the narrowing applied in memory: 1 bypassing site is reported either way,
    but 2, 3 and 4 bypassing sites all collapse to 1. The real tree is clean
    today, so this would only under-report once a bypass lands in a pinned site
    that is not the first one walked.

    A bypass in the *last* pinned function is the discriminating case, so this
    mounts every pinned site and puts the override in the final one.
    """
    sites = list(RETENTION_COUNT_SITES)
    assert len(sites) > 1, "this row is only meaningful with more than one pinned site"
    guarded = f"def {sites[0]}():\n    return run_owner(m, p)\n"
    last = sites[-1]
    overriding = f"def {last}():\n    return run_owner(m, p, **{{'clock_step': 0.5}})\n"
    offenders = _bypassing_sites(ast.parse(guarded + overriding))
    assert [name for name, _spelling in offenders] == [last], f"reported {offenders}"


def test_the_production_entry_point_is_what_actually_reports_a_bypass():
    """The spellings table must not be satisfiable while production is inert.

    The rows above drive ``_bypassing_sites()`` directly, so they stay green if
    ``count_sites_that_bypass_the_guard()`` stops consulting it altogether.
    Measured on this head: replacing that function's body with ``return []``
    left the whole sentinel file green. A table that cannot fail when the
    production entry point is stubbed out is not pinning the entry point.

    The production function reads the real module through ``_module_tree()``,
    so the way to pin it is to make it report something. ``_module_tree`` is
    temporarily pointed at a tree carrying a real bypass: the entry point must
    report it. Combined with the ``== []`` assertion already in
    ``test_the_pinned_counts_are_reached_with_the_261_precondition_active``,
    that pins the entry point from both directions — it must fire, and it must
    not fire spuriously.
    """
    site = next(iter(RETENTION_COUNT_SITES))
    mutant = ast.parse(f"def {site}():\n    return run_owner(m, p, **{{'clock_step': 0.5}})\n")
    assert _bypassing_sites(mutant), "sanity: the mutant tree must contain a bypass"

    real_tree = support._module_tree
    support._module_tree = lambda: mutant
    try:
        offenders = count_sites_that_bypass_the_guard()
    finally:
        support._module_tree = real_tree
    assert offenders, (
        "count_sites_that_bypass_the_guard() did not report a bypass that is "
        "demonstrably present, so it is not actually reading the tree"
    )


def test_the_pinned_record_subscript_counts_are_still_exact_equalities():
    """The ``record[...]`` retention counts must keep using ``==`` against their literals.

    ``test_the_four_retention_counts_are_still_exact_equalities`` works through
    ``count_comparisons()``, which only yields comparisons whose left side is a
    ``len(...)`` call. The same test also pins its counts through record
    subscripts -- ``record["call_log"]["record_count"]``,
    ``record["call_counts"]["requested_frames"]`` and
    ``record["call_counts"]["total"]`` -- and those were outside the mechanism
    entirely, so relaxing any of them to ``>= 1`` left this module green while
    the "at least one call was retained" contract came back.

    Same set-comparison design, so relaxing, deleting, duplicating, or
    neutralising any of these sites changes the observed set and fails.
    """
    expected = sorted(
        (function, path, op, literal)
        for function, sites in RETENTION_SUBSCRIPT_COUNT_SITES.items()
        for path, literal, op in sites
    )
    observed = retention_subscript_sites_observed()
    assert observed == expected, (
        "the pinned record-subscript retention counts are no longer exact "
        f"equality assertions; expected {expected}, observed {observed}"
    )


#: ``(label, body, live)`` -- can the assert in this body actually fail?
#: The handler axis is covered by ``_is_enforced``. These rows pin the other
#: half: a comparison a ``BoolOp`` can short-circuit around is present in the
#: AST and still unchecked, which no handler rule can see.
BYPASS_SHAPES = (
    ("plain assert", "assert x == 1", True),
    ("and of two comparisons", "assert len(a) == 1 and len(b) == 2", True),
    ("double negative", "assert not (x != 1)", True),
    ("comparison or True", "assert x != 1 or True", False),
    ("True or comparison", "assert True or x != 1", False),
    # `and` skips its right operand only when the left is *falsy*, so a truthy
    # runtime value does not make the comparison unchecked. These two rows were
    # previously pinned as bypasses, which was wrong: it silently dropped the
    # live contract `len(errors) == 1 and isinstance(errors[0], RuntimeError)`
    # out of the pinned count set. `or` is the operator that does bypass on a
    # truthy sibling, and it is pinned above.
    ("comparison and flag", "assert x != 1 and flag", True),
    ("flag and comparison", "assert flag and x != 1", True),
    # A tautology in the leading position does decide an `and`, because
    # `True and <comparison>` never evaluates the comparison at all.
    ("tautology and comparison", "assert (1 == 1) and x != 1", False),
    ("truthy literal and comparison", "assert True and x != 1", False),
    # ...but not in a trailing position: the comparison is evaluated first.
    ("comparison and tautology", "assert x != 1 and (1 == 1)", True),
    # A preceding operand that could be falsy leaves the comparison reachable.
    ("flag and tautology and comparison", "assert flag and True and x != 1", True),
    # ...and one that is provably falsy never reaches the tautology at all.
    ("falsy literal and tautology and comparison", "assert 0 and True and x != 1", True),
    ("nested or", "assert x != 1 or (y or True)", False),
    ("runtime condition", "if flag:\n assert x == 1", True),
    # A lone call cannot short-circuit on its own, so this stays enforced.
    ("lone call operand", "assert x != 1 or len(y) > 0", True),
    # Tautological operands decide the `or` whatever the record says, so the
    # comparison beside them is never evaluated. These are the shapes an
    # earlier version of _may_bypass missed by not recursing into Compare.
    ("tautology len >= 0", "assert x != 1 or len(y) >= 0", False),
    ("tautology len > -1", "assert x != 1 or len(y) > -1", False),
    ("tautology len > -5", "assert x != 1 or len(y) > -5", False),
    ("tautology call compare", 'assert x != 1 or len(y.get("e", [])) >= 0', False),
    ("tautology on the left", "assert len(y) >= 0 or x != 1", False),
    # A comparison built only from literals is decidable without reading any
    # state, so it short-circuits the `or` exactly as a bare `True` does. Its
    # left operand is a constant rather than a call, so the count heuristic
    # cannot see it and these were previously reported as enforced.
    ("literal compare eq", "assert x != 1 or (1 == 1)", False),
    ("literal compare zero eq", "assert x != 1 or (0 == 0)", False),
    ("literal compare string eq", "assert x != 1 or ('' == '')", False),
    ("literal compare lt", "assert x != 1 or (1 < 2)", False),
    ("literal compare gt", "assert x != 1 or (2 > 1)", False),
    ("literal container", "assert x != 1 or [1, 2]", False),
    ("literal arithmetic", "assert x != 1 or (1 + 1 == 2)", False),
    # Real comparisons that must stay enforced, or the rule would cry wolf and
    # a genuine regression would be waved through as a known shape.
    ("real count comparison", "assert x != 1 or len(y) == 300", True),
    ("real count and", "assert len(y) == 300 and len(z) == 271", True),
    ("real upper bound", "assert x != 1 or len(y) <= 100", True),
    ("empty-only bound", "assert x != 1 or len(y) < 1", True),
    ("non-numeric bound", 'assert x != 1 or len(y) > "a"', True),
    ("subscript left operand", 'assert x != 1 or record["n"] >= 0', True),
    ("bool is not a number", "assert x != 1 or len(y) >= True", True),
    ("arithmetic left operand", "assert x != 1 or a - b >= 0", True),
    # Reads a Name, so it is not constant-foldable and stays enforced. This is
    # the safe direction: a wrong answer reports a live assert as dead.
    ("self compare", "assert x != 1 or (x == x)", True),
    ("runtime value compare", "assert x != 1 or (a == b)", True),
    ("call compare", "assert x != 1 or f(a) == f(a)", True),
    ("subscript compare", 'assert x != 1 or record["k"] == record["k"]', True),
    ("runtime condition on count", "if len(y) > 0:\n assert x == 1", True),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    BYPASS_SHAPES,
    ids=[shape[0] for shape in BYPASS_SHAPES],
)
def test_the_bypass_check_separates_live_comparisons_from_short_circuited_ones(label, body, live):
    """A comparison counts as enforced unless a ``BoolOp`` can skip checking it.

    ``assert x != 1 or True`` keeps the comparison in the AST and keeps the
    operator, so a presence-only check still calls the contract intact while the
    assert is incapable of failing. The tautology form is the same hole spelled
    differently, and it survived an earlier version of this rule.
    """
    source = "def probe(x, flag, y, a, b, record):\n" + "\n".join(
        f"    {line}" for line in body.splitlines()
    )
    function = ast.parse(source).body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node) and not _may_bypass(node.test) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'short-circuited'}, got {results}"
    )


@pytest.mark.parametrize(
    ("source", "tautology"),
    (
        ("len(y) >= 0", True),
        ("len(y) > -1", True),
        ("len(y) > 0", False),
        ("len(y) <= 0", False),
        ("len(y) < 1", False),
        ("len(y) == 300", False),
        ("record['n'] >= 0", False),
        ("x > -1", False),
        ("a - b >= 0", False),
        ("len(y) >= True", False),
        ("len(y) >= 0.0", True),
        ("1 == 1", True),
        ("0 == 0", True),
        ("'' == ''", True),
        ("1 < 2", True),
        ("2 > 1", True),
        ("1 + 1 == 2", True),
        ("x == x", False),
        ("a == b", False),
        ("True", True),
        ("False", False),
        ("flag", False),
    ),
)
def test_tautology_detection_only_fires_on_provably_always_true_forms(source, tautology):
    """``_is_tautology`` must never claim a real comparison is always true.

    A false positive here would make the sentinels report a live assert as dead
    and cry wolf on a genuine regression, so the negative cases carry as much
    weight as the positive ones.
    """
    assert _is_tautology(ast.parse(source, mode="eval").body) is tautology


#: Every spelling that disarms an assert without removing it, and the matching
#: live control that must stay enforced. The controls are not padding: a rule
#: that flags everything is indistinguishable from a rule that works, and this
#: repo has already recorded three probes that reported a confident "caught"
#: verdict because the mutation never applied.
UNREACHABLE_SHAPES = (
    # `except*` parses to ast.TryStar, which is not a subclass of ast.Try.
    # Matching only the latter left it an unguarded spelling of the defeat.
    (
        "try star",
        "    try:\n        assert x != 1\n    except* AssertionError:\n        pass",
        False,
    ),
    ("bare except", "    try:\n        assert x != 1\n    except:\n        pass", False),
    (
        "except Exception",
        "    try:\n        assert x != 1\n    except Exception:\n        pass",
        False,
    ),
    # A swallowing `try` around a *sibling* does not disarm an assert outside
    # it; treating it as though it did would drop real pinned sites.
    (
        "try on sibling",
        "    try:\n        helper()\n    except AssertionError:\n        pass\n    assert x != 1",
        True,
    ),
    # The suppression family. Matched by resolved name, so all four spellings
    # collapse to the same value before the comparison.
    (
        "suppress qualified",
        "    with contextlib.suppress(AssertionError):\n        assert x != 1",
        False,
    ),
    (
        "suppress from import",
        "    with suppress(AssertionError):\n        assert x != 1",
        False,
    ),
    (
        "suppress aliased module",
        "    with c.suppress(AssertionError):\n        assert x != 1",
        False,
    ),
    (
        "suppress aliased name",
        "    with sq(AssertionError):\n        assert x != 1",
        False,
    ),
    # Cannot swallow a failing assert, so it must stay enforced.
    (
        "suppress other error",
        "    with contextlib.suppress(ValueError):\n        assert x != 1",
        True,
    ),
    (
        "suppress other error from import",
        "    with suppress(KeyError):\n        assert x != 1",
        True,
    ),
    # A context manager that is not a suppressor.
    (
        "unrelated context",
        "    with open('f') as fh:\n        assert x != 1",
        True,
    ),
    # Statically dead: the body never runs, so the assert is never evaluated.
    ("if False", "    if False:\n        assert x != 1", False),
    ("while False", "    while False:\n        assert x != 1", False),
    # The `else` of a falsy `if` is precisely the branch that *does* run.
    (
        "if False else",
        "    if False:\n        helper()\n    else:\n        assert x != 1",
        True,
    ),
    # A genuine runtime condition is not statically dead.
    ("runtime condition", "    if flag:\n        assert x != 1", True),
    # An assert moved into a nested def nothing calls never evaluates. A nested
    # def whose name is *loaded* is the callback shape and stays enforced.
    (
        "uncalled nested def",
        "    def inner():\n        assert x != 1",
        False,
    ),
    (
        "nested def used",
        "    def inner():\n        assert x != 1\n    return inner",
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    UNREACHABLE_SHAPES,
    ids=[shape[0] for shape in UNREACHABLE_SHAPES],
)
def test_reachability_rejects_exactly_the_shapes_that_cannot_fail(label, body, live):
    """An assert the interpreter can never fail is not an enforced contract.

    ``try/except``, ``with suppress(...)`` and a statically dead branch all keep
    the assert in the AST while making it incapable of failing, so a
    presence-based check certifies a contract that no longer exists (#287). The
    live controls carry equal weight: a wrong "defeated" verdict *removes* a
    real contract from the sentinel's view, which is the more damaging error.
    """
    # The body lines are already written with the indentation they need
    # relative to the function, so the header is the only line added here.
    # Indenting the whole body again is what produces the IndentationError this
    # repo has already mistaken for a caught mutation once.
    imports = (
        "    import contextlib\n"
        "    from contextlib import suppress\n"
        "    import contextlib as c\n"
        "    from contextlib import suppress as sq\n"
    )
    source = "def probe(x, flag, record, helper):\n" + imports + body + "\n"
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )


#: The residual defeats of #287: every spelling that keeps the assert in the AST
#: while making it incapable of failing, and the live control beside it that
#: must stay enforced.
#:
#: Each pair is one *behavioral* claim about the interpreter, not a guess about
#: what the checker ought to accept. ``with cs:`` where ``cs`` is a suppressor
#: really does swallow the failure and the test stays green; the same suppression
#: reached through ``.__enter__()`` raises ``TypeError`` instead, so that one is
#: listed as a control. Guessing wrong here is what would have shipped a rule
#: either missing the real defect or crying wolf on live asserts.
RESIDUAL_DEFEAT_SHAPES = (
    # A parameter has no import to resolve, so the dotted path is a bare name
    # and the resolution rule finds nothing to match.
    (
        "suppress passed as a parameter",
        (
            "    def probe(suppress):\n        with suppress(AssertionError):\n"
            "            assert x != 1"
        ),
        False,
    ),
    # ExitStack defers the suppression past the with header, into the body.
    (
        "enter_context defers past the header",
        (
            "    with ExitStack() as stack:\n"
            "        stack.enter_context(suppress(AssertionError))\n"
            "        assert x != 1"
        ),
        False,
    ),
    # pytest.raises(AssertionError) turns the failure into a *pass*: the raise
    # happens, is caught, matches, and the block ends normally.
    (
        "pytest.raises catches the assert",
        "    with pytest.raises(AssertionError):\n        assert x != 1",
        False,
    ),
    (
        "pytest.raises Exception",
        "    with pytest.raises(Exception):\n        assert x != 1",
        False,
    ),
    (
        "pytest.raises BaseException",
        "    with pytest.raises(BaseException):\n        assert x != 1",
        False,
    ),
    # A tuple argument is the ordinary spelling on the real file; reading only
    # the tuple node would report it as unreadable and thus universal, which
    # would drop four live pinned sites.
    (
        "pytest.raises tuple naming AssertionError",
        "    with pytest.raises((TypeError, AssertionError)):\n        assert x != 1",
        False,
    ),
    # `match=` is undecidable, not loud: whether the block ends green depends
    # on the assertion's own message at runtime. Measured with match="", ".*"
    # (both green) and match="1 == 2" (pytest re-raises). The conservative
    # reading is taken -- an assert that is *sometimes* unenforceable is not a
    # contract that can be relied on.
    (
        "pytest.raises AssertionError with a matching regex",
        "    with pytest.raises(AssertionError, match=''):\n        assert x != 1",
        False,
    ),
    # The alias form. This is the *silent* one: entering the object directly
    # swallows, whereas the __enter__ dunder returns None and raises TypeError.
    (
        "suppressor bound to a name",
        "    cs = contextlib.suppress(AssertionError)\n    with cs:\n        assert x != 1",
        False,
    ),
    (
        "aliased suppressor Exception",
        "    cs = suppress(Exception)\n    with cs:\n        assert x != 1",
        False,
    ),
    # Order decides whether the alias is bound *yet*. Entering `cs` before the
    # assignment that defines it raises NameError on entry, so the test fails
    # loudly rather than passing quietly -- not a defeat. A rule that collects
    # every assignment in the function and ignores position would call this
    # dead and drop a live contract.
    (
        "with cs before the assignment binds it",
        ("    with cs:\n        assert x != 1\n    cs = contextlib.suppress(AssertionError)"),
        True,
    ),
    # The same alias with the assignment first is the silent defeat.
    (
        "with cs after the assignment binds it",
        ("    cs = contextlib.suppress(AssertionError)\n    with cs:\n        assert x != 1"),
        False,
    ),
    (
        "annotated suppressor alias",
        (
            "    cs: object = contextlib.suppress(BaseException)\n    with cs:\n"
            "        assert x != 1"
        ),
        False,
    ),
    # #308: an alias binds its name wherever it is written. Restricting the
    # walk to the function's top level reports each of these as *enforced*,
    # which is the damaging direction -- a swallowed assert certified as
    # load-bearing.
    (
        "suppressor alias bound in an if branch",
        (
            "    if flag:\n        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    # The three rows below differ from the one above in exactly one way: the
    # `with cs:` is written INSIDE the block that binds it, so the assignment
    # and the entry are part of a *single* top-level statement. The swallow is
    # identical -- executed, the assert never fails -- but to `function.body`
    # the binder and the header share one index, and a walk that applied
    # bindings only at the end of each top-level statement read the header
    # before the name existed. All three execute green and were reported
    # enforced; see the #311 review. `items` and `flag` are the enclosing
    # function's parameters, so the binder really is reachable.
    (
        "suppressor alias bound and entered inside the same if",
        (
            "    if flag:\n        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
        False,
    ),
    (
        "suppressor alias bound and entered inside the same for",
        (
            "    for _ in items:\n        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
        False,
    ),
    (
        "suppressor alias bound and entered inside the same try",
        (
            "    try:\n        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1\n    finally:\n        pass"
        ),
        False,
    ),
    # The control for the three rows above, and the reason the fix orders by
    # source position rather than by statement. Moving the `with` back to the
    # top level leaves the swallow intact, so it must still be reported a
    # defeat; the existing "bound in an if branch" row already covers that,
    # and this pins the direction of the *same* block from the other side.
    (
        "suppressor alias entered after an if that may not have run",
        (
            "    if flag:\n        cs = contextlib.suppress(AssertionError)\n"
            "        helper()\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    (
        "suppressor alias bound in a loop",
        (
            "    for item in items:\n        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    (
        "suppressor alias bound in a try body",
        (
            "    try:\n        cs = contextlib.suppress(AssertionError)\n"
            "    except Exception:\n        pass\n    with cs:\n        assert x != 1"
        ),
        False,
    ),
    (
        "suppressor alias bound in a nested with",
        (
            "    with helper.make():\n        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    # #308 criterion 1: two branches bind ONE name to DIFFERENT targets, so
    # which suppressor applies depends on the path taken. Resolving by source
    # order gives a verdict for one branch only; the name is unreadable and must
    # be reported as a defeat.
    (
        "same alias bound to different suppressors in two branches",
        (
            "    if flag:\n        cs = contextlib.suppress(AssertionError)\n"
            "    else:\n        cs = contextlib.suppress(ValueError)\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    # The ambiguity is between two *harmless* targets, so nothing on either
    # path swallows an assert. The rule still cannot prove that from the source
    # alone, and #308 accepts over-reporting as the safe direction -- but this
    # row pins that decision so the direction cannot be flipped silently.
    (
        "same alias bound to two unrelated suppressors in two branches",
        (
            "    if flag:\n        cs = contextlib.suppress(ValueError)\n"
            "    else:\n        cs = contextlib.suppress(TypeError)\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    # A literal container that is empty never enters its body. The loop
    # spelling of the `if False:` defeat.
    ("for over empty list", "    for _ in []:\n        assert x != 1", False),
    ("for over empty tuple", "    for _ in ():\n        assert x != 1", False),
    ("for over empty dict", "    for _ in {}:\n        assert x != 1", False),
    ("for unpack over empty list", "    for _, v in []:\n        assert x != 1", False),
    # A tuple with one falsy member still iterates once, so the assert runs.
    ("for over single falsy member", "    for _ in (0,):\n        assert x != 1", True),
    # --- controls: every one of these must stay enforced ---
    (
        "parameter suppress of an unrelated error",
        ("    def probe(suppress):\n        with suppress(ValueError):\n            assert x != 1"),
        True,
    ),
    (
        "pytest.raises of an unrelated error",
        "    with pytest.raises(RuntimeError):\n        assert x != 1",
        True,
    ),
    # The exact tuple spellings the real pinned file uses. If the tuple rule
    # regresses to "unreadable", these are the sites it would wrongly drop.
    (
        "pytest.raises TypeError ValueError",
        "    with pytest.raises((TypeError, ValueError)):\n        assert x != 1",
        True,
    ),
    (
        "pytest.raises KeyError ValueError RuntimeError",
        ("    with pytest.raises((KeyError, ValueError, RuntimeError)):\n        assert x != 1"),
        True,
    ),
    (
        "pytest.raises BaseExceptionGroup",
        "    with pytest.raises(BaseExceptionGroup):\n        assert x != 1",
        True,
    ),
    # With no expected type at all, pytest raises ValueError while building the
    # context object -- before the body runs. No assert is evaluated, so this is
    # not a silent defeat and must not be counted as one.
    (
        "pytest.raises with no expected type",
        "    with pytest.raises():\n        assert x != 1",
        True,
    ),
    (
        "pytest.raises with only a regex",
        "    with pytest.raises(match='nomatch'):\n        assert x != 1",
        True,
    ),
    # `contextlib.suppress()` with no argument is legal and suppresses
    # everything, unlike pytest.raises()'s empty call above.
    (
        "suppress with no exception type",
        "    with contextlib.suppress():\n        assert x != 1",
        False,
    ),
    # An unrelated enter_context on the same stack is not a suppression.
    (
        "unrelated enter_context",
        (
            "    with ExitStack() as stack:\n        stack.enter_context(helper.make())\n"
            "        assert x != 1"
        ),
        True,
    ),
    # A name bound to an ordinary call must not be assumed to suppress.
    (
        "name bound to an ordinary call",
        "    cs = helper.make()\n    with cs:\n        assert x != 1",
        True,
    ),
    (
        "aliased suppressor of an unrelated error",
        "    cs = contextlib.suppress(ValueError)\n    with cs:\n        assert x != 1",
        True,
    ),
    # A context manager parameter is the common legitimate shape.
    ("context manager parameter", "    with cm:\n        assert x != 1", True),
    # Non-empty loops reach their bodies.
    ("for over a list", "    for _ in [1]:\n        assert x != 1", True),
    (
        "for over a falsy member",
        "    for _ in (False, True):\n        assert x != 1",
        True,
    ),
    ("for over range", "    for _ in range(3):\n        assert x != 1", True),
    ("for over a name", "    for _ in items:\n        assert x != 1", True),
    # An empty loop that does not contain the assert is not a defeat of it.
    ("empty loop on a sibling", "    for _ in []:\n        helper()\n    assert x != 1", True),
    # A suppression that is not wrapped around the assert does not disarm it.
    (
        "suppression on a sibling statement",
        "    with contextlib.suppress(AssertionError):\n        helper()\n    assert x != 1",
        True,
    ),
    # A module used as a context manager is not a suppressor. This is the
    # control for SUPPRESSOR_SPELLINGS: taking every component of the dotted
    # path would put `contextlib` in the set and fire here.
    ("module object as a context manager", "    with contextlib:\n        assert x != 1", True),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    RESIDUAL_DEFEAT_SHAPES,
    ids=[shape[0] for shape in RESIDUAL_DEFEAT_SHAPES],
)
def test_the_residual_defeats_of_287_are_rejected(label, body, live):
    """The remaining #287 spellings must be classified by their runtime effect.

    A rule that is too narrow leaves a present-but-dead pinned assertion
    certified as load-bearing; a rule that is too wide drops a live contract
    from the sentinel's view, which is the more damaging of the two errors. The
    controls therefore carry as much weight as the defeats, and several of them
    exist specifically to catch a rule that has been over-generalized to fix a
    gap -- most importantly the tuple-typed ``pytest.raises`` spellings the real
    file actually uses.
    """
    imports = (
        "    import contextlib\n"
        "    from contextlib import suppress\n"
        "    from contextlib import ExitStack\n"
    )
    # Each body is already indented for a function body, so only the header is
    # added -- re-indenting the whole body is what has produced spurious
    # IndentationErrors in this repo's own probes.
    source = "def outer(x, cm, items, record, helper, flag):\n" + imports + body + "\n"
    tree = ast.parse(source)
    outer = tree.body[0]
    for statement in outer.body:
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement is not outer
        ):
            # A `def probe(...)` fixture makes the assert the *only* one, so
            # the nested-def rule cannot confound the suppression verdict.
            function = statement
            break
    else:
        function = outer
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )


#: #308 named two escapes and a third that only appeared once #309 landed. Each
#: row is one *behavioural* claim about the interpreter, established by running
#: the shape rather than by reading the checker: a suppressor bound inline in the
#: header really does swallow the failure, so the assert is dead.
WALRUS_SHAPES = (
    # The binding and the entry are the same node, so nothing that tracks
    # `ast.Assign` ever sees the name and the header holds no bare `Name` to
    # resolve. Executed, the failure is swallowed.
    (
        "walrus binds and enters a suppressor",
        "    with (cs := contextlib.suppress(AssertionError)):\n        assert x != 1",
        False,
    ),
    (
        "walrus binds a broad suppressor",
        "    with (cs := contextlib.suppress(Exception)):\n        assert x != 1",
        False,
    ),
    (
        "walrus binds an assertion-capturing context",
        "    with (cs := pytest.raises(AssertionError)):\n        assert x != 1",
        False,
    ),
    (
        "walrus binds a suppressor by from-import",
        "    with (cs := suppress(AssertionError)):\n        assert x != 1",
        False,
    ),
    # A walrus binds its name for the rest of the enclosing scope, not just for
    # its own header, so a *later* `with cs:` re-enters the same suppressor.
    # Both asserts are really swallowed. These rows are the regression test for
    # the defect found in review of `b712beb`: the binding was recorded only for
    # headers enclosing the queried assert, so the second header never saw it and
    # the second assert was certified load-bearing -- a defeated assert reported
    # live, which is the damaging direction.
    (
        "walrus binding is re-entered by a later header",
        (
            "    with (cs := contextlib.suppress(AssertionError)):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        False,
    ),
    (
        "walrus binding re-entered after an intervening statement",
        (
            "    with (cs := suppress(AssertionError)):\n"
            "        assert x != 1\n"
            "    record.append(1)\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        False,
    ),
    # Carrying a walrus forward must carry the *suppression* forward, not the
    # name. The rows above re-enter a real suppressor and must read defeated;
    # these re-enter a walrus that was never a suppression, and re-entering it
    # genuinely raises, so they must stay live.
    #
    # These are the regression test for a defect the carry-forward itself
    # introduced: the binding was carried whatever its value, so a later
    # `with cs:` resolved a `nullcontext()`/non-call alias as a suppressor and
    # reported a live contract as defeated -- the damaging direction again, and
    # a `TypeError` crash for a non-call value. The single-statement controls
    # further down the table cannot catch this, because there the carried value
    # is never re-entered by a later header.
    (
        "non-suppressor walrus re-entered by a later header stays live",
        (
            "    with (cs := nullcontext()):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        True,
    ),
    (
        "non-call walrus re-entered by a later header stays live",
        ("    with (cs := 1):\n        pass\n    with cs:\n        assert x != 1"),
        True,
    ),
    (
        "non-suppressor helper walrus re-entered by a later header stays live",
        ("    with (cs := helper.make()):\n        pass\n    with cs:\n        assert x != 1"),
        True,
    ),
    # The carry-forward is scoped to the function that performs the binding.
    # A walrus inside a nested `def` binds a name in *that* scope, so an outer
    # `with cs:` never receives the suppressor and raises `NameError`. This is
    # the regression test for a defect the carry-forward introduced:
    # `ast.walk` descends into the nested body, so the binding escaped its
    # scope and the outer assert was reported swallowed -- damaging direction.
    (
        "walrus bound in a nested def does not leak to the outer scope",
        (
            "    def inner():\n"
            "        with (cs := contextlib.suppress(AssertionError)):\n"
            "            pass\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        True,
    ),
    # --- controls -----------------------------------------------------------
    # A zero-argument call reads as the unreadable case `BaseException` in
    # `_suppression_names`, and `BaseException` *does* catch `AssertionError`.
    # Classifying every walrus as a suppression therefore reports these two live
    # context managers as defeats, which is why the rule is gated on
    # `_is_suppression_call` rather than applied to any inline binding.
    (
        "walrus of a zero-argument context manager",
        "    with (cs := nullcontext()):\n        assert x != 1",
        True,
    ),
    (
        "walrus of a zero-argument helper",
        "    with (cs := helper.make()):\n        assert x != 1",
        True,
    ),
    (
        "walrus of an unrelated error",
        "    with (cs := contextlib.suppress(ValueError)):\n        assert x != 1",
        True,
    ),
    # A walrus whose value is not even a call cannot be a suppressor.
    ("walrus of a non-call value", "    with (cs := 1):\n        assert x != 1", True),
    # A bare name in the header is the already-closed alias case, not a walrus.
    ("bare name in the header", "    with cs:\n        assert x != 1", True),
)


#: #310: the ``.__enter__()`` dunder spelling, which shipped in #309 with no
#: test at all. Reverting the rule left the suite green, so these rows exist
#: first and foremost to make that impossible.
#:
#: The rule had a second defect, found by running it: it read the exception
#: types from the *``__enter__()``* call's arguments instead of the suppressor's.
#: The outer call has none, so every dunder spelling fell back to the unreadable
#: case ``BaseException`` -- which catches ``AssertionError`` -- and reported
#: ``suppress(ValueError).__enter__()`` as a defeat of the assert. The
#: ``ValueError``/``RuntimeError`` rows below are that defect's regression test.
#:
#: The loud-vs-silent question is settled by measurement, not taste: *any*
#: ``X.__enter__()`` returns ``None``, so the ``with`` body never runs and
#: ``TypeError`` is raised. The dunder form is therefore never a silent defeat.
#: The rule still flags it, and that is a deliberate conservative choice --
#: the shape is broken, and reporting a broken contract as not-load-bearing is
#: the safe direction. It is recorded here so the choice is visible rather
#: than implied.
DUNDER_SPELLING_SHAPES = (
    (
        "qualified suppressor dunder",
        "    with contextlib.suppress(AssertionError).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "bare suppressor dunder",
        "    with suppress(AssertionError).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "suppressor dunder over Exception",
        "    with suppress(Exception).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "suppressor dunder over BaseException",
        "    with suppress(BaseException).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "suppressor dunder over a tuple naming AssertionError",
        (
            "    with contextlib.suppress((ValueError, AssertionError)).__enter__():\n"
            "        assert x != 1"
        ),
        False,
    ),
    (
        "aliased suppressor dunder",
        (
            "    cs = contextlib.suppress(AssertionError)\n"
            "    with cs.__enter__():\n        assert x != 1"
        ),
        False,
    ),
    # --- the argument is irrelevant: `__enter__` is `pass` ---
    # `contextlib.suppress.__enter__` is `def __enter__(self): pass`, so it
    # returns `None` for every instantiation and `with None:` raises TypeError
    # before the body runs -- measured for every one of these arguments, and
    # for the no-argument form. The family is therefore answered uniformly:
    # an argument-reading split here would report byte-identical runtimes
    # differently. These rows are not "controls" any more; they are the
    # evidence that the exception list earns no distinction.
    (
        "suppressor dunder over ValueError",
        "    with contextlib.suppress(ValueError).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "suppressor dunder over RuntimeError",
        "    with contextlib.suppress(RuntimeError).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "aliased suppressor dunder over ValueError",
        (
            "    cs = contextlib.suppress(ValueError)\n"
            "    with cs.__enter__():\n        assert x != 1"
        ),
        False,
    ),
    # A different dunder is not this spelling, and an unrelated object that
    # happens to define `__enter__` is never a suppressor.
    (
        "unrelated object dunder",
        "    with helper.make().__enter__():\n        assert x != 1",
        True,
    ),
    (
        "nullcontext dunder",
        "    with contextlib.nullcontext().__enter__():\n        assert x != 1",
        True,
    ),
    (
        "a different dunder on a real suppressor",
        "    with contextlib.suppress(AssertionError).__exit__():\n        assert x != 1",
        True,
    ),
    # `suppress()` with empty parens raises TypeError on `__enter__()` just
    # like the rest of the family, so this row agrees with them rather than
    # standing out from them.
    (
        "empty-parens suppressor dunder",
        "    with contextlib.suppress().__enter__():\n        assert x != 1",
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    WALRUS_SHAPES,
    ids=[shape[0] for shape in WALRUS_SHAPES],
)
def test_walrus_bound_suppressors_in_a_with_header_are_rejected(label, body, live):
    """A suppressor bound *in* the header is the same defeat, still missed.

    ``cs = suppress(...)`` and ``with (cs := suppress(...)):`` differ only in
    where the binding is written, and both swallow the assertion failure. The
    gap was real and silent: on master this shape reported a live defeat as
    *enforced*, certifying a disarmed contract as load-bearing.
    """
    source = (
        "def outer(x, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    import pytest\n" + body + "\n"
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    # `all()` would collapse a two-assert fixture to its *first* verdict, which
    # is exactly what the re-entry rows need: their first assert is correctly
    # dead and their second is the one that was mis-reported live, so
    # `all(results) is False` would pass even with the second verdict wrong.
    # Every assert in a row shares one expected verdict, so equality is exact
    # and a single divergent row cannot hide behind another.
    assert results == [live] * len(results), (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )


@pytest.mark.parametrize(
    "rebind",
    ["helper.make()", "nullcontext()", "1"],
    ids=["unreadable-call", "live-context-manager", "non-call"],
)
def test_a_store_retires_a_carried_walrus_whatever_it_binds(rebind):
    """A later store wins even when its value is not a readable suppressor.

    ``_assigned_suppressors`` records only names whose right-hand side is a
    *readable* suppressor, so a store to an ordinary call leaves no trace in its
    result. Testing that map to decide what a carried walrus has been
    superseded by therefore leaves the stale walrus in place:

        with (cs := contextlib.suppress(AssertionError)):
            assert 1 == 2      # swallowed
        with cs:
            assert 1 == 2      # swallowed
        cs = helper.make()      # the interpreter keeps THIS binding
        with cs:
            assert 1 == 2      # live -- an ordinary context manager

    The first two asserts are defeated and the third is a live contract, so the
    row cannot live in ``WALRUS_SHAPES``, which requires one shared verdict.
    """
    source = (
        "def outer(x, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    import pytest\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n"
        "    with cs:\n"
        "        assert x != 1\n"
        f"    cs = {rebind}\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 3, f"fixture declared {len(asserts)} asserts, expected 3"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False, False, True], (
        f"rebind to {rebind}: expected the two swallowed asserts unenforced and "
        f"the rebound one live, got {results}"
    )


#: Every statement that binds a name, as the source of one statement that
#: rebinds a name a carried suppressor walrus is still bound to, with the
#: verdict the *interpreter* produces for that fixture.
#:
#: The verdict is measured, not asserted by hand. An earlier hand-written
#: version of this table shipped three rows whose expected value was wrong --
#: they failed against correct code, which is the same failure as a row that
#: passes against broken code. This one is generated from an interpreter run.
#:
#: This table is the gate that was missing. The four round-1 repairs and the
#: position-aware retirement all passed 190 tests and a green mutation matrix
#: while `Assign`-only handling shipped ten damaging-direction regressions.
#: Every mutant in that matrix was chosen from the code under review, so
#: nothing ever asked the language what its binding forms are.
#:
#: Rows commented `False` are measured as genuinely defeated and the checker
#: agrees: an `except ... as cs:` that never fires leaves the carried walrus
#: standing, and a `del` under an untaken `else` never runs. Those rows are as
#: load-bearing as the `True` ones -- they pin that the fix does not
#: over-reach into the safe direction.
REBINDING_STATEMENTS = {
    "annassign": "cs: int = 1",  # True (TypeError)
    "assign": "cs = helper.make()",  # True (AssertionError)
    "async-for-target": (
        "async def drain():\n    async for cs in helper.stream():\n        pass\nawait drain()"
    ),  # False (swallowed)
    "augassign": "cs += helper.make()",  # True (TypeError)
    "delete": "del cs",  # True (UnboundLocalError)
    "delete-inside-a-block": "if x:\n    del cs",  # True (UnboundLocalError)
    "delete-inside-a-loop": "for _ in [1]:\n    del cs",  # True (UnboundLocalError)
    "delete-inside-a-try": (
        "try:\n    del cs\nexcept ValueError:\n    pass"
    ),  # True (UnboundLocalError)
    "delete-inside-a-while": "while x:\n    del cs",  # True (UnboundLocalError)
    "delete-two-targets": "del cs, other",  # True (UnboundLocalError)
    "except-as": "try:\n    pass\nexcept ValueError as cs:\n    pass",  # False (swallowed)
    "except-as-inside-a-block": (
        "if x:\n    try:\n        pass\n    except ValueError as cs:\n        pass"
    ),  # False (swallowed)
    "except-as-multiple-types": (
        "try:\n    pass\nexcept (ValueError, TypeError) as cs:\n    pass"
    ),  # False (swallowed)
    "for-as-target": (
        "for _ in [1]:\n    for cs in [helper.make()]:\n        pass"
    ),  # True (AssertionError)
    "for-else-target": (
        "for _ in [1]:\n    pass\nelse:\n    for cs in [helper.make()]:\n        pass"
    ),  # True (AssertionError)
    "for-target": "for cs in [helper.make()]:\n    pass",  # True (AssertionError)
    "for-target-inside-a-try": (
        "try:\n    for cs in [helper.make()]:\n        pass\nexcept ValueError:\n    pass"
    ),  # True (AssertionError)
    "for-target-inside-a-with-body": (
        "with contextlib.nullcontext():\n    for cs in [helper.make()]:\n        pass"
    ),  # True (AssertionError)
    "for-tuple-target": (
        "for other, cs in [(1, helper.make())]:\n    pass"
    ),  # True (AssertionError)
    "list-unpack": "[other, cs] = [1, helper.make()]",  # True (AssertionError)
    "named-expression-on-another-name": (
        "if (check := helper.make()):\n    pass"
    ),  # False (swallowed)
    "named-expression-on-cs": "if (cs := helper.make()):\n    pass",  # True (AssertionError)
    "nested-tuple-unpack": "((other, cs),) = ((1, helper.make()),)",  # True (AssertionError)
    "starred-unpack": "(other, *cs) = (1, helper.make(), helper.make())",  # True (TypeError)
    "tuple-unpack": "(other, cs) = (1, helper.make())",  # True (AssertionError)
    "with-as": "with helper.make() as cs:\n    pass",  # True (TypeError)
    "with-as-and-a-second-item": (
        "with helper.make() as cs, helper.make():\n    pass"
    ),  # True (TypeError)
    "with-as-followed-by-another-with-as": (
        "with helper.make() as other:\n    with helper.make() as cs:\n        pass"
    ),  # True (TypeError)
    "with-as-inside-a-block": (
        "if x:\n    with helper.make() as cs:\n        pass"
    ),  # True (TypeError)
    "with-as-inside-a-loop": (
        "for _ in [1]:\n    with helper.make() as cs:\n        pass"
    ),  # True (TypeError)
    "with-as-inside-a-try": (
        "try:\n    with helper.make() as cs:\n        pass\nexcept ValueError:\n    pass"
    ),  # True (TypeError)
    "with-as-tuple": "with helper.make() as (other, cs):\n    pass",  # True (TypeError)
}

#: The same, for a rebind that runs *inside* the walrus's own `with` header.
#: The statement-level retirement runs before the walk that records walruses,
#: so it erases the very bind it is about to record; these rows pin that
#: position back.
REBINDS_INSIDE_A_HEADER = {
    "assign-inside-the-walrus-statement": (
        "__INSIDE_HEADER__cs = helper.make()"
    ),  # True (AssertionError)
    "delete-inside-the-walrus-statement": "__INSIDE_HEADER__del cs",  # True (UnboundLocalError)
    "for-target-inside-a-nested-with-item": (
        "with contextlib.nullcontext() as other:\n    with helper.make() as cs:\n        pass"
    ),  # True (measured)
    "for-target-inside-the-walrus-statement": (
        "__INSIDE_HEADER__for cs in [helper.make()]:\n    pass"
    ),  # True (AssertionError)
    "with-as-inside-the-walrus-statement": (
        "with helper.make() as cs:\n    pass"
    ),  # True (measured)
    # A name bound by `import ... as` rides on an `ast.alias`, and a name
    # bound by a `match` capture rides on a `MatchAs`/`MatchStar`/
    # `MatchMapping` string field. Neither is an `ast.Name` with a `Store`
    # ctx, so before this fix every row below was a live assert reported as
    # swallowed -- a regression against base `87a90da`, which gets all of
    # them right. Verdicts measured by executing each fixture.
    "import-as": "import os as cs",  # True (measured)
    "import-from-as": "from os import path as cs",  # True (measured)
    "match-as-capture": ("match [helper.make()]:\n    case [cs]:\n        pass"),  # True (measured)
    "match-as-on-another-name": (
        "match [helper.make()]:\n    case [other] as cs:\n        pass"
    ),  # True (measured)
    "match-star-capture": (
        "match [helper.make(), 2]:\n    case [_, *cs]:\n        pass"
    ),  # True (measured)
    "match-mapping-rest": (
        "match {'k': helper.make()}:\n    case {'k': _, **cs}:\n        pass"
    ),  # True (measured)
    "match-class-attribute": (
        "match helper.Pt():\n    case helper.Pt(cs):\n        pass"
    ),  # True (measured)
    "match-class-nested-capture": (
        "match [helper.Pt()]:\n    case [helper.Pt(inner=cs)]:\n        pass"
    ),  # True (measured)
    "match-inside-a-block": (
        "if x:\n    match [helper.make()]:\n        case [cs]:\n            pass"
    ),  # True (measured)
}


#: Marker for a rebind that has to be written *inside* the walrus's own
#: ``with`` header, rather than after it. Those rows are the ones the
#: statement-level retirement cannot see, because it erases the bind the walk
#: is about to record.
_INSIDE_HEADER = "__INSIDE_HEADER__"


def _rebind_source(body):
    """A fixture in which ``body`` rebinds a carried suppressor walrus."""
    prefix = "async def outer(x, helper):\n"
    prefix += "    import contextlib\n"
    prefix += "    from contextlib import suppress, nullcontext\n"
    prefix += "    import pytest\n"
    tail = "    with cs:\n        assert x != 1\n"
    if body.startswith(_INSIDE_HEADER):
        rebind = body[len(_INSIDE_HEADER) :]
        return (
            prefix
            + "    with (cs := contextlib.suppress(AssertionError)):\n"
            + textwrap.indent(rebind, "        ")
            + "\n"
            + tail
        )
    return (
        prefix
        + "    with (cs := contextlib.suppress(AssertionError)):\n"
        + "        pass\n"
        + textwrap.indent(body, "    ")
        + "\n"
        + tail
    )


def _verdicts(source):
    """The checker's per-assert verdicts for one fixture source."""
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"fixture declared {len(asserts)} asserts, expected 1"
    return [_is_enforced(function, node, tree) for node in asserts]


@pytest.mark.parametrize(
    ("label", "body", "live"),
    [
        ("annassign", "cs: int = 1", True),
        ("assign", "cs = helper.make()", True),
        (
            "async-for-target",
            "async def drain():\n    async for cs in helper.stream():\n        pass\nawait drain()",
            False,
        ),
        ("augassign", "cs += helper.make()", True),
        ("delete", "del cs", True),
        ("delete-inside-a-block", "if x:\n    del cs", True),
        ("delete-inside-a-loop", "for _ in [1]:\n    del cs", True),
        ("delete-inside-a-try", "try:\n    del cs\nexcept ValueError:\n    pass", True),
        ("delete-inside-a-while", "while x:\n    del cs", True),
        ("delete-two-targets", "del cs, other", True),
        ("except-as", "try:\n    pass\nexcept ValueError as cs:\n    pass", False),
        (
            "except-as-inside-a-block",
            "if x:\n    try:\n        pass\n    except ValueError as cs:\n        pass",
            False,
        ),
        (
            "except-as-multiple-types",
            "try:\n    pass\nexcept (ValueError, TypeError) as cs:\n    pass",
            False,
        ),
        ("for-as-target", "for _ in [1]:\n    for cs in [helper.make()]:\n        pass", True),
        (
            "for-else-target",
            "for _ in [1]:\n    pass\nelse:\n    for cs in [helper.make()]:\n        pass",
            True,
        ),
        ("for-target", "for cs in [helper.make()]:\n    pass", True),
        (
            "for-target-inside-a-try",
            "try:\n    for cs in [helper.make()]:\n        pass\nexcept ValueError:\n    pass",
            True,
        ),
        (
            "for-target-inside-a-with-body",
            "with contextlib.nullcontext():\n    for cs in [helper.make()]:\n        pass",
            True,
        ),
        ("for-tuple-target", "for other, cs in [(1, helper.make())]:\n    pass", True),
        ("list-unpack", "[other, cs] = [1, helper.make()]", True),
        ("named-expression-on-another-name", "if (check := helper.make()):\n    pass", False),
        ("named-expression-on-cs", "if (cs := helper.make()):\n    pass", True),
        ("nested-tuple-unpack", "((other, cs),) = ((1, helper.make()),)", True),
        ("starred-unpack", "(other, *cs) = (1, helper.make(), helper.make())", True),
        ("tuple-unpack", "(other, cs) = (1, helper.make())", True),
        ("with-as", "with helper.make() as cs:\n    pass", True),
        ("with-as-and-a-second-item", "with helper.make() as cs, helper.make():\n    pass", True),
        (
            "with-as-followed-by-another-with-as",
            "with helper.make() as other:\n    with helper.make() as cs:\n        pass",
            True,
        ),
        ("with-as-inside-a-block", "if x:\n    with helper.make() as cs:\n        pass", True),
        (
            "with-as-inside-a-loop",
            "for _ in [1]:\n    with helper.make() as cs:\n        pass",
            True,
        ),
        (
            "with-as-inside-a-try",
            "try:\n    with helper.make() as cs:\n        pass\nexcept ValueError:\n    pass",
            True,
        ),
        ("with-as-tuple", "with helper.make() as (other, cs):\n    pass", True),
        # A name bound by `import ... as` rides on an `ast.alias`; a name bound
        # by a `match` capture rides on a `MatchAs`/`MatchStar`/`MatchMapping`
        # string field. Neither is an `ast.Name` with a `Store` ctx, so before
        # this fix every row below was a LIVE assert reported as swallowed --
        # a regression against base `87a90da`, which gets all of them right.
        # Every `live` value here is measured by executing the fixture.
        ("import-as", "import os as cs", True),
        ("import-from-as", "from os import path as cs", True),
        ("match-as-capture", "match [helper.make()]:\n    case [cs]:\n        pass", True),
        (
            "match-as-on-another-name",
            "match [helper.make()]:\n    case [other] as cs:\n        pass",
            True,
        ),
        (
            "match-star-capture",
            "match [helper.make(), 2]:\n    case [_, *cs]:\n        pass",
            True,
        ),
        (
            "match-mapping-rest",
            "match {'k': helper.make()}:\n    case {'k': _, **cs}:\n        pass",
            True,
        ),
        (
            "match-class-attribute",
            "match helper.Pt():\n    case helper.Pt(cs):\n        pass",
            True,
        ),
        (
            "match-class-nested-capture",
            "match [helper.Pt()]:\n    case [helper.Pt(inner=cs)]:\n        pass",
            True,
        ),
        (
            "match-inside-a-block",
            "if x:\n    match [helper.make()]:\n        case [cs]:\n            pass",
            True,
        ),
    ],
    ids=[
        "annassign",
        "assign",
        "async-for-target",
        "augassign",
        "delete",
        "delete-inside-a-block",
        "delete-inside-a-loop",
        "delete-inside-a-try",
        "delete-inside-a-while",
        "delete-two-targets",
        "except-as",
        "except-as-inside-a-block",
        "except-as-multiple-types",
        "for-as-target",
        "for-else-target",
        "for-target",
        "for-target-inside-a-try",
        "for-target-inside-a-with-body",
        "for-tuple-target",
        "list-unpack",
        "named-expression-on-another-name",
        "named-expression-on-cs",
        "nested-tuple-unpack",
        "starred-unpack",
        "tuple-unpack",
        "with-as",
        "with-as-and-a-second-item",
        "with-as-followed-by-another-with-as",
        "with-as-inside-a-block",
        "with-as-inside-a-loop",
        "with-as-inside-a-try",
        "with-as-tuple",
        "import-as",
        "import-from-as",
        "match-as-capture",
        "match-as-on-another-name",
        "match-star-capture",
        "match-mapping-rest",
        "match-class-attribute",
        "match-class-nested-capture",
        "match-inside-a-block",
    ],
)
def test_every_binding_form_retires_a_carried_walrus(label, body, live):
    """A rebind of any form must retire the carried walrus bound to that name.

    ``_names_bound_by`` originally read only ``Assign``, ``AnnAssign``,
    ``AugAssign`` and ``NamedExpr``. Every other form in the language -- a
    ``for`` target, a ``with ... as ...`` target, an ``except ... as ...``
    name, a ``del``, and any of those reached through a tuple, list or star --
    bound a name without retiring the carried binding, so the ``with cs:`` that
    re-entered it was still read as the old suppressor and a live assert was
    reported swallowed.

    The expected verdict is the interpreter's, measured per row.
    """
    results = _verdicts(_rebind_source(body))
    assert results == [live], (
        f"{label}: the interpreter says this form is "
        f"{'live' if live else 'defeated'}; got {results}"
    )


def test_a_walrus_in_a_class_body_does_not_escape_the_class():
    """A class body is a nested scope, so its walrus binds the class local.

    The rows above all rebind a walrus the *enclosing* function bound. This one
    is the opposite: the only suppressor alias in the function lives on a
    ``class`` local, so the ``with cs:`` that re-enters the name raises
    ``NameError`` before the assert is reached and the assert is live.

    ``_nested_scope_nodes`` excluded ``def``/``async def``/``lambda`` but not
    ``class``, so the class-local alias was carried as though the enclosing
    function had bound it, and the head reported this live assert as defeated:

        def outer(x, helper):
            class Inner:
                with (cs := contextlib.suppress(AssertionError)):
                    pass
            with cs:               # NameError: `cs` is `Inner`'s local
                assert x != 1      # live

    This is a *damaging* direction, not the safe over-breadth the ceiling rows
    pin: reporting a live contract as defeated stops it protecting anything.
    The expected verdict is the interpreter's, measured.
    """
    source = (
        "async def outer(x, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    import pytest\n"
        "    class Inner:\n"
        "        with (cs := contextlib.suppress(AssertionError)):\n"
        "            pass\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    results = _verdicts(source)
    assert results == [True], (
        "a class-body walrus binds the class local, so the enclosing `with cs:` "
        f"raises NameError and the assert is live; got {results}"
    )


@pytest.mark.parametrize(
    ("label", "name", "statement", "live"),
    [
        # A bare `import a.b` binds `a`, not `b`, and a bare `from a import b`
        # binds `b`. Both with `asname is None`, so reading only the asname
        # left the carried walrus in place and a live assert was reported as
        # defeated. Base `87a90da` gets all of these right.
        # See `ledger/ROUND4_BARE_IMPORT_FINDING_20260927T1910Z.md`.
        ("bare-import", "os", "import os", True),
        ("bare-dotted-import", "xml", "import xml.etree", True),
        ("bare-from-import", "path", "from os import path", True),
        ("bare-from-dotted-import", "etree", "from xml import etree", True),
        # A single import statement binds every alias it names, not just the
        # first. Nothing pinned that: the round-5 ALT's M19 sliced
        # `node.names[:1]`, which left the *second* alias's carried walrus
        # alive and passed the whole 448-test lane green.
        ("multi-alias-second-import", "sys", "import os, sys", True),
        ("multi-alias-second-from-import", "sep", "from os import path, sep", True),
        ("multi-alias-first-still-binds", "os", "import os, sys", True),
        # The negative case, and the reason the bound name is resolved per
        # alias rather than "any import retires anything": an aliased import
        # binds a *different* name, so it must leave this walrus alone and the
        # assert stays swallowed.
        ("aliased-import-binds-another-name", "os", "import os as os2", False),
        ("aliased-from-import-binds-another-name", "os", "from os import path as p", False),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_bare_import_binds_a_name_and_retires_the_carried_walrus(label, name, statement, live):
    """An import binds a name even without `as`, and must retire that walrus.

    ``_bound_targets``' import branch collected only aliases carrying an
    explicit ``asname``, so a *bare* import -- which also binds a name -- was
    invisible. The module object then overwrote the suppressor alias, ``with
    <name>:`` raised ``TypeError`` before the assert, and the checker called
    the live assert defeated:

        async def outer(x, helper):
            with (os := contextlib.suppress(AssertionError)):
                pass
            import os          # `os` is now the module
            with os:           # TypeError, assert never runs
                assert x != 1  # live

    The bound name depends on the form: ``import a.b`` binds the head `a``
    because the submodules are attributes of the bound package, while
    ``from a import b`` binds `b`. An ``as`` overrides both.
    """
    source = (
        "async def outer(x, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    import pytest\n"
        f"    with ({name} := contextlib.suppress(AssertionError)):\n"
        "        pass\n"
        f"    {statement}\n"
        f"    with {name}:\n"
        "        assert x != 1\n"
    )
    results = _verdicts(source)
    assert results == [live], (
        f"{label}: the interpreter says this form is "
        f"{'live' if live else 'defeated'}; got {results}"
    )


def test_every_binding_node_type_is_reachable_from_the_walk():
    """No AST node that binds a name may be invisible to the walk.

    Three review rounds on this branch each found a *new* binding carrier the
    walk could not see, because each repair enumerated only the forms its
    author happened to think of:

    * round 3, `082ceae` — `alias.asname` and the `match` capture fields
    * round 4, `70f3b8a` — bare imports, where `asname is None`
    * round 5, `8de286c` — `def`/`async def`/`class`/`type X = ...`

    That pattern does not stop by patching one more form, so this test closes
    it by construction: it samples one statement per node type that binds a
    name and fails if any of them binds nothing the walk can see. A new
    binding form in the language, or a new one added here, cannot be added
    without coverage. That is precisely the defect class of the three heads
    above.

    Two forms are deliberately absent, each with a reason rather than a
    silent omission:

    * `ExceptHandler` — a documented accepted exception. The compiler unbinds
      the name when the handler exits, so it cannot survive to a later `with`.
      The walk still *reports* it, so listing it here would assert the opposite
      of what the language does.
    * `AsyncFor` — `async for` is only legal inside an `async def`, so its
      target always binds in a nested scope that the walk correctly excludes.
      It is covered from the other direction by the `async-for-target` row of
      `test_every_binding_form_retires_a_carried_walrus`.
    """
    samples = {
        "Assign": "cs = 1",
        "AnnAssign": "cs: int = 1",
        "AugAssign": "cs += 1",
        "NamedExpr": "(cs := 1)",
        "Delete": "del cs",
        "For": "for cs in []:\n    pass",
        "withitem": "with helper.make() as cs:\n    pass",
        "Import": "import os as cs",
        "ImportFrom": "from os import path as cs",
        "MatchAs": "match [1]:\n    case [cs]:\n        pass",
        "MatchStar": "match [1]:\n    case [*cs]:\n        pass",
        "MatchMapping": "match {}:\n    case {**cs}:\n        pass",
        "FunctionDef": "def cs(): pass",
        "AsyncFunctionDef": "async def cs(): pass",
        "ClassDef": "class cs: pass",
    }
    if _TYPE_ALIAS is not None:
        samples["TypeAlias"] = "type cs = int"

    invisible = [
        label
        for label, sample in samples.items()
        if "cs" not in _names_bound_by(ast.parse(textwrap.dedent(sample)).body[0])
    ]
    assert not invisible, (
        "these binding forms bind `cs` but the walk cannot see it, so a carried "
        f"walrus would survive them: {invisible}"
    )


@pytest.mark.parametrize(
    ("label", "definition", "live"),
    [
        ("def", "def cs(): pass", True),
        ("async-def", "async def cs(): pass", True),
        ("class", "class cs: pass", True),
        ("type-alias", "type cs = int", True),
        # The negative cases, and the reason the name is read off the
        # definition itself rather than assuming any definition retires
        # anything: these bind a *different* name, so the carried suppressor
        # must survive and the assert stays swallowed.
        ("def-binds-another-name", "def other(): pass", False),
        ("class-binds-another-name", "class other: pass", False),
        (
            "a-body-bind-does-not-escape",
            "def other():\n        cs = helper.make()",
            False,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_definition_binds_its_name_and_retires_the_carried_walrus(label, definition, live):
    """A `def`/`class`/`type` statement binds a name and must retire that walrus.

    A definition binds its name in the *enclosing* scope, so a carried
    suppressor it names is overwritten. None of the three is a context
    manager, so the later ``with cs:`` raises ``TypeError`` before the assert,
    and the assert is live:

        async def outer(x):
            with (cs := contextlib.suppress(AssertionError)):
                pass
            def cs(): pass    # `cs` is now a function
            with cs:           # TypeError, the assert never runs
                assert x != 1  # live

    ``def`` and ``async def`` are also nested *scopes*, and that exclusion is
    why this took a second fix: ``_nested_scope_nodes`` drops the whole
    subtree, the name included, so the branch in ``_bound_targets`` was never
    reached for them. The name is recorded from the excluded node in
    ``_binding_targets_by_name`` instead. The definition's *body* stays
    excluded, which the last row pins -- a bind inside the body is that
    scope's, not the enclosing function's.

    ``type cs = int`` is 3.12+ and this package supports 3.11, so its node
    type is resolved through a ``getattr`` guard rather than referenced bare.
    """
    if label == "type-alias" and not hasattr(ast, "TypeAlias"):
        pytest.skip("`type X = ...` syntax and ast.TypeAlias need Python 3.12")
    source = (
        "async def outer(x, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    import pytest\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        pass\n" + textwrap.indent(definition, "    ") + "\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    results = _verdicts(source)
    assert results == [live], (
        f"{label}: the interpreter says this form is "
        f"{'live' if live else 'defeated'}; got {results}"
    )


@pytest.mark.parametrize(
    ("label", "body", "live"),
    [
        ("assign-inside-the-walrus-statement", "__INSIDE_HEADER__cs = helper.make()", True),
        ("delete-inside-the-walrus-statement", "__INSIDE_HEADER__del cs", True),
        (
            "for-target-inside-a-nested-with-item",
            "with contextlib.nullcontext() as other:\n    with helper.make() as cs:\n        pass",
            True,
        ),
        (
            "for-target-inside-the-walrus-statement",
            "__INSIDE_HEADER__for cs in [helper.make()]:\n    pass",
            True,
        ),
        ("with-as-inside-the-walrus-statement", "with helper.make() as cs:\n    pass", True),
    ],
    ids=[
        "assign-inside-the-walrus-statement",
        "delete-inside-the-walrus-statement",
        "for-target-inside-a-nested-with-item",
        "for-target-inside-the-walrus-statement",
        "with-as-inside-the-walrus-statement",
    ],
)
def test_a_rebind_inside_the_walrus_own_header_retires_it(label, body, live):
    """A rebind inside the walrus's own ``with`` has to retire it as well.

    The statement-level retirement runs before the walk that records walruses,
    so it erases the bind it is about to record, and a bind in that header's
    body runs strictly after the header bound the name:

        with (cs := contextlib.suppress(AssertionError)):
            cs = helper.make()     # runs after the header bound `cs`
        with cs:                   # ...so the walrus must not survive
            assert 1 == 2          # live -- an ordinary context manager

    An earlier version reported this ``[False]``: a live contract certified as
    swallowed, and a regression against the base it was written to fix.
    """
    results = _verdicts(_rebind_source(body))
    assert results == [live], (
        f"{label}: a rebind that runs after the walrus header bound the name "
        f"must retire it; the interpreter says "
        f"{'live' if live else 'defeated'}; got {results}"
    )


#: Rows where the checker's answer is `True` but the interpreter really does
#: swallow the assert -- the SAFE direction, because an over-reported live
#: contract drops no assert from the sentinel's view. Each is measured to be
#: *unchanged from base `87a90da`*, which is what makes them acceptable here:
#: they are pre-existing over-breadth, owned by #287, and this fix neither
#: introduces nor widens them.
#:
#: The complementary rows matter just as much. Pining only the safe direction
#: would leave a future change free to break these without anything going red,
#: and these shapes are exactly where a scope fix could silently over-reach.
SAFE_DIRECTION_ROWS = (
    (
        "a del under an untaken else",
        "if x:\n    pass\nelse:\n    del cs",
    ),
    (
        "a with-as target in a class body",
        "class Inner:\n    with helper.make() as cs:\n        pass",
    ),
    (
        "a walrus inside a comprehension",
        "check = [cs for _ in [1] if (cs := contextlib.suppress(AssertionError))]",
    ),
    (
        "an untaken if",
        "if False:\n    check = (cs := contextlib.suppress(AssertionError))",
    ),
    (
        "a zero-iteration for",
        "for _ in []:\n    check = (cs := contextlib.suppress(AssertionError))",
    ),
    (
        "an except-as name is deleted when the handler exits",
        "try:\n    helper.make()\nexcept ValueError as cs:\n    cs = helper.make()",
    ),
)


@pytest.mark.parametrize(
    ("label", "body"),
    [(label, body) for label, body in SAFE_DIRECTION_ROWS],
    ids=[label for label, _body in SAFE_DIRECTION_ROWS],
)
def test_the_safe_direction_rows_stay_unchanged_from_base(label, body):
    """The over-breadth this fix must not widen, pinned as-is.

    Every row here is a shape where the interpreter swallows the assert and the
    checker calls it live anyway. That is the conservative answer, and it is
    what ``87a90da`` already produced -- so the requirement is not "be right"
    (that is #287's) but "do not get worse". Asserting ``[True]`` pins the
    ceiling: if a later scope change makes any of these ``[False]``, a defeated
    assert has been promoted to a load-bearing one and this row fails.

    The same rows were verified against the base commit by the lead; see
    ``ledger/R323_ROUND3_EVIDENCE.md``.
    """
    source = _rebind_source(body)
    results = _verdicts(source)
    assert results == [True], (
        f"{label}: this shape is pre-existing over-breadth in the SAFE direction "
        f"(the interpreter swallows the assert and the checker reports it live). "
        f"Pin the ceiling at [True] rather than widening it; got {results}"
    )


@pytest.mark.parametrize(
    ("label", "body", "live"),
    DUNDER_SPELLING_SHAPES,
    ids=[shape[0] for shape in DUNDER_SPELLING_SHAPES],
)
def test_the_dunder_spelling_is_covered_and_scoped_to_real_suppression(label, body, live):
    """#310: the dunder rule must be exercised, and must key on the exception.

    The rule shipped untested, so it could be deleted without any test going
    red. These rows fix that, and they also pin the scope: a suppressor dunder
    over an exception that does *not* catch ``AssertionError`` is a live
    contract, and flagging it would drop a real assert from the sentinel's
    view.
    """
    imports = (
        "    import contextlib\n"
        "    import pytest\n"
        "    from contextlib import suppress\n"
        "    from contextlib import ExitStack\n"
    )
    source = "def outer(x, cm, items, record, helper):\n" + imports + body + "\n"
    tree = ast.parse(source)
    outer = tree.body[0]
    for statement in outer.body:
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement is not outer
        ):
            function = statement
            break
    else:
        function = outer
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )


#: #308 criterion 1 asks that branch order never decide the verdict. Asserting a
#: single source order would not catch a rule that resolved by order and
#: happened to be right for that order, so both are pinned and required to
#: agree.
DISAGREEING_BRANCH_ORDERS = (
    (
        "import in the if branch",
        (
            "    if flag:\n"
            "        from contextlib import suppress\n"
            "    else:\n"
            "        suppress = contextlib.suppress\n"
            "    with suppress(AssertionError):\n"
            "        assert x != 1"
        ),
    ),
    (
        "import in the else branch",
        (
            "    if flag:\n"
            "        suppress = contextlib.suppress\n"
            "    else:\n"
            "        from contextlib import suppress\n"
            "    with suppress(AssertionError):\n"
            "        assert x != 1"
        ),
    ),
)


@pytest.mark.parametrize(
    ("label", "body"),
    DISAGREEING_BRANCH_ORDERS,
    ids=[shape[0] for shape in DISAGREEING_BRANCH_ORDERS],
)
def test_branch_order_never_decides_the_alias_verdict(label, body):
    """A name bound to different targets in two branches must be unreadable.

    ``_own_imports`` collects bindings with a LIFO ``stack.pop()``, so with two
    branches binding the same alias the surviving binding depends on source
    order and one of the two orders reaches the wrong verdict. Treating the
    ambiguous name as suppressing is what makes the answer order-independent --
    and over-reporting is the safe direction, since the alternative is
    certifying a disarmed assert as load-bearing.
    """
    source = "def outer(x, flag):\n    import contextlib\n" + body + "\n"
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert not any(results), f"{label}: a swallowed assert was reported enforced, got {results}"


def test_a_name_bound_to_different_suppressors_is_ambiguous_not_ordered():
    """Two readable suppressors under one name cannot be resolved by order.

    A single merged dict would keep whichever binding was collected last and
    report a verdict for one path only. The alias is therefore recorded as
    ambiguous, which is read as suppressing -- the over-reporting direction
    #308 criterion 1 accepts.
    """
    source = (
        "def outer(x, flag):\n"
        "    import contextlib\n"
        "    if flag:\n"
        "        cs = contextlib.suppress(AssertionError)\n"
        "    else:\n"
        "        cs = contextlib.suppress(ValueError)\n"
        "    with cs:\n"
        "        assert x != 1"
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert not any(results), f"ambiguous alias resolved by order, got {results}"


def test_plain_assignment_aliasing_needs_no_import_node():
    """``cs = contextlib.suppress(...)`` is an alias with no import to resolve.

    #308 criterion 2. The assignment produces no ``Import``/``ImportFrom`` node,
    so a resolver that only reads imports has nothing to match and the swallowed
    assert reads as enforced. The control proves the rule keys on the
    right-hand side rather than on the name.
    """
    defeat = (
        "def outer(x):\n"
        "    import contextlib\n"
        "    cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n"
        "        assert x != 1"
    )
    control = (
        "def outer(x):\n"
        "    import contextlib\n"
        "    cs = contextlib.json\n"
        "    with cs:\n"
        "        assert x != 1"
    )
    for label, source, expected in (
        ("suppressor alias", defeat, False),
        ("non-suppressor", control, True),
    ):
        tree = ast.parse(source)
        function = tree.body[0]
        asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
        results = [_is_enforced(function, node, tree) for node in asserts]
        assert all(results) is expected, f"{label}: expected {expected}, got {results}"


#: ``async with`` shapes. A suppressor reached through the *async* header is
#: loud at runtime -- neither ``contextlib.suppress`` nor ``pytest.raises``
#: implements ``__aenter__``, so entry raises ``TypeError`` before the body
#: runs. A *sync* ``with`` nested inside one is a different matter and is
#: still read, so the guard has to be scoped to the async header itself.
ASYNC_CONTEXT_SHAPES = (
    (
        "async with a suppressor",
        "    async with contextlib.suppress(AssertionError):\n        assert x != 1",
        True,
    ),
    (
        "async with pytest.raises",
        "    async with pytest.raises(AssertionError):\n        assert x != 1",
        True,
    ),
    (
        "async with a bare suppressor alias",
        ("    cs = contextlib.suppress(AssertionError)\n    async with cs:\n        assert x != 1"),
        True,
    ),
    (
        "sync with a suppressor nested in an async with",
        (
            "    async with helper.mgr():\n"
            "        with contextlib.suppress(AssertionError):\n            assert x != 1"
        ),
        False,
    ),
    (
        "sync with a suppressor alias nested in an async with",
        (
            "    async with helper.mgr():\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    ASYNC_CONTEXT_SHAPES,
    ids=[row[0] for row in ASYNC_CONTEXT_SHAPES],
)
def test_async_with_is_loud_but_a_sync_suppressor_inside_it_is_not(label, body, live):
    """``async with`` needs an async context manager, and suppressors are not.

    Measured on the pinned interpreter::

        async with contextlib.suppress(AssertionError):
            assert 1 == 2
        # TypeError: 'suppress' object does not support the asynchronous
        # context manager protocol

    The body never runs, so the assert is a live contract. Reading the async
    form as a suppression would drop a real assert from the sentinel's view.

    A *sync* ``with`` nested inside the ``async with`` is unaffected and must
    still be read, so the guard is scoped to the async header rather than to
    the enclosing function.
    """
    imports = "    import contextlib\n    import pytest\n    from contextlib import suppress\n"
    source = "async def outer(x, cm, items, record, helper):\n" + imports + body + "\n"
    tree = ast.parse(source)
    outer = tree.body[0]
    asserts = [node for node in ast.walk(outer) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(outer, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )


#: One block holding a ``with`` *before* the store and another *after* it. The
#: two asserts are opposite cases that sit under one top-level statement, so
#: they are pinned separately -- a rule that reports every ``with`` in the
#: block, or that reads the block's bindings before the position of the header,
#: gives both the same answer, and each of those answers is wrong in one
#: direction or the other.
SAME_BLOCK_ORDER_SHAPES = (
    (
        "if body holds both orders",
        (
            "    if p:\n        with cs:\n            assert x != 1\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
    ),
    (
        "nested with holds both orders",
        (
            "    with a():\n        with cs:\n            assert x != 1\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
    ),
    (
        "for body holds both orders",
        (
            "    for a in L:\n        with cs:\n            assert x != 1\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
    ),
)


@pytest.mark.parametrize(
    ("label", "body"),
    SAME_BLOCK_ORDER_SHAPES,
    ids=[row[0] for row in SAME_BLOCK_ORDER_SHAPES],
)
def test_both_orders_in_one_block_keep_their_own_verdict(label, body):
    """A ``with`` before the store and one after it must not share a verdict.

    The first raises `NameError` on entry, so its assert is a live contract.
    The second really is swallowed. Reporting the block uniformly gets one of
    the two wrong no matter which way, and both wrong answers are damaging --
    certifying a defeated assert, or dropping a live one.
    """
    imports = "    import contextlib\n    from contextlib import suppress\n"
    source = "def outer(x, cm, items, record, helper):\n" + imports + body + "\n"
    tree = ast.parse(source)
    outer = tree.body[0]
    asserts = [node for node in ast.walk(outer) if isinstance(node, ast.Assert)]
    assert len(asserts) == 2, f"{label}: expected two asserts to tell apart"
    results = [_is_enforced(outer, node, tree) for node in asserts]
    assert results == [True, False], (
        f"{label}: the assert before the store must stay enforced and the one "
        f"after it must be reported defeated, got {results}"
    )
