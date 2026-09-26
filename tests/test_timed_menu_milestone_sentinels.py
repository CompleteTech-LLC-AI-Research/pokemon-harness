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
    RUN_OWNER,
    _is_enforced,
    _may_bypass,
    count_sites_that_bypass_the_guard,
    guard_is_wired_on_the_fast_clock_path,
    guard_rejects_the_deadline_terminal_state,
    retention_sites_observed,
)

#: ``(label, body, live)`` -- does an assert in this body actually propagate?
#: These are the shapes #280 is about. The sentinel tests below prove the
#: sentinels *use* this logic; this table proves the logic itself is right, so a
#: future refactor cannot quietly widen or narrow what counts as enforced.
ENFORCEMENT_SHAPES = (
    ("plain", "assert x == 1", True),
    ("and of two comparisons", "assert len(a) == 1 and len(b) == 2", True),
    ("double negative", "assert not (x != 1)", True),
    ("try-except AssertionError", "try:\n assert x == 1\nexcept AssertionError:\n pass", False),
    ("bare except", "try:\n assert x == 1\nexcept:\n pass", False),
    ("except BaseException", "try:\n assert x == 1\nexcept BaseException:\n pass", False),
    ("except Exception", "try:\n assert x == 1\nexcept Exception:\n pass", False),
    ("tuple handler", "try:\n assert x == 1\nexcept (KeyError, AssertionError):\n pass", False),
    ("try-except-star", "try:\n assert x == 1\nexcept* AssertionError:\n pass", False),
    (
        "nested swallow",
        (
            "try:\n pass\nexcept AssertionError:\n try:\n  assert x == 1\n"
            " except AssertionError:\n  pass"
        ),
        False,
    ),
    (
        "contextlib suppress AssertionError",
        "with contextlib.suppress(AssertionError):\n assert x == 1",
        False,
    ),
    (
        "contextlib suppress Exception",
        "with contextlib.suppress(Exception):\n assert x == 1",
        False,
    ),
    ("static if False", "if False:\n assert x == 1", False),
    ("comparison or True", "assert x != 1 or True", False),
    ("True or comparison", "assert True or x != 1", False),
    ("comparison and flag", "assert x != 1 and flag", False),
    (
        "local AssertionError subclass",
        "class T(AssertionError):\n pass\ntry:\n assert x == 1\nexcept T:\n pass",
        False,
    ),
    # Shapes that must NOT be reported as defused, or the check would cry wolf
    # and a real regression would be waved through as a known shape.
    ("unrelated handler", "try:\n assert x == 1\nexcept ValueError:\n pass", True),
    (
        "else branch",
        "try:\n assert x == 1\nexcept ValueError:\n pass\nelse:\n assert x == 2",
        True,
    ),
    ("finally branch", "try:\n pass\nexcept ValueError:\n pass\nfinally:\n assert x == 1", True),
    ("handler body", "try:\n pass\nexcept AssertionError:\n assert x == 1", True),
    (
        "later handler only",
        "try:\n pass\nexcept AssertionError:\n assert x == 1\nexcept ValueError:\n pass",
        True,
    ),
    ("if True", "if True:\n assert x == 1", True),
    ("runtime condition", "if flag:\n assert x == 1", True),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    ENFORCEMENT_SHAPES,
    ids=[shape[0] for shape in ENFORCEMENT_SHAPES],
)
def test_the_enforcement_check_separates_live_asserts_from_inert_ones(label, body, live):
    """An assert counts as enforced only when its failure actually propagates.

    #280 exists because a structural sentinel is only as trustworthy as the
    shape it inspects. A handler that swallows the failure, a suppression
    context, a statically dead branch, or a short-circuiting ``or`` all leave the
    assert present in the tree while removing its ability to fail -- which is
    exactly what a presence-only check cannot see.
    """
    source = "def probe(x, flag):\n" + "\n".join(f"    {line}" for line in body.splitlines())
    function = ast.parse(source).body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    enforced = [_is_enforced(function, node) and not _may_bypass(node.test) for node in asserts]
    assert all(enforced) is live, (
        f"{label}: expected every assert to be {'enforced' if live else 'defused'}, got {enforced}"
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
