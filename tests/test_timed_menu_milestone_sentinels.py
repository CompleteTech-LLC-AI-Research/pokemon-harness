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
