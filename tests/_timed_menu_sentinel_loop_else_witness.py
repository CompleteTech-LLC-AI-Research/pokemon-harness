"""Loop-else execution witnesses and outcomes.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_elif_links import (
    _assert_suppression_contract,
)
from tests._timed_menu_sentinel_literal_match_imports import (
    CARRIED_ELIF_SUPPRESSOR_SHAPES,
)


@pytest.mark.parametrize(
    ("label", "carried", "arm", "assert_is_live"),
    CARRIED_ELIF_SUPPRESSOR_SHAPES,
    ids=[row[0] for row in CARRIED_ELIF_SUPPRESSOR_SHAPES],
)
def test_an_elif_binding_a_plain_manager_does_not_retire_a_carried_suppressor(
    label, carried, arm, assert_is_live
):
    """#445: the `elif` demotion must not retire the binding it superseded.

    `ELIF_LINK_SUPPRESSOR_SHAPES` above covers the live polarity, where the arm
    binds the suppressor and the carried manager is a plain one. This is the
    mirror, and it was the damaging direction: an `elif` arm that binds
    something harmless must not be read as clearing the name. On the calls that
    skip the arm -- and the assert's own failing call is one of them -- the name
    still holds whatever was carried in from before the chain, and that is the
    value the header enters.

    So the answer follows the carried value, not the arm's spelling:

    * carried `suppress(AssertionError)` / `BaseException` / `Exception`,
      arm binds a plain manager -- the failing call skips the arm and enters
      the carried suppressor, so the assert never fires and the header is
      **defeated**;
    * carried `suppress(ValueError)` / `KeyError` -- those do not catch
      `AssertionError`, so the same skip leaves the failure to escape and the
      header is **live**.

    Reading it the other way -- asking only what the arm binds -- is what made
    the first family report live and, symmetrically, would make this one report
    live too. The rows are executed before the verdict is compared so CPython,
    not this table, decides each of them.
    """
    source = (
        "import contextlib\n"
        "def outer(x, flag, helper):\n"
        f"    with (cs := contextlib.{carried}):\n"
        "        pass\n"
        "    if x:\n"
        "        pass\n"
        "    elif True:\n"
        f"        cs = contextlib.{arm}()\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_suppression_contract(label, source, assert_is_live)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [assert_is_live], (
        f"{label}: expected verdicts [{assert_is_live}], got {results}. An `elif` "
        f"arm runs only on the calls where every test above it failed, so the "
        f"binding it supersedes still holds on the rest. Demoting the arm must "
        f"not retire that carried binding: the header is defeated whenever the "
        f"value in force on the skipped path swallows AssertionError."
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


#: #451, the import spelling of the filed fixture. The witness keeps itself
#: honest by requiring every statement before the loop to be a `pass` or a
#: plain, readable assignment -- anything else is an effect it has not
#: accounted for. A function-local `import contextlib` is neither, so the
#: witness declined and the assert came back **defeated** on the exact fixture
#: issue #451 filed, while the module-scope spelling of the same program
#: passed:
#:
#:     def outer(x):
#:         import contextlib              # <- filed spelling
#:         cs = contextlib.nullcontext()
#:         for item in (1,):
#:             break                       # the else never runs
#:         else:
#:             cs = contextlib.suppress(AssertionError)
#:         with cs:                       # `cs` is still the nullcontext
#:             assert x != 1             # LIVE
#:
#: Executed on CPython 3.12 the assert fires for both spellings. The table
#: above pins only the module-scope one, so without these rows the gap is
#: invisible to the lane.
#:
#: The rows are the two imports that bind the module root the witness already
#: resolved through. A *second* binding of the same name inside the scope is
#: a shadow whose winner depends on the call, so the witness still declines
#: that -- the conservative direction, and the last row pins it as a control
#: rather than leaving it untested.
LOOP_ELSE_IMPORT_SPELLINGS = (
    # The filed spelling, verbatim.
    (
        "451 filed: the import is inside the function",
        "    import contextlib\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    # The same program with the import at module scope, as a control that the
    # two spellings agree.
    (
        "451 control: the import is at module scope",
        "",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    # A `from` import of the same names. The witness resolves through the bare
    # names here rather than through the module root, so this is a distinct
    # resolution path and not a re-spelling of the first row.
    (
        "451 a local from-import of the same managers",
        "    from contextlib import nullcontext, suppress\n",
        "nullcontext()",
        "suppress(AssertionError)",
        True,
    ),
    # A second import of the same root under a different local name is not a
    # shadow of it, so the witness still proves the row.
    (
        "451 an unrelated second import does not shadow the root",
        "    import contextlib\n    import json as _j\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    # Re-importing the same module is the same program, not a conflict. Both
    # spellings bind `contextlib` to the same object, so declining either one
    # would report a live assert defeated for no reason at all.
    (
        "451 a redundant re-import of the same module is not a conflict",
        "    import contextlib\n    import contextlib\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    (
        "451 a re-import aliasing the module to its own name is not a conflict",
        "    import contextlib\n    import contextlib as contextlib\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    # -- #481. A nested `def` binds its own name and does nothing else at this
    #    point: its body is not run, no value is produced, and nothing the
    #    header enters can come from it. The pre-chain scan rejected the
    #    *statement* -- it is neither a `Pass`, an `Assign`, nor an import --
    #    so the witness never fired and this live assert was certified dead.
    (
        "481 a nested def before the header is transparent",
        "    def inner():\n        pass\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    # -- The filed spelling, where the definition's body is itself an import.
    #    That import is not what makes the row live; the definition is. It
    #    matters because "the nested def contains an import" is the shape the
    #    issue filed, and a fix that only handled the empty body would miss it.
    (
        "481 a nested def containing an import is transparent",
        "    def inner():\n        import contextlib\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    # -- This empty class body executes only `pass`, so creation is inert.
    (
        "481 a nested class before the header is transparent",
        "    class Inner:\n        pass\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    (
        "485 inert try/pass",
        "    try:\n        pass\n    except ImportError:\n        pass\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    (
        "485 canonical import try",
        "    try:\n        import contextlib\n    except ImportError:\n        pass\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    (
        "485 nested canonical try",
        "    try:\n        try:\n            import contextlib\n        except ImportError:\n            pass\n    except Exception:\n        pass\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
)


#: The declines that keep the import acceptance above from over-reaching. A
#: `from` import, a submodule import and a relative import each bind the
#: witness root to a *different* object than ``import contextlib`` does, or to
#: one this walk cannot name, so which of them is in force depends on the
#: statement order. The witness must decline rather than assume.
#:
#: These rows are NOT run through `_assert_suppression_contract`, and must not
#: be. Two of the three fixtures raise while the module is being imported --
#: `import contextlib.nullcontext` and `from . import contextlib` both fail
#: outright -- so there is no `outer` to sweep and no assert to reach. They are
#: pinned as *declines*: the correct answer is that the witness does not fire,
#: and the point of the row is that a widened rule would fire it. That is a
#: statement about the analyzer, not about CPython, so it is asserted directly.
LOOP_ELSE_IMPORT_SHADOWS = (
    (
        "a from-import shadowing the root",
        "    import contextlib\n    from os import sep as contextlib\n",
    ),
    (
        "a submodule import shadowing the root",
        "    import contextlib\n    import contextlib.nullcontext as contextlib\n",
    ),
    ("a relative import of the root", "    from . import contextlib\n"),
    # -- #481's boundary. A nested definition is transparent precisely because
    #    it binds a *local* name, but a definition spelled with a walked root's
    #    own name is a shadow wearing a definition's clothes: it rebinds
    #    `contextlib` to a function object, so `contextlib.nullcontext()` in the
    #    header raises and the witness must decline rather than resolve through
    #    a root that is no longer the module.
    (
        "a nested def shadowing the walked root",
        "    def contextlib():\n        pass\n",
    ),
    (
        "a try that rebinds the carried manager",
        "    try:\n        cs = contextlib.suppress(AssertionError)\n    except Exception:\n        pass\n",
    ),
    (
        "a try whose finally rebinds the carried manager",
        "    try:\n        import os\n    finally:\n        cs = contextlib.suppress(AssertionError)\n",
    ),
    (
        "a try whose handler binds a name",
        "    try:\n        helper()\n    except Exception as exc:\n        pass\n",
    ),
    (
        "a try whose body defines a shadowing name",
        "    try:\n        def cs():\n            pass\n    except Exception:\n        pass\n",
    ),
    ("a try whose body returns", "    try:\n        return\n    except Exception:\n        pass\n"),
)


@pytest.mark.parametrize(
    ("label", "preamble", "carrier", "arm", "assert_is_live"),
    LOOP_ELSE_IMPORT_SPELLINGS,
    ids=[row[0] for row in LOOP_ELSE_IMPORT_SPELLINGS],
)
def test_a_loop_else_witness_survives_the_import_spelling(
    label, preamble, carrier, arm, assert_is_live
):
    """The `break` skips the loop `else` whichever way `contextlib` is imported.

    This is the second half of #451. The module-scope spelling of the filed
    fixture was already repaired and pinned, but the fixture in the *issue*
    imports `contextlib` inside the function -- and the two are the same
    program with different verdicts, which is the damaging direction: a live
    assert certified unreachable, on the text the issue actually filed.

    The cause is the pre-chain scan in `_elif_witness_reaches_header`, which
    accepts only `pass` and a plain readable assignment so that no unmodelled
    effect can sit between the carrier and the loop. A local `import` is a
    binding and nothing else -- it cannot rebind the manager, cannot raise on
    a value the assert depends on, and cannot skip the header -- so it is now
    accepted, but only when it cannot change what the header enters: #475
    settled that question by asking whether the name the import binds is one
    the witness resolves `contextlib.nullcontext` / `contextlib.suppress`
    through, and requiring it to be the real `contextlib` when it is.

    Three of these rows were measured as false-DEADs on `49b899a` and were
    retired as stale when this branch was first built. #475 then repaired
    exactly those shapes, so the rows are correct again and the retirement is
    reverted here; the table is back because the assertions it holds are true
    again, not because the original text was wrong about the base it was
    measured on.

    Every row is executed across the swept domain by
    :func:`_assert_suppression_contract` before the analyzer's verdict is
    compared, so CPython decides each row rather than this table.
    """
    source = (
        "import contextlib\n"
        "def outer(x, flag, helper):\n"
        f"{preamble}"
        f"    cs = {carrier}\n"
        "    for item in (1,):\n"
        "        break\n"
        "    else:\n"
        f"        cs = {arm}\n"
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
        f"{label}: expected verdicts [{assert_is_live}], got {results}. A `break` "
        f"skips the loop's `else`, so the suppressor bound there is never "
        f"installed and the header enters the carried `{carrier}`. Where "
        f"`contextlib` is imported must not change that: a function-local "
        f"import is a binding, not an effect the witness has to model."
    )


@pytest.mark.parametrize(
    ("label", "preamble"),
    LOOP_ELSE_IMPORT_SHADOWS,
    ids=[row[0] for row in LOOP_ELSE_IMPORT_SHADOWS],
)
def test_a_loop_else_witness_declines_an_import_that_shadows_its_root(label, preamble):
    """An import that rebinds the witness root to something else is declined.

    The pre-chain scan in `_elif_witness_reaches_header` accepts an import
    because an import binds names and is not an effect the walk has to model.
    That is only sound while the import does not *rebind the root the witness
    resolved through*: once two different objects share the name `contextlib`,
    which one the header sees depends on the order of the statements, and
    `_binding_order` keys by top-level statement, so it cannot separate them.

    The source-inert try store later overwritten by nullcontext is now proved
    by #361's structured witness and checked by execution. Other shapes decline.
    A decline reports the assert as defeated when it
    fires, which is the false-DEAD direction -- but the alternative is claiming
    a resolution the source does not determine, and that is how this module has
    historically produced false-LIVEs. The narrow witness is the safe error.

    Asserted directly rather than through `_assert_suppression_contract`: two
    of these fixtures cannot be imported at all, so there is no execution to
    compare against, and inventing one would be exactly the reconstruction this
    repository's ground-truth rules forbid.
    """
    source = (
        "import contextlib\n"
        "def outer(x, flag, helper):\n"
        f"{preamble}"
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
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    expected = label == "a try that rebinds the carried manager"
    if expected:
        # #361 now proves this inert normal try path and later overwrite.
        # Execute its complete source independently of the analyzer.
        namespace = {}
        exec(source, namespace)  # noqa: S102 - independent runtime oracle
        with pytest.raises(AssertionError):
            namespace["outer"](1, False, None)
    assert _is_enforced(function, target, tree) is expected, (
        f"{label}: the witness fired on an import that rebinds the module root "
        f"it resolved through. Which of two bindings of one name is in force "
        f"depends on statement order, so the witness must decline rather than "
        f"claim a resolution the source does not determine."
    )


#: #451. A guard whose evaluation *raises* is not a guard that may hold -- it is
#: a guard after which the loop body never completes, so the ``else`` is never
#: installed and the assert under test is never evaluated at all. Reading that
#: as "possibly reachable" reports a header live whose code cannot run.
#:
#: The witness still treats a guard it merely *cannot read* (``is``, ``in``,
#: a chain) as possibly-true, which is conservative and correct: there the
#: ``else`` genuinely may or may not run. The two cases used to share one
#: ``None``, and only one of them is safe that way.
#:
#: These rows pin the raising case. ``Eq``/``NotEq`` are deliberately absent
#: from the raising set: ``1 == "a"`` is ``False`` in Python rather than an
#: error, so a mixed-type equality leaves the loop able to complete and must not
#: be treated this way. Those are the controls, and a repair that swept them in
#: alongside the ordering operators would make the first a false-DEAD.
LOOP_ELSE_RAISING_GUARDS = (
    ('x < "a"', False),
    ('x > "a"', False),
    ('x <= "a"', False),
    ('x >= "a"', False),
    ('"a" < x', False),
    ('1 < "a"', False),
    ('x == "a"', False),
    ('x != "a"', True),
)


@pytest.mark.parametrize(
    ("guard", "assert_is_live"),
    LOOP_ELSE_RAISING_GUARDS,
    ids=[f"guard {row[0]}" for row in LOOP_ELSE_RAISING_GUARDS],
)
def test_a_loop_else_witness_declines_a_guard_that_raises(guard, assert_is_live):
    """#451: evaluating the guard can raise, which decides it, not a maybe.

    `1 < "a"` raises `TypeError` in CPython, so the loop body never finishes and
    the `else` never runs. There is no suppressor to swallow the assert and no
    contract to certify, so the header cannot be reported live.

    This is the damaging direction the whole #451 witness exists to avoid: a
    sentinel certified load-bearing whose code cannot run. The rows are executed
    before the verdict is compared, so CPython decides whether the guard raises
    rather than this table.
    """
    source = (
        "import contextlib\n"
        "def outer(x, flag, helper):\n"
        "    cs = contextlib.nullcontext()\n"
        "    for item in (1,):\n"
        f"        if {guard}:\n"
        "            break\n"
        "    else:\n"
        "        cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n"
        "        assert x != 1\n"
    )
    _assert_suppression_contract(f"451 raising guard {guard}", source, assert_is_live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    results = _is_enforced(function, target, tree)
    assert results is assert_is_live, (
        f"451 raising guard {guard}: expected {assert_is_live}, got {results}. "
        f"This guard raises rather than answering, so the loop cannot complete "
        f"and the else cannot be installed. Treating it as possibly-reachable "
        f"certifies a header that can never run."
    )


#: #464. A ``with`` header whose walrus value is a bare ``Name`` enters
#: whatever that name holds. #390 fixed the ``ast.Lambda`` spelling by reading
#: the entered value's own runtime type, but ``_literal_runtime_type`` declines
#: a plain ``Name``, so every name-valued walrus header kept the assert live.
#: When the name holds a function object the header raises ``TypeError`` while
#: it is being evaluated -- before the body is entered -- so the assert under it
#: never runs at all. Reporting that *enforced* is the damaging direction.
#:
#: Every row below is executed across the swept domain by
#: :func:`_assert_suppression_contract` before the analyzer's answer is
#: compared, so CPython decides each verdict rather than this table. The rows
#: that reach CPython's ``True``-shaped outcome are the controls: a repair that
#: swept the whole ``NamedExpr`` family into "dead" would fail them.
#:
#: The two rows that stay **live** are the deliberate declines, and they are
#: what keeps the repair honest:
#:
#: * a *parameter* is chosen by the caller. ``def outer(x, helper)`` enters
#:   ``helper``, which is live when the caller passes ``nullcontext()`` and
#:   raises when it passes ``1``. The function body does not determine the
#:   answer, so declining is the only honest verdict -- and answering dead
#:   would drop a real contract.
#: * a genuinely *unbound* name is a ``NameError`` on entry. Master already
#:   pins that as a live header (``bare name in the header``), and a fix that
#:   flipped it would contradict that row.
#:
#: The repair resolves only what the scope can actually determine: a local
#: store, an alias chain through local stores, a module-level store, and a
#: ``def``/``class`` carrier. Everything else declines.
WALRUS_NAME_ENTRY_SHAPES = (
    # -- The filed defect, reached through a local alias rather than a
    #    parameter: `h = _h` binds the *same* function object the header enters.
    (
        "464 local alias to a function",
        "def _maker():\n    return None\n",
        "    h = _maker\n",
        "h",
        False,
    ),
    # -- A module-level store whose value is a literal.
    (
        "464 module constant bound to an int",
        "CS = 1\n",
        "",
        "CS",
        False,
    ),
    # -- A module-level store whose value is an alias of a `def`. Two hops from
    #    the header: module store -> module `def` carrier.
    (
        "464 module constant aliased to a def",
        "def _maker():\n    return None\nCS = _maker\n",
        "",
        "CS",
        False,
    ),
    # -- A local `def` carrier reached by name rather than through a lambda.
    (
        "464 local def carrier",
        "",
        "    def _maker():\n        pass\n",
        "_maker",
        False,
    ),
    # -- Controls. These really are enterable, so a repair that answered "dead"
    #    for any name it could resolve would fail them.
    (
        "464 control: a real context manager is live",
        "",
        "",
        "contextlib.nullcontext()",
        True,
    ),
    (
        "464 control: a suppressor swallows, so the header is defeated",
        "",
        "",
        "contextlib.suppress(AssertionError)",
        False,
    ),
    # -- #464 residual. The repair above resolves a name through the store
    #    machinery, but the *value* that store binds was still unreadable for
    #    two whole families, so each of these stayed a false-LIVE:
    #
    #    * a zero-argument builtin constructor -- `m = list()`. The callee
    #      fixes the result, exactly as it does for `cs = list()`.
    #    * a bare builtin *name* -- `m = int`, `m = len`. The name is the
    #      object; there is no call to be opaque.
    (
        "464 residual: a local alias to a builtin constructor call",
        "",
        "    m = list()\n",
        "m",
        False,
    ),
    (
        "464 residual: a local alias to a bare builtin class",
        "",
        "    m = int\n",
        "m",
        False,
    ),
    (
        "464 residual: a local alias to a bare builtin function",
        "",
        "    m = len\n",
        "m",
        False,
    ),
    (
        "464 residual: a local alias to object()",
        "",
        "    m = object()\n",
        "m",
        False,
    ),
    # -- A shadowed constructor must NOT be read as the builtin. This is the
    #    control that keeps the repair from inventing false-DEADs: `list` here
    #    returns a real context manager, so the assert is live and a repair
    #    that read the callee as the builtin would report it defeated.
    (
        "464 control: a shadowed constructor stays live",
        "",
        "    def list():\n        return contextlib.nullcontext()\n    m = list()\n",
        "m",
        True,
    ),
    # -- The *bare name* spelling has no live control, and that is the point:
    #    `m = int` binds whatever the name denotes, never its result, so it is
    #    a function object here and a class object for the real builtin. Both
    #    are unenterable and both are reported dead. Only the *call* spelling
    #    can return a context manager, which is why the shadowing control
    #    above is the one that matters.
    # -- #464 residual, second pass. Entering a *class object* is decided by
    #    its metaclass, not by the class, and `memoryview` is the one exported
    #    builtin whose class object carries `__enter__`:
    #
    #        >>> hasattr(memoryview, "__enter__")     # True -- an INSTANCE method
    #        >>> hasattr(type(memoryview), "__enter__")  # False -- metaclass `type`
    #        >>> with memoryview as v: ...
    #        TypeError: 'type' object does not support the context manager protocol
    #
    #    So the header raises before the body and the assert is unreachable,
    #    while the *instance* spelling in the control below really is
    #    enterable. Reading `hasattr(obj, "__enter__")` off the name's own
    #    value cannot separate the two and declined both, so this row was a
    #    false-LIVE. It runs on the executed oracle precisely because CPython
    #    *can* adjudicate it -- which is what the previous pin, asserted
    #    directly against the analyzer, failed to establish.
    (
        "464 residual: a bare builtin class object is entered by its metaclass",
        "",
        "    m = memoryview\n",
        "m",
        False,
    ),
    # -- The control that decides the repair above is *narrow*: only the
    #    class-object spelling is unenterable. An instance of the same class
    #    implements the protocol and CPython enters it, so the assert is live.
    #    A rule that asked the metaclass for both would report this defeated.
    (
        "464 control: a memoryview instance is genuinely enterable",
        "",
        "    m = memoryview(b'xy')\n",
        "m",
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "module_preamble", "local_preamble", "value", "assert_is_live"),
    WALRUS_NAME_ENTRY_SHAPES,
    ids=[row[0] for row in WALRUS_NAME_ENTRY_SHAPES],
)
def test_a_walrus_header_named_to_an_unenterable_value_is_not_a_live_assert(
    label, module_preamble, local_preamble, value, assert_is_live
):
    """#464: resolve the name the walrus enters, or decline -- never assume live.

    The entered object is whatever the walrus *value* names, exactly as in the
    ``ast.Lambda`` case #390 already handles. Reading only literal shapes left
    every name-valued header reporting `enforced`, which for a function object
    is a contract the interpreter never evaluates.

    Resolution goes through the store machinery this module already trusts
    (`_stores_of`, `_module_stores`, `_carrier_runtime_kinds`) rather than a
    parallel walk, so a name bound by a caller or by nothing at all still
    declines -- and declining keeps the assert live, the safe direction.
    """
    source = (
        "import contextlib\n"
        f"{module_preamble}"
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        f"{local_preamble}"
        f"    with (cs := {value}):\n"
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
        f"{label}: expected verdicts [{assert_is_live}], got {results}. A walrus "
        f"header enters the value it names, so a name bound to a function, an "
        f"int or any other non-manager raises before the body and the assert is "
        f"unreachable. A name the scope does not determine -- a parameter, or "
        f"one bound nowhere -- must stay live rather than be called dead."
    )
