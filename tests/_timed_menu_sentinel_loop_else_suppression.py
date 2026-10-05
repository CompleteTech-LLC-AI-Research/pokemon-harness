"""Loop-else suppression and reachability cases.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_elif_links import (
    _assert_suppression_contract,
)

#: #451, the loop spelling of the arm the #441 table above pins for `elif`.
#:
#: A `for`/`else` or `while`/`else` `else` clause runs only when the loop
#: finishes *without* a `break`. #378 correctly established the complementary
#: rule -- a zero-iteration loop's `else` still runs -- but the other side was
#: not modelled, so a suppressor bound in an `else` that a `break` skips
#: retired the carried `nullcontext` and the assert was reported defeated.
#:
#: `loop` is the loop head, `body` its body, and `assert_is_live` the exact
#: verdict. Each fixture is executed across `x in (0, 1)` before the
#: analyzer's answer is compared, so CPython decides every row and a row
#: cannot claim a verdict the interpreter disagrees with.
LOOP_ELSE_SUPPRESSOR_SHAPES = (
    # -- The filed defect. `break` is unconditional, so the `else` never runs
    #    on any call and the carried `nullcontext` is what the `with` enters.
    (
        "451 filed: a break skips the loop else",
        "for item in (1,):",
        "        break",
        True,
    ),
    # -- Same shape with `while`. The clause is spelled the same way and the
    #    loop is spelled the other way; only the head differs.
    (
        "451 while/else: a break skips the loop else",
        "while True:",
        "        break",
        True,
    ),
    # -- A break the failure value does not exclude. The filed `assert x != 1`
    #    holds at `x == 1`, and so does `if x == 1: break`, so there IS a call
    #    that both breaks and fails. Pinned because it is the correlation the
    #    witness has to get right in the live direction.
    (
        "451 live: the break holds on a failing call",
        "for item in (1,):",
        "        if x == 1:\n            break",
        True,
    ),
    # -- No `break` at all: the loop always completes normally, so the `else`
    #    always runs, the suppressor really is installed on every call, and
    #    the assert really is swallowed. Declining the witness here is what
    #    keeps the change from inventing a false-LIVE.
    (
        "451 control: no break, so the else always runs",
        "for item in (1,):",
        "        pass",
        False,
    ),
    (
        "451 control: a while loop with no break completes normally",
        "while False:",
        "        pass",
        False,
    ),
    # -- `continue` is not a `break`. The loop still completes normally, so the
    #    `else` still runs. This is the row a naive "any jump out of the body"
    #    reading gets wrong, and it is the reason `_has_own_break` matches
    #    `ast.Break` and nothing else.
    (
        "451 control: continue does not skip the else",
        "for item in (1,):",
        "        continue",
        False,
    ),
    # -- A `break` bound to a NESTED loop. The inner loop's `break` leaves the
    #    *inner* loop, so the outer one still completes normally and the outer
    #    `else` still runs. `_has_own_break` stops at the inner loop for this.
    (
        "451 control: a break in a nested for belongs to that loop",
        "for item in (1,):",
        "        for inner in (2,):\n            break",
        False,
    ),
    (
        "451 control: a break in a nested while belongs to that loop",
        "for item in (1,):",
        "        while True:\n            break",
        False,
    ),
    # -- The break is guarded by exactly the assert's own condition. The two
    #    are *not* exclusive: the `else` binds the calls where the guard is
    #    false, and the assert fails on the calls where it is true, so the
    #    suppressor is in force on precisely the calls that cannot fail and
    #    the assert fires on the rest. Pinned because it is the row that
    #    decides the polarity of the correlation -- reading "the break and the
    #    failure are the same condition" as *exclusive* would wrongly decline
    #    a live header, and that is the mistake this table exists to catch.
    (
        "451 live: the break guard matches the failure condition",
        "for item in (1,):",
        "        if x:\n            break",
        True,
    ),
    # -- The guard that names the assert's own condition. The break is taken
    #    exactly where the assert *holds*, so the `else` binds precisely the
    #    calls where the assert fails -- and swallows every one of them. This is
    #    the exclusive shape, and it is the one that must be declined: the
    #    break is unreachable at the failing value, the loop completes
    #    normally, and the header really is defeated.
    (
        "451 control: a guard naming the assert condition is exclusive",
        "for item in (1,):",
        "        if x != 1:\n            break",
        False,
    ),
    # -- A guard that is *decided* the wrong way for every failing call, so
    #    the failing calls all take the `else` and really are swallowed. This
    #    is the correlation in the damaging direction, and the row the witness
    #    has to get right to avoid a new false-LIVE.
    (
        "451 control: the break excludes every failing call",
        "for item in (1,):",
        "        if x == 0:\n            break",
        False,
    ),
    (
        "451 control: an inverted guard also excludes the failing call",
        "for item in (1,):",
        "        if not x:\n            break",
        False,
    ),
    # -- #479. `Is` and `IsNot` were the operators #471 left declined, so they
    #    reached `_condition_can_hold` as "not readable" -- which that function
    #    counts as *possibly true*, the damaging direction. `x is None` decides
    #    False at the failing value `x == 1`, the `if` body is skipped, the loop
    #    completes normally, the `else` installs the suppressor, and the assert
    #    is swallowed on every call. Master certified it enforced: a false-LIVE
    #    live on `origin/master`, not a draft-only artifact.
    #
    #    `None` is not in the swept domain, so the identity guard is False at
    #    every value the oracle calls, which is exactly what makes the row a
    #    false-LIVE rather than a live contract.
    (
        "479 filed: an identity guard excludes every failing call",
        "for item in (1,):",
        "        if x is None:\n            break",
        False,
    ),
    # -- The mirror, and the reason a wholesale refusal of identity guards
    #    would be a regression rather than a fix: `x is not None` HOLDS at the
    #    failing value, so there really is a call that both breaks and fails,
    #    and the assert is live. Answering "unreadable" for every identity
    #    guard would flip this row to a false-DEAD.
    (
        "479 live: an inverted identity guard holds on a failing call",
        "for item in (1,):",
        "        if x is not None:\n            break",
        True,
    ),
    # -- Identity against a literal the domain can actually hold, so this is
    #    decided rather than constant. `x is 0` is False at `x == 1` for small
    #    ints under CPython's interning, which makes it a second excluded-failure
    #    row in the damaging direction.
    (
        "479 control: identity against a literal the domain holds",
        "for item in (1,):",
        "        if x is 0:\n            break",
        False,
    ),
    # -- #479 residual. Membership was declined for the same reason identity
    #    was: `None` reached `_condition_can_hold` as "possibly true". `x in [0]`
    #    decides False at the failing value `x == 1`, so the loop completes
    #    normally, the `else` installs the suppressor, and the assert is
    #    swallowed on every call.
    (
        "479 residual: a membership guard excludes every failing call",
        "for item in (1,):",
        "        if x in [0]:\n            break",
        False,
    ),
    # -- A membership guard the failing value satisfies, and the reason
    #    membership cannot be read as always-false. `x in (0, 1)` HOLDS at
    #    `x == 1`, so there is a call that both breaks and fails, the `else` is
    #    skipped, and the carried `nullcontext` is what the header enters.
    #
    #    The *inverted* spelling is the opposite and is deliberately absent:
    #    `x not in (0, 1)` is False at `x == 1`, so the `else` runs and the
    #    assert is swallowed -- it is the excluded-failure case, not a live one.
    (
        "479 residual live: a satisfied membership guard holds on a failing call",
        "for item in (1,):",
        "        if x in (0, 1):\n            break",
        True,
    ),
    # -- A container the guard holds on, for the damaging direction from the
    #    other side: `x in (0, 1)` is True at `x == 1`, so the break is reached
    #    on the failing call and the `else` is skipped. Live, and a repair that
    #    read membership as always-false would call this dead.
    (
        "479 residual live: membership against a tuple holds on a failing call",
        "for item in (1,):",
        "        if x in (0, 1, 2):\n            break",
        True,
    ),
    # -- #479 residual. `and`/`or` were declined as a whole, so a guard built
    #    from one read as "possibly true" even where a single operand settles it.
    #    `x is None` is False at `x == 1` and that false operand settles `and`
    #    on its own -- Python short-circuits there and never reads `y` at all --
    #    so the loop completes normally and the `else` installs the suppressor.
    (
        "479 residual: a short-circuited conjunction excludes every failing call",
        "for item in (1,):",
        "        if x is None and y:\n            break",
        False,
    ),
    # -- The mirror on the other side, and the reason the operator cannot be
    #    read as always-false. A false *first* operand does not settle `or`, so
    #    the second operand decides it; at `x == 1` the guard holds, the break
    #    is reached on the failing call, and the contract is live.
    #
    #    Both operands have to be pinned by the failing assert for this to be
    #    decidable, which is why the row reads the same parameter the assert
    #    reads. A guard naming a *different* parameter is caller-dependent --
    #    see `WALRUS`-style declines elsewhere in this module.
    (
        "479 residual live: a disjunction decides on its second operand",
        "for item in (1,):",
        "        if x == 0 or x:\n            break",
        True,
    ),
)


#: #451 as the issue *filed* it: the module is imported **inside the
#: function**, not at module scope.  The first version of this repair pinned
#: only the module-level spelling, so the filed fixture kept reporting dead
#: and the issue stayed open -- the exact hazard this repository's ground-truth
#: rule exists to catch.  These rows use the filed spelling verbatim.
#:
#: `import contextlib` is transparent to the witness: it binds the same module
#: root `_resolves_to` already follows, and introduces no value, branch, or
#: ordering that could settle `cs`.  The witnesses accept it; the rows below
#: pin that, in both the live and the declined direction, so the acceptance
#: cannot quietly widen into a false-LIVE.
FUNCTION_LOCAL_IMPORT_SHAPES = (
    # -- The filed fixture. `break` is unconditional, so the `else` never runs
    #    and the carried `nullcontext` is what the header enters.
    (
        "451 filed: function-local import, a break skips the loop else",
        "for item in (1,):",
        "        break",
        True,
    ),
    (
        "451 filed: function-local import, while/else",
        "while True:",
        "        break",
        True,
    ),
    # -- The live correlation under the filed spelling: the break's guard and
    #    the assert's condition are the same predicate, but a call needs only
    #    one of them, so the break is still reachable where the assert fails.
    (
        "451 filed: local import, the break guard matches the failure",
        "for item in (1,):",
        "        if x:\n            break",
        True,
    ),
    # -- Declined under the filed spelling. The guard names the assert's own
    #    condition, so the `else` binds precisely the failing calls and
    #    swallows every one. Answering live here would be a false-LIVE, and
    #    this row is what stops the import acceptance from becoming one.
    (
        "451 control: local import, guard naming the assert is exclusive",
        "for item in (1,):",
        "        if x != 1:\n            break",
        False,
    ),
    (
        "451 control: local import, the break excludes every failing call",
        "for item in (1,):",
        "        if x == 0:\n            break",
        False,
    ),
    # -- No `break`: the loop completes normally, so the `else` always runs and
    #    the suppressor really is installed on every call.
    (
        "451 control: local import, no break so the else always runs",
        "for item in (1,):",
        "        pass",
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "loop", "body", "assert_is_live"),
    LOOP_ELSE_SUPPRESSOR_SHAPES,
    ids=[row[0] for row in LOOP_ELSE_SUPPRESSOR_SHAPES],
)
def test_a_loop_else_suppressor_is_live_when_a_break_skips_it(label, loop, body, assert_is_live):
    """A `break` skipping a loop's `else` does not defeat the assert.

    This is #451. The filed fixture:

        import contextlib
        def outer(x):
            cs = contextlib.nullcontext()
            for item in (1,):
                break                       # loop exits without completion
            else:
                cs = contextlib.suppress(AssertionError)   # SKIPPED
            with cs:                       # `cs` is still the nullcontext
                assert x != 1             # LIVE

    Executed on CPython 3.12 the assert **fires**, while `_is_enforced`
    reported it defeated on master `2c81f10` and on merged `02776f8`. The
    suppressor was recorded as an unconditional store, so it retired the
    carried `nullcontext` and the header resolved to a suppressor.

    The repair demotes a store in a loop `else` that a `break` bound to *that
    loop* can skip, exactly as #441 demotes a store in an `elif` arm, and then
    proves the witness separately. The witness is correlated rather than
    symmetric with #441's: #441 shows a *failure value* selects an arm that
    skips the suppressor, while #451 has to show a call that both reaches the
    `break` and still fails. Those are the same call only when the break's
    guard does not exclude the failure, which is why the two exclusion rows
    are pinned as controls.

    Every row is executed before the analyzer's verdict is compared, so
    CPython decides the row rather than the author's reasoning about it.
    """
    # `_assert_suppression_contract` sweeps `outer(value, True, None)`, so the
    # fixture takes the same three parameters even though only `x` is read.
    # The two unused ones keep the row on the shared helper -- and therefore on
    # the shared CPython oracle -- rather than re-deriving one here.
    source = (
        "import contextlib\n"
        "def outer(x, flag, helper):\n"
        "    cs = contextlib.nullcontext()\n"
        f"    {loop}\n"
        f"{body}\n"
        "    else:\n"
        "        cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_suppression_contract(label, source, assert_is_live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [assert_is_live], (
        f"{label}: expected verdicts [{assert_is_live}], got {results}. A loop's "
        f"`else` runs only when the loop completes without a `break`, so a "
        f"suppressor bound there is installed on the calls that break and not "
        f"on the rest."
    )


@pytest.mark.parametrize(
    ("label", "loop", "body", "assert_is_live"),
    FUNCTION_LOCAL_IMPORT_SHAPES,
    ids=[row[0] for row in FUNCTION_LOCAL_IMPORT_SHAPES],
)
def test_a_loop_else_suppressor_is_live_under_a_function_local_import(
    label, loop, body, assert_is_live
):
    """#451 as filed: `import contextlib` is **inside** the function.

    The filed fixture is

        def outer(x):
            import contextlib
            cs = contextlib.nullcontext()
            for item in (1,):
                break
            else:
                cs = contextlib.suppress(AssertionError)   # SKIPPED
            with cs:
                assert x != 1

    Executed on CPython 3.12 the assert **fires**, so the correct verdict is
    live.  This spelling is the reason the issue was filed, and a repair that
    pins only the module-level import leaves it dead -- a false-DEAD in the
    damaging direction, on the exact fixture the issue names.

    A function-local import is transparent to the witness: it binds the same
    module root the witness already resolves through, and it introduces no
    value, branch, or ordering that could settle `cs`.  The repair therefore
    admits an import that binds exactly that root, and only that root -- an
    alias, a dotted import of a different root, or a `from ... import` of the
    leaf all still decline, because each of those rebinds something other than
    the name the witness reads.

    Every row is executed across `x in (0, 1)` before the analyzer's verdict is
    compared, so CPython decides the row.  The declined rows are what stop this
    acceptance from widening into a false-LIVE.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    cs = contextlib.nullcontext()\n"
        f"    {loop}\n"
        f"{body}\n"
        "    else:\n"
        "        cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_suppression_contract(label, source, assert_is_live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [assert_is_live], (
        f"{label}: expected verdicts [{assert_is_live}], got {results}. The filed "
        f"fixture imports contextlib inside the function; that import binds the "
        f"same root the witness resolves through and must not defeat the "
        f"correlated loop-else witness."
    )


#: The repair admits an import that cannot change what the header enters.  These
#: rows pin both sides of that rule: the spellings that are transparent, and the
#: one that genuinely rebinds a walked root.  Without the negative side the
#: acceptance can be widened to "any import is transparent" and the lane still
#: goes green -- measured as surviving mutant N2 -- which would let a
#: rebinding import talk the witness into a false-LIVE.
IMPORT_SHAPE_ROWS = (
    # -- The filed spelling is admitted.  Kept here so this table and the
    #    `#451` table cannot drift apart on the live direction.
    (
        "451 import accepted: the root the witness reads",
        "    import contextlib\n",
        "for item in (1,):",
        "        break",
        True,
    ),
    # -- The unrelated import is transparent, but the loop has no `break`, so
    #    its `else` runs and the suppressor really does install.  What defeats
    #    the header here is the missing `break`, not the import -- ROW2b below
    #    is the same import with the `break` restored, and it fires.
    (
        "451 import declined: an unrelated module",
        "    import json\n    import contextlib\n",
        "for item in (1,):",
        "        pass",
        False,
    ),
    # -- The same unrelated import with the `break` back.  `json` is not a name
    #    the witness resolves through, so it is transparent and the assert
    #    fires (#475).
    (
        "475 import accepted: an unrelated module, break present",
        "    import json\n    import contextlib\n",
        "for item in (1,):",
        "        break",
        True,
    ),
    # -- An alias bound to a name the witness never reads.  `cl` is not
    #    resolved through, so the statement cannot change what the header
    #    enters and the canonical `contextlib` calls still fire.  Refusing it
    #    reported this live assert dead (#475).
    #
    #    This row used to carry `import contextlib as cl` *alone* and claim
    #    DEAD, but the fixture could not judge anything: with `cl` bound and
    #    `contextlib` never imported, every call died with
    #    `NameError: name 'contextlib' is not defined` and never reached the
    #    assert.  The oracle only counted that as "did not fire", so the row
    #    was really asserting that an unrunnable fixture stayed unrunnable.
    (
        "475 import accepted: an alias the witness never reads",
        "    import contextlib as cl\n    import contextlib\n",
        "for item in (1,):",
        "        break",
        True,
    ),
    # -- The alias under the name the witness *does* resolve through.  `cb`
    #    expands to `contextlib`, so the alias is the filed spelling wearing a
    #    different name.
    #
    #    `import contextlib` is still needed beside it: this table's builder
    #    spells the `contextlib.nullcontext()` / `contextlib.suppress()` calls
    #    canonically, so an alias-only preamble leaves that name unbound and the
    #    fixture dies with `NameError` before reaching the assert -- which is
    #    how the old alias row ended up asserting nothing.
    (
        "475 import accepted: the filed spelling behind an alias",
        "    import contextlib as cb\n    import contextlib\n",
        "for item in (1,):",
        "        break",
        True,
    ),
    # -- `from` spellings bind a leaf, not the root, so the same collision test
    #    applies: a leaf of an unrelated module binds a name nothing here
    #    reads and is transparent.  These were false-DEADs on 49b899a and were
    #    found by the independent review of this change -- the rule it states
    #    covers every import spelling, so `from` had to be handled rather than
    #    excluded from the claim.
    (
        "475 import accepted: a from-import leaf of an unrelated module",
        "    from json import loads as cl\n    import contextlib\n",
        "for item in (1,):",
        "        break",
        True,
    ),
    (
        "475 import accepted: a plain from-import of an unrelated module",
        "    from json import loads\n    import contextlib\n",
        "for item in (1,):",
        "        break",
        True,
    ),
    # -- A leaf of the *real* module, under a name nothing reads.  It is still
    #    transparent: the header enters `cs`, not `sq`.
    (
        "475 import accepted: a from-import leaf of contextlib, unread",
        "    from contextlib import suppress as sq\n    import contextlib\n",
        "for item in (1,):",
        "        break",
        True,
    ),
    # -- Two plain imports of the same root.  Idempotent, so still transparent.
    (
        "451 import accepted: two plain imports of the same root",
        "    import contextlib\n    import contextlib\n",
        "for item in (1,):",
        "        break",
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "preamble", "loop", "body", "assert_is_live"),
    IMPORT_SHAPE_ROWS,
    ids=[row[0] for row in IMPORT_SHAPE_ROWS],
)
def test_only_the_resolved_root_is_admitted_as_a_transparent_import(
    label, preamble, loop, body, assert_is_live
):
    """An import is transparent unless it rebinds a root the witness reads.

    #451's repair lets a function-local `import contextlib` pass the pre-chain
    scan that otherwise rejects it.  "Any import is transparent" is still wrong
    -- `import fake as contextlib` really does swap the object the witness walks
    -- but that is pinned directly, below, because it needs a stand-in module
    rather than a fixture row.

    #475 widened the accepted set the other way.  Refusing every non-canonical
    spelling certified live asserts dead: an alias, a dotted import of an
    unrelated root, and an unrelated plain import all leave the walked root
    alone, and CPython fires the assert for each.

    Each row is executed first, so CPython decides the expected verdict rather
    than the author's reasoning about what the import means.  A row whose
    fixture cannot reach its assert cannot be judged this way, which is why
    every row here is a shape that actually runs.
    """
    source = (
        "def outer(x, flag, helper):\n"
        f"{preamble}"
        "    cs = contextlib.nullcontext()\n"
        f"    {loop}\n"
        f"{body}\n"
        "    else:\n"
        "        cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_suppression_contract(label, source, assert_is_live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [assert_is_live], (
        f"{label}: expected verdicts [{assert_is_live}], got {results}. An import "
        f"is transparent unless it rebinds a root the witness resolves through, "
        f"and then only if it really is the module the witness follows."
    )
