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


@pytest.mark.parametrize(
    "selected,live",
    [
        ("nullcontext()", True),
        ("suppress(AssertionError)", False),
        ("suppress(ValueError)", True),
        ("suppress()", True),
    ],
)
def test_global_class_capture_reads_actual_manager(selected, live):
    source = (
        "import contextlib\ncs=contextlib.suppress(AssertionError)\n"
        "class C:\n global cs\n"
        f" match [contextlib.{selected}]:\n  case [cs]:pass\n"
        "def outer(x):\n with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    else:
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is live


@pytest.mark.parametrize("declaration", ["nonlocal", "global"])
@pytest.mark.parametrize(
    "returned,live",
    [("nullcontext()", True), ("suppress(AssertionError)", False), ("suppress(ValueError)", True)],
)
def test_source_defined_module_helper_retains_actual_manager(declaration, returned, live):
    helper = f"def helper():\n return contextlib.{returned}\n"
    if declaration == "global":
        source = (
            "import contextlib\n" + helper + "cs=contextlib.suppress(AssertionError)\n"
            "class C:\n global cs\n match [helper()]:\n  case [cs]:pass\n"
            "def outer(x):\n with cs:assert x!=1\n"
        )
    else:
        source = (
            "import contextlib\n" + helper + "def outer(x):\n"
            " with (cs:=contextlib.suppress(AssertionError)):pass\n"
            " class C:\n  nonlocal cs\n  match [helper()]:\n   case [cs]:pass\n"
            " with cs:assert x!=1\n"
        )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    else:
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is live


@pytest.mark.parametrize(
    "selected,live",
    [("nullcontext()", True), ("suppress(AssertionError)", False), ("suppress(ValueError)", True)],
)
def test_global_capture_inside_function_uses_selected_module_value(selected, live):
    source = (
        "import contextlib\ncs=contextlib.suppress(AssertionError)\ndef outer(x):\n global cs\n"
        " with (cs:=contextlib.suppress(AssertionError)):pass\n"
        " class C:\n  global cs\n"
        f"  match [contextlib.{selected}]:\n   case [cs]:pass\n"
        " with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    else:
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is live


def test_class_global_store_does_not_replace_enclosing_local_store():
    source = """import contextlib
cs=contextlib.suppress(AssertionError)
def outer(x):
 with (cs:=contextlib.suppress(AssertionError)):pass
 class C:
  global cs
  match [contextlib.nullcontext()]:
   case [cs]:pass
 with cs:assert x!=1
"""
    namespace = {}
    exec(source, namespace)  # noqa: S102
    namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is False


@pytest.mark.parametrize(
    "nested,live",
    [
        (
            " class C:\n  nonlocal cs\n  match [contextlib.nullcontext()]:\n   case [cs]:pass\n",
            True,
        ),
        (
            " class C:\n  nonlocal cs\n  for cs in (contextlib.suppress(AssertionError),):pass\n",
            False,
        ),
    ],
)
def test_original_issue383_function_import_cases(nested, live):
    source = (
        "def outer(x):\n import contextlib\n"
        " with (cs:=contextlib.suppress(AssertionError)):pass\n" + nested + " with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    else:
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is live


@pytest.mark.parametrize(
    "tail,live",
    [("cs=contextlib.nullcontext()\n", True), ("cs=contextlib.suppress(AssertionError)\n", False)],
)
def test_final_global_store_after_capture_is_the_value_entered(tail, live):
    source = (
        "import contextlib\ncs=contextlib.suppress(AssertionError)\n"
        "class C:\n global cs\n match [contextlib.suppress(AssertionError)]:\n  case [cs]:pass\n"
        + tail
        + "def outer(x):\n with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    else:
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is live


def test_source_helper_parameter_shadow_keeps_callback_opaque():
    source = """import contextlib
def helper():return contextlib.nullcontext()
def outer(x,helper):
 with (cs:=contextlib.suppress(AssertionError)):pass
 class C:
  nonlocal cs
  match [helper()]:
   case [cs]:pass
 with cs:assert x!=1
"""
    namespace = {}
    exec(source, namespace)  # noqa: S102
    namespace["outer"](1, lambda: __import__("contextlib").suppress(AssertionError))
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is False


@pytest.mark.parametrize(
    "before,after,live",
    [
        ("nullcontext()", "suppress(AssertionError)", True),
        ("suppress(AssertionError)", "nullcontext()", False),
    ],
)
def test_module_capture_uses_helper_definition_at_execution_position(before, after, live):
    source = (
        "import contextlib\n"
        f"def helper():return contextlib.{before}\n"
        "cs=contextlib.suppress(AssertionError)\nclass C:\n global cs\n match [helper()]:\n  case [cs]:pass\n"
        f"def helper():return contextlib.{after}\n"
        "def outer(x):\n with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    else:
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is live


def test_module_capture_does_not_override_an_enclosing_parameter():
    source = """import contextlib
cs=contextlib.suppress(AssertionError)
class C:
 global cs
 match [contextlib.suppress(AssertionError)]:
  case [cs]:pass
def parent(cs):
 def outer(x):
  with cs:assert x!=1
 return outer
"""
    namespace = {}
    exec(source, namespace)  # noqa: S102
    import contextlib

    with pytest.raises(AssertionError):
        namespace["parent"](contextlib.nullcontext())(1)
    tree = ast.parse(source)
    function = tree.body[-1].body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, target, tree) is True


def test_global_capture_does_not_replace_an_implicitly_read_closure():
    source = """import contextlib
cs=contextlib.suppress(AssertionError)
def parent(cs):
 def outer(x):
  class C:
   global cs
   match [contextlib.nullcontext()]:
    case [cs]:pass
  with cs:assert x!=1
 return outer
"""
    namespace = {}
    exec(source, namespace)  # noqa: S102
    import contextlib

    namespace["parent"](contextlib.suppress(AssertionError))(1)
    tree = ast.parse(source)
    function = tree.body[-1].body[0]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    # This implicit closure is not an explicit nonlocal declaration; decline
    # the new global capture proof rather than overwriting the lexical value.
    support._remember_module_for_function(function, tree)
    assert support._global_function_capture_effects(function, target) == []
