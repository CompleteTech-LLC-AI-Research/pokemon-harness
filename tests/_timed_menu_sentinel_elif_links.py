"""Elif control-flow links and suppression cases.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_import_context_managers import (
    _assert_entry_contract,
)

#: #441. `ELIF_LINK_ARMS_SHAPES` above settles the *entry* question: every row
#: binds a `list()` or a `nullcontext()`, so what CPython does on entry
#: distinguishes "the header is enterable" from "the header raises before the
#: body". #441 needs the other question. Its header is perfectly enterable on
#: every call -- the damage was that the assert inside it was reported
#: *swallowed* when one of the two paths really does swallow it and the other
#: really fires.
#:
#: That needs a different oracle, and reusing `_assert_entry_contract` would
#: have been wrong rather than merely imprecise. It treats "returned normally"
#: as a *stale row*, on the grounds that a swallowed assert means the row no
#: longer describes anything. But swallowed is precisely the correct
#: ground truth for a defeated contract: a live call that raises
#: `AssertionError` and a swallowed call that returns cleanly are both real
#: outcomes, and the row's claim is about which of them can happen. So
#: `_assert_suppression_contract` below runs the fixture across the whole
#: argument domain and asks whether the assert fires on *any* call.
#:
#: The rows are executed, not asserted into the table, and each one is checked
#: against what CPython does rather than against the analyzer.
ELIF_LINK_SUPPRESSOR_SHAPES = (
    # The filed shape. The preamble's `cs = nullcontext()` is the binding in
    # force on the call that takes the `if` arm; the `elif` arm overwrites it
    # with a real suppressor, but only on the other call.
    #
    #     cs = contextlib.nullcontext()
    #     if x:
    #         pass
    #     elif True:
    #         cs = contextlib.suppress(AssertionError)
    #     with cs:
    #         assert x != 1
    #
    # At `x=1` the `if` arm runs, `elif` never does, `cs` is still the
    # `nullcontext`, and the assert **fires**. At `x=0` the `elif` runs and the
    # failure is swallowed. So the assert is not enforced on every call, and
    # the header is live.
    (
        "an elif link binding a suppressor over a plain manager is live",
        (
            "    if x:\n        pass\n    elif True:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        True,
    ),
    # The mirror control, and the one that decides whether the repair is
    # correct or merely cautious. When the binding in force on the call that
    # *skips* the `elif` arm is a suppressor too, then every call swallows the
    # assert and the header really is defeated. Answering `live` here would
    # certify a disarmed contract as load-bearing -- a false-LIVE, which stops
    # the mutation matrix from ever reporting a real defeat.
    #
    # This row cannot be written against the shared preamble, which always
    # binds a `nullcontext`; it rebinds `cs` in the first link instead.
    (
        "CONTROL an elif link binding a suppressor over a suppressor is dead",
        (
            "    cs = contextlib.suppress(AssertionError)\n"  # override preamble
            "    if x:\n        pass\n"
            "    elif True:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        False,
    ),
    # A suppressor that does not name `AssertionError` never disarms the
    # contract on either path, so no call swallows the failure and the header
    # is live for the ordinary reason. This is what keeps the new branch from
    # generalising into "an `elif` arm is always undecidable".
    (
        "CONTROL an elif link binding suppress(ValueError) stays live",
        ("    if x:\n        pass\n    elif True:\n        cs = contextlib.suppress(ValueError)"),
        True,
    ),
    # The `elif` link is the only shape that gets the new treatment. A
    # first-link `if` runs or does not, and when it runs it *does* replace the
    # preamble -- so this call is swallowed and the header is defeated, which
    # is what master already answers and what this row pins.
    (
        "CONTROL a first-link if binding a suppressor stays dead",
        "    if x:\n        cs = contextlib.suppress(AssertionError)",
        False,
    ),
    # #441. The two rows above and below are what pin *latest*, not *any*.
    #
    # Each writes two stores of the same name before the `elif`, one a
    # suppressor and one not, so "is any superseded store a suppressor?" and
    # "is the store in force on the skipped call a suppressor?" disagree.
    # A skipped call runs every store in order and ends on the last one, so the
    # answer is the last -- and the rows are ordered to make the two readings
    # give opposite verdicts.
    #
    # Here the suppressor is written FIRST and the `nullcontext` second:
    #
    #     cs = contextlib.suppress(AssertionError)   # x=0: superseded
    #     cs = contextlib.nullcontext()              # x=1: THIS is in force
    #     if x:
    #         pass
    #     elif True:
    #         cs = contextlib.suppress(AssertionError)
    #     with cs:
    #         assert x != 1
    #
    # At `x=1` the `if` arm is taken, the `elif` never runs, and the name holds
    # the `nullcontext` -- so the assert FIRES and the header is live. Asking
    # "did any superseded store suppress?" answers yes on the strength of the
    # first line and reports a disarmed contract, which is the same false-DEAD
    # #441 fixes, reached by a different route.
    (
        "an elif link over a nullcontext that supersedes an earlier suppressor",
        (
            "    cs = contextlib.suppress(AssertionError)\n"
            "    cs = contextlib.nullcontext()\n"
            "    if x:\n        pass\n"
            "    elif True:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        True,
    ),
    # The mirror, and the row that stops the fix generalising. Identical except
    # that the `nullcontext` is written first and the suppressor second, so the
    # *latest* prior store is a suppressor: on the call that skips the `elif`
    # the assert is swallowed there too, and every call that reaches the header
    # is defeated. The header really is dead.
    #
    # Without this row a repair that simply always answered "live" for an
    # `elif` arm would pass the row above and certify a disarmed contract as
    # load-bearing.
    (
        "CONTROL an elif link over a suppressor that supersedes an earlier nullcontext",
        (
            "    cs = contextlib.nullcontext()\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    if x:\n        pass\n"
            "    elif True:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        False,
    ),
)


def _assert_suppression_contract(label, source, assert_is_live):
    """Run the fixture over its whole domain and hold CPython to the row.

    This is the counterpart to :func:`_assert_entry_contract`, and the two ask
    different questions on purpose.

    ``_assert_entry_contract`` calls the fixture once and reads a single call's
    outcome, because the rows it guards differ in whether ``with cs:`` raises
    before the body. A row that returns normally is rejected as stale, which
    is right for that family: an enterable header that returns cleanly can
    only be a swallowed assert, and the family has nothing to say about it.

    The ``#441`` family is entirely about swallowed asserts, so that convention
    is inverted here. A defeat is a *legitimate* answer, and returning cleanly
    is the ground truth for it.

    So the fixture is swept over every value of its `x` parameter and the row
    is judged on whether the assert fires anywhere at all:

    * **live** -- some call raises `AssertionError`. The contract really is
      reachable, so the rule must report it enforced.
    * **dead** -- no call raises `AssertionError`; each either returns cleanly
      (swallowed) or raises something else before the body (unreachable
      entry). Either way the assert is not enforced on any call.

    Sweeping rather than sampling matters for the rows that are defeated: a
    single call that happens to return cleanly would prove nothing, because
    the *other* arm might still fire. The live row only needs one firing call,
    but taking the same measurement for both keeps the two verdicts
    commensurable and stops a row from passing on an argument value that no
    longer reaches the assert at all.
    """
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    outer = namespace["outer"]
    # A bare `except BaseException` is too wide to be worth stating: a
    # `KeyboardInterrupt` in a fixture would otherwise be recorded as "the row
    # is defeated" rather than failing the test. The tuple is the same one
    # `_assert_entry_contract` uses to mean "the header raised before the body".
    not_enterable = (TypeError, UnboundLocalError, NameError, AttributeError)
    fired = False
    outcomes = []
    for value in (0, 1):
        try:
            outer(value, True, None)
        except AssertionError:
            fired = True
            outcomes.append(f"x={value}: AssertionError")
        except not_enterable as exc:
            outcomes.append(f"x={value}: {type(exc).__name__}")
        else:
            outcomes.append(f"x={value}: returned")
    assert fired is assert_is_live, (
        f"{label}: CPython {'fired' if fired else 'never fired'} the assert across "
        f"the swept domain ({'; '.join(outcomes)}), which is "
        f"{'live' if assert_is_live else 'dead'}; the row claims the header is "
        f"{'live' if assert_is_live else 'defeated'}."
    )


@pytest.mark.parametrize(
    ("label", "chain", "assert_is_live"),
    ELIF_LINK_SUPPRESSOR_SHAPES,
    ids=[row[0] for row in ELIF_LINK_SUPPRESSOR_SHAPES],
)
def test_an_elif_link_suppressor_is_live_when_the_carried_store_is_not(
    label, chain, assert_is_live
):
    """A suppressor reached through an `elif` link does not defeat the assert alone.

    This is #441. #429's repair correctly demoted a store in an `elif` arm
    inside :func:`_stores_of`, but that is the table the *entry* rule reads.
    The suppression rule takes a different path: `_aliased_suppressions`
    resolves a ``with`` header against the raw binding table, where the `elif`
    arm was still recorded as an unconditional store. So the header resolved to
    the suppressor and the assert came back defeated on every call:

        cs = contextlib.nullcontext()
        if x:
            pass
        elif True:
            cs = contextlib.suppress(AssertionError)
        with cs:
            assert x != 1

    At ``x=1`` the ``if`` arm is taken, the ``elif`` never runs, ``cs`` is still
    the ``nullcontext``, and the assert **fires**. Reporting that as swallowed
    is a false-DEAD -- #308 criterion 1's damaging direction, where a contract
    that really enforces is certified as unreachable.

    The resolution is narrow. An `elif` arm is not a branch of its own: it runs
    only when *every* test above it failed, so the calls that skip it keep the
    earlier binding. The header is therefore defeated only when that earlier
    binding is itself a suppressor. When the arm's store swallows
    `AssertionError` and the store it supersedes provably does not, the name
    holds a suppressor on some calls and a plain manager on others, and the
    assert is not enforced on every call -- so the header stays live.

    Every row is executed across the fixture's argument domain by
    :func:`_assert_suppression_contract` before the analyzer's verdict is
    compared, so a row cannot claim "live" unless CPython agrees, nor "defeated"
    unless CPython really does swallow on every call.
    """
    source = (
        "import contextlib\n"
        "from contextlib import suppress, nullcontext\n"
        "def outer(x, flag, helper):\n"
        "    cs = contextlib.nullcontext()\n" + chain + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    _assert_suppression_contract(label, source, assert_is_live)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [assert_is_live], (
        f"{label}: expected verdicts [{assert_is_live}], got {results}. An "
        f"`elif` arm runs only on the calls where every test above it failed, "
        f"so the binding it supersedes still holds on the rest. The header is "
        f"defeated only when that earlier binding is a suppressor too."
    )


#: #452. `ELIF_LINK_SUPPRESSOR_SHAPES` above settles the `elif` form of the
#: question, and #441's repair deliberately stopped there: an `elif` is not a
#: branch of its own, whereas a terminal `else` is a branch in the ordinary
#: sense. That distinction is right about *coverage* -- an `if` body and its
#: `else` together account for every call -- but it is not right about
#: *liveness*, and reading only the arm that binds loses the call that skips
#: it:
#:
#:     cs = contextlib.nullcontext()
#:     if x:
#:         pass
#:     else:
#:         cs = contextlib.suppress(AssertionError)
#:     with cs:
#:         assert x != 1
#:
#: At `x=1` the `else` never runs, `cs` is still the `nullcontext`, and the
#: assert **fires**. The arm is where the suppressor lives, so reading the
#: arm alone answers "defeated" -- a false-DEAD on a contract that really
#: enforces.
#:
#: The rows below are executed across the fixture's argument domain by
#: :func:`_assert_suppression_contract` before the analyzer's verdict is
#: compared, so CPython -- not the table -- decides each row.
ELSE_ARM_SUPPRESSOR_SHAPES = (
    # The filed shape. `cs` is a plain `nullcontext` for every call that takes
    # the `if` arm, so the failure at `x=1` is swallowed by nothing and fires.
    (
        "an else arm binding a suppressor over a plain manager is live",
        ("    if x:\n        pass\n    else:\n        cs = contextlib.suppress(AssertionError)"),
        True,
    ),
    # The mirror control, and the one that decides whether the repair is
    # correct or merely cautious. When the binding in force on the call that
    # *skips* the `else` is a suppressor too, then every call swallows the
    # assert and the header really is defeated. Answering `live` here would
    # certify a disarmed contract as load-bearing.
    (
        "CONTROL an else arm binding a suppressor over a suppressor is dead",
        (
            "    cs = contextlib.suppress(AssertionError)\n"  # override preamble
            "    if x:\n        pass\n    else:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        False,
    ),
    # A suppressor that does not name `AssertionError` never disarms the
    # contract, so no call swallows the failure and the header is live for the
    # ordinary reason. This is what keeps the new branch from generalising into
    # "an `else` arm is always undecidable".
    (
        "CONTROL an else arm binding suppress(ValueError) stays live",
        "    if x:\n        pass\n    else:\n        cs = contextlib.suppress(ValueError)",
        True,
    ),
    # A first-link `if` runs or does not, and when it runs it *does* replace
    # the preamble -- so that call is swallowed and the header is defeated.
    # This is what master already answers, and it is the row that keeps the
    # repair scoped to the *trailing* arm.
    (
        "CONTROL a first-link if binding a suppressor stays dead",
        "    if x:\n        cs = contextlib.suppress(AssertionError)",
        False,
    ),
    # An `else` that also binds a plain manager does not restore the carried
    # one, and the `if` arm does not either, so the suppressor really is in
    # force on every call that reaches the header. This is the both-arms-bind
    # control: it is the row a repair that answered "an `else` arm is always
    # skipped" would break.
    (
        "CONTROL both arms binding a suppressor stay dead",
        (
            "    if x:\n        cs = contextlib.suppress(AssertionError)\n    else:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        False,
    ),
    # `if False:` never takes the `if` arm, so the `else` runs on every call
    # and the header really is defeated. This is the decided-chain control that
    # the witness walk must not rescue: the failure value cannot reach an arm
    # that skips the suppressor, because there is no such call.
    (
        "CONTROL a decided if False else arm binding a suppressor stays dead",
        (
            "    if False:\n        pass\n    else:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        False,
    ),
    # The mirror decided case. `if True:` always takes the `if` arm, so the
    # `else` never runs, `cs` stays the `nullcontext`, and the assert fires.
    # The `else` arm is unreachable here rather than skipped, and the header is
    # live for the ordinary reason -- no suppressor is ever installed at all.
    (
        "an unreachable else arm over a plain manager stays live",
        ("    if True:\n        pass\n    else:\n        cs = contextlib.suppress(AssertionError)"),
        True,
    ),
    # #452. `ELIF_LINK_SUPPRESSOR_SHAPES` has the two-store rows that pin
    # *latest* rather than *any*; these are the `else`-arm equivalents. The
    # suppressor is written FIRST and the `nullcontext` second, so on the call
    # that skips the `else` the name holds the `nullcontext` and the assert
    # fires.
    (
        "an else arm over a nullcontext that supersedes an earlier suppressor",
        (
            "    cs = contextlib.suppress(AssertionError)\n"
            "    cs = contextlib.nullcontext()\n"
            "    if x:\n        pass\n    else:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        True,
    ),
    # The mirror, and the row that stops the fix generalising: the
    # `nullcontext` is written first and the suppressor second, so the
    # *latest* prior store is a suppressor and every call that reaches the
    # header is defeated.
    (
        "CONTROL an else arm over a suppressor that supersedes an earlier nullcontext",
        (
            "    cs = contextlib.nullcontext()\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    if x:\n        pass\n    else:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        False,
    ),
    # #452, second form. The trailing `else` here is reached through an `elif`
    # link, so the #441 walk above cannot descend to it: that walk needs the
    # *failure value* to decide every link above the arm, and `assert x != 1`
    # says nothing about `flag`. The `else` does not need that -- it only has
    # to be skippable, meaning some call takes no arm at all and so keeps the
    # preamble's `nullcontext`.
    #
    #     cs = contextlib.nullcontext()
    #     if False:
    #         pass
    #     elif flag:
    #         pass
    #     else:
    #         cs = contextlib.suppress(AssertionError)
    #     with cs:
    #         assert x != 1
    #
    # At `x == 1, flag == True` the leading link is decided-false and `flag` is
    # true, so no arm runs and the assert FIRES. This row failed on the first
    # `else`-arm head (`4d7c5c4`) as well as on base.
    (
        "an else arm reached past a decided link and an unbound predicate is live",
        (
            "    if False:\n        pass\n    elif flag:\n        pass\n    else:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        True,
    ),
    # The control that decides whether the relaxation above is sound or merely
    # eager. The decided-true link here is an `elif`, and an `elif True` is
    # always taken, so the trailing `else` is *unreachable* rather than merely
    # skipped: the suppressor is never installed at all, the preamble's
    # `nullcontext` is what every call enters, and the assert fires at
    # `x == 1`. The analyzer reports live, which is the right answer -- and
    # crucially it is the answer it already gave before this repair, so the
    # row pins that reaching a decided-true link does not change anything.
    (
        "CONTROL a decided-true elif above makes the else unreachable and stays live",
        (
            "    if False:\n        pass\n    elif True:\n        pass\n    else:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        True,
    ),
    # The row that actually discriminates soundness from eagerness, and the
    # one that caught a real FALSE-LIVE while this was being written.
    #
    # Here the decided link is the *first* link, and it is decided-false, so
    # its body never runs and the chain always falls through to the `else`:
    # the suppressor is installed on every call that reaches the header, the
    # preamble's `nullcontext` is never entered, and the assert never fires.
    # The header is genuinely DEFEATED.
    #
    # A relaxation that treated "some link above is skippable" as sufficient
    # would answer `live` here and certify a disarmed contract as load-bearing.
    # The difference from the row two above is that this chain's `else` is
    # *always* taken, so there is no skipping call at all -- the walk has to
    # fall off the end of the chain rather than find a value that skips it.
    (
        "CONTROL a decided-false leading link means the else always runs and stays dead",
        (
            "    if False:\n        pass\n    else:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        False,
    ),
    # The second false-LIVE this relaxation produced while it was written, and
    # the row that pins the direction the walk has to travel. The link's test
    # is `local`, a name this scope pins to `0`:
    #
    #     cs = contextlib.nullcontext()
    #     local = 0
    #     if False:
    #         pass
    #     elif local:         # never true
    #         pass
    #     else:
    #         cs = contextlib.suppress(AssertionError)
    #     with cs:
    #         assert x != 1
    #
    # `local` is always falsy, so both arms above the `else` are dead and the
    # `else` runs on every call: the suppressor swallows the assert and the
    # header is genuinely DEFEATED. Treating "some link above may be true" as
    # sufficient answered `live` here.
    (
        "CONTROL a link pinned to a falsy constant keeps the else always running and dead",
        (
            "    local = 0\n"
            "    if False:\n        pass\n    elif local:\n        pass\n    else:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        False,
    ),
    # The mirror of the filed row, and the one that shows the walk is looking
    # for a *link that can be true* rather than for the failure value. Here the
    # unconstrained name is supplied by the caller, so a truthy call skips the
    # `else` and the assert fires.
    (
        "an else arm skipped by a caller-supplied truthy link is live",
        (
            "    if False:\n        pass\n    elif flag:\n        pass\n    else:\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "chain", "assert_is_live"),
    ELSE_ARM_SUPPRESSOR_SHAPES,
    ids=[row[0] for row in ELSE_ARM_SUPPRESSOR_SHAPES],
)
def test_an_else_arm_suppressor_is_live_when_the_carried_store_is_not(label, chain, assert_is_live):
    """A suppressor reached through a trailing `else` does not defeat the assert alone.

    This is #452. `test_an_elif_link_suppressor_is_live_when_the_carried_store_is_not`
    settled the `elif` form, and #441's repair stopped at it deliberately -- its
    `_store_is_in_an_elif_link` records in its own docstring that "an `else`
    arm ... fail[s] it, which is what keeps them on their existing answers",
    because an `else` and its `if` body are complementary and so together cover
    every call.

    That reasoning is sound for *coverage* and wrong for *liveness*, and the
    difference is the whole of this issue. An `else` arm and its `if` body do
    account for every call, but the calls that reach the `with` through the
    `if` body never install the suppressor at all: they keep whatever the
    preamble bound. So "every call is covered by some arm" does not imply
    "every call is covered by the suppressor", and reading only the arm that
    binds turns a live contract into a false-DEAD.

    The repair is the same proof #441 uses, aimed at the trailing arm: when the
    arm's store swallows `AssertionError` and the store in force on the calls
    that skip it provably does not, the name holds a suppressor on some calls
    and a plain manager on others, and the header is not defeated. The
    `CONTROL` rows are what keep it from over-reaching: a decided `if False:`
    whose `else` always runs, an `if` body that itself binds a suppressor, and
    a both-arms-bind chain all still report DEFEATED.

    Every row is executed across the fixture's argument domain by
    :func:`_assert_suppression_contract` before the analyzer's verdict is
    compared, so a row cannot claim "live" unless CPython agrees, nor "defeated"
    unless CPython really does swallow on every call.
    """
    source = (
        "import contextlib\n"
        "from contextlib import suppress, nullcontext\n"
        "def outer(x, flag, helper):\n"
        "    cs = contextlib.nullcontext()\n" + chain + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    _assert_suppression_contract(label, source, assert_is_live)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [assert_is_live], (
        f"{label}: expected verdicts [{assert_is_live}], got {results}. An "
        f"`else` arm runs only on the calls where every test above it failed, "
        f"so the binding it supersedes still holds on the rest. The header is "
        f"defeated only when that earlier binding is a suppressor too."
    )


#: #435. #429 made an `elif` link's *own* literal-true test stop implying an
#: unconditional store, but it only looked one link deep. A store nested under
#: a further `if True:` -- or under a third `elif True:` -- is reached through
#: the same conditionally-taken outer link, and the single-parent read missed
#: it. The store was then settled as unconditional, the earlier
#: `nullcontext()` was dropped, and a header CPython enters was reported DEAD:
#: a damaging false-DEAD that survived every #429 regression because each of
#: those nests the store exactly one link deep, where the one-level walk was
#: already right.
#:
#: These rows are the deeper shapes. Every one is executed by
#: ``_assert_entry_contract`` at ``x=1``, so the interpreter -- not the
#: analyzer's opinion -- decides whether each row's `with cs:` is enterable and
#: whether the assert fires.
NESTED_ELIF_LINK_SHAPES = (
    # The filed false-DEAD. `cs = list()` sits under a nested `if True:` inside
    # an `elif True:` link. With `x=1` the outer `if x:` arm runs, the elif
    # body never does, `cs` is still the `nullcontext()`, `with cs:` enters and
    # the assert FIRES. The nested store must NOT settle the name.
    (
        "a nested if True: inside an elif True: link",
        "    if x:\n        pass\n    elif True:\n        if True:\n            cs = list()",
        True,
    ),
    # One level deeper again, with a `pass` in the middle link. The deepest
    # `elif True:` store is still only reached when the outer `if x:` is false.
    # This row is already read correctly by #429 (it is reached through the
    # middle link's non-always-true parent), so it is a control that the
    # chain-walk must not break rather than a fresh regression.
    (
        "a store under a third elif True: link",
        "    if x:\n        pass\n    elif True:\n        pass\n    elif True:\n        cs = list()",
        True,
    ),
    # CONTROL: a first-link `if True:` is genuinely unconditional, even with a
    # nested `if True:` inside it. The nested `if True:` inherits an
    # always-true *first* link, so the whole chain always runs.
    (
        "CONTROL a nested if True: inside a first-link if True:",
        "    if True:\n        if True:\n            cs = list()",
        False,
    ),
    # CONTROL: a plain first-link `if True:` (the row #429 exists to serve)
    # still settles the name.
    (
        "CONTROL a first-link if True: is still unconditional",
        "    if True:\n        cs = list()",
        False,
    ),
    # CONTROL: an `elif` link that binds a real context manager rather than a
    # `list` is a live header on both paths; the verdict stays True.
    (
        "CONTROL an elif link binding a real context manager",
        "    if x:\n        pass\n    elif True:\n        cs = nullcontext()",
        True,
    ),
    # CONTROL: a nested `if True:` inside a *conditional* (non-elif) `if x:`
    # body. At `x=1` the `if x:` arm runs and binds the `list`, the `else` never
    # does, and `with cs:` raises `TypeError` before the body -- so this is
    # genuinely DEAD, and the row must keep the analyzer's `False`.
    (
        "CONTROL a nested if True: inside a conditional if body",
        "    if x:\n        if True:\n            cs = list()\n    else:\n        cs = nullcontext()",
        False,
    ),
    # The same gap reached without any `elif` at all. `if not x:` is a
    # conditional test -- it is false whenever `x` is true -- so the arm
    # holding the literal-true block is entered on some calls and skipped on
    # others. At `x=1` it is skipped, `cs` is still the `nullcontext`, and the
    # assert FIRES, so the store must not settle the name. This row is a second
    # false-DEAD of the same kind as the filed one, and it is the shape that
    # pins the general rule rather than the `elif` special case.
    (
        "a nested if True: under a negated conditional test",
        "    if not x:\n        if True:\n            cs = list()",
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "chain", "second_assert_live"),
    NESTED_ELIF_LINK_SHAPES,
    ids=[row[0] for row in NESTED_ELIF_LINK_SHAPES],
)
def test_a_nested_link_inside_a_literal_true_elif_stays_conditional(
    label, chain, second_assert_live
):
    """A literal-true ``elif`` link does not make a *nested* store unconditional.

    This is #435. #429's rule already stops a store directly inside an ``elif
    True:`` link from settling a name, because the link is entered only when
    every test above it failed. It did not carry that reasoning one level
    deeper: a store under a further ``if True:`` -- or a third ``elif True:``
    -- is reached through the same conditionally-taken outer link, but the
    one-level read saw only the immediately-enclosing literal-true block and
    settled the name.

    Executed on CPython 3.12.14 with ``x=1``, the filed false-DEAD row
    (``elif True:`` containing ``if True: cs = list()``) takes the outer
    ``if x:`` arm, leaves ``cs`` as the ``nullcontext``, enters ``with cs:``
    and **fires** the assert -- so ground truth is live and the analyzer must
    not report DEAD. The controls pin the two sides the repair must not
    over-reach into: a *first*-link ``if True:`` (with or without a nested
    ``if True:``) really does run every time and stays unconditional, and a
    real context manager stays a live header.

    The repair is two-part. Reachability of a nested link depends on the whole
    ``if``/``orelse`` chain, not just the immediate parent; and a
    conditionally-reached ``elif`` link is neither always-true nor never-runs,
    so it must fail the "runs every time" gate outright rather than falling
    through the never-runs fallback.
    """
    source = (
        "import contextlib\n"
        "from contextlib import suppress, nullcontext\n"
        "def outer(x, flag, helper):\n"
        "    cs = contextlib.nullcontext()\n" + chain + "\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, second_assert_live)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [second_assert_live], (
        f"{label}: expected verdicts [{second_assert_live}], got {results}. A "
        f"store under an `elif` link is reached only when every test above it "
        f"failed, however deeply nested it is, so it settles the name on some "
        f"calls only. A first-link `if True:` still runs every time."
    )
