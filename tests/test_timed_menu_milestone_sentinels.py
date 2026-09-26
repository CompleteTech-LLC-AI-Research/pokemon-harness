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

from tests._timed_menu_milestone_sentinel_support import (
    DEADLINE_TERMINATION,
    GUARD_FUNCTION,
    RETENTION_COUNT_SITES,
    RUN_OWNER,
    _reachable_asserts,
    _swallows_assertion_error,
    count_sites_that_bypass_the_guard,
    guard_is_wired_on_the_fast_clock_path,
    guard_rejects_the_deadline_terminal_state,
    retention_sites_observed,
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


def test_the_swallow_classifier_recognises_exactly_the_handlers_it_claims():
    """Control: the classifier must not be a check that cannot fail.

    ``except ValueError`` must stay out, otherwise the reachability walk would
    discard legitimate asserts. ``except Exception`` must stay in, because
    ``AssertionError`` derives from it.
    """
    import ast as _ast

    def handler(exception):
        if exception is None:
            return _ast.ExceptHandler(type=None, name=None, body=[])
        return _ast.ExceptHandler(type=_ast.parse(exception, mode="eval").body, name=None, body=[])

    for expression in (None, "AssertionError", "BaseException", "Exception"):
        assert _swallows_assertion_error(handler(expression)) is True, (
            f"{expression!r} was not recognised as swallowing an AssertionError"
        )

    for expression in ("ValueError", "(ValueError, TypeError)", "KeyboardInterrupt"):
        assert _swallows_assertion_error(handler(expression)) is False, (
            f"{expression!r} was wrongly treated as swallowing an AssertionError"
        )


def test_asserts_that_cannot_execute_are_not_counted_as_teeth():
    """Control: the reachability walk must reject each inert shape.

    Every case below keeps the rejecting ``!=`` comparison present in the
    source, so a presence-based check accepts it, while the guard would return
    normally on the state it exists to reject.
    """
    import ast as _ast

    top = '    assert record["termination"] != "cancelled_or_deadline"\n'
    nested = '        assert record["termination"] != "cancelled_or_deadline"\n'

    def build(body: str):
        return _ast.parse("def assert_not_deadline_truncated(record):\n" + body).body[0]

    assert len(list(_reachable_asserts(build(top)))) == 1
    # An assert under a handler that cannot swallow is still able to fail.
    assert (
        len(
            list(
                _reachable_asserts(
                    build("    try:\n" + nested + "    except ValueError:\n        pass\n")
                )
            )
        )
        == 1
    )
    # `finally` does not consume the exception, so an assert there can still fail.
    assert (
        len(list(_reachable_asserts(build("    try:\n        pass\n    finally:\n" + nested)))) == 1
    )

    for label, body in (
        (
            "swallowed by except AssertionError",
            "    try:\n" + nested + "    except AssertionError:\n        pass\n",
        ),
        ("swallowed by a bare except", "    try:\n" + nested + "    except:\n        pass\n"),
        (
            "swallowed by except Exception",
            "    try:\n" + nested + "    except Exception:\n        pass\n",
        ),
        ("parked in a never-called nested function", "    def _inner():\n" + nested),
        ("inside a decidably dead if branch", "    if False:\n" + nested),
        ("inside a decidably dead while branch", "    while False:\n" + nested),
    ):
        assert not list(_reachable_asserts(build(body))), (
            f"an assert {label} was counted as able to reject the deadline state"
        )
