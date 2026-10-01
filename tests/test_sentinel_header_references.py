"""Executing controls for mutation and lexical lookup in issue #422."""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

ROWS = (
    (
        "element overwrite",
        "",
        " h=[contextlib.suppress(AssertionError)]\n h[0]=contextlib.nullcontext()\n",
        "h[0]",
        "AssertionError",
    ),
    (
        "alias element overwrite",
        "",
        " h=[contextlib.suppress(AssertionError)]\n alias=h\n alias[0]=contextlib.nullcontext()\n",
        "h[0]",
        "AssertionError",
    ),
    (
        "duplicate key final value",
        "",
        " h={'k':contextlib.suppress(AssertionError),'k':contextlib.nullcontext()}\n",
        "h['k']",
        "AssertionError",
    ),
    (
        "class final function store",
        "class Box:\n ctx=contextlib.suppress(AssertionError)\n",
        " Box.ctx=contextlib.suppress(AssertionError)\n Box.ctx=contextlib.nullcontext()\n",
        "Box.ctx",
        "AssertionError",
    ),
    (
        "class final body store",
        "class Box:\n ctx=contextlib.suppress(AssertionError)\n ctx=contextlib.nullcontext()\n",
        "",
        "Box.ctx",
        "AssertionError",
    ),
    (
        "class final module store",
        "class Box:\n pass\nBox.ctx=contextlib.suppress(AssertionError)\nBox.ctx=contextlib.nullcontext()\n",
        "",
        "Box.ctx",
        "AssertionError",
    ),
    (
        "unselected missing call",
        "",
        " h=[contextlib.suppress(AssertionError),missing()]\n",
        "h[0]",
        "NameError",
    ),
    (
        "unselected missing name",
        "",
        " h=[contextlib.suppress(AssertionError),missing]\n",
        "h[0]",
        "NameError",
    ),
    (
        "unselected raising expression",
        "",
        " h=[contextlib.suppress(AssertionError),1/0]\n",
        "h[0]",
        "ZeroDivisionError",
    ),
    (
        "decorator replaces attribute",
        "def decorate(cls):\n class Other:\n  ctx=contextlib.nullcontext()\n return Other\n@decorate\nclass Box:\n ctx=contextlib.suppress(AssertionError)\n",
        "",
        "Box.ctx",
        "AssertionError",
    ),
    (
        "metaclass intercepts attribute",
        "class Meta(type):\n def __getattribute__(cls,name): return contextlib.nullcontext()\nclass Box(metaclass=Meta):\n ctx=contextlib.suppress(AssertionError)\n",
        "",
        "Box.ctx",
        "AssertionError",
    ),
    (
        "shadowed type constructor",
        "def type(*args):\n class Other:\n  ctx=contextlib.nullcontext()\n return Other\n",
        " Box=type('Box',(),{'ctx':contextlib.suppress(AssertionError)})\n",
        "Box.ctx",
        "AssertionError",
    ),
)


def verdict(source):
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    return support._is_enforced(function, target, tree)


@pytest.mark.parametrize("label,module,prefix,header,outcome", ROWS, ids=[row[0] for row in ROWS])
def test_reference_requires_an_unmutated_readable_runtime_value(
    label, module, prefix, header, outcome
):
    source = (
        "import contextlib\n"
        + module
        + "def outer(x):\n"
        + prefix
        + f" with {header}: assert x!=1\n"
    )
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    with pytest.raises(Exception) as caught:
        namespace["outer"](1)
    assert type(caught.value).__name__ == outcome
    assert verdict(source) is True


def test_parameter_container_blocks_module_fallback():
    import contextlib

    source = "import contextlib\nh=[contextlib.suppress(AssertionError)]\ndef outer(x,h):\n with h[0]: assert x!=1\n"
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1, [contextlib.nullcontext()])
    assert verdict(source) is True


def test_future_local_binding_blocks_module_fallback():
    source = "import contextlib\nh=[contextlib.suppress(AssertionError)]\ndef outer(x):\n with h[0]: assert x!=1\n h=[contextlib.suppress(AssertionError)]\n"
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(UnboundLocalError):
        namespace["outer"](1)
    assert verdict(source) is True


def test_early_invocation_cannot_read_future_module_container():
    source = "import contextlib\nh=[contextlib.nullcontext()]\ndef outer(x):\n with h[0]: assert x!=1\nouter(1)\nh=[contextlib.suppress(AssertionError)]\n"
    with pytest.raises(AssertionError):
        exec(source, {})  # noqa: S102
    assert verdict(source) is True
