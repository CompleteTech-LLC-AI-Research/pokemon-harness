"""Import-root and canonical module import cases.

Actual test functions and literal cases from the original collector.
"""

import ast
import contextlib
import sys
import types

import pytest

from tests import _timed_menu_milestone_sentinel_support as support
from tests._timed_menu_milestone_sentinel_support import _is_enforced


def test_an_alias_that_rebinds_the_walked_root_is_not_transparent():
    """`import fake as contextlib` must not pass as the filed spelling.

    This is the negative side of #475, and it is the one that keeps the widened
    acceptance sound.  Widening to "an alias the witness never reads is
    inert" is only safe while an alias that *does* rebind a walked root is
    still refused, because such an import puts a different module behind the
    name the witness walks:

        import fake as contextlib   # fake.suppress is contextlib.suppress
        cs = contextlib.nullcontext()
        for item in (1,):
            break
        else:
            cs = contextlib.suppress(AssertionError)
        with cs:
            assert x != 1

    A stand-in module is needed rather than a fixture row, because the fixture
    has to run for this to measure anything, and no unrelated module in the
    standard library carries the `nullcontext` / `suppress` pair.  It is
    registered in `sys.modules` so `import fake` resolves, and restored
    afterwards so the rest of the lane sees a clean namespace.

    What is pinned here is the *predicate*, which is the only part #475
    changed.  With the `break` above, CPython fires the assert and the
    end-to-end verdict agrees with the refusal, so this shape cannot
    demonstrate a false-LIVE by itself.  Dropping the `break` makes the `else`
    install `fake.suppress` and swallow the assert while `_is_enforced` still
    answers True -- but that residual is pre-existing on `49b899a`, is
    unchanged by this repair, and belongs to the loop-`else` reachability
    family tracked in #466 rather than to the import rule.  Widening the
    predicate here must not make that worse, which is what this test holds.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import fake as contextlib\n"
        "    cs = contextlib.nullcontext()\n"
        "    for item in (1,):\n"
        "        break\n"
        "    else:\n"
        "        cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    statement = function.body[0]
    assert isinstance(statement, ast.Import), "fixture no longer starts with a bare import"

    stand_in = types.ModuleType("fake")
    stand_in.nullcontext = contextlib.nullcontext
    stand_in.suppress = contextlib.suppress
    previous = sys.modules.get("fake")
    sys.modules["fake"] = stand_in
    try:
        namespace = {}
        exec(compile(source, "<alias-rebind>", "exec"), namespace)  # noqa: S102
        assert (
            support._import_only_binds_the_resolved_root(
                statement, function, support._bound_names(tree, function)
            )
            is False
        ), (
            "an alias that rebinds the root the witness walks is not the filed "
            "spelling and must not be admitted as a transparent import"
        )
        # The false-LIVE residual lives in the loop-`else` family (#466), so it
        # is held at its measured base value rather than fixed here.  What #475
        # must not do is make it worse by letting this import through the
        # pre-chain scan, which is what the assertion above prevents.
        rebound = ast.parse(source.replace("        break\n", "        pass\n"))
        shadow_fn = next(
            node
            for node in rebound.body
            if isinstance(node, ast.FunctionDef) and node.name == "outer"
        )
        targets = [node for node in ast.walk(shadow_fn) if isinstance(node, ast.Assert)]
        results = [_is_enforced(shadow_fn, node, rebound) for node in targets]
        assert results == [True], (
            "known residual, unchanged from 49b899a: the rebinding alias with no "
            "`break` is still certified enforced while CPython swallows the assert "
            f"(got {results})."
        )
        # The same rule has to hold for `from` spellings, which bind a leaf.
        # `from json import loads as contextlib` puts an unrelated leaf under
        # the name the witness walks, exactly as `import fake as contextlib`
        # does, so it must be refused for the same reason.
        for preamble in (
            "    from json import loads as contextlib\n    import contextlib\n",
            "    from contextlib import suppress as contextlib\n    import contextlib\n",
        ):
            rebound_from = ast.parse(
                "def outer(x, flag, helper):\n" + preamble + "    cs = contextlib.nullcontext()\n"
                "    for item in (1,):\n"
                "        break\n"
                "    else:\n"
                "        cs = contextlib.suppress(AssertionError)\n"
                "    with cs:\n"
                "        assert x != 1\n"
            )
            from_fn = next(
                node
                for node in rebound_from.body
                if isinstance(node, ast.FunctionDef) and node.name == "outer"
            )
            assert (
                support._import_only_binds_the_resolved_root(
                    from_fn.body[0],
                    from_fn,
                    support._bound_names(rebound_from, from_fn),
                )
                is False
            ), (
                "a from-import that rebinds the root the witness walks must be "
                f"refused too, like its `import ... as ...` counterpart: {preamble!r}"
            )
    finally:
        if previous is None:
            sys.modules.pop("fake", None)
        else:
            sys.modules["fake"] = previous


def test_a_dotted_import_of_a_leaf_is_not_a_transparent_root():
    """`import contextlib.nullcontext` binds the leaf, so it must decline.

    This cannot be an execution row: CPython rejects the import outright
    (`ModuleNotFoundError: 'contextlib' is not a package`), so the shared
    oracle never reaches an assert and cannot judge a verdict.  The predicate
    is therefore pinned directly, which is the only honest way to hold it.

    The distinction matters because a dotted import is exactly the shape that
    *does* shadow the attribute path the witness follows.  Treating it as
    transparent would let a rebinding import pass the pre-chain scan and talk
    the correlated witness into a false-LIVE.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib.nullcontext\n"
        "    cs = contextlib.nullcontext()\n"
        "    for item in (1,):\n"
        "        break\n"
        "    else:\n"
        "        cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    statement = function.body[0]
    assert isinstance(statement, ast.Import), "fixture no longer starts with a bare import"
    assert not support._import_only_binds_the_resolved_root(statement, function, set()), (
        "a dotted import binds the leaf name, not the root the witness reads, "
        "so it must not be admitted as a transparent import"
    )


def test_an_elif_cannot_invent_a_nullcontext_from_a_shadowed_import():
    source = (
        "import contextlib\n"
        "from types import SimpleNamespace\n"
        "def outer(x):\n"
        "    contextlib = SimpleNamespace(nullcontext=lambda: real.suppress(AssertionError), suppress=real.suppress)\n"
        "    cs = contextlib.nullcontext()\n"
        "    if x:\n        pass\n"
        "    elif True:\n        cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<elif-import-shadow>", "exec"), namespace)  # noqa: S102
    namespace["real"] = namespace["contextlib"]
    for value in (0, 1):
        namespace["outer"](value)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


@pytest.mark.parametrize(
    "earlier",
    (
        "    if x == 1:\n        return\n",
        "    if x == 1:\n        raise TypeError\n",
        "    doomed = factory()\n",
    ),
)
def test_an_elif_failure_witness_cannot_skip_earlier_control_or_opaque_calls(earlier):
    source = (
        "import contextlib\n"
        "def factory(): raise TypeError\n"
        "def outer(x):\n"
        "    cs = contextlib.nullcontext()\n"
        + earlier
        + "    if x:\n        pass\n    elif True:\n        cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<elif-unreachable-witness>", "exec"), namespace)  # noqa: S102
    for value in (0, 1):
        try:
            namespace["outer"](value)
        except TypeError:
            pass
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


def test_an_elif_cannot_invent_a_nullcontext_from_a_shadowed_module_import():
    source = (
        "import contextlib\n"
        "from types import SimpleNamespace\n"
        "real = contextlib\n"
        "contextlib = SimpleNamespace(nullcontext=lambda: real.suppress(AssertionError), suppress=real.suppress)\n"
        "def outer(x):\n"
        "    cs = contextlib.nullcontext()\n"
        "    if x:\n        pass\n"
        "    elif True:\n        cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<elif-global-import-shadow>", "exec"), namespace)  # noqa: S102
    for value in (0, 1):
        namespace["outer"](value)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


UNREACHED_LOOP_BODY_ROWS = (
    (
        "an empty tuple loop body never rebinds the name",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in ():\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        True,
    ),
    (
        "an empty list loop body never rebinds the name",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in []:\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        True,
    ),
    (
        "a header nested in the same block reads the earlier binding too",
        (
            "    cs = contextlib.nullcontext()\n"
            "    if True:\n"
            "        for item in ():\n"
            "            cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n"
            '            assert x != 1, "A1"'
        ),
        True,
    ),
    (
        "an empty loop nested in a running loop is still unreachable",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for outer in (1,):\n"
            "        for item in ():\n"
            "            cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        True,
    ),
    (
        "CONTROL a loop that iterates does rebind the name",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in [1]:\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
    ),
    (
        "CONTROL a falsy member still counts as an iteration",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in (0,):\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
    ),
    (
        "CONTROL a two-element loop rebinds on the last iteration",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in (1, 2):\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
    ),
    # #378, from the independent review of `3edecda`. The `else` clause of a
    # loop is what runs *because* the loop finished without `break`, so it
    # executes on every path through a zero-iteration loop -- the body is
    # skipped, the `else` is not. `For.body` and `For.orelse` are AST
    # siblings, so a walk over the ancestors sees both, and treating any
    # ancestor `For` as "its stores are unreachable" swept these up too.
    #
    # Executed on CPython 3.12.14 each of these *swallows* the assert, and
    # `master` (`2c81f10`) already answered all of them correctly. Reporting
    # them live was a regression the `3edecda` helper introduced, in the
    # damaging direction: a defeated contract certified as load-bearing.
    (
        "the else clause of a zero-iteration loop still runs",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in ():\n"
            "        pass\n"
            "    else:\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
    ),
    (
        "an else clause over a list literal still runs",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in []:\n"
            "        pass\n"
            "    else:\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
    ),
    (
        "an else clause over a dict literal still runs",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in {}:\n"
            "        pass\n"
            "    else:\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
    ),
    (
        "a store nested in the else clause still runs",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in ():\n"
            "        pass\n"
            "    else:\n"
            "        if True:\n"
            "            cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
    ),
    (
        "a store inside a with in the else clause still runs",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in ():\n"
            "        pass\n"
            "    else:\n"
            "        with contextlib.suppress(ValueError):\n"
            "            cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
    ),
    # Control for the rows above. A `for`/`else` `else` clause runs on normal
    # completion, and a loop that completes normally has run every iteration,
    # so a non-empty loop's `else` rebinds as well -- it must keep reporting
    # `False`, or a fix that read *every* `else` as unreachable would pass the
    # five rows above.
    #
    # The `break` case is deliberately not a row here. A `break` really does
    # skip the `else`, so the assert is live, but the analyzer answers
    # `defeated` for that shape on `master` (`2c81f10`) exactly as it does on
    # this branch -- measured on both, CPython fires and both trees report
    # `False`. That is a pre-existing false-DEAD of its own, unrelated to
    # #378, and pinning it here would either fail this PR for a defect it did
    # not introduce or quietly widen its scope. It is left for a separate
    # issue rather than absorbed.
    (
        "CONTROL a completed loop runs its else clause too",
        (
            "    cs = contextlib.nullcontext()\n"
            "    for item in (1,):\n"
            "        pass\n"
            "    else:\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n"
            '        assert x != 1, "A1"'
        ),
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "expected_live"),
    UNREACHED_LOOP_BODY_ROWS,
    ids=[row[0] for row in UNREACHED_LOOP_BODY_ROWS],
)
def test_an_unreachable_loop_body_does_not_rebind_the_name(label, body, expected_live):
    """A loop with no iterations performs none of the stores in its body.

    #378. `for item in ():` is a statement that has been *reached*, and the
    store rules list statements by that question -- "have the block's stores
    run by the time this header is read" -- so a `for` was counted. But the
    guarantee a loop offers is per *iteration*: a loop over a literal empty
    iterable has none, and the body store never happens.

    The consequence was a live contract reported as defeated, because the
    carried `nullcontext` was retired by a suppressor that was never bound.

    Scoped to a provably empty literal on purpose. `helper.items()` may yield
    nothing, but it may not, so a store in that body keeps competing and the
    ordinary conservative handling applies. The controls pin that a loop which
    does iterate still rebinds, and that a falsy member `(0,)` is an iteration.
    """
    source = "def outer(x, helper):\n    import contextlib\n" + body + "\n"
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    fired = False
    try:
        namespace["outer"](1, None)
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
    # CPython settles the expectation, so a `defeated` row is only credible
    # when the assert really is swallowed, and a `live` row only when it
    # really fires.
    assert fired is expected_live, (
        f"{label}: executed on CPython the assert "
        f"{'fired' if fired else 'did not fire'}, so the row's expectation "
        f"{expected_live} does not match. Row is stale."
    )
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [expected_live], (
        f"{label}: expected {[expected_live]}, got {results}. A loop over an "
        f"empty literal performs none of its body's stores."
    )


@pytest.mark.parametrize(
    "initial,conditional,live",
    (
        ("contextlib.suppress(AssertionError)", "None", False),
        ("contextlib.suppress(AssertionError)", "1", False),
        ("contextlib.suppress(AssertionError)", "[]", False),
        ("contextlib.suppress(AssertionError)", "{}", False),
        ("contextlib.suppress(AssertionError)", "()", False),
        ("contextlib.suppress(AssertionError)", "contextlib.nullcontext()", True),
        ("contextlib.suppress(AssertionError)", "contextlib.suppress(ValueError)", True),
        ("contextlib.suppress(ValueError)", "None", True),
        ("contextlib.nullcontext()", "None", True),
    ),
)
@pytest.mark.parametrize(
    "ghost", ("contextlib.nullcontext()", "contextlib.suppress(AssertionError)", "None", "1")
)
@pytest.mark.parametrize("ghost_before", (False, True))
def test_empty_loop_filter_keeps_conditional_entry_outcomes(
    initial, conditional, live, ghost, ghost_before
):
    conditional_store = "    if flag:\n        cs = " + conditional + "\n"
    ghost_store = "    for item in ():\n        cs = " + ghost + "\n"
    source = (
        "import contextlib\ndef outer(x, flag):\n    cs = "
        + initial
        + "\n"
        + (ghost_store + conditional_store if ghost_before else conditional_store + ghost_store)
        + "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<conditional-empty-loop>", "exec"), namespace)  # noqa: S102
    fired = []
    for flag in (False, True):
        try:
            namespace["outer"](1, flag)
        except AssertionError:
            fired.append(True)
        except TypeError:
            fired.append(False)
        else:
            fired.append(False)
    assert any(fired) is live
    tree = ast.parse(source)
    function = tree.body[1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, assertion, tree) is live


@pytest.mark.parametrize("placement", ("module", "parent", "parameter", "local"))
def test_empty_loop_filter_declines_shadowed_exception_argument(placement):
    module = "AssertionError = ValueError\n" if placement == "module" else ""
    setup = "    AssertionError = ValueError\n" if placement == "local" else ""
    signature = "flag, AssertionError=ValueError" if placement == "parameter" else "flag"
    body = (
        "def outer("
        + signature
        + "):\n"
        + setup
        + "    cs = contextlib.suppress(AssertionError)\n"
        + "    if flag:\n        cs = None\n"
        + "    for item in ():\n        cs = contextlib.nullcontext()\n"
        + "    with cs:\n        assert False\n"
    )
    if placement == "parent":
        body = (
            "def parent():\n    AssertionError = ValueError\n"
            + "".join("    " + line for line in body.splitlines(keepends=True))
            + "    return outer\nouter = parent()\n"
        )
    source = "import contextlib\n" + module + body
    namespace = {}
    exec(compile(source, "<shadowed-empty-loop>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](False)
    with pytest.raises(TypeError):
        namespace["outer"](True)
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, assertion, tree) is True


@pytest.mark.parametrize(
    "body,live",
    (
        ("    match [contextlib.nullcontext(), True]:\n        case [cs, False]: pass\n", False),
        ("    match [contextlib.nullcontext(), False]:\n        case [cs, True]: pass\n", False),
        ("    match [contextlib.nullcontext(), 0]:\n        case [cs, None]: pass\n", False),
        ("    match [contextlib.nullcontext(), True]:\n        case [cs, True]: pass\n", True),
        ("    match [contextlib.nullcontext(), False]:\n        case [cs, False]: pass\n", True),
        ("    match [contextlib.nullcontext(), None]:\n        case [cs, None]: pass\n", True),
        (
            "    subject = [contextlib.nullcontext()]\n    subject.clear()\n    match subject:\n        case [cs]: pass\n",
            False,
        ),
        (
            "    subject = [contextlib.nullcontext()]\n    subject, other = [], 1\n    match subject:\n        case [cs]: pass\n",
            False,
        ),
        (
            "    subject = [contextlib.nullcontext()]\n    alias = subject\n    alias.clear()\n    match subject:\n        case [cs]: pass\n",
            False,
        ),
        (
            "    subject = [contextlib.nullcontext()]\n    subject[:] = []\n    match subject:\n        case [cs]: pass\n",
            False,
        ),
        (
            "    subject = [contextlib.nullcontext()]\n    match subject:\n        case [cs]: pass\n",
            True,
        ),
        (
            "    subject = [contextlib.nullcontext()]\n    pass\n    match subject:\n        case [cs]: pass\n",
            True,
        ),
        ("    match [contextlib.nullcontext(), True]:\n        case [cs, 1]: pass\n", True),
        ("    match [contextlib.nullcontext(), 1.0]:\n        case [cs, 1]: pass\n", True),
    ),
)
def test_literal_match_selection_preserves_runtime_truth(body, live):
    source = (
        "import contextlib\ndef outer(x):\n"
        "    cs = contextlib.suppress(AssertionError)\n"
        + body
        + "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<literal-match-selection>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1)
    except AssertionError:
        fired = True
    else:
        fired = False
    assert fired is live
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is live


@pytest.mark.parametrize(
    "manager,live",
    (
        ("contextlib.suppress(AssertionError)", False),
        ("contextlib.nullcontext()", True),
        ("contextlib.suppress(ValueError)", True),
    ),
)
@pytest.mark.parametrize("named", (False, True))
def test_literal_match_records_captured_manager(manager, live, named):
    subject = "[" + manager + "]"
    setup = "    subject = " + subject + "\n" if named else ""
    source = (
        "import contextlib\ndef outer(x):\n    cs = contextlib.suppress(AssertionError)\n"
        + setup
        + "    match "
        + ("subject" if named else subject)
        + ":\n        case [cs]: pass\n    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<literal-captured-manager>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1)
    except AssertionError:
        fired = True
    else:
        fired = False
    assert fired is live
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is live


@pytest.mark.parametrize(
    "element,error",
    (
        ("missing()", NameError),
        ("1 / 0", ZeroDivisionError),
        ("contextlib.nullcontext(1, 2)", TypeError),
    ),
)
def test_literal_match_declines_subject_construction_failure(element, error):
    source = (
        "import contextlib\ndef outer(x):\n    cs = contextlib.suppress(AssertionError)\n"
        + "    match [contextlib.nullcontext(), "
        + element
        + "]:\n        case [cs, _]: pass\n    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<failing-literal-subject>", "exec"), namespace)  # noqa: S102
    with pytest.raises(error):
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False
