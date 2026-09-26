"""Sentinels that keep the #261 guard load-bearing, not merely present (#270).

``test_timed_menu_milestone_sentinels.py`` proves the #261 guard is still
*called* on the ``clock_step == 0.0`` path, and that the four retention counts
are still exact equalities. Neither can tell whether the guard still *does*
anything. Measured on ``master`` at ``47f066b``:

* **The guard loses its teeth -- completely invisible.** Replacing
  ``assert_not_deadline_truncated``'s body with ``pass`` leaves the sentinels,
  the milestones, and the frame-bound files at **86 passed, 0 failed**. The
  scenarios meant to reach the frame bound do reach it, so a guard that
  rejects nothing still satisfies every behavioural assertion. Only a source
  read tells the two apart.
* **A count site opts out of the guard.** ``run_owner`` applies the
  terminal-state check only on the default ``clock_step == 0.0`` path. Adding
  ``clock_step=1.0`` to a pinned site keeps its ``== 300`` assertion in place,
  so the exact-count sentinel still passes. This one *is* caught behaviourally
  today, by ``test_default_retention_keeps_full_calls_and_installs_no_hooks``,
  but only incidentally: the pin makes the precondition explicit instead of
  depending on that scenario continuing to observe a truncated run.

Both are deletions of meaning rather than of code, which is why these checks
parse the source, for the same reason #271 pinned its clock read that way. The
behavioural assertions remain the contract; this is the backstop.
"""

from tests._timed_menu_milestone_sentinel_support import (
    RETENTION_COUNT_SITES,
    guard_rejects_the_deadline_terminal_state,
    overridden_clock_steps_in_protected_sites,
)


def test_the_261_guard_still_rejects_the_deadline_terminal_state():
    """The guard must keep asserting a non-deadline terminal state.

    Mutation that must fail this: ``pass`` for the guard body, or flipping the
    guard's ``!=`` to ``==``. Both keep the call wired, so
    ``guard_is_wired_on_the_fast_clock_path`` still returns ``True``.
    """
    assert guard_rejects_the_deadline_terminal_state(), (
        "assert_not_deadline_truncated no longer rejects "
        "termination == 'cancelled_or_deadline'; a deadline-truncated run would "
        "again read as a retention result instead of a #261 guard failure"
    )


def test_the_pinned_count_sites_all_inherit_the_261_guard():
    """Every pinned retention count must still run on the guarded clock path.

    Mutation that must fail this: adding ``clock_step=1.0`` to any of the four
    pinned ``run_owner(...)`` calls. The exact count survives, so
    ``retention_sites_observed`` still matches, but the precondition that makes
    the count meaningful is gone.
    """
    assert not overridden_clock_steps_in_protected_sites(), (
        "pinned retention counts bypass the #261 guard via an explicit "
        f"clock_step: {overridden_clock_steps_in_protected_sites()}. Those sites "
        "no longer prove the run reached the frame bound rather than the deadline."
    )


def test_every_pinned_count_site_is_still_present():
    """The helper above must not pass vacuously if a pinned site is deleted.

    If ``RETENTION_COUNT_SITES`` ever names a function that no longer exists,
    the clock-step check would have nothing to inspect and would report no
    offenders. The existing exact-count sentinel would also fail, but this
    keeps the failure attributable rather than incidental.
    """
    missing = sorted(name for name in RETENTION_COUNT_SITES if not _function_exists(name))
    assert not missing, (
        f"the pinned retention sites {missing} no longer exist, so the clock-step "
        "sentinel above has nothing to inspect and would pass vacuously"
    )


def _function_exists(name):
    """Is the pinned test function ``name`` still defined in the milestones module?"""
    from tests._timed_menu_milestone_sentinel_support import _find_function, _module_tree

    return _find_function(_module_tree(), name) is not None
