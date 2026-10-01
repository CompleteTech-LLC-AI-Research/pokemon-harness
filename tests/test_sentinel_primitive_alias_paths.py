"""Executed primitive failing paths for conditional alias stores (#366)."""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

ROWS = (
    (
        "filed",
        "first=contextlib.nullcontext()\nif flag:first=contextlib.suppress(AssertionError)",
        "first",
        "1==2",
        False,
        "AssertionError",
        True,
    ),
    (
        "filed suppressing invocation",
        "first=contextlib.nullcontext()\nif flag:first=contextlib.suppress(AssertionError)",
        "first",
        "1==2",
        True,
        "RETURN",
        True,
    ),
    (
        "one alias",
        "first=contextlib.nullcontext()\nif flag:first=contextlib.suppress(AssertionError)\nsecond=first",
        "second",
        "1==2",
        False,
        "AssertionError",
        True,
    ),
    (
        "two aliases",
        "first=contextlib.nullcontext()\nif flag:first=contextlib.suppress(AssertionError)\nsecond=first\nthird=second",
        "third",
        "1==2",
        False,
        "AssertionError",
        True,
    ),
    (
        "failure correlated with protected arm",
        "first=contextlib.nullcontext()\nif flag:first=contextlib.suppress(AssertionError)",
        "first",
        "flag!=True",
        True,
        "RETURN",
        False,
    ),
    (
        "snapshot protected",
        "first=contextlib.suppress(AssertionError)\nsecond=first\nfirst=contextlib.nullcontext()",
        "second",
        "1==2",
        False,
        "RETURN",
        False,
    ),
    (
        "snapshot live",
        "first=contextlib.nullcontext()\nsecond=first\nfirst=contextlib.suppress(AssertionError)",
        "second",
        "1==2",
        False,
        "AssertionError",
        True,
    ),
    (
        "else live",
        "first=contextlib.suppress(AssertionError)\nif flag:pass\nelse:first=contextlib.nullcontext()",
        "first",
        "1==2",
        False,
        "AssertionError",
        True,
    ),
    (
        "not condition",
        "first=contextlib.suppress(AssertionError)\nif not flag:first=contextlib.nullcontext()",
        "first",
        "1==2",
        False,
        "AssertionError",
        True,
    ),
    (
        "unrelated suppression",
        "first=contextlib.suppress(ValueError)\nif flag:first=contextlib.suppress(AssertionError)",
        "first",
        "1==2",
        False,
        "AssertionError",
        True,
    ),
    (
        "always protected",
        "first=contextlib.suppress(AssertionError)\nif flag:first=contextlib.suppress(AssertionError)",
        "first",
        "1==2",
        False,
        "RETURN",
        False,
    ),
    (
        "constant assertion passes",
        "first=contextlib.nullcontext()\nif flag:first=contextlib.suppress(AssertionError)",
        "first",
        "1==1",
        False,
        "RETURN",
        False,
    ),
    (
        "Exception protects all paths",
        "first=contextlib.suppress(Exception)\nif flag:first=contextlib.suppress(AssertionError)",
        "first",
        "1==2",
        False,
        "RETURN",
        False,
    ),
    (
        "BaseException protects all paths",
        "first=contextlib.suppress(BaseException)\nif flag:first=contextlib.suppress(AssertionError)",
        "first",
        "1==2",
        False,
        "RETURN",
        False,
    ),
)


@pytest.mark.parametrize(
    "label,prefix,manager,predicate,flag,outcome,proof", ROWS, ids=[r[0] for r in ROWS]
)
def test_primitive_alias_failure_path(label, prefix, manager, predicate, flag, outcome, proof):
    source = (
        "import contextlib\ndef probe(flag=False):\n"
        + "\n".join("    " + line for line in prefix.splitlines())
        + f"\n    with(cs:={manager}):assert {predicate}\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    try:
        namespace["probe"](flag)
    except AssertionError:
        actual = "AssertionError"
    else:
        actual = "RETURN"
    assert actual == outcome
    module = ast.parse(source)
    function = module.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    support._remember_module_for_function(function, module)
    assert bool(support._primitive_alias_failure_path(function, target, module)) is proof
    if proof:
        assert support._is_enforced(function, target, module)
    elif label in ("failure correlated with protected arm", "always protected"):
        assert not support._is_enforced(function, target, module)


@pytest.mark.parametrize(
    "prefix",
    (
        "callback()\nfirst=contextlib.nullcontext()\nif flag:first=contextlib.suppress(AssertionError)",
        "first=contextlib.nullcontext()\nif flag:first=contextlib.suppress(AssertionError)\ncallback()",
    ),
)
def test_unknown_callback_is_not_a_primitive_path_proof(prefix):
    source = (
        "import contextlib\ndef probe(flag=False,callback=None):\n"
        + "\n".join("    " + line for line in prefix.splitlines())
        + "\n    with(cs:=first):assert 1==2\n"
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"](False, lambda: None)
    module = ast.parse(source)
    function = module.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    support._remember_module_for_function(function, module)
    assert support._primitive_alias_failure_path(function, target, module) is None


def test_parameter_exception_binding_declines_primitive_path_proof():
    source = "import contextlib\ndef probe(flag=False,AssertionError=Exception):\n first=contextlib.nullcontext()\n if flag:first=contextlib.suppress(AssertionError)\n with(cs:=first):assert 1==2\n"
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["probe"]()
    module = ast.parse(source)
    function = module.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    support._remember_module_for_function(function, module)
    assert support._primitive_alias_failure_path(function, target, module) is None
