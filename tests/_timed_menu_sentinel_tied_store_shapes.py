"""Literal tied-store shapes retained for loop cases.

Actual test functions and literal cases from the original collector.
"""

#: #367: two stores that share a top-level statement are *tied*, and the
#: original resolver broke the tie by walk position -- a question the source
#: does not answer. `max` returns the first maximal entry, so a `for`/`else`
#: pair resolved to whichever branch the walk reached first, which is fixed
#: regardless of which branch actually runs.
#:
#: The `expected` column is the exact per-assert verdict list, never
#: ``all(...)``: collapsing it to the first verdict is what let the original
#: defect ship, and the whole point of a tie rule is that *each* store in the
#: tie is a candidate the resolver must decline.
TIED_STORE_ROWS = (
    # #395. This row's comment used to claim that `items == []` runs the `else`
    # and binds a `nullcontext` (assert FIRES) while `items == [1]` runs the
    # body and binds a `suppress` (assert swallowed), so that "the interpreter
    # disagrees with itself" and the name had to be declined.
    #
    # That was false. A `for`'s `else` runs when the loop completes *without
    # `break`* -- for any iteration count, including zero. It is not the "the
    # loop was empty" branch:
    #
    #     items == []   ->  else runs
    #     items == [1]  ->  body runs, THEN the else runs
    #
    # So `first` is the `nullcontext` on every input, the `with` enters a real
    # context manager, and `assert x != 1` FIRES whenever `x == 1`. The
    # correct verdict is `True`, and both `ed9d9b0` and this branch answered
    # `False` -- a live contract certified as disarmed, the damaging direction
    # per #308 criterion 1.
    #
    # The row is now the shape it was always meant to be: the two stores tie on
    # `_binding_order`, and the tie is NOT ambiguous, because a loop `else` is
    # a continuation rather than a peer branch. The later write settles the
    # name. `_loop_else_always_runs` is what decides it, and the shape that
    # genuinely needs declining -- the same loop with a `break`, where the two
    # arms really are exclusive -- is the row below.
    (
        "a for/else pair whose else runs on every path settles the name",
        (
            "    for item in items:\n"
            "        first = contextlib.suppress(AssertionError)\n"
            "    else:\n"
            "        first = contextlib.nullcontext()\n"
            "    with (cs := first):\n"
            "        assert x != 1"
        ),
        [True],
    ),
    # The same loop, but the body can `break` out of it. Then the `else` runs
    # only when the iterable is empty, the two arms really are exclusive, and
    # which one ran is an input:
    #
    #     items == []   ->  else runs, binds a nullcontext, assert FIRES
    #     items == [1]  ->  body runs, breaks, binds a suppress, swallowed
    #
    # The interpreter disagrees with itself here, so no single verdict is
    # right and the name must be declined -- a defeat, the safe side. This is
    # the row the first cut of #367 believed it was pinning; it is the one
    # that actually earns the decline.
    (
        "a for/else pair the loop can break out of is declined",
        (
            "    for item in items:\n"
            "        first = contextlib.suppress(AssertionError)\n"
            "        break\n"
            "    else:\n"
            "        first = contextlib.nullcontext()\n"
            "    with (cs := first):\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # A `break` inside a NESTED loop belongs to that inner loop, so it cannot
    # suppress the outer loop's `else` and the outer store still settles the
    # name. The rule walks the outer body without descending into an inner
    # loop, and this row is what keeps that walk honest: a version that
    # counted every `break` in the subtree would decline here and report this
    # live assert defeated.
    (
        "a break in a nested loop does not suppress the outer loop's else",
        (
            "    for item in items:\n"
            "        for inner in range(2):\n"
            "            if inner:\n"
            "                break\n"
            "        first = contextlib.suppress(AssertionError)\n"
            "    else:\n"
            "        first = contextlib.nullcontext()\n"
            "    with (cs := first):\n"
            "        assert x != 1"
        ),
        [True],
    ),
    # The row above is the easy half of the nested-loop rule, and on its own it
    # is exactly what makes the hard half look safe. `_breaks_own_loop` stops
    # at a nested loop because a `break` in its BODY exits the inner loop. A
    # nested loop's `else`, though, is a plain block rather than a loop, so a
    # `break` written there binds to the ENCLOSING loop:
    #
    #     items == []  ->  outer body never runs, outer else runs,
    #                     `first` is a nullcontext, assert FIRES
    #     items == [1] ->  inner loop completes, its else runs `break`,
    #                     the outer else is SKIPPED, `first` is still the
    #                     suppress, assert SWALLOWED
    #
    # So the outer `else` is NOT guaranteed, the tie between the two stores is
    # genuinely input-dependent, and the name has to be declined. Reading a
    # nested loop as a blanket "cannot break the loop we are asking about"
    # called the outer `else` guaranteed, answered `True`, and certified the
    # swallowed assert ENFORCED -- head-worse-than-base, and the damaging
    # direction per #308 criterion 1.
    (
        "a break in a nested loop's else does suppress the outer loop's else",
        (
            "    for item in items:\n"
            "        first = contextlib.suppress(AssertionError)\n"
            "        for inner in range(1):\n"
            "            pass\n"
            "        else:\n"
            "            break\n"
            "    else:\n"
            "        first = contextlib.nullcontext()\n"
            "    with (cs := first):\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # The same defect reached through `while`, whose `else` is the same kind of
    # plain block. Without this row the rule could be repaired for `for` only
    # and the suite would still be green.
    (
        "a break in a nested while's else does suppress the outer loop's else",
        (
            "    for item in items:\n"
            "        first = contextlib.suppress(AssertionError)\n"
            "        inner = 0\n"
            "        while inner < 1:\n"
            "            inner += 1\n"
            "        else:\n"
            "            break\n"
            "    else:\n"
            "        first = contextlib.nullcontext()\n"
            "    with (cs := first):\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # The control for the two rows above: a nested loop that does NOT break out
    # leaves the outer `else` guaranteed, so the outer store still settles the
    # name and the assert stays live. If the repair were "any nested loop makes
    # the outer `else` undecidable", this row would report `False` and fail.
    (
        "CONTROL a nested loop that never breaks leaves the outer else settled",
        (
            "    for item in items:\n"
            "        first = contextlib.suppress(AssertionError)\n"
            "        for inner in range(1):\n"
            "            pass\n"
            "    else:\n"
            "        first = contextlib.nullcontext()\n"
            "    with (cs := first):\n"
            "        assert x != 1"
        ),
        [True],
    ),
    # Two stores in one top-level statement where NEITHER is unconditional.
    # `max` would answer the `suppress` simply because it is walked first, but
    # neither store provably ran, so the name is declined by the *competing*
    # rule -- the same safe side, reached before the tie is ever formed. This
    # row is here to pin that the two paths agree, since #367's tie handling
    # sits immediately after them and a change to either could drift.
    (
        "two conditional stores in one top-level statement",
        (
            "    with (cs := contextlib.suppress(AssertionError)):\n"
            "        if flag:\n"
            "            cs = contextlib.suppress(ValueError)\n"
            "        else:\n"
            "            cs = contextlib.suppress(TypeError)\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # #367's own control, and the row that re-opens #323 if the mixed-tie rule
    # is written as "any tie is ambiguous". The walrus is unconditional and
    # runs on every path; the `nullcontext` assignment is later in the same
    # block. Once the body has run the name IS the `nullcontext`, so the
    # following header is live and must stay live.
    (
        "CONTROL an unconditional walrus beside a later rebind in one block",
        (
            "    with (cs := contextlib.suppress(AssertionError)):\n"
            "        cs = contextlib.nullcontext()\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, True],
    ),
    # The row that makes the *control* above discriminating rather than
    # accidental. In the control the walk happens to reach the `nullcontext`
    # store first, so a resolver that read the first maximal entry would reach
    # the same answer and the row would pass for the wrong reason. Here the
    # walk reaches the carried `suppress` walrus first, so picking the first
    # maximal entry resurrects the stale suppressor and reports the live
    # second assert defeated -- the damaging direction. Only the mixed-tie
    # rule, which returns the *conditional* member, answers `True`.
    #
    # Both stores sit in the SAME top-level statement, which is what makes
    # them a tie: the walrus is the `with` header's own named expression
    # (unconditional -- the header is evaluated on every path that reaches it)
    # and the `nullcontext` assignment is in that header's own body.
    (
        "a carried suppressor walked before its in-block rebind",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    with (cs := base):\n"
            "        cs = contextlib.nullcontext()\n"
            "        assert x != 1\n"
            "    with cs:\n"
            "        assert x != 2"
        ),
        [False, True],
    ),
    # The mixed tie where the later write cannot be entered at all. `cs` is a
    # module by the time the second header reads it, so the assert under that
    # header is unreachable -- and the FIRST assert, under the walrus header,
    # really is swallowed. Both are `False`, from two different rules: the
    # first from the alias walk, the second from the dead-entry rule that
    # #367 makes reachable by recognising the in-header store as settled.
    (
        "an unconditional walrus beside a later carrier in one block",
        (
            "    with (cs := contextlib.suppress(AssertionError)):\n"
            "        import os as cs\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # --- #370: a `for` target binds a value, and the single-element literal
    # --- is the shape where that value is unambiguous.
    #
    # #324 made a loop target *visible* so it could retire a carried
    # suppressor, and it recorded that store with no value at all. Recording
    # nothing is enough to retire -- a later write of the name wins either
    # way -- but it leaves the name equally unresolvable in the other
    # direction, so a loop that binds a suppressor of its own is invisible:
    #
    #     for cs in [contextlib.suppress(AssertionError)]:
    #         with cs:
    #             assert x != 1     # executed: swallowed
    #
    # Executed, that assert never fires. Reported `enforced`, it certifies a
    # disarmed contract as load-bearing -- the damaging direction per #308
    # criterion 1. The target does receive a value (the current element of
    # the iterable), and with exactly one element every iteration binds the
    # same object, so recording it is sound both inside the body and after
    # the loop.
    (
        "#370 a single-element loop target reaches a header in its own body",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        with cs:\n"
            "            assert x != 1"
        ),
        [False],
    ),
    # The same loop read *after* it completes. The body ran and the name
    # survived it, so the trailing header enters the same suppressor. A fix
    # that only taught the in-body header would leave this one live.
    (
        "#370 a single-element loop target survives the loop it was bound in",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        pass\n"
            "    with cs:\n"
            "        assert x != 1"
        ),
        [False],
    ),
    # The self-alias spelling. The loop binds the suppressor, and the header
    # rebinds `cs` to itself, so the entered value is unchanged and the
    # assert is still swallowed. This is the row that pins the walrus path
    # against the loop's own store rather than only the bare-`Name` path.
    (
        "#370 a loop-bound suppressor reached through a self-aliasing walrus",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        with (cs := cs):\n"
            "            assert x != 1"
        ),
        [False],
    ),
    # CONTROL: the same loop shape over a *non*-suppressing element. The
    # repair must not read "single-element loop" as "swallows" -- the verdict
    # here is decided by the element, exactly as it is for a plain store.
    (
        "CONTROL a single-element loop target over a nullcontext stays live",
        ("    for cs in [contextlib.nullcontext()]:\n        with cs:\n            assert x != 1"),
        [True],
    ),
    # The multi-element limit, stated as a row rather than left implicit.
    # Inside the body the target holds a *different* value per iteration, so
    # for the tuple below the first pass swallows the assert and the second
    # does not -- one static answer cannot be right for both, and guessing
    # either way is a coin flip that lands on a false verdict half the time.
    # Declining leaves the assert `enforced`, which is the safe direction: an
    # over-cautious sentinel still reports the contract it was asked to
    # protect.
    #
    # #385 measured this trade on a real head. Indexing to one element or the
    # other does not remove the damaging cell, it moves it -- so the limit
    # stands for a header read *inside* the body, which is the shape this row
    # is. The after-loop variant, where the last element IS the right answer,
    # is settled separately by the rows below rather than by changing this one.
    (
        "CONTROL a multi-element loop target stays live while undecided",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.nullcontext()):\n"
            "        with cs:\n"
            "            assert x != 1"
        ),
        [True],
    ),
    # The ordinary same-block case, and the reason the exception is scoped to
    # the loop that owns the body. Here the store comes *after* the header, so
    # `cs` is not bound when the header is read and entry raises
    # `UnboundLocalError` on every path. A repair that treated any enclosing
    # block as "its stores are already in force" would report this live
    # assert defeated and drop a contract that raises loudly.
    (
        "CONTROL a same-block store after the header does not bind it early",
        (
            "    if flag:\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        [True],
    ),
    # A `while` body has no target, so the same exception must not fire for
    # it. Here the loop target is the whole point: the store is inside the
    # loop and is seen by the ordinary "a store earlier in this block has run"
    # rule, with no help from #370.
    (
        "CONTROL a while body needs no loop-target exception",
        (
            "    cs = contextlib.suppress(AssertionError)\n"
            "    while flag:\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "        break"
        ),
        [False],
    ),
    # A name the loop does NOT bind, read inside the loop body. The exception
    # #370 adds is scoped to the loop's own target, so it must leave every
    # other carried binding alone -- the suppressor `base` is still in force
    # under `with base:`, and the assert is swallowed.
    #
    # This is the row that pins the exception as *additive*. Reading the loop
    # target and returning it in place of what the enclosing blocks carried
    # reports this defeated-to-live (`True`), certifying a swallowed assert as
    # load-bearing. The loop binds `other`, so `base` is never among the
    # bindings that get merged -- which is exactly why a replacement cannot
    # be told apart from a merge by any of the rows above.
    (
        "CONTROL a loop leaves a carried binding the loop never touches",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    for other in [0]:\n"
            "        with base:\n"
            "            assert x != 1"
        ),
        [False],
    ),
    # The same source with the loop rebinding the name. Here the loop's own
    # store really is the latest write, so it supersedes the carried
    # suppressor and the assert is live. Read together with the row above,
    # the pair says the merge is layered in binding order rather than either
    # dropped or always winning.
    # The body is *walked*, not matched against `node.body` directly, so a
    # header nested one block down inside the loop is reached the same way the
    # direct-header row above is. Executed, every one of these swallows the
    # assert, so a rule scoped to the direct body reports them `enforced` --
    # #308 criterion 1, a disarmed contract certified as load-bearing -- while
    # still passing the direct-header row. The direct row cannot catch this.
    (
        "#370 a loop-bound suppressor reaches a header nested under an if",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        if flag:\n"
            "            with cs:\n"
            "                assert x != 1"
        ),
        [False],
    ),
    (
        "#370 a loop-bound suppressor reaches a header nested under a try",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        try:\n"
            "            with cs:\n"
            "                assert x != 1\n"
            "        except ValueError:\n"
            "            pass"
        ),
        [False],
    ),
    (
        "#370 a loop-bound suppressor reaches a header under an inner loop",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        for _ in range(1):\n"
            "            with cs:\n"
            "                assert x != 1"
        ),
        [False],
    ),
    # A loop that binds a name the header never reads must not *retire* the
    # binding that loop's sibling does supply. `other` binds nothing the
    # header uses, so `cs` still resolves to the inner loop's suppressor, and
    # a rule that returned the first enclosing loop's bindings -- or that let a
    # non-matching loop end the search -- reports this live when it is
    # swallowed. This is the row that pins the search as "every enclosing loop,
    # merged", not "the first one found".
    (
        "#370 an enclosing loop that binds nothing does not hide the inner one",
        (
            "    for other in [0]:\n"
            "        for cs in [contextlib.suppress(AssertionError)]:\n"
            "            with cs:\n"
            "                assert x != 1"
        ),
        [False],
    ),
    # The inner loop's target is the LAST write before the header, so it wins
    # over the outer loop's. Taking the outermost loop's element instead --
    # which is what source order and breadth-first `ast.walk` both suggest --
    # reads the suppressor and reports the assert defeated, removing a contract
    # that really does fire. This is the damaging direction for this rule, and
    # it is the row that says the merge is layered in *containment* order.
    (
        "CONTROL an inner loop's target supersedes the outer loop's",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        for cs in [contextlib.nullcontext()]:\n"
            "            with cs:\n"
            "                assert x != 1"
        ),
        [True],
    ),
    # Three levels deep, with the suppressor at the bottom and a non-suppressor
    # in each enclosing loop. Only the innermost loop's target is the last write
    # before the header, so only its element decides the verdict; a rule that
    # stopped at the first or the last enclosing loop it happened to visit gets
    # one of the two other answers, both of which are wrong. Written with the
    # same name throughout so each loop genuinely overwrites the last.
    (
        "#370 the innermost of three nested loop targets decides the header",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        for cs in [contextlib.nullcontext()]:\n"
            "            for cs in [contextlib.suppress(AssertionError)]:\n"
            "                with cs:\n"
            "                    assert x != 1"
        ),
        [False],
    ),
    (
        "CONTROL the innermost of three nested loop targets can be benign",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        for cs in [contextlib.suppress(AssertionError)]:\n"
            "            for cs in [contextlib.nullcontext()]:\n"
            "                with cs:\n"
            "                    assert x != 1"
        ),
        [True],
    ),
    # Only a store *before* the header counts, and these three pin that
    # boundary. A mutation that drops "strictly before" reads a store that has
    # not run yet, and all three flip -- which is how the guard was found to be
    # load-bearing rather than decorative.
    #
    # The first two are `False`, not `True`: the loop's own target *is* in
    # force when the header is evaluated, so the assert really is swallowed and
    # the later `cs = nullcontext()` changes nothing about it. They are here to
    # pin that the later store is ignored, not to claim a loud failure.
    (
        "CONTROL an else-arm store after the header does not rebind it",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        pass\n"
            "    else:\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "        cs = contextlib.nullcontext()"
        ),
        [False],
    ),
    (
        "CONTROL a store after a nested header does not rebind it either",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "        cs = contextlib.nullcontext()"
        ),
        [False],
    ),
    # The mirror image, and the damaging one. Here the loop's target is a
    # `nullcontext`, so the header is live and the assert really does fire; the
    # suppressor stored *after* it has not run yet. A rule that counted that
    # store would read the suppressor and report the assert defeated, deleting a
    # contract that is load-bearing. This is the row that says the cutoff runs
    # in the safe direction.
    (
        "CONTROL a suppressor stored after the header does not reach it",
        (
            "    for cs in [contextlib.nullcontext()]:\n"
            "        pass\n"
            "    else:\n"
            "        with cs:\n"
            "            assert x != 1\n"
            "        cs = contextlib.suppress(AssertionError)"
        ),
        [True],
    ),
    # A loop's `else` arm is a block of its own, and the target is bound by the
    # time it runs -- the body once per iteration, the arm once after the loop
    # finishes. The arm is a sibling list rather than a child, so a walk of
    # `node.body` never reached it and the header reported `enforced` while
    # executed it is swallowed. Same defect as the nested-body rows above, on
    # the one arm that is easy to overlook.
    (
        "#370 a loop-bound suppressor reaches a header in the else arm",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        pass\n"
            "    else:\n"
            "        with cs:\n"
            "            assert x != 1"
        ),
        [False],
    ),
    # The arm's own store is the latest write and supersedes the loop target.
    # This one is the damaging direction: reading the loop's element instead
    # reports the assert defeated and drops a contract that really fires. It
    # also pins that the arm is read as a block in its own right -- `own` is
    # keyed by position in `function.body`, and a store in an `else` arm has no
    # index there, so it is only reachable through the raw store table.
    (
        "CONTROL a store in the else arm supersedes the loop target",
        (
            "    for cs in [contextlib.suppress(AssertionError)]:\n"
            "        pass\n"
            "    else:\n"
            "        cs = contextlib.nullcontext()\n"
            "        with cs:\n"
            "            assert x != 1"
        ),
        [True],
    ),
    (
        "CONTROL a non-suppressing loop target keeps the else arm live",
        (
            "    for cs in [contextlib.nullcontext()]:\n"
            "        pass\n"
            "    else:\n"
            "        with cs:\n"
            "            assert x != 1"
        ),
        [True],
    ),
    # The nested form of the multi-element limit, which is the shape the
    # walk-based match newly makes reachable. Undecided inside the body, and
    # undecided here for the same reason, so it stays `enforced`.
    (
        "CONTROL a nested multi-element loop target stays live while undecided",
        (
            "    for cs in (contextlib.suppress(AssertionError),\n"
            "               contextlib.nullcontext()):\n"
            "        if flag:\n"
            "            with cs:\n"
            "                assert x != 1"
        ),
        [True],
    ),
    (
        "CONTROL a loop target supersedes a carried binding of the same name",
        (
            "    base = contextlib.suppress(AssertionError)\n"
            "    for base in [contextlib.nullcontext()]:\n"
            "        with base:\n"
            "            assert x != 1"
        ),
        [True],
    ),
)
