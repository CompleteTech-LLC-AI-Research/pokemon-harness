"""Pin entry proofs to executed body reachability and surviving loop values."""

import ast
import sys

import pytest

from tests import _timed_menu_milestone_sentinel_support as support


def execute_with_trace(source):
    namespace = {}
    exec(source, namespace)  # noqa: S102 - independent runtime oracle
    function = namespace["outer"]
    module = ast.parse(source)
    definition = next(n for n in module.body if isinstance(n, ast.FunctionDef))
    assertion = next(n for n in ast.walk(definition) if isinstance(n, ast.Assert))
    observed = {"assertion_reached": False, "loop_survivors": []}

    def trace(frame, event, _argument):
        if frame.f_code is function.__code__ and event == "line":
            if frame.f_lineno == assertion.lineno:
                observed["assertion_reached"] = True
            if isinstance(definition.body[-1], ast.With) and (
                frame.f_lineno == definition.body[-1].lineno and "cs" in frame.f_locals
            ):
                observed["loop_survivors"].append(frame.f_locals["cs"])
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        function(1)
    except BaseException as error:  # noqa: BLE001 - retain every actual outcome
        observed["outcome"] = type(error).__name__
    else:
        observed["outcome"] = "RETURN"
    finally:
        sys.settrace(previous)
    return module, definition, assertion, observed


@pytest.mark.parametrize("last", ("None", "2"))
def test_after_loop_resolver_retains_the_executed_nonenterable_element(last, monkeypatch):
    source = f"def outer(x):\n for cs in (1, {last}):\n  pass\n with cs:\n  assert x != 1\n"
    module, definition, assertion, observed = execute_with_trace(source)
    assert observed["outcome"] == "TypeError"
    assert observed["assertion_reached"] is False
    assert len(observed["loop_survivors"]) == 1
    actual = observed["loop_survivors"][0]

    original = support._loop_element_for_read_after
    calls = []

    def recording(iterable):
        element = original(iterable)
        calls.append(element)
        return element

    monkeypatch.setattr(support, "_loop_element_for_read_after", recording)
    assert support._is_enforced(definition, assertion, module) is False
    assert calls, "the production resolution path must consult the after-loop helper"
    # Pin the resolver's documented return contract against the real surviving
    # runtime value. Refusing this literal must fail even if another independent
    # entry-kind proof still protects the overall assertion verdict.
    element = support._loop_element_for_read_after(definition.body[0].iter)
    assert isinstance(element, ast.Constant)
    assert type(element.value) is type(actual)
    assert element.value == actual


ENTRY_ROWS = (
    (
        "suppress-as-star",
        "with contextlib.suppress(AssertionError) as (*cs,):",
        "TypeError",
        True,
        False,
    ),
    ("undefined-star", "with (*cs,):", "NameError", True, False),
    ("null-as-star", "with contextlib.nullcontext() as (*cs,):", "TypeError", True, False),
    ("suppressed-unpack", "with contextlib.suppress(TypeError) as (*cs,):", "RETURN", True, False),
    (
        "iterable-as-star",
        "with contextlib.nullcontext((1, 2)) as (*cs,):",
        "AssertionError",
        False,
        True,
    ),
    ("plain-as", "with contextlib.nullcontext() as cs:", "AssertionError", False, True),
)


@pytest.mark.parametrize(("label", "header", "outcome", "blocked", "reached"), ENTRY_ROWS)
def test_real_entry_guard_call_matches_executed_body_reachability(
    label, header, outcome, blocked, reached, monkeypatch
):
    source = "def outer(x):\n import contextlib\n " + header + "\n  assert x != 1\n"
    module, definition, assertion, observed = execute_with_trace(source)
    assert observed["outcome"] == outcome, label
    assert observed["assertion_reached"] is reached, label
    original = support._known_with_target_cannot_unpack
    calls = []

    def recording(function, query, owning):
        result = original(function, query, owning)
        calls.append((query is assertion, result))
        return result

    monkeypatch.setattr(support, "_known_with_target_cannot_unpack", recording)
    assert support._is_enforced(definition, assertion, module) is reached, label
    assert calls == [(True, blocked)], label
