"""Terminal binding metadata and edge-case cases.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_class_try_witnesses import (
    NESTED_DECLARATION_SCOPE_ROWS,
)


@pytest.mark.parametrize(
    "label,rebind,fires",
    NESTED_DECLARATION_SCOPE_ROWS,
    ids=[row[0] for row in NESTED_DECLARATION_SCOPE_ROWS],
)
def test_nested_store_reaches_only_its_actual_binding(label, rebind, fires):
    source = (
        "import contextlib\ndef outer(x):\n    with(cs:=contextlib.suppress(AssertionError)):pass\n"
        + rebind
        + "    with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    try:
        namespace["outer"](1)
    except AssertionError:
        actual = True
    else:
        actual = False
    assert actual is fires
    module = ast.parse(source)
    function = module.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is fires


@pytest.mark.parametrize("future", (False, True), ids=("eager", "postponed"))
def test_annotation_reads_do_not_retire_a_carrier(future):
    source = (
        ("from __future__ import annotations\n" if future else "")
        + "import contextlib\ndef outer(x):\n    with(cs:=contextlib.suppress(AssertionError)):pass\n    def inner(arg:cs)->cs:pass\n    with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    namespace["outer"](1)
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


def test_a_nested_global_rebind_reaches_a_global_carrier():
    source = "import contextlib\ndef outer(x):\n    global cs\n    with(cs:=contextlib.suppress(AssertionError)):pass\n    def inner():\n        global cs\n        cs=contextlib.nullcontext()\n    inner()\n    with cs:assert x!=1\n"
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1)
    module = ast.parse(source)
    function = module.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module)


@pytest.mark.parametrize(
    "definition,error",
    (
        ("    def inner(arg=((cs:=contextlib.nullcontext()),missing())[0]):pass\n", NameError),
        ("    def inner(arg=(cs:=contextlib.nullcontext()),other=1/0):pass\n", ZeroDivisionError),
        ("    @missing\n    def inner(arg=(cs:=contextlib.nullcontext())):pass\n", NameError),
        (
            "    class Inner((cs:=contextlib.nullcontext()).__class__):\n        raise RuntimeError('blocked')\n",
            RuntimeError,
        ),
    ),
)
def test_failing_definition_metadata_does_not_create_a_live_assertion(definition, error):
    source = (
        "import contextlib\ndef outer(x):\n    with(cs:=contextlib.suppress(AssertionError)):pass\n"
        + definition
        + "    with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(error):
        namespace["outer"](1)
    module = ast.parse(source)
    function = module.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


@pytest.mark.parametrize(
    "setup",
    (
        "    callback()\n",
        "    def prior(arg=missing()):pass\n",
    ),
)
def test_opaque_setup_before_metadata_keeps_the_known_decline(setup):
    source = (
        "import contextlib\ndef outer(x,callback):\n    with(cs:=contextlib.suppress(AssertionError)):pass\n"
        + setup
        + "    def inner(arg=(cs:=contextlib.nullcontext())):pass\n    with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102

    def callback():
        raise NameError("opaque setup")

    with pytest.raises(NameError):
        namespace["outer"](1, callback)
    module = ast.parse(source)
    function = module.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


@pytest.mark.parametrize(
    "creation",
    (
        "def corrupt(arg=(contextlib:=type('Fake',(),{'nullcontext':staticmethod(lambda:real.suppress(AssertionError)),'suppress':staticmethod(real.suppress)}))):pass\n",
        "def corrupt(*,arg=(contextlib:=type('Fake',(),{'nullcontext':staticmethod(lambda:real.suppress(AssertionError)),'suppress':staticmethod(real.suppress)}))):pass\n",
    ),
)
def test_module_definition_metadata_cannot_install_a_fake_nullcontext(creation):
    source = (
        "import contextlib\nimport contextlib as real\n"
        + creation
        + "def outer(x):\n    with(cs:=contextlib.suppress(AssertionError)):pass\n    def inner(arg=(cs:=contextlib.nullcontext())):pass\n    with cs:assert x!=1\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    namespace["outer"](1)
    module = ast.parse(source)
    function = module.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, module) is False


@pytest.mark.parametrize("container", ("tuple", "list"))
@pytest.mark.parametrize("exit_value", ("True", "False"))
def test_completed_user_instance_loop_last_element_has_executed_protocol(container, exit_value):
    """#417: removing after-loop instance resolution must kill these rows."""
    from tests.test_sentinel_completed_instance_loops import (
        execute_and_classify,
        instance_loop_source,
    )

    outcome, enforced = execute_and_classify(instance_loop_source(exit_value, container))
    assert outcome == ("RETURN" if exit_value == "True" else "AssertionError")
    assert enforced is (exit_value == "False")
