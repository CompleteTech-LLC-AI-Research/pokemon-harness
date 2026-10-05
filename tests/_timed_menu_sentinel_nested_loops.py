"""Nested loop bindings and capture behavior.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support
from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_import_context_managers import (
    _assert_entry_contract,
)
from tests._timed_menu_sentinel_module_binding_order import (
    STARRED_LOOP_ENTRY_UNREACHABLE_ROWS,
)


@pytest.mark.parametrize(
    ("label", "block", "second_assert_live"),
    STARRED_LOOP_ENTRY_UNREACHABLE_ROWS,
    ids=[row[0] for row in STARRED_LOOP_ENTRY_UNREACHABLE_ROWS],
)
def test_a_starred_target_reached_through_a_loop_binds_a_list(label, block, second_assert_live):
    """#420. The list-wrapping of a starred target survives loop machinery.

    #419 fixed ``*cs, = (...)`` and ``a, *cs = (...)`` where the store is
    written directly in the function body. The same name read through a loop
    was still certified ``enforced`` while CPython raised ``TypeError`` before
    the body, so the assert was unreachable and the contract was dead.

    The two shapes fail for different reasons, and both are pinned:

    * a starred *store* inside a ``for`` body is declined by
      ``_store_is_settled_before``, which is right in general -- a ``with``
      header is evaluated before its own statement's body runs -- but
      collapses "has not run yet" into the same "cannot tell" answer as
      "undecidable". A starred target is decidable either way, because the
      list-wrapping comes from the target syntax and is the same whether the
      store ran, has not run, or never will.
    * a starred *loop target* is declined by the #336 rule, which is right for
      a plain loop target (it binds the next element, and
      ``for cs in (nullcontext(),):`` is genuinely live) and over-broad for the
      starred sub-case, where the iterable is irrelevant.

    Measured on CPython 3.12.14 with ``x=1``, so ``assert x != 1`` is false:
    both fixtures raise ``TypeError: 'list' object does not support the context
    manager protocol`` on entry, and ``hasattr(cs, "__enter__")`` is ``False``
    in both. The two controls really do enter and really do fire, which is
    what keeps the repair from generalising to every loop-bound name.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n" + block
    )
    _assert_entry_contract(label, source, False, second_assert_live)
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [second_assert_live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A starred "
        f"target binds a list however the loop is reached, and entering a list "
        f"raises before the body, so the assert under the header is "
        f"unreachable."
    )


@pytest.mark.parametrize(
    ("body", "live"),
    (
        ("    for *cs, cs in ((1, contextlib.nullcontext()),):\n", True),
        ("    for (*cs, (cs,)) in ((1, (contextlib.nullcontext(),)),):\n", True),
        ("    for cs, *cs in ((contextlib.nullcontext(), 1),):\n", False),
        (
            "    for _ in (1,):\n        *cs, cs = (1, contextlib.nullcontext())\n",
            True,
        ),
        (
            "    for _ in (1,):\n        *cs, = (1,)\n        cs = contextlib.nullcontext()\n",
            True,
        ),
        (
            "    for *cs, in ((1,),):\n        cs = contextlib.nullcontext()\n",
            True,
        ),
        (
            "    for _ in (1,):\n        if not flag:\n            *cs, = (1,)\n",
            True,
        ),
        (
            (
                "    for _ in (1,):\n"
                "        *cs, = (1,)\n"
                "        if flag:\n"
                "            cs = contextlib.nullcontext()\n"
            ),
            True,
        ),
    ),
)
def test_starred_loop_store_must_remain_in_force_at_entry(body, live):
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    cs = contextlib.nullcontext()\n" + body + "        with cs:\n"
        "            assert x != 1\n"
    )
    # The conditional controls execute the path where the name stays usable.
    _assert_entry_contract(body, source, False, live)
    tree = ast.parse(source)
    function = tree.body[0]
    header = next(node for node in ast.walk(function) if isinstance(node, ast.With))
    assert support._entered_name_is_dead(header, function, {"contextlib": "contextlib"}, tree) is (
        not live
    )


@pytest.mark.parametrize("future_store", (False, True))
def test_starred_store_after_a_loop_header_does_not_rewrite_its_value(future_store):
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    cs = contextlib.nullcontext()\n"
        "    for _ in (1,):\n"
        "        with cs:\n"
        "            assert x != 1\n"
    )
    if future_store:
        source += "        *cs, = (1,)\n"
    _assert_entry_contract("future starred store", source, False, True)
    tree = ast.parse(source)
    function = tree.body[0]
    header = next(node for node in ast.walk(function) if isinstance(node, ast.With))
    assert not support._entered_name_is_dead(header, function, {"contextlib": "contextlib"}, tree)


@pytest.mark.parametrize(("iterable", "live"), (("()", True), ("((1,),)", False)))
def test_starred_target_after_a_loop_respects_whether_it_ran(iterable, live):
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    cs = contextlib.nullcontext()\n"
        f"    for *cs, in {iterable}:\n"
        "        pass\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_entry_contract("completed starred loop", source, False, live)
    tree = ast.parse(source)
    function = tree.body[0]
    header = function.body[-1]
    assert support._entered_name_is_dead(header, function, {"contextlib": "contextlib"}, tree) is (
        not live
    )


NONLOCAL_SUPPRESSOR_SHAPES = (
    (
        "a walrus self-alias, store after the nested def",
        (
            "    def inner():\n"
            "        nonlocal cs\n"
            "        with (cs := cs):\n"
            "            assert x != 1\n"
            "    cs = {value}\n"
            "    inner()"
        ),
    ),
    (
        "a walrus self-alias, store before the nested def",
        (
            "    cs = {value}\n"
            "    def inner():\n"
            "        nonlocal cs\n"
            "        with (cs := cs):\n"
            "            assert x != 1\n"
            "    inner()"
        ),
    ),
    (
        "a rebind then a direct read",
        (
            "    def inner():\n"
            "        nonlocal cs\n"
            "        cs = cs\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "    cs = {value}\n"
            "    inner()"
        ),
    ),
    (
        "a direct read with no rebind at all",
        (
            "    def inner():\n"
            "        nonlocal cs\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "    cs = {value}\n"
            "    inner()"
        ),
    ),
)


@pytest.mark.parametrize(
    ("label", "body"),
    NONLOCAL_SUPPRESSOR_SHAPES,
    ids=[shape[0] for shape in NONLOCAL_SUPPRESSOR_SHAPES],
)
@pytest.mark.parametrize(
    ("value", "enforced"),
    [
        ("contextlib.suppress(AssertionError)", False),
        ("contextlib.nullcontext()", True),
    ],
    ids=["suppressor-is-defeating", "nullcontext-stays-live"],
)
def test_a_nonlocal_name_reaches_its_enclosing_binding(label, body, value, enforced):
    """A `nonlocal` header reads the enclosing function's binding.

    The assert is scored in `inner`, the scope it is written in. That matters:
    handed `outer` instead, a whole-module walk finds the assert in the wrong
    scope and answers a different question (#355 spells this trap out).
    """
    source = "def outer(x):\n    import contextlib\n" + body.format(value=value) + "\n"
    tree = ast.parse(source)
    inner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "inner"
    )
    asserts = [node for node in ast.walk(inner) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(inner, node, tree) for node in asserts]
    assert results == [enforced], (
        f"{label}: expected {[enforced]}, got {results}. A `nonlocal` name is "
        f"an alias for the enclosing function's binding, so the header enters "
        f"whatever `cs` holds out there."
    )


def test_a_closure_name_that_is_not_declared_nonlocal_is_not_followed():
    """Following an outer binding is confined to names declared `nonlocal`.

    The rule that consults the enclosing function is gated on the declaration,
    because a `nonlocal` is the one place the source states outright that a
    name belongs to an enclosing function. A plain closure read is not
    followed, and here the enclosing store is a `nullcontext()`, so the assert
    is live and must stay live. This is the guard on the fix over-reaching.
    """
    source = (
        "def outer(x):\n"
        "    import contextlib\n"
        "    cs = contextlib.nullcontext()\n"
        "    def inner():\n"
        "        with cs:\n"
        "            assert x != 1\n"
        "    inner()\n"
    )
    tree = ast.parse(source)
    inner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "inner"
    )
    asserts = [node for node in ast.walk(inner) if isinstance(node, ast.Assert)]
    results = [_is_enforced(inner, node, tree) for node in asserts]
    assert results == [True], (
        f"expected [True], got {results}. Nothing declared `cs` nonlocal, so "
        f"the enclosing store is out of scope for the resolution."
    )


AFTER_LOOP_FOR_TARGET_SHAPES = (
    (
        "a suppressing target, which swallows the assertion",
        (
            "    for cs in (1, contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    (
        "a nullcontext target, which really enters",
        (
            "    for cs in (1, contextlib.nullcontext()):\n"
            "        pass\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "enforced"),
    AFTER_LOOP_FOR_TARGET_SHAPES,
    ids=[shape[0] for shape in AFTER_LOOP_FOR_TARGET_SHAPES],
)
def test_a_loop_target_read_after_the_loop_keeps_its_real_value(label, body, enforced):
    """A `for` target read after the loop carries the loop's LAST element.

    The loop literal is multi-element, so the value that survives the loop is
    decided by position: the trailing ``nullcontext()`` for the first row and
    the trailing ``suppress(...)`` for the second. Reading the *first* element
    would make both rows ``1``, which is not a context manager at all, and the
    answer would collapse to a single wrong verdict for the pair.
    """
    source = "def probe(x):\n    import contextlib\n" + body
    tree = ast.parse(source)
    probe = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "probe"
    )
    target = next(node for node in ast.walk(probe) if isinstance(node, ast.Assert))
    assert _is_enforced(probe, target, tree) is enforced, (
        f"{label}: expected enforced={enforced}. After the loop `cs` is the "
        f"last element of the literal, and the header enters whatever that is."
    )


def test_the_after_loop_for_target_expectation_matches_executed_cpython():
    """The row above is scored against the interpreter, not against a table.

    A hand-written expectation pins whatever the author believed; executing
    the fixture pins what the contract actually does. This is the check that
    would have caught the merge regression at authoring time rather than in a
    follow-up differential, and it is deliberately the *only* place in this
    group that calls the function.
    """
    for label, body, enforced in AFTER_LOOP_FOR_TARGET_SHAPES:
        source = "import contextlib\ndef probe(x):\n" + body
        namespace: dict = {}
        exec(compile(source, "<after-loop-for-target>", "exec"), namespace)  # noqa: S102
        try:
            namespace["probe"](1)
        except AssertionError:
            fires = True
        except (TypeError, UnboundLocalError, NameError):
            # A loud `TypeError` on entry is still a live contract: the
            # interpreter is refusing to enter, not swallowing the assert.
            fires = True
        else:
            fires = False
        assert enforced is fires, (
            f"{label}: the test table says enforced={enforced} but executed "
            f"CPython {'raises' if fires else 'does not raise'} for x=1"
        )


NONLOCAL_REBIND_REGRESSIONS = (
    (
        "the nearest function owns the binding",
        (
            "def outer(x):\n"
            "    import contextlib\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    def middle():\n"
            "        cs = contextlib.nullcontext()\n"
            "        def inner():\n"
            "            nonlocal cs\n"
            "            with cs:\n"
            "                assert x != 1\n"
            "        inner()\n"
            "    middle()\n"
        ),
        True,
    ),
    (
        "the nearest suppressor defeats independently of the outer value",
        (
            "def outer(x):\n"
            "    import contextlib\n"
            "    cs = contextlib.nullcontext()\n"
            "    def middle():\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "        def inner():\n"
            "            nonlocal cs\n"
            "            with cs:\n"
            "                assert x != 1\n"
            "        inner()\n"
            "    middle()\n"
        ),
        False,
    ),
    (
        "the parent's later store retires its suppressor",
        (
            "def outer(x):\n"
            "    import contextlib\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    cs = contextlib.nullcontext()\n"
            "    def inner():\n"
            "        nonlocal cs\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "    inner()\n"
        ),
        True,
    ),
    (
        "an inner store retires the enclosing suppressor",
        (
            "def outer(x):\n"
            "    import contextlib\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    def inner():\n"
            "        nonlocal cs\n"
            "        cs = contextlib.nullcontext()\n"
            "        with (cs := cs):\n"
            "            assert x != 1\n"
            "    inner()\n"
        ),
        True,
    ),
    (
        "a grandchild declaration does not govern its parent's local",
        (
            "def outer(x):\n"
            "    import contextlib\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    def inner():\n"
            "        cs = contextlib.nullcontext()\n"
            "        def grandchild():\n"
            "            nonlocal cs\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "    inner()\n"
        ),
        True,
    ),
    (
        "a parent store after invocation has not run yet",
        (
            "def outer(x):\n"
            "    import contextlib\n"
            "    cs = contextlib.nullcontext()\n"
            "    def inner():\n"
            "        nonlocal cs\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "    inner()\n"
            "    cs = contextlib.suppress(AssertionError)\n"
        ),
        True,
    ),
    (
        "a later nullcontext cannot change an earlier invocation",
        (
            "def outer(x):\n"
            "    import contextlib\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    def inner():\n"
            "        nonlocal cs\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "    inner()\n"
            "    cs = contextlib.nullcontext()\n"
        ),
        False,
    ),
    (
        "another called closure can replace the captured object",
        (
            "def outer(x):\n"
            "    import contextlib\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    def rebind():\n"
            "        nonlocal cs\n"
            "        cs = contextlib.nullcontext()\n"
            "    def inner():\n"
            "        nonlocal cs\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "    rebind()\n"
            "    inner()\n"
        ),
        True,
    ),
)


@pytest.mark.parametrize(("label", "source", "enforced"), NONLOCAL_REBIND_REGRESSIONS)
def test_nonlocal_scope_and_rebinding_match_executed_calls(label, source, enforced):
    """A readable historical value cannot defeat a currently live assertion."""
    namespace = {}
    exec(compile(source, f"<nonlocal-path:{label}>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1)
    except AssertionError:
        reached = True
    else:
        reached = False
    assert reached is enforced, f"{label}: fixture's interpreter truth differs"
    tree = ast.parse(source)
    inner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "inner"
    )
    assertion = next(node for node in ast.walk(inner) if isinstance(node, ast.Assert))
    assert _is_enforced(inner, assertion, tree) is enforced, label


ALWAYS_RUN_ARM_EXECUTED_ROWS = (
    ("plain decided elif", "if False:\n    pass\nelif True:\n    cs = list()", False),
    ("plain decided else", "if False:\n    pass\nelse:\n    cs = list()", False),
    (
        "decided chain",
        "if False:\n    pass\nelif False:\n    pass\nelif True:\n    cs = list()",
        False,
    ),
    (
        "decided chain else",
        "if False:\n    pass\nelif False:\n    pass\nelse:\n    cs = list()",
        False,
    ),
    ("a true predecessor skips its elif", "if True:\n    pass\nelif True:\n    cs = list()", True),
    (
        "a conditional predecessor can skip its elif",
        "if flag:\n    pass\nelif True:\n    cs = list()",
        True,
    ),
    (
        "a conditional intermediate link can skip its elif",
        "if False:\n    pass\nelif flag:\n    pass\nelif True:\n    cs = list()",
        True,
    ),
    (
        "a conditional parent can skip its else store",
        "if flag:\n    if False:\n        pass\n    else:\n        cs = list()",
        True,
    ),
    (
        "a conditional child can skip a store under decided else",
        "if False:\n    pass\nelse:\n    if flag:\n        cs = list()",
        True,
    ),
    (
        "a conditional child under decided elif",
        "if False:\n    pass\nelif True:\n    if flag:\n        cs = list()",
        True,
    ),
    (
        "a skipped else leaves an assertion active",
        "if True:\n    pass\nelse:\n    cs = list()",
        True,
    ),
    (
        "an always-run arm can leave a manager",
        "if False:\n    pass\nelif True:\n    cs = contextlib.nullcontext()",
        True,
    ),
    (
        "an always-run class store is not an instance",
        "if False:\n    pass\nelse:\n    class cs:\n        pass",
        False,
    ),
    (
        "an always-run function store is not a manager",
        "if False:\n    pass\nelif True:\n    def cs():\n        pass",
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "branch", "live"),
    ALWAYS_RUN_ARM_EXECUTED_ROWS,
    ids=[row[0] for row in ALWAYS_RUN_ARM_EXECUTED_ROWS],
)
def test_always_run_arms_follow_every_enclosing_conditional(label, branch, live):
    source = (
        "import contextlib\ndef outer(x, flag):\n"
        "    cs = contextlib.nullcontext()\n"
        + "\n".join("    " + line for line in branch.splitlines())
        + "\n    with cs:\n        assert x != 1\n"
    )
    runtime = {}
    exec(compile(source, "<always-run-arm>", "exec"), runtime)  # noqa: S102
    fired = []
    for flag in (False, True):
        try:
            runtime["outer"](1, flag)
        except AssertionError:
            fired.append(True)
        except TypeError:
            fired.append(False)
        else:
            fired.append(False)
    assert any(fired) is live, label
    tree = ast.parse(source)
    function = tree.body[1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, assertion, tree) is live, label


@pytest.mark.parametrize("inner_arm", ("else", "elif"))
def test_a_decided_inner_arm_still_depends_on_a_conditional_outer_else(inner_arm):
    inner = (
        "        if False:\n            pass\n        "
        + ("else:" if inner_arm == "else" else "elif True:")
        + "\n            cs = []\n"
    )
    source = (
        "import contextlib\n"
        "def outer(flag):\n"
        "    cs = contextlib.nullcontext()\n"
        "    if flag:\n        pass\n    else:\n" + inner + "    with cs:\n        assert False\n"
    )
    namespace = {}
    exec(compile(source, "<conditional-outer-else>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](True)
    with pytest.raises(TypeError):
        namespace["outer"](False)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True


@pytest.mark.parametrize(
    ("setup", "predicate", "expected"),
    (
        ("cs = contextlib.nullcontext()", "x", True),
        ("cs = contextlib.nullcontext()", "x == 1", True),
        ("cs = contextlib.nullcontext()", "not x", False),
        ("cs = contextlib.nullcontext()", "x != 1", False),
        # #471. The ordering operators. These were declined outright, which was
        # safe while `None` only ever meant "decline the claim" -- and stopped
        # being safe once a caller read `None` as "possibly true". The pairs
        # below are the ones that disagree with each other, so a repair that
        # simply declined every ordering comparison would fail half of them:
        # at the failing value `x == 1`, `x < 2` holds and skips the suppressor
        # (live), while `x > 2` does not and swallows it (dead).
        ("cs = contextlib.nullcontext()", "x < 2", True),
        ("cs = contextlib.nullcontext()", "x > 2", False),
        ("cs = contextlib.nullcontext()", "x <= 1", True),
        ("cs = contextlib.nullcontext()", "x > 1", False),
        ("cs = contextlib.nullcontext()", "x >= 1", True),
        ("cs = contextlib.nullcontext()", "x < 1", False),
        ("cs = contextlib.nullcontext()", "x <= 0", False),
        ("cs = contextlib.nullcontext()", "x >= 0", True),
        ("", "x", False),
        ("cs = None", "x", False),
        ("cs = 1", "x", False),
        ("cs = contextlib.suppress(AssertionError)", "x", False),
        ("cs = factory()", "x", False),
        ("cs = contextlib.nullcontext(1, 2)", "x", False),
        ("cs = contextlib.suppress(AssertionError)\n    cs = contextlib.nullcontext()", "x", True),
        ("cs = contextlib.nullcontext()\n    cs = contextlib.suppress(AssertionError)", "x", False),
        # An ordering comparison Python itself cannot evaluate has no truth
        # value to report, and the assert under test cannot be evaluated against
        # one either. Declining is the only honest answer, and it must not
        # raise out of the predicate.
        ("cs = contextlib.nullcontext()", 'x < "a"', False),
    ),
)
def test_elif_suppression_resolution_requires_an_enterable_failing_skipped_path(
    setup, predicate, expected
):
    source = (
        "import contextlib\n"
        "def factory(): return contextlib.suppress(AssertionError)\n"
        "def outer(x):\n"
        + ("    " + setup + "\n" if setup else "")
        + "    if "
        + predicate
        + ":\n        pass\n"
        "    elif True:\n        cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<elif-failure-witness>", "exec"), namespace)  # noqa: S102
    fired = False
    for value in (0, 1):
        try:
            namespace["outer"](value)
        except AssertionError:
            fired = True
        except (TypeError, UnboundLocalError):
            pass
    assert fired is expected
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is expected
