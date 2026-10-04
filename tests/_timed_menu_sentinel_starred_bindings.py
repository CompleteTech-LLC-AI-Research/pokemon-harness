"""Starred target bindings and observable stores.

Actual test functions and literal cases from the original collector.
"""

import ast
import contextlib

import pytest

from tests import _timed_menu_milestone_sentinel_support as support
from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_capture_namespaces import (
    CARRIER_ENTRY_UNREACHABLE_ROWS,
    STARRED_TARGET_ENTRY_UNREACHABLE_ROWS,
)
from tests._timed_menu_sentinel_import_context_managers import (
    _assert_entry_contract,
)

#: #359: the binding forms whose right-hand side is an *element* of a
#: container, or a loop's next element, rather than the whole value.
#:
#: Every row here binds a name to a real ``contextlib.suppress`` and then reads
#: it back -- directly, or through a self-alias. The assert under it is
#: genuinely swallowed on all of them, so each row's ``defeated`` verdict is a
#: *contract* and not a preference.
#:
#: These are the spellings the per-statement rules could not read. A ``for``
#: target was recorded with no value at all, a destructuring target was
#: recorded with the whole container, and :func:`_bindings_before` did not count
#: a loop as a store that had run by the time a ``with`` nested in its body was
#: read. Each of those reported a swallowed assert as ``enforced`` -- a
#: disarmed contract certified as load-bearing, the damaging direction per #308
#: criterion 1.
#:
#: ``expected_live`` is the exact verdict for the marked assert.
#: ``control_element`` pairs each row with the same spelling over a
#: ``nullcontext()`` element, which is genuinely live; without it a rule that
#: answered "defeated" for everything would pass this table while dropping
#: every real contract in it. ``None`` means the row has no readable control,
#: because an arbitrary iterable genuinely is undecidable.
BINDING_FORM_SHAPES = (
    (
        "a tuple-unpack target reads its own element",
        (
            "    cs, other = (contextlib.suppress(AssertionError), 2)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a list target reads its own element",
        (
            "    [cs] = [contextlib.suppress(AssertionError)]\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a nested destructuring target reads the nested element",
        (
            "    cs, (other, third) = (contextlib.suppress(AssertionError), (2, 3))\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a nested second-position target reads its own element",
        (
            "    (other, (cs, third)) = (2, (contextlib.suppress(AssertionError), 3))\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a loop target reads the iterated element",
        (
            "    for cs in (contextlib.suppress(AssertionError),):\n"
            "        with cs:\n"
            '            assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a self-alias after a loop target keeps the element",
        (
            "    for cs in (contextlib.suppress(AssertionError),):\n"
            "        with (cs := cs):\n"
            '            assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a self-alias in a tuple target keeps its own element",
        (
            "    cs, other = (contextlib.suppress(AssertionError), 2)\n"
            "    with (cs := cs):\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a starred target is declined rather than paired with an element",
        (
            "    cs, *rest = (contextlib.suppress(AssertionError), 2, 3)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a target after a starred one is bound from the end",
        (
            "    first, *rest, cs = (1, 2, 3, contextlib.suppress(AssertionError))\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a leading star still binds the trailing target",
        (
            "    *rest, cs = (1, 2, contextlib.suppress(AssertionError))\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "two targets after a star bind from the end in order",
        (
            "    first, *rest, cs, last = "
            "(1, 2, 3, contextlib.suppress(AssertionError), 5)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a deeply nested target reads the deeply nested element",
        (
            "    a, (b, (cs, d)) = "
            "(1, (2, (contextlib.suppress(AssertionError), 4)))\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a loop over an unreadable iterable is declined, not guessed",
        ('    for cs in helper.items():\n        with cs:\n            assert x != 1, "A1"'),
        True,
        None,
    ),
    # #359, residual found by the 09:00Z lead triage and confirmed on this
    # head: the container does not have to be a *literal* at the destructuring
    # site. A name already bound to one is the common spelling, and picking the
    # element out of the right-hand side saw a bare `Name` instead of a
    # container, so the suppressor was never extracted and the swallowed assert
    # came back `enforced`. All four were damaging.
    (
        "a tuple target over a named container reads the element",
        (
            "    t = (contextlib.suppress(AssertionError),)\n"
            "    cs, = t\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a first-of-two target over a named container reads the element",
        (
            "    t = (contextlib.suppress(AssertionError), 2)\n"
            "    cs, other = t\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a second-of-two target over a named container reads the element",
        (
            "    t = (2, contextlib.suppress(AssertionError))\n"
            "    other, cs = t\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "a nested target over a named container reads the nested element",
        (
            "    t = (2, (contextlib.suppress(AssertionError), 3))\n"
            "    other, (cs, third) = t\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    (
        "both suppressor names survive a swap at the binding epoch",
        (
            "    a = contextlib.suppress(AssertionError)\n"
            "    b = contextlib.suppress(AssertionError)\n"
            "    a, b = b, a\n"
            "    with a:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    # #381/#382: the element a destructuring target receives can be a *Name*,
    # and then it stands for whatever that name holds rather than for itself.
    # A swap makes that unavoidable -- both elements are names -- and it is the
    # shape that decides the verdict, because the answer depends entirely on
    # which name lands on the target. The two rows below are one pair: same
    # spelling, elements exchanged, opposite verdicts. A rule that reported
    # "defeated" for every name element, or "live" for every name element,
    # passes one of them and fails the other, which is the point.
    (
        "a swapped element that lands a suppressor is defeated",
        (
            "    a = contextlib.nullcontext()\n"
            "    b = contextlib.suppress(AssertionError)\n"
            "    a, b = b, a\n"
            "    with a:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        # Each swap row is the other's control: exchanging the two elements
        # flips the verdict, and a rule that cannot tell them apart fails one.
        # A `nullcontext()` substitution would not help here, because the whole
        # question is *which* element the target receives.
        None,
    ),
    (
        "a swapped element that lands a live manager is enforced",
        (
            "    a = contextlib.suppress(AssertionError)\n"
            "    b = contextlib.nullcontext()\n"
            "    a, b = b, a\n"
            "    with a:\n"
            '        assert x != 1, "A1"'
        ),
        True,
        None,
    ),
    (
        "a list target over a named container reads the element",
        (
            "    t = [contextlib.suppress(AssertionError)]\n"
            "    [cs] = t\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
    # The same element reached through two hops has to survive both. Only the
    # first link is a destructuring one, so a rule that stopped after one step
    # would answer "enforced" here while the assert is swallowed.
    (
        "a target over an aliased named container reads the element",
        (
            "    t = (contextlib.suppress(AssertionError), 2)\n"
            "    u = t\n"
            "    cs, other = u\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
        "contextlib.nullcontext()",
    ),
)


@pytest.mark.parametrize(
    ("label", "rebind", "second_assert_live"),
    CARRIER_ENTRY_UNREACHABLE_ROWS,
    ids=[row[0] for row in CARRIER_ENTRY_UNREACHABLE_ROWS],
)
def test_a_string_field_carrier_cannot_be_entered_so_the_assert_is_unreachable(
    label, rebind, second_assert_live
):
    """A carrier binds the name to something no ``with`` can enter.

    This is #359. The bug it reports is a *coverage* gap rather than a defect
    on master: master answers every row below correctly, which is exactly why
    the gap survived. The gap is in what the shipped tables pin, and it is
    demonstrably load-bearing -- see the mutation note in
    ``ledger/FINDING_359_GATE_STILL_OPEN_20260928.md``. Reverting the
    ``_entry_is_dead`` value-less branch so that a carrier no longer reads as
    a dead entry leaves the whole 273-test sentinel lane **green**, and
    reproduces the #337 regression this issue was filed about.

    The first assert is the retirement contract and is ``False`` on every row,
    including the controls: the carried walrus suppressor is entered and
    swallows it. The second is the entry contract and is what divides.

    AC2 is the reason this test **executes** rather than only reading the
    checker. Every row is run on CPython and held to ``second_assert_live``
    through ``_assert_entry_contract``, so a row cannot claim "live" without
    the interpreter agreeing, and cannot claim "dead" either. That is what
    makes a table that reads only the checker unable to hide here: the
    interpreter is in the loop.

    Measured on CPython 3.12.14, with ``x=1`` so ``assert x != 1`` is false:

    * ``import os as cs`` and ``from os import path as cs`` raise
      ``TypeError: 'module' object does not support the context manager
      protocol``.
    * ``def cs(): pass`` raises ``TypeError: 'function' object ...``.
    * ``class cs: pass`` raises ``TypeError: 'type' object ...``.
    * ``case [cs]:`` over the subject ``[1]`` binds the integer ``1``, so the
      capture row raises ``TypeError: 'int' object ...``.
    * both controls enter cleanly and the assert fires.

    One correction worth recording, because the first draft of the second
    control got it wrong. The control was written as a ``class cs:`` that
    *defines* ``__enter__``/``__exit__``, expecting it to be enterable. It is
    not. ``with cs:`` enters the **class object itself**, and a class is not a
    context manager no matter what its instances support -- measured on 3.12.14
    it raises ``TypeError: 'type' object does not support the context manager
    protocol`` exactly as a bare ``class cs: pass`` does. The name has to hold
    an *instance*, so the row ends ``cs = cs()``. Defining the dunders is
    necessary and not sufficient; this is the same reason
    ``_defines_context_manager_protocol`` reads a class body without
    concluding the binding is enterable.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + rebind + "\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, second_assert_live)
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 2, f"{label}: fixture declared {len(asserts)} asserts, expected 2"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [False, second_assert_live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A carrier that "
        f"cannot be entered makes the assert under the header *unreachable*, "
        f"so reporting it enforced certifies a dead contract as load-bearing."
    )


@pytest.mark.parametrize(
    ("label", "rebind", "second_assert_live"),
    STARRED_TARGET_ENTRY_UNREACHABLE_ROWS,
    ids=[row[0] for row in STARRED_TARGET_ENTRY_UNREACHABLE_ROWS],
)
def test_a_starred_target_binds_a_list_so_the_assert_is_unreachable(
    label, rebind, second_assert_live
):
    """A name bound through ``ast.Starred`` holds a list, which cannot be entered.

    This is #418, and it is a real defect on ``origin/master`` (``6b72bf6``)
    rather than a coverage gap: every row below reported the unreachable
    assert ``enforced`` before the fix.

    The failure is an ordering error inside ``_entry_is_dead``. The general
    destructuring branch declines ``cs, other = (a, b)`` because the type of a
    plain element is not readable from the container's syntax, and declining is
    the safe answer -- the assert stays live. A starred target was falling into
    that same decline, but it is decidable: ``*cs, = (...)`` and ``a, *cs =
    (...)`` collect a run of elements into a **list** regardless of what the
    right-hand side held. Letting the wrapped element's own kind vouch for the
    name is what let a usable ``contextlib.suppress`` certify a bare list as an
    enterable object.

    Measured on CPython 3.12.14, with ``x=1`` so ``assert x != 1`` is false:

    * ``*cs, = (suppress(),)`` binds ``[suppress_object]``
    * ``a, *cs = (suppress(), 2)`` binds ``[2]``
    * ``b, *c = (1, suppress())`` binds ``[suppress_object]``

    and ``hasattr(cs, "__enter__")`` is ``False`` in every case, so
    ``with cs:`` raises ``TypeError`` before the body and the assert is
    unreachable. Note the third shape: a starred tail that *does* contain a
    usable context manager is still a list, so the element's own kind is
    irrelevant to the entry decision.

    The control keeps the sibling decline honest. Its expected value is
    ``True`` and the interpreter agrees -- ``cs, other = (nullcontext(), 2)``
    really does enter and really does fire. A fix that keyed on "any
    destructuring target" instead of "a starred target" would report this dead,
    and this test would catch it.

    A second control was drafted and then removed rather than shipped: the
    matching non-starred row with a *suppressor* element
    (``cs, other = (suppress(), 2)``) is **also** reported ``enforced`` by the
    analyzer, while CPython swallows it -- the same false-live, on the
    non-starred path. That is the ``#336`` element-decline family, not this
    issue, and it is unaffected by the change under test. It was caught here
    because ``_assert_entry_contract`` executes the fixture rather than
    trusting the expected column, which is the only reason a row this stale
    could not have been shipped silently. Measured ground truth for it:

    | store                                     | CPython 3.12.14 | analyzer |
    |-------------------------------------------|-----------------|----------|
    | ``cs, other = (nullcontext(), 2)``        | fires (live)    | ``True`` |
    | ``cs, other = (suppress(), 2)``           | swallowed       | ``True`` |
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + rebind + "\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, second_assert_live)
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 2, f"{label}: fixture declared {len(asserts)} asserts, expected 2"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [False, second_assert_live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A starred target "
        f"binds a list, and entering a list raises before the body, so the "
        f"assert under the header is unreachable."
    )


@pytest.mark.parametrize(
    ("store", "starred", "live"),
    (
        ("*cs, cs = (1, contextlib.nullcontext())", False, True),
        ("(*cs, (cs,)) = (1, (contextlib.nullcontext(),))", False, True),
        ("*cs, tail = head, cs = (1, contextlib.nullcontext())", False, True),
        ("head, cs = *cs, tail = (1, contextlib.nullcontext())", True, False),
        ("cs, *cs = (contextlib.nullcontext(), 1)", True, False),
        ("(*cs, (cs, *cs)) = (1, (contextlib.nullcontext(), 2))", True, False),
        ("*(cs, tail), = (contextlib.nullcontext(), 2)", False, True),
    ),
)
def test_starred_target_kind_uses_the_final_store(store, starred, live):
    """A repeated target name receives its last value, including nested stores.

    Pin the dead-entry predicate directly: an older duplicate-binding
    limitation elsewhere in `_is_enforced` masks this specific regression.
    Every expected entry outcome is checked by executing the same fixture.
    """
    source = (
        f"def outer(x, flag, helper):\n    import contextlib\n    {store}\n"
        "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract(store, source, False, live)
    tree = ast.parse(source)
    function = tree.body[0]
    statement = function.body[1]
    header = function.body[2]
    assert support._binds_starred_target(statement, "cs") is starred
    assert support._entered_name_is_dead(header, function, {"contextlib": "contextlib"}, tree) is (
        not live
    )


@pytest.mark.parametrize(
    "store",
    (
        "*cs, tail = cs = (1, contextlib.nullcontext())",
        "(*cs, tail) = (cs, tail) = (1, contextlib.nullcontext())",
    ),
)
def test_later_chained_plain_target_clears_a_starred_binding(store):
    statement = ast.parse(store).body[0]
    assert not support._binds_starred_target(statement, "cs")


#: AC1's second position. A carrier can sit in two places relative to the
#: header that reads it: as a *later sibling* statement (the rows above), or
#: **inside the header's own scope**, so it has already run by the time the
#: name is entered. Both are pinned because they are not a superset of one
#: another -- the first is decided by source order in the binding resolver, the
#: second by containment in the header, and a rule that handled only one would
#: pass the table above while leaving this hole open.
#:
#: Runtime is identical for both positions: the header's own body rebinds
#: ``cs`` before the body of the *next* ``with`` is entered, and entering a
#: module, function, class or the captured ``1`` raises ``TypeError`` in either
#: arrangement. Measured on CPython 3.12.14 with ``x=1``:
#:
#:     with (cs := suppress(AssertionError)):
#:         import os as cs
#:     with cs:
#:         assert x != 1        # TypeError: 'module' object ...
CARRIER_IN_HEADER_ROWS = (
    ("import-as inside the header", "        import os as cs", False),
    (
        "import-from-as inside the header",
        "        from os import path as cs",
        False,
    ),
    ("def inside the header", "        def cs():\n            pass", False),
    ("class inside the header", "        class cs:\n            pass", False),
    (
        "a match capture inside the header",
        "        match [1]:\n            case [cs]:\n                pass",
        False,
    ),
    (
        "CONTROL a plain assign inside the header",
        "        cs = nullcontext()",
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "expected_live", "control_element"),
    BINDING_FORM_SHAPES,
    ids=[row[0] for row in BINDING_FORM_SHAPES],
)
def test_a_binding_form_reads_the_value_that_lands_on_the_name(
    label, body, expected_live, control_element
):
    """A destructuring or loop target must resolve to its own element.

    The one question every row asks is whether the analyzer sees the
    ``suppress`` that really is bound to ``cs``. If it does, the marked assert
    is swallowed and must be reported defeated; if it does not, the very same
    code is reported enforced and a disarmed contract is certified as
    load-bearing.

    Runtime is executed per row, so a ``defeated`` expectation is credible only
    when CPython really swallows the assert. The control row repeats the
    spelling with a ``nullcontext()`` element, which does not suppress, and
    requires that one to be reported live. That pair is what stops this table
    from degenerating into "report every alias as a suppressor".
    """
    source = "def outer(x, helper):\n    import contextlib\n" + body + "\n"
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    outer = namespace["outer"]
    # A helper whose `items()` yields exactly one *non-suppressing* manager.
    # Yielding nothing was the original spelling and it made this fixture
    # vacuous: the unreadable-iterable row ran its body zero times, never
    # reached the assert, and passed its runtime check without executing the
    # code it claims to pin. With one real element the body really runs, the
    # assert really is reached, and because the element is a `nullcontext` it
    # cannot swallow anything -- so a rule that answered "defeated" for every
    # unreadable iterable now fails here instead of passing silently.
    _yielded = [contextlib.nullcontext()]
    helper = type("H", (), {"items": staticmethod(lambda: _yielded)})()
    fired = False
    try:
        outer(1, helper)
    except AssertionError:
        fired = True
    except (NameError, TypeError, UnboundLocalError) as error:
        raise AssertionError(
            f"{label}: the fixture raised {type(error).__name__} instead of "
            f"running the assert. Row is stale."
        ) from None

    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    # The runtime half is a cross-check against the verdict this row expects,
    # not a one-sided "it did not fire" test. A row that claims the assert is
    # swallowed must be shown CPython swallowing it, and a row that claims the
    # assert is live must be shown the failure actually escaping -- otherwise a
    # fixture that never reaches its assert passes either way. `fired` and
    # `expected_live` describe the same two states, so they must be equal, and
    # that also keeps the live rows honest: they are the ones that were
    # previously vacuous.
    assert fired is expected_live, (
        f"{label}: expected the assert to "
        f"{'fire' if expected_live else 'be swallowed'}, but it "
        f"{'did not fire' if expected_live else 'was swallowed'}."
    )
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [expected_live], f"{label}: expected {[expected_live]}, got {results}."

    if control_element is None:
        return
    control = body.replace("contextlib.suppress(AssertionError)", control_element)
    assert control != body, f"{label}: the control row is identical to the row"
    control_source = "def outer(x, helper):\n    import contextlib\n" + control + "\n"
    control_tree = ast.parse(control_source)
    control_function = control_tree.body[0]
    control_asserts = [node for node in ast.walk(control_function) if isinstance(node, ast.Assert)]
    control_results = [
        _is_enforced(control_function, node, control_tree) for node in control_asserts
    ]
    assert control_results == [True], (
        f"{label}: the nullcontext control must be reported live, got "
        f"{control_results}. A rule that calls this defeated drops a real "
        f"contract."
    )


@pytest.mark.parametrize(
    ("label", "rebind", "second_assert_live"),
    CARRIER_IN_HEADER_ROWS,
    ids=[row[0] for row in CARRIER_IN_HEADER_ROWS],
)
def test_a_carrier_inside_the_reading_header_leaves_the_name_unenterable(
    label, rebind, second_assert_live
):
    """The carrier ran inside the header, and the header is still unenterable.

    The second half of #359 AC1. Here the rebind happens in the body of the
    ``with (cs := ...)`` header itself, so the carried suppressor is entered
    and exits *after* the rebind has already replaced the name. The following
    ``with cs:`` therefore reads the carrier's value, not the suppressor, and
    the position cannot be decided by the source ordering the sibling rows use.

    Like the table above, every row is executed and checked against CPython
    through ``_assert_entry_contract`` (AC2), and the control row is a plain
    assignment of a real context manager that must stay live (AC3).
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n" + rebind + "\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, second_assert_live)
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [second_assert_live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A carrier that "
        f"cannot be entered makes the assert unreachable, not load-bearing."
    )
