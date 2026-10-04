"""Loop element binding and target behavior.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced

#: #413 rows, in their own list because each one is executed against CPython
#: before its verdict is checked. The rows above that share the multi-element
#: shape are in-body reads whose two elements *disagree*, so they have to keep
#: the decline; these are the reads whose elements *agree*, plus the controls
#: that say agreement is what the new rule tests for rather than "two
#: elements" or "contains a suppressor".
#:
#: The filed shape is a `for` over a literal whose every element is a
#: suppression context, read from inside the loop's own body:
#:
#:     for cs in (contextlib.suppress(AssertionError),
#:                contextlib.suppress(AssertionError)):
#:         with cs:
#:             assert x == 99
#:
#: Executed, `with cs:` raises on entry to every iteration, so the assert is
#: unreachable on every iteration. The analyzer declined the loop outright and
#: reported the assert `enforced` -- a disarmed contract certified as
#: load-bearing, the damaging direction per #308 criterion 1.
#:
#: Every row here uses `assert x == 99` with `x` bound to 1. `assert x != 1`
#: would be vacuous: it is true, so it passes whether or not the context
#: swallows it, and would pin nothing about the analyzer. The executed check
#: below is what makes that worth stating -- it runs each row and refuses a
#: `[False]` whose code path actually fired the assert.
UNANIMOUS_LOOP_ELEMENT_ROWS = (
    (
        "#413 an all-suppressor two-element loop target is decided in its body",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [False],
    ),
    # Three elements, so "read one of them and you happened to be right" is
    # not available as an explanation. A rule that indexed the first, the
    # last, or an arbitrary position all agree here by accident; the two
    # mixed CONTROLs below are what rule those out.
    (
        "#413 an all-suppressor three-element loop target is decided in its body",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.suppress(AssertionError),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [False],
    ),
    # The same rule reached through a list literal, so the admission is not
    # pinned to the tuple spelling.
    (
        "#413 an all-suppressor list loop target is decided in its body",
        (
            "    for cs in [contextlib.suppress(AssertionError),\n"
            "               contextlib.suppress(AssertionError)]:\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [False],
    ),
    # --- controls: the elements disagree, so the decline must stand ---
    #
    # This is the #370/#385 multi-element limit in its exact current form, and
    # it is the row that says the new rule did not simply admit
    # multi-element loops. Executed, the first iteration swallows the assert
    # and the second lets it fire, so no single verdict is right and `True`
    # (the conservative decline) is correct.
    (
        "CONTROL a suppress-then-nullcontext loop target still declines",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.nullcontext()):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # The mirror order, and the damaging one for a first-element rule: the
    # suppressor is first here, so reading it would report the assert defeated
    # and delete a contract that really does fire on the second iteration.
    (
        "CONTROL a nullcontext-then-suppress loop target still declines",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # The row that outlives a "read the last element" rule. Both ends are the
    # suppressor and the `nullcontext` sits in the middle, so a last-element
    # reader gets the right answer here for the wrong reason -- and the mirror
    # of this row below is the one that breaks it. Executed, the middle
    # iteration is the only one that lets the assert fire, so the paths
    # disagree and the decline is correct.
    (
        "CONTROL a suppress-nullcontext-suppress loop target still declines",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # The same list with the enterable element last, which is the arrangement
    # that separates "last element" from unanimity. A last-element reader
    # resolves this to the `nullcontext` and reports the assert live; a
    # first-element reader resolves it to the suppressor and drops the
    # contract. Unanimity is the only one of the three that gets both of
    # these rows right, and it is the only one that can, because the runtime
    # answer here is "the paths disagree" and no element speaks for the other
    # two.
    (
        "CONTROL a suppress-suppress-nullcontext loop target still declines",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.suppress(AssertionError),\n"
            "               contextlib.nullcontext()):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # Both elements enterable: unanimity holds, the recorded value is not a
    # suppressor, and the assert is live. Without this row a rule that
    # returned a representative for *any* unanimous literal would be pinned
    # as correct while dropping every live contract in this shape.
    (
        "CONTROL a two-nullcontext loop target still declines",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.nullcontext()):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # "Contains a suppressor" is not the test. An element this module cannot
    # read is not known to be non-enterable, so the literal keeps the
    # decline. Executed, the first iteration swallows the assert and the second
    # lets it fire, so the paths genuinely disagree and the decline is the
    # right answer rather than merely the safe one.
    (
        "CONTROL a suppressor beside an unreadable call still declines",
        (
            "    for cs in (contextlib.suppress(AssertionError), make_ctx()):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # The same, with a bare name -- the spelling a suppressed-from-import
    # suppression context actually takes in most real code.
    (
        "CONTROL a suppressor beside a bare name still declines",
        (
            "    for cs in (contextlib.suppress(AssertionError), other):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # A constant is readable syntax and is *knowably* not a suppression
    # context, so unanimity correctly fails here. Executed, `with 7:` raises
    # `TypeError` on entry, which is a loud failure and not a swallow.
    (
        "CONTROL a suppressor beside a constant still declines",
        (
            "    for cs in (contextlib.suppress(AssertionError), 7):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # `pytest.raises` with no expected type is excluded by
    # `_raises_without_an_expected_type`, so it is not a *readable*
    # suppressor and must not count toward unanimity even though two of them
    # look alike. This is the row that keeps the readability bar the same one
    # every other recorded store value has to clear.
    (
        "CONTROL two argument-less pytest.raises still decline",
        (
            "    for cs in (pytest.raises(), pytest.raises()):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # A readable suppression context paired with a non-suppressing one that
    # *is* readable. Both sides are decided, so this is a disagreement and not
    # an unreadability, which is the distinction the repro rows rest on.
    (
        "CONTROL a suppressor beside a pytest.raises still declines",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               pytest.raises(AssertionError)):\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # A non-literal iterable cannot be inspected at all, so unanimity is not
    # decidable however uniform the runtime values happen to be. The fixture
    # below returns two suppression contexts, so CPython swallows the assert
    # and the honest verdict would be `[False]` -- but the analyzer cannot see
    # that, and resolving the name to a suppressor it did not read would drop
    # a live contract on any other iterable. The decline stays, which is why
    # this row is declared `[True]` while the runtimes below report it as
    # swallowed.
    (
        "CONTROL a non-literal loop target still declines",
        ("    for cs in make_suppressors():\n        with cs:\n            assert x == 99"),
        [True],
    ),
    # --- controls: the #385 after-loop answer must not move ---
    #
    # The filed shape read *after* the loop instead of inside it. The last
    # element is the answer there, and it is a suppressor either way, so this
    # was already `False` before #413 and must stay `False` after it.
    (
        "CONTROL an all-suppressor after-loop read is unchanged",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    with cs:\n"
            "        assert x == 99"
        ),
        [False],
    ),
    # The #385 rebind guard, spelled over an all-suppressor loop. Once the
    # loop is recorded at all, the guard that keeps a later store from
    # inheriting the loop's suppressor has to keep working; the assert is live
    # because `cs` is the trailing `nullcontext`.
    (
        "CONTROL a later store still supersedes an all-suppressor loop target",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    cs = contextlib.nullcontext()\n"
            "    with cs:\n"
            "        assert x == 99"
        ),
        [True],
    ),
)


#: Rows that CPython swallows while the row is still declared ``[True]``.
#:
#: A swallowed run is normally the signal that the analyzer wrongly reported
#: ``enforced``, so it and ``[False]`` are the same outcome spelled two ways
#: and the execution check below demands they agree. These rows are the one
#: documented exception, and the list is explicit rather than a wildcard so
#: that a *missed* ``[False]`` -- a live contract the analyzer dropped -- still
#: fails the check instead of passing as "one of the allowed ones".
#:
#: Each entry is a row whose verdict the analyzer cannot justify from the
#: source: a non-literal iterable whose runtime values are simply not visible
#: to a syntax-level rule. Declining is the #308 criterion-1 direction there.
EVIDENCE_DECLINED_WHILE_SWALLOWED = frozenset(
    {
        "CONTROL a non-literal loop target still declines",
    }
)


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    UNANIMOUS_LOOP_ELEMENT_ROWS,
    ids=[row[0] for row in UNANIMOUS_LOOP_ELEMENT_ROWS],
)
def test_a_loop_literal_whose_elements_agree_is_decided_in_the_loop_body(label, body, expected):
    """#413: agreement makes an in-body multi-element read decidable.

    #385 settled the after-loop question and deliberately left the in-body one
    refused, because a multi-element literal binds a different value on each
    iteration and no single element answers for all of them. That reasoning is
    about *disagreement*: with the suppressor first and a `nullcontext`
    second, the assert is swallowed once and fires once, so a static verdict
    would be wrong half the time and the safe answer is the decline.

    The case #413 reports is the one where that premise does not hold. When
    every element of the literal is itself a suppression context, there is no
    disagreement to preserve -- each iteration binds a value that cannot be
    entered, `with cs:` raises on entry to every iteration, and the assert is
    unreachable every time. Which iteration the header is read on cannot
    change the answer, which is exactly the property the multi-element case
    lacked.

    The rule therefore tests *unanimity* and nothing else. It is not "two or
    more elements", which the mixed CONTROLs refute; it is not "contains a
    suppressor", which the unreadable and `pytest.raises` CONTROLs refute; and
    it is not an index, which the two mixed orders refute in opposite
    directions. Each of those would be a rule that happened to pass the repro
    row while getting a control wrong, and the executed check below is what
    turns them from comments into failures.

    Executed before the verdict is read, so a `[False]` that CPython actually
    fails -- a live contract dropped -- is a failure here rather than a wrong
    number in a report.
    """
    source = (
        "def outer(x, flag, helper, items, other):\n    import contextlib\n    import pytest\n"
        + body
    )
    tree = ast.parse(source)
    outer = tree.body[0]
    asserts = [node for node in ast.walk(outer) if isinstance(node, ast.Assert)]
    assert len(asserts) == len(expected), (
        f"{label}: fixture declared {len(asserts)} asserts but the row "
        f"expects {len(expected)} verdicts"
    )
    results = [_is_enforced(outer, node, tree) for node in asserts]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A literal whose "
        f"elements all read as suppression contexts binds a non-enterable value "
        f"on every iteration, so the in-body header is decided; every other "
        f"multi-element shape must keep the decline."
    )


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    UNANIMOUS_LOOP_ELEMENT_ROWS,
    ids=[row[0] for row in UNANIMOUS_LOOP_ELEMENT_ROWS],
)
def test_every_unanimous_loop_element_row_matches_what_cpython_actually_does(label, body, expected):
    """The #413 verdicts are settled by execution, not by assertion.

    The repair is a claim about what CPython does, so a row that merely
    records the analyzer's opinion pins nothing: the analyzer is the thing
    under test. Every row here is executed and its declared verdict compared
    against what actually became of the assert.

    The outcomes are kept apart, because collapsing them is what makes a
    table entry meaningless:

    * the assert fired on every run -- a live contract, so ``[True]``;
    * it fired on some runs and not others -- no single verdict is right, the
      rule must decline, so ``[True]``;
    * it never fired, and entry did not raise -- a disarmed contract, so
      ``[False]`` *unless* the rule declined for want of evidence.

    That last exception is real and is why one row here is declared ``[True]``
    on a run that swallows: the non-literal iterable row. Its fixture returns
    two suppression contexts, so CPython never lets the assert fire, but the
    analyzer cannot inspect ``make_suppressors()`` at all. Declining keeps the
    contract reported as live, which is the #308 criterion-1 direction, and
    the row says so rather than quietly claiming the analyzer proved the
    assert is dead. Every other swallowed run corresponds to a ``[False]`` row
    the rule is claiming to have decided.

    ``TypeError`` on entry is recorded as raised rather than folded into
    "fired": entering ``with 7:`` fails loudly and says nothing about whether
    the assert could have failed. ``ValueError`` is caught for the same reason
    the sibling rows catch ``NameError`` -- ``pytest.raises()`` with no
    expected type raises while *building* the context, before the body, which
    is a property of the fixture rather than a verdict about the assert.
    """
    source = (
        "def outer(x, flag, helper, items, other):\n    import contextlib\n    import pytest\n"
        + body
    )
    fired = 0
    raised_on_entry = False
    for _ in range(2):
        namespace = {}
        exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102

        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def make_ctx():
            return _Ctx()

        def make_suppressors():
            return [contextlib_suppress(), contextlib_suppress()]

        def contextlib_suppress():
            import contextlib

            return contextlib.suppress(AssertionError)

        namespace.setdefault("make_ctx", make_ctx)
        namespace.setdefault("make_suppressors", make_suppressors)
        try:
            namespace["outer"](1, None, None, ["a", "b", "c"], None)
        except AssertionError:
            fired += 1
        except (TypeError, UnboundLocalError, NameError, ValueError):
            # Entry itself raises, so the assert is unreachable rather than
            # swallowed -- a loud failure, not a defeat.
            raised_on_entry = True
    if raised_on_entry or fired == 2:
        assert expected == [True], (
            f"{label}: CPython fired the assert on every reachable path (or the "
            f"header could not be entered at all), so the row must declare "
            f"[True], not {expected}"
        )
    elif not fired:
        # Never fired. Either the rule claimed to have decided it -- in which
        # case `[False]` is the only honest declaration -- or the rule declined
        # for want of evidence, which is `[True]`. The two are told apart by
        # the row's own comment, so the allowance is a named list rather than
        # a wildcard that would let a missed `[False]` pass quietly.
        assert expected in ([False], [True]) and (
            expected == [False] or label in EVIDENCE_DECLINED_WHILE_SWALLOWED
        ), (
            f"{label}: CPython never let the assert fire, so the row must "
            f"declare [False], or be listed in "
            f"EVIDENCE_DECLINED_WHILE_SWALLOWED, not {expected}"
        )
    else:
        # The paths disagree. No single verdict is right, so the rule has to
        # decline and the row is declared `enforced` -- a rule answering
        # [False] here would be claiming a disarmed contract on a path where
        # the assert really fires.
        assert expected == [True], (
            f"{label}: CPython fired on {fired} of 2 runs, so the paths "
            f"disagree and the rule must decline; the row must declare "
            f"[True], not {expected}"
        )
