"""Execution-backed acceptance for issue #338's exception exit spellings."""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("statement", ("assert trigger", "trigger == 0", "trigger += 1"))
def test_new_exit_proof_declines_implicit_pre_assertion_callbacks(statement):
    source = (
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return exc[0] is not None\n"
        "manager = Manager()\n"
        "def probe(x, trigger):\n"
        f"    {statement}\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<implicit-prior-callback>", "exec"), namespace)  # noqa: S102

    class Trigger:
        def change(self):
            namespace["Manager"].__exit__ = lambda *args: False
            return True

        def __bool__(self):
            return self.change()

        def __eq__(self, other):
            return self.change()

        def __iadd__(self, other):
            self.change()
            return self

    with pytest.raises(AssertionError):
        namespace["probe"](1, Trigger())
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare)
    )
    assert support._is_enforced(function, assertion, tree) is True


def test_new_exit_proof_declines_an_external_context_entering_before_the_manager():
    source = (
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return exc[0] is not None\n"
        "manager = Manager()\n"
        "def probe(x, trigger):\n"
        "    with trigger: pass\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<prior-external-context>", "exec"), namespace)  # noqa: S102

    class Trigger:
        def __enter__(self):
            namespace["Manager"].__exit__ = lambda *args: False
            return self

        def __exit__(self, *args):
            return False

    with pytest.raises(AssertionError):
        namespace["probe"](1, Trigger())
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


def test_new_exit_proof_declines_a_caller_callback_named_like_the_owner():
    source = (
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return exc[0] is not None\n"
        "manager = Manager()\n"
        "def probe(x, Manager):\n"
        "    Manager()\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<caller-owner-mutation>", "exec"), namespace)  # noqa: S102

    def callback():
        namespace["Manager"].__exit__ = lambda *args: False

    with pytest.raises(AssertionError):
        namespace["probe"](1, callback)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


def test_new_exit_proof_declines_a_caller_callback_named_bool():
    source = (
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return exc[0] is not None\n"
        "manager = Manager()\n"
        "def probe(x, bool):\n"
        "    bool(x)\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<caller-bool-mutation>", "exec"), namespace)  # noqa: S102

    def callback(value):
        namespace["Manager"].__exit__ = lambda *args: False

    with pytest.raises(AssertionError):
        namespace["probe"](1, callback)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


@pytest.mark.parametrize("expression", ("trigger[0]", "trigger.value", "trigger == 0"))
def test_new_exit_proof_declines_implicit_module_evaluation_mutation(expression):
    source = (
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return exc[0] is not None\n"
        "manager = Manager()\n" + expression + "\n"
        "def probe(x):\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}

    class Trigger:
        def change(self):
            namespace["Manager"].__exit__ = lambda *args: False
            return 0

        def __getitem__(self, key):
            return self.change()

        @property
        def value(self):
            return self.change()

        def __eq__(self, other):
            return self.change()

    namespace["trigger"] = Trigger()
    exec(compile(source, "<implicit-module-protocol>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


@pytest.mark.parametrize("callback_in_enter", (False, True))
def test_new_exit_proof_declines_implicit_truthiness_protocol_mutation(callback_in_enter):
    enter = (
        "    def __enter__(self):\n        if trigger: pass\n        return self\n"
        if callback_in_enter
        else "    def __enter__(self): return self\n"
    )
    source = (
        "class Manager:\n" + enter + "    def __exit__(self, *exc): return exc[0] is not None\n"
        "manager = Manager()\n"
        + ("with manager: pass\n" if callback_in_enter else "if trigger: pass\n")
        + "def probe(x):\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}

    class Trigger:
        def __bool__(self):
            namespace["Manager"].__exit__ = lambda *args: False
            return True

    namespace["trigger"] = Trigger()
    exec(compile(source, "<implicit-exit-mutation>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


def test_new_exit_proof_declines_duplicate_exit_definitions():
    source = (
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return exc[0] is not None\n"
        "    def __exit__(self, *exc): return False\n"
        "manager = Manager()\n"
        "def probe(x):\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<duplicate-exit>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


def test_new_exit_proof_declines_member_defaults_that_overwrite_the_exit():
    source = (
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return exc[0] is not None\n"
        "    def other(self, x=(__exit__ := lambda *args: False)): pass\n"
        "manager = Manager()\n"
        "def probe(x):\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<exit-member-default>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


@pytest.mark.parametrize(
    "mutation",
    (
        "Manager.__exit__ = lambda *args: False",
        "Alias = Manager\nAlias.__exit__ = lambda *args: False",
        "setattr(Manager, '__exit__', lambda *args: False)",
        "replace = setattr\nreplace(Manager, '__exit__', lambda *args: False)",
    ),
)
def test_new_exit_proof_declines_protocol_mutation_before_construction(mutation):
    source = (
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return exc[0] is not None\n"
        + mutation
        + "\nmanager = Manager()\n"
        "def probe(x):\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<exit-protocol-mutation>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


@pytest.mark.parametrize("constructor", ("__init__", "__new__"))
def test_new_exit_proof_declines_constructors_that_replace_the_exit(constructor):
    body = (
        "    def __init__(self):\n        type(self).__exit__ = lambda *args: False\n"
        if constructor == "__init__"
        else "    def __new__(cls):\n"
        "        cls.__exit__ = lambda *args: False\n"
        "        return object.__new__(cls)\n"
    )
    source = (
        "class Manager:\n" + body + "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return exc[0] is not None\n"
        "manager = Manager()\n"
        "def probe(x):\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<exit-constructors>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


def test_new_exit_proof_declines_inherited_metaclass_rewriting_the_exit():
    source = (
        "class Meta(type):\n"
        "    def __new__(mcls, name, bases, namespace):\n"
        "        if name == 'Manager':\n"
        "            namespace['__exit__'] = lambda *args: False\n"
        "        return super().__new__(mcls, name, bases, namespace)\n"
        "class Base(metaclass=Meta): pass\n"
        "class Manager(Base):\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return exc[0] is not None\n"
        "def probe(x):\n"
        "    with Manager():\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<inherited-exit-metaclass>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


@pytest.mark.parametrize(
    "expression,live",
    [
        ("exc[0] is not None", False),
        ("bool(exc[0])", False),
        ("exc[0] is not False", False),
        ("exc[0] in (AssertionError,)", False),
        ("exc[0] in (AssertionError, ValueError)", False),
        ("exc[0] in (ValueError, AssertionError)", False),
        ("exc[0] is not 0", False),
        ("exc[0] is not ()", False),
        ("exc", False),
        ("bool(exc)", False),
        ("exc is not None", False),
        ("exc is not False", False),
        ("exc in (AssertionError,)", True),
        ("exc[0] not in (AssertionError,)", True),
        ("exc[0] is AssertionError", False),
        ("True", False),
        ("1", False),
        ("False", True),
        ("None", True),
        ("exc[0] is ValueError", True),
        ("exc[0] in (ValueError,)", True),
        ("exc[0] is None", True),
    ],
)
@pytest.mark.parametrize("local_instance", (False, True))
def test_exit_predicate_agrees_with_executed_assertion_failure(expression, live, local_instance):
    source = (
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        f"    def __exit__(self, *exc): return {expression}\n"
        + ("" if local_instance else "manager = Manager()\n")
        + "def probe(x):\n"
        + ("    manager = Manager()\n" if local_instance else "")
        + "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<exit-backlog>", "exec"), namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["probe"](1)
    else:
        assert namespace["probe"](1) is None
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is live


@pytest.mark.parametrize(
    "prefix,signature,body",
    [
        ("bool = lambda value: False\n", "self, *exc", "return bool(exc[0])"),
        ("bool = lambda value: False\n", "self, *exc", "return bool(exc)"),
        ("AssertionError = ValueError\n", "self, *exc", "return exc[0] in (AssertionError,)"),
        (
            "AssertionError = ValueError\n",
            "self, *exc",
            "return exc[0] in (AssertionError, ValueError)",
        ),
        ("", "self, *exc", "exc = (None,)\n        return exc[0] is not None"),
        (
            "",
            "self, *exc",
            "if False:\n            return exc[0] is not None\n        return False",
        ),
    ],
)
def test_new_exit_proof_declines_shadowing_and_non_unconditional_returns(prefix, signature, body):
    source = (
        prefix + "class Manager:\n"
        "    def __enter__(self): return self\n"
        f"    def __exit__({signature}):\n        {body}\n"
        "manager = Manager()\n"
        "def probe(x):\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<exit-controls>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True


@pytest.mark.parametrize(
    "expression",
    (
        "received_type is not None",
        "bool(received_type)",
        "received_type is not False",
        "received_type in (AssertionError,)",
    ),
)
def test_exit_predicate_reads_the_actual_named_type_parameter(expression):
    source = (
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        f"    def __exit__(self, received_type, value, tb): return {expression}\n"
        "def probe(x):\n"
        "    with Manager():\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<named-exit-type>", "exec"), namespace)  # noqa: S102
    assert namespace["probe"](1) is None
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is False


@pytest.mark.parametrize("decorate_class", (False, True))
def test_new_exit_proof_declines_decorators_that_replace_protocol_methods(decorate_class):
    source = (
        "def loud_exit(*args): return False\n"
        "def wrap_exit(original): return loud_exit\n"
        "def wrap_class(original):\n"
        "    original.__exit__ = loud_exit\n"
        "    return original\n" + ("@wrap_class\n" if decorate_class else "") + "class Manager:\n"
        "    def __enter__(self): return self\n"
        + ("" if decorate_class else "    @wrap_exit\n")
        + "    def __exit__(self, *exc): return bool(exc[0])\n"
        "manager = Manager()\n"
        "def probe(x):\n"
        "    with manager:\n"
        "        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<exit-decorators>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](1)
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert support._is_enforced(function, assertion, tree) is True
