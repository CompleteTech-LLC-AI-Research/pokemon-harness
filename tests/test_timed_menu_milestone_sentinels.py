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
    _bound_names,
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


#: ``(label, imports, body, live)`` -- can the assert in ``body`` actually fail?
#: ``imports`` is prepended so each row can spell ``suppress`` the way a real edit
#: might. ``live`` is what ``_is_enforced`` must report for every assert in the body.
#:
#: These rows exist because the suppression rule is only as good as its coverage of
#: spellings. #287 was defeated by the *from-import* form while the qualified form
#: was already handled, which is the same one-spelling gap #288 had with ``except*``.
SUPPRESSION_SHAPES = (
    (
        "qualified contextlib.suppress",
        "import contextlib",
        "with contextlib.suppress(AssertionError):\n assert x == 1",
        False,
    ),
    (
        "from-import suppress",
        "from contextlib import suppress",
        "with suppress(AssertionError):\n assert x == 1",
        False,
    ),
    (
        "aliased module",
        "import contextlib as c",
        "with c.suppress(AssertionError):\n assert x == 1",
        False,
    ),
    (
        "aliased name",
        "from contextlib import suppress as sq",
        "with sq(AssertionError):\n assert x == 1",
        False,
    ),
    (
        "suppress Exception",
        "from contextlib import suppress",
        "with suppress(Exception):\n assert x == 1",
        False,
    ),
    (
        "suppress BaseException",
        "from contextlib import suppress",
        "with suppress(BaseException):\n assert x == 1",
        False,
    ),
    (
        "suppress tuple naming AssertionError",
        "from contextlib import suppress",
        "with suppress((KeyError, AssertionError)):\n assert x == 1",
        False,
    ),
    ("static if False", "", "if False:\n assert x == 1", False),
    # Must stay live, or the check would cry wolf and a real regression would be
    # waved through as a known shape.
    (
        "CONTROL suppress ValueError",
        "from contextlib import suppress",
        "with suppress(ValueError):\n assert x == 1",
        True,
    ),
    (
        "CONTROL aliased suppress KeyError",
        "from contextlib import suppress as sq",
        "with sq(KeyError):\n assert x == 1",
        True,
    ),
    (
        "CONTROL unrelated context manager",
        "from contextlib import nullcontext",
        "with nullcontext():\n assert x == 1",
        True,
    ),
    ("CONTROL if True", "", "if True:\n assert x == 1", True),
    ("CONTROL runtime condition", "", "if flag:\n assert x == 1", True),
    ("CONTROL plain assert", "", "assert x == 1", True),
    (
        "CONTROL handler for ValueError",
        "",
        "try:\n assert x == 1\nexcept ValueError:\n pass",
        True,
    ),
    (
        "CONTROL except* for ValueError",
        "",
        "try:\n assert x == 1\nexcept* ValueError:\n pass",
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "imports", "body", "live"),
    SUPPRESSION_SHAPES,
    ids=[shape[0] for shape in SUPPRESSION_SHAPES],
)
def test_the_enforcement_check_separates_live_asserts_from_suppressed_ones(
    label, imports, body, live
):
    """A suppression context or a dead branch must read as unenforced.

    #287 measured ``with contextlib.suppress(AssertionError):`` leaving every
    sentinel green while the owning test could not fail, so a wrong retention count
    reported as a pass. The assert does run and does raise there -- the raise is
    what gets discarded -- which is why no behavioural test can stand in for this
    one. The retention counts have no behavioural counterpart; this is the check.

    The false rows matter as much as the true ones: a rule that flagged
    ``suppress(ValueError)`` would report real pinned sites as unenforced and train
    the reader to ignore it.
    """
    header = "".join(f"{line}\n" for line in ([imports] if imports else []))
    source = f"{header}def probe():\n" + "\n".join(f"    {line}" for line in body.splitlines())
    module = ast.parse(source)
    function = module.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    enforced = [_is_enforced(function, node, tree=module) for node in asserts]
    assert all(enforced) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'suppressed'}, got {enforced}"
    )


def test_the_suppression_rule_resolves_names_rather_than_matching_text():
    """Import bindings must be read, so aliases and from-imports resolve alike.

    A rule that compared the literal text ``contextlib.suppress`` would pass every
    mutation in the table above except the first, and would still be wrong. This
    asserts the resolution step directly, so the *reason* the aliases are covered
    is pinned rather than just its current outcome.
    """
    module = ast.parse(
        "import contextlib\n"
        "import contextlib as c\n"
        "from contextlib import suppress\n"
        "from contextlib import suppress as sq\n"
    )
    bound = _bound_names(module)
    assert bound["contextlib"] == "contextlib"
    assert bound["c"] == "contextlib"
    assert bound["suppress"] == "contextlib.suppress"
    assert bound["sq"] == "contextlib.suppress"
    assert bound["sq"] == bound["suppress"], (
        "an alias must resolve to the same dotted path as the plain name, or the "
        "suppression rule silently misses aliased imports"
    )
