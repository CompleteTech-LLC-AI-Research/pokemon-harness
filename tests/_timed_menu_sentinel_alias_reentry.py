"""Alias re-entry and rebinding witnesses.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced

#: #324: a walrus in a ``with`` header binds the name for the rest of the
#: enclosing scope, so a *later* header that re-enters that name enters the
#: same suppressor. ``NamedExpr`` is not an ``ast.Assign``, so the binding was
#: never recorded and the re-entering assert was reported live -- a defeated
#: assert certified as load-bearing, which is the damaging direction.
#:
#: The rows below compare the full list of verdicts rather than reducing it
#: with ``all()``. Each fixture holds more than one assert whose verdicts are
#: *not* all the same, and ``all()`` collapses such a fixture to its first
#: verdict -- so a wrong second verdict passed the shipped table. That harness
#: bug is itself fixed here; the exact comparison is what keeps these rows
#: honest.
#
#: Every fixture is two- or three-assert on purpose: the first assert is the
#: one already covered by the single-header walrus rows, and the later
#: assert(s) are the ones this issue is about.
WALRUS_REENTRY_SHAPES = (
    # The defect itself. `cs` is still the suppressor at the second header, so
    # its assert is swallowed too. Both are defeated; the first assert being
    # already correct means `all()` would have passed while the second was
    # wrong.
    (
        "a walrus-bound suppressor is re-entered by a later header",
        (
            "    with (cs := suppress(AssertionError)):\n        assert x != 1\n"
            "    with cs:\n        assert x != 2"
        ),
        [False, False],
    ),
    # #324 criterion 1: the same must hold when the walrus is not written at
    # the top level. Inside an `if` the bind happens on one path only, but the
    # later header re-enters whatever was bound, and executed that is the
    # suppressor.
    (
        "a walrus inside an if is re-entered by a later header",
        (
            "    if flag:\n"
            "        with (cs := suppress(AssertionError)):\n"
            "            assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # ... and inside a `try`.
    (
        "a walrus inside a try is re-entered by a later header",
        (
            "    try:\n"
            "        with (cs := suppress(AssertionError)):\n"
            "            assert x != 1\n"
            "    except Exception:\n"
            "        pass\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # #324 criterion 2: a later rebind must WIN over the carried walrus
    # value. `nullcontext()` does not suppress, so the second assert really is
    # live and must be reported enforced. This is the row that a naive
    # "record every walrus" repair gets wrong -- it is exactly the
    # over-breadth that blocked #323.
    (
        "a later nullcontext rebind wins over a carried walrus value",
        (
            "    with (cs := suppress(AssertionError)):\n        assert x != 1\n"
            "    cs = nullcontext()\n"
            "    with cs:\n        assert x != 2"
        ),
        [False, True],
    ),
    # ... the same through a helper, which is the `#308` rebind family.
    (
        "a later helper rebind wins over a carried walrus value",
        (
            "    with (cs := suppress(AssertionError)):\n        assert x != 1\n"
            "    cs = helper.make()\n"
            "    with cs:\n        assert x != 2"
        ),
        [False, True],
    ),
    # ... and a rebind nested in an `if`, which is conditional.
    (
        "a later rebind inside an if wins over a carried walrus value",
        (
            "    with (cs := suppress(AssertionError)):\n        assert x != 1\n"
            "    if flag:\n"
            "        cs = nullcontext()\n"
            "    with cs:\n        assert x != 2"
        ),
        [False, True],
    ),
    # #324 criterion 3: a walrus whose value is not a suppressor never yields
    # a defeated verdict, even when the name is re-entered. This is the
    # cleanest canary -- nothing rebinds `cs`, so the suppressor value simply
    # must never have been attached to it.
    (
        "a walrus of a non-suppressor is never reported defeated when re-entered",
        (
            "    with (cs := nullcontext()):\n        assert x != 1\n"
            "    with cs:\n        assert x != 2"
        ),
        [True, True],
    ),
    # #348: a store whose right-hand side is the name it binds. The alias walk
    # stands on the very store it is resolving, so the suppressor it already
    # holds is never seen and both headers reported a SWALLOWED assert as
    # live. Both are the damaging direction, and neither was pinned before.
    (
        "a walrus aliasing the very name it binds re-enters",
        (
            "    cs = contextlib.suppress(AssertionError)\n"
            "    with (cs := cs):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # The same self-alias written as an ordinary store rather than a walrus.
    # It resolves on a different path, so pinning only the walrus spelling
    # would leave this one unpinned.
    (
        "a plain store aliasing the very name it binds re-enters",
        (
            "    cs = contextlib.suppress(AssertionError)\n"
            "    cs = cs\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # A rebind of the aliased name *after* the header. The interpreter has not
    # run it when the header reads the name, so the suppressor is still in
    # force and the assert is swallowed -- but a whole-function "last binding"
    # view resolves the header to the nullcontext instead, which reads as live.
    # The re-entering `with first:` is the control: by then the rebind HAS
    # run, so that assert really is live and must stay reported so.
    (
        "an alias read before its own later rebind re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    with (cs := first):\n"
            "        assert x != 1\n"
            "    first = nullcontext()\n"
            "    with first:\n"
            "        assert x != 2"
        ),
        [False, True],
    ),
    # A two-hop chain whose last link is rebound after the header that reads
    # it. Same failure as the row above, one link further from the store, so
    # a rule that only special-cases the direct name would still look right.
    (
        "a two-hop alias read before its last link is rebound re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    second = first\n"
            "    with (cs := second):\n"
            "        assert x != 1\n"
            "    first = nullcontext()\n"
            "    with first:\n"
            "        assert x != 2"
        ),
        [False, True],
    ),
    # #348 follow-on: the store that HAS run is not always the last one
    # `ast.walk` visits. `ast.walk` is breadth-first, so a store written
    # directly in the body is recorded before a store nested in an EARLIER
    # top-level statement. Picking the last recorded entry therefore returns
    # the nested one, which is stale -- and the verdict it produces is the
    # damaging one, a live contract reported as defeated.
    #
    # Here `if flag:` does not run, so `first` is the `nullcontext()` below
    # it. `nullcontext()` is a working context manager, so the assert really
    # runs and is live -- it must be reported enforced. Resolving the chain to
    # the nested `suppress(...)` instead makes it look swallowed.
    #
    # The second assert re-enters `first` directly rather than through the
    # chain, as the control: the same stale pick governs it, so a rule that
    # ignored ordering entirely -- returning `entries[-1]` -- would report
    # that one defeated too, and the fixture would read [False, False].
    (
        "a two-hop alias resolves to the store that actually ran, not the last walked",
        (
            "    if flag:\n"
            "        first = contextlib.suppress(AssertionError)\n"
            "    first = contextlib.nullcontext()\n"
            "    second = first\n"
            "    with (cs := second):\n"
            "        assert x != 1\n"
            "    with second:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # The same mis-ordering reached through a one-hop alias rather than a
    # two-hop chain, so pinning only the chain spelling would leave the
    # direct one unpinned. Both asserts are live for the same reason as the
    # row above, and both are certified only by picking the store that ran.
    (
        "an alias read picks the store that ran even when a nested store was walked later",
        (
            "    if flag:\n"
            "        first = contextlib.suppress(AssertionError)\n"
            "    first = contextlib.nullcontext()\n"
            "    alias = first\n"
            "    with (cs := alias):\n"
            "        assert x != 1\n"
            "    with first:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # Two walruses of the same name in sequence: the later one supersedes the
    # earlier, so the third assert runs under a `nullcontext` and is live.
    (
        "a later walrus of the same name supersedes the earlier one",
        (
            "    with (cs := suppress(AssertionError)):\n        assert x != 1\n"
            "    with (cs := nullcontext()):\n        assert x != 2\n"
            "    with cs:\n        assert x != 3"
        ),
        [False, True, True],
    ),
    # The bind does not have to be written in a `with` header. An assignment
    # expression is a binding wherever it appears, and the three rows below
    # are what separates recording a `NamedExpr` from harvesting the walruses
    # out of `with` headers specifically. Executed, each of these really does
    # swallow, so reporting them live is the damaging direction.
    (
        "a walrus in a plain assignment is re-entered by a later header",
        ("    y = (cs := suppress(AssertionError))\n    with cs:\n        assert x != 1"),
        [False],
    ),
    (
        "a walrus in a comprehension is re-entered by a later header",
        (
            "    rows = [(cs, v) for v in items if (cs := suppress(AssertionError))]\n"
            "    with cs:\n        assert x != 1"
        ),
        [False],
    ),
    # A rebind in the *same* block as the walrus supersedes it. Executed, the
    # first assert is swallowed by the suppressor that was current when the
    # `with` was entered, and the second runs under the rebound
    # `nullcontext` and is live.
    (
        "a rebind in the same block supersedes a walrus of the same name",
        (
            "    with (cs := suppress(AssertionError)):\n"
            "        cs = nullcontext()\n"
            "        assert x != 1\n"
            "    with cs:\n        assert x != 2"
        ),
        [False, True],
    ),
    # #324 criterion 4, and the three rows that actually pin
    # `_walrus_is_conditional`.
    #
    # Every row above reaches the re-entry through a walrus in a *top-level*
    # `with` header, so a walrus's conditionality never has to be decided: it
    # is unconditional on every path, and the two competing walruses in the
    # supersession row are settled by source order before ambiguity is ever
    # consulted. The mutation "force every walrus unconditional" therefore
    # leaves this whole table green.
    #
    # These three do decide it. Here the walrus is under an `if`, so it binds
    # `cs` on some paths only -- exactly the case
    # `_walrus_skipped_by_a_branch` exists to recognise. The competing
    # `cs = nullcontext()` is under its own `if`, so it too is conditional.
    # Two conditional bindings of one name cannot be ordered: on the `flag`
    # path the `nullcontext` wins and the assert is live, and on the `not
    # flag` path the suppressor is still bound. Which one is in force is
    # undecidable, so the name is ambiguous and the safe reading is "a
    # suppressor may be in force" -- the middle assert is reported defeated.
    #
    # Read the mutation the other way and it is the whole point: force the
    # walrus unconditional and source order picks the `nullcontext` as the
    # winner, so the live assert flips to enforced. That is the damaging
    # direction -- a real contract dropped from the sentinel's view -- and it
    # is the defect #324 is filed about, reached by a different road.
    #
    # Executed with `flag=True` the middle assert raises `AssertionError`
    # (nullcontext does not suppress) while the first is swallowed; with
    # `flag=False` neither `if` body runs and the assert does not execute at
    # all. Both were run, unmodified, before these rows were written.
    (
        "a conditional walrus does not outrank a later conditional store",
        (
            "    if flag:\n"
            "        with (cs := suppress(AssertionError)):\n"
            "            assert x != 1\n"
            "    if flag:\n"
            "        cs = nullcontext()\n"
            "        assert x != 2\n"
            "    with cs:\n"
            "        assert x != 3"
        ),
        [True, False, False],
    ),
    # ... the same with the conditional walrus written as a loop rather than
    # an `if`. `for` is in `_walrus_skipped_by_a_branch`'s set for the same
    # reason `if` is, and a rule that recognised only one of the two would be
    # half a rule.
    (
        "a walrus under a loop does not outrank a later conditional store",
        (
            "    for item in items:\n"
            "        with (cs := suppress(AssertionError)):\n"
            "            assert x != 1\n"
            "    if flag:\n"
            "        cs = nullcontext()\n"
            "        assert x != 2\n"
            "    with cs:\n"
            "        assert x != 3"
        ),
        [True, False, False],
    ),
    # ... and under a `try`. The three blocks are the complete set that can
    # skip a walrus, so all three are pinned: dropping any one of them from
    # `_walrus_skipped_by_a_branch` turns exactly the matching row red.
    (
        "a walrus under a try does not outrank a later conditional store",
        (
            "    try:\n"
            "        with (cs := suppress(AssertionError)):\n"
            "            assert x != 1\n"
            "    except Exception:\n"
            "        pass\n"
            "    if flag:\n"
            "        cs = nullcontext()\n"
            "        assert x != 2\n"
            "    with cs:\n"
            "        assert x != 3"
        ),
        [True, False, False],
    ),
    # #333: the binding takes its value from a NAME that is already bound to a
    # suppressor, rather than from an inline call. Executed, `cs` *is* `base`,
    # so both asserts are swallowed. The rule recorded the bare `base` node,
    # `_is_readable_suppressor` accepts only an `ast.Call`, and both headers
    # were reported live -- a disarmed contract certified as load-bearing.
    (
        "a walrus of a pre-bound suppressor name re-enters",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    with (cs := base):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # Two hops, so a rule that follows exactly one link is caught. Both links
    # are ordinary unconditional stores, so nothing is ambiguous here.
    (
        "a walrus of a two-hop suppressor alias re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    second = first\n"
            "    with (cs := second):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # The from-import spelling: the suppressor is built from the bare name
    # `suppress`, so the chain has to terminate at a suppressor the module
    # spells without a dotted prefix. The walrus still binds the *instance*:
    # `with (cs := suppress):` would enter the factory object itself, which
    # raises `TypeError` on `__enter__` and never reaches the assert, so it is
    # not this shape and is not pinned as one.
    (
        "a walrus of a from-imported suppressor re-enters",
        (
            "    base = suppress(AssertionError)\n"
            "    with (cs := base):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # The binding is not confined to a `with` header. An assignment expression
    # binds a name wherever it appears, and #324 already taught the rule to
    # harvest them from assignments and comprehensions -- but it classified
    # each by its own right-hand side, so a pre-bound NAME was still unreadable
    # in all three of those positions.
    (
        "a walrus of a pre-bound name in a plain assignment re-enters",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    y = (cs := base)\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        [False],
    ),
    (
        "a walrus of a pre-bound name in a comprehension re-enters",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    rows = [(cs, v) for v in items if (cs := base)]\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # A conditional store of the alias. `flag` gates the store, so on the
    # `not flag` path `cs` is never bound and the header raises `NameError` --
    # loudly. On the `flag` path it really is the suppressor, so the safe
    # reading is the same one `_resolve_bindings` already gives an ambiguous
    # name.
    (
        "a conditional store of a pre-bound suppressor name re-enters",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    if flag:\n"
            "        cs = base\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # The name the walrus aliases is bound TWICE, so the value in force is the
    # LATER store. Reading the first binding instead resolves the alias to the
    # ordinary call, leaves it unreadable, and reports a swallowed assert as
    # live -- a supersession this module already models for every other store.
    (
        "a walrus of a name rebound to a suppressor re-enters",
        (
            "    first = helper.make()\n"
            "    first = contextlib.suppress(AssertionError)\n"
            "    with (cs := first):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # The same supersession in the other direction, so a rule that simply
    # reaches for the most recent *suppressor* is caught as well: the suppressor
    # is the earlier store and the ordinary call is what is live.
    (
        "a walrus of a name rebound to a non-suppressor re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    first = contextlib.nullcontext()\n"
            "    with (cs := first):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # --- controls: the chain must not manufacture a suppressor ---------------
    # The name the walrus binds is bound to a context manager that does NOT
    # swallow. Following the alias must reach that value and stop, not conclude
    # "somewhere in this chain there was a call" and report a defeat.
    (
        "a walrus of a pre-bound non-suppressor name stays live",
        (
            "    base = contextlib.nullcontext()\n"
            "    with (cs := base):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # An unbound name is not a suppressor. The chain runs out of entries, the
    # value stays unreadable, and the assert stays live.
    (
        "a walrus of an unbound name stays live",
        (
            "    with (cs := helper.make()):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # A cycle must terminate rather than recurse. The mutually referential
    # stores live in a nested frame so the outer scope is unaffected; the point
    # of the row is that the run reaches a verdict at all.
    (
        "a walrus whose alias chain cycles terminates",
        (
            "    def build():\n"
            "        first = second\n"
            "        second = first\n"
            "        return first\n"
            "    with (cs := build()):\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
    # A later store supersedes the walrus exactly as it supersedes an ordinary
    # assign, so substituting the aliased value must not make the carried
    # binding outlive its replacement.
    (
        "a walrus of a pre-bound name after a superseding store",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    with (cs := base):\n"
            "        assert x != 1\n"
            "    cs = contextlib.nullcontext()\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, True],
    ),
    # --- #328: the same alias chain WITHOUT a walrus -----------------------
    #
    # These three rows are not #333. They were filed as a separate issue and
    # were blind in exactly the same way -- a swallowed assert certified as
    # load-bearing -- because a bare `with alias:` records a `Name` that
    # `_is_readable_suppressor` rejects. They are pinned HERE because the fix
    # that repairs them is the fix that repairs #333: the whole-function
    # pre-pass in `_raw_store_values` hands the ordinary alias path the same
    # complete table the walrus path needs, so both resolve together or not at
    # all.
    #
    # They are stated as rows rather than left to the PR narrative so the
    # side-effect fix is pinned by a test. A future change that narrows the
    # dereference back to walrus headers alone fails these.
    (
        "a plain alias of a pre-bound suppressor re-enters",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    alias = base\n"
            "    with alias:\n"
            "        assert x != 1\n"
            "    with alias:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # Two hops. Note this is NOT the discriminating row a single-hop mutation
    # would catch, and the difference matters: the dereference runs at the
    # *store site*, so `second = first` is resolved to the suppressor the
    # moment it is recorded, and the header then reads an already-resolved
    # value. The walrus path resolves at the *header*, one link further down
    # the chain, which is why the walrus two-hop row is the one a single-hop
    # mutation breaks. This row still pins #328: it fails outright if the
    # ordinary alias path resolves nothing at all.
    (
        "a plain two-hop suppressor alias re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    second = first\n"
            "    with second:\n"
            "        assert x != 1\n"
            "    with second:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # #328 acceptance criterion 4 names a THREE-link chain explicitly
    # ("a = suppress(...); b = a; c = b"), and the shipped table only pinned
    # two. Three links is past the point where a hand-written "follow one or
    # two" rule would still look correct, so the row is here to make the bound
    # a tested property rather than an accident of how far the fixture reached.
    #
    # The walrus twin of this shape already exists two rows up; this one is the
    # ordinary `with` spelling, which resolves at the store site instead of the
    # header and so is reached by a different call path.
    (
        "a plain three-hop suppressor alias re-enters",
        (
            "    first = contextlib.suppress(AssertionError)\n"
            "    second = first\n"
            "    third = second\n"
            "    with third:\n"
            "        assert x != 1\n"
            "    with third:\n"
            "        assert x != 2"
        ),
        [False, False],
    ),
    # The control for the ordinary path: following the chain must stop at a
    # context manager that does not suppress, rather than report a defeat
    # because a suppressor appeared somewhere earlier in the chain.
    (
        "a plain alias of a pre-bound non-suppressor stays live",
        (
            "    base = contextlib.nullcontext()\n"
            "    alias = base\n"
            "    with alias:\n"
            "        assert x != 1\n"
            "    with alias:\n"
            "        assert x != 2"
        ),
        [True, True],
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    WALRUS_REENTRY_SHAPES,
    ids=[row[0] for row in WALRUS_REENTRY_SHAPES],
)
def test_a_walrus_bound_alias_reaches_the_headers_that_re_enter_it(label, body, expected):
    """A walrus bind is a real binding, and a later rebind still supersedes it.

    #323 tried to close the re-entry half of this and could not without
    re-opening the supersession half: carrying the walrus value forward
    unconditionally made a stale suppressor outlive a later ``cs =
    nullcontext()``, so a *live* assert was reported defeated on three
    separate rows. The shipped rule records a ``NamedExpr`` through the same
    per-index binding machinery as an ordinary store, so both halves hold at
    once.
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
        f"{label}: expected verdicts {expected}, got {results}. Every assert "
        f"is compared individually -- `all()` would collapse this fixture to "
        f"its first verdict and hide a wrong later one."
    )
