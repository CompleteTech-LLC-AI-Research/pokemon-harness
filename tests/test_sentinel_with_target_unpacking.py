"""Executed entry-target and completed-loop controls for #431/#425."""

import ast
import sys

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced

ROWS = [
    (
        "431-star-as-suppress-A",
        "def outer(x):\n    import contextlib\n    with contextlib.suppress(AssertionError) as (*cs,):\n        assert x != 1\n",
        "TypeError",
        False,
        False,
    ),
    (
        "431-star-as-suppress-V",
        "def outer(x):\n    import contextlib\n    with contextlib.suppress(ValueError) as (*cs,):\n        assert x != 1\n",
        "TypeError",
        False,
        False,
    ),
    (
        "431-star-as-null-None",
        "def outer(x):\n    import contextlib\n    with contextlib.nullcontext() as (*cs,):\n        assert x != 1\n",
        "TypeError",
        False,
        False,
    ),
    (
        "431-star-as-null-tuple",
        "def outer(x):\n    import contextlib\n    with contextlib.nullcontext((1,2)) as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "431-star-as-null-empty",
        "def outer(x):\n    import contextlib\n    with contextlib.nullcontext(()) as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "431-star-as-null-list",
        "def outer(x):\n    import contextlib\n    with contextlib.nullcontext([]) as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "431-star-as-null-string",
        'def outer(x):\n    import contextlib\n    with contextlib.nullcontext("hi") as (*cs,):\n        assert x != 1\n',
        "AssertionError",
        True,
        True,
    ),
    (
        "431-star-as-suppress-TypeError",
        "def outer(x):\n    import contextlib\n    with contextlib.suppress(TypeError) as (*cs,):\n        assert x != 1\n",
        "returned",
        False,
        False,
    ),
    (
        "431-undefined-star",
        "def outer(x):\n    import contextlib\n    with (*cs,):\n        assert x != 1\n",
        "NameError",
        False,
        False,
    ),
    (
        "431-bound-star",
        "def outer(x):\n    import contextlib\n    with (*(1,2),):\n        assert x != 1\n",
        "TypeError",
        False,
        False,
    ),
    (
        "431-tuple-manager",
        "def outer(x):\n    import contextlib\n    with ((contextlib.nullcontext(),),):\n        assert x != 1\n",
        "TypeError",
        False,
        False,
    ),
    (
        "431-ordinary-null",
        "def outer(x):\n    import contextlib\n    with contextlib.nullcontext() as cs:\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "431-ordinary-V",
        "def outer(x):\n    import contextlib\n    with contextlib.suppress(ValueError) as cs:\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "431-ordinary-A",
        "def outer(x):\n    import contextlib\n    with contextlib.suppress(AssertionError) as cs:\n        assert x != 1\n",
        "returned",
        True,
        False,
    ),
    (
        "425-last-None",
        "def outer(x):\n    import contextlib\n    for cs in (1,None):\n        pass\n    with cs:\n        assert x != 1\n",
        "TypeError",
        False,
        False,
    ),
    (
        "425-last-int",
        "def outer(x):\n    import contextlib\n    for cs in (1,2):\n        pass\n    with cs:\n        assert x != 1\n",
        "TypeError",
        False,
        False,
    ),
    (
        "425-last-null",
        "def outer(x):\n    import contextlib\n    for cs in (1,contextlib.nullcontext()):\n        pass\n    with cs:\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "425-last-V",
        "def outer(x):\n    import contextlib\n    for cs in (1,contextlib.suppress(ValueError)):\n        pass\n    with cs:\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "425-last-A",
        "def outer(x):\n    import contextlib\n    for cs in (1,contextlib.suppress(AssertionError)):\n        pass\n    with cs:\n        assert x != 1\n",
        "returned",
        True,
        False,
    ),
    (
        "431-successful-star-store-later-list-entry",
        "def outer(x):\n    import contextlib\n    with contextlib.nullcontext((1,2)) as (*cs,):\n        pass\n    with cs:\n        assert x != 1\n",
        "TypeError",
        False,
        False,
    ),
    (
        "431-nested-star-success",
        "def outer(x):\n    import contextlib\n    with contextlib.nullcontext((0,(1,2))) as (head,(*cs,)):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "431-nested-star-failure",
        "def outer(x):\n    import contextlib\n    with contextlib.nullcontext((0,None)) as (head,(*cs,)):\n        assert x != 1\n",
        "TypeError",
        False,
        False,
    ),
]


ROWS += [
    (
        "earlier context callback can change entry payload",
        "def outer(x,trigger):\n    import contextlib\n    with trigger, contextlib.nullcontext() as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "module evaluated metadata keeps patched entry live",
        "import contextlib\ndef install(arg=setattr(contextlib.nullcontext,'__enter__',lambda self: (1,2))):\n    pass\ndef outer(x):\n    import contextlib\n    with contextlib.nullcontext() as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "parameter shadow manager stays live",
        "import contextlib as real\nclass Fake:\n    @staticmethod\n    def nullcontext():\n        return real.nullcontext((1,2))\ndef outer(x,contextlib=Fake):\n    with contextlib.nullcontext() as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "source-known protocol patch preserves iterable live target",
        "import contextlib\ndef patch():\n    contextlib.nullcontext.__enter__ = lambda self: (1,2)\ndef outer(x):\n    import contextlib\n    with contextlib.nullcontext() as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "local constructor shadow stays live",
        "def outer(x):\n    import contextlib\n    factory = lambda: contextlib.nullcontext((1,2))\n    with factory() as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "opaque prefix stays live",
        "def setup():\n    return None\ndef outer(x):\n    import contextlib\n    setup()\n    with contextlib.nullcontext((1,2)) as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "module default constructor shadow stays live",
        "import contextlib\ndef factory():\n    return contextlib.nullcontext((1,2))\ndef outer(x, constructor=factory):\n    with constructor() as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "source-defined iterable entry stays live",
        "class Manager:\n    def __enter__(self):\n        return (1,2)\n    def __exit__(self,*args):\n        return False\ndef outer(x):\n    with Manager() as (*cs,):\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
    (
        "conditional final store keeps live path",
        "def outer(x):\n    import contextlib\n    for cs in (1,None):\n        pass\n    if x == 1:\n        cs = contextlib.nullcontext()\n    with cs:\n        assert x != 1\n",
        "AssertionError",
        True,
        True,
    ),
]


@pytest.mark.parametrize("label,source,outcome,reached,enforced", ROWS, ids=[r[0] for r in ROWS])
def test_entry_target_and_last_loop_value(label, source, outcome, reached, enforced):
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "outer")
    target = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    namespace = {}
    observed = []

    def trace(frame, event, arg):
        if (
            event == "line"
            and frame.f_code.co_filename == label
            and frame.f_lineno == target.lineno
        ):
            observed.append(True)
        return trace

    import contextlib

    original_enter = contextlib.nullcontext.__enter__
    exec(compile(source, label, "exec"), namespace)  # noqa: S102
    if label == "source-known protocol patch preserves iterable live target":
        namespace["patch"]()
    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        if label == "earlier context callback can change entry payload":

            class Trigger:
                def __enter__(self):
                    contextlib.nullcontext.__enter__ = lambda self: (1, 2)

                def __exit__(self, *args):
                    return False

            namespace["outer"](1, Trigger())
        else:
            namespace["outer"](1)
        actual = "returned"
    except (AssertionError, TypeError, NameError) as error:
        actual = type(error).__name__
    finally:
        sys.settrace(previous)
        contextlib.nullcontext.__enter__ = original_enter
    assert (actual, bool(observed)) == (outcome, reached)
    assert _is_enforced(function, target, tree) is enforced
