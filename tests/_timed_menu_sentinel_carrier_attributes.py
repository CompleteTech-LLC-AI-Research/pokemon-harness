"""Carrier attributes and context-manager aliases.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_import_context_managers import (
    _assert_entry_contract,
)

#: #422. A ``with`` header that reaches the suppressor *through* a container
#: or a class attribute enters a real ``contextlib.suppress(AssertionError)``
#: while naming no call at all. Executed, the assert is swallowed; the
#: analyzer certified it ``enforced``, which is a disarmed contract reported
#: load-bearing -- the damaging direction #308 criterion 1 names.
#:
#: Each row is executed before it is judged, so the table cannot drift away
#: from CPython: the swallow is *measured*, not asserted, and the analyzer is
#: required to agree with the measurement. The rows carrying a value that does
#: **not** swallow must stay live -- they are what stops a blanket "a
#: subscript or attribute is defeated" rule, which would drop real contracts.
SUBSCRIPT_ATTRIBUTE_SUPPRESSOR_ROWS = (
    (
        "422 a suppressor reached through a list subscript",
        False,
        "",
        (
            "    holder = [contextlib.suppress(AssertionError)]\n"
            "    with holder[0]:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 a suppressor reached through a tuple subscript",
        False,
        "",
        (
            "    holder = (contextlib.suppress(AssertionError),)\n"
            "    with holder[0]:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 a suppressor reached through a dict subscript",
        False,
        "",
        (
            "    holder = {'k': contextlib.suppress(AssertionError)}\n"
            "    with holder['k']:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 a suppressor reached through a class attribute",
        False,
        "class Box:\n    ctx = contextlib.suppress(AssertionError)\n",
        "    with Box.ctx:\n        assert x != 1\n",
        True,
    ),
    # The attribute may be attached from outside the class body, and from the
    # enclosing function. Both are the same readable store one level out.
    (
        "422 a suppressor assigned onto the class after its body",
        False,
        "class Late:\n    pass\nLate.ctx = contextlib.suppress(AssertionError)\n",
        "    with Late.ctx:\n        assert x != 1\n",
        True,
    ),
    (
        "422 a suppressor assigned onto the class inside the function",
        False,
        "class Local:\n    pass\n",
        (
            "    Local.ctx = contextlib.suppress(AssertionError)\n"
            "    with Local.ctx:\n        assert x != 1\n"
        ),
        True,
    ),
    # Chains. The filed shapes are one dereference deep, but nothing about the
    # defect stops there: an *unfollowed step* is the same false-LIVE one
    # level out, so each chain is pinned to the value its final step selects.
    (
        "422 a suppressor reached through a nested list subscript",
        False,
        "",
        (
            "    holder = [[contextlib.suppress(AssertionError)]]\n"
            "    with holder[0][0]:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 a suppressor reached through a nested dict subscript",
        False,
        "",
        (
            "    holder = {'a': {'b': contextlib.suppress(AssertionError)}}\n"
            "    with holder['a']['b']:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 a suppressor reached through a negative index",
        False,
        "",
        (
            "    holder = [1, contextlib.suppress(AssertionError)]\n"
            "    with holder[-1]:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 a suppressor reached through a subscript of a class attribute",
        False,
        "",
        (
            "    Box = type('H', (), {'b': [contextlib.suppress(AssertionError)]})\n"
            "    with Box.b[0]:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 a suppressor reached through a nested type-built class",
        False,
        "",
        (
            "    Box = type('H', (), "
            "{'b': type('I', (), {'c': contextlib.suppress(AssertionError)})})\n"
            "    with Box.b.c:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 a suppressor assigned onto a class attribute with an annotation",
        False,
        "class Ann:\n    ctx: object = contextlib.suppress(AssertionError)\n",
        "    with Ann.ctx:\n        assert x != 1\n",
        True,
    ),
    # --- the boundary: the same shapes carrying a value that is not a
    # --- suppression. Reading any subscript as a defeat would drop these.
    (
        "422 a nullcontext reached through a subscript stays live",
        False,
        "",
        ("    holder = [contextlib.nullcontext()]\n    with holder[0]:\n        assert x != 1\n"),
        False,
    ),
    (
        "422 a wrong-exception suppressor reached through a subscript stays live",
        False,
        "",
        (
            "    holder = [contextlib.suppress(ValueError)]\n"
            "    with holder[0]:\n        assert x != 1\n"
        ),
        False,
    ),
    (
        "422 a nullcontext reached through a class attribute stays live",
        False,
        "class Dead:\n    ctx = contextlib.nullcontext()\n",
        "    with Dead.ctx:\n        assert x != 1\n",
        False,
    ),
    (
        "422 a plain value reached through a chained subscript stays live",
        False,
        "",
        (
            "    Box = type('H', (), {'b': [contextlib.nullcontext()]})\n"
            "    with Box.b[0]:\n        assert x != 1\n"
        ),
        False,
    ),
    # --- declines: unreadable, so the header raises or does not suppress and
    # --- the assert stays live.
    (
        "422 a computed subscript key stays live",
        True,
        "",
        (
            "    holder = {'k': contextlib.suppress(AssertionError)}\n"
            "    i = 0\n"
            "    with holder[i]:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 a factory-built container stays live",
        True,
        "",
        ("    holder = make_holders()\n    with holder[0]:\n        assert x != 1\n"),
        True,
    ),
    (
        "422 a dict key that is absent stays live",
        True,
        "",
        (
            "    holder = {'j': contextlib.suppress(AssertionError)}\n"
            "    with holder['k']:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 an out-of-range index stays live",
        True,
        "",
        (
            "    holder = [contextlib.suppress(AssertionError)]\n"
            "    with holder[5]:\n        assert x != 1\n"
        ),
        True,
    ),
    (
        "422 an attribute of a name that is not a local class stays live",
        True,
        "",
        "    with box.ctx:\n        assert x != 1\n",
        True,
    ),
    (
        "422 a set literal is not indexable and stays live",
        True,
        "",
        (
            "    holder = {contextlib.suppress(AssertionError)}\n"
            "    with holder[0]:\n        assert x != 1\n"
        ),
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "unenterable", "preamble", "body", "suppressed"),
    SUBSCRIPT_ATTRIBUTE_SUPPRESSOR_ROWS,
    ids=[row[0] for row in SUBSCRIPT_ATTRIBUTE_SUPPRESSOR_ROWS],
)
def test_a_suppressor_reached_through_a_subscript_or_attribute_is_read(
    label, unenterable, preamble, body, suppressed
):
    """#422: the header enters a value; read the value, not its spelling.

    Two things are pinned per row, in this order, and the order matters: the
    fixture is **executed** first so the expected verdict is a measurement,
    and only then is the analyzer required to match it. A row whose fixture
    stopped swallowing would fail on the execution half rather than silently
    teaching the analyzer a stale answer.

    ``unenterable`` marks the rows whose header raises before the assert -- a
    computed key, a factory container, an absent key, an out-of-range index,
    an attribute of a name that is not a local class, an unindexable set. For
    those the body never runs, so there is no assert to judge, and the row
    asserts only that the analyzer does not claim a defeat it cannot justify.
    That is the safe error: reporting them ``enforced`` costs nothing,
    whereas resolving them would mean guessing a value.
    """
    source = "import contextlib\n" + preamble + "def outer(x, flag):\n" + body
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"

    if unenterable:
        namespace = {}
        exec(compile(source, "<422-unenterable>", "exec"), namespace)  # noqa: S102
        raised = None
        try:
            namespace["outer"](1, True)
        except BaseException as error:  # noqa: BLE001 - the point is which one
            raised = type(error).__name__
        assert raised is not None, (
            f"{label}: this row is filed as an unenterable header, but the "
            f"fixture ran to completion"
        )
        results = [_is_enforced(function, node, tree) for node in asserts]
        assert results == [True], (
            f"{label}: an unreadable subscript must leave the assert enforced, got {results}"
        )
        return

    namespace = {}
    exec(compile(source, "<422-executed>", "exec"), namespace)  # noqa: S102
    fired = False
    try:
        namespace["outer"](1, True)
    except AssertionError:
        fired = True
    measured_swallowed = not fired
    assert measured_swallowed is suppressed, (
        f"{label}: CPython disagrees with this row -- the assert "
        f"{'fired' if fired else 'was swallowed'}"
    )

    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [not suppressed], (
        f"{label}: expected the assert to be "
        f"{'unenforced' if suppressed else 'enforced'}, got {results}"
    )


def test_an_unenterable_nested_container_is_not_read_as_a_defeat():
    """#422 over-reach guard: every step is followed, none is invented.

    ``holder[0]`` here is a *list*, so entering it raises ``TypeError`` before
    the assert runs. A rule that followed the chain far enough to reach the
    nested suppressor would report a defeat for a body that never executed.

    This is the counterpart to the chained rows in the table above, which pin
    ``holder[0][0]`` as a defeat: the chain is walked to its end in both
    cases, and what each step *lands on* decides the verdict. One step deeper
    than the runtime enters is the over-reach, so the table and this test are
    pinned together.
    """
    source = (
        "import contextlib\n"
        "def outer(x, flag):\n"
        "    holder = [[contextlib.suppress(AssertionError)]]\n"
        "    with holder[0]:\n        assert x != 1\n"
    )
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"a container of a container raises TypeError on entry, so the header "
        f"is not a defeat -- got {results}"
    )


#: Previously measured #422 residuals, now covered by scoped source proofs.
SUPPRESSOR_REACHED_THROUGH_AN_UNREADABLE_STEP_ROWS = (
    (
        "422 signed literal-container length reaches the suppressor",
        (
            "    holder = [contextlib.suppress(AssertionError)]\n"
            "    with holder[-len(holder)]:\n        assert x != 1\n"
        ),
    ),
    (
        "422 keyword dict type namespace reaches the suppressor",
        (
            "    Box = type('H', (), dict(b=contextlib.suppress(AssertionError)))\n"
            "    with Box.b:\n        assert x != 1\n"
        ),
    ),
)


@pytest.mark.parametrize(
    ("label", "body"),
    SUPPRESSOR_REACHED_THROUGH_AN_UNREADABLE_STEP_ROWS,
    ids=[row[0] for row in SUPPRESSOR_REACHED_THROUGH_AN_UNREADABLE_STEP_ROWS],
)
def test_previously_unreadable_steps_match_executed_suppression(label, body):
    """The original residual inputs remain unchanged and now agree with execution."""
    source = "import contextlib\ndef outer(x, flag):\n" + body
    namespace = {}
    exec(compile(source, "<422-residual>", "exec"), namespace)  # noqa: S102
    fired = False
    try:
        namespace["outer"](1, True)
    except AssertionError:
        fired = True
    assert not fired, (
        f"{label}: CPython swallowed this assert, so the fixture is not the "
        f"residual it claims to be"
    )

    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False], f"{label}: executed swallow must be reported defeated: {results}"


def test_a_block_nested_module_carrier_is_still_declined():
    """Pin the *known* limit of the module-scope rule, so it cannot widen silently.

    A carrier written directly in the module body is an unconditional store:
    it has run by the time any function is entered, so the rule can judge the
    header on it. A carrier nested in a module-level ``if`` has run only on
    some paths, and nothing in the syntax says which -- so the rule declines
    and the answer stays ``enforced``.

    That is the **wrong** answer for this fixture: at runtime ``sys.platform``
    is truthy, the carrier binds, and ``with cs:`` raises ``TypeError`` before
    the assert. The row is here precisely because it is wrong, and it is kept
    out of :data:`MODULE_CARRIER_SHAPES` because that table's ``live`` column is
    held to CPython and this row must not claim otherwise.

    What the row buys is that the narrowing is *pinned*. If a later change
    starts reading conditional module bindings as settled, this fails and the
    widening has to be argued for on its own mutation matrix rather than
    arriving as a side effect. It is the same reason #359's criterion 3 wants
    a control: a rule that is merely "conservative" is indistinguishable from
    one that is broken until something says which it is.
    """
    source = (
        "import sys\n"
        "if sys.platform:\n"
        "    import os as cs\n"
        "def outer(x, flag, helper):\n"
        "    with cs:\n        assert x != 1\n"
    )
    # The interpreter half, so the gap is documented as a real one.
    _assert_entry_contract(
        "a block-nested module carrier (runtime: unreachable)",
        source,
        False,
        False,
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"expected the rule to DECLINE a conditional module binding, i.e. "
        f"[True], got {results}. CPython disagrees on this row (the assert is "
        f"unreachable), so a change that reads it as settled has to update "
        f"this test and justify the widening -- not land silently."
    )


@pytest.mark.parametrize(
    ("label", "rebind", "live"),
    [
        (
            "a global rebind run at import time supersedes the carrier",
            "def _rebind():\n    global cs\n    cs = contextlib.nullcontext()\n_rebind()\n",
            True,
        ),
        (
            "CONTROL a global rebind that runs before the carrier is superseded by it",
            "def _rebind():\n    global cs\n    cs = contextlib.nullcontext()\n_rebind()\n",
            False,
        ),
    ],
    ids=[
        "a global rebind run at import time supersedes the carrier",
        "CONTROL a global rebind that runs before the carrier is superseded by it",
    ],
)
def test_a_conditional_module_store_after_a_carrier_is_declined(label, rebind, live):
    """A carrier followed by a *conditional* store leaves the value undecided.

    ``_stores_of`` keeps only unconditional stores, so an unconditional
    carrier survives a conditional store that follows it. That is the right
    answer when the conditional store merely *mentions* the name, but not when
    it can have replaced the carrier with something enterable:

        import os as cs
        import contextlib
        def _rebind():
            global cs
            cs = contextlib.nullcontext()
        _rebind()
        def outer(x, flag, helper):
            with cs:            # succeeds: cs is a real nullcontext()
                assert x != 1   # live

    The store is nested in a function body, so whether it ran is not decidable
    from the carrier's point of view -- but it *can* have run, and if it did the
    carrier is stale. Answering from the carrier drops a live assert, so the
    rule declines instead.

    The control is the same source with the call moved *before* the carrier. A
    conditional store that precedes the last carrier really is superseded by
    it, so ``defeated`` is correct there and no decline is warranted. Without
    the control, a fix that simply declined every shape containing a
    conditional store would pass.

    This is deliberately not a row in :data:`MODULE_CARRIER_SHAPES`: that
    table's fixture is ``prelude + "def outer(...)"``, which cannot express a
    helper *call* between the carrier and the header.
    """
    carrier = "import os as cs\nimport contextlib\n"
    if "before" in label:
        source = (
            "import contextlib\n"
            + rebind
            + carrier
            + "def outer(x, flag, helper):\n    with cs:\n        assert x != 1\n"
        )
    else:
        source = (
            carrier + rebind + "def outer(x, flag, helper):\n    with cs:\n        assert x != 1\n"
        )
    _assert_entry_contract(label, source, False, live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A conditional "
        f"store after the last carrier can have replaced it with an enterable "
        f"value, so the header must be declined rather than judged on a store "
        f"that may be stale."
    )


#: #388: a **function-scope** carrier superseded by a conditional store.
#:
#: `_module_stores` was given the ordered decline by #375, but the function
#: path was not: `_stores_of` keeps only unconditional stores, so an
#: unconditional function-scope carrier survived a *later conditional*
#: non-carrier store, went stale, and was read as the settled value. The
#: answer was `defeated` on asserts the interpreter really evaluates --
#: a live pinned contract dropped, which is the damaging direction this whole
#: rule exists to correct.
#:
#: Every row below is a store form that **may** have rebound the name to a
#: real `nullcontext()` after the carrier ran. Each is executed by
#: `_assert_entry_contract`, so the `live` column is held to CPython rather
#: than asserted about the checker.
#:
#: Every one of these ten rows is a **regression**: all ten answer `defeated`
#: against a live assert on `ed9d9b0`, and all ten answer correctly on the
#: pre-#375 base `0529c8a`. The three shapes that were *already* wrong on that
#: base are not here -- they are a separate defect, pinned as a known limit in
#: :func:`test_a_function_carrier_supersession_limit_is_still_declined`.
FUNCTION_CARRIER_SUPERSESSION_SHAPES = (
    (
        "a conditional store after a function-scope carrier is declined",
        "    import os as cs\n    if flag:\n        cs = contextlib.nullcontext()\n",
        True,
    ),
    (
        "a conditional walrus after a function-scope carrier is declined",
        "    import os as cs\n    if flag:\n        (cs := contextlib.nullcontext())\n",
        True,
    ),
    (
        "a conditional tuple-unpack after a function-scope carrier is declined",
        "    import os as cs\n    if flag:\n        cs, other = (contextlib.nullcontext(), 2)\n",
        True,
    ),
    (
        "a conditional annotated store after a function-scope carrier is declined",
        "    import os as cs\n    if flag:\n        cs: object = contextlib.nullcontext()\n",
        True,
    ),
    (
        "a loop target after a function-scope carrier is declined",
        "    import os as cs\n    for cs in (contextlib.nullcontext(),):\n        pass\n",
        True,
    ),
    (
        "a nested conditional store after a function-scope carrier is declined",
        "    import os as cs\n    if flag:\n        if flag:\n            cs = contextlib.nullcontext()\n",
        True,
    ),
    (
        "a store inside a with-block after a function-scope carrier is declined",
        "    import os as cs\n    with contextlib.nullcontext():\n        cs = contextlib.nullcontext()\n",
        True,
    ),
    (
        "a while-body store after a function-scope carrier is declined",
        "    import os as cs\n    while flag:\n        cs = contextlib.nullcontext()\n        break\n",
        True,
    ),
    (
        "a try/else store after a function-scope carrier is declined",
        "    import os as cs\n    try:\n        pass\n    except Exception:\n        pass\n    else:\n        cs = contextlib.nullcontext()\n",
        True,
    ),
    # Two carriers with a conditional store *between* them. The decline has to
    # compare against the **last** carrier, not the first: `def cs()` runs
    # unconditionally and re-binds the name to a function, so the conditional
    # store is already stale and the header is genuinely `defeated`. Reading
    # the first carrier instead would decline this row.
    (
        "a conditional store between two carriers is decided by the last carrier",
        "    import os as cs\n    if flag:\n        cs = contextlib.nullcontext()\n    def cs():\n        pass\n",
        False,
    ),
    # No carrier at all: an unconditional real store, then a conditional one.
    # The decline is about a *stale carrier*, so with no carrier to go stale
    # the last unconditional binding settles the value and the row stays live.
    # A decline keyed on "any conditional store" rather than on a carrier
    # would get this backwards.
    (
        "a conditional store after an unconditional non-carrier is still live",
        "    cs = contextlib.nullcontext()\n    if flag:\n        cs = contextlib.nullcontext()\n",
        True,
    ),
    # The two rows below are the *negative* of the row above, and they are
    # what stops the decline from being keyed on "a conditional store exists"
    # rather than on "a carrier may have been superseded". There is no carrier
    # in either one, so there is nothing to go stale and the unconditional
    # `cs = None` settles the value; the later conditional store never runs on
    # this path, and `with cs:` raises on the `None`. The `if False` row is
    # exactly the store-not-taken case, and the `def` row is the nested-scope
    # case whose store binds a different local.
    #
    # The store-not-taken row was spelled `if not flag:` and was measured to be
    # wrong: that branch is *taken* for `flag=False`, where the store installs
    # a real manager and the assert does fire. `_assert_entry_contract` calls
    # `outer(1, True, None)`, so it only ever saw the `flag=True` path and
    # could not tell the two apart -- the row was claiming a property of the
    # fixture that the fixture did not have. `if False:` is the spelling that
    # actually makes the store unreachable, and it is dead for every value of
    # `flag`, which is what "the branch is not taken" has to mean. Verified by
    # execution on CPython 3.12.14 at both `flag=True` and `flag=False`.
    (
        "a conditional store in a nested def does not supersede a non-carrier",
        "    cs = None\n    def inner():\n        cs = contextlib.nullcontext()\n",
        False,
    ),
    (
        "a conditional store whose branch is not taken does not supersede",
        "    cs = None\n    if False:\n        cs = contextlib.nullcontext()\n",
        False,
    ),
    # The ordering bound itself. The conditional store is *after* the `with`,
    # so it cannot have superseded anything the header read; the carrier is
    # still in force and the header genuinely raises.
    (
        "CONTROL a conditional store after the header does not supersede it",
        "    import os as cs\n    with cs:\n        assert x != 1\n    if flag:\n        cs = contextlib.nullcontext()\n",
        False,
    ),
    # `del cs` *unbinds*; it never installs an enterable value. So it cannot
    # supersede the carrier, and the header stays `defeated` on both paths:
    # with flag=False the carrier raises TypeError, with flag=True CPython has
    # deleted the name and it raises UnboundLocalError. Counting the delete as
    # a superseding store would decline the header, and a decline reports
    # `enforced` -- inventing a dead assert. Found as a blocking review
    # finding on #388.
    (
        "CONTROL a conditional del after a function-scope carrier is defeated",
        "    import os as cs\n    if flag:\n        del cs\n",
        False,
    ),
    (
        "CONTROL an unconditional store after a function-scope carrier is live",
        "    import os as cs\n    cs = contextlib.nullcontext()\n",
        True,
    ),
    (
        "CONTROL a conditional store before a function-scope carrier is defeated",
        "    if flag:\n        cs = contextlib.nullcontext()\n    import os as cs\n",
        False,
    ),
    (
        "CONTROL an except-as after a function-scope carrier is defeated",
        "    import os as cs\n    try:\n        pass\n    except Exception as cs:\n        pass\n",
        False,
    ),
)
