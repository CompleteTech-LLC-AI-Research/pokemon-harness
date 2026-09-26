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
import pathlib

import pytest

from tests._timed_menu_milestone_sentinel_support import (
    DEADLINE_TERMINATION,
    GUARD_FUNCTION,
    RETENTION_COUNT_SITES,
    RETENTION_SUBSCRIPT_COUNT_SITES,
    RUN_OWNER,
    _is_enforced,
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
#: #288 covers the ``try``/handler half and #280 the presence half. These rows
#: pin the reachability half (#287): a pinned assert that is present, correctly
#: bounded, and never evaluated. Each ``live=False`` row leaves the owning
#: behavioural test *green*, so nothing else in the repository notices.
REACHABILITY_SHAPES = (
    # --- defeats: the assert cannot fail ---
    ("if False", "if False:\n assert x == 1", False),
    ("if 0", "if 0:\n assert x == 1", False),
    ("if None", "if None:\n assert x == 1", False),
    ("if empty string", "if '':\n assert x == 1", False),
    # An empty *collection* literal is also falsy, but recognising it would mean
    # evaluating an expression rather than reading a literal. Left deliberately
    # unclaimed: a wrong "defeated" verdict drops a live contract, while a
    # missed one leaves the shape weak rather than unprotected.
    ("if empty list", "if []:\n assert x == 1", True),
    ("while False", "while False:\n assert x == 1", False),
    ("uncalled nested def", "def helper():\n assert x == 1", False),
    (
        "suppress AssertionError",
        "import contextlib\nwith contextlib.suppress(AssertionError):\n assert x == 1",
        False,
    ),
    (
        "from-import suppress",
        "from contextlib import suppress\nwith suppress(AssertionError):\n assert x == 1",
        False,
    ),
    ("suppress bare", "import contextlib\nwith contextlib.suppress():\n assert x == 1", False),
    (
        "suppress tuple",
        "import contextlib\nwith contextlib.suppress(ValueError, AssertionError):\n assert x == 1",
        False,
    ),
    (
        "async with suppress",
        "import contextlib\nasync with contextlib.suppress(AssertionError):\n assert x == 1",
        False,
    ),
    # --- must stay live, or the rule cries wolf on a real regression ---
    ("plain assert", "assert x == 1", True),
    ("double negative", "assert not (x != 1)", True),
    ("if True", "if True:\n assert x == 1", True),
    ("if False else branch", "if False:\n pass\nelse:\n assert x == 1", True),
    (
        "suppress ValueError",
        "import contextlib\nwith contextlib.suppress(ValueError):\n assert x == 1",
        True,
    ),
    (
        "suppress unrelated name",
        "import contextlib\nwith contextlib.suppress(TimeoutError):\n assert x == 1",
        True,
    ),
    ("callback nested def", "def cb():\n assert x == 1\nsession = cb", True),
    ("undecidable BoolOp test", "if 1 and False:\n assert x == 1", True),
    ("runtime condition", "if flag:\n assert x == 1", True),
    ("try ValueError", "try:\n assert x == 1\nexcept ValueError:\n pass", True),
    ("finally branch", "try:\n pass\nfinally:\n assert x == 1", True),
    ("handler body", "try:\n pass\nexcept ValueError:\n assert x == 1", True),
    ("orelse branch", "try:\n pass\nexcept ValueError:\n pass\nelse:\n assert x == 1", True),
    ("sibling swallowing try", "try:\n pass\nexcept AssertionError:\n pass\nassert x == 1", True),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    REACHABILITY_SHAPES,
    ids=[shape[0] for shape in REACHABILITY_SHAPES],
)
def test_the_enforcement_check_separates_reachable_asserts_from_defeated_ones(label, body, live):
    """A pinned assert counts only if it can actually run and fail.

    The defeat rows are the #287 shapes. ``if False:`` and the uncalled nested
    ``def`` are the dangerous pair: they keep the owning behavioural test green
    as well, so nothing in the repository notices. ``contextlib.suppress`` is
    different in kind -- the assert does run and does raise, and only the raise
    is discarded -- which is why it needs this static check rather than a
    behavioural one.

    The must-stay-live rows carry as much weight as the defeats: a wrong
    "defeated" verdict removes a live contract from the sentinel's view, which
    is the more damaging error.
    """
    source = "async def probe(x, flag, record, session):\n" + "\n".join(
        f"    {line}" for line in body.splitlines()
    )
    function = ast.parse(source).body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be {'enforced' if live else 'defeated'}, got {results}"
    )


def test_the_reachability_rule_drops_none_of_the_real_asserts():
    """No real assert in the milestones module may be reported as defeated.

    The false-positive direction is the damaging one: a live contract assert
    reported as defeated stops being pinned, silently. This walks all of them
    rather than trusting the table above to have covered the real shapes -- the
    callback pattern in particular (``def checkpoint`` handed to the session) is
    not in the table and is why the nested-def rule is a reference check rather
    than a blanket one.
    """
    from tests import test_timed_menu_milestones as milestones

    source = pathlib.Path(milestones.__file__).read_text()
    tree = ast.parse(source)
    defeated = [
        (function.name, ast.unparse(node.test)[:60])
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(function)
        if isinstance(node, ast.Assert) and not _is_enforced(function, node)
    ]
    assert not defeated, f"the reachability rule reports real, live asserts as defeated: {defeated}"
