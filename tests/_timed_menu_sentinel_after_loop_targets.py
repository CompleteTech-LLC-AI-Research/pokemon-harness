"""After-loop target and stale-local behavior.

Actual test functions and literal cases from the original collector.
"""

import ast
import asyncio

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_tied_store_shapes import (
    TIED_STORE_ROWS,
)

#: #385 after-loop rows, kept in their own list because they are also executed
#: against CPython. The tie rows above deliberately mix vacuous probes and
#: fixtures whose entry raises before the assert runs, so executing the whole
#: table would report disagreement for shapes these rows never claim anything
#: about.
#
# The in-body rows above decline a multi-element loop for a header read INSIDE
# the body, where the target holds a different value on each iteration and no
# one element answers for all of them. A header read AFTER the loop is the
# opposite question and the same refusal was wrong for it: once the last
# iteration is done the target holds the final element, and that is the same
# answer on every path -- the property the in-body case lacks.
#
#     for cs in (contextlib.nullcontext(),
#                contextlib.suppress(AssertionError)):
#         pass
#     with cs:                 # `cs` is the LAST element, the suppressor
#         assert x == 99       # executed: swallowed
#
# Executed, that assert never fires. Reported `enforced`, it certifies a
# disarmed contract as load-bearing -- the damaging direction per #308
# criterion 1. The two questions are therefore separated by POSITION rather
# than averaged: `_single_loop_element` still answers the in-body case and
# still refuses, so the CONTROL above is untouched by this repair.
#
# Every row here uses `assert x == 99` with `x` bound to 1. `assert x != 1`
# would be vacuous -- it is true, so it passes whether or not the context
# swallows it -- and would pin nothing about the analyzer.
AFTER_LOOP_ROWS = (
    (
        "#385 a multi-element loop target is read as its last element after it",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    with cs:\n"
            "        assert x == 99"
        ),
        [False],
    ),
    # The mirror image, and the damaging one. The last element here is a
    # `nullcontext`, so the assert really does fire. Reading the FIRST
    # element instead -- the only other candidate -- finds the suppressor
    # and reports the assert defeated, deleting a live contract. This row
    # is what says "last", not "some".
    (
        "CONTROL a multi-element after-loop read follows the LAST element",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.nullcontext()):\n"
            "        pass\n"
            "    with cs:\n"
            "        assert x == 99"
        ),
        [True],
    ),
    # Three elements, so the answer is not a first-or-last coin flip
    # between two. The suppressor is in the middle: a rule that read the
    # first or the last element would get this wrong, and one that read an
    # arbitrary index would be right only by accident.
    (
        "CONTROL a three-element after-loop read still follows the last",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.nullcontext(),\n"
            "               contextlib.nullcontext()):\n"
            "        pass\n"
            "    with cs:\n"
            "        assert x == 99"
        ),
        [True],
    ),
    # The same after-loop answer reached from inside a block, so the
    # ordering test is not merely "the loop is a top-level statement before
    # the header". Loop and header are siblings of the `if` body here,
    # which is the case a top-level index cannot distinguish from "the
    # header is inside the loop".
    (
        "#385 an after-loop read works from inside a block",
        (
            "    if flag:\n"
            "        for cs in (contextlib.nullcontext(),\n"
            "                   contextlib.suppress(AssertionError)):\n"
            "            pass\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [False],
    ),
    # `continue` still leaves the target on the FINAL element, so the answer
    # stands. It is paired with the `break` CONTROL below to say the
    # exclusion is `break` specifically and not "any jump in the body".
    (
        "CONTROL a continue in the loop does not block the after-loop read",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        if flag:\n"
            "            continue\n"
            "    with cs:\n"
            "        assert x == 99"
        ),
        [False],
    ),
    # `break` leaves the target on whatever element was CURRENT, not the
    # last. Executed, the two values of `flag` disagree -- swallowed when
    # the loop runs to the end, live when it breaks on the first pass --
    # so the loop is declined and the header stays `enforced`, the safe
    # direction. Reading the last element here would report the
    # `flag=True` path as swallowed and drop a contract that really fires.
    (
        "CONTROL a break in the loop blocks the after-loop read",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        if flag:\n"
            "            break\n"
            "    with cs:\n"
            "        assert x == 99"
        ),
        [True],
    ),
    # The body runs AFTER the target on every pass, so a rebound name is
    # whatever the body's own store left -- the iterable's last element
    # says nothing about it. Executed, this is the `nullcontext` the body
    # stored, so the assert fires. Reading the loop's final element instead
    # reports it swallowed and deletes a live contract.
    (
        "CONTROL a loop body that rebinds the name beats the iterable",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        cs = contextlib.nullcontext()\n"
            "    with cs:\n"
            "        assert x == 99"
        ),
        [True],
    ),
    # A store between the loop and the header is the last write, and it
    # supersedes the loop exactly as an ordinary supersession does. This is
    # the row that keeps the new path from resurrecting a stale
    # suppressor: the loop is unrecorded in the raw table, so a rule that
    # asked "is this the last recorded store of the name?" would read the
    # loop as final.
    (
        "CONTROL a store between the loop and the header supersedes it",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    cs = contextlib.nullcontext()\n"
            "    with cs:\n"
            "        assert x == 99"
        ),
        [True],
    ),
    # The same supersession when the store is *conditional*, which is the case
    # a plain order-key comparison cannot see: both the loop and the store are
    # top-level statements, but the store is inside an `if`, so the two paths
    # through the header carry different values.
    #
    #     for cs in (nullcontext(), suppress(AssertionError)):
    #         pass
    #     if flag:
    #         cs = nullcontext()
    #     with cs:                 # nullcontext when flag, suppressor otherwise
    #         assert x == 99
    #
    # Executed, the assert FIRES when `flag` is true and is swallowed when it
    # is false, so no single verdict is right and the loop is declined. Reading
    # the loop's last element anyway answers `enforced` and is a false LIVE on
    # the `flag=True` path -- the damaging direction. Declining is the safe
    # side, and it is the same call `_resolve_bindings` makes for an ambiguous
    # conditional store.
    (
        "CONTROL a conditional store between loop and header blocks the read",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    if flag:\n"
            "        cs = contextlib.nullcontext()\n"
            "    with cs:\n"
            "        assert x == 99"
        ),
        [True],
    ),
    # A loop written AFTER the header has not run when the header is read,
    # so the name is unbound there and CPython raises `UnboundLocalError`.
    # This is the ordering guard: a rule that accepted "some loop in this
    # function binds this name" rather than "this loop precedes this
    # header" would report this as swallowed and drop a loud failure.
    (
        "CONTROL a loop written after the header does not bind it",
        (
            "    with cs:\n"
            "        assert x == 99\n"
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        pass"
        ),
        [True],
    ),
    # An empty literal binds nothing at all, so the header raises
    # `UnboundLocalError` on every path rather than entering a context. The
    # after-loop answer must not read a "last element" out of nothing.
    (
        "CONTROL an empty literal binds nothing for a later header",
        ("    for cs in ():\n        pass\n    with cs:\n        assert x == 99"),
        [True],
    ),
    # A non-literal iterable: which element survives is a property of the
    # object, not of the syntax. Declined, so the header stays `enforced`.
    # Executed, the literal list yielded here ends in a string, so entry
    # raises `TypeError` before the assert -- a loud failure, which is why
    # the safe answer and the executed truth agree on `enforced`.
    (
        "CONTROL a non-literal iterable gives no after-loop element",
        ("    for cs in items:\n        pass\n    with cs:\n        assert x == 99"),
        [True],
    ),
    # A carried binding the loop never touches must survive it. This is the
    # additive row for the new path, mirroring the in-body one: returning
    # the loop's bindings in place of what the enclosing blocks carried
    # would drop `base` and report this assert live.
    (
        "CONTROL an after-loop read leaves a carried binding alone",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    for other in [0]:\n"
            "        pass\n"
            "    with base:\n"
            "        assert x == 99"
        ),
        [False],
    ),
    # An `else` arm runs *after* the iterable is exhausted -- that is the only
    # way to reach it without a `break` -- so the target holds the final
    # element there for exactly the reason a header after the loop does. It
    # was grouped with the loop's `body` and answered as the in-body question,
    # which declines the multi-element case and leaves this header `enforced`.
    # Executed, the assert is swallowed:
    #
    #     for cs in (contextlib.nullcontext(),
    #                contextlib.suppress(AssertionError)):
    #         pass
    #     else:
    #         with cs:              # `cs` IS the last element
    #             assert x == 99    # never fires
    #
    # Reported `enforced` that certifies a disarmed contract as load-bearing.
    (
        "#385 an else arm reads the last element like any after-loop header",
        (
            "    for cs in (contextlib.nullcontext(),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    else:\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [False],
    ),
    # The mirror, and the reason the arm cannot simply be excluded again: the
    # last element is a `nullcontext`, so the assert really does fire and the
    # arm must read it as live.
    (
        "CONTROL an else arm follows the LAST element too",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.nullcontext()):\n"
            "        pass\n"
            "    else:\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
    # A store written in the arm BEFORE the header is the latest write and
    # supersedes the loop's target. This is the damaging direction: layering
    # the loop's element over the arm's store reports the assert defeated and
    # drops a contract that really fires.
    #
    # The order key cannot catch it. `_binding_order` maps every store inside
    # one top-level statement to the same index and documents that equal keys
    # carry no ordering, so the loop and the arm's store compare *equal* and a
    # strict `<` never fires. Only a positional check over the block that
    # sequences the two separates them.
    (
        "CONTROL a store in the else arm supersedes the loop's last element",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    else:\n"
            "        cs = contextlib.nullcontext()\n"
            "        with cs:\n"
            "            assert x == 99"
        ),
        [True],
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    AFTER_LOOP_ROWS,
    ids=[row[0] for row in AFTER_LOOP_ROWS],
)
def test_a_loop_target_read_after_its_loop_is_answered_from_the_last_element(label, body, expected):
    """#385: a header *after* a completed loop reads that loop's last element.

    The in-body question is the opposite one, and its answer is already pinned
    above: inside the body the target holds a different value on each
    iteration, so a multi-element loop is declined and the header stays
    ``enforced``. After the loop has run to exhaustion the target holds the
    final element, which is one answer on every path -- so the two questions
    are separated by position rather than averaged.

    The guards around that answer are the substance of the repair, and each has
    a CONTROL row: the loop must precede the header, must be able to run to
    exhaustion (no ``break``), must not have its name rebound by the body, and
    must not be superseded by a store written between the loop and the header.
    Any of those failing has to leave the verdict alone, because each one is a
    case where "the last element" is not the answer.
    """
    source = "def outer(x, flag, helper, items):\n    import contextlib\n" + body + "\n"
    tree = ast.parse(source)
    outer = tree.body[0]
    asserts = [node for node in ast.walk(outer) if isinstance(node, ast.Assert)]
    assert len(asserts) == len(expected), (
        f"{label}: fixture declared {len(asserts)} asserts but the row "
        f"expects {len(expected)} verdicts"
    )
    results = [_is_enforced(outer, node, tree) for node in asserts]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A loop target "
        f"read after its loop is its LAST element, and every guard around that "
        f"claim has to be honoured."
    )


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    AFTER_LOOP_ROWS,
    ids=[row[0] for row in AFTER_LOOP_ROWS],
)
def test_every_after_loop_row_matches_what_cpython_actually_does(label, body, expected):
    """The after-loop verdicts are settled by execution, not by assertion.

    The whole repair turns on a claim that is easy to state and easy to get
    backwards: after a loop runs to exhaustion the target holds the *last*
    element. Getting that backwards does not merely flip a table entry, it
    deletes a live contract -- so every row is executed here and its declared
    verdict compared against what CPython actually did.

    Each body runs twice, with ``flag`` true and false, because a guarded row
    is only decidable when both paths agree. Where they do not -- the ``break``
    row, where CPython swallows the assert on one path and lets it fire on the
    other -- no single verdict is right, so the rule declines and the row is
    declared ``enforced``. That is checked as "not swallowed on every path":
    a rule that answered ``False`` there would be claiming a disarmed contract
    on a path where the assert really fires.

    ``x`` is bound to 1 and the probe is ``assert x == 99``, so it fails unless
    something suppresses it. ``assert x != 1`` would be vacuous: it is true,
    so it passes whether or not the context swallows it.
    """
    source = "def outer(x, flag, helper, items):\n    import contextlib\n" + body + "\n"
    fired = []
    raised_on_entry = False
    for flag in (True, False):
        namespace = {}
        exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
        try:
            namespace["outer"](1, flag, None, ["a", "b", "c"])
        except AssertionError:
            fired.append(flag)
        except (TypeError, UnboundLocalError, NameError):
            # Entry itself raises, so the assert is unreachable rather than
            # swallowed -- a loud failure, not a defeat.
            raised_on_entry = True
    if raised_on_entry or len(fired) == 2:
        # Either the assert fired on every path, or the header could not be
        # entered at all. Both are live contracts, so the row must say so.
        assert expected == [True], (
            f"{label}: CPython fired the assert on every reachable path, so the "
            f"row must declare [True], not {expected}"
        )
    elif not fired:
        assert expected == [False], (
            f"{label}: CPython never let the assert fire, so the row must "
            f"declare [False], not {expected}"
        )
    else:
        # The paths disagree -- `break` is the only shape here that does this.
        assert expected == [True], (
            f"{label}: CPython fired on {fired} but not on the other path, so "
            f"the rule must decline and the row must declare [True], not "
            f"{expected}"
        )


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    TIED_STORE_ROWS,
    ids=[row[0] for row in TIED_STORE_ROWS],
)
def test_two_stores_sharing_one_statement_resolve_without_walk_order(label, body, expected):
    """A binding tie is declined, never broken by where the walk reached.

    #367. `_binding_order` keys a store by the top-level statement containing
    it, so two stores inside one statement compare equal *by construction*.
    Reading that equality as "the first one wins" answers a question the
    source does not pose: which of two stores in the same block ran last is
    decided by control flow, not by the order `ast.walk` happened to visit
    them.

    The rule here is therefore split, and the split is the whole repair:

    * every tied member conditional -- none of them provably ran, so the name
      is declined and reported as a defeat (#308 criterion 1, the safe side);
    * a mixed tie -- an unconditional store and a conditional one share the
      block, the unconditional one ran on every path, and the conditional one
      is the later *write*, so the value read afterwards is the conditional
      one's. That is #323's supersession, not an ambiguity, and treating it as
      one would drop the `CONTROL` row's live assert.

    A mixed tie can still retire the name entirely, when the later write is
    something that cannot be entered. The last row is that case: an
    `import ... as cs` in the same block makes the following header raise
    `TypeError`, so its assert is unreachable. The carrier is recorded as a
    runtime kind and the dead-entry rule reports it, which is what keeps the
    row from certifying a `TypeError`-raising header as load-bearing.
    """
    source = (
        "def outer(x, flag, helper, items):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n" + body + "\n"
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
        f"{label}: expected verdicts {expected}, got {results}. A tie must be "
        f"declined, and a mixed tie must resolve to the later write."
    )


def _async_loop_target_is_undecidable(label, source):
    """Pin why a loop target is excluded from the dead-entry rule.

    ``async for cs in <iterable>:`` binds the loop variable, so whether the
    later ``with cs:`` can be entered is decided entirely by the *element
    type* of the iterable -- something the analyzer cannot read without
    running the loop. Both outcomes are reachable from the same syntax:

    * an iterable of ``nullcontext()`` leaves an enterable value, so the
      assert is **live** and reporting it as dead would drop a real contract;
    * an iterable of ``int`` leaves an ``int``, so entering raises
      ``TypeError`` and the assert is unreachable.

    The row above uses the enterable iterable, and the table declares that
    verdict ``True``. This helper proves the exclusion is necessary rather
    than merely convenient: the same rebind spelling, differing only in what
    the loop yields, is live in one case and unreachable in the other. A rule
    that answered either way for both would be wrong on one of them.
    """
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False, True], (
        f"{label}: expected verdicts [False, True], got {results}. The rule "
        f"must decline a loop target rather than read a type it cannot know."
    )
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    try:
        asyncio.run(namespace["outer"](1, True, None))
    except AssertionError:
        pass  # the declared live row: the body ran and the assert fired
    else:
        raise AssertionError(f"{label}: the enterable-iterable row did not run its assert.")
    # The same spelling over a non-manager iterable is the other half of the
    # proof: it must be unreachable, or the rule would have had a decidable
    # answer available and declined for no reason.
    hostile = source.replace("yield nullcontext()", "yield 1").replace(
        "async for cs in agen():", "async for cs in gen():"
    )
    assert hostile != source, f"{label}: could not build the hostile variant"
    namespace = {}
    exec(compile(hostile, f"<{label}-hostile>", "exec"), namespace)  # noqa: S102
    try:
        asyncio.run(namespace["outer"](1, True, None))
    except AssertionError:
        raise AssertionError(
            f"{label}: the hostile variant reached its assert, so a loop "
            f"target is not actually undecidable and the rule should model it."
        ) from None
    except (TypeError, UnboundLocalError, NameError):
        return
    raise AssertionError(f"{label}: the hostile variant returned normally; the fixture is stale.")
