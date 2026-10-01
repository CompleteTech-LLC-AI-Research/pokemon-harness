"""Execute module binding order controls for obsolete guard cleanup (#520)."""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

CONTROLS = [
    (
        "builtins_list_before_other_if_binding",
        "import builtins\nif True:\n other=0\ncs=builtins.list()\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "TypeError",
    ),
    (
        "builtins_list_inside_if",
        "import builtins\nif True:\n other=0\n cs=builtins.list()\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "TypeError",
    ),
    (
        "builtins_capture_before_later_shadow",
        "import builtins\ncs=builtins.list()\nclass Shadow:\n def list(self):\n  import contextlib\n  return contextlib.nullcontext()\nbuiltins=Shadow()\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "TypeError",
    ),
    (
        "builtins_shadow_before_capture",
        "import builtins\nclass Shadow:\n def list(self):\n  import contextlib\n  return contextlib.nullcontext()\nbuiltins=Shadow()\ncs=builtins.list()\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "AssertionError",
    ),
    (
        "contextlib_null_before_other_if_binding",
        "import contextlib\nif True:\n other=0\ncs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "AssertionError",
    ),
    (
        "contextlib_null_inside_if",
        "import contextlib\nif True:\n other=0\n cs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "AssertionError",
    ),
    (
        "from_null_before_other_if_binding",
        "from contextlib import nullcontext\nif True:\n other=0\ncs=nullcontext()\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "AssertionError",
    ),
    (
        "from_null_inside_if",
        "from contextlib import nullcontext\nif True:\n other=0\n cs=nullcontext()\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "AssertionError",
    ),
    (
        "from_null_after_nested_unrelated_def",
        "from contextlib import nullcontext\nif True:\n def unrelated():\n  nullcontext=None\ncs=nullcontext()\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "AssertionError",
    ),
    (
        "from_null_capture_before_later_shadow",
        "from contextlib import nullcontext\ncs=nullcontext()\nnullcontext=None\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "AssertionError",
    ),
    (
        "module_false_block_preserves_constructor",
        "from contextlib import nullcontext\nif False:\n nullcontext=None\ncs=nullcontext()\ndef outer(x):\n with cs:\n  assert x != 1\n",
        "AssertionError",
    ),
]


@pytest.mark.parametrize("name,source,expected_outcome", CONTROLS, ids=[row[0] for row in CONTROLS])
def test_module_binding_order_matches_execution(name, source, expected_outcome):
    namespace = {}
    exec(source, namespace)  # noqa: S102 - independent complete CPython oracle
    try:
        namespace["outer"](1)
    except BaseException as error:  # noqa: BLE001 - actual entry/assertion outcome
        outcome = type(error).__name__
    else:
        outcome = "RETURN"
    assert outcome == expected_outcome, name
    module = ast.parse(source)
    function = module.body[-1]
    target = function.body[0].body[0]
    assert support._is_enforced(function, target, module) is (outcome == "AssertionError")
