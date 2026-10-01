"""Executing controls for mutation and lexical lookup in issue #422."""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

ROWS = (
    (
        "same-name class replacement",
        "class Box:\n ctx=contextlib.suppress(AssertionError)\nBox.ctx=contextlib.suppress(AssertionError)\nclass Box:\n ctx=contextlib.nullcontext()\n",
        "",
        "Box.ctx",
        "AssertionError",
    ),
    (
        "type assignment then class replacement",
        "Box=type('First',(),{'ctx':contextlib.suppress(AssertionError)})\nclass Box:\n ctx=contextlib.nullcontext()\n",
        "",
        "Box.ctx",
        "AssertionError",
    ),
    (
        "annotated class overwrite",
        "class Box:\n ctx=contextlib.suppress(AssertionError)\n",
        " Box.ctx: object=contextlib.nullcontext()\n",
        "Box.ctx",
        "AssertionError",
    ),
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


@pytest.mark.parametrize(
    "construction",
    [
        "class Box:\n ctx=contextlib.suppress(AssertionError)\n",
        "Box=type('DifferentName',(),{'ctx':contextlib.suppress(AssertionError)})\n",
    ],
)
def test_module_final_attribute_store_runs_before_external_invocation(construction):
    source = (
        "import contextlib\n"
        + construction
        + "def outer(x):\n with Box.ctx: assert x!=1\nBox.ctx=contextlib.nullcontext()\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1)
    assert verdict(source) is True


def test_namespace_class_name_does_not_bind_an_unrelated_module_class():
    source = """import contextlib
class SameName:
 ctx=contextlib.nullcontext()
SameName.ctx=contextlib.nullcontext()
def outer(x):
 Box=type('SameName',(),{'ctx':contextlib.suppress(AssertionError)})
 with Box.ctx: assert x!=1
"""
    namespace = {}
    exec(source, namespace)  # noqa: S102
    namespace["outer"](1)
    assert verdict(source) is False


@pytest.mark.parametrize(
    "setup,header,outcome,live",
    [
        (" h=[contextlib.suppress(AssertionError)]\n", "h[-len(h)]", "swallowed", False),
        (" h=(contextlib.suppress(AssertionError),)\n", "h[-len(h)]", "swallowed", False),
        (" h=[contextlib.nullcontext()]\n", "h[-len(h)]", "AssertionError", True),
        (" h=[contextlib.suppress(ValueError)]\n", "h[-len(h)]", "AssertionError", True),
        (
            " h=[contextlib.suppress(AssertionError),contextlib.nullcontext()]\n len=lambda _:1\n",
            "h[-len(h)]",
            "AssertionError",
            True,
        ),
        (
            " h=[contextlib.suppress(AssertionError),contextlib.nullcontext()]\n other=[1]\n",
            "h[-len(other)]",
            "AssertionError",
            True,
        ),
        (
            " h=[contextlib.suppress(AssertionError)]\n h[0]=contextlib.nullcontext()\n",
            "h[-len(h)]",
            "AssertionError",
            True,
        ),
        (" h=[]\n", "h[-len(h)]", "IndexError", True),
        (
            " Box=type('H',(),dict(b=contextlib.suppress(AssertionError)))\n",
            "Box.b",
            "swallowed",
            False,
        ),
        (" Box=type('H',(),dict(b=contextlib.nullcontext()))\n", "Box.b", "AssertionError", True),
        (
            " Box=type('H',(),dict(b=contextlib.suppress(ValueError)))\n",
            "Box.b",
            "AssertionError",
            True,
        ),
        (
            " dict=lambda **kw:{'b':contextlib.nullcontext()}\n Box=type('H',(),dict(b=contextlib.suppress(AssertionError)))\n",
            "Box.b",
            "AssertionError",
            True,
        ),
        (
            " Box=type('H',(),dict(b=contextlib.suppress(AssertionError),other=missing()))\n",
            "Box.b",
            "NameError",
            True,
        ),
        (
            " Box=type('H',(),dict(b=contextlib.suppress(AssertionError)))\n Box.b=contextlib.nullcontext()\n",
            "Box.b",
            "AssertionError",
            True,
        ),
        (
            " Box=type('H',(),dict(b=contextlib.suppress(AssertionError),__slots__=42))\n",
            "Box.b",
            "TypeError",
            True,
        ),
        (
            " Box=type('H',(),dict(b=contextlib.suppress(AssertionError),__classcell__=None))\n",
            "Box.b",
            "TypeError",
            True,
        ),
    ],
)
def test_scoped_residual_proofs_match_execution(setup, header, outcome, live):
    source = "import contextlib\ndef outer(x):\n" + setup + f" with {header}: assert x!=1\n"
    namespace = {}
    exec(source, namespace)  # noqa: S102
    measured = "swallowed"
    try:
        namespace["outer"](1)
    except (AssertionError, NameError, IndexError, TypeError) as error:
        measured = type(error).__name__
    assert measured == outcome
    assert verdict(source) is live
