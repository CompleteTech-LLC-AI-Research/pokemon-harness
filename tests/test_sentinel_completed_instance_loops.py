"""Execute user instance protocols after completed literal loops (#417)."""

import ast
import contextlib
import sys

import pytest

from tests import _timed_menu_milestone_sentinel_support as support


def instance_loop_source(
    exit_value="True",
    container="tuple",
    *,
    body="pass",
    suffix="",
    last="Sup()",
    import_alias="contextlib",
):
    elements = f"{import_alias}.nullcontext(), {last}"
    iterable = f"({elements})" if container == "tuple" else f"[{elements}]"
    module_import = "import contextlib" + (
        f" as {import_alias}" if import_alias != "contextlib" else ""
    )
    return (
        module_import + "\ndef probe(x):\n"
        " class Sup:\n  def __enter__(self):return self\n"
        f"  def __exit__(self,*args):return {exit_value}\n"
        f" for cs in {iterable}:\n"
        + "".join("  " + line + "\n" for line in body.splitlines())
        + "".join(" " + line + "\n" for line in suffix.splitlines())
        + " with cs:\n  assert x!=1\n"
    )


def execute_and_classify(source):
    namespace = {}
    exec(source, namespace)  # noqa: S102 - complete independent CPython oracle
    try:
        namespace["probe"](1)
    except BaseException as error:  # noqa: BLE001 - retain actual outcomes
        outcome = type(error).__name__
    else:
        outcome = "RETURN"
    module = ast.parse(source)
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef))
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    return outcome, support._is_enforced(function, query, module)


@pytest.mark.parametrize("exit_value", ("True", "1", "'yes'"))
@pytest.mark.parametrize("container", ("tuple", "list"))
@pytest.mark.parametrize("import_alias", ("contextlib", "ctx"))
def test_completed_instance_loop_swallowing_exit_matches_execution(
    exit_value, container, import_alias
):
    source = instance_loop_source(exit_value, container, import_alias=import_alias)
    assert execute_and_classify(source) == ("RETURN", False)


@pytest.mark.parametrize("exit_value", ("False", "None", "0", "''"))
@pytest.mark.parametrize("container", ("tuple", "list"))
def test_completed_instance_loop_falsy_exit_remains_live(exit_value, container):
    assert execute_and_classify(instance_loop_source(exit_value, container)) == (
        "AssertionError",
        True,
    )


@pytest.mark.parametrize("container", ("tuple", "list"))
@pytest.mark.parametrize(
    "options",
    (
        {"last": "contextlib.nullcontext()"},
        {"suffix": "cs=contextlib.nullcontext()"},
        {"body": "cs=contextlib.nullcontext()"},
        {"body": "break"},
    ),
)
def test_completed_loop_stale_instance_is_not_used(container, options):
    assert execute_and_classify(instance_loop_source(container=container, **options)) == (
        "AssertionError",
        True,
    )


@pytest.mark.parametrize("container", ("tuple", "list"))
def test_in_body_multi_element_instance_question_is_unchanged(container):
    source = instance_loop_source(container=container)
    source = source.replace("  pass\n with cs:\n  assert x!=1\n", "  with cs:\n   assert x!=1\n")
    assert execute_and_classify(source) == ("AssertionError", True)


@pytest.mark.parametrize("edit", ("direct", "callback"))
def test_protocol_mutation_and_callback_are_declines_with_actual_live_execution(edit):
    source = instance_loop_source()
    replacement = " Sup.__exit__=lambda *args:False\n"
    if edit == "callback":
        replacement = " def change():Sup.__exit__=lambda *args:False\n change()\n"
    source = source.replace(" for cs in", replacement + " for cs in")
    assert execute_and_classify(source) == ("AssertionError", True)


def test_custom_constructor_cannot_supply_the_stable_instance_proof():
    source = instance_loop_source().replace(
        " class Sup:\n",
        " class Sup:\n  def __new__(cls):return contextlib.nullcontext()\n",
    )
    assert execute_and_classify(source) == ("AssertionError", True)


def test_custom_constructor_cannot_return_an_opaque_caller_instance():
    source = (
        instance_loop_source()
        .replace("def probe(x):", "def probe(x, instance):")
        .replace(" class Sup:\n", " class Sup:\n  def __new__(cls):return instance\n")
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102 - actual caller supplies a live manager
    with pytest.raises(AssertionError):
        namespace["probe"](1, contextlib.nullcontext())
    module = ast.parse(source)
    function = module.body[1]
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert support._is_enforced(function, query, module) is True


def test_earlier_with_item_can_change_the_instance_protocol():
    source = (
        instance_loop_source()
        .replace(
            " for cs in",
            " class Change:\n"
            "  def __enter__(self):Sup.__exit__=lambda *args:False\n"
            "  def __exit__(self,*args):return False\n"
            " for cs in",
        )
        .replace(" with cs:\n", " with Change(),cs:\n")
    )
    assert execute_and_classify(source) == ("AssertionError", True)


def test_enclosing_callee_parameter_cannot_supply_a_stable_constructor_proof():
    ordinary = instance_loop_source()
    source = (
        "import contextlib\ndef parent(contextlib):\n"
        + "".join(" " + line + "\n" for line in ordinary.splitlines()[1:])
        + " return probe\n"
    )

    class CallerRoot:
        def nullcontext(self):
            # This is ordinary caller code, outside the analyzed source. The
            # supplied constructor can mutate the class before entering it.
            local_class = sys._getframe(1).f_locals["Sup"]
            local_class.__exit__ = lambda *arguments: False
            return contextlib.nullcontext()

    namespace = {}
    exec(source, namespace)  # noqa: S102 - complete source with actual caller
    with pytest.raises(AssertionError):
        namespace["parent"](CallerRoot())(1)
    module = ast.parse(source)
    function = module.body[1].body[0]
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert support._is_enforced(function, query, module) is True


@pytest.mark.parametrize("mutation", ("global-root", "member", "opaque-passing-root"))
def test_source_global_writer_or_member_callback_cannot_supply_stable_import(mutation):
    ordinary = instance_loop_source()
    if mutation == "global-root":
        prefix = "def patch(value):\n global contextlib\n contextlib=value\n"
    elif mutation == "member":
        prefix = "def patch(value):\n contextlib.nullcontext=value\n"
    else:
        prefix = "def patch(callback):\n callback(contextlib)\n"
    source = ordinary.replace("def probe(x):\n", prefix + "def probe(x):\n")
    saved = contextlib.nullcontext

    def caller_constructor():
        local_class = sys._getframe(1).f_locals["Sup"]
        local_class.__exit__ = lambda *arguments: False
        return saved()

    class CallerRoot:
        nullcontext = staticmethod(caller_constructor)

    namespace = {}
    exec(source, namespace)  # noqa: S102 - source writer and actual caller
    try:
        if mutation == "global-root":
            namespace["patch"](CallerRoot())
        elif mutation == "member":
            namespace["patch"](caller_constructor)
        else:
            namespace["patch"](lambda root: setattr(root, "nullcontext", caller_constructor))
        with pytest.raises(AssertionError):
            namespace["probe"](1)
    finally:
        contextlib.nullcontext = saved
    module = ast.parse(source)
    function = module.body[-1]
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert support._is_enforced(function, query, module) is True
