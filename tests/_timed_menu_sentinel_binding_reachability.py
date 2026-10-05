"""Binding reachability through local and captured stores.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced

#: The residual defeats of #287: every spelling that keeps the assert in the AST
#: while making it incapable of failing, and the live control beside it that
#: must stay enforced.
#:
#: Each pair is one *behavioral* claim about the interpreter, not a guess about
#: what the checker ought to accept. ``with cs:`` where ``cs`` is a suppressor
#: really does swallow the failure and the test stays green; the same suppression
#: reached through ``.__enter__()`` raises ``TypeError`` instead, so that one is
#: listed as a control. Guessing wrong here is what would have shipped a rule
#: either missing the real defect or crying wolf on live asserts.
RESIDUAL_DEFEAT_SHAPES = (
    # A parameter has no import to resolve, so the dotted path is a bare name
    # and the resolution rule finds nothing to match.
    (
        "suppress passed as a parameter",
        (
            "    def probe(suppress):\n        with suppress(AssertionError):\n"
            "            assert x != 1"
        ),
        False,
    ),
    # ExitStack defers the suppression past the with header, into the body.
    (
        "enter_context defers past the header",
        (
            "    with ExitStack() as stack:\n"
            "        stack.enter_context(suppress(AssertionError))\n"
            "        assert x != 1"
        ),
        False,
    ),
    # pytest.raises(AssertionError) turns the failure into a *pass*: the raise
    # happens, is caught, matches, and the block ends normally.
    (
        "pytest.raises catches the assert",
        "    with pytest.raises(AssertionError):\n        assert x != 1",
        False,
    ),
    (
        "pytest.raises Exception",
        "    with pytest.raises(Exception):\n        assert x != 1",
        False,
    ),
    (
        "pytest.raises BaseException",
        "    with pytest.raises(BaseException):\n        assert x != 1",
        False,
    ),
    # A tuple argument is the ordinary spelling on the real file; reading only
    # the tuple node would report it as unreadable and thus universal, which
    # would drop four live pinned sites.
    (
        "pytest.raises tuple naming AssertionError",
        "    with pytest.raises((TypeError, AssertionError)):\n        assert x != 1",
        False,
    ),
    # `match=` is undecidable, not loud: whether the block ends green depends
    # on the assertion's own message at runtime. Measured with match="", ".*"
    # (both green) and match="1 == 2" (pytest re-raises). The conservative
    # reading is taken -- an assert that is *sometimes* unenforceable is not a
    # contract that can be relied on.
    (
        "pytest.raises AssertionError with a matching regex",
        "    with pytest.raises(AssertionError, match=''):\n        assert x != 1",
        False,
    ),
    # The alias form. This is the *silent* one: entering the object directly
    # swallows, whereas the __enter__ dunder returns None and raises TypeError.
    (
        "suppressor bound to a name",
        "    cs = contextlib.suppress(AssertionError)\n    with cs:\n        assert x != 1",
        False,
    ),
    (
        "aliased suppressor Exception",
        "    cs = suppress(Exception)\n    with cs:\n        assert x != 1",
        False,
    ),
    # Order decides whether the alias is bound *yet*. Entering `cs` before the
    # assignment that defines it raises NameError on entry, so the test fails
    # loudly rather than passing quietly -- not a defeat. A rule that collects
    # every assignment in the function and ignores position would call this
    # dead and drop a live contract.
    (
        "with cs before the assignment binds it",
        ("    with cs:\n        assert x != 1\n    cs = contextlib.suppress(AssertionError)"),
        True,
    ),
    # The same alias with the assignment first is the silent defeat.
    (
        "with cs after the assignment binds it",
        ("    cs = contextlib.suppress(AssertionError)\n    with cs:\n        assert x != 1"),
        False,
    ),
    (
        "annotated suppressor alias",
        (
            "    cs: object = contextlib.suppress(BaseException)\n    with cs:\n"
            "        assert x != 1"
        ),
        False,
    ),
    # #308: an alias binds its name wherever it is written. Restricting the
    # walk to the function's top level reports each of these as *enforced*,
    # which is the damaging direction -- a swallowed assert certified as
    # load-bearing.
    (
        "suppressor alias bound in an if branch",
        (
            "    if flag:\n        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    # The three rows below differ from the one above in exactly one way: the
    # `with cs:` is written INSIDE the block that binds it, so the assignment
    # and the entry are part of a *single* top-level statement. The swallow is
    # identical -- executed, the assert never fails -- but to `function.body`
    # the binder and the header share one index, and a walk that applied
    # bindings only at the end of each top-level statement read the header
    # before the name existed. All three execute green and were reported
    # enforced; see the #311 review. `items` and `flag` are the enclosing
    # function's parameters, so the binder really is reachable.
    (
        "suppressor alias bound and entered inside the same if",
        (
            "    if flag:\n        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
        False,
    ),
    (
        "suppressor alias bound and entered inside the same for",
        (
            "    for _ in items:\n        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
        False,
    ),
    (
        "suppressor alias bound and entered inside the same try",
        (
            "    try:\n        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1\n    finally:\n        pass"
        ),
        False,
    ),
    # The control for the three rows above, and the reason the fix orders by
    # source position rather than by statement. Moving the `with` back to the
    # top level leaves the swallow intact, so it must still be reported a
    # defeat; the existing "bound in an if branch" row already covers that,
    # and this pins the direction of the *same* block from the other side.
    (
        "suppressor alias entered after an if that may not have run",
        (
            "    if flag:\n        cs = contextlib.suppress(AssertionError)\n"
            "        helper()\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    (
        "suppressor alias bound in a loop",
        (
            "    for item in items:\n        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    (
        "suppressor alias bound in a try body",
        (
            "    try:\n        cs = contextlib.suppress(AssertionError)\n"
            "    except Exception:\n        pass\n    with cs:\n        assert x != 1"
        ),
        False,
    ),
    (
        "suppressor alias bound in a nested with",
        (
            "    with helper.make():\n        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    # #308 criterion 1: two branches bind ONE name to DIFFERENT targets, so
    # which suppressor applies depends on the path taken. Resolving by source
    # order gives a verdict for one branch only; the name is unreadable and must
    # be reported as a defeat.
    (
        "same alias bound to different suppressors in two branches",
        (
            "    if flag:\n        cs = contextlib.suppress(AssertionError)\n"
            "    else:\n        cs = contextlib.suppress(ValueError)\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    # The ambiguity is between two *harmless* targets, so nothing on either
    # path swallows an assert. The rule still cannot prove that from the source
    # alone, and #308 accepts over-reporting as the safe direction -- but this
    # row pins that decision so the direction cannot be flipped silently.
    (
        "same alias bound to two unrelated suppressors in two branches",
        (
            "    if flag:\n        cs = contextlib.suppress(ValueError)\n"
            "    else:\n        cs = contextlib.suppress(TypeError)\n"
            "    with cs:\n        assert x != 1"
        ),
        False,
    ),
    # A literal container that is empty never enters its body. The loop
    # spelling of the `if False:` defeat.
    ("for over empty list", "    for _ in []:\n        assert x != 1", False),
    ("for over empty tuple", "    for _ in ():\n        assert x != 1", False),
    ("for over empty dict", "    for _ in {}:\n        assert x != 1", False),
    ("for unpack over empty list", "    for _, v in []:\n        assert x != 1", False),
    # #439: an empty *string* is the one literal sequence the rule above was
    # missing. `''` is a `Constant` whose value `_literal_value` already reads
    # exactly, and it iterates zero times just as `[]` does -- so leaving it
    # out reported the assert live, a false-live in the same damaging
    # direction as the defeat, and inconsistent with its three siblings.
    ("for over empty string", "    for _ in '':\n        assert x != 1", False),
    ("for over empty double-quoted string", '    for _ in "":\n        assert x != 1', False),
    # `bytes` is admitted by the same `isinstance` test rather than a separate
    # rule: it is the other literal sequence type with a readable length, and
    # an empty one also yields nothing.
    ("for over empty bytes", "    for _ in b'':\n        assert x != 1", False),
    ("for unpack over empty string", "    for c in '':\n        assert x != 1", False),
    # A non-empty string *does* iterate, so the assert runs and must stay
    # enforced. Without this the rule could be satisfied by matching any
    # string at all, which would report a real contract as defeated.
    ("CONTROL for over one-char string", "    for _ in 'a':\n        assert x != 1", True),
    ("CONTROL for over multi-char string", "    for _ in 'abc':\n        assert x != 1", True),
    (
        "CONTROL for over single empty-char iteration",
        "    for _ in '':\n        pass\n    assert x != 1",
        True,
    ),
    # The sibling case the type test exists for. A *number* and `None` are
    # also readable `Constant`s, but iterating one is a `TypeError` rather
    # than zero iterations, so it is emphatically not an empty iterable.
    # These rows are what stop the new branch from degenerating into "any
    # constant is empty": that mutant calls `for _ in 0:` and `for _ in
    # None:` provably empty and drops the assert from the sentinel's view,
    # and it survives every string row above on its own.
    ("CONTROL for over an int is not empty", "    for _ in 0:\n        assert x != 1", True),
    ("CONTROL for over a float is not empty", "    for _ in 1.5:\n        assert x != 1", True),
    ("CONTROL for over None is not empty", "    for _ in None:\n        assert x != 1", True),
    ("CONTROL for over False is not empty", "    for _ in False:\n        assert x != 1", True),
    # #449: `f''` is the same empty string reached through a different node. It
    # parses to a `JoinedStr` with no `values`, not to a `Constant`, so the
    # str/bytes branch above cannot see it. Still a literal with a decidable
    # value, so it belongs here rather than with the undecidable calls.
    ("for over empty f-string", "    for _ in f'':\n        assert x != 1", False),
    # An f-string carrying a replacement field is deliberately NOT decided --
    # that means reasoning about the substituted expressions, the same problem
    # as `range(0)`. Both of these must keep their non-empty answer.
    ("CONTROL for over non-empty f-string", "    for _ in f'a':\n        assert x != 1", True),
    (
        "CONTROL for over f-string with a field",
        "    for _ in f'{x}':\n        assert x != 1",
        True,
    ),
    # A tuple with one falsy member still iterates once, so the assert runs.
    ("for over single falsy member", "    for _ in (0,):\n        assert x != 1", True),
    # --- controls: every one of these must stay enforced ---
    (
        "parameter suppress of an unrelated error",
        ("    def probe(suppress):\n        with suppress(ValueError):\n            assert x != 1"),
        True,
    ),
    (
        "pytest.raises of an unrelated error",
        "    with pytest.raises(RuntimeError):\n        assert x != 1",
        True,
    ),
    # The exact tuple spellings the real pinned file uses. If the tuple rule
    # regresses to "unreadable", these are the sites it would wrongly drop.
    (
        "pytest.raises TypeError ValueError",
        "    with pytest.raises((TypeError, ValueError)):\n        assert x != 1",
        True,
    ),
    (
        "pytest.raises KeyError ValueError RuntimeError",
        ("    with pytest.raises((KeyError, ValueError, RuntimeError)):\n        assert x != 1"),
        True,
    ),
    (
        "pytest.raises BaseExceptionGroup",
        "    with pytest.raises(BaseExceptionGroup):\n        assert x != 1",
        True,
    ),
    # With no expected type at all, pytest raises ValueError while building the
    # context object -- before the body runs. No assert is evaluated, so this is
    # not a silent defeat and must not be counted as one.
    (
        "pytest.raises with no expected type",
        "    with pytest.raises():\n        assert x != 1",
        True,
    ),
    (
        "pytest.raises with only a regex",
        "    with pytest.raises(match='nomatch'):\n        assert x != 1",
        True,
    ),
    # `contextlib.suppress()` with no argument is legal, and unlike
    # `pytest.raises()`'s empty call above it is not even loud -- it simply
    # suppresses *nothing*. Measured: `suppress()` stores `_exceptions == ()`,
    # and `__exit__` returns `issubclass(exctype, ())`, which is False for
    # every exception, so an assert inside it fails loudly. #500 corrected
    # this row, which had it backwards with the comment "suppresses
    # everything" -- a description of the function, not of the call.
    (
        "suppress with no exception type",
        "    with contextlib.suppress():\n        assert x != 1",
        True,
    ),
    # An unrelated enter_context on the same stack is not a suppression.
    (
        "unrelated enter_context",
        (
            "    with ExitStack() as stack:\n        stack.enter_context(helper.make())\n"
            "        assert x != 1"
        ),
        True,
    ),
    # A name bound to an ordinary call must not be assumed to suppress.
    (
        "name bound to an ordinary call",
        "    cs = helper.make()\n    with cs:\n        assert x != 1",
        True,
    ),
    (
        "aliased suppressor of an unrelated error",
        "    cs = contextlib.suppress(ValueError)\n    with cs:\n        assert x != 1",
        True,
    ),
    # A context manager parameter is the common legitimate shape.
    ("context manager parameter", "    with cm:\n        assert x != 1", True),
    # Non-empty loops reach their bodies.
    ("for over a list", "    for _ in [1]:\n        assert x != 1", True),
    (
        "for over a falsy member",
        "    for _ in (False, True):\n        assert x != 1",
        True,
    ),
    ("for over range", "    for _ in range(3):\n        assert x != 1", True),
    ("for over a name", "    for _ in items:\n        assert x != 1", True),
    # An empty loop that does not contain the assert is not a defeat of it.
    ("empty loop on a sibling", "    for _ in []:\n        helper()\n    assert x != 1", True),
    # A suppression that is not wrapped around the assert does not disarm it.
    (
        "suppression on a sibling statement",
        "    with contextlib.suppress(AssertionError):\n        helper()\n    assert x != 1",
        True,
    ),
    # A module used as a context manager is not a suppressor. This is the
    # control for SUPPRESSOR_SPELLINGS: taking every component of the dotted
    # path would put `contextlib` in the set and fire here.
    ("module object as a context manager", "    with contextlib:\n        assert x != 1", True),
)


#: #500. ``contextlib.suppress()`` with no exception argument suppresses
#: **nothing**: ``__init__`` stores ``_exceptions == ()`` and ``__exit__``
#: returns ``issubclass(exctype, ())``, which is ``False`` for every
#: exception. An assert inside it therefore fails loudly and is live.
#:
#: The analyzer used to answer universal for this spelling, describing the
#: *function* rather than the runtime behaviour of the *call*, and reported
#: the contract as disarmed. Each row below is executed before it is judged,
#: so the expected verdict is measured rather than asserted.
#:
#: The unreadable spelling is the load-bearing neighbour. ``suppress(*excs)``
#: arrives as a single :class:`ast.Starred`, which is not readable here, so it
#: must still answer universal -- that is what keeps "an argument list I cannot
#: read" from being confused with "an argument list that is empty", and the
#: two are decidably different.
#:
#: ``preamble`` sits at module scope and ``body`` inside ``outer``; the
#: subscript and attribute rows need their binding visible where it is read.
BARE_SUPPRESS_ROWS = (
    (
        "500 a bare suppress() swallows nothing",
        "",
        "    with contextlib.suppress():\n        assert x != 1",
        True,
    ),
    (
        "500 a bare suppress() carried by a subscript swallows nothing",
        "",
        ("    holder = [contextlib.suppress()]\n    with holder[0]:\n        assert x != 1"),
        True,
    ),
    (
        "500 a bare suppress() carried by a class attribute swallows nothing",
        "class Box:\n    ctx = contextlib.suppress()\n",
        "    with Box.ctx:\n        assert x != 1",
        True,
    ),
    (
        "500 a bare suppress() reached through a nested subscript swallows nothing",
        "",
        ("    holder = [[contextlib.suppress()]]\n    with holder[0][0]:\n        assert x != 1"),
        True,
    ),
    (
        "500 a suppress() naming AssertionError is still a defeat",
        "",
        "    with contextlib.suppress(AssertionError):\n        assert x != 1",
        False,
    ),
    (
        "500 a suppress() naming only ValueError stays live",
        "",
        "    with contextlib.suppress(ValueError):\n        assert x != 1",
        True,
    ),
    # `*excs` is passed a *non-empty* tuple, so the runtime really does
    # suppress here. The analyzer cannot see that the star-argument is
    # non-empty -- it sees one unreadable argument -- so it must answer
    # universal anyway. That is the row's whole point: an argument list this
    # check cannot read must never become a licence to call it harmless.
    (
        "500 an unreadable star-argued suppress() stays a defeat",
        "",
        "    with contextlib.suppress(*excs):\n        assert x != 1",
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "preamble", "body", "live"),
    BARE_SUPPRESS_ROWS,
    ids=[row[0] for row in BARE_SUPPRESS_ROWS],
)
def test_a_bare_suppress_is_not_read_as_universal(label, preamble, body, live):
    """#500: an empty argument list is measured, not treated as unreadable.

    Every fixture is executed first, so each row's verdict is CPython's rather
    than an assumption baked into the table.

    The ``*excs`` row is what stops a blanket "empty means harmless" reading.
    At runtime an empty ``*excs`` really does suppress nothing, but the
    analyzer cannot see that it is empty -- it sees one unreadable argument --
    so the row is filed against the *static* reading and must stay a defeat.
    Calling a suppressor unreadable must never quietly become a licence to
    call it harmless, because that is the direction which drops a live
    contract.
    """
    source = "import contextlib\n" + preamble + "def outer(x, excs):\n" + body
    # The starred row is the only one that reads `excs`; it is given a
    # non-empty tuple so the runtime swallow is real and the row measures a
    # defeat the analyzer reaches only by declining to read the argument.
    excs = (AssertionError,) if "*excs" in body else ()
    namespace = {"excs": excs}
    exec(compile(source, "<500-executed>", "exec"), namespace)  # noqa: S102
    fired = False
    raised = None
    try:
        namespace["outer"](1, excs)
    except AssertionError:
        fired = True
    except BaseException as error:  # noqa: BLE001 - the point is which one
        raised = type(error).__name__
    assert raised is None, f"{label}: fixture raised {raised} before the assert"

    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [live], (
        f"{label}: CPython fired={fired}, so the expected verdict is "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )
    assert fired is live, (
        f"{label}: the fixture's runtime disagrees with its own row -- fired={fired}, live={live}"
    )


@pytest.mark.parametrize(
    ("label", "body", "live"),
    RESIDUAL_DEFEAT_SHAPES,
    ids=[shape[0] for shape in RESIDUAL_DEFEAT_SHAPES],
)
def test_the_residual_defeats_of_287_are_rejected(label, body, live):
    """The remaining #287 spellings must be classified by their runtime effect.

    A rule that is too narrow leaves a present-but-dead pinned assertion
    certified as load-bearing; a rule that is too wide drops a live contract
    from the sentinel's view, which is the more damaging of the two errors. The
    controls therefore carry as much weight as the defeats, and several of them
    exist specifically to catch a rule that has been over-generalized to fix a
    gap -- most importantly the tuple-typed ``pytest.raises`` spellings the real
    file actually uses.
    """
    imports = (
        "    import contextlib\n"
        "    from contextlib import suppress\n"
        "    from contextlib import ExitStack\n"
    )
    # Each body is already indented for a function body, so only the header is
    # added -- re-indenting the whole body is what has produced spurious
    # IndentationErrors in this repo's own probes.
    source = "def outer(x, cm, items, record, helper, flag):\n" + imports + body + "\n"
    tree = ast.parse(source)
    outer = tree.body[0]
    for statement in outer.body:
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement is not outer
        ):
            # A `def probe(...)` fixture makes the assert the *only* one, so
            # the nested-def rule cannot confound the suppression verdict.
            function = statement
            break
    else:
        function = outer
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )


#: #308 named two escapes and a third that only appeared once #309 landed. Each
#: row is one *behavioural* claim about the interpreter, established by running
#: the shape rather than by reading the checker: a suppressor bound inline in the
#: header really does swallow the failure, so the assert is dead.
WALRUS_SHAPES = (
    # The binding and the entry are the same node, so nothing that tracks
    # `ast.Assign` ever sees the name and the header holds no bare `Name` to
    # resolve. Executed, the failure is swallowed.
    (
        "walrus binds and enters a suppressor",
        "    with (cs := contextlib.suppress(AssertionError)):\n        assert x != 1",
        False,
    ),
    (
        "walrus binds a broad suppressor",
        "    with (cs := contextlib.suppress(Exception)):\n        assert x != 1",
        False,
    ),
    (
        "walrus binds an assertion-capturing context",
        "    with (cs := pytest.raises(AssertionError)):\n        assert x != 1",
        False,
    ),
    (
        "walrus binds a suppressor by from-import",
        "    with (cs := suppress(AssertionError)):\n        assert x != 1",
        False,
    ),
    # --- controls -----------------------------------------------------------
    # A zero-argument call reads as the unreadable case `BaseException` in
    # `_suppression_names`, and `BaseException` *does* catch `AssertionError`.
    # Classifying every walrus as a suppression therefore reports these two live
    # context managers as defeats, which is why the rule is gated on
    # `_is_suppression_call` rather than applied to any inline binding.
    (
        "walrus of a zero-argument context manager",
        "    with (cs := nullcontext()):\n        assert x != 1",
        True,
    ),
    (
        "walrus of a zero-argument helper",
        "    with (cs := helper.make()):\n        assert x != 1",
        True,
    ),
    (
        "walrus of an unrelated error",
        "    with (cs := contextlib.suppress(ValueError)):\n        assert x != 1",
        True,
    ),
    # A walrus whose value is not even a call cannot be a suppressor -- but it
    # still cannot be *entered*. `with (cs := 1):` raises
    # `TypeError: 'int' object does not support the context manager protocol`
    # while evaluating the header, so the assert never runs and the contract is
    # dead. This row previously read `True`, which was the damaging direction:
    # "not a suppressor" was being read as "live", conflating a live assert with
    # an unreachable one. #390 makes the entered value's own runtime type the
    # question, and an `int` is pinned to non-enterable by its syntax.
    ("walrus of a non-call value", "    with (cs := 1):\n        assert x != 1", False),
    # A bare name in the header is the already-closed alias case, not a walrus.
    ("bare name in the header", "    with cs:\n        assert x != 1", True),
)
