"""Behavioural contract for the #261 deadline-truncation guard.

#272 pinned that the guard is *called*, and #278 that its body still contains a
``!=`` comparison that rejects the terminal state. Both are syntactic, and
``ast`` predicates keep needing a new predicate for each new way to neutralise
the guard while leaving the node in place.

This module closes the dead-code arm behaviourally instead. It *calls*
``assert_not_deadline_truncated`` with a deadline-truncated record and requires
an ``AssertionError``. No reachability analysis is involved: a guard that never
executes its assert cannot raise, so every unreachability shape -- ``if False``,
``if 0``, an early ``return``, a permanently-false ``while`` -- fails this test
the same way, without a new predicate per shape.

It deliberately lives in its own module rather than alongside
``test_timed_menu_frame_bound.py``. The guard is imported from a support module,
so if this test were deleted together with the file that uses the guard, the
contract would disappear at exactly the moment it stopped being satisfied.
"""

from __future__ import annotations

import pytest

from tests._timed_menu_frame_bound_support import assert_not_deadline_truncated

DEADLINE_TERMINATION = "cancelled_or_deadline"


def test_guard_rejects_a_deadline_truncated_record() -> None:
    """The guard must raise on exactly the record it exists to reject.

    This is the whole contract of #261: a run that hit the wall clock leaves
    ``termination`` at its default, so without this guard a short call list is
    indistinguishable from a dropped record and a truncated run gets reported
    as a retention failure. The failure mode is a *wrong diagnosis*, not a lost
    record, which is why the guard has to be enforced rather than merely
    present.
    """

    with pytest.raises(AssertionError):
        assert_not_deadline_truncated({"termination": DEADLINE_TERMINATION})


def test_guard_admits_a_run_that_reached_its_terminal_state() -> None:
    """The guard must not reject a run that genuinely completed.

    Without this half, a guard that raised unconditionally -- ``raise
    AssertionError`` on every input, or an ``assert False`` -- would satisfy the
    rejection test above while making every legitimate retention run fail. The
    two halves together are the contract; either one alone is a defect.
    """

    assert_not_deadline_truncated({"termination": "completed"})


def test_guard_admits_every_non_deadline_terminal_state() -> None:
    """Only the deadline-truncated state is rejected, not the others.

    Pins that the guard discriminates on the value rather than rejecting the
    whole ``termination`` field, which a ``try``/``except``-free rewrite such
    as ``assert record`` would also satisfy.
    """

    for termination in ("completed", "cancelled_by_user", "error", "interrupted"):
        assert_not_deadline_truncated({"termination": termination})
