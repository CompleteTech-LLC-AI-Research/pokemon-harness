"""Executed class and directly invoked function nonlocal capture controls."""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support


@pytest.mark.parametrize("scope", ["class", "called function", "uncalled function"])
@pytest.mark.parametrize(
    "initial,selected,live",
    [
        ("suppress(AssertionError)", "nullcontext()", True),
        ("suppress(AssertionError)", "suppress(AssertionError)", False),
        ("suppress(AssertionError)", "suppress(ValueError)", True),
        ("nullcontext()", "suppress(AssertionError)", False),
    ],
)
def test_known_nonlocal_capture_retains_actual_selected_value(scope, initial, selected, live):
    declaration = "class C:" if scope == "class" else "def inner():"
    invoke = "    inner()\n" if scope == "called function" else ""
    source = (
        "import contextlib\ndef outer(x):\n"
        f"    with (cs:=contextlib.{initial}): pass\n"
        f"    {declaration}\n        nonlocal cs\n"
        f"        match [contextlib.{selected}]:\n            case [cs]: pass\n"
        + invoke
        + "    with cs: assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    expected = live if scope != "uncalled function" else initial == "nullcontext()"
    if expected:
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    else:
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is expected


@pytest.mark.parametrize("subject", ["[]", "[contextlib.nullcontext(),contextlib.nullcontext()]"])
def test_unselected_nonlocal_sequence_preserves_carried_suppressor(subject):
    source = (
        "import contextlib\ndef outer(x):\n"
        " with (cs:=contextlib.suppress(AssertionError)): pass\n"
        " class C:\n  nonlocal cs\n"
        f"  match {subject}:\n   case [cs]: pass\n"
        " with cs: assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is False


def test_literal_projection_restores_source_tree():
    source = """import contextlib
 def_placeholder
""".replace(
        " def_placeholder",
        "def outer(x):\n with (cs:=contextlib.suppress(AssertionError)): pass\n class C:\n  nonlocal cs\n  match [contextlib.nullcontext()]:\n   case [cs]: pass\n with cs: assert x!=1",
    )
    tree = ast.parse(source)
    function = tree.body[-1]
    before = ast.dump(tree, include_attributes=True)
    support._nonlocal_match_captures(function)
    assert ast.dump(tree, include_attributes=True) == before


@pytest.mark.parametrize("message,error", [("missing", NameError), ("1/0", ZeroDivisionError)])
def test_capture_does_not_prove_a_raising_assert_message(message, error):
    source = (
        "import contextlib\ndef outer(x):\n"
        " with (cs:=contextlib.suppress(AssertionError)): pass\n"
        " class C:\n  nonlocal cs\n  match [contextlib.nullcontext()]:\n   case [cs]:pass\n"
        f" with cs:assert x!=1,{message}\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(error):
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is False


@pytest.mark.parametrize("signature", ["*x", "**x"])
def test_collectors_cannot_supply_the_primitive_failing_witness(signature):
    source = (
        f"import contextlib\ndef outer({signature}):\n"
        " with (cs:=contextlib.suppress(AssertionError)): pass\n"
        " class C:\n  nonlocal cs\n  match [contextlib.nullcontext()]:\n   case [cs]:pass\n"
        " with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if signature == "*x":
        namespace["outer"](1)
    else:
        namespace["outer"](x=1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is False


@pytest.mark.parametrize("scope", ["class", "called function"])
def test_definition_binding_cannot_be_removed_from_parameter_witness(scope):
    definition = "class x:" if scope == "class" else "def x():"
    invocation = " x()\n" if scope == "called function" else ""
    source = (
        "import contextlib\ndef outer(x):\n"
        " with (cs:=contextlib.suppress(AssertionError)): pass\n"
        f" {definition}\n  nonlocal cs\n  match [contextlib.nullcontext()]:\n   case [cs]:pass\n"
        + invocation
        + " with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is False


def test_definition_annotation_callback_cannot_establish_canonical_capture():
    import contextlib

    source = """import contextlib
 def outer(x: exec("contextlib.nullcontext=lambda:contextlib.suppress(AssertionError)")):
  with (cs:=contextlib.suppress(AssertionError)):pass
  class C:
   nonlocal cs
   match [contextlib.nullcontext()]:
    case [cs]:pass
  with cs:assert x!=1
""".replace("\n ", "\n")
    original = contextlib.nullcontext
    try:
        namespace = {}
        exec(source, namespace)  # noqa: S102
        namespace["outer"](1)
        tree = ast.parse(source)
        function = tree.body[-1]
        target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
        assert support._is_enforced(function, target, tree) is False
    finally:
        contextlib.nullcontext = original
