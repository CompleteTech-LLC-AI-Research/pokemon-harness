"""Structural guard wiring and retention-count sentinels.

Actual test functions and literal cases from the original collector.
"""

import ast
import inspect

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
    count_sites_that_bypass_the_guard,
    guard_is_wired_on_the_fast_clock_path,
    guard_rejects_the_deadline_terminal_state,
    observed_count_comparisons,
    retention_sites_observed,
    retention_subscript_sites_observed,
)


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
    that pins the entry point from both directions â€” it must fire, and it must
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
    # A leading tautology does NOT decide an `and`: Python continues past a
    # truthy operand, so `True and <comparison>` evaluates the comparison and
    # the assert fails when it is false. These two rows were pinned as
    # bypasses, which was wrong (a live comparison reported as dead).
    ("tautology and comparison", "assert (1 == 1) and x != 1", True),
    ("truthy literal and comparison", "assert True and x != 1", True),
    ("literal run and comparison", "assert True and True and x != 1", True),
    ("count tautology and comparison", "assert len(y) >= 0 and x != 1", True),
    # A nested bypass behind the leading literal is still one: the inner `or`
    # runs the comparison first, but its `True` operand masks a false result,
    # so that operand cannot fail and neither can the assert.
    ("literal and nested or bypass", "assert True and (x != 1 or True)", False),
    # A count that cannot be negative is a known tautology prefix too: it
    # passes, then the nested `or` masks the comparison behind it.
    ("count tautology and nested or bypass", "assert len(y) >= 0 and (x != 1 or True)", False),
    (
        "count tautologies and nested or bypass",
        "assert len(y) > -1 and len(y) >= 0 and (x != 1 or True)",
        False,
    ),
    (
        "deadline guard count prefix and nested or bypass",
        (
            'assert len(record.get("errors", [])) >= 0 and '
            '(record["termination"] != "cancelled_or_deadline" or True)'
        ),
        False,
    ),
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
    # comparison beside them cannot fail: a later one is skipped and an earlier
    # one runs with its false result masked. These are the shapes an earlier
    # version of _may_bypass missed by not recursing into Compare.
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
