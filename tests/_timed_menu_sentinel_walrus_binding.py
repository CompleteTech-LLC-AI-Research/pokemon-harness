"""Walrus binding order and shadowing cases.

Actual test functions and literal cases from the original collector.
"""

import ast
import contextlib
import sys

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced

#: #464, the two shapes that must NOT be swept into the repair. Neither can be
#: judged by executing the fixture, which is exactly why they need pinning:
#:
#: * a *parameter* is chosen by the caller. ``def outer(x, helper)`` enters
#:   ``helper``; with ``nullcontext()`` the assert is live, with ``1`` the header
#:   raises ``TypeError``. The body determines neither, so the only honest
#:   verdict is the decline, and a "dead" answer would drop a real contract.
#: * a genuinely *unbound* name raises ``NameError`` on entry. Master already
#:   pins that header as live (``bare name in the header``), so flipping it
#:   would contradict a row that is already green.
#:
#: They are therefore asserted directly against `_is_enforced`, with the
#: executed outcome recorded in the message, because a widened rule that
#: resolved either name would make them dead -- the damaging direction for the
#: first and a regression for the second.
WALRUS_NAME_ENTRY_DECLINES = (
    ("a parameter is chosen by the caller", "helper", "live or TypeError, per call site"),
    ("a genuinely unbound name", "mystery", "NameError on entry"),
    # #464 residual. A *parameter* named for a builtin is the other way a
    # bare name stops being the builtin, and it is the one a body-only walk
    # cannot see -- the signature binds the name for the whole call:
    #
    #     def outer(x, int=None):
    #         m = int        # the caller's object, not the class object
    #
    # The caller decides, so the honest verdict is the decline. Reading it as
    # the builtin reported the header dead, which is a false-DEAD whenever the
    # caller passes a context manager -- the opposite error from the one this
    # repair exists to remove.
    (
        "a parameter named for a builtin",
        "m",
        "the caller's object; per call site",
    ),
)


@pytest.mark.parametrize(
    ("label", "value", "executed", "signature", "local_preamble"),
    [
        (label, value, executed, "x, flag, helper", "")
        for label, value, executed in WALRUS_NAME_ENTRY_DECLINES[:2]
    ]
    + [
        # A parameter named `int` binds the name for the whole call, so the
        # body's `m = int` reads the caller's object. Nothing in the body
        # rebinds it, which is exactly the case a body-only shadow walk misses.
        (
            WALRUS_NAME_ENTRY_DECLINES[2][0],
            "m",
            WALRUS_NAME_ENTRY_DECLINES[2][2],
            "x, flag, helper, int=None",
            "    m = int\n",
        ),
    ],
    ids=[row[0] for row in WALRUS_NAME_ENTRY_DECLINES],
)
def test_a_walrus_name_the_scope_cannot_resolve_is_left_live(
    label, value, executed, signature, local_preamble
):
    """#464: the conservative fallback must not be inverted.

    #308 criterion 1 makes a false-LIVE the damaging direction, but a repair
    that answered "dead" for every name it could not resolve would trade these
    two false-LIVEs for false-DEADs on real pinned contracts -- the opposite
    error, and the one #308 also names. A name the scope genuinely does not
    determine stays live.

    CPython cannot adjudicate either row (``executed`` records why: the caller
    picks the parameter's value, and an unbound name raises before the body),
    so the verdict is asserted directly rather than through the swept oracle.
    """
    source = (
        "import contextlib\n"
        f"def outer({signature}):\n"
        "    import contextlib\n"
        f"{local_preamble}"
        f"    with (cs := {value}):\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    outcome = "returned"
    try:
        # The fourth row's signature takes a fourth argument; the rest take
        # exactly `(x, flag, helper)`, so the call is sized from the signature.
        if signature.endswith("int=None"):
            namespace["outer"](0, True, None, contextlib.nullcontext())
        else:
            namespace["outer"](0, True, None)
    except AssertionError:
        outcome = "AssertionError"
    except BaseException as error:  # noqa: BLE001 - the outcome is the datum
        outcome = type(error).__name__
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, assertion, tree) is True, (
        f"{label}: the analyzer must decline this name and keep the assert live. "
        f"CPython gives `{outcome}` here ({executed}), so this row is pinned "
        f"against the analyzer directly rather than through the swept oracle."
    )


@pytest.mark.parametrize(
    "module_preamble,prefix,parameter,live",
    (
        ("h = 1\n", "", "h", True),
        ("h = 1\n", "    h = contextlib.nullcontext()\n", "", True),
        ("", "    a = contextlib.nullcontext()\n    h = a\n    a = 1\n", "", True),
        ("", "    a = 1\n    h = a\n    a = contextlib.nullcontext()\n", "", False),
        ("a = 1\nh = a\na = contextlib.nullcontext()\n", "", "", False),
        ("", "    def h(): pass\n    h = contextlib.nullcontext()\n", "", True),
        ("h = 1\n", "    h = 1\n    if flag: h = contextlib.nullcontext()\n", "", True),
    ),
)
def test_walrus_name_uses_lexical_store_and_alias_snapshot(
    module_preamble, prefix, parameter, live
):
    source = (
        "import contextlib\n"
        + module_preamble
        + "def outer(x, flag"
        + (", h" if parameter else "")
        + "):\n"
        + prefix
        + "    with (cs := h):\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<walrus-lexical-snapshot>", "exec"), namespace)  # noqa: S102
    import contextlib

    outcomes = []
    for flag in (False, True):
        try:
            namespace["outer"](1, flag, contextlib.nullcontext()) if parameter else namespace[
                "outer"
            ](1, flag)
        except AssertionError:
            outcomes.append(True)
        except TypeError:
            outcomes.append(False)
        else:
            outcomes.append(False)
    assert any(outcomes) is live
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is live


def test_walrus_name_declines_module_binding_shadowed_by_closure():
    source = (
        "import contextlib\nh = 1\ndef parent(h):\n"
        "    def outer(x):\n        with (cs := h):\n            assert x != 1\n"
        "    return outer\n"
    )
    namespace = {}
    exec(compile(source, "<walrus-closure-shadow>", "exec"), namespace)  # noqa: S102
    import contextlib

    with pytest.raises(AssertionError):
        namespace["parent"](contextlib.nullcontext())(1)
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True


def test_walrus_name_does_not_assume_decorated_definition_is_function():
    source = (
        "import contextlib\ndef decorate(function):\n    return contextlib.nullcontext()\n"
        "def outer(x):\n    @decorate\n    def h(): pass\n"
        "    with (cs := h):\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<decorated-walrus-carrier>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True


def test_walrus_name_does_not_assume_class_metaclass_is_unenterable():
    source = (
        "class Meta(type):\n    def __enter__(cls): return cls\n"
        "    def __exit__(cls, *args): return False\n"
        "def outer(x):\n    class h(metaclass=Meta): pass\n"
        "    with (cs := h):\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<metaclass-walrus-carrier>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True


def test_walrus_name_does_not_read_module_future_store_into_earlier_invocation():
    source = (
        "import contextlib\nhelper = contextlib.nullcontext()\n"
        "def outer(x):\n    with (cs := helper):\n        assert x != 1\n"
        "outer(1)\nhelper = 1\n"
    )
    namespace = {}
    with pytest.raises(AssertionError):
        exec(compile(source, "<early-module-walrus-invocation>", "exec"), namespace)  # noqa: S102
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True


def test_walrus_module_snapshot_declines_implicit_decorator_invocation():
    source = """import contextlib
helper = contextlib.nullcontext()
def outer(x):
    with (cs := helper):
        assert x != 1
def invoke(function):
    outer(1)
    return function
@invoke
def marker():
    pass
helper = 1
"""
    with pytest.raises(AssertionError):
        exec(source, {})  # noqa: S102
    module = ast.parse(source)
    function = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is True


def test_walrus_module_snapshot_declines_implicit_truthiness_invocation():
    source = """import contextlib
helper = contextlib.nullcontext()
def outer(x):
    with (cs := helper):
        assert x != 1
if trigger:
    pass
helper = 1
"""
    namespace = {}

    class Trigger:
        def __bool__(self):
            namespace["outer"](1)
            return True

    namespace["trigger"] = Trigger()
    with pytest.raises(AssertionError):
        exec(source, namespace)  # noqa: S102
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is True


def test_walrus_module_snapshot_declines_late_import_callback(monkeypatch):
    import sys
    from types import ModuleType

    source = """import contextlib
helper = contextlib.nullcontext()
def outer(x):
    with (cs := helper):
        assert x != 1
from sentinel_walrus_import_fixture import trigger
helper = 1
"""
    namespace = {}
    fixture = ModuleType("sentinel_walrus_import_fixture")

    def lookup(name):
        if name == "trigger":
            namespace["outer"](1)
        raise AttributeError(name)

    fixture.__getattr__ = lookup
    monkeypatch.setitem(sys.modules, fixture.__name__, fixture)
    with pytest.raises(AssertionError):
        exec(source, namespace)  # noqa: S102
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is True


@pytest.mark.parametrize("guard,literal", (("x is 1000", 1000), ("x is True", 1)))
def test_loop_else_identity_uses_runtime_object_not_ast_object(guard, literal):
    source = (
        "import contextlib\ndef outer(x):\n    cs = contextlib.nullcontext()\n"
        "    for item in (1,):\n        if " + guard + ": break\n"
        "    else: cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n        assert x != " + str(literal) + "\n"
    )
    namespace = {}
    exec(compile(source, "<runtime-identity-witness>", "exec"), namespace)  # noqa: S102
    function = namespace["outer"]
    value = (
        next(value for value in function.__code__.co_consts if type(value) is int and value == 1000)
        if literal == 1000
        else True
    )
    with pytest.raises(AssertionError):
        function(value)
    tree = ast.parse(source)
    outer = tree.body[1]
    target = next(node for node in ast.walk(outer) if isinstance(node, ast.Assert))
    assert _is_enforced(outer, target, tree) is True


@pytest.mark.parametrize(
    "definition,error",
    (
        ("    class Inner:\n        raise ValueError\n", ValueError),
        ("    def inner(value=missing()): pass\n", NameError),
        ("    @missing()\n    def inner(): pass\n", NameError),
        ("    class Inner(missing()): pass\n", NameError),
        ("    def inner(value: missing()): pass\n", NameError),
    ),
)
def test_loop_else_witness_declines_effectful_definition_creation(definition, error):
    source = (
        "import contextlib\ndef outer(x):\n"
        + definition
        + "    cs = contextlib.nullcontext()\n    for item in (1,):\n        break\n"
        + "    else: cs = contextlib.suppress(AssertionError)\n"
        + "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<effectful-definition>", "exec"), namespace)  # noqa: S102
    with pytest.raises(error):
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


def test_loop_else_witness_declines_definition_overwriting_failure_parameter():
    source = (
        "import contextlib\ndef outer(x):\n    def x(): pass\n"
        "    cs = contextlib.nullcontext()\n    for item in (1,):\n        break\n"
        "    else: cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<definition-overwrites-witness>", "exec"), namespace)  # noqa: S102
    namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


@pytest.mark.parametrize(("exit_value", "live"), [("False", True), ("True", False)])
def test_class_carrier_proves_metaclass_exit_before_admitting_body(exit_value, live):
    source = (
        "def outer(x):\n    class Meta(type):\n"
        "        def __enter__(cls): return cls\n"
        f"        def __exit__(cls, *exc): return {exit_value}\n"
        "    class CM(metaclass=Meta): pass\n    with CM:\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    else:
        assert namespace["outer"](1) is None
    module = ast.parse(source)
    function = module.body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is live


@pytest.mark.parametrize(
    ("prefix", "error"),
    [
        ("    missing_name\n", NameError),
        ("    if trigger:\n        pass\n", AssertionError),
        ("    trigger[0]\n", TypeError),
        ("    return\n", None),
    ],
)
def test_class_carrier_declines_opaque_setup_before_bare_header(prefix, error):
    source = (
        "def outer(x, trigger):\n    class Meta(type):\n"
        "        def __enter__(cls): return cls\n        def __exit__(cls, *exc): return False\n"
        "    class CM(metaclass=Meta): pass\n" + prefix + "    with CM:\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if error is None:
        assert namespace["outer"](1, object()) is None
    else:
        with pytest.raises(error):
            namespace["outer"](1, object())
    module = ast.parse(source)
    function = module.body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


@pytest.mark.parametrize(
    "declaration",
    [
        "    class CM(**{'metaclass': Meta}): pass\n",
        "    class Base(metaclass=Meta): pass\n    class CM(Base): pass\n",
    ],
)
def test_class_carrier_unresolved_shape_keeps_live_runtime_as_known_decline(declaration):
    source = (
        "def outer(x):\n    class Meta(type):\n"
        "        def __enter__(cls): return cls\n        def __exit__(cls, *exc): return False\n"
        + declaration
        + "    with CM:\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1)
    module = ast.parse(source)
    function = module.body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    # Runtime is live; inherited/unpacked metaclass resolution is not claimed.
    assert _is_enforced(function, target, module) is False


def test_class_carrier_declines_member_overwrite_before_entry():
    source = (
        "def outer(x):\n    class Meta(type):\n"
        "        def __enter__(cls): return cls\n        def __exit__(cls, *exc): return False\n"
        "    class CM(metaclass=Meta): pass\n"
        "    Meta.__exit__ = lambda cls, *exc: True\n"
        "    with CM:\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    assert namespace["outer"](1) is None
    module = ast.parse(source)
    function = module.body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


def test_class_carrier_type_spelling_does_not_resolve_caller_parameter():
    class Meta(type):
        def __enter__(cls):
            return cls

        def __exit__(cls, *exc):
            return False

    class Base(metaclass=Meta):
        pass

    source = "def outer(x, type):\n    class CM(type): pass\n    with CM:\n        assert x != 1\n"
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1, Base)
    module = ast.parse(source)
    function = module.body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    # Unknown caller metatypes are a retained conservative decline.
    assert _is_enforced(function, target, module) is False


def test_class_carrier_declines_decorator_replacing_class_value():
    source = """import contextlib
def outer(x):
    def decorate(cls):
        return contextlib.nullcontext()
    @decorate
    class CM:
        pass
    with CM:
        assert x != 1
"""
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1)
    module = ast.parse(source)
    function = module.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


@pytest.mark.parametrize(
    ("message", "error"), [("missing_name", NameError), ("1 / 0", ZeroDivisionError)]
)
def test_class_carrier_requires_inert_assertion_message(message, error):
    source = (
        "def outer(x):\n    class Meta(type):\n"
        "        def __enter__(cls): return cls\n        def __exit__(cls, *exc): return False\n"
        "    class CM(metaclass=Meta): pass\n    with CM:\n"
        f"        assert x != 1, {message}\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(error):
        namespace["outer"](1)
    module = ast.parse(source)
    function = module.body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


@pytest.mark.parametrize(("parameter", "error"), [("Meta", TypeError), ("T", AssertionError)])
def test_class_carrier_declines_generic_metaclass_lookup_scope(parameter, error):
    if sys.version_info < (3, 12):
        from tests._grammar_runtime import run_grammar_case

        run_grammar_case(parameter, error)
        return
    source = (
        "def outer(x):\n    class Meta(type):\n"
        "        def __enter__(cls): return cls\n        def __exit__(cls, *exc): return False\n"
        f"    class CM[{parameter}](metaclass=Meta): pass\n"
        "    with CM:\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(error):
        namespace["outer"](1)
    module = ast.parse(source)
    function = module.body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


@pytest.mark.parametrize("signature", ["*x", "**x"])
def test_class_carrier_variadic_collectors_are_not_primitive_failure_witnesses(signature):
    source = (
        f"def outer({signature}):\n    class Meta(type):\n"
        "        def __enter__(cls): return cls\n        def __exit__(cls, *exc): return False\n"
        "    class CM(metaclass=Meta): pass\n    with CM:\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if signature == "*x":
        assert namespace["outer"](1) is None
    else:
        assert namespace["outer"](x=1) is None
    module = ast.parse(source)
    function = module.body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


@pytest.mark.parametrize("parameter", ["CM", "Meta"])
def test_class_carrier_class_definitions_cannot_be_parameter_failure_witnesses(parameter):
    source = (
        f"def outer({parameter}):\n    class Meta(type):\n"
        "        def __enter__(cls): return cls\n        def __exit__(cls, *exc): return False\n"
        "    class CM(metaclass=Meta): pass\n    with CM:\n"
        f"        assert {parameter} != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    assert namespace["outer"](1) is None
    module = ast.parse(source)
    function = module.body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


@pytest.mark.parametrize(
    ("preamble", "error"),
    [
        (
            "    try:\n        import missing_sentinel_491_probe\n    except ImportError:\n        return\n",
            None,
        ),
        (
            "    try:\n        import missing_sentinel_491_probe\n    except ImportError:\n        raise ValueError\n",
            ValueError,
        ),
        (
            "    try:\n        import missing_sentinel_491_probe\n    except ValueError:\n        pass\n",
            ModuleNotFoundError,
        ),
        (
            "    try:\n        import missing_sentinel_491_probe\n    except 123:\n        pass\n",
            TypeError,
        ),
        ("    try:\n        pass\n    finally:\n        return\n", None),
        (
            "    try:\n        pass\n    except Exception:\n        pass\n    else:\n        return\n",
            None,
        ),
    ],
)
def test_try_witness_requires_every_reachable_arm_to_resume(preamble, error):
    source = (
        "import contextlib\ndef outer(x):\n"
        + preamble
        + (
            "    cs=contextlib.nullcontext()\n    for item in (1,):\n        break\n"
            "    else:\n        cs=contextlib.suppress(AssertionError)\n"
            "    with cs:\n        assert x != 1\n"
        )
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if error is None:
        assert namespace["outer"](1) is None
    else:
        with pytest.raises(error):
            namespace["outer"](1)
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


def test_try_witness_declines_opaque_helper_even_when_one_call_is_harmless():
    source = """import contextlib
def outer(x, helper):
    try:
        helper()
    except Exception:
        pass
    cs = contextlib.nullcontext()
    for item in (1,):
        break
    else:
        cs = contextlib.suppress(AssertionError)
    with cs:
        assert x != 1
"""
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1, lambda: None)
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    # Caller-specific callback effects cannot be inferred from this body.
    assert _is_enforced(function, target, module) is False


TRY_UNKNOWN_IMPORT_RUNTIME_ROWS = (
    (
        "485 a try/except above the loop is transparent",
        "    try:\n        import nope_missing_xyz\n    except ImportError:\n        pass\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    (
        "485 a try*/except* above the loop is transparent",
        "    try:\n        import nope_missing_xyz\n    except* ImportError:\n        pass\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
    (
        "485 a nested admitted try above the loop is transparent",
        "    try:\n        try:\n            import nope_missing_xyz\n        except ImportError:\n            pass\n    except Exception:\n        pass\n",
        "contextlib.nullcontext()",
        "contextlib.suppress(AssertionError)",
        True,
    ),
)
