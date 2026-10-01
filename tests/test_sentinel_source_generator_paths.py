"""Execute source-owned generator values without guessing opaque iterables."""

import ast
import asyncio
import sys

import pytest

from tests import _timed_menu_milestone_sentinel_support as support


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "payload, runtime, verdict",
    [
        ("contextlib.suppress(AssertionError)", "RETURN", False),
        ("contextlib.suppress(ValueError)", "AssertionError", True),
        ("contextlib.suppress()", "AssertionError", True),
        ("contextlib.nullcontext()", "AssertionError", True),
        ("1", "TypeError", True),
        ("None", "TypeError", True),
    ],
)
def test_source_generator_payload(asynchronous, payload, runtime, verdict):
    source = _source(asynchronous, payload)
    outcome, reached, actual, proof = _evaluate(source, asynchronous)
    assert outcome == runtime
    assert reached is (runtime != "TypeError")
    assert actual is verdict
    assert proof is (payload == "contextlib.suppress(AssertionError)")


def _source(asynchronous, payload):
    if asynchronous:
        return (
            "import contextlib\nasync def outer(x):\n"
            "    async def gen():\n        yield " + payload + "\n"
            "    async for cs in gen():\n        with cs:\n            assert x != 1\n"
        )
    return (
        "import contextlib\ndef outer(x):\n    for cs in gen():\n"
        "        with cs:\n            assert x != 1\n"
        "def gen():\n    yield " + payload + "\n"
    )


def _evaluate(source, asynchronous=False, namespace=None, extra=()):
    tree = ast.parse(source)
    function = next(
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "outer"
    )
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    namespace = {} if namespace is None else namespace
    filename = "<generator-control>"
    exec(compile(source, filename, "exec"), namespace)  # noqa: S102
    reached = False

    def trace(frame, event, arg):
        nonlocal reached
        if (
            event == "line"
            and frame.f_code.co_filename == filename
            and frame.f_lineno == query.lineno
        ):
            reached = True
        return trace

    before = sys.gettrace()
    sys.settrace(trace)
    try:
        try:
            result = namespace["outer"](1, *extra)
            if asynchronous:
                asyncio.run(result)
            runtime = "RETURN"
        except (AssertionError, TypeError, NameError, UnboundLocalError, ValueError) as error:
            runtime = type(error).__name__
    finally:
        sys.settrace(before)
    return (
        runtime,
        reached,
        support._is_enforced(function, query, tree),
        support._known_generator_swallows_query(function, query, tree),
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_multiple_source_yields_suppress(asynchronous):
    source = _source(asynchronous, "contextlib.suppress(AssertionError)")
    indent = "        " if asynchronous else "    "
    source = source.replace(
        indent + "yield contextlib.suppress(AssertionError)\n",
        (indent + "yield contextlib.suppress(AssertionError)\n") * 2,
    )
    assert _evaluate(source, asynchronous) == ("RETURN", True, False, True)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_mixed_generator_keeps_live_manager(asynchronous):
    source = _source(asynchronous, "contextlib.suppress(AssertionError)")
    indent = "        " if asynchronous else "    "
    source = source.replace(
        indent + "yield contextlib.suppress(AssertionError)\n",
        indent
        + "yield contextlib.suppress(AssertionError)\n"
        + indent
        + "yield contextlib.nullcontext()\n",
    )
    assert _evaluate(source, asynchronous) == ("AssertionError", True, True, False)


@pytest.mark.parametrize(
    "payload,runtime",
    [
        ("contextlib.nullcontext()", "AssertionError"),
        ("1", "TypeError"),
        ("contextlib.suppress(AssertionError)", "RETURN"),
    ],
)
def test_unknown_caller_generator_is_not_reclassified(payload, runtime):
    source = (
        "import contextlib\ndef outer(x, gen):\n    for cs in gen():\n"
        "        with cs:\n            assert x != 1\n"
    )
    namespace = {}
    exec("import contextlib\ndef supplied():\n    yield " + payload, namespace)  # noqa: S102
    outcome, reached, _, proof = _evaluate(source, extra=(namespace["supplied"],))
    assert outcome == runtime
    assert reached is (runtime != "TypeError")
    assert proof is False


@pytest.mark.parametrize(
    "edit,runtime",
    [
        ("gen = 1", "TypeError"),
        ("cs = contextlib.nullcontext()", "AssertionError"),
        ("del cs", "UnboundLocalError"),
    ],
)
def test_generator_or_target_rebinding_declines(edit, runtime):
    source = _source(False, "contextlib.suppress(AssertionError)")
    if edit.startswith("gen"):
        source = source.replace("    for cs", "    " + edit + "\n    for cs")
    else:
        source = source.replace("        with cs:", "        " + edit + "\n        with cs:")
    outcome, _, _, proof = _evaluate(source)
    assert outcome == runtime
    assert proof is False


def test_enclosing_contextlib_cell_is_not_canonical():
    source = (
        "import contextlib\ndef outer(x, contextlib):\n"
        "    async def gen():\n        yield contextlib.suppress(AssertionError)\n"
        "    async def consume():\n        async for cs in gen():\n"
        "            with cs:\n                assert x != 1\n"
        "    return consume()\n"
    )
    # The query belongs to a nested callable; this proof deliberately owns direct module functions.
    tree = ast.parse(source)
    outer = tree.body[1]
    query = next(n for n in ast.walk(outer) if isinstance(n, ast.Assert))
    assert support._known_generator_swallows_query(outer, query, tree) is False


@pytest.mark.parametrize(
    "statement",
    [
        "contextlib.suppress = contextlib.nullcontext",
        "contextlib.suppress += 1",
        "contextlib.__dict__['suppress'] = contextlib.nullcontext",
        "setattr(contextlib, 'suppress', contextlib.nullcontext)",
    ],
)
def test_member_mutation_source_budget(statement):
    tree = ast.parse(_source(False, "contextlib.suppress(AssertionError)") + statement + "\n")
    function = tree.body[1]
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert support._known_generator_swallows_query(function, query, tree) is False


@pytest.mark.parametrize("asynchronous", [False, True])
def test_conditional_generator_keeps_live_alternative(asynchronous):
    source = _source(asynchronous, "contextlib.suppress(AssertionError)")
    indent = "        " if asynchronous else "    "
    source = source.replace(
        indent + "yield contextlib.suppress(AssertionError)",
        indent + "if True:\n" + indent + "    yield contextlib.nullcontext()",
    )
    outcome, reached, _, proof = _evaluate(source, asynchronous)
    assert (outcome, reached, proof) == ("AssertionError", True, False)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_generator_callback_effect_is_not_a_suppressor_proof(asynchronous):
    source = _source(asynchronous, "contextlib.suppress(AssertionError)")
    indent = "        " if asynchronous else "    "
    source = source.replace(
        indent + "yield contextlib.suppress(AssertionError)", indent + "yield make()"
    )
    import contextlib

    outcome, reached, _, proof = _evaluate(source, asynchronous, {"make": contextlib.nullcontext})
    assert (outcome, reached, proof) == ("AssertionError", True, False)


def test_local_generator_inherits_real_caller_root():
    source = (
        "async def outer(x, contextlib):\n"
        "    async def gen():\n        yield contextlib.suppress(AssertionError)\n"
        "    async for cs in gen():\n        with cs:\n            assert x != 1\n"
    )
    import contextlib
    from types import SimpleNamespace

    root = SimpleNamespace(suppress=lambda *args: contextlib.nullcontext())
    outcome, reached, _, proof = _evaluate(source, True, extra=(root,))
    assert (outcome, reached, proof) == ("AssertionError", True, False)


def test_prior_callback_condition_cannot_supply_proof():
    source = _source(False, "contextlib.suppress(AssertionError)")
    source = source.replace("def outer(x):", "def outer(x, callback):")
    source = source.replace("    for cs", "    if callback:\n        pass\n    for cs")
    outcome, reached, _, proof = _evaluate(source, extra=(True,))
    assert (outcome, reached, proof) == ("RETURN", True, False)


def test_custom_iterable_protocol_remains_unreadable():
    source = (
        "import contextlib\ndef outer(x):\n    for cs in gen():\n"
        "        with cs:\n            assert x != 1\n"
        "class gen:\n    def __iter__(self):\n        yield contextlib.nullcontext()\n"
    )
    assert _evaluate(source) == ("AssertionError", True, True, False)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_return_before_yield_has_no_live_witness(asynchronous):
    source = _source(asynchronous, "contextlib.suppress()")
    indent = "        " if asynchronous else "    "
    source = source.replace(
        indent + "yield contextlib.suppress()",
        indent + "return\n" + indent + "yield contextlib.suppress()",
    )
    outcome, reached, actual, proof = _evaluate(source, asynchronous)
    assert (outcome, reached, actual, proof) == ("RETURN", False, False, True)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_final_return_after_yield_preserves_live_witness(asynchronous):
    source = _source(asynchronous, "contextlib.suppress()")
    indent = "        " if asynchronous else "    "
    source = source.replace(
        indent + "yield contextlib.suppress()\n",
        indent + "yield contextlib.suppress()\n" + indent + "return\n",
    )
    outcome, reached, actual, proof = _evaluate(source, asynchronous)
    assert (outcome, reached, actual, proof) == ("AssertionError", True, True, False)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_return_cuts_off_unreachable_later_manager(asynchronous):
    source = _source(asynchronous, "contextlib.suppress(AssertionError)")
    indent = "        " if asynchronous else "    "
    source = source.replace(
        indent + "yield contextlib.suppress(AssertionError)\n",
        indent
        + "yield contextlib.suppress(AssertionError)\n"
        + indent
        + "return\n"
        + indent
        + "yield contextlib.nullcontext()\n",
    )
    assert _evaluate(source, asynchronous) == ("RETURN", True, False, True)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("before", [False, True])
def test_same_line_return_and_yield_follow_statement_order(asynchronous, before):
    source = _source(asynchronous, "contextlib.suppress()")
    replacement = (
        "return; yield contextlib.suppress()" if before else "yield contextlib.suppress(); return"
    )
    source = source.replace("yield contextlib.suppress()", replacement)
    outcome, reached, actual, proof = _evaluate(source, asynchronous)
    assert outcome == ("RETURN" if before else "AssertionError")
    assert reached is not before
    assert actual is not before
    assert proof is before


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unknown_assertion_message_does_not_get_new_positive_proof(asynchronous):
    source = _source(asynchronous, "contextlib.suppress()")
    source = source.replace("assert x != 1", "assert x != 1, missing")
    outcome, reached, _, proof = _evaluate(source, asynchronous)
    assert (outcome, reached, proof) == ("NameError", True, False)
