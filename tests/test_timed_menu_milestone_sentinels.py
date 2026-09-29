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
import asyncio
import contextlib
import inspect
from types import SimpleNamespace

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
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
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


#: #324: a walrus in a ``with`` header binds the name for the rest of the
#: enclosing scope, so a *later* header that re-enters that name enters the
#: same suppressor. ``NamedExpr`` is not an ``ast.Assign``, so the binding was
#: never recorded and the re-entering assert was reported live -- a defeated
#: assert certified as load-bearing, which is the damaging direction.
#:
#: The rows below compare the full list of verdicts rather than reducing it
#: with ``all()``. Each fixture holds more than one assert whose verdicts are
#: *not* all the same, and ``all()`` collapses such a fixture to its first
#: verdict -- so a wrong second verdict passed the shipped table. That harness
#: bug is itself fixed here; the exact comparison is what keeps these rows
#: honest.
#
#: Every fixture is two- or three-assert on purpose: the first assert is the
#: one already covered by the single-header walrus rows, and the later
#: assert(s) are the ones this issue is about.
WALRUS_REENTRY_SHAPES = (
    # The defect itself. `cs` is still the suppressor at the second header, so
    # its assert is swallowed too. Both are defeated; the first assert being
    # already correct means `all()` would have passed while the second was
    # wrong.
    (
        "a walrus-bound suppressor is re-entered by a later header",
        (
            "    with (cs := suppress(AssertionError)):\n        assert x != 1\n"
            "    with cs:\n        assert x != 2"
        ),
        [False, False],
    ),
    # #324 criterion 1: the same must hold when the walrus is not written at
    # the top level. Inside an `if` the bind happens on one path only, but the
    # later header re-enters whatever was bound, and executed that is the
    # suppressor.
    (
        "a walrus inside an if is re-entered by a later header",
        (
            "    if flag:\n"
            "        with (cs := suppress(AssertionError)):\n"
            "            assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # ... and inside a `try`.
    (
        "a walrus inside a try is re-entered by a later header",
        (
            "    try:\n"
            "        with (cs := suppress(AssertionError)):\n"
            "            assert x != 1\n"
            "    except Exception:\n"
            "        pass\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # #324 criterion 2: a later rebind must WIN over the carried walrus
    # value. `nullcontext()` does not suppress, so the second assert really is
    # live and must be reported enforced. This is the row that a naive
    # "record every walrus" repair gets wrong -- it is exactly the
    # over-breadth that blocked #323.
    (
        "a later nullcontext rebind wins over a carried walrus value",
        (
            "    with (cs := suppress(AssertionError)):\n        assert x != 1\n"
            "    cs = nullcontext()\n"
            "    with cs:\n        assert x != 2"
        ),
        [False, True],
    ),
    # ... the same through a helper, which is the `#308` rebind family.
    (
        "a later helper rebind wins over a carried walrus value",
        (
            "    with (cs := suppress(AssertionError)):\n        assert x != 1\n"
            "    cs = helper.make()\n"
            "    with cs:\n        assert x != 2"
        ),
        [False, True],
    ),
    # ... and a rebind nested in an `if`, which is conditional.
    (
        "a later rebind inside an if wins over a carried walrus value",
        (
            "    with (cs := suppress(AssertionError)):\n        assert x != 1\n"
            "    if flag:\n"
            "        cs = nullcontext()\n"
            "    with cs:\n        assert x != 2"
        ),
        [False, True],
    ),
    # #324 criterion 3: a walrus whose value is not a suppressor never yields
    # a defeated verdict, even when the name is re-entered. This is the
    # cleanest canary -- nothing rebinds `cs`, so the suppressor value simply
    # must never have been attached to it.
    (
        "a walrus of a non-suppressor is never reported defeated when re-entered",
        (
            "    with (cs := nullcontext()):\n        assert x != 1\n"
            "    with cs:\n        assert x != 2"
        ),
        [True, True],
    ),
    # #348: a store whose right-hand side is the name it binds. The alias walk
    # stands on the very store it is resolving, so the suppressor it already
    # holds is never seen and both headers reported a SWALLOWED assert as
    # live. Both are the damaging direction, and neither was pinned before.
    (
        "a walrus aliasing the very name it binds re-enters",
        (
            "    cs = contextlib.suppress(AssertionError)\n"
            "    with (cs := cs):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # The same self-alias written as an ordinary store rather than a walrus.
    # It resolves on a different path, so pinning only the walrus spelling
    # would leave this one unpinned.
    (
        "a plain store aliasing the very name it binds re-enters",
        (
            "    cs = contextlib.suppress(AssertionError)\n"
            "    cs = cs\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # A rebind of the aliased name *after* the header. The interpreter has not
    # run it when the header reads the name, so the suppressor is still in
    # force and the assert is swallowed -- but a whole-function "last binding"
    # view resolves the header to the nullcontext instead, which reads as live.
    # The re-entering `with first:` is the control: by then the rebind HAS
    # run, so that assert really is live and must stay reported so.
    (
        "an alias read before its own later rebind re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    with (cs := first):\n"
            "        assert x != 1\n"
            "    first = nullcontext()\n"
            "    with first:\n"
            "        assert x != 2"
        ),
        [False, True],
    ),
    # A two-hop chain whose last link is rebound after the header that reads
    # it. Same failure as the row above, one link further from the store, so
    # a rule that only special-cases the direct name would still look right.
    (
        "a two-hop alias read before its last link is rebound re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    second = first\n"
            "    with (cs := second):\n"
            "        assert x != 1\n"
            "    first = nullcontext()\n"
            "    with first:\n"
            "        assert x != 2"
        ),
        [False, True],
    ),
    # #348 follow-on: the store that HAS run is not always the last one
    # `ast.walk` visits. `ast.walk` is breadth-first, so a store written
    # directly in the body is recorded before a store nested in an EARLIER
    # top-level statement. Picking the last recorded entry therefore returns
    # the nested one, which is stale -- and the verdict it produces is the
    # damaging one, a live contract reported as defeated.
    #
    # Here `if flag:` does not run, so `first` is the `nullcontext()` below
    # it. `nullcontext()` is a working context manager, so the assert really
    # runs and is live -- it must be reported enforced. Resolving the chain to
    # the nested `suppress(...)` instead makes it look swallowed.
    #
    # The second assert re-enters `first` directly rather than through the
    # chain, as the control: the same stale pick governs it, so a rule that
    # ignored ordering entirely -- returning `entries[-1]` -- would report
    # that one defeated too, and the fixture would read [False, False].
    (
        "a two-hop alias resolves to the store that actually ran, not the last walked",
        (
            "    if flag:\n"
            "        first = contextlib.suppress(AssertionError)\n"
            "    first = contextlib.nullcontext()\n"
            "    second = first\n"
            "    with (cs := second):\n"
            "        assert x != 1\n"
            "    with second:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # The same mis-ordering reached through a one-hop alias rather than a
    # two-hop chain, so pinning only the chain spelling would leave the
    # direct one unpinned. Both asserts are live for the same reason as the
    # row above, and both are certified only by picking the store that ran.
    (
        "an alias read picks the store that ran even when a nested store was walked later",
        (
            "    if flag:\n"
            "        first = contextlib.suppress(AssertionError)\n"
            "    first = contextlib.nullcontext()\n"
            "    alias = first\n"
            "    with (cs := alias):\n"
            "        assert x != 1\n"
            "    with first:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # Two walruses of the same name in sequence: the later one supersedes the
    # earlier, so the third assert runs under a `nullcontext` and is live.
    (
        "a later walrus of the same name supersedes the earlier one",
        (
            "    with (cs := suppress(AssertionError)):\n        assert x != 1\n"
            "    with (cs := nullcontext()):\n        assert x != 2\n"
            "    with cs:\n        assert x != 3"
        ),
        [False, True, True],
    ),
    # The bind does not have to be written in a `with` header. An assignment
    # expression is a binding wherever it appears, and the three rows below
    # are what separates recording a `NamedExpr` from harvesting the walruses
    # out of `with` headers specifically. Executed, each of these really does
    # swallow, so reporting them live is the damaging direction.
    (
        "a walrus in a plain assignment is re-entered by a later header",
        ("    y = (cs := suppress(AssertionError))\n    with cs:\n        assert x != 1"),
        [False],
    ),
    (
        "a walrus in a comprehension is re-entered by a later header",
        (
            "    rows = [(cs, v) for v in items if (cs := suppress(AssertionError))]\n"
            "    with cs:\n        assert x != 1"
        ),
        [False],
    ),
    # A rebind in the *same* block as the walrus supersedes it. Executed, the
    # first assert is swallowed by the suppressor that was current when the
    # `with` was entered, and the second runs under the rebound
    # `nullcontext` and is live.
    (
        "a rebind in the same block supersedes a walrus of the same name",
        (
            "    with (cs := suppress(AssertionError)):\n"
            "        cs = nullcontext()\n"
            "        assert x != 1\n"
            "    with cs:\n        assert x != 2"
        ),
        [False, True],
    ),
    # #324 criterion 4, and the three rows that actually pin
    # `_walrus_is_conditional`.
    #
    # Every row above reaches the re-entry through a walrus in a *top-level*
    # `with` header, so a walrus's conditionality never has to be decided: it
    # is unconditional on every path, and the two competing walruses in the
    # supersession row are settled by source order before ambiguity is ever
    # consulted. The mutation "force every walrus unconditional" therefore
    # leaves this whole table green.
    #
    # These three do decide it. Here the walrus is under an `if`, so it binds
    # `cs` on some paths only -- exactly the case
    # `_walrus_skipped_by_a_branch` exists to recognise. The competing
    # `cs = nullcontext()` is under its own `if`, so it too is conditional.
    # Two conditional bindings of one name cannot be ordered: on the `flag`
    # path the `nullcontext` wins and the assert is live, and on the `not
    # flag` path the suppressor is still bound. Which one is in force is
    # undecidable, so the name is ambiguous and the safe reading is "a
    # suppressor may be in force" -- the middle assert is reported defeated.
    #
    # Read the mutation the other way and it is the whole point: force the
    # walrus unconditional and source order picks the `nullcontext` as the
    # winner, so the live assert flips to enforced. That is the damaging
    # direction -- a real contract dropped from the sentinel's view -- and it
    # is the defect #324 is filed about, reached by a different road.
    #
    # Executed with `flag=True` the middle assert raises `AssertionError`
    # (nullcontext does not suppress) while the first is swallowed; with
    # `flag=False` neither `if` body runs and the assert does not execute at
    # all. Both were run, unmodified, before these rows were written.
    (
        "a conditional walrus does not outrank a later conditional store",
        (
            "    if flag:\n"
            "        with (cs := suppress(AssertionError)):\n"
            "            assert x != 1\n"
            "    if flag:\n"
            "        cs = nullcontext()\n"
            "        assert x != 2\n"
            "    with cs:\n"
            "        assert x != 3"
        ),
        [True, False, False],
    ),
    # ... the same with the conditional walrus written as a loop rather than
    # an `if`. `for` is in `_walrus_skipped_by_a_branch`'s set for the same
    # reason `if` is, and a rule that recognised only one of the two would be
    # half a rule.
    (
        "a walrus under a loop does not outrank a later conditional store",
        (
            "    for item in items:\n"
            "        with (cs := suppress(AssertionError)):\n"
            "            assert x != 1\n"
            "    if flag:\n"
            "        cs = nullcontext()\n"
            "        assert x != 2\n"
            "    with cs:\n"
            "        assert x != 3"
        ),
        [True, False, False],
    ),
    # ... and under a `try`. The three blocks are the complete set that can
    # skip a walrus, so all three are pinned: dropping any one of them from
    # `_walrus_skipped_by_a_branch` turns exactly the matching row red.
    (
        "a walrus under a try does not outrank a later conditional store",
        (
            "    try:\n"
            "        with (cs := suppress(AssertionError)):\n"
            "            assert x != 1\n"
            "    except Exception:\n"
            "        pass\n"
            "    if flag:\n"
            "        cs = nullcontext()\n"
            "        assert x != 2\n"
            "    with cs:\n"
            "        assert x != 3"
        ),
        [True, False, False],
    ),
    # #333: the binding takes its value from a NAME that is already bound to a
    # suppressor, rather than from an inline call. Executed, `cs` *is* `base`,
    # so both asserts are swallowed. The rule recorded the bare `base` node,
    # `_is_readable_suppressor` accepts only an `ast.Call`, and both headers
    # were reported live -- a disarmed contract certified as load-bearing.
    (
        "a walrus of a pre-bound suppressor name re-enters",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    with (cs := base):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # Two hops, so a rule that follows exactly one link is caught. Both links
    # are ordinary unconditional stores, so nothing is ambiguous here.
    (
        "a walrus of a two-hop suppressor alias re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    second = first\n"
            "    with (cs := second):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # The from-import spelling: the suppressor is built from the bare name
    # `suppress`, so the chain has to terminate at a suppressor the module
    # spells without a dotted prefix. The walrus still binds the *instance*:
    # `with (cs := suppress):` would enter the factory object itself, which
    # raises `TypeError` on `__enter__` and never reaches the assert, so it is
    # not this shape and is not pinned as one.
    (
        "a walrus of a from-imported suppressor re-enters",
        (
            "    base = suppress(AssertionError)\n"
            "    with (cs := base):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # The binding is not confined to a `with` header. An assignment expression
    # binds a name wherever it appears, and #324 already taught the rule to
    # harvest them from assignments and comprehensions -- but it classified
    # each by its own right-hand side, so a pre-bound NAME was still unreadable
    # in all three of those positions.
    (
        "a walrus of a pre-bound name in a plain assignment re-enters",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    y = (cs := base)\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        [False],
    ),
    (
        "a walrus of a pre-bound name in a comprehension re-enters",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    rows = [(cs, v) for v in items if (cs := base)]\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # A conditional store of the alias. `flag` gates the store, so on the
    # `not flag` path `cs` is never bound and the header raises `NameError` --
    # loudly. On the `flag` path it really is the suppressor, so the safe
    # reading is the same one `_resolve_bindings` already gives an ambiguous
    # name.
    (
        "a conditional store of a pre-bound suppressor name re-enters",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    if flag:\n"
            "        cs = base\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # The name the walrus aliases is bound TWICE, so the value in force is the
    # LATER store. Reading the first binding instead resolves the alias to the
    # ordinary call, leaves it unreadable, and reports a swallowed assert as
    # live -- a supersession this module already models for every other store.
    (
        "a walrus of a name rebound to a suppressor re-enters",
        (
            "    first = helper.make()\n"
            "    first = contextlib.suppress(AssertionError)\n"
            "    with (cs := first):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # The same supersession in the other direction, so a rule that simply
    # reaches for the most recent *suppressor* is caught as well: the suppressor
    # is the earlier store and the ordinary call is what is live.
    (
        "a walrus of a name rebound to a non-suppressor re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    first = contextlib.nullcontext()\n"
            "    with (cs := first):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # --- controls: the chain must not manufacture a suppressor ---------------
    # The name the walrus binds is bound to a context manager that does NOT
    # swallow. Following the alias must reach that value and stop, not conclude
    # "somewhere in this chain there was a call" and report a defeat.
    (
        "a walrus of a pre-bound non-suppressor name stays live",
        (
            "    base = contextlib.nullcontext()\n"
            "    with (cs := base):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # An unbound name is not a suppressor. The chain runs out of entries, the
    # value stays unreadable, and the assert stays live.
    (
        "a walrus of an unbound name stays live",
        (
            "    with (cs := helper.make()):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # A cycle must terminate rather than recurse. The mutually referential
    # stores live in a nested frame so the outer scope is unaffected; the point
    # of the row is that the run reaches a verdict at all.
    (
        "a walrus whose alias chain cycles terminates",
        (
            "    def build():\n"
            "        first = second\n"
            "        second = first\n"
            "        return first\n"
            "    with (cs := build()):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # A later store supersedes the walrus exactly as it supersedes an ordinary
    # assign, so substituting the aliased value must not make the carried
    # binding outlive its replacement.
    (
        "a walrus of a pre-bound name after a superseding store",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    with (cs := base):\n"
            "        assert x != 1\n"
            "    cs = contextlib.nullcontext()\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, True],
    ),
    # --- #328: the same alias chain WITHOUT a walrus -----------------------
    #
    # These three rows are not #333. They were filed as a separate issue and
    # were blind in exactly the same way -- a swallowed assert certified as
    # load-bearing -- because a bare `with alias:` records a `Name` that
    # `_is_readable_suppressor` rejects. They are pinned HERE because the fix
    # that repairs them is the fix that repairs #333: the whole-function
    # pre-pass in `_raw_store_values` hands the ordinary alias path the same
    # complete table the walrus path needs, so both resolve together or not at
    # all.
    #
    # They are stated as rows rather than left to the PR narrative so the
    # side-effect fix is pinned by a test. A future change that narrows the
    # dereference back to walrus headers alone fails these.
    (
        "a plain alias of a pre-bound suppressor re-enters",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    alias = base\n"
            "    with alias:\n"
            "        assert x != 1\n"
            "    with alias:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # Two hops. Note this is NOT the discriminating row a single-hop mutation
    # would catch, and the difference matters: the dereference runs at the
    # *store site*, so `second = first` is resolved to the suppressor the
    # moment it is recorded, and the header then reads an already-resolved
    # value. The walrus path resolves at the *header*, one link further down
    # the chain, which is why the walrus two-hop row is the one a single-hop
    # mutation breaks. This row still pins #328: it fails outright if the
    # ordinary alias path resolves nothing at all.
    (
        "a plain two-hop suppressor alias re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    second = first\n"
            "    with second:\n"
            "        assert x != 1\n"
            "    with second:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # #328 acceptance criterion 4 names a THREE-link chain explicitly
    # ("a = suppress(...); b = a; c = b"), and the shipped table only pinned
    # two. Three links is past the point where a hand-written "follow one or
    # two" rule would still look correct, so the row is here to make the bound
    # a tested property rather than an accident of how far the fixture reached.
    #
    # The walrus twin of this shape already exists two rows up; this one is the
    # ordinary `with` spelling, which resolves at the store site instead of the
    # header and so is reached by a different call path.
    (
        "a plain three-hop suppressor alias re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    second = first\n"
            "    third = second\n"
            "    with third:\n"
            "        assert x != 1\n"
            "    with third:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # The control for the ordinary path: following the chain must stop at a
    # context manager that does not suppress, rather than report a defeat
    # because a suppressor appeared somewhere earlier in the chain.
    (
        "a plain alias of a pre-bound non-suppressor stays live",
        (
            "    base = contextlib.nullcontext()\n"
            "    alias = base\n"
            "    with alias:\n"
            "        assert x != 1\n"
            "    with alias:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    WALRUS_REENTRY_SHAPES,
    ids=[row[0] for row in WALRUS_REENTRY_SHAPES],
)
def test_a_walrus_bound_alias_reaches_the_headers_that_re_enter_it(label, body, expected):
    """A walrus bind is a real binding, and a later rebind still supersedes it.

    #323 tried to close the re-entry half of this and could not without
    re-opening the supersession half: carrying the walrus value forward
    unconditionally made a stale suppressor outlive a later ``cs =
    nullcontext()``, so a *live* assert was reported defeated on three
    separate rows. The shipped rule records a ``NamedExpr`` through the same
    per-index binding machinery as an ordinary store, so both halves hold at
    once.
    """
    source = (
        "def outer(x, flag, helper, items):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n" + body + "\n"
    )
    tree = ast.parse(source)
    outer = tree.body[0]
    asserts = [node for node in ast.walk(outer) if isinstance(node, ast.Assert)]
    assert len(asserts) == len(expected), (
        f"{label}: fixture declared {len(asserts)} asserts but the row "
        f"expects {len(expected)} verdicts"
    )
    results = [_is_enforced(outer, node, tree) for node in asserts]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. Every assert "
        f"is compared individually -- `all()` would collapse this fixture to "
        f"its first verdict and hide a wrong later one."
    )


#: #324: every binding form Python has must retire a carried walrus, not just
#: ``ast.Name`` targets.
#:
#: A walrus-bound suppressor is only correct while it is the *live* binding of
#: the name. Rebinding that name to a ``nullcontext`` on every path makes the
#: later ``with cs:`` enter something that does not suppress, so the assert
#: under it is live and must be reported enforced. Reading only ``ast.Name``
#: targets left every nested and non-``Assign`` store invisible, so a stale
#: suppressor outranked the rebinding and a **live** assert was reported
#: defeated -- the damaging direction.
#:
#: ``expected`` is the exact per-assert verdict list, not ``all(...)``:
#: collapsing to the first verdict is what hid this defect in the first place.
WALRUS_REBINDING_SHAPES = (
    (
        "a plain store retires the walrus",
        "    cs = contextlib.nullcontext()",
        False,
        True,
    ),
    (
        "a tuple-unpack store retires the walrus",
        "    cs, other = (contextlib.nullcontext(), 2)",
        False,
        True,
    ),
    (
        "a starred-unpack store retires the walrus",
        "    cs, *rest = (contextlib.nullcontext(), 2, 3)",
        False,
        True,
    ),
    (
        "a list-target store retires the walrus",
        "    [cs] = [contextlib.nullcontext()]",
        False,
        True,
    ),
    (
        "an annotated store retires the walrus",
        "    cs: object = contextlib.nullcontext()",
        False,
        True,
    ),
    (
        "a loop target retires the walrus",
        "    for cs in (contextlib.nullcontext(),):\n        pass",
        False,
        True,
    ),
    (
        "a with-as target retires the walrus",
        "    with nullcontext() as cs:\n        pass",
        False,
        False,
    ),
    (
        "an async with-as target retires the walrus",
        "    async with nullcontext() as cs:\n        pass",
        True,
        False,
    ),
    (
        "an async loop target over a context manager is undecided by the rule",
        "    async for cs in agen():\n        pass",
        True,
        True,
    ),
    (
        "an except-as target retires the walrus",
        "    try:\n        raise ValueError()\n    except ValueError as cs:\n        pass",
        False,
        False,
    ),
    (
        "a del retires the walrus",
        "    del cs",
        False,
        False,
    ),
)


#: #359: a ``with``-header bound by a carrier that is not a target.
#:
#: Three binding forms bind their name as a *string field* of a node rather
#: than as a target: ``import os as cs`` (``Import.asname``), ``def cs()`` and
#: ``class cs``. The name therefore holds a module, a class or a function --
#: none of which has ``__enter__`` -- so ``with cs:`` raises ``TypeError``
#: while evaluating the header, before the assert under it is ever reached.
#:
#: ``from M import N as cs`` is **not** here. It looks identical but binds an
#: arbitrary attribute of ``M``; see :data:`UNDECIDABLE_IMPORT_FROM_SHAPES`
#: and #376.
#:
#: This table exists separately from ``WALRUS_REBINDING_SHAPES`` because that
#: table's fixture *wraps* its rebind in a preceding
#: ``with (cs := contextlib.suppress(...))``. A carrier row added there is
#: **vacuous**: the walrus already makes the second assert's verdict
#: ``[False]``, so deleting the whole carrier implementation leaves the row
#: green. These rows carry no walrus, so the carrier alone decides the
#: verdict and the fixture fails if the rule stops reading them.
#:
#: ``live`` is the per-row entry contract, held to CPython by
#: ``_assert_entry_contract`` rather than merely asserted about the checker.
CARRIER_ONLY_SHAPES = (
    (
        "an import-as carrier leaves a module",
        "    import os as cs",
        False,
    ),
    (
        "a def carrier leaves a function",
        "    def cs():\n        pass",
        False,
    ),
    (
        "a class carrier leaves a class",
        "    class cs:\n        pass",
        False,
    ),
    (
        "CONTROL a plain store leaves a real context manager",
        "    cs = nullcontext()",
        True,
    ),
)


#: #359: the same carriers bound at **module** scope.
#:
#: A module-scope carrier binds the name for the whole file, so a ``with``
#: header inside a function reads it as a *free* name. The function-scoped
#: walk behind :func:`_carrier_runtime_kinds` cannot see it, so before #359
#: the store table was silent and the header read as ``enforced`` -- on an
#: assert the interpreter never evaluates. This is the residue that was left
#: behind when #354's factual claim was refuted and closed.
#:
#: These rows are separate from :data:`CARRIER_ONLY_SHAPES` because the name
#: is bound in a *different scope*, and that is the whole difference: the same
#: source gives two different verdicts on master depending only on where the
#: carrier is written. ``MODULE_CARRIER_SHAPES`` is in the module body;
#: ``CARRIER_ONLY_SHAPES`` is inside the function.
MODULE_CARRIER_SHAPES = (
    (
        "a module-scope import-as carrier leaves a module",
        "import os as cs",
        False,
    ),
    (
        "a module-scope def carrier leaves a function",
        "def cs():\n    pass",
        False,
    ),
    (
        "a module-scope class carrier leaves a class",
        "class cs:\n    pass",
        False,
    ),
    (
        "a later module-scope def supersedes an earlier carrier",
        "import os as cs\ndef cs():\n    pass",
        False,
    ),
    # --- #359 supersession family -------------------------------------
    # A name's value is settled by the last *binding*, not the last carrier.
    # Each row below binds a module-scope carrier and then rebinds the name
    # to a real `nullcontext()` by a *different* module-level store form.
    # Because the later store wins at runtime, `with cs:` succeeds and the
    # assert is live -- but a rule that reads only carriers still sees the
    # earlier `import os as cs`, reports the name as a module, and answers
    # `defeated`, dropping a real pinned contract. Master already answers
    # these correctly, so they are regression rows, not new repairs.
    (
        "a later module-scope plain store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\ncs = contextlib.nullcontext()",
        True,
    ),
    (
        "a later module-scope walrus store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\n(cs := contextlib.nullcontext())",
        True,
    ),
    (
        "a later module-scope tuple-unpack store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\ncs, other = (contextlib.nullcontext(), 2)",
        True,
    ),
    (
        "a later module-scope annotated store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\ncs: object = contextlib.nullcontext()",
        True,
    ),
    (
        "a later module-scope for-target store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\nfor cs in (contextlib.nullcontext(),):\n    pass",
        True,
    ),
    (
        "a later module-scope del-then-store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\ncs = contextlib.nullcontext()\ndel cs\ncs = contextlib.nullcontext()",
        True,
    ),
    (
        "CONTROL a module-scope store of a real context manager is live",
        "import contextlib\ncs = contextlib.nullcontext()",
        True,
    ),
    (
        "CONTROL a function-local store shadows the module carrier and is live",
        "import os as cs",
        True,
    ),
)

#: #376: ``from M import N as cs`` binds an attribute, not a module.
#:
#: It looks exactly like ``import os as cs`` -- a name carried in a string
#: field of an import node -- but the two are not the same claim. ``import
#: os as cs`` binds the module ``os``, and the language gives that spelling
#: one meaning. ``from M import N as cs`` binds whatever attribute ``N`` is on
#: ``M``, and one spelling produces every runtime type (measured):
#:
#:     from os import path as cs         -> os.path   a module
#:     from os import sep as cs          -> os.sep    a str
#:     from decimal import Decimal as cs -> a class
#:     from mymod import ctx as cs       -> WHATEVER mymod.ctx is
#:
#: The last row is the point. ``mymod.ctx`` is a real ``nullcontext()``, so
#: ``with cs:`` **succeeds** and the assert is live. Recording the shape as a
#: module would report that live assert as dead and drop a real pinned
#: contract -- the damaging direction, introduced by the very rule meant to
#: fix it. So the rule **declines** it, and the analyzer answers ``enforced``.
#:
#: Declining costs the one genuinely dead ``os.path`` row, which is the right
#: trade: a wrong "dead" is unrecoverable, and a conservative "enforced" is
#: the same answer master already gave.
UNDECIDABLE_IMPORT_FROM_SHAPES = (
    (
        "an import-from-as binding a module is declined, not guessed",
        "from os import path as cs",
    ),
    (
        "an import-from-as binding a str is declined, not guessed",
        "from os import sep as cs",
    ),
    (
        "an import-from-as binding a class is declined, not guessed",
        "from decimal import Decimal as cs",
    ),
)


@pytest.mark.parametrize(
    ("label", "bind"),
    UNDECIDABLE_IMPORT_FROM_SHAPES,
    ids=[shape[0] for shape in UNDECIDABLE_IMPORT_FROM_SHAPES],
)
def test_an_import_from_as_is_declined_rather_than_read_as_a_module(label, bind):
    """``from M import N`` must not be answered as "a module".

    Each row is a real ``import from`` whose bound value is genuinely
    unenterable, so ``defeated`` would be right for all three -- the same
    answer ``os.path`` alone would give. The rule declines anyway and so
    answers ``enforced``: wrong for these three, and the price of not being
    wrong for the fourth.

    This is not a coverage hole because of the control in
    :func:`test_an_import_from_as_can_bind_a_real_context_manager`: the same
    spelling, over a module that exports an actual context manager, is live.
    A rule answering "module" there would drop a real contract.
    """
    source = (
        "import contextlib\n"
        "from contextlib import suppress, nullcontext\n"
        "def outer(x, flag, helper):\n    " + bind + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"{label}: expected the rule to DECLINE, i.e. [True], got {results}. "
        f"The value bound by `from M import N` is not readable off the syntax."
    )


def test_an_import_from_as_can_bind_a_real_context_manager():
    """The control that makes the decline above necessary rather than cautious.

    ``mymod.ctx`` is a real ``contextlib.nullcontext()`` instance, so
    ``from mymod import ctx as cs`` binds an **enterable** value, ``with cs:``
    succeeds, the assert is live, and the verdict must be ``enforced``.

    Nothing in the source distinguishes this from
    ``from os import path as cs``, which binds an unenterable module. That is
    the whole argument for declining the shape: the syntax fixes the value for
    ``import ... as`` and does not fix it for ``from ... import ... as``.

    Executed rather than asserted about the analyzer, so the row cannot pass
    by the checker and the claim being wrong together.
    """
    source = (
        "import contextlib\n"
        "def outer(x, flag, helper):\n"
        "    from tests._import_from_carrier_support import ctx as cs\n"
        "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract("an import-from-as binding a real context manager", source, False, True)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"expected [True] -- the assert is live -- got {results}. A rule that "
        f"answered 'dead' here would drop a real pinned contract."
    )


@pytest.mark.parametrize(
    ("label", "bind", "live"),
    CARRIER_ONLY_SHAPES,
    ids=[shape[0] for shape in CARRIER_ONLY_SHAPES],
)
def test_a_with_header_bound_by_a_carrier_is_dead_entry(label, bind, live):
    """A module, class or function in a ``with`` header defeats the assert.

    The fixture binds the name in one syntactic form and immediately enters
    it, with **no** walrus suppressor anywhere -- so the carrier is the only
    thing that can make the assert unreachable.

    The four carrier rows are dead entry: ``with cs:`` raises ``TypeError``
    in the header, so the assert under it never runs and reporting it as
    load-bearing certifies a contract the interpreter never applies. The
    ``CONTROL`` row binds a real context manager with a plain store and is
    therefore live, and exists so that the four rows cannot be made to pass
    by simply declaring every carrier-shaped header dead -- the control is
    the row that would break under that shortcut.
    """
    source = (
        "import contextlib\n"
        "from contextlib import suppress, nullcontext\n"
        "def outer(x, flag, helper):\n" + bind + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, live)
    tree = ast.parse(source)
    # Two module-level imports precede `def outer`, so it is the last body
    # node, not `tree.body[1]`.
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. The carrier "
        f"alone decides this verdict, so the two must agree."
    )


@pytest.mark.parametrize(
    ("label", "prelude", "live"),
    MODULE_CARRIER_SHAPES,
    ids=[shape[0] for shape in MODULE_CARRIER_SHAPES],
)
def test_a_module_scope_carrier_defeats_the_assert_below_it(label, prelude, live):
    """A carrier bound at module scope defeats an assert in any function.

    The carrier is written in the module body and the assert lives in
    ``outer``, so the name reaches the ``with`` header as a free name. Nothing
    inside the function binds it, which is exactly the case the function-scoped
    walk cannot see.

    The five dead rows are held to CPython by ``_assert_entry_contract``. The
    two controls pin both ways a carrier must *not* be over-read: a
    module-scope store of a real context manager is live, and a function-local
    store shadows the module carrier and is live again. Without the second
    control a fix that simply declared every free-name header dead would pass.

    The six **supersession** rows are the other direction, and they are
    regression rows rather than new repairs: master already answers them
    correctly. Each binds a module-scope carrier and then rebinds the same
    name with a different module-level store form, so the *last binding* --
    not the last carrier -- settles the value, and ``with cs:`` succeeds. A
    rule that reads only carriers still sees the ``import os as cs``, reports
    the name as a module, and answers ``defeated``, dropping a live contract.
    They are what keeps the module rule honest about *which* binding wins.
    """
    if "shadows" in label:
        body = "    from contextlib import nullcontext\n    cs = nullcontext()\n"
    else:
        body = ""
    source = (
        prelude + "\ndef outer(x, flag, helper):\n" + body + "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A module-scope "
        f"carrier is a real store that runs at import time, so the header "
        f"must be judged on it rather than declined."
    )


def test_a_block_nested_module_carrier_is_still_declined():
    """Pin the *known* limit of the module-scope rule, so it cannot widen silently.

    A carrier written directly in the module body is an unconditional store:
    it has run by the time any function is entered, so the rule can judge the
    header on it. A carrier nested in a module-level ``if`` has run only on
    some paths, and nothing in the syntax says which -- so the rule declines
    and the answer stays ``enforced``.

    That is the **wrong** answer for this fixture: at runtime ``sys.platform``
    is truthy, the carrier binds, and ``with cs:`` raises ``TypeError`` before
    the assert. The row is here precisely because it is wrong, and it is kept
    out of :data:`MODULE_CARRIER_SHAPES` because that table's ``live`` column is
    held to CPython and this row must not claim otherwise.

    What the row buys is that the narrowing is *pinned*. If a later change
    starts reading conditional module bindings as settled, this fails and the
    widening has to be argued for on its own mutation matrix rather than
    arriving as a side effect. It is the same reason #359's criterion 3 wants
    a control: a rule that is merely "conservative" is indistinguishable from
    one that is broken until something says which it is.
    """
    source = (
        "import sys\n"
        "if sys.platform:\n"
        "    import os as cs\n"
        "def outer(x, flag, helper):\n"
        "    with cs:\n        assert x != 1\n"
    )
    # The interpreter half, so the gap is documented as a real one.
    _assert_entry_contract(
        "a block-nested module carrier (runtime: unreachable)",
        source,
        False,
        False,
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"expected the rule to DECLINE a conditional module binding, i.e. "
        f"[True], got {results}. CPython disagrees on this row (the assert is "
        f"unreachable), so a change that reads it as settled has to update "
        f"this test and justify the widening -- not land silently."
    )


@pytest.mark.parametrize(
    ("label", "rebind", "live"),
    [
        (
            "a global rebind run at import time supersedes the carrier",
            "def _rebind():\n    global cs\n    cs = contextlib.nullcontext()\n_rebind()\n",
            True,
        ),
        (
            "CONTROL a global rebind that runs before the carrier is superseded by it",
            "def _rebind():\n    global cs\n    cs = contextlib.nullcontext()\n_rebind()\n",
            False,
        ),
    ],
    ids=[
        "a global rebind run at import time supersedes the carrier",
        "CONTROL a global rebind that runs before the carrier is superseded by it",
    ],
)
def test_a_conditional_module_store_after_a_carrier_is_declined(label, rebind, live):
    """A carrier followed by a *conditional* store leaves the value undecided.

    ``_stores_of`` keeps only unconditional stores, so an unconditional
    carrier survives a conditional store that follows it. That is the right
    answer when the conditional store merely *mentions* the name, but not when
    it can have replaced the carrier with something enterable:

        import os as cs
        import contextlib
        def _rebind():
            global cs
            cs = contextlib.nullcontext()
        _rebind()
        def outer(x, flag, helper):
            with cs:            # succeeds: cs is a real nullcontext()
                assert x != 1   # live

    The store is nested in a function body, so whether it ran is not decidable
    from the carrier's point of view -- but it *can* have run, and if it did the
    carrier is stale. Answering from the carrier drops a live assert, so the
    rule declines instead.

    The control is the same source with the call moved *before* the carrier. A
    conditional store that precedes the last carrier really is superseded by
    it, so ``defeated`` is correct there and no decline is warranted. Without
    the control, a fix that simply declined every shape containing a
    conditional store would pass.

    This is deliberately not a row in :data:`MODULE_CARRIER_SHAPES`: that
    table's fixture is ``prelude + "def outer(...)"``, which cannot express a
    helper *call* between the carrier and the header.
    """
    carrier = "import os as cs\nimport contextlib\n"
    if "before" in label:
        source = (
            "import contextlib\n"
            + rebind
            + carrier
            + "def outer(x, flag, helper):\n    with cs:\n        assert x != 1\n"
        )
    else:
        source = (
            carrier + rebind + "def outer(x, flag, helper):\n    with cs:\n        assert x != 1\n"
        )
    _assert_entry_contract(label, source, False, live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A conditional "
        f"store after the last carrier can have replaced it with an enterable "
        f"value, so the header must be declined rather than judged on a store "
        f"that may be stale."
    )


@pytest.mark.parametrize(
    ("label", "rebind", "is_async", "second_assert_live"),
    WALRUS_REBINDING_SHAPES,
    ids=[shape[0] for shape in WALRUS_REBINDING_SHAPES],
)
def test_a_walrus_alias_is_retired_by_every_binding_form(
    label, rebind, is_async, second_assert_live
):
    """A carried suppressor must not outlive the name being rebound.

    Each row binds a suppressor through a walrus, then rebinds that name in a
    different syntactic form, then enters it. The first assert is swallowed by
    the walrus-bound suppressor on **every** row -- that is the retirement
    claim, and it is ``False`` throughout.

    The second assert divides, and the division is the point. It used to be
    pinned to a single ``[False, True]`` for all eleven rows, which was wrong
    twice over: the two async rows are not even executable (they sit inside a
    plain ``def`` and call an undefined ``gen()``), and no row was ever run --
    there is no ``exec``, ``compile`` or ``eval`` in the body, only
    ``ast.parse`` and a call to the checker. A table that only reads the
    checker cannot notice when its claim and the checker's answer are wrong
    together.

    Executed on CPython, the rows split cleanly in two:

    * ``second_assert_live`` -- the rebind leaves ``cs`` bound to something
      enterable, so ``with cs:`` succeeds and the assert is a real contract.
      That covers every row whose right-hand side is a *call*:
      ``cs = contextlib.nullcontext()``, ``cs, other = (nullcontext(), 2)``,
      ``[cs] = [nullcontext()]``, the annotated store, and
      ``for cs in (nullcontext(),):``. Destructuring is not special here --
      the name still receives a real context manager, just one element out of
      a container. The async loop row is here for a second reason, and it is
      the one row whose entry is genuinely undecidable: the rule cannot read
      the element type out of an arbitrary async iterable, so it declines
      rather than guessing, and this row pins that choice. See
      :func:`_async_loop_target_is_undecidable` for the evidence.
    * ``not second_assert_live`` -- the rebind leaves ``cs`` holding something
      that cannot be entered, so the header raises before the body runs and
      the assert is unreachable. ``with nullcontext() as cs:`` binds
      ``__enter__``'s return value, which is ``None``; ``except E as cs:``
      and ``del cs`` unbind the name; ``async for cs in gen()`` binds the loop
      variable, an ``int``.

    The first half of each row is the retirement contract and the second is
    the entry contract, and they are independent: both are asserted, and the
    entry half is checked against the interpreter rather than against the
    analyzer it is supposed to police.
    """
    source = (
        f"{'async def' if is_async else 'def'} outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    async def gen():\n"
        "        yield 1\n"
        "    async def agen():\n"
        "        yield nullcontext()\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + rebind + "\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    if is_async and "async for" in rebind:
        _async_loop_target_is_undecidable(label, source)
    else:
        _assert_entry_contract(label, source, is_async, second_assert_live)
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [False, second_assert_live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. Both are "
        f"fixed by what the rebind leaves bound to the name."
    )


def _assert_entry_contract(label, source, is_async, second_assert_live):
    """Run the fixture and hold CPython to the row's declared entry contract.

    This is the check the table did not have. Every row ends with a
    ``with cs:``, and what CPython does on entry -- succeed and run the body,
    or raise before the body -- is the ground truth the analyzer's verdict is
    checked against. Pinning it here means a future row cannot claim "live"
    without the interpreter agreeing, and cannot claim "dead" either.

    Called with ``x=1``, so ``assert x != 1`` is **false** and the assert
    fires if and only if it is reachable. That distinguishes the two outcomes
    cleanly: a live row must raise ``AssertionError``, and an unreachable row
    must raise something *else* (``TypeError`` for a non-manager, or
    ``UnboundLocalError`` for an unbound name). Running with a value that
    makes the assert trivially true would let a swallowed row and an
    unreachable row look identical, which is the confusion this replaces.
    """
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    outer = namespace["outer"]
    try:
        if is_async:
            asyncio.run(outer(1, True, None))
        else:
            outer(1, True, None)
    except AssertionError:
        if second_assert_live:
            return
        raise AssertionError(
            f"{label}: the second assert fired, so the name was enterable. "
            f"The row claims it is unreachable."
        ) from None
    except (TypeError, UnboundLocalError, NameError):
        if second_assert_live:
            raise AssertionError(
                f"{label}: entering `with cs:` raised instead of running the "
                f"body, so the second assert is unreachable. The row claims "
                f"it is live."
            ) from None
        return
    raise AssertionError(
        f"{label}: the fixture returned normally, so the second assert was "
        f"swallowed rather than reachable. Row is stale."
    )


#: #350. Which nesting forms bind the *enclosing* function's local, and which
#: bind their own.
#:
#: A ``match`` capture inside a nested ``def``/``lambda``/``class`` body runs
#: in that body's namespace. Only a capture in the function's own scope retires
#: the carried suppressor. Every row here is executed, so "does not retire" is
#: backed by CPython swallowing the assert rather than by the analyzer's
#: opinion.
CAPTURE_SCOPE_BOUNDARY_ROWS = (
    # The filed shape. `class C:` opens a new namespace; the capture binds
    # `C.cs`, and `outer`'s `cs` is still the suppressor, so the second assert
    # is swallowed and the verdict is False.
    #
    # The clause is the IRREFUTABLE `case cs:` from the issue's reproduction.
    # That spelling matters. The first draft of this row used the *refutable*
    # `case nullcontext() as cs:`, and it passed against the unfixed support
    # module for the wrong reason: a refutable capture over an opaque
    # `helper()` is already declined (#369), so the buggy path and the correct
    # one both answered `False` and the row could not see the bug at all.
    # Measured on unfixed master: `case cs:` -> analyzer `True` against runtime
    # `False`; `case nullcontext() as cs:` -> `False` against `False`, i.e.
    # accidentally correct. Only the irrefutable form discriminates.
    (
        "a capture in a class body binds the class, not the function",
        ("    class C:\n        match helper():\n            case cs:\n                pass\n"),
        False,
        False,
    ),
    # Same, for a plain import rather than a capture -- the other walk that
    # used the same boundary (`_own_imports`).
    (
        "an import in a class body binds the class, not the function",
        "    class C:\n        import os as cs\n",
        False,
        False,
    ),
    # Controls: these must NOT change. A nested function is the pre-existing
    # correct behaviour, so a fix that simply stopped resolving captures
    # anywhere would fail this row.
    (
        "CONTROL a capture in a nested function does not bind the function",
        ("    def inner():\n        match helper():\n            case cs:\n                pass\n"),
        False,
        False,
    ),
    # The row that would break if the boundary were over-widened: a capture in
    # the function's OWN body does retire the suppressor, and `with cs:`
    # successfully enters the nullcontext, so the assert is live and the
    # correct verdict is `True`.
    #
    # This row is unchanged by the fix (it answered `True` before and after),
    # which is exactly its purpose: a repair that added `ClassDef` to the
    # boundary but also broke function-body captures would go red here.
    #
    # Note the difference from the sibling refutable rows elsewhere in this
    # file. `case cs:` is irrefutable, so this is decidable and `True` is
    # sound. The refutable spelling of the same shape is declined instead --
    # that is #369, filed and open, and it is why those rows pin `False`.
    (
        "CONTROL a capture in the function body does retire the suppressor",
        "    match helper():\n        case cs:\n            pass\n",
        True,
        True,
    ),
)

#: #359. A *string-field carrier* binds its name to a value the binding syntax
#: does not describe, and nothing pinned what the analyzer must answer when a
#: later ``with cs:`` raises **on entry** because of it.
#:
#: ``WALRUS_REBINDING_SHAPES`` above already pins the value-less carriers
#: (``with ... as cs``, ``except ... as cs``, ``del cs``), because those rows
#: retire the carried walrus. The five here are the ones it was missing: a
#: module, a function, a class, and a ``match`` capture are all bound by a
#: statement that reads as a store, and all four end up in the same
#: "no readable right-hand side" bucket in ``_entry_is_dead``. That bucket is
#: what makes the gap easy to lose.
#:
#: AC1 asks for each carrier pinned in both positions. The ``second_assert_live``
#: column is ``False`` for every row here, and it is that value which is the
#: point: entering raises ``TypeError`` before the body runs, so the second
#: assert is *unreachable*. Reporting it ``enforced`` is the damaging direction
#: per #308 criterion 1 -- it certifies a contract that can never fail as
#: load-bearing.
#:
#: AC3 asks for a non-carrier control so a fix cannot pass by disabling the
#: rule wholesale. Two are included below and both must stay ``True``: a plain
#: assignment of a real context manager, and a class that *does* implement the
#: protocol. Neither is in the table above, so neither is a duplicate.
CARRIER_ENTRY_UNREACHABLE_ROWS = (
    ("import-as binds the module", "    import os as cs", False),
    ("import-from-as binds the module", "    from os import path as cs", False),
    ("def binds a function object", "    def cs():\n        pass", False),
    ("class binds a type object", "    class cs:\n        pass", False),
    (
        "a match capture binds whatever it matched",
        "    match [1]:\n        case [cs]:\n            pass",
        False,
    ),
    # Controls: these leave the name enterable, so the assert is a real
    # contract. If either of these reports dead, the rule has been widened past
    # the carriers and is dropping live asserts.
    ("CONTROL a plain assign of a real context manager", "    cs = nullcontext()", True),
    (
        "CONTROL an instance of a class that implements the protocol",
        (
            "    class cs:\n"
            "        def __enter__(self):\n"
            "            return self\n"
            "        def __exit__(self, *exc):\n"
            "            return False\n"
            "    cs = cs()"
        ),
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "nested", "verdict", "runtime_live"),
    CAPTURE_SCOPE_BOUNDARY_ROWS,
    ids=[row[0] for row in CAPTURE_SCOPE_BOUNDARY_ROWS],
)
def test_a_capture_binds_only_the_namespace_it_was_written_in(label, nested, verdict, runtime_live):
    """A store in a nested namespace cannot rebind the enclosing function's name.

    This is #350. ``_scope_body_nodes`` stopped walking at a nested ``def``,
    ``async def`` or ``lambda`` but not at a ``ClassDef``, so a ``match``
    capture written in a class body was attributed to the enclosing function
    and retired its carried suppressor. The assert under the following
    ``with cs:`` was therefore reported *enforced* when it is really swallowed
    -- a dead contract certified as load-bearing.

    Measured on CPython 3.12.14 with ``x=1`` and ``helper()`` returning a
    ``nullcontext()``:

    * class body, capture      -> assert swallowed (correct verdict ``False``)
    * class body, ``import``   -> assert swallowed (correct ``False``)
    * nested ``def``, capture  -> assert swallowed (correct ``False``)
    * function body, capture   -> assert **live**

    The last row is what makes the fix safe. It answers ``True`` both before
    and after, so a repair that added ``ClassDef`` to the boundary but also
    broke function-body captures would go red here rather than passing every
    ``False`` row on the strength of the fix alone.

    An earlier draft of this row passed ``None`` as ``helper()``'s result,
    which made the capture bind ``None``, raised on entry, and failed the
    executed check for the wrong reason. The helper now returns a real
    ``nullcontext()``, so the capture binds an actual context manager and the
    ``with cs:`` really is entered.
    """
    source = (
        "def outer(x, flag, items, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        pass\n" + nested + "    with cs:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    from contextlib import nullcontext

    try:
        namespace["outer"](1, None, [1], nullcontext)
    except AssertionError:
        ran = True
    except (TypeError, UnboundLocalError, NameError):
        # Entering a non-manager raises before the body runs. Deliberately
        # narrow, matching `_assert_entry_contract`: a broader catch would let
        # a fixture that fails for an unrelated reason still pass this row.
        ran = False
    else:
        # Returned normally: the suppressor was still in force and swallowed
        # the assert.
        ran = False
    assert ran is runtime_live, (
        f"{label}: CPython says the second assert "
        f"{'ran' if ran else 'did not run'}, but the row's measured ground "
        f"truth says it should "
        f"{'run' if runtime_live else 'not run'}."
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [verdict], (
        f"{label}: expected verdicts {[verdict]}, got {results}. A "
        f"store in a nested namespace must not rebind the enclosing name."
    )


@pytest.mark.parametrize(
    ("label", "rebind", "second_assert_live"),
    CARRIER_ENTRY_UNREACHABLE_ROWS,
    ids=[row[0] for row in CARRIER_ENTRY_UNREACHABLE_ROWS],
)
def test_a_string_field_carrier_cannot_be_entered_so_the_assert_is_unreachable(
    label, rebind, second_assert_live
):
    """A carrier binds the name to something no ``with`` can enter.

    This is #359. The bug it reports is a *coverage* gap rather than a defect
    on master: master answers every row below correctly, which is exactly why
    the gap survived. The gap is in what the shipped tables pin, and it is
    demonstrably load-bearing -- see the mutation note in
    ``ledger/FINDING_359_GATE_STILL_OPEN_20260928.md``. Reverting the
    ``_entry_is_dead`` value-less branch so that a carrier no longer reads as
    a dead entry leaves the whole 273-test sentinel lane **green**, and
    reproduces the #337 regression this issue was filed about.

    The first assert is the retirement contract and is ``False`` on every row,
    including the controls: the carried walrus suppressor is entered and
    swallows it. The second is the entry contract and is what divides.

    AC2 is the reason this test **executes** rather than only reading the
    checker. Every row is run on CPython and held to ``second_assert_live``
    through ``_assert_entry_contract``, so a row cannot claim "live" without
    the interpreter agreeing, and cannot claim "dead" either. That is what
    makes a table that reads only the checker unable to hide here: the
    interpreter is in the loop.

    Measured on CPython 3.12.14, with ``x=1`` so ``assert x != 1`` is false:

    * ``import os as cs`` and ``from os import path as cs`` raise
      ``TypeError: 'module' object does not support the context manager
      protocol``.
    * ``def cs(): pass`` raises ``TypeError: 'function' object ...``.
    * ``class cs: pass`` raises ``TypeError: 'type' object ...``.
    * ``case [cs]:`` over the subject ``[1]`` binds the integer ``1``, so the
      capture row raises ``TypeError: 'int' object ...``.
    * both controls enter cleanly and the assert fires.

    One correction worth recording, because the first draft of the second
    control got it wrong. The control was written as a ``class cs:`` that
    *defines* ``__enter__``/``__exit__``, expecting it to be enterable. It is
    not. ``with cs:`` enters the **class object itself**, and a class is not a
    context manager no matter what its instances support -- measured on 3.12.14
    it raises ``TypeError: 'type' object does not support the context manager
    protocol`` exactly as a bare ``class cs: pass`` does. The name has to hold
    an *instance*, so the row ends ``cs = cs()``. Defining the dunders is
    necessary and not sufficient; this is the same reason
    ``_defines_context_manager_protocol`` reads a class body without
    concluding the binding is enterable.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + rebind + "\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, second_assert_live)
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 2, f"{label}: fixture declared {len(asserts)} asserts, expected 2"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [False, second_assert_live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A carrier that "
        f"cannot be entered makes the assert under the header *unreachable*, "
        f"so reporting it enforced certifies a dead contract as load-bearing."
    )


#: AC1's second position. A carrier can sit in two places relative to the
#: header that reads it: as a *later sibling* statement (the rows above), or
#: **inside the header's own scope**, so it has already run by the time the
#: name is entered. Both are pinned because they are not a superset of one
#: another -- the first is decided by source order in the binding resolver, the
#: second by containment in the header, and a rule that handled only one would
#: pass the table above while leaving this hole open.
#:
#: Runtime is identical for both positions: the header's own body rebinds
#: ``cs`` before the body of the *next* ``with`` is entered, and entering a
#: module, function, class or the captured ``1`` raises ``TypeError`` in either
#: arrangement. Measured on CPython 3.12.14 with ``x=1``:
#:
#:     with (cs := suppress(AssertionError)):
#:         import os as cs
#:     with cs:
#:         assert x != 1        # TypeError: 'module' object ...
CARRIER_IN_HEADER_ROWS = (
    ("import-as inside the header", "        import os as cs", False),
    (
        "import-from-as inside the header",
        "        from os import path as cs",
        False,
    ),
    ("def inside the header", "        def cs():\n            pass", False),
    ("class inside the header", "        class cs:\n            pass", False),
    (
        "a match capture inside the header",
        "        match [1]:\n            case [cs]:\n                pass",
        False,
    ),
    (
        "CONTROL a plain assign inside the header",
        "        cs = nullcontext()",
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "rebind", "second_assert_live"),
    CARRIER_IN_HEADER_ROWS,
    ids=[row[0] for row in CARRIER_IN_HEADER_ROWS],
)
def test_a_carrier_inside_the_reading_header_leaves_the_name_unenterable(
    label, rebind, second_assert_live
):
    """The carrier ran inside the header, and the header is still unenterable.

    The second half of #359 AC1. Here the rebind happens in the body of the
    ``with (cs := ...)`` header itself, so the carried suppressor is entered
    and exits *after* the rebind has already replaced the name. The following
    ``with cs:`` therefore reads the carrier's value, not the suppressor, and
    the position cannot be decided by the source ordering the sibling rows use.

    Like the table above, every row is executed and checked against CPython
    through ``_assert_entry_contract`` (AC2), and the control row is a plain
    assignment of a real context manager that must stay live (AC3).
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n" + rebind + "\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, second_assert_live)
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [second_assert_live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A carrier that "
        f"cannot be entered makes the assert unreachable, not load-bearing."
    )


def _async_loop_target_is_undecidable(label, source):
    """Pin why a loop target is excluded from the dead-entry rule.

    ``async for cs in <iterable>:`` binds the loop variable, so whether the
    later ``with cs:`` can be entered is decided entirely by the *element
    type* of the iterable -- something the analyzer cannot read without
    running the loop. Both outcomes are reachable from the same syntax:

    * an iterable of ``nullcontext()`` leaves an enterable value, so the
      assert is **live** and reporting it as dead would drop a real contract;
    * an iterable of ``int`` leaves an ``int``, so entering raises
      ``TypeError`` and the assert is unreachable.

    The row above uses the enterable iterable, and the table declares that
    verdict ``True``. This helper proves the exclusion is necessary rather
    than merely convenient: the same rebind spelling, differing only in what
    the loop yields, is live in one case and unreachable in the other. A rule
    that answered either way for both would be wrong on one of them.
    """
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False, True], (
        f"{label}: expected verdicts [False, True], got {results}. The rule "
        f"must decline a loop target rather than read a type it cannot know."
    )
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    try:
        asyncio.run(namespace["outer"](1, True, None))
    except AssertionError:
        pass  # the declared live row: the body ran and the assert fired
    else:
        raise AssertionError(f"{label}: the enterable-iterable row did not run its assert.")
    # The same spelling over a non-manager iterable is the other half of the
    # proof: it must be unreachable, or the rule would have had a decidable
    # answer available and declined for no reason.
    hostile = source.replace("yield nullcontext()", "yield 1").replace(
        "async for cs in agen():", "async for cs in gen():"
    )
    assert hostile != source, f"{label}: could not build the hostile variant"
    namespace = {}
    exec(compile(hostile, f"<{label}-hostile>", "exec"), namespace)  # noqa: S102
    try:
        asyncio.run(namespace["outer"](1, True, None))
    except AssertionError:
        raise AssertionError(
            f"{label}: the hostile variant reached its assert, so a loop "
            f"target is not actually undecidable and the rule should model it."
        ) from None
    except (TypeError, UnboundLocalError, NameError):
        return
    raise AssertionError(f"{label}: the hostile variant returned normally; the fixture is stale.")


def test_an_except_as_handler_is_decidable_even_after_a_conditional_store():
    """The ``ExceptHandler`` clause in ``_stores_of`` is load-bearing alone.

    The ``except-as`` row in ``WALRUS_REBINDING_SHAPES`` does not actually
    exercise the clause that lets a handler count as a decidable store. In
    that row the preceding store is the *unconditional* walrus, so removing

        or isinstance(entry[0], ast.ExceptHandler)

    from ``_stores_of`` falls back to that walrus, whose value is an
    ``ast.Call``, and ``_entry_is_dead`` declines at the "a call is a call"
    guard -- reaching the same answer by a different route. The clause can
    therefore be deleted with the whole table still green.

    It stops being redundant as soon as the prior store is *conditional*.
    Then the handler is the only store whose value is decidable, and without
    the clause ``_stores_of`` returns ``None`` and the rule declines to say
    anything. This row is what makes the clause's removal detectable.

    Whether the handler runs or not, ``cs`` cannot hold a usable context
    manager afterwards: if it ran, CPython deleted the name when the handler
    exited; if it did not, no suppressor was ever bound, so entry raises
    ``UnboundLocalError`` before the assert. Both paths are unreachable, so
    the honest verdict is ``False`` either way.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    if flag:\n"
        "        cs = contextlib.suppress(AssertionError)\n"
        "    try:\n"
        "        raise ValueError()\n"
        "    except ValueError as cs:\n"
        "        pass\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_entry_contract("an except-as after a conditional store", source, False, False)
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False], (
        f"expected [False], got {results}. The handler is the only decidable "
        f"store here, so this row is what pins the clause."
    )


#: #324: a walrus bound in a comprehension is a binding like any other.
#:
#: The comprehension's condition expression runs and leaves ``cs`` bound, so a
#: later ``with cs:`` really does enter the suppressor. Recording only
#: ``ast.Assign`` made this invisible and reported a swallowed assert as live.
WALRUS_NON_ASSIGN_SHAPES = (
    (
        "a walrus in a comprehension condition",
        "    [y for y in (0,) if (cs := contextlib.suppress(AssertionError))]",
    ),
    (
        "a walrus in a list comprehension filter",
        "    [y for y in (0, 1) if y and (cs := contextlib.suppress(AssertionError))]",
    ),
    (
        "a walrus in a generator expression filter",
        "    list(y for y in (0,) if (cs := contextlib.suppress(AssertionError)))",
    ),
    (
        "a walrus in a call argument",
        "    helper.consume((cs := contextlib.suppress(AssertionError)))",
    ),
)


@pytest.mark.parametrize(
    ("label", "binder"),
    WALRUS_NON_ASSIGN_SHAPES,
    ids=[shape[0] for shape in WALRUS_NON_ASSIGN_SHAPES],
)
def test_a_walrus_bound_outside_an_assignment_still_reaches_a_later_header(label, binder):
    """A walrus is a binding wherever it is written.

    The walrus does not have to sit in an assignment statement's right-hand
    side. A comprehension condition, a generator filter and a call argument all
    bind the name, and all of them are evaluated before the ``with`` that
    follows.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    import pytest\n" + binder + "\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [False]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. The walrus "
        f"binds `cs` before the `with` is reached, so the assert is swallowed."
    )


#: #324: a ``match`` capture is a store, and it is not reachable from any
#: statement's target list -- ``case [cs]:`` parses to an ``ast.MatchAs`` whose
#: ``name`` is a plain string, not an ``ast`` target, so the target walk that
#: covers tuple, list and starred unpacking never sees it.
#:
#: A capture rebinds the name to the value that was matched. Left unmodelled,
#: the suppressor carried in by the earlier walrus survived the clause and
#: reached the later ``with cs:``, which reported the assert under it as
#: swallowed. Executing the fixture confirms the opposite: the capture puts a
#: non-suppressor in ``cs``, so the second assert is **live** and its
#: ``AssertionError`` propagates.
MATCH_CAPTURE_SHAPES = (
    (
        "a sequence-pattern capture",
        "    match flag:\n        case [cs]:\n            pass",
    ),
    (
        "an as-pattern capture",
        "    match flag:\n        case [other] as cs:\n            pass",
    ),
    (
        "a mapping-pattern capture",
        "    match flag:\n        case {'key': cs}:\n            pass",
    ),
    (
        "a starred capture",
        "    match flag:\n        case [other, *cs]:\n            pass",
    ),
    (
        "an irrefutable as-pattern capture",
        "    match flag:\n        case _ as cs:\n            pass",
    ),
    (
        "a mapping rest capture",
        "    match flag:\n        case {'key': 1, **cs}:\n            pass",
    ),
)


#: #342. Whether a capture retires a carried suppressor is a property of the
#: *clause*, not of the capture: a capture binds as a side effect of its clause
#: being selected, and a refutable clause is not selected for every subject.
#: ``case _ as cs:`` is irrefutable, so that one always binds and always
#: retires; the five refutable forms only bind when they are selected.
MATCH_CAPTURE_ALWAYS_BINDS_SHAPES = (MATCH_CAPTURE_SHAPES[4],)

#: The three conditions that make a capture *guaranteed*, each pinned apart so
#: that dropping one of them is caught rather than shipped silently:
#:
#: * **irrefutable pattern** -- ``case _ as cs:`` has nothing to fail;
#: * **last clause** -- an earlier capture can be pre-empted by a later clause
#:   that is selected instead, so it is not guaranteed.
#:
#: A guard is *not* one of the conditions: the name is bound once the pattern
#: matches and is not unbound when a guard turns out false, which was measured
#: against CPython 3.12.14 rather than assumed. The guard row below pins that
#: measurement, so a future change cannot quietly reintroduce a guard check on
#: the strength of intuition.
#:
#: Each row turns one condition off while leaving the other in place, which is
#: what makes it discriminate: dropping "last clause" turns the second row's
#: verdict, and dropping "irrefutable" is caught by the refutable rows above.
MATCH_CAPTURE_GUARANTEE_ROWS = (
    (
        "a false guard does not undo a binding the pattern already made",
        # The pattern is irrefutable, so `cs` is bound to the subject before
        # the guard is ever evaluated. The guard then fails, the clause body is
        # skipped, and `cs` is *not* restored -- so the alias really is
        # retired. This row is the measured answer to "does a guard block the
        # binding", and it is the reason `_capture_always_binds` ignores guards.
        "    match flag:\n        case _ as cs if x > 100:\n            pass\n",
        "unreachable:TypeError",
    ),
    (
        "a refutable pattern with a false guard is decided by the pattern alone",
        # `flag=[1]` selects the clause and binds `cs = 1` before the guard is
        # evaluated, so the later `with cs:` raises; `flag='x'` selects nothing
        # and leaves the carried suppressor bound, so the assert is swallowed.
        # A guard is therefore orthogonal: the pattern alone decides which of
        # the two happened, and both outcomes are defeated either way.
        "    match flag:\n        case [cs] if x > 100:\n            pass\n",
        ("unreachable:TypeError", "swallowed", "swallowed"),
    ),
    (
        "a capture in an earlier clause is pre-empted by a later wildcard",
        # `flag=[1]` selects the *first* clause and binds `cs = 1`; the trailing
        # `case _` never gets the chance to pre-empt it. `flag='x'` falls
        # through to the wildcard, which captures nothing, so the carried
        # suppressor survives. One source, two runtimes, one verdict: the
        # analyzer must not read the *matching* path and call the assert live.
        "    match flag:\n        case [cs]:\n            pass\n        case _:\n            pass\n",
        ("unreachable:TypeError", "swallowed", "swallowed"),
    ),
    (
        "an or-pattern of two sequence captures still fails on a non-sequence",
        # Every alternative here is a sequence pattern, so neither can match a
        # mapping or a string: the clause is refutable even though it *looks*
        # exhaustive. `flag=[1]` selects it and binds a list.
        "    match flag:\n        case [*cs] | [*cs]:\n            pass\n",
        ("unreachable:TypeError", "swallowed", "swallowed"),
    ),
    (
        "an or-pattern of value patterns is refutable",
        "    match flag:\n        case 1 | 2 as cs:\n            pass\n",
        "swallowed",
    ),
)

#: A subject that selects each clause, per shape. Used to prove the "bound"
#: half of the contract independently of the verdict being asserted.
MATCH_CAPTURE_MATCHING_SUBJECT = {
    "a sequence-pattern capture": [1],
    "an as-pattern capture": [1],
    "a mapping-pattern capture": {"key": 1},
    "a starred capture": [1, 2],
    "an irrefutable as-pattern capture": "anything",
    "a mapping rest capture": {"key": 1},
}


def _capture_fixture(capture):
    """The two-assert walrus/capture fixture the capture rows share."""
    source = (
        "def outer(x, flag):\n"
        "    import contextlib\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + capture + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    return source, ast.parse(source)


def _verdicts(tree):
    """The analyzer's verdict for each assert, in source order."""
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, "fixture declared no assert to check"
    return [_is_enforced(function, node, tree) for node in asserts]


def _execute_outer(source, flag):
    """Run the fixture and report what became of the second assert."""
    namespace = {}
    exec(compile(source, "<capture-fixture>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](2, flag)
    except AssertionError:
        return "live"
    except TypeError as error:
        # Entering a non-context-manager. `NameError` and `UnboundLocalError`
        # are deliberately *not* caught: those are the loud forms #334 and the
        # `except ... as` shape own, and folding them in here would let a
        # fixture that raises for an unrelated reason still pass.
        return f"unreachable:{type(error).__name__}"
    return "swallowed"


def _execute_guarantee(source, subject):
    """Run a guarantee-row fixture, which takes the or-pattern's two names."""
    namespace = {}
    exec(compile(source, "<guarantee-fixture>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](2, subject, "left", "right")
    except AssertionError:
        return "live"
    except TypeError as error:
        return f"unreachable:{type(error).__name__}"
    return "swallowed"


@pytest.mark.parametrize(
    ("label", "clause", "outcome"),
    MATCH_CAPTURE_GUARANTEE_ROWS,
    ids=[row[0] for row in MATCH_CAPTURE_GUARANTEE_ROWS],
)
def test_a_capture_binds_guaranteedly_only_on_an_irrefutable_last_unguarded_clause(
    label, clause, outcome
):
    """A capture retires the alias only on a clause that cannot be passed over.

    Each row turns exactly one of the three conditions off while leaving the
    other two in place, so a regression that drops any single condition flips
    the verdict for that row. The expected outcome is measured by executing
    the fixture rather than asserted from the source.
    """
    source = (
        "def outer(x, flag, a, b):\n"
        "    import contextlib\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + clause + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    tree = ast.parse(source)
    subjects = ([1], "x", {"key": 1})
    # A row either behaves the same for every subject, or spells out one
    # outcome per subject. The distinction is recorded per row rather than
    # guessed, so a refutable clause is not forced to a single verdict.
    expected_runtimes = outcome if isinstance(outcome, tuple) else (outcome,) * len(subjects)
    for subject, expected_runtime in zip(subjects, expected_runtimes):
        assert _execute_guarantee(source, subject) == expected_runtime, (
            f"{label}: subject {subject!r} did not produce {expected_runtime}."
        )
    # The analyzer is per-source, so it cannot report a verdict per subject.
    # It reports the *safe* reading of a row whose subjects disagree: the
    # assert is only "enforced" when the clause is guaranteed to bind on every
    # path, which for these rows is exactly the ones where every subject
    # reaches an unreachable header. A row with a mix reports defeated.
    assert "live" not in expected_runtimes, f"{label}: row has a live subject"
    always_binds = all(outcome == "unreachable:TypeError" for outcome in expected_runtimes)
    results = _verdicts(tree)
    expected = [False, always_binds]
    assert results == expected, f"{label}: expected verdicts {expected}, got {results}."


@pytest.mark.parametrize(
    ("label", "capture"),
    MATCH_CAPTURE_ALWAYS_BINDS_SHAPES,
    ids=[shape[0] for shape in MATCH_CAPTURE_ALWAYS_BINDS_SHAPES],
)
def test_an_irrefutable_capture_always_retires_a_carried_suppressor(label, capture):
    """A capture that cannot fail to bind must retire the alias on every path.

    ``case _ as cs:`` matches whatever subject it is given, so ``cs`` really is
    rebound before the later ``with cs:``. Executed against four different
    subjects, the second assert is always unreachable because the captured
    value is not a context manager -- and the analyzer agrees.
    """
    source, tree = _capture_fixture(capture)
    for subject in ("anything", [1], {"key": 1}, None):
        assert _execute_outer(source, subject) == "unreachable:TypeError", (
            f"{label}: subject {subject!r} should bind `cs` to a "
            f"non-context-manager, making the assert unreachable."
        )
    expected = [False, True]
    results = _verdicts(tree)
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. An irrefutable "
        f"capture always binds, so the carried suppressor cannot survive it."
    )


@pytest.mark.parametrize(
    ("label", "capture"),
    tuple(
        shape for shape in MATCH_CAPTURE_SHAPES if shape not in MATCH_CAPTURE_ALWAYS_BINDS_SHAPES
    ),
    ids=[
        shape[0] for shape in MATCH_CAPTURE_SHAPES if shape not in MATCH_CAPTURE_ALWAYS_BINDS_SHAPES
    ],
)
def test_a_refutable_capture_does_not_retire_a_binding_it_never_made(label, capture):
    """A clause that was not selected binds nothing, so it retires nothing.

    This is #342. With ``flag='x'`` no clause matches, the capture never runs,
    and the carried suppressor is still bound -- executed, the second assert is
    **swallowed**. Calling it enforced certifies a defeated contract as
    load-bearing, so the alias has to be kept in force instead.
    """
    source, tree = _capture_fixture(capture)
    assert _execute_outer(source, "x") == "swallowed", (
        f"{label}: with flag='x' no clause matches, so the assert is swallowed "
        f"and the fixture no longer demonstrates the #342 defect."
    )
    expected = [False, False]
    results = _verdicts(tree)
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A capture whose "
        f"clause was not selected cannot retire the carried suppressor."
    )


@pytest.mark.parametrize(
    ("label", "capture"),
    tuple(
        shape for shape in MATCH_CAPTURE_SHAPES if shape not in MATCH_CAPTURE_ALWAYS_BINDS_SHAPES
    ),
    ids=[
        shape[0] for shape in MATCH_CAPTURE_SHAPES if shape not in MATCH_CAPTURE_ALWAYS_BINDS_SHAPES
    ],
)
def test_a_selected_capture_binds_a_value_that_cannot_be_entered(label, capture):
    """When the clause *is* selected, the captured value is not a context manager.

    This is #336's reachability question rather than #342's suppression one:
    the capture does run, and it binds the matched subject. Entering it raises
    ``TypeError``, so the assert below is genuinely unreachable. Recorded
    separately so the two directions are not conflated, and so a future fix
    cannot make the no-match case "safe" by also claiming this one.
    """
    subject = MATCH_CAPTURE_MATCHING_SUBJECT[label]
    source, _tree = _capture_fixture(capture)
    assert _execute_outer(source, subject) == "unreachable:TypeError", (
        f"{label}: subject {subject!r} is expected to bind `cs` to a value that "
        f"cannot be entered, making the assert unreachable."
    )


#: Two over-fix guards. The capture rules must not retire a binding they did
#: not make, in either direction -- the first is a *missed* retirement and the
#: second is a *spurious* one, and both were live bugs while this was written.
MATCH_CAPTURE_SCOPE_ROWS = (
    (
        "a capture in a nested function does not retire the outer binding",
        ("    def inner():\n        match flag:\n            case [cs]:\n                pass\n"),
        [False, False],
    ),
    # #350. A class body is a namespace too, and the walk that finds captures
    # used to stop at nested `def`/`lambda` but not at `ClassDef`. So a capture
    # written in a class body retired the *enclosing function's* local and
    # reported the assert under the later `with cs:` as enforced.
    #
    # The subject is `[1]`, so the capture binds the integer 1: entering it
    # raises TypeError before the assert. The nested-function row above is the
    # control -- it was already correct, so the two together prove the class
    # row is not passing for the same reason as a pre-existing decline.
    (
        "a capture in a nested class body does not retire the outer binding",
        ("    class Inner:\n        match flag:\n            case [cs]:\n                pass\n"),
        [False, False],
    ),
    (
        "a capture of a different name does not retire this one",
        "    match flag:\n        case [other]:\n            pass\n",
        [False, False],
    ),
    (
        "a capture after the header does not retire it retroactively",
        None,  # the capture is appended after the header instead
        [False, False],
    ),
    (
        "a capture before the header does retire it",
        # An irrefutable capture is used here so the row still demonstrates a
        # *retirement* under #342. A refutable capture before the header no
        # longer retires anything, which is the defect the neighbouring
        # `test_a_refutable_capture_does_not_retire_a_binding_it_never_made`
        # covers; keeping the refutable spelling in this row would make it a
        # second copy of that test rather than a check on ordering.
        "    match flag:\n        case _ as cs:\n            pass\n",
        [False, True],
    ),
)


#: A capture is not a *statement*, so nothing in the block that holds a header
#: can be found by looking for a store statement -- but a header written inside
#: the capturing ``case`` body is reached only on the path where that clause was
#: selected, so it has had the capture run by then. These rows are what makes
#: that block in ``_bindings_before`` load-bearing rather than dead: without it
#: the capture is invisible to a header in the same block.
#:
#: All three report the second assert as defeated rather than enforced. Under
#: #342 a refutable capture retires the alias only on the path it is selected,
#: and a header written inside the clause body is on exactly that path -- where
#: the captured value is a plain subject, not a context manager, so entering it
#: raises and the assert is unreachable. The path that does *not* select the
#: clause never reaches the header at all, so there is no live reading of it
#: that the header has to preserve.
MATCH_CAPTURE_OWNS_NESTED_HEADER_ROWS = (
    (
        "a header in the capturing clause body reads the capture",
        (
            "    match flag:\n"
            "        case [cs]:\n"
            "            with cs:\n"
            "                assert x != 1\n"
        ),
        False,
    ),
    (
        "a header in a second clause body reads that clause's capture",
        (
            "    match flag:\n"
            "        case [other]:\n"
            "            pass\n"
            "        case [cs]:\n"
            "            with cs:\n"
            "                assert x != 1\n"
        ),
        False,
    ),
    (
        "a header in a clause that captures nothing still reads the suppressor",
        (
            "    match flag:\n"
            "        case [other]:\n"
            "            with cs:\n"
            "                assert x != 1\n"
        ),
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "enforced"),
    MATCH_CAPTURE_OWNS_NESTED_HEADER_ROWS,
    ids=[row[0] for row in MATCH_CAPTURE_OWNS_NESTED_HEADER_ROWS],
)
def test_a_capture_reaches_a_header_nested_in_its_own_clause_body(label, body, enforced):
    """The capture is not a store *statement*, so the block holding a header
    sees no store at all and would fall back to the carried suppressor.

    Every row here is read as ``defeated``, and that is the point. A
    *refutable* capture cannot be decided from the source: the same program
    admits a subject that selects the capture -- leaving a real, possibly
    live, context manager at the nested header -- and a subject that does not,
    which leaves the carried suppressor in force and swallows the assert.
    Since this module answers a source-level question, one admitting subject
    is enough to refuse to certify the assert, and ``defeated`` is the safe
    direction (#308 criterion 1).

    So a header inside the capturing clause body is *not* promoted back to
    ``enforced`` just because the clause was written to capture, and a header
    in a non-capturing clause is not reported swallowed for the same reason.
    Both are ``defeated`` because the capture may not have run.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + body
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 2, f"{label}: fixture declared {len(asserts)} asserts, expected 2"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False, enforced], (
        f"{label}: expected verdicts [False, {enforced}], got {results}. "
        f"The capture binds `cs` before the nested header is reached."
    )


@pytest.mark.parametrize(
    ("label", "extra", "expected"),
    MATCH_CAPTURE_SCOPE_ROWS,
    ids=[row[0] for row in MATCH_CAPTURE_SCOPE_ROWS],
)
def test_a_match_capture_respects_its_own_scope_and_position(label, extra, expected):
    """A capture retires the carried suppressor, and only where it really binds.

    A ``match`` inside a nested ``def`` binds in *that* scope, so it must not
    retire the outer function's name -- that would report a swallowed assert as
    live. A capture of some other name must not retire this one. And a capture
    written *after* the ``with`` header cannot have run when the header is
    read, so it must not retire it either.
    """
    capture_after = (
        "    match flag:\n        case [cs]:\n            pass\n" if extra is None else extra
    )
    after = "    match flag:\n        case [cs]:\n            pass\n" if extra is None else ""
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    import pytest\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + capture_after + "    with cs:\n        assert x != 1\n" + after
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == expected, f"{label}: expected verdicts {expected}, got {results}."


#: #316: a user-defined context manager swallows an assert exactly when its
#: ``__exit__`` returns truthy for the raised ``AssertionError``. That is a
#: different mechanism from every named suppressor -- none of the other rules
#: reads a return value, because none of them can.
#:
#: Executed, each of these really is silent::
#:
#:     >>> probe(1)          # returns normally, rc=0
#:
#: so reporting the assert as ``enforced`` would certify a disarmed contract as
#: load-bearing. The rows are paired with the loud controls below, because the
#: failure mode of a rule like this is firing on an ordinary context manager
#: rather than missing a suppressor.
USER_EXIT_SWALLOW_SHAPES = (
    # --- the filed shape, in the three bindings #316's criterion 4 names ---
    (
        "module-level instance",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    (
        "instance bound in a nested if",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        (
            "    if flag:\n"
            "        helper = Suppressor()\n"
            "        with helper:\n"
            "            assert x != 1"
        ),
        False,
    ),
    (
        "local alias",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        "    cs = Suppressor()\n    with cs:\n        assert x != 1",
        False,
    ),
    # The other two spellings of the same runtime test.
    (
        "named exception parameter",
        ("def __exit__(self, exc_type, exc, tb):\n    return exc_type is AssertionError"),
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    (
        "issubclass spelling",
        "def __exit__(self, *exc):\n    return issubclass(exc[0], AssertionError)",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    # The same test spelled `==`. At runtime `exc_type == AssertionError` is
    # true exactly when an assert was swallowed, so declining it left a real
    # swallow reported as `enforced`. Only `Is` was accepted, and the operator
    # guard was not pinned by any row, so both sides of that operator were
    # unpinned: dropping the guard let a *negated* test through, and adding `Eq`
    # closes the missed detection.
    (
        "equality spelling",
        "def __exit__(self, *exc):\n    return exc[0] == AssertionError",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    # --- loud controls: an __exit__ that does not swallow ---
    (
        "returns False",
        "def __exit__(self, *exc):\n    return False",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    (
        "returns None",
        "def __exit__(self, *exc):\n    return None",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    (
        "no return at all",
        "def __exit__(self, *exc):\n    pass",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    (
        "tests for a different exception",
        "def __exit__(self, *exc):\n    return exc[0] is ValueError",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    # The `issubclass` form tested against a *different* exception. The only
    # `issubclass` row above spells the operand `AssertionError`, so dropping
    # that operand check left the whole suite green while the rule started
    # reporting this live assert as defeated.
    (
        "issubclass against a different exception",
        "def __exit__(self, *exc):\n    return issubclass(exc[0], ValueError)",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    # The negated test. `is not AssertionError` is false when an assert was
    # swallowed, so this `__exit__` propagates and the assert stays live --
    # reading it as a swallow would be the damaging direction. This row is what
    # makes the operator guard non-vacuous: removing it lets this through.
    (
        "negated test",
        "def __exit__(self, *exc):\n    return exc[0] is not AssertionError",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    (
        "raises instead of returning",
        "def __exit__(self, *exc):\n    raise RuntimeError",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "exit_body", "body", "enforced"),
    USER_EXIT_SWALLOW_SHAPES,
    ids=[row[0] for row in USER_EXIT_SWALLOW_SHAPES],
)
def test_a_user_exit_that_swallows_assertion_error_is_a_defeat(label, exit_body, body, enforced):
    """#316: the ``__exit__`` return value is the mechanism, not the name.

    The named suppressors are known by what they are called. A user-defined
    manager is knowable only by what its ``__exit__`` *does*, and a return that
    is truthy precisely for ``AssertionError`` swallows the failure silently.

    Both halves of that are pinned here. The swallowing rows must be reported
    defeated, and the loud rows -- ``False``, ``None``, no return, a different
    exception, a raise -- must stay enforced, because an ``__exit__`` that does
    not swallow leaves a real contract in place and reporting it defeated
    would drop a live assert.
    """
    # Every line of `exit_body` needs the class-body indent, not just the first:
    # the table stores the `def` line and its body unindented so each row reads
    # as the method alone.
    indented_exit = "\n".join("    " + line for line in exit_body.splitlines())
    source = (
        "class Suppressor:\n"
        "    def __enter__(self):\n"
        "        return self\n" + indented_exit + "\ndef outer(x, flag):\n" + body + "\n"
    )
    tree = ast.parse(source)
    outer = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(outer) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(outer, node, tree) for node in asserts]
    assert results == [enforced], (
        f"{label}: expected the assert to be "
        f"{'enforced' if enforced else 'unenforced'}, got {results}"
    )


def test_the_user_exit_rule_does_not_fire_on_an_ordinary_context_manager():
    """#316 criterion 2: no over-breadth on the real pinned file.

    A rule that reported every class with an ``__exit__`` as a defeat would
    pass every row above and still be worthless, because ``pytest``'s and the
    harness's own managers all have one. The budget is stated as a number: the
    real file must measure the same asserts walked and the same zero unenforced
    as it does on master. Measured on both trees, not asserted from memory.
    """
    tree = support.milestones_tree()
    walked = 0
    unenforced = 0
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Assert):
                continue
            walked += 1
            if not _is_enforced(function, node, tree):
                unenforced += 1
    assert unenforced == 0, (
        f"the #316 rule reported {unenforced} of {walked} real pinned asserts "
        f"as defeated; it must not fire on an ordinary context manager"
    )
    assert walked == 143, (
        f"the pinned file now walks {walked} asserts, expected 143 -- either "
        f"the file changed or the walk lost sites"
    )


def test_an_unreadable_constructor_is_not_assumed_to_suppress():
    """A factory or a parameter is not followed, so nothing is invented.

    Following one hop from ``make()`` to whatever it returns would mean
    assuming that an arbitrary call produces a swallowing context manager,
    which is the over-breadth #316's criterion 2 rules out. The class below
    *is* a suppressor -- only the binding is unreadable -- so this row
    separates "cannot see it" from "it is not one".
    """
    source = (
        "class Suppressor:\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *exc):\n"
        "        return exc[0] is AssertionError\n"
        "def make():\n"
        "    return Suppressor()\n"
        "def outer(x):\n"
        "    with make():\n"
        "        assert x != 1\n"
    )
    tree = ast.parse(source)
    outer = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(outer) if isinstance(node, ast.Assert))
    assert _is_enforced(outer, target, tree), (
        "an unreadable constructor was assumed to produce a swallowing context "
        "manager; the rule must decline rather than assume"
    )


def test_only_a_module_level_class_counts():
    """#316 criterion 2, the scope half: a nested class is not matched.

    A class nested inside another is reached as ``Outer.Inner()`` in a ``with``
    header. Collecting class definitions from every scope would match the inner
    *name* alone, so a bare ``Inner`` meaning something else entirely would be
    reported as a defeat. The limit is deliberate, and it is only a real limit
    if something pins it -- an earlier version of this rule collected classes
    from any scope and the whole suite stayed green, which is exactly the
    no-op mutation the #310 dunder rule shipped as.

    The class below really is a swallowing context manager; only its nesting
    puts it out of reach. That is what separates "cannot see it" from "it is
    not one".
    """
    source = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def __enter__(self):\n"
        "            return self\n"
        "        def __exit__(self, *exc):\n"
        "            return exc[0] is AssertionError\n"
        "def outer(x):\n"
        "    with Outer.Inner():\n"
        "        assert x != 1\n"
    )
    tree = ast.parse(source)
    outer = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(outer) if isinstance(node, ast.Assert))
    assert "Inner" not in support._locally_defined_classes(tree), (
        "a nested class leaked into the module-level class table; the rule "
        "would then match a bare `Inner` that is a different object"
    )
    assert _is_enforced(outer, target, tree), (
        "a class nested inside another was matched by its bare inner name"
    )


def test_a_module_scope_lookup_does_not_descend_into_a_function_body():
    """#316: the two scopes must stay distinguishable, or nothing pins them.

    ``_assigned_value`` reads a module's *own top-level statements* and a
    function's statements separately, with the function consulted first. That
    split is load-bearing: ``_constructed_class_of`` relies on a function-local
    binding shadowing an outer one, and on the module fallback firing only when
    the function really has nothing to say.

    Using ``ast.walk`` on the module would collapse the two -- it descends into
    every function body, so a module lookup would find assignments belonging to
    an unrelated function. The row below is the shape that tells the two
    readings apart, and it is not reachable from any other #316 test: they all
    resolve their binding inside the one function under test, so the module
    fallback is never the thing being exercised.

    Reintroducing ``ast.walk`` here leaves the whole suite green while flipping
    this row: the module-level ``helper`` is a loud ``Loud``, so the assert is
    live and must stay ``enforced``, but the walk finds ``unrelated``'s
    ``helper = Suppressor()`` and reports the live assert as a defeat -- the
    damaging direction.
    """
    source = (
        "class Suppressor:\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *exc):\n"
        "        return exc[0] is AssertionError\n"
        "class Loud:\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *exc):\n"
        "        return False\n"
        "def unrelated(y):\n"
        "    helper = Suppressor()\n"
        "helper = Loud()\n"
        "def probe(x):\n"
        "    with helper:\n"
        "        assert x != 1\n"
    )
    tree = ast.parse(source)
    probe = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "probe"
    )
    target = next(node for node in ast.walk(probe) if isinstance(node, ast.Assert))
    assert _is_enforced(probe, target, tree), (
        "a module-scope lookup descended into an unrelated function body and "
        "adopted its binding; a live assert was reported as defeated"
    )


#: #375 round 4. A **conditional** store that may have superseded a
#: function-local carrier. The carrier is unconditional and settles the name on
#: its own, but a later conditional store can replace it with something
#: enterable, and then the assert under the header is live. The value is
#: therefore undecidable and the rule must decline.
#:
#: Every row is executed under CPython with ``flag`` **true** by
#: ``_assert_entry_contract``, which is the path on which the store runs and the
#: assert really fires. These are the four false-dead verdicts ``origin/master``
#: (``ed9d9b0``) returned for exactly these sources.
STALE_LOCAL_CARRIER_SHAPES = (
    (
        "an import carrier superseded by a conditional store",
        "    import os as cs",
        "    if flag:\n        cs = nullcontext()",
    ),
    (
        "a def carrier superseded by a conditional store",
        "    def cs():\n        pass",
        "    if flag:\n        cs = nullcontext()",
    ),
    (
        "a class carrier superseded by a conditional store",
        "    class cs:\n        pass",
        "    if flag:\n        cs = nullcontext()",
    ),
    (
        "an import carrier superseded by a loop store",
        "    import os as cs",
        "    for _ in (1,):\n        cs = nullcontext()",
    ),
    # Controls for the third review round, all of which must stay LIVE. Each
    # is a shape the second round read as decidable-and-dead, and each is in
    # fact decidable-and-live: a false operand in an `or` does not make the
    # condition false, and a loop whose *last* element is a context manager
    # leaves that element bound.
    (
        "a false operand in an or-ed condition",
        "    import os as cs",
        "    if flag or False:\n        cs = nullcontext()",
    ),
    (
        "a false operand in an or-ed condition after a def carrier",
        "    def cs():\n        pass",
        "    if False or flag:\n        cs = nullcontext()",
    ),
    (
        "a loop whose last element is a context manager",
        "    import os as cs",
        "    if flag:\n        for cs in (None, nullcontext()):\n            pass",
    ),
    # The rows below are the fourth review round. An independent reviewer
    # executed 73 fixtures under real CPython and found these three reported
    # DEAD while CPython reaches the assert. A false-dead is the damaging
    # direction: it certifies a real contract as swallowed, so each row below
    # is a LIVE header that an earlier revision got backwards.
    (
        "a shadowed builtin constructor returning a manager",
        "    import os as cs\n    def int():\n        return nullcontext()",
        "    if flag:\n        cs = int()",
    ),
    (
        "a loop body rebinding the target to a manager",
        "    import os as cs",
        "    if flag:\n        for cs in (None,):\n            cs = nullcontext()",
    ),
    (
        "a conditional manager after an unconditional none store",
        "    import os as cs\n    cs = None",
        "    if flag:\n        cs = nullcontext()",
    ),
    # The row below is the fifth review round's one *live* carrier row. A fresh
    # review of `86fab6d` reproduced all four findings by execution; this one is
    # the case where CPython genuinely enters the header, so the correct verdict
    # is LIVE.
    #
    # #388-5c. The condition path was handed no enclosing scope at all, so a
    # *locally* defined `list()` was read as the builtin and `if list():` was
    # decided as the empty builtin -- retiring a branch CPython enters, because
    # the local definition returns a real context manager.
    (
        "a locally shadowed constructor guarding a branch",
        "    import os as cs\n    def list():\n        return nullcontext()",
        "    if list():\n        cs = nullcontext()",
    ),
    # #388-5a's discriminating half. The row above pins the *zero-argument*
    # form, whose branch genuinely never runs. These four pin the
    # **argument-bearing** forms, whose branches CPython *does* enter because
    # the argument produces an element. Emptiness was decided from the
    # constructor *type* rather than from the call's argument list, so every
    # one of them was read as the empty builtin and the branch was retired --
    # a false-dead, which is the damaging direction.
    #
    # Each row is therefore executed under CPython with `flag` true, so the
    # fixture proves the branch really is entered before the LIVE verdict is
    # checked.
    (
        "a branch guarded by a set built from a literal",
        "    import os as cs",
        "    if set([1]):\n        cs = nullcontext()",
    ),
    (
        "a branch guarded by a list built from a tuple",
        "    import os as cs",
        "    if list((1,)):\n        cs = nullcontext()",
    ),
    (
        "a branch guarded by a dict built from a keyword",
        "    import os as cs",
        "    if dict(a=1):\n        cs = nullcontext()",
    ),
    (
        "a branch guarded by a bytearray built from bytes",
        "    import os as cs",
        "    if bytearray(b'x'):\n        cs = nullcontext()",
    ),
)


#: The mirror of :data:`STALE_LOCAL_CARRIER_SHAPES`, and the reason a first cut
#: of the guard above was rejected. A conditional store that is itself pinned
#: **non-enterable** cannot rescue the header on the path where it runs, so the
#: carrier still decides every path and the assert is unreachable either way.
#: Declining these would replace a correct dead verdict with a false-live one
#: -- a regression against ``origin/master``, not a repair.
#:
#: Measured on CPython 3.12.14 with ``outer(1, False, None)`` and
#: ``outer(1, True, None)``: both raise ``TypeError`` for all four rows, so the
#: assert is unreachable on both paths and the correct verdict is dead.
DEAD_CONDITION_AFTER_CARRIER_SHAPES = (
    (
        "a None store after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = None",
    ),
    (
        "a None store after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        cs = None",
    ),
    (
        "a None store after a class carrier",
        "    class cs:\n        pass",
        "    if flag:\n        cs = None",
    ),
    (
        "an int store after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = 42",
    ),
    (
        "a list store after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = [1]",
    ),
    (
        "a str store after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = 'x'",
    ),
    (
        "a starred target after an import carrier",
        "    import os as cs",
        "    if flag:\n        *cs, = (helper,)",
    ),
    (
        "a starred target after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        *cs, = (helper,)",
    ),
    (
        "a starred target after a class carrier",
        "    class cs:\n        pass",
        "    if flag:\n        *cs, = (helper,)",
    ),
    (
        "a chained bare target after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = (other, x) = (helper, 1)",
    ),
    (
        "a chained bare target after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        cs = (other, x) = (helper, 1)",
    ),
    (
        "a chained bare target after a class carrier",
        "    class cs:\n        pass",
        "    if flag:\n        cs = (other, x) = (helper, 1)",
    ),
    (
        "a starred target on a non-tuple right-hand side",
        "    import os as cs",
        "    if flag:\n        *cs, = helper",
    ),
    (
        "a leading starred target after an import carrier",
        "    import os as cs",
        "    if flag:\n        *cs, rest = pair",
    ),
    (
        "a nested starred target after an import carrier",
        "    import os as cs",
        "    if flag:\n        a, (b, *cs) = pair",
    ),
    (
        "a trailing starred target after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        first, *cs = pair",
    ),
    (
        "a trailing starred target after a class carrier",
        "    class cs:\n        pass",
        "    if flag:\n        first, *cs = pair",
    ),
    # The rows below are the second review round. The first cut of this repair
    # declined any conditional store whose value it could not read as a literal,
    # and that is true too often: a builtin constructor call, an element taken
    # from a literal container, and a store inside a branch that provably never
    # runs are each decidable, and declining them reported a dead header live
    # where CPython raises on every path.
    (
        "a builtin constructor call after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = int()",
    ),
    (
        "a builtin constructor call after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        cs = list()",
    ),
    (
        "a builtin constructor with an argument",
        "    import os as cs",
        "    if flag:\n        cs = bool(1)",
    ),
    (
        "an element of a literal container",
        "    import os as cs",
        "    if flag:\n        cs, other = (None, 1)",
    ),
    (
        "a list-target element of a literal container",
        "    import os as cs",
        "    if flag:\n        [cs] = [None]",
    ),
    (
        "a literal element inside a nested pattern",
        "    import os as cs",
        "    if flag:\n        cs, (other, third) = (None, (1, 2))",
    ),
    (
        "a loop over a literal container of None",
        "    import os as cs",
        "    if flag:\n        for cs in (None,):\n            pass",
    ),
    (
        "a store in a literally false branch",
        "    import os as cs",
        "    if False:\n        cs = nullcontext()",
    ),
    (
        "a store in a conjunctively false branch",
        "    import os as cs",
        "    if flag and False:\n        cs = nullcontext()",
    ),
    (
        "a store in a false branch after a class carrier",
        "    class cs:\n        pass",
        "    if flag and False:\n        cs = nullcontext()",
    ),
    # The rows below are the third review round. Each was a regression the
    # second round introduced while fixing the first, and each is the kind of
    # mistake that only a fresh fixture finds: a type missing from the
    # non-enterable set, a boolean operator read the wrong way round, a loop
    # spelling of a dead branch, and a loop whose *last* element -- not its
    # first -- is the value left bound.
    (
        "a range constructor after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = range(3)",
    ),
    (
        "an argument-less range constructor",
        "    import os as cs",
        "    if flag:\n        cs = range()",
    ),
    (
        "a frozenset constructor after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        cs = frozenset()",
    ),
    (
        "a store in an and-ed empty-literal branch",
        "    import os as cs",
        "    if flag and ():\n        cs = nullcontext()",
    ),
    (
        "a store in a while-false branch",
        "    import os as cs",
        "    while False:\n        cs = nullcontext()",
    ),
    (
        "a loop whose last element is not enterable",
        "    import os as cs",
        "    if flag:\n        for cs in (nullcontext(), None):\n            pass",
    ),
    (
        "a loop over an empty builtin container",
        "    import os as cs",
        "    if flag:\n        for cs in set():\n            pass",
    ),
    (
        "a nested pattern holding a starred element",
        "    import os as cs",
        "    if flag:\n        cs, (other, rest3) = (None, (1, 2))",
    ),
    # The remaining fourth-round rows were reported LIVE while CPython raises
    # before the assert. These are the false-live direction: they do not hide a
    # contract, but they claim an entry works when it cannot, which is the
    # mirror error and still blocks a merge.
    (
        "a store in a branch guarded by an or of false operands",
        "    import os as cs",
        "    if flag and (False or ()):\n        cs = nullcontext()",
    ),
    (
        "a store in a branch guarded by an empty builtin call",
        "    import os as cs",
        "    if set():\n        cs = nullcontext()",
    ),
    (
        "a loop over an empty bytearray",
        "    import os as cs",
        "    if flag:\n        for cs in bytearray():\n            pass",
    ),
    (
        "an unconditional constructor store",
        "    import os as cs\n    cs = int()",
        "",
    ),
    (
        "a store in a branch guarded by an empty dict",
        "    import os as cs",
        "    if {}:\n        cs = nullcontext()",
    ),
    # The rows below are the fifth review round's remaining carrier rows. A
    # fresh review of `86fab6d` reproduced all four findings by execution, and
    # these three are the ones where CPython raises before the assert anyway, so
    # the correct verdict is DEAD. A regression here shows up as a row flipping
    # to LIVE -- the mild direction, but still a row claiming an entry works
    # when it cannot.
    #
    # #388-5a. Emptiness was decided from the constructor *type* rather than from
    # the call's argument list, so `set([1])`, `list((1,))`, `dict(a=1)` and
    # `bytearray(b"x")` -- all truthy -- were read as empty containers and their
    # branch was treated as one that never runs.
    (
        "a set built from a literal is not an empty container",
        "    import os as cs",
        "    if flag:\n        cs = set([1])",
    ),
    (
        "a list built from a tuple is not an empty container",
        "    import os as cs",
        "    if flag:\n        cs = list((1,))",
    ),
    (
        "a dict built from a keyword is not an empty container",
        "    import os as cs",
        "    if flag:\n        cs = dict(a=1)",
    ),
    (
        "a bytearray built from bytes is not an empty container",
        "    import os as cs",
        "    if flag:\n        cs = bytearray(b'x')",
    ),
    # #388-5d. Collapsing a loop target into its body's rebind retired the
    # target, and the resolution then still sorted over *every* entry, so the
    # retired target won the source-order tie against the body's store and the
    # suppressor was dropped in favour of the element the target yielded.
    (
        "a loop body rebinding the target to a suppressor",
        "    import os as cs\n    cs = None",
        "    if flag:\n        for cs in (None,):\n            cs = suppress(AssertionError)",
    ),
)


@pytest.mark.parametrize(
    ("label", "carrier", "conditional"),
    STALE_LOCAL_CARRIER_SHAPES,
    ids=[shape[0] for shape in STALE_LOCAL_CARRIER_SHAPES],
)
def test_a_conditional_store_superseding_a_local_carrier_is_declined(label, carrier, conditional):
    """A carrier a conditional store may have replaced is not still in force.

    The rule must not report the entry dead on the strength of a carrier that
    a later conditional store may have replaced. Every row is executed under
    CPython with ``flag`` **true**, which is the path on which the assert is
    live, so the fixture proves the header really is enterable and the
    ``enforced`` verdict is the correct answer.
    """
    source = (
        "import contextlib\n"
        "from contextlib import nullcontext\n"
        "def outer(x, flag, helper):\n" + carrier + "\n" + conditional + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    # `_assert_entry_contract` calls `outer(1, True, None)`: `x=1` makes the
    # assert false, and `flag=True` makes the conditional store run, so the
    # name holds a real context manager and the assert is live.
    _assert_entry_contract(label, source, False, True)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"{label}: expected verdicts [True], got {results}. A conditional store "
        f"that may have superseded the carrier leaves the header's value "
        f"undecidable, so the rule must decline instead of reporting the "
        f"entry dead."
    )


@pytest.mark.parametrize(
    ("label", "carrier", "conditional"),
    DEAD_CONDITION_AFTER_CARRIER_SHAPES,
    ids=[shape[0] for shape in DEAD_CONDITION_AFTER_CARRIER_SHAPES],
)
def test_a_nonenterable_conditional_store_does_not_revive_a_stale_carrier(
    label, carrier, conditional
):
    """A conditional store that cannot be entered leaves the carrier decisive.

    The counterpart to
    :func:`test_a_conditional_store_superseding_a_local_carrier_is_declined`.
    There the later store could bind a real context manager, so the header's
    value is undecidable; here it is pinned to a module, a class, a function or
    a non-enterable literal, so *every* path through the header raises before
    the assert is evaluated. Declining would certify a dead contract as
    enforced, which is the damaging direction, so the rule must answer from the
    carrier as usual.

    The last six rows are the ones an independent review caught on the first
    cut of the repair. Deciding "this assignment destructures, so the value is
    unreadable" from the *statement* is wrong in both directions at once:

    * ``*cs, = (helper,)`` builds a **list** for ``cs`` whatever the elements
      are, so the name is pinned non-enterable and the assert is dead;
    * ``cs = (other, x) = (helper, 1)`` gives the bare ``cs`` the **whole**
      right-hand side -- a tuple -- while ``other`` and ``x`` get elements of
      it. Deciding the statement once calls ``cs`` unreadable and declines.

    Both were reported live on the first cut, against a master that answers
    them correctly. Whether a store pins *this* name is a question about that
    name's own target, which is what `_store_may_bind_enterable` is now asked.

    Both rows are executed under CPython here too -- ``_assert_entry_contract``
    is called with ``second_assert_live=False``, which requires the fixture to
    raise something *other* than ``AssertionError``. That is what distinguishes
    "unreachable" from "swallowed", and it is what makes these rows
    non-vacuous rather than an assertion about the checker.

    **A known limit this table deliberately does not cover.** A conditional
    store that is itself a *carrier* --

        def outer(x, flag, helper):
            import os as cs
            if flag:
                import os as cs
            with cs:
                assert x != 1

    -- is dead under CPython on both paths, and `origin/master` (``ed9d9b0``)
    reports it `True` just the same. That is a separate pre-existing gap in
    `_carrier_runtime_kinds` on the carrier path, unchanged by this repair and
    not visible to the guard here, which reads the *settled* store. It is left
    alone rather than folded in: widening this table to cover it would make
    the non-vacuity proof below depend on a second repair.
    """
    source = (
        "import contextlib\n"
        "from contextlib import nullcontext\n"
        # `other` is bound by the chained-target rows, and the starred rows
        # unpack `pair` and bind `first`/`rest`/`a`/`b`, so every name they
        # touch is a parameter here rather than an unbound global: CPython has
        # to be able to run the fixture for the entry contract to mean
        # anything. `_assert_entry_contract` calls `outer(1, True, None)`, so
        # the extra names take their defaults.
        "def outer(x, flag, helper, other=None, pair=(1, 2), first=None, rest=None, a=None, b=None, third=None, rest3=None):\n"
        + carrier
        + "\n"
        + conditional
        + "\n"
        + "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, False)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False], (
        f"{label}: expected verdicts [False], got {results}. A conditional store "
        f"that is itself pinned non-enterable cannot make the header enterable, "
        f"so the carrier still settles the name and the assert stays unreachable."
    )


#: The fifth review round's signature- and module-scope shadowing rows.
#:
#: These cannot live in either carrier table, because both build their fixture
#: with a **fixed** signature ``outer(x, flag, helper)``. A row that needs the
#: *callee's own name* to be a parameter cannot be expressed through a carrier
#: string at all, and a module-level binding cannot be either. Each row
#: therefore supplies its whole source, which is what lets both halves of
#: #388-5b be expressed:
#:
#: * a **parameter** binds for the whole call, so ``def outer(list, x)`` calls
#:   whatever the caller passed -- never the builtin ``list``. Positional,
#:   keyword-only, ``*args`` and ``**kwargs`` each bind in their own way, and
#:   ``ast`` records the last two under ``vararg``/``kwarg`` rather than in the
#:   flat ``args`` list, so a filter reading only ``args`` misses them.
#: * a **module-level** ``def list(): ...`` binds the name for every function
#:   in the file, and a module-scope ``cs = list()`` is a store that has already
#:   run by the time any header reads it.
#:
#: Each row is **executed** before its verdict is asserted, so no row can pin a
#: verdict the interpreter does not agree with.
SHADOWED_CALLEE_SOURCES = (
    (
        "a positional parameter shadowing a builtin constructor",
        ("def outer(list, x):\n    cs = list()\n    with cs:\n        assert x != 1\n"),
        (contextlib.nullcontext,),
        {"x": 1},
    ),
    (
        "a keyword-only parameter shadowing a builtin constructor",
        (
            "from contextlib import nullcontext\n"
            "def outer(x, *, list=nullcontext):\n"
            "    cs = list()\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1},
    ),
    (
        "a vararg parameter shadowing a builtin constructor",
        "def outer(*list, x):\n    cs = list[0]()\n    with cs:\n        assert x != 1\n",
        (contextlib.nullcontext,),
        {"x": 1},
    ),
    (
        "a kwarg parameter shadowing a builtin constructor",
        (
            "from contextlib import nullcontext\n"
            "def outer(x, **list):\n"
            "    cs = list['k']()\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1, "k": contextlib.nullcontext},
    ),
    (
        "a module-level def shadowing a builtin constructor",
        (
            "from contextlib import nullcontext\n"
            "def list():\n"
            "    return nullcontext()\n"
            "cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1},
    ),
    (
        "a module-level assignment shadowing a builtin constructor",
        (
            "from contextlib import nullcontext\n"
            "list = nullcontext\n"
            "cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1},
    ),
    # #388-5d. The positive half of the module-scope rule, and the case a
    # naive "only descend into plain statements" fix would break: a `def`
    # inside a module-level `if` is still a **module** binding, because the
    # block runs in the scope that encloses it. The branch has run by the time
    # `cs = list()` is evaluated, so the call really is the shadow and the
    # header really is LIVE. This is what stops the false-lives above from
    # being "fixed" by simply refusing to look inside any block.
    (
        "a module-level if branch defining the shadowing constructor",
        (
            "from contextlib import nullcontext\n"
            "if True:\n"
            "    def list():\n"
            "        return nullcontext()\n"
            "cs = list()\n"
            "def outer(x, flag, helper):\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1, "flag": True, "helper": None},
    ),
    # #388-5d. The same binding one level deeper: a block inside a block. A
    # walk that only descends one level reports the name unshadowed and retires
    # a header CPython enters, so the descent has to be recursive.
    (
        "a shadowing constructor defined in a nested module if",
        (
            "from contextlib import nullcontext\n"
            "if True:\n"
            "    if True:\n"
            "        def list():\n"
            "            return nullcontext()\n"
            "cs = list()\n"
            "def outer(x, flag, helper):\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1, "flag": True, "helper": None},
    ),
    # #388-5d. A `match` clause runs in the scope that encloses it, exactly as
    # an `if` body does. `Case` is not one of the block shapes the walk opens,
    # so the clause has to be flattened to its statements; reading past it made
    # this a false-dead on an earlier draft of the repair.
    (
        "a shadowing constructor defined in a module match case",
        (
            "from contextlib import nullcontext\n"
            "match 1:\n"
            "    case 1:\n"
            "        def list():\n"
            "            return nullcontext()\n"
            "cs = list()\n"
            "def outer(x, flag, helper):\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1, "flag": True, "helper": None},
    ),
)


def _shadowed_callee_reaches_assert(label, source, arguments, keywords):
    """Does CPython reach the assert in this row? Executed, never assumed."""
    namespace = {"nullcontext": contextlib.nullcontext}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](*arguments, **keywords)
    except AssertionError:
        return True
    except (TypeError, AttributeError, KeyError, IndexError, NameError, UnboundLocalError):
        return False
    raise AssertionError(
        f"{label}: the fixture returned normally, so neither the assert nor an "
        f"entry failure was observed and the row proves nothing."
    ) from None


#: The round-5 review's findings against `d921aac`, all **false-live**: the tool
#: reported a header LIVE where CPython raises before the assert. The direction
#: is the mild one -- a header reported as enterable when it is not -- but these
#: were *introduced* by the module-scope shadowing that #388-5b added, so they
#: are regressions against `86fab6d` and had to be repaired.
#:
#: Each row is executed first: `x=2` makes `assert x != 1` true, so a clean
#: return proves the with-body was entered (LIVE) and any exception proves it
#: was not (DEAD). The expected verdict is DEAD in all four.
MODULE_SCOPE_FALSE_LIVE_SOURCES = (
    # #388-5d(f1). `cs = list()` runs BEFORE the `def list()` below it, so the
    # call is still the real builtin and binds the empty list -- which cannot be
    # entered. Scanning the whole module regardless of order called the name
    # shadowed, and a `TypeError`-raising header came back LIVE.
    (
        "a module binding that runs after the call",
        "cs = list()\ndef list():\n    return None\ndef outer(x):\n    with cs:\n        assert x != 1\n",
    ),
    # #388-5d(f1b). The same, with the later binding inside a module-level `if`
    # rather than at the top level. The branch has not run when `cs = list()`
    # is evaluated, so it does not count either.
    (
        "a later module binding inside a module if",
        (
            "cs = list()\n"
            "if True:\n"
            "    def list():\n"
            "        return None\n"
            "def outer(x):\n"
            "    with cs:\n        assert x != 1\n"
        ),
    ),
    # #388-5d(f2). `list` is bound in `unrelated`'s LOCALS, when `unrelated` is
    # called. Nothing has called it, so the module never binds `list` and
    # `cs = list()` is still the builtin. Descending into a nested function body
    # read the `TypeError` a real call raises as a callable the header enters.
    (
        "a binding in another function's body",
        (
            "def unrelated():\n"
            "    def list(): return None\n"
            "cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n        assert x != 1\n"
        ),
    ),
    # #388-5d(f2b). A `class` body is a separate scope for the same reason: a
    # method named `list` does not bind the module name.
    (
        "a binding in a class body",
        (
            "class Holder:\n"
            "    def list(self): return None\n"
            "cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n        assert x != 1\n"
        ),
    ),
    # #388-5d(f3). `import builtins` is the ordinary import that brings the
    # real module in -- the same `import x` spelling that everywhere else in
    # this file means "this is the module named x". Treating it as a rebinding
    # of the name `builtins` made `cs = builtins.list()`, the canonical way of
    # naming a builtin, read as shadowed.
    (
        "a qualified builtin call through the builtins module",
        "import builtins\ncs = builtins.list()\ndef outer(x):\n    with cs:\n        assert x != 1\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"),
    MODULE_SCOPE_FALSE_LIVE_SOURCES,
    ids=[shape[0] for shape in MODULE_SCOPE_FALSE_LIVE_SOURCES],
)
def test_a_name_the_module_does_not_bind_at_call_time_is_reported_dead(label, source):
    """A name is shadowed only by a module binding that has actually run.

    #388-5d. #388-5b added a module-scope shadowing rule, and reading the whole
    module without regard to *when* the binding runs, or to *which scope* it is
    written in, produced false-lives on `d921aac`:

    * a binding written **after** the call, which has not run yet;
    * a binding inside **another function's** or a **class body**, which binds
      a local, not the module name;
    * `import builtins`, which is an ordinary import rather than a rebinding,
      so the qualified `builtins.list()` was read as shadowed.

    Every row is executed before its verdict is checked, so the DEAD
    expectation is CPython's own answer rather than an assumption. A row whose
    fixture turns out to reach the assert fails here rather than passing
    vacuously.
    """
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](2)
    except AssertionError:
        pytest.fail(
            f"{label}: CPython reached the assert, so this row cannot pin a DEAD "
            f"verdict. Either the fixture is wrong or the expectation is."
        )
    except (AttributeError, IndexError, KeyError, NameError, TypeError, UnboundLocalError):
        pass
    else:
        pytest.fail(f"{label}: the fixture returned normally, so it proves nothing.")

    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    assert [_is_enforced(function, asserts[0], tree)] == [False], (
        f"{label}: expected the header to be reported DEAD. A binding that has "
        f"not run, or that binds another scope's name, leaves the call reaching "
        f"the real builtin, and the empty list it returns cannot be entered."
    )


@pytest.mark.parametrize(
    ("label", "source", "arguments", "keywords"),
    SHADOWED_CALLEE_SOURCES,
    ids=[shape[0] for shape in SHADOWED_CALLEE_SOURCES],
)
def test_a_constructor_callee_shadowed_outside_the_body_is_reported_live(
    label, source, arguments, keywords
):
    """A callee shadowed by a parameter or a module binding is not a builtin.

    #388-5b. The shadowing check looked only inside the function body, so two
    bindings that are never *written* in the body were invisible:

    * the **signature** -- a parameter binds for the whole call, so
      ``def outer(list, x): cs = list()`` calls whatever the caller passed and
      never the builtin ``list``;
    * **module scope** -- a module-level ``def list(): ...`` binds the name for
      every function in the file, and a module-level ``cs = list()`` has
      already run by the time any header reads it.

    Both were read as the builtin, and both are a **false-dead** when the shadow
    happens to return a real context manager: the tool reported the header DEAD
    where CPython enters it and the assert is genuinely reachable. That is the
    damaging direction -- it certifies a live contract as swallowed -- so it is
    the direction these rows pin.

    Every row is executed first, so the LIVE verdict is checked against CPython's
    own answer rather than asserted from the fixture's shape. A row whose
    fixture does not actually reach the assert fails here rather than passing
    vacuously.
    """
    assert _shadowed_callee_reaches_assert(label, source, arguments, keywords) is True, (
        f"{label}: CPython does not reach the assert here, so the row cannot "
        f"pin a LIVE verdict. Either the fixture is wrong or the expectation is."
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    assert [_is_enforced(function, asserts[0], tree)] == [True], (
        f"{label}: expected the header to be reported LIVE. A callee shadowed "
        f"by a parameter or a module binding is a different callable, so the "
        f"store must be read as possibly-enterable and the assert kept."
    )


#: The round-6 review's findings against `5535135`. Three were **false-dead**
#: -- the tool reported a header DEAD where CPython enters it, which is the
#: damaging direction because it certifies a reachable assert as swallowed --
#: and one was a residual **false-live** inside a single module block.
#:
#: Every row is *executed* first, with `x=2` so that `assert x != 1` is TRUE.
#: A clean return therefore proves the with-body was ENTERED (LIVE) and any
#: exception proves it was not (DEAD). That is the opposite of an
#: `x=1`-plus-`except AssertionError` harness, which cannot tell a body that
#: was entered from one whose failure a suppressor ate.
#: The round-7 review's findings against `b90f985`. These are **regressions the
#: round-6 repairs introduced**, each caught by execution at `x=2` and each
#: pinned here with a positive control beside it.
ROUND_SEVEN_SOURCES = (
    # Review finding 1. `_statement_always_runs` decides that a store inside
    # `if True:` always runs, but it only looked at the *innermost* block. The
    # store here is also inside a `for _ in ():` that never iterates, so it
    # never runs at all: `cs` is still the `nullcontext()` bound above it, the
    # header is entered, and the assert is reachable. Treating the store as
    # unconditional settled `cs` on the `cs = list()` that never ran and
    # reported a `TypeError`-raising header DEAD. Every block on the path has
    # to run for the rule to fire.
    (
        "an always-true branch inside a loop that never runs",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "for _ in ():\n"
            "    if True:\n"
            "        cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The same defect by the `while False:` spelling of a never-run body.
    (
        "an always-true branch inside a while loop that never runs",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "while False:\n"
            "    if True:\n"
            "        cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The positive control for finding 1. The loop *does* iterate, so the
    # always-true store really does run and really does rebind `cs` to the
    # empty list, which `with` cannot enter. A fix that simply stopped
    # counting always-true stores would report this header LIVE.
    (
        "an always-true branch inside a loop that does run",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "for _ in (1,):\n"
            "    if True:\n"
            "        cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    # Review finding 3. `builtins` is rebound *after* the call, so it is not
    # yet in force where the call is evaluated: `cs` holds the empty list the
    # real builtin returned, and `with cs:` raises. The module rebinding check
    # scanned the whole module, so it counted a store that had not run and
    # read the rebound attribute, reporting the `TypeError` as LIVE.
    (
        "builtins rebound after the qualified call",
        (
            "from types import SimpleNamespace\n"
            "import builtins\n"
            "cs = builtins.list()\n"
            "builtins = SimpleNamespace(list=len)\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    # The positive control for the *other* direction: the same rebinding
    # before the call, but to something that does build a real context manager.
    # The store is shadowed for real here, so the assert is reachable.
    (
        "builtins rebound to a manager before the qualified call",
        (
            "from contextlib import nullcontext\n"
            "from types import SimpleNamespace\n"
            "import builtins\n"
            "builtins = SimpleNamespace(list=nullcontext)\n"
            "cs = builtins.list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
)


ROUND_SIX_SOURCES = (
    # Finding 1. The call is inside `outer`'s body, and a function body is only
    # reached *after the whole module has executed*. The `def list()` below has
    # therefore already run by the time `outer(2)` is called, so `cs` holds a
    # `nullcontext` and the assert is reachable. Cutting the module walk at
    # `outer`'s own `def` -- correct for a *module-scope* call -- read the
    # builtin `list` instead and reported the header DEAD.
    (
        "a module binding written after the function definition",
        (
            "from contextlib import nullcontext\n"
            "def outer(x):\n"
            "    cs = list()\n"
            "    with cs:\n"
            "        assert x != 1\n"
            "def list():\n"
            "    return nullcontext()\n"
        ),
        True,
    ),
    # Finding 1, second spelling. An assignment binds the module name exactly
    # as a `def` does, so the position argument is the same and the answer has
    # to be the same.
    (
        "a module assignment written after the function definition",
        (
            "from contextlib import nullcontext\n"
            "def outer(x):\n"
            "    cs = list()\n"
            "    with cs:\n"
            "        assert x != 1\n"
            "list = nullcontext\n"
        ),
        True,
    ),
    # Finding 2. An `except` handler body runs in the scope that encloses it,
    # and this one has plainly run by the time `cs = list()` is evaluated. The
    # module walk opened `try` bodies, `else` and `finally` but not the
    # handlers, so the binding was skipped and the builtin `list` was read.
    (
        "a module binding inside an except handler",
        (
            "from contextlib import nullcontext\n"
            "try:\n"
            "    raise ValueError\n"
            "except ValueError:\n"
            "    def list():\n"
            "        return nullcontext()\n"
            "cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # Finding 3. `builtins.attr` only names the real module while `builtins`
    # itself is unbound. A store that replaces the name makes the attribute
    # lookup reach an arbitrary object, and `builtins.list()` is then
    # `nullcontext()`. Answering `False` for every `builtins.*` callee read
    # the shadowed call as the builtin and reported the header DEAD.
    (
        "a rebound builtins name read through an attribute",
        (
            "from contextlib import nullcontext\n"
            "from types import SimpleNamespace\n"
            "builtins = SimpleNamespace(list=nullcontext)\n"
            "cs = builtins.list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # Finding 3, function-local spelling. The same attribute read, but the
    # store is a local rather than a module one, so it is caught by the
    # function-scope half of the question rather than the module half.
    (
        "a function-local builtins name read through an attribute",
        (
            "from contextlib import nullcontext\n"
            "def outer(x):\n"
            "    from types import SimpleNamespace\n"
            "    builtins = SimpleNamespace(list=nullcontext)\n"
            "    cs = builtins.list()\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # Finding 4, the residual false-live. `cs = list()` runs BEFORE the
    # `def list()` beside it, so the call really is the builtin and binds the
    # empty list, which `with` cannot enter. The order cut used to stop at the
    # enclosing `if` as a whole statement, so the `def` inside that same block
    # read as though it shadowed the call, and a `TypeError`-raising header
    # came back LIVE.
    (
        "a later binding inside the same module block as the call",
        (
            "from contextlib import nullcontext\n"
            "if True:\n"
            "    cs = list()\n"
            "    def list():\n"
            "        return nullcontext()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    # The positive control for finding 4, and the one that would break if the
    # always-true branch were ignored. The shadow is written *before* the call
    # inside the same block, so the call really is the shadow and the assert is
    # reachable.
    (
        "an earlier binding inside the same module block as the call",
        (
            "from contextlib import nullcontext\n"
            "if True:\n"
            "    def list():\n"
            "        return nullcontext()\n"
            "    cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The positive control for finding 3. `import builtins` is the ordinary
    # import that brings the *real* module in, so the qualified spelling still
    # reaches the builtin and the empty list cannot be entered. Reading the
    # canonical import as a rebinding would report this header LIVE.
    (
        "the canonical import of builtins read through an attribute",
        (
            "import builtins\n"
            "cs = builtins.list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
)


def _fixture_is_entered(label, source):
    """Does CPython enter the with-body? Executed, never assumed.

    The fixture is called with ``x=2``, which makes ``assert x != 1`` true. A
    clean return therefore means the body ran (LIVE); any exception means it
    did not (DEAD). The assert is therefore never *reached as a failure*: the
    question is only whether entry raised before it, so the harness counts a
    clean return as LIVE rather than catching ``AssertionError`` as success.
    """
    namespace = {"nullcontext": contextlib.nullcontext, "SimpleNamespace": SimpleNamespace}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](2)
    except Exception:  # noqa: BLE001 - any exception means the body was not entered
        return False
    return True


@pytest.mark.parametrize(
    ("label", "source", "expected_live"),
    (*ROUND_SEVEN_SOURCES, *ROUND_SIX_SOURCES),
    ids=[shape[0] for shape in (*ROUND_SEVEN_SOURCES, *ROUND_SIX_SOURCES)],
)
def test_module_scope_order_scope_and_builtins_reading(label, source, expected_live):
    """A function-body call, a handler body and a rebound ``builtins`` are read right.

    #388-5e. The round-6 review found three false-deads and one false-live in
    the module-scope shadowing rule, each from a different place where the walk
    stopped short of the truth:

    * a function body runs only *after* the whole module has executed, so a
      module binding written **after** the function's ``def`` still shadows the
      callee there -- the order cut is only valid for a module-scope call;
    * an ``except`` **handler** body runs in the enclosing scope, so a binding
      in one is a module binding, and the walk has to open the handlers;
    * ``builtins.attr`` only names the real module while ``builtins`` itself is
      unbound, so the qualified spelling needs the base name checked -- while
      ``import builtins`` must keep counting as the real module;
    * and a binding *after* the call **inside the same block** does not shadow
      it, which needs the descent to be order-aware rather than to stop at the
      enclosing statement.

    #388-5f. The round-7 review then found two *regressions* those repairs
    # introduced, and both are the same mistake in opposite directions: a rule
    # that keys on one enclosing block and ignores the rest of the path.

    * ``_statement_always_runs`` read the innermost ``if True:`` and ignored a
      ``for _ in ():`` around it, so a store that never runs settled the name;
    * the ``builtins`` rebinding check scanned the whole module, so a store
      written *after* a module-scope call counted even though it had not run
      yet. Both are checked here, each beside a positive control that fails if
      the rule is dropped rather than narrowed.

    Each row is executed under real CPython with ``x=2`` before its verdict is
    checked, so the expectation is CPython's own answer. A fixture that does
    not behave as the row claims fails here rather than passing vacuously.
    """
    assert _fixture_is_entered(label, source) is expected_live, (
        f"{label}: CPython "
        f"{'entered' if expected_live else 'did not enter'} the with-body, so this "
        f"row cannot pin the opposite verdict. Either the fixture is wrong or "
        f"the expectation is."
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    assert [_is_enforced(function, asserts[0], tree)] == [expected_live], (
        f"{label}: expected the header to be reported "
        f"{'LIVE' if expected_live else 'DEAD'}. A callee shadowed by a module "
        f"binding that has already run is a different callable; one that has "
        f"not run yet still reaches the real builtin."
    )
