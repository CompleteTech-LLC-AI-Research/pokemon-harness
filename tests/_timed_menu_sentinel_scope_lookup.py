"""Scope lookup and loop-else suppression contracts.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support
from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_import_context_managers import (
    _assert_entry_contract,
)


def test_only_a_module_level_class_counts():
    """#316 criterion 2, the scope half: a nested class is not matched.

    A class nested inside another is reached as ``Outer.Inner()`` in a ``with``
    header. Collecting class definitions from every scope would match the inner
    *name* alone, so a bare ``Inner`` meaning something else entirely would be
    reported as a defeat. The limit is deliberate, and it is only a real limit
    if something pins it -- an earlier version of this rule collected classes
    from any scope and the whole suite stayed green, which is exactly the
    no-op mutation the #310 dunder rule shipped as.

    The class below really is a swallowing context manager; only its nesting
    puts it out of reach. That is what separates "cannot see it" from "it is
    not one".
    """
    source = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def __enter__(self):\n"
        "            return self\n"
        "        def __exit__(self, *exc):\n"
        "            return exc[0] is AssertionError\n"
        "def outer(x):\n"
        "    with Outer.Inner():\n"
        "        assert x != 1\n"
    )
    tree = ast.parse(source)
    outer = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(outer) if isinstance(node, ast.Assert))
    assert "Inner" not in support._locally_defined_classes(tree), (
        "a nested class leaked into the module-level class table; the rule "
        "would then match a bare `Inner` that is a different object"
    )
    assert _is_enforced(outer, target, tree), (
        "a class nested inside another was matched by its bare inner name"
    )


def test_a_module_scope_lookup_does_not_descend_into_a_function_body():
    """#316: the two scopes must stay distinguishable, or nothing pins them.

    ``_assigned_value`` reads a module's *own top-level statements* and a
    function's statements separately, with the function consulted first. That
    split is load-bearing: ``_constructed_class_of`` relies on a function-local
    binding shadowing an outer one, and on the module fallback firing only when
    the function really has nothing to say.

    Using ``ast.walk`` on the module would collapse the two -- it descends into
    every function body, so a module lookup would find assignments belonging to
    an unrelated function. The row below is the shape that tells the two
    readings apart, and it is not reachable from any other #316 test: they all
    resolve their binding inside the one function under test, so the module
    fallback is never the thing being exercised.

    Reintroducing ``ast.walk`` here leaves the whole suite green while flipping
    this row: the module-level ``helper`` is a loud ``Loud``, so the assert is
    live and must stay ``enforced``, but the walk finds ``unrelated``'s
    ``helper = Suppressor()`` and reports the live assert as a defeat -- the
    damaging direction.
    """
    source = (
        "class Suppressor:\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *exc):\n"
        "        return exc[0] is AssertionError\n"
        "class Loud:\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *exc):\n"
        "        return False\n"
        "def unrelated(y):\n"
        "    helper = Suppressor()\n"
        "helper = Loud()\n"
        "def probe(x):\n"
        "    with helper:\n"
        "        assert x != 1\n"
    )
    tree = ast.parse(source)
    probe = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "probe"
    )
    target = next(node for node in ast.walk(probe) if isinstance(node, ast.Assert))
    assert _is_enforced(probe, target, tree), (
        "a module-scope lookup descended into an unrelated function body and "
        "adopted its binding; a live assert was reported as defeated"
    )


#: #375 round 4. A **conditional** store that may have superseded a
#: function-local carrier. The carrier is unconditional and settles the name on
#: its own, but a later conditional store can replace it with something
#: enterable, and then the assert under the header is live. The value is
#: therefore undecidable and the rule must decline.
#:
#: Every row is executed under CPython with ``flag`` **true** by
#: ``_assert_entry_contract``, which is the path on which the store runs and the
#: assert really fires. These are the four false-dead verdicts ``origin/master``
#: (``ed9d9b0``) returned for exactly these sources.
STALE_LOCAL_CARRIER_SHAPES = (
    (
        "an import carrier superseded by a conditional store",
        "    import os as cs",
        "    if flag:\n        cs = nullcontext()",
    ),
    (
        "a def carrier superseded by a conditional store",
        "    def cs():\n        pass",
        "    if flag:\n        cs = nullcontext()",
    ),
    (
        "a class carrier superseded by a conditional store",
        "    class cs:\n        pass",
        "    if flag:\n        cs = nullcontext()",
    ),
    (
        "an import carrier superseded by a loop store",
        "    import os as cs",
        "    for _ in (1,):\n        cs = nullcontext()",
    ),
    # Controls for the third review round, all of which must stay LIVE. Each
    # is a shape the second round read as decidable-and-dead, and each is in
    # fact decidable-and-live: a false operand in an `or` does not make the
    # condition false, and a loop whose *last* element is a context manager
    # leaves that element bound.
    (
        "a false operand in an or-ed condition",
        "    import os as cs",
        "    if flag or False:\n        cs = nullcontext()",
    ),
    (
        "a false operand in an or-ed condition after a def carrier",
        "    def cs():\n        pass",
        "    if False or flag:\n        cs = nullcontext()",
    ),
    (
        "a loop whose last element is a context manager",
        "    import os as cs",
        "    if flag:\n        for cs in (None, nullcontext()):\n            pass",
    ),
    # The rows below are the fourth review round. An independent reviewer
    # executed 73 fixtures under real CPython and found these three reported
    # DEAD while CPython reaches the assert. A false-dead is the damaging
    # direction: it certifies a real contract as swallowed, so each row below
    # is a LIVE header that an earlier revision got backwards.
    (
        "a shadowed builtin constructor returning a manager",
        "    import os as cs\n    def int():\n        return nullcontext()",
        "    if flag:\n        cs = int()",
    ),
    (
        "a loop body rebinding the target to a manager",
        "    import os as cs",
        "    if flag:\n        for cs in (None,):\n            cs = nullcontext()",
    ),
    (
        "a conditional manager after an unconditional none store",
        "    import os as cs\n    cs = None",
        "    if flag:\n        cs = nullcontext()",
    ),
    # The row below is the fifth review round's one *live* carrier row. A fresh
    # review of `86fab6d` reproduced all four findings by execution; this one is
    # the case where CPython genuinely enters the header, so the correct verdict
    # is LIVE.
    #
    # #388-5c. The condition path was handed no enclosing scope at all, so a
    # *locally* defined `list()` was read as the builtin and `if list():` was
    # decided as the empty builtin -- retiring a branch CPython enters, because
    # the local definition returns a real context manager.
    (
        "a locally shadowed constructor guarding a branch",
        "    import os as cs\n    def list():\n        return nullcontext()",
        "    if list():\n        cs = nullcontext()",
    ),
    # #388-5a's discriminating half. The row above pins the *zero-argument*
    # form, whose branch genuinely never runs. These four pin the
    # **argument-bearing** forms, whose branches CPython *does* enter because
    # the argument produces an element. Emptiness was decided from the
    # constructor *type* rather than from the call's argument list, so every
    # one of them was read as the empty builtin and the branch was retired --
    # a false-dead, which is the damaging direction.
    #
    # Each row is therefore executed under CPython with `flag` true, so the
    # fixture proves the branch really is entered before the LIVE verdict is
    # checked.
    (
        "a branch guarded by a set built from a literal",
        "    import os as cs",
        "    if set([1]):\n        cs = nullcontext()",
    ),
    (
        "a branch guarded by a list built from a tuple",
        "    import os as cs",
        "    if list((1,)):\n        cs = nullcontext()",
    ),
    (
        "a branch guarded by a dict built from a keyword",
        "    import os as cs",
        "    if dict(a=1):\n        cs = nullcontext()",
    ),
    (
        "a branch guarded by a bytearray built from bytes",
        "    import os as cs",
        "    if bytearray(b'x'):\n        cs = nullcontext()",
    ),
)


#: The mirror of :data:`STALE_LOCAL_CARRIER_SHAPES`, and the reason a first cut
#: of the guard above was rejected. A conditional store that is itself pinned
#: **non-enterable** cannot rescue the header on the path where it runs, so the
#: carrier still decides every path and the assert is unreachable either way.
#: Declining these would replace a correct dead verdict with a false-live one
#: -- a regression against ``origin/master``, not a repair.
#:
#: Measured on CPython 3.12.14 with ``outer(1, False, None)`` and
#: ``outer(1, True, None)``: both raise ``TypeError`` for all four rows, so the
#: assert is unreachable on both paths and the correct verdict is dead.
DEAD_CONDITION_AFTER_CARRIER_SHAPES = (
    (
        "a None store after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = None",
    ),
    (
        "a None store after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        cs = None",
    ),
    (
        "a None store after a class carrier",
        "    class cs:\n        pass",
        "    if flag:\n        cs = None",
    ),
    (
        "an int store after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = 42",
    ),
    (
        "a list store after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = [1]",
    ),
    (
        "a str store after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = 'x'",
    ),
    (
        "a starred target after an import carrier",
        "    import os as cs",
        "    if flag:\n        *cs, = (helper,)",
    ),
    (
        "a starred target after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        *cs, = (helper,)",
    ),
    (
        "a starred target after a class carrier",
        "    class cs:\n        pass",
        "    if flag:\n        *cs, = (helper,)",
    ),
    (
        "a chained bare target after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = (other, x) = (helper, 1)",
    ),
    (
        "a chained bare target after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        cs = (other, x) = (helper, 1)",
    ),
    (
        "a chained bare target after a class carrier",
        "    class cs:\n        pass",
        "    if flag:\n        cs = (other, x) = (helper, 1)",
    ),
    (
        "a starred target on a non-tuple right-hand side",
        "    import os as cs",
        "    if flag:\n        *cs, = helper",
    ),
    (
        "a leading starred target after an import carrier",
        "    import os as cs",
        "    if flag:\n        *cs, rest = pair",
    ),
    (
        "a nested starred target after an import carrier",
        "    import os as cs",
        "    if flag:\n        a, (b, *cs) = pair",
    ),
    (
        "a trailing starred target after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        first, *cs = pair",
    ),
    (
        "a trailing starred target after a class carrier",
        "    class cs:\n        pass",
        "    if flag:\n        first, *cs = pair",
    ),
    # The rows below are the second review round. The first cut of this repair
    # declined any conditional store whose value it could not read as a literal,
    # and that is true too often: a builtin constructor call, an element taken
    # from a literal container, and a store inside a branch that provably never
    # runs are each decidable, and declining them reported a dead header live
    # where CPython raises on every path.
    (
        "a builtin constructor call after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = int()",
    ),
    (
        "a builtin constructor call after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        cs = list()",
    ),
    (
        "a builtin constructor with an argument",
        "    import os as cs",
        "    if flag:\n        cs = bool(1)",
    ),
    (
        "an element of a literal container",
        "    import os as cs",
        "    if flag:\n        cs, other = (None, 1)",
    ),
    (
        "a list-target element of a literal container",
        "    import os as cs",
        "    if flag:\n        [cs] = [None]",
    ),
    (
        "a literal element inside a nested pattern",
        "    import os as cs",
        "    if flag:\n        cs, (other, third) = (None, (1, 2))",
    ),
    (
        "a loop over a literal container of None",
        "    import os as cs",
        "    if flag:\n        for cs in (None,):\n            pass",
    ),
    (
        "a store in a literally false branch",
        "    import os as cs",
        "    if False:\n        cs = nullcontext()",
    ),
    (
        "a store in a conjunctively false branch",
        "    import os as cs",
        "    if flag and False:\n        cs = nullcontext()",
    ),
    (
        "a store in a false branch after a class carrier",
        "    class cs:\n        pass",
        "    if flag and False:\n        cs = nullcontext()",
    ),
    # The rows below are the third review round. Each was a regression the
    # second round introduced while fixing the first, and each is the kind of
    # mistake that only a fresh fixture finds: a type missing from the
    # non-enterable set, a boolean operator read the wrong way round, a loop
    # spelling of a dead branch, and a loop whose *last* element -- not its
    # first -- is the value left bound.
    (
        "a range constructor after an import carrier",
        "    import os as cs",
        "    if flag:\n        cs = range(3)",
    ),
    (
        "an argument-less range constructor",
        "    import os as cs",
        "    if flag:\n        cs = range()",
    ),
    (
        "a frozenset constructor after a def carrier",
        "    def cs():\n        pass",
        "    if flag:\n        cs = frozenset()",
    ),
    (
        "a store in an and-ed empty-literal branch",
        "    import os as cs",
        "    if flag and ():\n        cs = nullcontext()",
    ),
    (
        "a store in a while-false branch",
        "    import os as cs",
        "    while False:\n        cs = nullcontext()",
    ),
    (
        "a loop whose last element is not enterable",
        "    import os as cs",
        "    if flag:\n        for cs in (nullcontext(), None):\n            pass",
    ),
    (
        "a loop over an empty builtin container",
        "    import os as cs",
        "    if flag:\n        for cs in set():\n            pass",
    ),
    (
        "a nested pattern holding a starred element",
        "    import os as cs",
        "    if flag:\n        cs, (other, rest3) = (None, (1, 2))",
    ),
    # The remaining fourth-round rows were reported LIVE while CPython raises
    # before the assert. These are the false-live direction: they do not hide a
    # contract, but they claim an entry works when it cannot, which is the
    # mirror error and still blocks a merge.
    (
        "a store in a branch guarded by an or of false operands",
        "    import os as cs",
        "    if flag and (False or ()):\n        cs = nullcontext()",
    ),
    (
        "a store in a branch guarded by an empty builtin call",
        "    import os as cs",
        "    if set():\n        cs = nullcontext()",
    ),
    (
        "a loop over an empty bytearray",
        "    import os as cs",
        "    if flag:\n        for cs in bytearray():\n            pass",
    ),
    (
        "an unconditional constructor store",
        "    import os as cs\n    cs = int()",
        "",
    ),
    (
        "a store in a branch guarded by an empty dict",
        "    import os as cs",
        "    if {}:\n        cs = nullcontext()",
    ),
    # The rows below are the fifth review round's remaining carrier rows. A
    # fresh review of `86fab6d` reproduced all four findings by execution, and
    # these three are the ones where CPython raises before the assert anyway, so
    # the correct verdict is DEAD. A regression here shows up as a row flipping
    # to LIVE -- the mild direction, but still a row claiming an entry works
    # when it cannot.
    #
    # #388-5a. Emptiness was decided from the constructor *type* rather than from
    # the call's argument list, so `set([1])`, `list((1,))`, `dict(a=1)` and
    # `bytearray(b"x")` -- all truthy -- were read as empty containers and their
    # branch was treated as one that never runs.
    (
        "a set built from a literal is not an empty container",
        "    import os as cs",
        "    if flag:\n        cs = set([1])",
    ),
    (
        "a list built from a tuple is not an empty container",
        "    import os as cs",
        "    if flag:\n        cs = list((1,))",
    ),
    (
        "a dict built from a keyword is not an empty container",
        "    import os as cs",
        "    if flag:\n        cs = dict(a=1)",
    ),
    (
        "a bytearray built from bytes is not an empty container",
        "    import os as cs",
        "    if flag:\n        cs = bytearray(b'x')",
    ),
    # #388-5d. Collapsing a loop target into its body's rebind retired the
    # target, and the resolution then still sorted over *every* entry, so the
    # retired target won the source-order tie against the body's store and the
    # suppressor was dropped in favour of the element the target yielded.
    (
        "a loop body rebinding the target to a suppressor",
        "    import os as cs\n    cs = None",
        "    if flag:\n        for cs in (None,):\n            cs = suppress(AssertionError)",
    ),
)


@pytest.mark.parametrize(
    ("label", "carrier", "conditional"),
    STALE_LOCAL_CARRIER_SHAPES,
    ids=[shape[0] for shape in STALE_LOCAL_CARRIER_SHAPES],
)
def test_a_conditional_store_superseding_a_local_carrier_is_declined(label, carrier, conditional):
    """A carrier a conditional store may have replaced is not still in force.

    The rule must not report the entry dead on the strength of a carrier that
    a later conditional store may have replaced. Every row is executed under
    CPython with ``flag`` **true**, which is the path on which the assert is
    live, so the fixture proves the header really is enterable and the
    ``enforced`` verdict is the correct answer.
    """
    source = (
        "import contextlib\n"
        "from contextlib import nullcontext\n"
        "def outer(x, flag, helper):\n" + carrier + "\n" + conditional + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    # `_assert_entry_contract` calls `outer(1, True, None)`: `x=1` makes the
    # assert false, and `flag=True` makes the conditional store run, so the
    # name holds a real context manager and the assert is live.
    _assert_entry_contract(label, source, False, True)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"{label}: expected verdicts [True], got {results}. A conditional store "
        f"that may have superseded the carrier leaves the header's value "
        f"undecidable, so the rule must decline instead of reporting the "
        f"entry dead."
    )


#: #429. An `elif` is not a top-level statement: it is an `ast.If` nested in
#: the *previous* `if`'s `orelse`. So the store below is reached through TWO
#: enclosing blocks, and the inner one carries a literal-`True` test. The
#: always-true-branch rule read that inner block on its own and settled the
#: name -- but the link is entered only when `if x:` is *false*, and `x` is a
#: parameter. That dropped the earlier `nullcontext()` from the store table
#: and reported a header CPython enters as DEAD.
#:
#: This is the damaging direction (#308 criterion 1): a contract that really
#: fires is certified as unreachable. The regression entered at `b90f985`, a
#: commit whose own message says three of its four fixes were false-deads, and
#: then survived six further review rounds and every later CI run.
#:
#: The controls matter as much as the row. `if True:` as the *first* link is
#: genuinely unconditional and must keep its answer, and the repair is not
#: allowed to generalise into "any nested `if True:` is conditional".
ELIF_LINK_ARMS_SHAPES = (
    (
        "an elif link with a literal-true test",
        "    if x:\n        pass\n    elif True:\n        cs = list()",
        True,
    ),
    (
        "CONTROL a first-link if True: is still unconditional",
        "    if True:\n        cs = list()",
        False,
    ),
    (
        "CONTROL an elif link binding a real context manager",
        "    if x:\n        pass\n    elif True:\n        cs = nullcontext()",
        True,
    ),
    (
        "CONTROL a nested if True: inside a conditional if",
        "    if x:\n        cs = list()\n    else:\n        cs = nullcontext()",
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "chain", "second_assert_live"),
    ELIF_LINK_ARMS_SHAPES,
    ids=[row[0] for row in ELIF_LINK_ARMS_SHAPES],
)
def test_an_elif_link_is_reached_only_when_every_test_above_failed(
    label, chain, second_assert_live
):
    """A literal-true ``elif`` test does not make its arm unconditional.

    This is #429. The rule that lets a store inside an always-true branch count
    as an unconditional store exists so that ``if True:`` settles a name -- a
    shape ``b90f985`` needed for module-scope shadowing. An ``elif True:`` arm
    has a literal-true test too, but it is a *later link* of a chain: it is
    entered only when every test above it failed. When the test above is a
    parameter, the arm settles the name on some calls and not others, so the
    earlier store stays in force on the rest -- and the header is enterable
    there.

    Executed on CPython 3.12.14, with ``x=1``:

    * ``if x: pass elif True: cs = list()`` -- the ``if x:`` arm runs, the
      ``elif`` body never does, ``cs`` is still the ``nullcontext()``, and
      ``with cs:`` enters. The assert **fires**. Ground truth is live.
    * ``if True: cs = list()`` -- the arm runs on every call, ``cs`` is a
      ``list``, and ``with cs:`` raises ``TypeError`` before the body.
      Ground truth is dead.

    The repair distinguishes the two by asking whether the literal-true block
    is itself a later link of a chain, not by reading its own test in
    isolation. Every row here is executed by ``_assert_entry_contract``, so a
    control that disagreed with the interpreter would fail rather than be
    asserted into the table.
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
        f"{label}: expected verdicts [{second_assert_live}], got {results}. An "
        f"`elif` arm runs only when every test above it failed, so a store in "
        f"it settles the name on some calls only. A literal-true *first* `if` "
        f"is a different case and stays unconditional."
    )
