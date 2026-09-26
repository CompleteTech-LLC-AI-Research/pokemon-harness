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

import tests._timed_menu_milestone_sentinel_support as support
from tests._timed_menu_milestone_sentinel_support import (
    DEADLINE_TERMINATION,
    GUARD_FUNCTION,
    RETENTION_COUNT_SITES,
    RETENTION_SUBSCRIPT_COUNT_SITES,
    RUN_OWNER,
    count_sites_that_bypass_the_guard,
    guard_is_wired_on_the_fast_clock_path,
    guard_rejects_the_deadline_terminal_state,
    retention_sites_observed,
    retention_subscript_sites_observed,
)


#: ``(label, guard body, may it still be able to fail?)``
#:
#: Each row is a distinct way the #261 guard can be present in the source,
#: keep its ``!=`` comparison, and yet be unable to fail. The predicate under
#: test is ``guard_rejects_the_deadline_terminal_state``, applied to a guard
#: module built from the row's body.
#:
#: The ``False`` rows are the defeats; they are what each of the three
#: mechanisms exists to catch. The ``True`` rows are the controls, and they
#: matter as much: a rule that reported those as defused would cry wolf on a
#: real regression, and the over-broad ``or True`` / ``if False`` readings are
#: exactly where that has already happened on this code. Each control was
#: confirmed to still reject the deadline record at runtime when the table
#: passed.
def _shape(label, body, expected):
    """One row of :data:`ENFORCEMENT_SHAPES`."""
    return (label, body, expected)


#: Bodies are written at zero indentation relative to the guard's own body;
#: :func:`_guard_tree_for` indents them.
_TERMINATION = "record['termination'] != 'cancelled_or_deadline'"
_ASSERT = f"assert {_TERMINATION}"


def _in_try(handler_body, handler_head="except AssertionError:"):
    return f"try:\n    {_ASSERT}\n{handler_head}\n    pass\n"


def _in_suppress(exception):
    return f"with contextlib.suppress({exception}):\n    {_ASSERT}\n"


def _in_if(condition):
    return f"if {condition}:\n    {_ASSERT}\n"


#: ``(label, guard body, must still be reported as able to fail?)``
#:
#: Each row is a distinct way the #261 guard can sit in the source, keep its
#: ``!=`` comparison, and still be unable to fail. The predicate under test is
#: ``guard_rejects_the_deadline_terminal_state``, applied to a guard module
#: built from the row's body.
#:
#: The ``False`` rows are the defeats, and each maps to one of the three
#: mechanisms that exist to catch them: a handler that swallows the failure
#: (``try``/``except``, ``except*``, ``contextlib.suppress``), an operand that
#: short-circuits the comparison away, and a branch the interpreter statically
#: skips. The ``True`` rows are controls and they matter as much: a rule that
#: reported those as defused would cry wolf on a real regression, and the
#: over-broad ``or True`` / ``if False`` readings are exactly where that has
#: already happened on this code. Every ``True`` row was separately confirmed
#: to still raise at runtime when this table passed.
ENFORCEMENT_SHAPES = (
    # --- swallowed by a handler ---
    _shape("except_assertion_error", _in_try(None), False),
    _shape("except_star_assertion_error", _in_try(None, "except* AssertionError:"), False),
    _shape("except_base_exception", _in_try(None, "except BaseException:"), False),
    _shape("except_tuple", _in_try(None, "except (AssertionError, TypeError):"), False),
    _shape("bare_except", _in_try(None, "except:"), False),
    _shape("except_exception", _in_try(None, "except Exception:"), False),
    _shape(
        "local_assertion_error_subclass",
        "class Truncated(AssertionError):\n    pass\n" + _in_try(None, "except Truncated:"),
        False,
    ),
    # --- swallowed by contextlib.suppress ---
    _shape("suppress_assertion_error", _in_suppress("AssertionError"), False),
    _shape("suppress_exception", _in_suppress("Exception"), False),
    _shape("suppress_base_exception", _in_suppress("BaseException"), False),
    # --- short-circuited by a tautological operand ---
    _shape("or_true", f"{_ASSERT} or True\n", False),
    _shape(
        "or_trivially_true_comparison",
        f"{_ASSERT} or len(record.get('errors', [])) >= 0\n",
        False,
    ),
    _shape("true_or_comparison", f"assert True or {_TERMINATION}\n", False),
    _shape("or_nonempty_list_literal", f"{_ASSERT} or [1]\n", False),
    # --- unreachable: statically dead branch ---
    _shape("if_false", _in_if("False"), False),
    _shape("if_none", _in_if("None"), False),
    _shape("if_zero", _in_if("0"), False),
    # --- gutted: nothing left to enforce ---
    _shape("empty_body", "pass\n", False),
    _shape("operator_flipped", "assert record['termination'] == 'cancelled_or_deadline'\n", False),
    _shape("no_assert_at_all", "return None\n", False),
    # --- controls: these must keep being reported as able to fail ---
    _shape("plain_assert", f"{_ASSERT}\n", True),
    _shape("message_argument", f"{_ASSERT}, record['termination']\n", True),
    _shape("except_value_error", _in_try(None, "except ValueError:"), True),
    _shape("suppress_value_error", _in_suppress("ValueError"), True),
    _shape(
        "and_of_two_real_comparisons",
        f"{_ASSERT} and len(record.get('errors', [])) > 0\n",
        True,
    ),
    _shape("or_of_two_real_comparisons", f"{_ASSERT} or len(calls) == 300\n", True),
    _shape("if_true", _in_if("True"), True),
    _shape("runtime_condition", _in_if("record.get('checked')"), True),
    _shape("try_finally", f"try:\n    {_ASSERT}\nfinally:\n    pass\n", True),
    # `assert not x == y` is a `UnaryOp(Not, Compare(Eq))`, not a `NotEq`
    # comparison, so the teeth check does not recognise this spelling. The
    # guard still rejects at runtime, so this row pins the *conservative*
    # direction the helpers document: an assert it cannot prove live is
    # reported defused rather than the reverse. Reporting a live assert as
    # defused costs a false alarm; missing a dead one loses the contract.
    _shape(
        "not_eq_spelled_as_not_eq",
        "assert not record['termination'] == 'cancelled_or_deadline'\n",
        False,
    ),
)


def _guard_tree_for(body):
    """A guard module AST containing ``body`` as the #261 guard's body."""
    source = "def assert_not_deadline_truncated(record):\n"
    source += "".join(f"    {line}\n" if line.strip() else "\n" for line in body.splitlines())
    tree = ast.parse(source)
    return tree.body[0]


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    ENFORCEMENT_SHAPES,
    ids=[shape[0] for shape in ENFORCEMENT_SHAPES],
)
def test_an_assert_that_cannot_fail_is_never_counted_as_the_teeth_check(
    label, body, expected, monkeypatch
):
    """Each defeat shape must be reported defused; each control must not be.

    This is the table that keeps the three mechanisms from being quietly
    narrowed. The rows were each confirmed against a real guard module: every
    ``False`` row leaves the guard returning normally for
    ``{"termination": "cancelled_or_deadline"}``, and every ``True`` row has it
    raise.

    A ``False`` row is a defeat when the guard is genuinely unable to fail, and
    a pinned conservatism when the helper simply cannot *prove* the assert
    live. Both are the documented direction of error: reporting a live assert
    as defused costs a false alarm, missing a dead one loses the contract. The
    table therefore pins ``expected=False`` for the handful of spellings that
    land in that second category, and each says so in a comment.
    """
    function = _guard_tree_for(body)
    monkeypatch.setattr(support, "_guard_source_tree", lambda: ast.Module(body=[function]))
    monkeypatch.setattr(support, "GUARD_FUNCTION", "assert_not_deadline_truncated")
    monkeypatch.setattr(support, "_guard_globals", lambda: {"contextlib": __import__("contextlib")})

    assert support.guard_rejects_the_deadline_terminal_state() is expected, (
        f"{label}: expected the teeth check to report "
        f"{'enforced' if expected else 'defused'}, "
        "so the #261 guard would be certified while unable to fail"
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
