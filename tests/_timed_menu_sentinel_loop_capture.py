"""Loop capture and post-loop rebindings.

Actual test functions and literal cases from the original collector.
"""

import ast
import contextlib

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_import_context_managers import (
    _assert_entry_contract,
)

#: #377 and #378, from the independent review of `c6d488d`: rows whose correct
#: verdict is **live**. They cannot live in ``BINDING_FORM_SHAPES``, because
#: that table executes every fixture and rejects one whose assert fires -- a
#: guard that is right for its own rows (a `defeated` expectation is only
#: credible if the assert really is swallowed) and structurally wrong for these.
#:
#: The failure mode is the mirror of the one the module exists to prevent. That
#: table catches "swallowed but reported live"; this one catches "live but
#: reported defeated", where a real pinned contract stops being counted. Both
#: are silent, so this is stated as its own contract with an executed runtime.
#:
#: ``expected_live`` is the exact verdict for the marked assert and
#: ``swallowed`` says whether CPython really swallows it -- asserted here rather
#: than assumed, so a fixture that stops behaving as described fails loudly
#: instead of quietly testing nothing.
LOOP_ELEMENT_LIVE_SHAPES = (
    (
        "a multi-element loop with the suppressor first leaves it live",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.nullcontext()):\n"
            "        pass\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        True,
        False,
    ),
    # The pair for the row above. Same shape, elements swapped, so the LAST one
    # is the suppressor and the assert is really swallowed. Reading element zero
    # in both directions answered `enforced` where it is swallowed and
    # `defeated` where it is live -- a confident answer that was wrong in both
    # directions at once, which one row on its own cannot pin.
    (
        "a multi-element loop with the suppressor last swallows",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        True,
    ),
    # #378: a loop over an empty literal reaches its body zero times, so the
    # store inside it never ran. Counting the `for` as a store that had executed
    # admitted the dead assignment and reported this live assert as defeated.
    # Master answers `enforced` here, so this was a regression the change
    # introduced rather than a pre-existing gap.
    (
        "a zero-iteration loop does not count as having run its body",
        (
            "    cs = contextlib.nullcontext()\n"
            "    if True:\n"
            "        for x in ():\n"
            "            cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n"
            '            assert x != 1, "A1"'
        ),
        True,
        False,
    ),
    # The non-empty loop is the control for the row above: one element, so the
    # body really does run and the assert really is swallowed. Without it a rule
    # that declined *every* loop would pass both rows.
    (
        "a one-iteration loop does count as having run its body",
        (
            "    cs = contextlib.nullcontext()\n"
            "    if True:\n"
            "        for x in (1,):\n"
            "            cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n"
            '            assert x != 1, "A1"'
        ),
        False,
        True,
    ),
    # #450. A loop over an iterable that provably yields nothing reaches its
    # body zero times, so the store in that body never ran and the carrier is
    # still the one in force. These are the three spellings the rule had
    # declined: two builtin calls and a literal-false `while` test. Each is a
    # false-DEAD on master -- the live assert was reported defeated.
    (
        "a zero-iteration builtin-constructor loop does not count as having run",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in set():\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        True,
        False,
    ),
    (
        "a zero-iteration range loop does not count as having run",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in range(0):\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '            assert x != 1, "A1"'
        ),
        True,
        False,
    ),
    # A negative one-argument bound is empty for the same reason `range(0)` is:
    # `range(stop)` counts from 0 upwards by 1, so any `stop <= 0` yields
    # nothing. The one-argument branch read this as `not stop`, which is
    # "not empty" for every negative bound, so these two rows were missing and
    # the defect shipped through a green suite.
    #
    # `range(-10**18)` is here to pin the *no-overflow* property: the fold
    # compares the integers directly, so a bound far outside a machine word
    # cannot round through a float and flip the answer.
    (
        "a negative one-argument range loop does not count as having run",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in range(-5):\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '            assert x != 1, "A1"'
        ),
        True,
        False,
    ),
    (
        "a very large negative range bound does not count as having run",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in range(-10**18):\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '            assert x != 1, "A1"'
        ),
        True,
        False,
    ),
    # The control that separates this from "any non-empty-looking number is
    # empty": `range(-1)` is empty, `range(1)` is not, and both are written
    # with a leading minus/plus so only the sign decides.
    (
        "CONTROL a one-argument range of one is not empty",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in range(1):\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '            assert x != 1, "A1"'
        ),
        False,
        True,
    ),
    (
        "a literal-false while loop does not count as having run",
        (
            "    cs = contextlib.nullcontext()\n"
            "    while False:\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '            assert x != 1, "A1"'
        ),
        True,
        False,
    ),
    # The controls for the three rows above, one per decision the widening
    # makes. Without them a rule that answered "empty" for *every* loop, or
    # that read only the first `range` argument, would pass all three.
    #
    # `range(5, 0)` and `range(0, 5, -1)` are empty but do not read as zero in
    # their leading argument, so they are the rows that separate the fold from
    # a first-argument test. `range(0, 5)` is the opposite: a leading zero and
    # a non-empty range, so reading the first argument would defeat a live
    # header here.
    (
        "CONTROL a descending range with an empty span is still empty",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in range(5, 0):\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '            assert x != 1, "A1"'
        ),
        True,
        False,
    ),
    (
        "CONTROL a non-empty range with a leading zero does count as having run",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in range(0, 5):\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '            assert x != 1, "A1"'
        ),
        False,
        True,
    ),
    (
        "CONTROL a range of one element does count as having run",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in range(1):\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '            assert x != 1, "A1"'
        ),
        False,
        True,
    ),
    # The unreadable-argument control the issue calls for by name: a builtin
    # name that is not the builtin yields, so its body really does run. This
    # is the row that keeps the widening from reading `set`/`range` as the
    # builtins unconditionally.
    (
        "CONTROL a shadowed constructor does count as having run",
        (
            "    def set():\n"
            "        return [0]\n"
            "    cs = contextlib.nullcontext()\n"
            "    for item in set():\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '            assert x != 1, "A1"'
        ),
        False,
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "expected_live", "swallowed"),
    LOOP_ELEMENT_LIVE_SHAPES,
    ids=[row[0] for row in LOOP_ELEMENT_LIVE_SHAPES],
)
def test_a_loop_binds_the_element_it_leaves_behind(label, body, expected_live, swallowed):
    """A ``for`` over a literal must resolve to what the loop leaves bound.

    Two questions, and both directions are load-bearing. Which element survives
    a multi-element loop is the LAST one, not the first, and which direction
    that decides depends on the order. And a loop that provably iterates zero
    times must not be treated as proof that its body's stores ran.
    """
    source = "def outer(x, helper):\n    import contextlib\n" + body + "\n"
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    fired = False
    # One real, non-suppressing element, for the same reason as the
    # binding-form table: an empty `items()` made the `with cs:` body
    # unreachable, so `fired` stayed False for every row regardless of what
    # the analyzer said and the runtime half of this test proved nothing.
    _yielded = [contextlib.nullcontext()]
    try:
        namespace["outer"](1, type("H", (), {"items": staticmethod(lambda: _yielded)})())
    except AssertionError:
        fired = True
    except (NameError, TypeError, UnboundLocalError) as error:
        raise AssertionError(
            f"{label}: the fixture raised {type(error).__name__} instead of "
            f"running the assert. Row is stale."
        ) from None
    # Stated, not assumed: a fixture that stops behaving as described fails
    # loudly rather than quietly testing nothing.
    assert fired is not swallowed, (
        f"{label}: expected the assert to "
        f"{'fire' if not swallowed else 'be swallowed'}, but it "
        f"{'was swallowed' if not swallowed else 'fired'}."
    )

    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [expected_live], f"{label}: expected {[expected_live]}, got {results}."


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
