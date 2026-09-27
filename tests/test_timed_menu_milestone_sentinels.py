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

import pytest

from tests._timed_menu_milestone_sentinel_support import (
    DEADLINE_TERMINATION,
    GUARD_FUNCTION,
    RETENTION_COUNT_SITES,
    RETENTION_SUBSCRIPT_COUNT_SITES,
    RUN_OWNER,
    _is_enforced,
    _is_tautology,
    _may_bypass,
    count_sites_that_bypass_the_guard,
    guard_is_wired_on_the_fast_clock_path,
    guard_rejects_the_deadline_terminal_state,
    retention_sites_observed,
    retention_subscript_sites_observed,
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
    # A bare call is already a deciding operand, so the `or` rule catches this
    # one without the new `Compare` clause. The row is kept as a control.
    ("lone call operand", "assert x != 1 or len(y) > 0", False),
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
    #
    # The four `or` rows below were `True` on master and are now `False`. In
    # `A or B` the assert holds whenever `B` is true, whatever `A` says, so a
    # right-hand comparison decides the assert exactly as `or True` does.
    # Whether `B` is *provably* true cannot be decided without the record, and
    # the two errors are not symmetric: a false report is a red test somebody
    # investigates, while a false negative is a silently unenforceable
    # contract. `x > 5` decides the assert for a real record as surely as `or
    # True` does. The `and` row below is unaffected and stays `True`.
    ("real count comparison", "assert x != 1 or len(y) == 300", False),
    ("real count and", "assert len(y) == 300 and len(z) == 271", True),
    ("real upper bound", "assert x != 1 or len(y) <= 100", False),
    ("empty-only bound", "assert x != 1 or len(y) < 1", False),
    ("non-numeric bound", 'assert x != 1 or len(y) > "a"', False),
    ("subscript left operand", 'assert x != 1 or record["n"] >= 0', False),
    ("bool is not a number", "assert x != 1 or len(y) >= True", False),
    ("arithmetic left operand", "assert x != 1 or a - b >= 0", False),
    # Reads a Name, so it is not constant-foldable and stays enforced. This is
    # the safe direction: a wrong answer reports a live assert as dead.
    ("self compare", "assert x != 1 or (x == x)", False),
    ("runtime value compare", "assert x != 1 or (a == b)", False),
    ("call compare", "assert x != 1 or f(a) == f(a)", False),
    ("subscript compare", 'assert x != 1 or record["k"] == record["k"]', False),
    # The exact shape #294 review reported, spelled out. `errors` is empty on
    # every successful run, so the right operand is true for the record the
    # contract is about and the left comparison is never evaluated.
    (
        "deadline guard or count bound",
        (
            'assert record["termination"] != "cancelled_or_deadline" or '
            'len(record.get("errors", [])) < 99'
        ),
        False,
    ),
    # A nested `BoolOp` neutralises a comparison wherever it sits, so these are
    # reported even where a sibling comparison in the same assert is live.
    # `x != 1` is never evaluated, which is what the rule exists to catch; the
    # false report is the safe direction.
    ("nested bypass left of and", "assert (x != 1 or True) and y != 2", False),
    ("nested bypass left of and count", "assert (x != 1 or True) and y == 300", False),
    ("nested tautology left of and", "assert (len(y) >= 0 or True) and z != 1", False),
    ("nested bypass right of and", "assert y != 2 and (x != 1 or True)", False),
    ("nested compare right of and", "assert y != 2 and (x != 1 or len(y) == 300)", False),
    # These two are why the `or` rule scans *every* operand and not only the
    # right-hand ones. A decider on the left of an `or` skips the whole right
    # side, including an `and` nested there, and the nested `and` is itself not
    # a bypass. Scanning the right operand alone would report each of these
    # enforced, which is the same false negative one level down.
    ("decider left of or over and", "assert True or (x != 1 and y != 2)", False),
    (
        "tautology left of or over and",
        "assert len(y) >= 0 or (x != 1 and z != 2)",
        False,
    ),
    # Same shape with a *runtime* decider on the left, which the `_is_tautology`
    # clause cannot see. These three are the rows that fail if the `or` rule is
    # narrowed to the right-hand operands.
    ("name left of or over and", "assert flag or (x != 1 and y != 2)", False),
    ("subscript left of or over and", "assert record['k'] or (x != 1 and y != 2)", False),
    ("call left of or over and", "assert f(a) or (x != 1 and y != 2)", False),
    # A tautology on the LEFT of an `or` is caught by the `_is_tautology`
    # clause rather than the operator rule, because the `or` rule scans the
    # right-hand operands. These rows keep that clause pinned.
    ("true left of or", "assert True or x != 1", False),
    ("literal compare left of or", "assert (1 == 1) or x != 1", False),
    ("list left of or", "assert [1, 2] or x != 1", False),
    ("tautology count left of or", "assert len(y) >= 0 or x != 1", False),
    # The one shape that separates the tautology clause from the `or` rule: a
    # tautology on the left of an `and` makes the right comparison unreachable.
    ("true left of and", "assert True and x != 1", False),
    # The `and` rows carrying a bare decider on the right are the ones that
    # would fail if the rule regressed to scanning every `and` operand. Each is
    # live -- the left operand is evaluated first -- and each is spelled from
    # test_timed_menu_milestones.py rather than invented.
    (
        "real errors and isinstance",
        "assert len(errors) == 1 and isinstance(errors[0], RuntimeError)",
        True,
    ),
    (
        "queued and all",
        "assert queued and all(item['in_flight']['input']['status'] == 'queued' for item in queued)",
        True,
    ),
    (
        "termination and errors",
        "assert record['termination'] == 'owner_failure' and record['errors']",
        True,
    ),
    ("count and call index", "assert len(calls) == 271 and calls[-1]['call_index'] == 270", True),
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
    source = "def probe(x, flag, y, z, a, b, record, errors, calls, queued, item):\n" + "\n".join(
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
