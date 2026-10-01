"""Execute global manager provenance around unrelated inert blocks (#526)."""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

ROWS = [
    (
        "0",
        "import contextlib\ncs=contextlib.suppress(AssertionError)\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "RETURN",
        False,
    ),
    (
        "1",
        "import contextlib\ncs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "2",
        "import contextlib\nother=0\ncs=contextlib.suppress(AssertionError)\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "RETURN",
        False,
    ),
    (
        "3",
        "import contextlib\nother=0\ncs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "4",
        "import contextlib\nif True:\n other=0\ncs=contextlib.suppress(AssertionError)\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "RETURN",
        False,
    ),
    (
        "5",
        "import contextlib\nif True:\n other=0\ncs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "6",
        "import contextlib\ntry:\n other=0\nexcept ValueError:\n other=1\ncs=contextlib.suppress(AssertionError)\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "RETURN",
        False,
    ),
    (
        "7",
        "import contextlib\ntry:\n other=0\nexcept ValueError:\n other=1\ncs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "false ifsuppress(AssertionError)",
        "import contextlib\nif False:\n other=0\nelse:\n other=1\ncs=contextlib.suppress(AssertionError)\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "RETURN",
        False,
    ),
    (
        "false ifnullcontext()",
        "import contextlib\nif False:\n other=0\nelse:\n other=1\ncs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "finallysuppress(AssertionError)",
        "import contextlib\ntry:\n other=0\nexcept ValueError:\n other=1\nfinally:\n another=2\ncs=contextlib.suppress(AssertionError)\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "RETURN",
        False,
    ),
    (
        "finallynullcontext()",
        "import contextlib\ntry:\n other=0\nexcept ValueError:\n other=1\nfinally:\n another=2\ncs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "try elsesuppress(AssertionError)",
        "import contextlib\ntry:\n other=0\nexcept ValueError:\n other=1\nelse:\n another=2\ncs=contextlib.suppress(AssertionError)\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "RETURN",
        False,
    ),
    (
        "try elsenullcontext()",
        "import contextlib\ntry:\n other=0\nexcept ValueError:\n other=1\nelse:\n another=2\ncs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "pass trysuppress(AssertionError)",
        "import contextlib\ntry:\n pass\nexcept ValueError:\n pass\ncs=contextlib.suppress(AssertionError)\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "RETURN",
        False,
    ),
    (
        "pass trynullcontext()",
        "import contextlib\ntry:\n pass\nexcept ValueError:\n pass\ncs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "callbackif True:\n reset()\n",
        "import contextlib\ncs=contextlib.suppress(AssertionError)\ndef reset():\n global cs\n cs=contextlib.nullcontext()\nif True:\n reset()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "callbacktry:\n reset()\nexcept ValueError:\n pass\n",
        "import contextlib\ncs=contextlib.suppress(AssertionError)\ndef reset():\n global cs\n cs=contextlib.nullcontext()\ntry:\n reset()\nexcept ValueError:\n pass\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "callbacktry:\n other=0\nexcept ValueError:\n pass\nfinally:\n reset()\n",
        "import contextlib\ncs=contextlib.suppress(AssertionError)\ndef reset():\n global cs\n cs=contextlib.nullcontext()\ntry:\n other=0\nexcept ValueError:\n pass\nfinally:\n reset()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
    (
        "conditional store",
        "import contextlib\ncs=contextlib.suppress(AssertionError)\nflag=True\nif flag:\n cs=contextlib.nullcontext()\ndef outer(x):\n with cs:\n  assert x!=1\n",
        "AssertionError",
        True,
    ),
]


@pytest.mark.parametrize(
    "label,source,expected_outcome,enforced", ROWS, ids=[row[0] for row in ROWS]
)
def test_module_manager_provenance(label, source, expected_outcome, enforced):
    namespace = {}
    exec(source, namespace)  # noqa: S102 - complete independent runtime oracle
    try:
        namespace["outer"](1)
    except BaseException as error:  # noqa: BLE001 - actual failure outcome
        outcome = type(error).__name__
    else:
        outcome = "RETURN"
    assert outcome == expected_outcome, label
    assert (outcome == "AssertionError") is enforced
    module = ast.parse(source)
    function = module.body[-1]
    query = function.body[0].body[0]
    assert support._is_enforced(function, query, module) is enforced
