"""Entry-contract rows and their executable witnesses.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support
from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_after_loop_targets import (
    _async_loop_target_is_undecidable,
)
from tests._timed_menu_sentinel_import_context_managers import (
    WALRUS_REBINDING_SHAPES,
    _assert_entry_contract,
)

#: #388 review: the ``global``/``nonlocal`` pair, asked from module scope.
#:
#: Telling those two declarations apart by the scope they target only works if
#: the module path is still covered, and the module path is the one the fix
#: could plausibly have broken: it is what makes #375's `_rebind` shape live.
#: The function-scope table above cannot reach it -- every row there binds the
#: carrier *inside* `outer`, which is exactly the case `global cs` does not
#: reach -- so these rows are written against module-scope sources.
MODULE_SCOPED_DECLARATION_SHAPES = (
    (
        "a module carrier superseded by a global-scoped store is live",
        (
            "import os as cs\n"
            "def _rebind():\n"
            "    global cs\n"
            "    cs = contextlib.nullcontext()\n"
            "_rebind()\n"
        ),
        True,
    ),
    # The mirror: a `nonlocal` inside a nested def names a *function* scope, so
    # it cannot supersede a module-level carrier, and the nested def is a real
    # boundary for it. This is the same pair the function table pins, read from
    # the other side. The inner `def` reads a *function* local rather than the
    # module name, because `nonlocal cs` needs a real enclosing function
    # binding to name -- a `nonlocal` that resolved to the module would be a
    # `SyntaxError`, and a `global` spelling would be the row above.
    (
        "a module carrier is not superseded by a nonlocal-scoped store",
        (
            "import os as cs\n"
            "def _rebind():\n"
            "    cs = os\n"
            "    def inner():\n"
            "        nonlocal cs\n"
            "        cs = contextlib.nullcontext()\n"
            "    inner()\n"
            "_rebind()\n"
        ),
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "prelude", "live"),
    MODULE_SCOPED_DECLARATION_SHAPES,
    ids=[shape[0] for shape in MODULE_SCOPED_DECLARATION_SHAPES],
)
def test_a_module_carrier_reads_global_and_nonlocal_apart(label, prelude, live):
    """``global`` supersedes a module carrier; ``nonlocal`` does not.

    The scope test that keeps a nested store from counting has to let a
    declaration *through* when the declaration really does name the queried
    carrier's namespace, or #375's `_rebind` regresses to `defeated` on a live
    header. Reading the declaration without asking which scope it targets
    breaks it the other way -- on a function-local carrier, where neither
    spelling reaches -- which is the other half of the pair.

    Every row is executed, so `live` is held to CPython rather than asserted
    about the checker.
    """
    source = (
        "import contextlib\n"
        "import os\n"
        + prelude
        + "def outer(x, flag, helper):\n    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [live], (
        f"{label}: expected {[live]}, got {results}. `global` names the module "
        f"and `nonlocal` names an enclosing function, so exactly one of them "
        f"can supersede a module-level carrier."
    )


#: Each non-enterable category, paired with the helper that excludes it.
#:
#: The table above holds the end-to-end verdict to CPython. That is not
#: enough to show the exclusions are load-bearing, and the reason is worth
#: recording: for three of these four shapes the verdict is *already* correct
#: without the decline at all, because `_stores_of` drops the conditional
#: store and the unconditional carrier is what settles the name. So an
#: end-to-end row cannot move when the exclusion is taken away -- it was never
#: the rule deciding that row.
#:
#: What decides them is the decline, and the decline is only observable
#: directly. These rows therefore assert the decline itself: that each helper
#: is what keeps its category from counting as a superseding store, and that
#: removing the helper makes it count. The end-to-end consequence is pinned
#: separately, and only where it exists, by the control rows.
NON_ENTERABLE_EXCLUSION_ROWS = (
    (
        "the known-None with exclusion keeps its store out of the decline",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext() as cs:\n            pass\n",
        "_with_binds_a_known_non_enterable",
        False,
    ),
    (
        "the starred-target exclusion keeps its store out of the decline",
        "    import os as cs\n    if flag:\n        *cs, = (contextlib.nullcontext(),)\n",
        "_target_is_starred",
        False,
    ),
    (
        "the nested-scope exclusion keeps its store out of the decline",
        "    import os as cs\n    if flag:\n        def inner():\n            cs = contextlib.nullcontext()\n",
        "_store_is_in_scope",
        True,
    ),
)


def _decline_fires_for(source):
    """Does the #388 decline fire on the ``with cs:`` header in `source`?"""
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    header = next(
        node
        for node in function.body
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Name) and item.context_expr.id == "cs"
            for item in node.items
        )
    )
    index = function.body.index(header)
    bindings, _raw = support._store_bindings(function, set())
    orders = {
        id(statement): support._binding_order(function, statement)
        for entries in bindings.values()
        for statement, _value, _conditional in entries
    }
    return support._carrier_may_have_been_superseded(
        bindings.get("cs", ()),
        orders,
        index,
        support._bound_names(tree, function),
        function,
        "cs",
    )


@pytest.mark.parametrize(
    ("label", "body", "helper", "neutralised"),
    NON_ENTERABLE_EXCLUSION_ROWS,
    ids=[row[0] for row in NON_ENTERABLE_EXCLUSION_ROWS],
)
def test_each_non_enterable_exclusion_is_load_bearing(
    label, body, helper, neutralised, monkeypatch
):
    """Neutralising an exclusion must make its store count in the decline.

    The helper is replaced with one that reports the opposite of what it
    normally reports, which turns its exclusion off. The decline has to go from
    silent to firing, because a store that cannot bind an enterable value must
    not be read as a stale carrier.

    The value that removes an exclusion is not the same for every helper, and
    working that out is part of what these rows pin. Two of the helpers are
    consulted positively -- "is this store in the same scope", "is the name the
    starred target" -- so `True` removes the exclusion for the scope test and
    `False` removes it for the starred test, because the caller negates the
    scope answer and not the starred one. The `with` helper is a negative test
    whose answer is negated by its caller, so `False` removes that exclusion
    too. Each row's value is written in the table rather than derived from a
    rule, because deriving it is what got this wrong twice while writing it.

    The first assertion also matters: it pins that the row is *silent* to begin
    with. Without it, a shape whose decline already fired would satisfy the
    second assertion for the wrong reason, and the row would stop testing the
    exclusion it names.
    """
    source = (
        "import contextlib\n"
        "def outer(x, flag, helper):\n" + body + "    with cs:\n        assert x != 1\n"
    )
    assert _decline_fires_for(source) is False, (
        f"{label}: the decline already fires for this row, so the exclusion it "
        f"names is not what keeps the store out."
    )
    monkeypatch.setattr(support, helper, lambda *args, **kwargs: neutralised)
    assert _decline_fires_for(source) is True, (
        f"{label}: neutralising `{helper}` did not make the store count, so "
        f"the exclusion is not load-bearing -- the decline is answering for "
        f"some other reason and this row is not testing it."
    )


#: #388: the shapes this repair does **not** reach, and why.
#:
#: These three also read `defeated` against a live assert, and they read that
#: way on the pre-#375 base `0529c8a` as well -- so they are a **separate
#: pre-existing defect**, not part of the #388 regression, and closing #388
#: does not close them.
#:
#: The ordered decline in :func:`_carrier_may_have_been_superseded` does fire
#: on all three: each does have an unconditional carrier followed by a
#: conditional non-carrier store. They still end up `defeated` because a
#: *different* rule answers first -- `_aliased_suppressions` records the name
#: as :data:`AMBIGUOUS_SUPPRESSOR` (#308 criterion 1: a name bound by several
#: stores cannot be resolved by source order, so it is reported as a
#: suppression), and `_is_suppressing_with` turns that into a defeat. So the
#: carrier decline is silent here for a reason outside its own scope.
#:
#: They are kept in this file rather than dropped so the boundary is explicit
#: instead of being rediscovered later: a future change to the #308 ambiguity
#: rule that reads a total branch rebinding as settled will move these, and
#: the move has to be argued for on its own matrix.
FUNCTION_CARRIER_SUPERSESSION_LIMIT_SHAPES = (
    (
        "an if/else after a function-scope carrier is still declined",
        "    import os as cs\n    if flag:\n        cs = contextlib.nullcontext()\n    else:\n        cs = contextlib.nullcontext()\n",
        True,
    ),
    (
        "an if/elif/else after a function-scope carrier is still declined",
        "    import os as cs\n    if flag:\n        cs = contextlib.nullcontext()\n    elif flag:\n        cs = contextlib.nullcontext()\n    else:\n        cs = contextlib.nullcontext()\n",
        True,
    ),
    (
        "a conditional del-then-store after a function-scope carrier is still declined",
        "    import os as cs\n    if flag:\n        del cs\n        cs = contextlib.nullcontext()\n",
        True,
    ),
    # A `with` that binds the same name from two items leaves the store table
    # with two entries for it, so #308 reads the name as ambiguous and answers
    # before the carrier decline is consulted. `with nullcontext() as cs, CM()
    # as cs:` really does leave `cs` enterable -- the last item wins -- so
    # `defeated` is the wrong end-to-end answer here, and it is the wrong
    # answer on `ed9d9b0` too. Held here for the same reason as the rows above:
    # the #388 repair must not widen it, and closing it belongs to #308.
    (
        "a with rebinding the name from two items is still declined",
        (
            "    import os as cs\n"
            "    if flag:\n"
            "        with contextlib.nullcontext() as cs, CM() as cs:\n"
            "            pass\n"
        ),
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    FUNCTION_CARRIER_SUPERSESSION_LIMIT_SHAPES,
    ids=[shape[0] for shape in FUNCTION_CARRIER_SUPERSESSION_LIMIT_SHAPES],
)
def test_a_function_carrier_supersession_limit_is_still_declined(label, body, live):
    """Pin the known limit of this repair, so it cannot widen silently.

    With `flag=True` the store runs on every path and ``with cs:`` succeeds on
    every row, so `defeated` is the **wrong** answer for all of them -- they
    drop a live pinned contract. They are held to `defeated` here anyway,
    because that is what the analyzer actually says; a test asserting the
    *right* answer would be a failing test rather than a pin. The `live`
    column is what keeps that claim honest: it is read from the interpreter
    rather than assumed, and it is per-row because a `with` that binds the
    same name twice leaves a different value behind than the store forms do.

    The row is not claiming the verdict is correct. It pins **where this
    repair stops**: the #308 ambiguity rule answers these shapes first, so
    nothing here can widen the carrier decline without the #308 matrix moving
    too. #388 repairs the ten regression rows only; these three are tracked
    separately rather than absorbed, because they were already wrong on
    `0529c8a`, the base of the #375 branch.

    The interpreter half runs first, and it is the half that matters: it holds
    CPython to the claim that the assert is live, so the table's own comment --
    that this is a real defect rather than a quibble about the checker -- is
    verified rather than asserted.
    """
    source = (
        "import contextlib\n"
        "class CM:\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *exc):\n"
        "        return False\n"
        "def outer(x, flag, helper):\n" + body + "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False], (
        f"expected the current (wrong) [False] for this shape, got {results}. "
        f"CPython disagrees -- the assert is live -- so whatever eventually "
        f"fixes these rows will land here as a deliberate change to a pinned "
        f"answer, argued on its own matrix, not as a side effect of some "
        f"other repair."
    )


@pytest.mark.parametrize(
    ("label", "rebind", "is_async", "second_assert_live"),
    WALRUS_REBINDING_SHAPES,
    ids=[shape[0] for shape in WALRUS_REBINDING_SHAPES],
)
def test_a_walrus_alias_is_retired_by_every_binding_form(
    label, rebind, is_async, second_assert_live
):
    """A carried suppressor must not outlive the name being rebound.

    Each row binds a suppressor through a walrus, then rebinds that name in a
    different syntactic form, then enters it. The first assert is swallowed by
    the walrus-bound suppressor on **every** row -- that is the retirement
    claim, and it is ``False`` throughout.

    The second assert divides, and the division is the point. It used to be
    pinned to a single ``[False, True]`` for all eleven rows, which was wrong
    twice over: the two async rows are not even executable (they sit inside a
    plain ``def`` and call an undefined ``gen()``), and no row was ever run --
    there is no ``exec``, ``compile`` or ``eval`` in the body, only
    ``ast.parse`` and a call to the checker. A table that only reads the
    checker cannot notice when its claim and the checker's answer are wrong
    together.

    Executed on CPython, the rows split cleanly in two:

    * ``second_assert_live`` -- the rebind leaves ``cs`` bound to something
      enterable, so ``with cs:`` succeeds and the assert is a real contract.
      That covers every row whose right-hand side is a *call*:
      ``cs = contextlib.nullcontext()``, ``cs, other = (nullcontext(), 2)``,
      ``[cs] = [nullcontext()]``, the annotated store, and
      ``for cs in (nullcontext(),):``. Destructuring is not special here --
      the name still receives a real context manager, just one element out of
      a container. The async loop row is here for a second reason, and it is
      the one row whose entry is genuinely undecidable: the rule cannot read
      the element type out of an arbitrary async iterable, so it declines
      rather than guessing, and this row pins that choice. See
      :func:`_async_loop_target_is_undecidable` for the evidence.
    * ``not second_assert_live`` -- the rebind leaves ``cs`` holding something
      that cannot be entered, so the header raises before the body runs and
      the assert is unreachable. ``with nullcontext() as cs:`` binds
      ``__enter__``'s return value, which is ``None``; ``except E as cs:``
      and ``del cs`` unbind the name; ``async for cs in gen()`` binds the loop
      variable, an ``int``.

    The first half of each row is the retirement contract and the second is
    the entry contract, and they are independent: both are asserted, and the
    entry half is checked against the interpreter rather than against the
    analyzer it is supposed to police.
    """
    source = (
        f"{'async def' if is_async else 'def'} outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    async def gen():\n"
        "        yield 1\n"
        "    async def agen():\n"
        "        yield nullcontext()\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + rebind + "\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    if is_async and "async for" in rebind:
        _async_loop_target_is_undecidable(label, source)
    else:
        _assert_entry_contract(label, source, is_async, second_assert_live)
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [False, second_assert_live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. Both are "
        f"fixed by what the rebind leaves bound to the name."
    )


@pytest.mark.parametrize(
    ("label", "source", "expected"),
    [
        (
            "#390 a lambda rebound over a carried suppressor is not enterable",
            (
                "import contextlib\n"
                "def outer(x, flag, helper):\n"
                "    with (cs := contextlib.suppress(AssertionError)):\n"
                "        pass\n"
                "    cs = lambda: None\n"
                "    with cs:\n"
                "        assert x != 1\n"
            ),
            False,
        ),
        (
            # The same lambda bound *in* the header. #390 filed both rows and
            # the first repair covered only this one's assignment spelling: a
            # `NamedExpr` header never becomes a store entry, so the readable
            # set that admits `ast.Lambda` was never consulted for it, and the
            # assert was left certified as load-bearing while `with cs:`
            # raised `TypeError` before the body ran.
            "#390 a lambda bound in the header itself is not enterable",
            (
                "import contextlib\n"
                "def outer(x, flag, helper):\n"
                "    with (cs := lambda: None):\n"
                "        assert x != 1\n"
            ),
            False,
        ),
        (
            "#394 a body import shadows a module-level suppressor alias",
            (
                "import contextlib as fake\n"
                "def outer(x, flag, helper):\n"
                "    import json as fake\n"
                "    with fake.suppress(AssertionError):\n"
                "        assert x != 1\n"
            ),
            False,
        ),
        (
            "#426 a starred unpack in the with header enters a tuple",
            (
                "import contextlib\n"
                "def outer(x, flag, helper):\n"
                "    cs = [contextlib.nullcontext(), contextlib.suppress(AssertionError)]\n"
                "    with (*cs,):\n"
                "        assert x != 1\n"
            ),
            False,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and value.startswith("#") else None,
)
def test_a_header_that_cannot_be_entered_defeats_the_assert(label, source, expected):
    """A ``with`` header that raises before the body leaves the assert unreachable.

    Each row binds a value that cannot implement the context manager protocol
    in the header itself -- a function object, an attribute the module does
    not have, and a tuple. Entering any of them raises ``TypeError`` or
    ``AttributeError`` while the header is evaluated, so the assert under it
    never runs.

    The analyzer answered ``True`` for all three: a disarmed contract
    certified as load-bearing, which is the damaging direction. The verdicts
    are checked against the runtime contract rather than against the
    analyzer's own opinion, so a row cannot pass by the checker and the claim
    being wrong together.
    """
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [expected], f"{label}: expected {[expected]}, got {results}."

    # Hold CPython to the same row, so the expected verdict is measured rather
    # than asserted about. An unreachable row must raise something that is
    # *not* an AssertionError.
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1, True, None)
    except AssertionError:
        if not expected:
            raise AssertionError(
                f"{label}: the assert fired, so the header was enterable and "
                f"the contract is live. The row claims it is dead."
            ) from None
    except (TypeError, AttributeError, NameError, UnboundLocalError):
        if expected:
            raise AssertionError(
                f"{label}: the header raised instead of running the body, so "
                f"the assert is unreachable. The row claims it is live."
            ) from None
    else:
        raise AssertionError(
            f"{label}: the fixture returned normally, so the assert was "
            f"swallowed rather than reachable."
        )


def test_a_live_header_is_not_read_as_dead_by_the_unenterable_rules():
    """The controls that keep #390/#394/#426 from over-correcting.

    Each of the three rules keys off a shape that is also the shape of a
    genuinely live header:

    * a name bound by ``from M import N as cs`` may be a real context manager;
    * a ``cs = <call>`` store may return one; and
    * a name read in a ``with`` header may be perfectly enterable.

    A rule that answered "dead" for the whole family would drop each of these
    real contracts, so they are pinned here as live.
    """
    live_rows = (
        (
            "a from-import binding a real context manager stays live",
            (
                "def outer(x, flag, helper):\n"
                "    from tests._import_from_carrier_support import ctx as cs\n"
                "    with cs:\n"
                "        assert x != 1\n"
            ),
        ),
        (
            "a store of a real context manager stays live",
            (
                "import contextlib\n"
                "def outer(x, flag, helper):\n"
                "    cs = contextlib.nullcontext()\n"
                "    with cs:\n"
                "        assert x != 1\n"
            ),
        ),
        (
            "a lambda that RETURNS a context manager is not read as one",
            (
                "import contextlib\n"
                "def outer(x, flag, helper):\n"
                "    make = lambda: contextlib.nullcontext()\n"
                "    with make():\n"
                "        assert x != 1\n"
            ),
        ),
    )
    for label, source in live_rows:
        tree = ast.parse(source)
        function = tree.body[-1]
        asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
        results = [_is_enforced(function, node, tree) for node in asserts]
        assert results == [True], (
            f"{label}: expected [True] -- the header is enterable, so the "
            f"assert is live -- got {results}. A rule reading this family as "
            f"dead would drop a real pinned contract."
        )


#: #350. Which nesting forms bind the *enclosing* function's local, and which
#: bind their own.
#:
#: A ``match`` capture inside a nested ``def``/``lambda``/``class`` body runs
#: in that body's namespace. Only a capture in the function's own scope retires
#: the carried suppressor. Every row here is executed, so "does not retire" is
#: backed by CPython swallowing the assert rather than by the analyzer's
#: opinion.
CAPTURE_SCOPE_BOUNDARY_ROWS = (
    # The filed shape. `class C:` opens a new namespace; the capture binds
    # `C.cs`, and `outer`'s `cs` is still the suppressor, so the second assert
    # is swallowed and the verdict is False.
    #
    # The clause is the IRREFUTABLE `case cs:` from the issue's reproduction.
    # That spelling matters. The first draft of this row used the *refutable*
    # `case nullcontext() as cs:`, and it passed against the unfixed support
    # module for the wrong reason: a refutable capture over an opaque
    # `helper()` is already declined (#369), so the buggy path and the correct
    # one both answered `False` and the row could not see the bug at all.
    # Measured on unfixed master: `case cs:` -> analyzer `True` against runtime
    # `False`; `case nullcontext() as cs:` -> `False` against `False`, i.e.
    # accidentally correct. Only the irrefutable form discriminates.
    (
        "a capture in a class body binds the class, not the function",
        ("    class C:\n        match helper():\n            case cs:\n                pass\n"),
        False,
        False,
    ),
    # Same, for a plain import rather than a capture -- the other walk that
    # used the same boundary (`_own_imports`).
    (
        "an import in a class body binds the class, not the function",
        "    class C:\n        import os as cs\n",
        False,
        False,
    ),
    # Controls: these must NOT change. A nested function is the pre-existing
    # correct behaviour, so a fix that simply stopped resolving captures
    # anywhere would fail this row.
    (
        "CONTROL a capture in a nested function does not bind the function",
        ("    def inner():\n        match helper():\n            case cs:\n                pass\n"),
        False,
        False,
    ),
    # The row that would break if the boundary were over-widened: a capture in
    # the function's OWN body does retire the suppressor, and `with cs:`
    # successfully enters the nullcontext, so the assert is live and the
    # correct verdict is `True`.
    #
    # This row is unchanged by the fix (it answered `True` before and after),
    # which is exactly its purpose: a repair that added `ClassDef` to the
    # boundary but also broke function-body captures would go red here.
    #
    # Note the difference from the sibling refutable rows elsewhere in this
    # file. `case cs:` is irrefutable, so this is decidable and `True` is
    # sound. The refutable spelling of the same shape is declined instead --
    # that is #369, filed and open, and it is why those rows pin `False`.
    (
        "CONTROL a capture in the function body does retire the suppressor",
        "    match helper():\n        case cs:\n            pass\n",
        True,
        True,
    ),
)
