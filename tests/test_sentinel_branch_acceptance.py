"""Execute branch acceptance with the full caller domain made explicit."""

import ast
import sys

import pytest

from tests import _timed_menu_milestone_sentinel_support as support


class FalseyEqualOne:
    """A legal caller value missed by the historical 0/1-only sweep."""

    def __bool__(self):
        return False

    def __eq__(self, other):
        return other == 1

    def __ne__(self, other):
        return not self == other


CHAINS = [
    ("466 false", "if False:\n    pass\nelse:\n    cs=contextlib.suppress(AssertionError)", False),
    ("466 true", "if True:\n    pass\nelse:\n    cs=contextlib.suppress(AssertionError)", True),
    (
        "466 free",
        "if False:\n    pass\nelif flag:\n    pass\nelse:\n    cs=contextlib.suppress(AssertionError)",
        True,
    ),
    (
        "466 negated",
        "if not flag:\n    pass\nelif True:\n    pass\nelse:\n    cs=contextlib.suppress(AssertionError)",
        True,
    ),
    # Exhaustive PASS arms never select the suppressor; they keep the live manager.
    (
        "466 opposite",
        "if flag:\n    pass\nelif not flag:\n    pass\nelse:\n    cs=contextlib.suppress(AssertionError)",
        True,
    ),
    (
        "466 duplicate",
        "if flag:\n    pass\nelif flag:\n    pass\nelse:\n    cs=contextlib.suppress(AssertionError)",
        True,
    ),
    (
        "466 constant",
        "if False:\n    pass\nelif True:\n    pass\nelse:\n    cs=contextlib.suppress(AssertionError)",
        True,
    ),
    ("434 E", "if False:\n    pass\nelif True:\n    cs=list()", False),
    ("434 K", "if False:\n    pass\nelif False:\n    pass\nelif True:\n    cs=list()", False),
    ("434 M", "if False:\n    pass\nelse:\n    cs=list()", False),
    ("434 C1", "if not x:\n    pass\nelse:\n    cs=list()", True),
    ("434 C2", "if not x:\n    pass\nelif True:\n    cs=list()", True),
    ("434 positive control", "if x:\n    pass\nelse:\n    cs=list()", True),
]


@pytest.mark.parametrize("label,chain,expected", CHAINS, ids=[r[0] for r in CHAINS])
def test_branch_chain_contract(label, chain, expected):
    source = (
        "import contextlib\ndef outer(x, flag):\n    cs=contextlib.nullcontext()\n"
        + "".join("    " + line + "\n" for line in chain.splitlines())
        + "    with cs:\n        assert x != 1\n"
    )
    tree, function, query, namespace = _compile(source)
    fired = False
    observations = []
    for value in (0, 1, FalseyEqualOne()):
        for flag in (False, True):
            outcome, reached = _execute(namespace, query, value, flag)
            fired |= outcome == "AssertionError"
            observations.append((type(value).__name__, flag, outcome, reached))
    assert fired is expected, (label, observations)
    assert bool(support._is_enforced(function, query, tree)) is expected


MODULE_STORES = [
    ("plain true", "if True:\n    cs=list()", False),
    ("nested true", "for _ in 'a':\n    if True:\n        cs=list()", False),
    ("elif true", "if False:\n    pass\nelif True:\n    cs=list()", False),
    ("else true", "if False:\n    pass\nelse:\n    cs=list()", False),
    ("never true", "if False:\n    cs=list()", True),
    ("empty outer", "for _ in ():\n    if True:\n        cs=list()", True),
    ("real conditional", "if flag:\n    cs=list()", True),
    # #403 explicitly excludes a store directly in a string-loop body.
    ("direct loop unchanged", "for _ in 'a':\n    cs=list()", True),
]


@pytest.mark.parametrize("label,block,expected", MODULE_STORES, ids=[r[0] for r in MODULE_STORES])
def test_module_store_scope(label, block, expected):
    source = (
        "import contextlib\ncs=contextlib.nullcontext()\n"
        + block
        + "\ndef outer(x, flag):\n    with cs:\n        assert x != 1\n"
    )
    results = []
    for flag in (False, True):
        tree, function, query, namespace = _compile(source, {"flag": flag})
        results.append(_execute(namespace, query, 1, flag))
        assert bool(support._is_enforced(function, query, tree)) is expected
    if label == "direct loop unchanged":
        # This known residual is not turned into a new false expectation.
        assert results == [("TypeError", False), ("TypeError", False)]
    else:
        assert any(outcome == "AssertionError" for outcome, _ in results) is expected


def _compile(source, namespace=None):
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "outer")
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    namespace = {} if namespace is None else namespace
    exec(compile(source, "<branch-contract>", "exec"), namespace)  # noqa: S102
    return tree, function, query, namespace


def _execute(namespace, query, value, flag):
    reached = False

    def trace(frame, event, arg):
        nonlocal reached
        if (
            event == "line"
            and frame.f_code.co_filename == "<branch-contract>"
            and frame.f_lineno == query.lineno
        ):
            reached = True
        return trace

    prior = sys.gettrace()
    sys.settrace(trace)
    try:
        try:
            namespace["outer"](value, flag)
            outcome = "RETURN"
        except (AssertionError, TypeError) as error:
            outcome = type(error).__name__
    finally:
        sys.settrace(prior)
    return outcome, reached


@pytest.mark.parametrize("spelling", ["qualified", "from import"])
def test_minimal_403_import_spellings(spelling):
    prefix = (
        "import contextlib\ncs=contextlib.nullcontext()"
        if spelling == "qualified"
        else "from contextlib import nullcontext\ncs=nullcontext()"
    )
    source = (
        prefix
        + "\nif True:\n    cs=list()\ndef outer(x,flag):\n    with cs:\n        assert x != 1\n"
    )
    tree, function, query, namespace = _compile(source)
    assert _execute(namespace, query, 1, False) == ("TypeError", False)
    assert support._is_enforced(function, query, tree) is False


def test_named_constructor_shadow_retains_actual_live_context():
    source = (
        "import contextlib\ndef list():\n    return contextlib.nullcontext()\n"
        "cs=contextlib.nullcontext()\nif True:\n    cs=list()\n"
        "def outer(x,flag):\n    with cs:\n        assert x != 1\n"
    )
    tree, function, query, namespace = _compile(source)
    assert _execute(namespace, query, 1, False) == ("AssertionError", True)
    assert support._is_enforced(function, query, tree) is True


def test_original_466_unused_helper_parameter_preserves_live_path():
    chain = CHAINS[2][1]
    source = (
        "import contextlib\ndef outer(x,flag,helper=None):\n    cs=contextlib.nullcontext()\n"
        + "".join("    " + line + "\n" for line in chain.splitlines())
        + "    with cs:\n        assert x != 1\n"
    )
    tree, function, query, namespace = _compile(source)
    assert _execute(namespace, query, 1, True) == ("AssertionError", True)
    assert support._is_enforced(function, query, tree) is True
