"""Executed exception outcomes for issue #500's empty suppressor."""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

ROWS = (
    ("list empty", "import contextlib\n", "    h=[contextlib.suppress()]\n", "h[0]", True),
    (
        "list catching",
        "import contextlib\n",
        "    h=[contextlib.suppress(AssertionError)]\n",
        "h[0]",
        False,
    ),
    (
        "list unrelated",
        "import contextlib\n",
        "    h=[contextlib.suppress(ValueError)]\n",
        "h[0]",
        True,
    ),
    ("tuple empty", "import contextlib\n", "    h=(contextlib.suppress(),)\n", "h[0]", True),
    ("dict empty", "import contextlib\n", "    h={'b':contextlib.suppress()}\n", "h['b']", True),
    ("class empty", "import contextlib\nclass H:\n    b=contextlib.suppress()\n", "", "H.b", True),
    ("qualified empty", "import contextlib\n", "", "contextlib.suppress()", True),
    ("qualified catching", "import contextlib\n", "", "contextlib.suppress(AssertionError)", False),
    ("qualified unrelated", "import contextlib\n", "", "contextlib.suppress(ValueError)", True),
    ("module alias empty", "import contextlib as cl\n", "", "cl.suppress()", True),
    ("member alias empty", "from contextlib import suppress as quiet\n", "", "quiet()", True),
    ("local import empty", "", "    import contextlib\n", "contextlib.suppress()", True),
    ("stored empty", "import contextlib\n", "    cs=contextlib.suppress()\n", "cs", True),
    ("walrus empty", "import contextlib\n", "", "(cs:=contextlib.suppress())", True),
    (
        "local catching alias",
        "from contextlib import suppress as quiet\n",
        "",
        "quiet(AssertionError)",
        False,
    ),
)


def verdict(source):
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    return support._is_enforced(function, target, tree)


@pytest.mark.parametrize("label,module,prefix,header,live", ROWS, ids=[row[0] for row in ROWS])
def test_empty_suppress_matches_executed_exception_outcome(label, module, prefix, header, live):
    source = module + "def outer(x):\n" + prefix + f"    with {header}:\n        assert x != 1\n"
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1)
    except AssertionError:
        runtime = True
    else:
        runtime = False
    assert runtime is live
    assert verdict(source) is live


@pytest.mark.parametrize(
    "prefix",
    [
        "    suppress=lambda: contextlib.suppress(AssertionError)\n",
        "    def suppress(): return contextlib.suppress(AssertionError)\n",
    ],
)
def test_empty_unknown_suppressor_does_not_gain_a_harmless_proof(prefix):
    source = (
        "import contextlib\nfrom contextlib import suppress\ndef outer(x):\n"
        + prefix
        + "    with suppress():\n        assert x != 1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    namespace["outer"](1)
    assert verdict(source) is False


def test_module_member_replacement_does_not_gain_an_empty_call_proof():
    source = """import contextlib
real=contextlib.suppress
contextlib.suppress=lambda: real(AssertionError)
def outer(x):
    with contextlib.suppress():
        assert x != 1
"""
    # Run the mutation in an isolated namespace and restore the actual module.
    import contextlib

    original = contextlib.suppress
    try:
        namespace = {}
        exec(source, namespace)  # noqa: S102
        namespace["outer"](1)
        assert verdict(source) is False
    finally:
        contextlib.suppress = original


def test_empty_suppress_mutation_is_killed(monkeypatch):
    source = (
        "import contextlib\ndef outer(x):\n    with contextlib.suppress():\n        assert x != 1\n"
    )
    assert verdict(source) is True
    monkeypatch.setattr(support, "_empty_suppress_call_is_genuine", lambda *_: False)
    assert verdict(source) is False


def test_caller_parameter_cannot_be_assumed_to_be_empty_contextlib_suppress():
    source = """from contextlib import suppress
def outer(x, suppress):
    with suppress():
        assert x != 1
"""
    import contextlib

    namespace = {}
    exec(source, namespace)  # noqa: S102
    namespace["outer"](1, lambda: contextlib.suppress(AssertionError))
    assert verdict(source) is False


def test_relative_import_does_not_earn_an_empty_suppress_proof():
    source = "from .contextlib import suppress\ndef outer(x):\n    with suppress():\n        assert x != 1\n"
    # Relative imports refer to an operator-chosen package, not the stdlib.
    assert verdict(source) is False


def test_unknown_arguments_keep_the_existing_conservative_verdict():
    source = "import contextlib\ndef outer(x, exceptions):\n    with contextlib.suppress(*exceptions):\n        assert x != 1\n"

    namespace = {}
    exec(source, namespace)  # noqa: S102
    namespace["outer"](1, (AssertionError,))
    with pytest.raises(AssertionError):
        namespace["outer"](1, ())
    assert verdict(source) is False


def test_imported_class_alias_protocol_write_invalidates_empty_proof():
    import contextlib

    source = """import contextlib
alias = contextlib.suppress
alias.__exit__ = lambda *args: True
def outer(x):
    with contextlib.suppress():
        assert x != 1
"""
    original = contextlib.suppress.__exit__
    try:
        namespace = {}
        exec(source, namespace)  # noqa: S102
        namespace["outer"](1)
        assert verdict(source) is False
    finally:
        contextlib.suppress.__exit__ = original


def test_protocol_globals_subscript_write_invalidates_empty_proof():
    import contextlib

    source = """import contextlib
namespace = contextlib.suppress.__exit__.__globals__
namespace['issubclass'] = lambda *args: True
def outer(x):
    with contextlib.suppress():
        assert x != 1
"""
    namespace = contextlib.suppress.__exit__.__globals__
    absent = object()
    original = namespace.get("issubclass", absent)
    try:
        scope = {}
        exec(source, scope)  # noqa: S102
        scope["outer"](1)
        assert verdict(source) is False
    finally:
        if original is absent:
            namespace.pop("issubclass", None)
        else:
            namespace["issubclass"] = original
