"""Pin complete historical alias-read reproductions using executed body reachability.

Issue357's displayed R2 writes nullcontext before the second header: that header
is genuinely live for x=2. Its prose's later-after-both variant is separately
pinned as swallowed. These already-correct cases need no analyzer widening.
"""

import ast
import sys

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

ROWS = [
    (
        "loop-self-suppress",
        "import contextlib\nx=1\ndef probe():\n    for cs in [contextlib.suppress(AssertionError)]:\n        with (cs := cs):\n            assert x != 1\n",
        1,
        "RETURN",
        [False],
    ),
    (
        "loop-self-null",
        "import contextlib\nx=1\ndef probe():\n    for cs in [contextlib.nullcontext()]:\n        with (cs := cs):\n            assert x != 1\n",
        1,
        "AssertionError",
        [True],
    ),
    (
        "later-after-both",
        "import contextlib\ndef probe(x):\n    cs=contextlib.suppress(AssertionError)\n    with (cs := cs):\n        assert x != 1\n    with cs:\n        assert x != 2\n    cs=contextlib.nullcontext()\n",
        1,
        "RETURN",
        [False, False],
    ),
    (
        "later-after-both",
        "import contextlib\ndef probe(x):\n    cs=contextlib.suppress(AssertionError)\n    with (cs := cs):\n        assert x != 1\n    with cs:\n        assert x != 2\n    cs=contextlib.nullcontext()\n",
        2,
        "RETURN",
        [False, False],
    ),
    (
        "filed-before-second",
        "import contextlib\ndef probe(x):\n    cs=contextlib.suppress(AssertionError)\n    with (cs := cs):\n        assert x != 1\n    cs=contextlib.nullcontext()\n    with cs:\n        assert x != 2\n",
        1,
        "RETURN",
        [False, True],
    ),
    (
        "filed-before-second",
        "import contextlib\ndef probe(x):\n    cs=contextlib.suppress(AssertionError)\n    with (cs := cs):\n        assert x != 1\n    cs=contextlib.nullcontext()\n    with cs:\n        assert x != 2\n",
        2,
        "AssertionError",
        [False, True],
    ),
    (
        "try-import",
        "def probe(x):\n    from contextlib import suppress\n    with (cs := suppress(AssertionError)):\n        pass\n    try:\n        import os as cs\n        with cs:\n            assert x != 1\n    except TypeError:\n        pass\n",
        1,
        "RETURN",
        [False],
    ),
    (
        "inline",
        "class Suppressor:\n    def __enter__(self):return self\n    def __exit__(self,*exc):return exc[0] is AssertionError\ndef probe(x):\n    with Suppressor():\n        assert x != 1\n",
        1,
        "RETURN",
        [False],
    ),
]

# A source-visible factory remains outside the one-constructor-hop rule.
ROWS.append(
    (
        "factory-known-decline",
        "class Suppressor:\n    def __enter__(self):return self\n    def __exit__(self,*exc):return exc[0] is AssertionError\ndef make():return Suppressor()\ndef probe(x):\n    with make():\n        assert x != 1\n",
        1,
        "RETURN",
        [True],
    )
)


@pytest.mark.parametrize("label,source,x,outcome,expected", ROWS)
def test_complete_read_site_reproduction(label, source, x, outcome, expected):
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "probe")
    queries = [n for n in ast.walk(function) if isinstance(n, ast.Assert)]
    namespace = {}
    filename = f"<read-site:{label}>"
    exec(compile(source, filename, "exec"), namespace)  # noqa: S102
    visits = set()

    def trace(frame, event, arg):
        if event == "line" and frame.f_code.co_filename == filename:
            visits.add(frame.f_lineno)
        return trace

    old = sys.gettrace()
    sys.settrace(trace)
    try:
        args = [x] if function.args.args else []
        if outcome == "AssertionError":
            with pytest.raises(AssertionError):
                namespace["probe"](*args)
        else:
            namespace["probe"](*args)
    finally:
        sys.settrace(old)
    assert [support._is_enforced(function, q, tree) for q in queries] == expected
    if label == "try-import":
        assert all(q.lineno not in visits for q in queries)
    else:
        assert all(q.lineno in visits for q in queries)
