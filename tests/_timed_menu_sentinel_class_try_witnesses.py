"""Class scope and try-handler execution witnesses.

Actual test functions and literal cases from the original collector.
"""

import ast
import sys

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_elif_links import (
    _assert_suppression_contract,
)
from tests._timed_menu_sentinel_walrus_binding import (
    TRY_UNKNOWN_IMPORT_RUNTIME_ROWS,
)


@pytest.mark.parametrize(
    ("label", "preamble", "carrier", "arm", "runtime_live"), TRY_UNKNOWN_IMPORT_RUNTIME_ROWS
)
def test_try_unknown_import_above_the_loop_else_is_transparent(
    label, preamble, carrier, arm, runtime_live
):
    """Retain actual missing-import outcomes separately from source proof.

    An unknown initializer may raise another exception or mutate contextlib;
    local importability is not a portable source contract. These remain known
    proof declines even though the measured absent-module execution is live.
    """
    source = (
        "import contextlib\ndef outer(x, flag, helper):\n"
        + preamble
        + (
            f"    cs = {carrier}\n    for item in (1,):\n        break\n    else:\n        cs = {arm}\n"
            "    with cs:\n        assert x != 1\n"
        )
    )
    _assert_suppression_contract(label, source, runtime_live)
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    # Runtime truth and portable source proof are distinct for unknown imports.
    assert runtime_live is True
    assert _is_enforced(function, target, module) is False


#: #485. Preserve all author import inputs and measured runtime outcomes.
#: Unknown initializer effects are not proven by their import binding names;
#: the portable checker declines these rows separately from their execution.
TRY_ABOVE_LOOP_ELSE_BOUNDARY_ROWS = (
    # Runtime reaches the header in this environment; source proof declines.
    (
        "reached a missing import caught by ImportError",
        "    try:\n        import nope_missing_xyz\n    except ImportError:\n        pass\n",
        True,
    ),
    (
        "reached a missing import caught by a bare except",
        "    try:\n        import nope_missing_xyz\n    except:\n        pass\n",
        True,
    ),
    (
        "reached a missing import caught by a superset handler",
        "    try:\n        import nope_missing_xyz\n    except BaseException:\n        pass\n",
        True,
    ),
    (
        "reached a from-import of an unrelated module",
        "    try:\n        from json import loads\n    except ImportError:\n        pass\n",
        True,
    ),
    (
        "reached a missing import behind a nested admitted try",
        (
            "    try:\n        try:\n            import nope_missing_xyz\n"
            "        except ImportError:\n            pass\n"
            "    except Exception:\n        pass\n"
        ),
        True,
    ),
    # --- controls: control does not reach the header, so the assert is dead ---
    (
        "stopped an uncaught missing import",
        "    try:\n        import nope_missing_xyz\n    except ValueError:\n        pass\n",
        False,
    ),
    (
        "stopped a handler that returns",
        "    try:\n        import nope_missing_xyz\n    except ImportError:\n        return\n",
        False,
    ),
    (
        "stopped a handler that re-raises",
        "    try:\n        import nope_missing_xyz\n    except ImportError:\n        raise ValueError\n",
        False,
    ),
    (
        "stopped a finally that returns",
        (
            "    try:\n        import nope_missing_xyz\n    except ImportError:\n        pass\n"
            "    finally:\n        return\n"
        ),
        False,
    ),
)


@pytest.mark.parametrize(("label", "preamble", "enforced"), TRY_ABOVE_LOOP_ELSE_BOUNDARY_ROWS)
def test_try_above_loop_else_boundary(label, preamble, enforced):
    """Unknown import rows retain runtime truth and an explicit proof decline."""
    source = (
        "import contextlib\ndef outer(x, flag, helper):\n"
        + preamble
        + (
            "    cs = contextlib.nullcontext()\n    for item in (1,):\n        break\n"
            "    else:\n        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n        assert x != 1\n"
        )
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    fired, other = [], []
    for value in (0, 1, 2, -1):
        try:
            namespace["outer"](value, True, None)
        except AssertionError:
            fired.append(value)
        except BaseException as error:  # noqa: BLE001 - any failure is recorded
            other.append((value, type(error).__name__))
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False, label
    # Admitting a row is only sound while the assert genuinely fires, and
    # declining one only while the header is not reached with a live assert.
    assert bool(fired) is enforced, f"{label}: fired={fired} other={other}"
    # A `stopped` row has to be stopped for a reason the row describes. Every
    # one of them stops by raising something else or by returning, never by
    # returning cleanly past a suppressor that is actually installed.
    if not enforced:
        assert fired == [], f"{label}: fired={fired}"


#: #485. Unknown module availability/caller callbacks remain explicit declines.
#: The shadowing try-import now declines an unknown/unbound local root.
TRY_ABOVE_LOOP_ELSE_NEIGHBOURING_ROWS = (
    (
        "a named handler target is not yet admitted",
        "    try:\n        import nope_missing_xyz\n    except ImportError as exc:\n        pass\n",
        False,
        True,
    ),
    (
        "an import shadowing the walked root is refused, and the fixture never runs",
        "    try:\n        import fake as contextlib\n    except ImportError:\n        pass\n",
        False,
        False,
    ),
    (
        "an opaque helper call is not admitted",
        "    try:\n        helper()\n    except Exception:\n        pass\n",
        False,
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "preamble", "enforced", "fires"), TRY_ABOVE_LOOP_ELSE_NEIGHBOURING_ROWS
)
def test_485_records_neighbouring_forms_without_claiming_them(label, preamble, enforced, fires):
    """Record unresolved callbacks/imports and the repaired unknown root decline."""
    source = (
        "import contextlib\ndef outer(x, flag, helper):\n"
        + preamble
        + (
            "    cs = contextlib.nullcontext()\n    for item in (1,):\n        break\n"
            "    else:\n        cs = contextlib.suppress(AssertionError)\n"
            "    with cs:\n        assert x != 1\n"
        )
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    fired, other = [], []
    for value in (0, 1, 2, -1):
        try:
            namespace["outer"](value, True, None)
        except AssertionError:
            fired.append(value)
        except BaseException as error:  # noqa: BLE001 - any failure is recorded
            other.append((value, type(error).__name__))
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is enforced, label
    assert bool(fired) is fires, f"{label}: fired={fired} other={other}"


#: #358. A ``match`` clause the subject cannot select does not run, so a store
#: written in that clause's body is a store that never executes. Each row is
#: judged on an **execution sweep** of its own subject: the fixture is run and
#: the assert is watched, and only then is ``_is_enforced`` consulted. ``fires``
#: and ``enforced`` are two independent measurements and the test asserts
#: both, so a row cannot pass by agreeing with the implementation.
#:
#: The ``unselected`` rows are the defect. A sequence pattern cannot select a
#: string subject -- ``match`` deliberately excludes ``str`` from structural
#: sequence matching -- so the carried suppressor stays in force, the assert is
#: swallowed on every input, and reporting it ``enforced`` certifies a disarmed
#: contract. That is the damaging direction.
#:
#: The ``selected`` rows are the guard: the same store, in a clause the subject
#: really selects, does run and does retire the suppressor, so the assert fires
#: and must keep reading ``enforced``.
UNSELECTED_MATCH_CASE_STORE_ROWS = (
    (
        "358 a `case ['nope']:` body store does not retire the carried suppressor",
        "    flag = 'subject'\n    match flag:\n        case ['nope']:\n"
        + "            cs = contextlib.nullcontext()\n        case _:\n            pass\n",
        False,
    ),
    (
        "358 a `case {'k': 1}:` body store against a str subject does not retire it",
        "    flag = 'subject'\n    match flag:\n        case {'k': 1}:\n"
        + "            cs = contextlib.nullcontext()\n        case _:\n            pass\n",
        False,
    ),
    (
        "358 a `case 'nope':` value-pattern body store that cannot match does not retire it",
        "    flag = 'subject'\n    match flag:\n        case 'nope':\n"
        + "            cs = contextlib.nullcontext()\n        case _:\n            pass\n",
        False,
    ),
    # --- controls: these clauses really are selected, so the store runs ---
    (
        "358 control a selected `case ['nope']:` on a list subject still supersedes",
        "    flag = ['nope']\n    match flag:\n        case ['nope']:\n"
        + "            cs = contextlib.nullcontext()\n        case _:\n            pass\n",
        True,
    ),
    (
        "358 control a selected `case 'nope':` on the matching str still supersedes",
        "    flag = 'nope'\n    match flag:\n        case 'nope':\n"
        + "            cs = contextlib.nullcontext()\n        case _:\n            pass\n",
        True,
    ),
    (
        "358 control a selected capture-pattern clause still supersedes",
        "    flag = ['captured']\n    match flag:\n        case ['captured']:\n"
        + "            cs = contextlib.nullcontext()\n        case _:\n            pass\n",
        True,
    ),
)


@pytest.mark.parametrize(("label", "match_block", "fires"), UNSELECTED_MATCH_CASE_STORE_ROWS)
def test_a_store_in_an_unselected_match_case_does_not_retire_a_carried_suppressor(
    label, match_block, fires
):
    """#358. Only a clause the subject really selects may supersede the value.

    The sweep runs the fixture and records whether the assert raises, so a row
    is a claim about CPython *and* about the rule; neither can be satisfied by
    the other.
    """
    source = (
        "import contextlib\ndef probe():\n"
        "    x = 1\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n        pass\n"
        + match_block
        + "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    raised = False
    try:
        namespace["probe"]()
    except AssertionError:
        raised = True
    assert raised is fires, label
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    # `enforced` must track the runtime: True exactly when the assert fires.
    assert _is_enforced(function, target, module) is fires, label


#: #365. A store written in a branch that can never run must not retire the
#: carried suppressor. Each row is judged on an **execution sweep** of its whole
#: domain: the fixture is run and the assert is watched, and only then is
#: ``_is_enforced`` consulted. ``enforced`` and ``fires`` are therefore two
#: independent measurements, and the test asserts both -- so a row can never
#: pass by agreeing with the implementation.
#:
#: The ``unreachable`` rows are the defect: the assert is swallowed on every
#: input, so reporting it enforced certifies a disarmed contract.
#:
#: The ``reachable`` rows are the guard. ``if flag:`` is not a literal, so
#: ``_falsy_literal`` declines it and the store keeps competing -- and it
#: genuinely does supersede on the ``flag`` path, where the assert fires. The
#: ``else`` row is the guard for the other side: ``If.body`` and ``If.orelse``
#: are AST siblings, and the ``else`` arm is exactly the one that *does* run
#: under a falsy test, so that store must keep superseding.
UNREACHABLE_BRANCH_STORE_ROWS = (
    (
        "365 an `if False:` body store does not retire the carried suppressor",
        "    if False:\n        cs = contextlib.nullcontext()\n",
        False,
        False,
    ),
    (
        "365 an `if ():` body store does not retire it either",
        "    if ():\n        cs = contextlib.nullcontext()\n",
        False,
        False,
    ),
    (
        "365 an `if {}:` body store does not retire it either",
        "    if {}:\n        cs = contextlib.nullcontext()\n",
        False,
        False,
    ),
    (
        "365 a nested `if False:` inside a live branch also does not retire it",
        "    if flag:\n        if False:\n            cs = contextlib.nullcontext()\n",
        False,
        False,
    ),
    # --- controls: these stores really do run, so they must keep superseding ---
    (
        "control an `if flag:` store still supersedes (the assert fires on that path)",
        "    if flag:\n        cs = contextlib.nullcontext()\n",
        True,
        True,
    ),
    (
        "control the `else` of a falsy `if` still supersedes",
        "    if False:\n        pass\n    else:\n        cs = contextlib.nullcontext()\n",
        True,
        True,
    ),
)


def _unreachable_branch_store_fixture(preamble, signature="def probe(x, flag):\n"):
    return (
        "import contextlib\n"
        + signature
        + "    with (cs := contextlib.suppress(AssertionError)):\n        pass\n"
        + preamble
        + "    with cs:\n        assert x != 1\n"
    )


@pytest.mark.parametrize(("label", "preamble", "enforced", "fires"), UNREACHABLE_BRANCH_STORE_ROWS)
def test_a_store_in_a_branch_that_cannot_run_does_not_retire_a_carried_suppressor(
    label, preamble, enforced, fires
):
    """#365. Only a store that can actually run may supersede the carried value.

    The sweep runs the fixture across ``x`` and ``flag`` and records which
    inputs raise ``AssertionError``. Asserting ``bool(fired) is fires`` and
    ``_is_enforced(...) is enforced`` together means each row is a claim about
    CPython *and* about the rule, and neither can be satisfied by the other.
    """
    source = _unreachable_branch_store_fixture(preamble)
    namespace = {}
    exec(source, namespace)  # noqa: S102
    observed = []
    for value in (0, 1):
        for flag in (True, False):
            try:
                namespace["probe"](value, flag)
                observed.append((value, flag, None))
            except AssertionError:
                observed.append((value, flag, "AssertionError"))
            except BaseException as error:  # noqa: BLE001 - recorded, not ignored
                observed.append((value, flag, type(error).__name__))
    fired = [entry for entry in observed if entry[2] == "AssertionError"]
    assert bool(fired) is fires, f"{label}: {observed}"
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is enforced, f"{label}: {observed}"


@pytest.mark.parametrize("with_else", [False, True])
def test_issue365_full_two_assertions_keep_carried_suppressor(with_else):
    source = (
        "import contextlib\ndef probe(x):\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n        assert x != 1\n"
        "    if False:\n        cs = contextlib.nullcontext()\n"
        + ("    else:\n        pass\n" if with_else else "")
        + "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    assert namespace["probe"](1) is None
    module = ast.parse(source)
    function = module.body[1]
    targets = [n for n in ast.walk(function) if isinstance(n, ast.Assert)]
    assert [_is_enforced(function, n, module) for n in targets] == [False, False]


@pytest.mark.parametrize(
    ("initial", "rebind", "live"),
    [
        ("nullcontext()", "suppress(AssertionError)", True),
        ("suppress(AssertionError)", "nullcontext()", False),
    ],
)
@pytest.mark.parametrize("hops", [0, 1, 2])
def test_unreachable_literal_branch_does_not_replace_alias_source(initial, rebind, live, hops):
    source = (
        "import contextlib\ndef probe(x):\n"
        f"    first=contextlib.{initial}\n    if False:\n        first=contextlib.{rebind}\n"
    )
    name = "first"
    for index in range(hops):
        next_name = f"alias{index}"
        source += f"    {next_name}={name}\n"
        name = next_name
    source += f"    with(cs:={name}):\n        assert x != 1\n"
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["probe"](1)
    else:
        assert namespace["probe"](1) is None
    module = ast.parse(source)
    function = module.body[1]
    target = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert _is_enforced(function, target, module) is live


def test_issue365_top_level_retirement_remains_live():
    source = (
        "import contextlib\ndef probe(x):\n"
        "    with(cs:=contextlib.suppress(AssertionError)):\n        pass\n"
        "    cs=contextlib.nullcontext()\n    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    module = ast.parse(source)
    function = module.body[1]
    target = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert _is_enforced(function, target, module) is True


@pytest.mark.parametrize("constructor", ["list", "tuple", "set", "dict", "bytearray"])
def test_unreachable_if_store_proof_does_not_trust_enclosing_constructor_parameter(constructor):
    source = (
        "import contextlib\n"
        f"def parent({constructor}):\n    def probe(x):\n"
        "        with(cs:=contextlib.suppress(AssertionError)):\n            pass\n"
        f"        if {constructor}():\n            cs=contextlib.nullcontext()\n"
        "        with cs:\n            assert x != 1\n    return probe\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["parent"](lambda: [1])(1)
    module = ast.parse(source)
    function = module.body[1].body[0]
    target = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert _is_enforced(function, target, module) is True


@pytest.mark.parametrize(
    "loop",
    [
        "        while list():\n            first=contextlib.nullcontext()\n            break\n",
        "        for item in list():\n            first=contextlib.nullcontext()\n",
    ],
)
def test_literal_if_alias_filter_preserves_other_enclosing_loop_callables(loop):
    source = (
        "import contextlib\ndef parent(list):\n    def probe(x):\n"
        "        first=contextlib.suppress(AssertionError)\n"
        + loop
        + "        with(cs:=first):\n            assert x != 1\n    return probe\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["parent"](lambda: [1])(1)
    module = ast.parse(source)
    function = module.body[1].body[0]
    target = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert _is_enforced(function, target, module) is True


@pytest.mark.parametrize(
    ("subject", "pattern", "live"),
    [
        ("'subject'", "['nope']", False),
        ("b'nope'", "[*_]", False),
        ("None", "[1]", False),
        ("None", "None", True),
        ("1", "True", False),
        ("False", "True", False),
        ("True", "1", True),
        ("1.0", "1", True),
        ("False", "0", True),
    ],
)
def test_unselected_store_literal_pattern_semantics_are_executed(subject, pattern, live):
    source = (
        "import contextlib\ndef probe():\n    x=1\n"
        "    cs=contextlib.suppress(AssertionError)\n"
        f"    flag={subject}\n    with cs:\n        pass\n    match flag:\n"
        f"        case {pattern}:\n            cs=contextlib.nullcontext()\n"
        "        case _:\n            pass\n    with cs:\n        assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["probe"]()
    else:
        assert namespace["probe"]() is None
    module = ast.parse(source)
    function = module.body[1]
    target = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert _is_enforced(function, target, module) is live


def test_match_subject_store_proof_refuses_with_body_import_rebinding():
    source = (
        "import contextlib\ndef probe(x):\n"
        "    with(cs:=contextlib.suppress(AssertionError)):\n        pass\n"
        "    flag='subject'\n    with contextlib.nullcontext():\n        from sys import path as flag\n"
        "    match flag:\n        case [*_]:\n            cs=contextlib.nullcontext()\n"
        "        case _:\n            pass\n    with cs:\n        assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    module = ast.parse(source)
    function = module.body[1]
    target = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert _is_enforced(function, target, module) is True


def test_match_subject_store_proof_refuses_context_enter_callback():
    source = (
        "import contextlib\ndef probe(x):\n    class Mut:\n"
        "        def __enter__(self):\n            nonlocal flag\n            flag=['nope']\n            return self\n"
        "        def __exit__(self,*exc):return False\n"
        "    cs=contextlib.suppress(AssertionError)\n    flag='subject'\n    with Mut():pass\n"
        "    match flag:\n        case ['nope']:\n            cs=contextlib.nullcontext()\n"
        "        case _:\n            pass\n    with cs:\n        assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    module = ast.parse(source)
    function = module.body[1]
    target = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert _is_enforced(function, target, module) is True


#: #334. A store written in a nested scope binds *that* scope's name, so it
#: cannot retire a value the enclosing function still holds. Each row is judged
#: on an **execution sweep**: the fixture is run, the assert is watched, and
#: only then is ``_is_enforced`` consulted, so a row cannot pass by agreeing
#: with the implementation.
#:
#: The ``rebind`` rows are the defect. The nested store wins on order and
#: retires the carried suppressor, so the analyzer reports ``enforced`` on an
#: assert CPython swallows -- the damaging direction.
#:
#: The ``nonlocal`` row is the guard on the other side: ``nonlocal`` is the one
#: declaration that *does* rebind the enclosing binding, so that store must
#: keep retiring the carrier and the assert is genuinely live.
NESTED_SCOPE_REBIND_ROWS = (
    (
        "334 a nested plain rebind does not retire the carried suppressor",
        "    def inner():\n        cs = contextlib.nullcontext()\n    inner()\n",
        False,
    ),
    (
        "334 a nested `global` rebind does not retire it either",
        "    def inner():\n        global cs\n        cs = contextlib.nullcontext()\n    inner()\n",
        False,
    ),
    (
        "334 CONTROL a nested `nonlocal` rebind does retire it (the assert is live)",
        "    def inner():\n        nonlocal cs\n        cs = contextlib.nullcontext()\n    inner()\n",
        True,
    ),
    (
        "334 CONTROL a nested class-body store does not retire it either",
        "    class Inner:\n        cs = contextlib.nullcontext()\n",
        False,
    ),
)


@pytest.mark.parametrize(("label", "rebind", "fires"), NESTED_SCOPE_REBIND_ROWS)
def test_a_nested_scope_store_does_not_retire_a_carried_suppressor(label, rebind, fires):
    """#334. Only a store that can reach this binding may supersede it.

    ``_store_is_in_scope`` draws the boundary and already separates ``nonlocal``
    (rebinds the enclosing function's binding) from ``global`` (writes the
    module), so this row pins the *use* of that rule at the resolution site
    rather than restating it.
    """
    source = (
        "import contextlib\ndef outer(x, helper, flag=True):\n"
        "    from contextlib import suppress, nullcontext\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n        pass\n"
        + rebind
        + "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    raised = False
    try:
        namespace["outer"](1, None, True)
    except AssertionError:
        raised = True
    assert raised is fires, label
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is fires, label


DEFINITION_TIME_REBIND_ROWS = (
    (
        "known inert source setup",
        "    def before():return None\n    before()\n    def inner(arg=(cs:=contextlib.nullcontext())):pass\n",
    ),
    (
        "default with filed imports",
        "    from contextlib import suppress,nullcontext\n    def inner(arg=(cs:=nullcontext())):pass\n",
    ),
    ("function positional default", "    def inner(arg=(cs:=contextlib.nullcontext())):pass\n"),
    ("function keyword default", "    def inner(*,arg=(cs:=contextlib.nullcontext())):pass\n"),
    ("lambda default", "    inner=lambda arg=(cs:=contextlib.nullcontext()):None\n"),
    ("class base", "    class Inner((cs:=contextlib.nullcontext()).__class__):pass\n"),
    (
        "function decorator",
        "    def identity(value):return value\n    @((cs:=contextlib.nullcontext()) and identity)\n    def inner():pass\n",
    ),
    (
        "class decorator",
        "    def identity(value):return value\n    @((cs:=contextlib.nullcontext()) and identity)\n    class Inner:pass\n",
    ),
    ("eager argument annotation", "    def inner(arg:(cs:=contextlib.nullcontext())):pass\n"),
    ("eager return annotation", "    def inner()->(cs:=contextlib.nullcontext()):pass\n"),
)


@pytest.mark.parametrize(
    "label,rebind", DEFINITION_TIME_REBIND_ROWS, ids=[row[0] for row in DEFINITION_TIME_REBIND_ROWS]
)
def test_definition_metadata_rebinds_the_containing_carrier(label, rebind):
    """These Python3.12 definitions evaluate metadata in the enclosing scope."""
    source = (
        "import contextlib\ndef outer(x):\n    with(cs:=contextlib.suppress(AssertionError)):pass\n"
        + rebind
        + "    with cs:assert x!=1\n"
    )
    namespace = {}
    if label.startswith("eager") and sys.version_info >= (3, 14):
        # Python3.14 annotation scopes prohibit assignment expressions.
        with pytest.raises(SyntaxError):
            exec(source, namespace)  # noqa: S102
        return
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1)
    module = ast.parse(source)
    function = module.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module)


NESTED_DECLARATION_SCOPE_ROWS = (
    (
        "return before metadata",
        "    def inner():\n        nonlocal cs\n        return\n        def nested(arg=(cs:=contextlib.nullcontext())):pass\n    inner()\n",
        False,
    ),
    (
        "uncalled nonlocal",
        "    def inner():\n        nonlocal cs\n        cs=contextlib.nullcontext()\n",
        False,
    ),
    (
        "nearest nonlocal belongs to middle",
        "    def middle():\n        cs=contextlib.suppress(AssertionError)\n        def inner():\n            nonlocal cs\n            cs=contextlib.nullcontext()\n        inner()\n    middle()\n",
        False,
    ),
    ("lambda body local", "    inner=lambda:(cs:=contextlib.nullcontext())\n    inner()\n", False),
    (
        "metadata in called nonlocal function",
        "    def inner():\n        nonlocal cs\n        def nested(arg=(cs:=contextlib.nullcontext())):pass\n    inner()\n",
        True,
    ),
)
