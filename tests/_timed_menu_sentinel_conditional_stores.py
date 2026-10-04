"""Conditional stores and carrier rebinding.

Actual test functions and literal cases from the original collector.
"""

import ast
import contextlib

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_import_context_managers import (
    _assert_entry_contract,
)
from tests._timed_menu_sentinel_scope_lookup import (
    DEAD_CONDITION_AFTER_CARRIER_SHAPES,
)


@pytest.mark.parametrize(
    ("label", "carrier", "conditional"),
    DEAD_CONDITION_AFTER_CARRIER_SHAPES,
    ids=[shape[0] for shape in DEAD_CONDITION_AFTER_CARRIER_SHAPES],
)
def test_a_nonenterable_conditional_store_does_not_revive_a_stale_carrier(
    label, carrier, conditional
):
    """A conditional store that cannot be entered leaves the carrier decisive.

    The counterpart to
    :func:`test_a_conditional_store_superseding_a_local_carrier_is_declined`.
    There the later store could bind a real context manager, so the header's
    value is undecidable; here it is pinned to a module, a class, a function or
    a non-enterable literal, so *every* path through the header raises before
    the assert is evaluated. Declining would certify a dead contract as
    enforced, which is the damaging direction, so the rule must answer from the
    carrier as usual.

    The last six rows are the ones an independent review caught on the first
    cut of the repair. Deciding "this assignment destructures, so the value is
    unreadable" from the *statement* is wrong in both directions at once:

    * ``*cs, = (helper,)`` builds a **list** for ``cs`` whatever the elements
      are, so the name is pinned non-enterable and the assert is dead;
    * ``cs = (other, x) = (helper, 1)`` gives the bare ``cs`` the **whole**
      right-hand side -- a tuple -- while ``other`` and ``x`` get elements of
      it. Deciding the statement once calls ``cs`` unreadable and declines.

    Both were reported live on the first cut, against a master that answers
    them correctly. Whether a store pins *this* name is a question about that
    name's own target, which is what `_store_may_bind_enterable` is now asked.

    Both rows are executed under CPython here too -- ``_assert_entry_contract``
    is called with ``second_assert_live=False``, which requires the fixture to
    raise something *other* than ``AssertionError``. That is what distinguishes
    "unreachable" from "swallowed", and it is what makes these rows
    non-vacuous rather than an assertion about the checker.

    **A known limit this table deliberately does not cover.** A conditional
    store that is itself a *carrier* --

        def outer(x, flag, helper):
            import os as cs
            if flag:
                import os as cs
            with cs:
                assert x != 1

    -- is dead under CPython on both paths, and `origin/master` (``ed9d9b0``)
    reports it `True` just the same. That is a separate pre-existing gap in
    `_carrier_runtime_kinds` on the carrier path, unchanged by this repair and
    not visible to the guard here, which reads the *settled* store. It is left
    alone rather than folded in: widening this table to cover it would make
    the non-vacuity proof below depend on a second repair.
    """
    source = (
        "import contextlib\n"
        "from contextlib import nullcontext\n"
        # `other` is bound by the chained-target rows, and the starred rows
        # unpack `pair` and bind `first`/`rest`/`a`/`b`, so every name they
        # touch is a parameter here rather than an unbound global: CPython has
        # to be able to run the fixture for the entry contract to mean
        # anything. `_assert_entry_contract` calls `outer(1, True, None)`, so
        # the extra names take their defaults.
        "def outer(x, flag, helper, other=None, pair=(1, 2), first=None, rest=None, a=None, b=None, third=None, rest3=None):\n"
        + carrier
        + "\n"
        + conditional
        + "\n"
        + "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, False)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False], (
        f"{label}: expected verdicts [False], got {results}. A conditional store "
        f"that is itself pinned non-enterable cannot make the header enterable, "
        f"so the carrier still settles the name and the assert stays unreachable."
    )


#: The fifth review round's signature- and module-scope shadowing rows.
#:
#: These cannot live in either carrier table, because both build their fixture
#: with a **fixed** signature ``outer(x, flag, helper)``. A row that needs the
#: *callee's own name* to be a parameter cannot be expressed through a carrier
#: string at all, and a module-level binding cannot be either. Each row
#: therefore supplies its whole source, which is what lets both halves of
#: #388-5b be expressed:
#:
#: * a **parameter** binds for the whole call, so ``def outer(list, x)`` calls
#:   whatever the caller passed -- never the builtin ``list``. Positional,
#:   keyword-only, ``*args`` and ``**kwargs`` each bind in their own way, and
#:   ``ast`` records the last two under ``vararg``/``kwarg`` rather than in the
#:   flat ``args`` list, so a filter reading only ``args`` misses them.
#: * a **module-level** ``def list(): ...`` binds the name for every function
#:   in the file, and a module-scope ``cs = list()`` is a store that has already
#:   run by the time any header reads it.
#:
#: Each row is **executed** before its verdict is asserted, so no row can pin a
#: verdict the interpreter does not agree with.
SHADOWED_CALLEE_SOURCES = (
    (
        "a positional parameter shadowing a builtin constructor",
        ("def outer(list, x):\n    cs = list()\n    with cs:\n        assert x != 1\n"),
        (contextlib.nullcontext,),
        {"x": 1},
    ),
    (
        "a keyword-only parameter shadowing a builtin constructor",
        (
            "from contextlib import nullcontext\n"
            "def outer(x, *, list=nullcontext):\n"
            "    cs = list()\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1},
    ),
    (
        "a vararg parameter shadowing a builtin constructor",
        "def outer(*list, x):\n    cs = list[0]()\n    with cs:\n        assert x != 1\n",
        (contextlib.nullcontext,),
        {"x": 1},
    ),
    (
        "a kwarg parameter shadowing a builtin constructor",
        (
            "from contextlib import nullcontext\n"
            "def outer(x, **list):\n"
            "    cs = list['k']()\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1, "k": contextlib.nullcontext},
    ),
    (
        "a module-level def shadowing a builtin constructor",
        (
            "from contextlib import nullcontext\n"
            "def list():\n"
            "    return nullcontext()\n"
            "cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1},
    ),
    (
        "a module-level assignment shadowing a builtin constructor",
        (
            "from contextlib import nullcontext\n"
            "list = nullcontext\n"
            "cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1},
    ),
    # #388-5d. The positive half of the module-scope rule, and the case a
    # naive "only descend into plain statements" fix would break: a `def`
    # inside a module-level `if` is still a **module** binding, because the
    # block runs in the scope that encloses it. The branch has run by the time
    # `cs = list()` is evaluated, so the call really is the shadow and the
    # header really is LIVE. This is what stops the false-lives above from
    # being "fixed" by simply refusing to look inside any block.
    (
        "a module-level if branch defining the shadowing constructor",
        (
            "from contextlib import nullcontext\n"
            "if True:\n"
            "    def list():\n"
            "        return nullcontext()\n"
            "cs = list()\n"
            "def outer(x, flag, helper):\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1, "flag": True, "helper": None},
    ),
    # #388-5d. The same binding one level deeper: a block inside a block. A
    # walk that only descends one level reports the name unshadowed and retires
    # a header CPython enters, so the descent has to be recursive.
    (
        "a shadowing constructor defined in a nested module if",
        (
            "from contextlib import nullcontext\n"
            "if True:\n"
            "    if True:\n"
            "        def list():\n"
            "            return nullcontext()\n"
            "cs = list()\n"
            "def outer(x, flag, helper):\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1, "flag": True, "helper": None},
    ),
    # #388-5d. A `match` clause runs in the scope that encloses it, exactly as
    # an `if` body does. `Case` is not one of the block shapes the walk opens,
    # so the clause has to be flattened to its statements; reading past it made
    # this a false-dead on an earlier draft of the repair.
    (
        "a shadowing constructor defined in a module match case",
        (
            "from contextlib import nullcontext\n"
            "match 1:\n"
            "    case 1:\n"
            "        def list():\n"
            "            return nullcontext()\n"
            "cs = list()\n"
            "def outer(x, flag, helper):\n"
            "    with cs:\n        assert x != 1\n"
        ),
        (),
        {"x": 1, "flag": True, "helper": None},
    ),
)


def _shadowed_callee_reaches_assert(label, source, arguments, keywords):
    """Does CPython reach the assert in this row? Executed, never assumed."""
    namespace = {"nullcontext": contextlib.nullcontext}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](*arguments, **keywords)
    except AssertionError:
        return True
    except (TypeError, AttributeError, KeyError, IndexError, NameError, UnboundLocalError):
        return False
    raise AssertionError(
        f"{label}: the fixture returned normally, so neither the assert nor an "
        f"entry failure was observed and the row proves nothing."
    ) from None


#: The round-5 review's findings against `d921aac`, all **false-live**: the tool
#: reported a header LIVE where CPython raises before the assert. The direction
#: is the mild one -- a header reported as enterable when it is not -- but these
#: were *introduced* by the module-scope shadowing that #388-5b added, so they
#: are regressions against `86fab6d` and had to be repaired.
#:
#: Each row is executed first: `x=2` makes `assert x != 1` true, so a clean
#: return proves the with-body was entered (LIVE) and any exception proves it
#: was not (DEAD). The expected verdict is DEAD in all four.
MODULE_SCOPE_FALSE_LIVE_SOURCES = (
    # #388-5d(f1). `cs = list()` runs BEFORE the `def list()` below it, so the
    # call is still the real builtin and binds the empty list -- which cannot be
    # entered. Scanning the whole module regardless of order called the name
    # shadowed, and a `TypeError`-raising header came back LIVE.
    (
        "a module binding that runs after the call",
        "cs = list()\ndef list():\n    return None\ndef outer(x):\n    with cs:\n        assert x != 1\n",
    ),
    # #388-5d(f1b). The same, with the later binding inside a module-level `if`
    # rather than at the top level. The branch has not run when `cs = list()`
    # is evaluated, so it does not count either.
    (
        "a later module binding inside a module if",
        (
            "cs = list()\n"
            "if True:\n"
            "    def list():\n"
            "        return None\n"
            "def outer(x):\n"
            "    with cs:\n        assert x != 1\n"
        ),
    ),
    # #388-5d(f2). `list` is bound in `unrelated`'s LOCALS, when `unrelated` is
    # called. Nothing has called it, so the module never binds `list` and
    # `cs = list()` is still the builtin. Descending into a nested function body
    # read the `TypeError` a real call raises as a callable the header enters.
    (
        "a binding in another function's body",
        (
            "def unrelated():\n"
            "    def list(): return None\n"
            "cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n        assert x != 1\n"
        ),
    ),
    # #388-5d(f2b). A `class` body is a separate scope for the same reason: a
    # method named `list` does not bind the module name.
    (
        "a binding in a class body",
        (
            "class Holder:\n"
            "    def list(self): return None\n"
            "cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n        assert x != 1\n"
        ),
    ),
    # #388-5d(f3). `import builtins` is the ordinary import that brings the
    # real module in -- the same `import x` spelling that everywhere else in
    # this file means "this is the module named x". Treating it as a rebinding
    # of the name `builtins` made `cs = builtins.list()`, the canonical way of
    # naming a builtin, read as shadowed.
    (
        "a qualified builtin call through the builtins module",
        "import builtins\ncs = builtins.list()\ndef outer(x):\n    with cs:\n        assert x != 1\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"),
    MODULE_SCOPE_FALSE_LIVE_SOURCES,
    ids=[shape[0] for shape in MODULE_SCOPE_FALSE_LIVE_SOURCES],
)
def test_a_name_the_module_does_not_bind_at_call_time_is_reported_dead(label, source):
    """A name is shadowed only by a module binding that has actually run.

    #388-5d. #388-5b added a module-scope shadowing rule, and reading the whole
    module without regard to *when* the binding runs, or to *which scope* it is
    written in, produced false-lives on `d921aac`:

    * a binding written **after** the call, which has not run yet;
    * a binding inside **another function's** or a **class body**, which binds
      a local, not the module name;
    * `import builtins`, which is an ordinary import rather than a rebinding,
      so the qualified `builtins.list()` was read as shadowed.

    Every row is executed before its verdict is checked, so the DEAD
    expectation is CPython's own answer rather than an assumption. A row whose
    fixture turns out to reach the assert fails here rather than passing
    vacuously.
    """
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](2)
    except AssertionError:
        pytest.fail(
            f"{label}: CPython reached the assert, so this row cannot pin a DEAD "
            f"verdict. Either the fixture is wrong or the expectation is."
        )
    except (AttributeError, IndexError, KeyError, NameError, TypeError, UnboundLocalError):
        pass
    else:
        pytest.fail(f"{label}: the fixture returned normally, so it proves nothing.")

    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    assert [_is_enforced(function, asserts[0], tree)] == [False], (
        f"{label}: expected the header to be reported DEAD. A binding that has "
        f"not run, or that binds another scope's name, leaves the call reaching "
        f"the real builtin, and the empty list it returns cannot be entered."
    )


@pytest.mark.parametrize(
    ("label", "source", "arguments", "keywords"),
    SHADOWED_CALLEE_SOURCES,
    ids=[shape[0] for shape in SHADOWED_CALLEE_SOURCES],
)
def test_a_constructor_callee_shadowed_outside_the_body_is_reported_live(
    label, source, arguments, keywords
):
    """A callee shadowed by a parameter or a module binding is not a builtin.

    #388-5b. The shadowing check looked only inside the function body, so two
    bindings that are never *written* in the body were invisible:

    * the **signature** -- a parameter binds for the whole call, so
      ``def outer(list, x): cs = list()`` calls whatever the caller passed and
      never the builtin ``list``;
    * **module scope** -- a module-level ``def list(): ...`` binds the name for
      every function in the file, and a module-level ``cs = list()`` has
      already run by the time any header reads it.

    Both were read as the builtin, and both are a **false-dead** when the shadow
    happens to return a real context manager: the tool reported the header DEAD
    where CPython enters it and the assert is genuinely reachable. That is the
    damaging direction -- it certifies a live contract as swallowed -- so it is
    the direction these rows pin.

    Every row is executed first, so the LIVE verdict is checked against CPython's
    own answer rather than asserted from the fixture's shape. A row whose
    fixture does not actually reach the assert fails here rather than passing
    vacuously.
    """
    assert _shadowed_callee_reaches_assert(label, source, arguments, keywords) is True, (
        f"{label}: CPython does not reach the assert here, so the row cannot "
        f"pin a LIVE verdict. Either the fixture is wrong or the expectation is."
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    assert [_is_enforced(function, asserts[0], tree)] == [True], (
        f"{label}: expected the header to be reported LIVE. A callee shadowed "
        f"by a parameter or a module binding is a different callable, so the "
        f"store must be read as possibly-enterable and the assert kept."
    )


#: The round-8 review's findings against `31e3b22`. Both are holes in the
#: round-7 narrowing, and both are the same shape of mistake: a rule that
#: proved one part of a path and then stopped looking.
#:
#: The obvious control for the first finding -- a store in the `else` of a
#: never-true `if`, whose arm *is* taken -- is deliberately absent. That
#: spelling is a **pre-existing** false-live: the carrier is bound at module
#: scope, and a settled `cs = list()` makes the header raise exactly as
#: `cs = list()` written straight at module scope does. It answers identically
#: on `fee41bf`, `5535135`, `b90f985` and `31e3b22`, so it is a module-carrier
#: limitation rather than a gap in this rule, and pinning it here would assert
#: a fix this series has not made. `test_a_block_nested_module_carrier_is_still_declined`
#: is where that limitation is already recorded.
#:
#: The mirror shape -- an `else` whose *own* test is undecidable, such as
#: `if os.name: ... else: cs = list()` inside an `if True:` -- is also absent,
#: for the same reason. The helper reads that arm as runnable, which is
#: correct, but the resulting verdict is the same module-carrier false-DEAD.
#: Dropping the always-true test from `_statement_in_unreachable_arm` *does*
#: change that shape's verdict, so the test is not redundant; it is merely
#: unfalsifiable here, because the module-carrier limit masks the answer.
ROUND_EIGHT_SOURCES = (
    # Review finding 1. The `if True:` above made the block look like an
    # always-true branch, and the store in the *unreachable* `else` beside the
    # inner `if True:` was then read as one that always runs. It never does:
    # the inner condition is true, so the `else` is skipped, and `cs` is still
    # the `nullcontext()` bound above the branch.
    (
        "a store in the unreachable else arm of an always-true test",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "if True:\n"
            "    if True:\n"
            "        pass\n"
            "    else:\n"
            "        cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The same hole one `elif` deeper, which is an `else` whose body is another
    # `if` and so is reached through the same test.
    (
        "a store in the else arm of a nested always-true test",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "if True:\n"
            "    if True:\n"
            "        if True:\n"
            "            pass\n"
            "        else:\n"
            "            cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The positive control on the `elif` side. An `elif` is an `else` whose
    # body is another `if`, and the whole arm is skipped while the test above
    # it holds -- so behind `if False:` even a *later* `elif` is dead and the
    # header is still the `nullcontext`. The trailing `else` restores a real
    # manager, so the store in the dead `elif` arm changes nothing and the
    # header is entered. A rule that walked into the `elif` through the
    # parent's test would settle that store and report this header DEAD.
    (
        "a store in an elif arm behind a never-true elif",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "if False:\n"
            "    cs = list()\n"
            "elif True:\n"
            "    pass\n"
            "elif False:\n"
            "    cs = list()\n"
            "else:\n"
            "    cs = nullcontext()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The same hole reached through an `elif` rather than a plain `else`, and
    # the row that fails if the arm is not covered at all. The test above is
    # true, so the `elif` never runs, `cs` is still the `nullcontext` bound at
    # the top, and the header is entered.
    (
        "a store in an elif arm behind a chain of always-true tests",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "if True:\n"
            "    pass\n"
            "elif True:\n"
            "    cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # Review finding 2. A string literal is not one of the container literals
    # the empty-iterable test matched, so `for _ in "":` -- which yields
    # nothing, exactly like `for _ in ():` -- was read as a body that runs.
    (
        "an always-true branch inside a loop over an empty string",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            'for _ in "":\n'
            "    if True:\n"
            "        cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The same hole by the zero-argument `set()` spelling, which is a call
    # rather than a literal and so is a third shape the loop test had to
    # cover. `set([1])` is *not* empty, which is why the argument list is
    # checked rather than the callee name alone.
    (
        "an always-true branch inside a loop over an empty set call",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "for _ in set():\n"
            "    if True:\n"
            "        cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The positive control. `"a"` has one element, so the loop body *does* run
    # and the store settles the name. Reading every string as empty would
    # report this header LIVE.
    (
        "an always-true branch inside a loop over a non-empty string",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            'for _ in "a":\n'
            "    if True:\n"
            "        cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    (
        "a store in an elif arm that always runs settles the name",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "if False:\n"
            "    pass\n"
            "elif True:\n"
            "    cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    (
        "an elif True store behind a conditional if is not unconditional",
        (
            "from contextlib import nullcontext\n"
            "def outer(x):\n"
            "    cs = nullcontext()\n"
            "    if x:\n"
            "        pass\n"
            "    elif True:\n"
            "        cs = list()\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    (
        "an elif True store behind a second never-true link settles the name",
        (
            "from contextlib import nullcontext\n"
            "def outer(x):\n"
            "    cs = nullcontext()\n"
            "    if False:\n"
            "        pass\n"
            "    elif False:\n"
            "        pass\n"
            "    elif True:\n"
            "        cs = list()\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    (
        "a store in an else arm that always runs settles the name",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "if False:\n"
            "    pass\n"
            "else:\n"
            "    cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    (
        "a store in an elif arm behind a conditional test stays undecided",
        (
            "from contextlib import nullcontext\n"
            "flag = bool(int('1'))\n"
            "cs = nullcontext()\n"
            "if False:\n"
            "    pass\n"
            "elif flag:\n"
            "    pass\n"
            "elif True:\n"
            "    cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
)
