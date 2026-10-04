"""Capture namespaces and binding-scope boundaries.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_entry_contracts import (
    CAPTURE_SCOPE_BOUNDARY_ROWS,
)

#: #359. A *string-field carrier* binds its name to a value the binding syntax
#: does not describe, and nothing pinned what the analyzer must answer when a
#: later ``with cs:`` raises **on entry** because of it.
#:
#: ``WALRUS_REBINDING_SHAPES`` above already pins the value-less carriers
#: (``with ... as cs``, ``except ... as cs``, ``del cs``), because those rows
#: retire the carried walrus. The five here are the ones it was missing: a
#: module, a function, a class, and a ``match`` capture are all bound by a
#: statement that reads as a store, and all four end up in the same
#: "no readable right-hand side" bucket in ``_entry_is_dead``. That bucket is
#: what makes the gap easy to lose.
#:
#: AC1 asks for each carrier pinned in both positions. The ``second_assert_live``
#: column is ``False`` for every row here, and it is that value which is the
#: point: entering raises ``TypeError`` before the body runs, so the second
#: assert is *unreachable*. Reporting it ``enforced`` is the damaging direction
#: per #308 criterion 1 -- it certifies a contract that can never fail as
#: load-bearing.
#:
#: AC3 asks for a non-carrier control so a fix cannot pass by disabling the
#: rule wholesale. Two are included below and both must stay ``True``: a plain
#: assignment of a real context manager, and a class that *does* implement the
#: protocol. Neither is in the table above, so neither is a duplicate.
#:
#: #418 adds the four starred-target rows. These are the same shape as the
#: carriers -- entering raises ``TypeError`` before the body, so the second
#: assert is unreachable -- but they are decided by a *value* the module can
#: read rather than by an unreadable one, which is why they sat in a different
#: bucket and stayed wrong. The fix keys off the target syntax alone, so the
#: right-hand side in these rows is deliberately varied: a lone element, a
#: bind-from-end-after-star tail, a usable context manager, and a plain int all
#: land on the same list.
STARRED_TARGET_ENTRY_UNREACHABLE_ROWS = (
    (
        "a starred target binds a list even from a lone suppressor",
        "    *cs, = (contextlib.suppress(AssertionError),)",
        False,
    ),
    (
        "a bind-from-end-after-star tail binds the list of the rest",
        "    a, *cs = (contextlib.suppress(AssertionError), 2)",
        False,
    ),
    (
        "a starred target holding a usable context manager is still a list",
        "    *cs, = (contextlib.nullcontext(),)",
        False,
    ),
    (
        "a starred target over a non-manager element binds a list",
        "    *cs, = (1,)",
        False,
    ),
    # Controls. A plain element binding is NOT decidable from the container's
    # syntax, so the rule declines and the assert stays live. These keep that
    # decline intact: a fix that over-corrected every destructuring target
    # would report these dead and fail.
    (
        "CONTROL a non-starred element binding of a real context manager",
        "    cs, other = (contextlib.nullcontext(), 2)",
        True,
    ),
)
CARRIER_ENTRY_UNREACHABLE_ROWS = (
    ("import-as binds the module", "    import os as cs", False),
    ("import-from-as binds the module", "    from os import path as cs", False),
    ("def binds a function object", "    def cs():\n        pass", False),
    ("class binds a type object", "    class cs:\n        pass", False),
    (
        "a match capture binds whatever it matched",
        "    match [1]:\n        case [cs]:\n            pass",
        False,
    ),
    # Controls: these leave the name enterable, so the assert is a real
    # contract. If either of these reports dead, the rule has been widened past
    # the carriers and is dropping live asserts.
    ("CONTROL a plain assign of a real context manager", "    cs = nullcontext()", True),
    (
        "CONTROL an instance of a class that implements the protocol",
        (
            "    class cs:\n"
            "        def __enter__(self):\n"
            "            return self\n"
            "        def __exit__(self, *exc):\n"
            "            return False\n"
            "    cs = cs()"
        ),
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "nested", "verdict", "runtime_live"),
    CAPTURE_SCOPE_BOUNDARY_ROWS,
    ids=[row[0] for row in CAPTURE_SCOPE_BOUNDARY_ROWS],
)
def test_a_capture_binds_only_the_namespace_it_was_written_in(label, nested, verdict, runtime_live):
    """A store in a nested namespace cannot rebind the enclosing function's name.

    This is #350. ``_scope_body_nodes`` stopped walking at a nested ``def``,
    ``async def`` or ``lambda`` but not at a ``ClassDef``, so a ``match``
    capture written in a class body was attributed to the enclosing function
    and retired its carried suppressor. The assert under the following
    ``with cs:`` was therefore reported *enforced* when it is really swallowed
    -- a dead contract certified as load-bearing.

    Measured on CPython 3.12.14 with ``x=1`` and ``helper()`` returning a
    ``nullcontext()``:

    * class body, capture      -> assert swallowed (correct verdict ``False``)
    * class body, ``import``   -> assert swallowed (correct ``False``)
    * nested ``def``, capture  -> assert swallowed (correct ``False``)
    * function body, capture   -> assert **live**

    The last row is what makes the fix safe. It answers ``True`` both before
    and after, so a repair that added ``ClassDef`` to the boundary but also
    broke function-body captures would go red here rather than passing every
    ``False`` row on the strength of the fix alone.

    An earlier draft of this row passed ``None`` as ``helper()``'s result,
    which made the capture bind ``None``, raised on entry, and failed the
    executed check for the wrong reason. The helper now returns a real
    ``nullcontext()``, so the capture binds an actual context manager and the
    ``with cs:`` really is entered.
    """
    source = (
        "def outer(x, flag, items, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        pass\n" + nested + "    with cs:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    from contextlib import nullcontext

    try:
        namespace["outer"](1, None, [1], nullcontext)
    except AssertionError:
        ran = True
    except (TypeError, UnboundLocalError, NameError):
        # Entering a non-manager raises before the body runs. Deliberately
        # narrow, matching `_assert_entry_contract`: a broader catch would let
        # a fixture that fails for an unrelated reason still pass this row.
        ran = False
    else:
        # Returned normally: the suppressor was still in force and swallowed
        # the assert.
        ran = False
    assert ran is runtime_live, (
        f"{label}: CPython says the second assert "
        f"{'ran' if ran else 'did not run'}, but the row's measured ground "
        f"truth says it should "
        f"{'run' if runtime_live else 'not run'}."
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [verdict], (
        f"{label}: expected verdicts {[verdict]}, got {results}. A "
        f"store in a nested namespace must not rebind the enclosing name."
    )


#: #383 author fixtures are retained. Opaque helper results do not establish
#: a universally live captured manager: both first rows execute with a chosen
#: nullcontext callback but deliberately pin a known proof decline (False).
#: Literal, source-proved cases are covered by test_sentinel_nonlocal_captures.
NONLOCAL_CAPTURE_SCOPE_ROWS = (
    # The filed shape. The capture binds `outer`'s `cs`, so it holds a real
    # `nullcontext` and `with cs:` is entered: the assert FIRES -> `True`.
    (
        "a nonlocal capture in a class body binds the enclosing function",
        (
            "    class C:\n"
            "        nonlocal cs\n"
            "        match [helper()]:\n"
            "            case [cs]:\n"
            "                pass\n"
        ),
        False,
        True,
    ),
    # Same defect reached through a nested `def` rather than a class. #383
    # measured the class spelling; the walk that missed it is scope-agnostic,
    # so the function spelling was damaging on every tree tested too.
    (
        "a nonlocal capture in a nested function binds the enclosing function",
        (
            "    def inner():\n"
            "        nonlocal cs\n"
            "        match [helper()]:\n"
            "            case [cs]:\n"
            "                pass\n"
            "    inner()\n"
        ),
        False,
        True,
    ),
    # Controls that must NOT change. The `nonlocal` is what makes the capture
    # reach outward; without it the class body has its own namespace and the
    # carried suppressor is still in force, so the assert is swallowed. These
    # are #350's pinned rows restated beside the new ones, because a repair
    # that simply stopped resolving captures anywhere would pass every `True`
    # row above while breaking them.
    (
        "CONTROL a capture in a class body without nonlocal binds the class",
        ("    class C:\n        match [helper()]:\n            case [cs]:\n                pass\n"),
        False,
        False,
    ),
    (
        "CONTROL a capture in a nested function without nonlocal binds that function",
        (
            "    def inner():\n"
            "        match [helper()]:\n"
            "            case [cs]:\n"
            "                pass\n"
            "    inner()\n"
        ),
        False,
        False,
    ),
    # A capture in the function's OWN scope already retired the suppressor and
    # is unchanged. It is here as the no-nested-scope control, and it uses the
    # same irrefutable `case cs:` spelling as the two rows above: a refutable
    # capture over an opaque `helper()` is declined by #369, which would make
    # the row pass for the wrong reason.
    (
        "CONTROL a capture in the function body does retire the suppressor",
        ("    match helper():\n        case cs:\n            pass\n"),
        True,
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "nested", "verdict", "runtime_live"),
    NONLOCAL_CAPTURE_SCOPE_ROWS,
    ids=[row[0] for row in NONLOCAL_CAPTURE_SCOPE_ROWS],
)
def test_a_nonlocal_capture_binds_the_enclosing_function(label, nested, verdict, runtime_live):
    """Retain original runtime inputs while distinguishing opaque proof declines."""
    source = (
        "def outer(x, flag, items, helper):\n"
        "    import contextlib\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        pass\n" + nested + "    with cs:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    from contextlib import nullcontext

    try:
        namespace["outer"](1, None, [1], nullcontext)
    except AssertionError:
        ran = True
    except (TypeError, UnboundLocalError, NameError):
        ran = False
    else:
        ran = False
    assert ran is runtime_live, (
        f"{label}: CPython says the second assert "
        f"{'ran' if ran else 'did not run'}, but the row's measured ground "
        f"truth says it should "
        f"{'run' if runtime_live else 'not run'}."
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [verdict], (
        f"{label}: expected verdicts {[verdict]}, got {results}. A `nonlocal` "
        f"capture retires the enclosing function's carried suppressor."
    )


#: #399. A `match` capture is only a binding while it is the most recent
#: write. A store written below it in the same clause overwrites it, so the
#: capture cannot be the value a later `with` header reads.
#:
#: The capture and that store share one top-level `match` statement, so
#: `_binding_order` gives them the same key and the capture was counted as a
#: competing binding. The name was recorded `AMBIGUOUS`, which downstream reads
#: as "may be a suppressor" â€” so a header CPython enters happily was reported
#: defeated. That is the damaging direction under #308 criterion 1.
#:
#: Rows are executed. `verdict` is the analyzer's answer; `runtime_live` is
#: what CPython did, so no row can be satisfied by a static expectation.
MATCH_CAPTURE_SHADOWED_ROWS = (
    # The filed shape. The capture binds the literal `1`, the store below it
    # rebinds to a real `nullcontext`, and `with cs:` is entered, so the assert
    # FIRES and the header is live.
    (
        "a capture shadowed by a later store is not the value in force",
        (
            "    match [1]:\n"
            "        case [cs]:\n"
            "            cs = contextlib.nullcontext()\n"
            "            with cs:\n"
            "                assert x != 1\n"
        ),
        True,
        True,
    ),
    # Same defect with the shadowing store nested one level down. The store is
    # conditional, so the `flag=False` path still reads the captured `1` â€” but
    # the *contract* is live because CPython can reach a firing call, and the
    # analyzer returns one verdict per AST, so the ambiguity marker is the only
    # answer available and it is the one that drops the contract.
    (
        "a capture shadowed by a later store inside a branch",
        (
            "    match [1]:\n"
            "        case [cs]:\n"
            "            if flag:\n"
            "                cs = contextlib.nullcontext()\n"
            "            with cs:\n"
            "                assert x != 1\n"
        ),
        True,
        True,
    ),
    # Controls. These must NOT change: the capture is still the value in
    # force, and both are genuinely swallowed.
    (
        "CONTROL an unshadowed capture of a suppressor is still swallowed",
        (
            "    match [contextlib.suppress(AssertionError)]:\n"
            "        case [cs]:\n"
            "            with cs:\n"
            "                assert x != 1\n"
        ),
        False,
        False,
    ),
    (
        "CONTROL a capture shadowed by a suppressor is still swallowed",
        (
            "    match [1]:\n"
            "        case [cs]:\n"
            "            cs = contextlib.suppress(AssertionError)\n"
            "            with cs:\n"
            "                assert x != 1\n"
        ),
        False,
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "clause", "verdict", "runtime_live"),
    MATCH_CAPTURE_SHADOWED_ROWS,
    ids=[row[0] for row in MATCH_CAPTURE_SHADOWED_ROWS],
)
def test_a_capture_shadowed_by_a_later_store_is_not_the_value_in_force(
    label, clause, verdict, runtime_live
):
    """A `match` capture that a later store overwrites cannot bind the header.

    This is #399. A capture is a write like any other, so it holds the name
    only until the next write. Once a store below it in the same clause has run,
    the captured value is gone and the `with` header reads the store's value:

        match [1]:
            case [cs]:
                cs = contextlib.nullcontext()   # overwrites the capture
                with cs:                      # `cs` is the nullcontext
                    assert x != 1             # fires

    Measured on unfixed master `a5cece2`, analyzer `False` against runtime
    **live**. `False` here is the damaging direction: the sentinel suite stops
    counting an assert that still holds, so a defeated contract and a live one
    become indistinguishable in the enforced set.

    The fix compares the *source position* of the binding statements rather
    than :func:`_binding_order`. The capture and the store that shadows it live
    inside the same top-level `match` statement, so the order key ties by
    construction and cannot separate them; a store in a clause body is written
    after the `match` that owns the capture, which line and column can see.

    Two captures of one name keep the ambiguity marker â€” which clause ran is a
    runtime fact, so the value stays undecidable. The control rows pin that the
    `AMBIGUOUS` answer survives everywhere it is still correct.
    """
    source = "def outer(x, flag, helper):\n    import contextlib\n" + clause
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102

    live = False
    for probe_x in (0, 1, 2, -1):
        for probe_flag in (True, False):
            try:
                namespace["outer"](probe_x, probe_flag, None)
            except AssertionError:
                live = True
                break
            except (TypeError, UnboundLocalError, NameError):
                continue
            else:
                continue
        if live:
            break
    assert live is runtime_live, (
        f"{label}: CPython fires the assert on some input "
        f"{'and the row says it should' if live else 'and the row says it should not'}"
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [verdict], (
        f"{label}: expected verdicts {[verdict]}, got {results}. A capture that a "
        f"later store overwrites cannot be the value the header reads."
    )
