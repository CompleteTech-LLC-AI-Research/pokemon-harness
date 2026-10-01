"""Execute compound-store paths before reading their snapshotted manager (#361)."""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

SUPPRESS = "contextlib.suppress(AssertionError)"
LIVE = "contextlib.nullcontext()"


def source_for(body):
    return "import contextlib\ndef f(flag=False):\n" + "".join(
        " " + line + "\n" for line in body.splitlines()
    )


def executed(source, flag):
    namespace = {}
    exec(source, namespace)  # noqa: S102 - execute the independent runtime oracle
    try:
        namespace["f"](flag)
    except BaseException as error:  # noqa: BLE001 - retain all runtime outcomes
        return type(error).__name__
    return "RETURN"


def verdict(source):
    module = ast.parse(source)
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef))
    assertion = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    return support._is_enforced(function, assertion, module)


POSITIVE_PREFIXES = (
    ("filed", f"if flag:\n first={SUPPRESS}\nfirst={LIVE}"),
    ("if body else", f"if flag:\n first={SUPPRESS}\nelse:\n first={LIVE}"),
    ("nested", f"if True:\n if flag:\n  first={SUPPRESS}\n first={LIVE}"),
    ("for else", f"for item in (1,):\n first={SUPPRESS}\nelse:\n first={LIVE}"),
    ("empty for else", f"for item in ():\n first={SUPPRESS}\nelse:\n first={LIVE}"),
    ("list for else", f"for item in [1, 2]:\n first={SUPPRESS}\nelse:\n first={LIVE}"),
    ("try else", f"try:\n pass\nexcept ValueError:\n first={SUPPRESS}\nelse:\n first={LIVE}"),
    ("try finally", f"try:\n first={SUPPRESS}\nfinally:\n first={LIVE}"),
    (
        "break runs finally and skips loop else",
        f"first={SUPPRESS}\nfor item in (1,):\n try:\n  break\n finally:\n  first={LIVE}\nelse:\n first={SUPPRESS}",
    ),
    (
        "finally break overrides normal loop else",
        f"first={SUPPRESS}\nfor item in (1,):\n try:\n  pass\n finally:\n  first={LIVE}\n  break\nelse:\n first={SUPPRESS}",
    ),
    (
        "nested loop break stays in inner loop",
        f"first={SUPPRESS}\nfor item in (1,):\n for inner in (1,):\n  break\n else:\n  first={SUPPRESS}\nelse:\n first={LIVE}",
    ),
    (
        "nested try",
        f"try:\n if flag:\n  first={SUPPRESS}\n else:\n  first={LIVE}\nexcept Exception:\n first={SUPPRESS}",
    ),
    (
        "handler on alternate path",
        f"try:\n if flag:\n  raise ValueError\nexcept ValueError:\n first={SUPPRESS}\nelse:\n first={LIVE}",
    ),
)


@pytest.mark.parametrize(("label", "prefix"), POSITIVE_PREFIXES)
@pytest.mark.parametrize("hops", (0, 1, 2))
def test_structured_store_failure_witness_matches_execution(label, prefix, hops):
    manager = "first"
    for index in range(hops):
        alias = f"alias{index}"
        prefix += f"\n{alias}={manager}"
        manager = alias
    source = source_for(prefix + f"\nwith(cs:={manager}):assert 1==2")
    assert executed(source, False) == "AssertionError", label
    assert verdict(source) is True, label


NEGATIVE_PREFIXES = (
    ("if both swallowed", f"if flag:\n first={SUPPRESS}\nelse:\n first={SUPPRESS}", "RETURN"),
    (
        "for else suppresses",
        f"for item in (1,):\n first={LIVE}\nelse:\n first={SUPPRESS}",
        "RETURN",
    ),
    ("finally suppresses", f"try:\n first={LIVE}\nfinally:\n first={SUPPRESS}", "RETURN"),
    (
        "handler suppresses",
        f"try:\n raise ValueError\nexcept ValueError:\n first={SUPPRESS}",
        "RETURN",
    ),
    ("finally raises", f"try:\n first={LIVE}\nfinally:\n raise ValueError", "ValueError"),
    ("finally returns", f"try:\n first={LIVE}\nfinally:\n return", "RETURN"),
    ("body transfers", f"if not flag:\n return\nfirst={SUPPRESS}", "RETURN"),
    ("loop breaks", f"first={SUPPRESS}\nfor item in (1,):\n break\nelse:\n first={LIVE}", "RETURN"),
    (
        "finally break suppressing store",
        f"first={LIVE}\nfor item in (1,):\n try:\n  pass\n finally:\n  first={SUPPRESS}\n  break\nelse:\n first={LIVE}",
        "RETURN",
    ),
    ("unenterable final", "if flag:\n first=None\nelse:\n first=None", "TypeError"),
)


@pytest.mark.parametrize(("label", "prefix", "outcome"), NEGATIVE_PREFIXES)
def test_structured_witness_does_not_invent_failure(label, prefix, outcome):
    source = source_for(prefix + "\nsecond=first\nwith(cs:=second):assert 1==2")
    assert executed(source, False) == outcome, label
    assert verdict(source) is False, label


def test_structured_try_callback_is_a_decline_with_independent_live_execution():
    source = (
        "import contextlib\n"
        "def helper():pass\n"
        "def f(flag=False):\n"
        " try:\n  helper()\n except ValueError:\n  first=contextlib.suppress(AssertionError)\n"
        " else:\n  first=contextlib.nullcontext()\n"
        " with(cs:=first):assert 1==2\n"
    )
    assert executed(source, False) == "AssertionError"
    module = ast.parse(source)
    function = module.body[-1]
    assertion = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert support._is_enforced(function, assertion, module) is False
