"""Execution controls for builtin entry identity and lexical shadowing."""

import ast
import builtins
import contextlib

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced

ROWS = [
    (
        "subscript extracted builtin namespace mutator",
        "import contextlib\ndef patch():\n    writer=contextlib.__builtins__['exec']\n    writer('import builtins; builtins.list=contextlib.nullcontext')\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "augmented builtin namespace mutation",
        "import contextlib\ndef patch():\n    namespace=contextlib.__builtins__\n    namespace |= {'list':contextlib.nullcontext}\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "enclosing opaque suffix can replace builtin",
        "import builtins\nimport contextlib\ndef parent(setup):\n    def outer(x):\n        m=list()\n        with(cs:=m):\n            assert x!=1\n    setup()\n    return outer\n",
        "parent-opaque",
        True,
        "AssertionError",
    ),
    (
        "enclosing opaque prefix can replace builtin",
        "import builtins\nimport contextlib\ndef parent(setup):\n    setup()\n    def outer(x):\n        m=list()\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-opaque",
        True,
        "AssertionError",
    ),
    (
        "source global class writer replaces builtin name",
        "import contextlib\ndef patch():\n    global int\n    class Meta(type):\n        def __enter__(cls): return cls\n        def __exit__(cls,*exc): return False\n    class int(metaclass=Meta): pass\ndef outer(x):\n    m=int\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "source global import writer replaces builtin name",
        "import contextlib\ndef patch():\n    global int\n    from contextlib import nullcontext as int\ndef outer(x):\n    global int\n    m=int()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "direct global declaration retains builtin class",
        "def outer(x):\n    global int\n    m=int\n    with(cs:=m):\n        assert x!=1\n",
        "outer",
        False,
        "TypeError",
    ),
    (
        "source global writer can replace builtin name",
        "import contextlib\ndef patch():\n    global int\n    int=contextlib.nullcontext\ndef outer(x):\n    m=int()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "direct global declaration retains genuine builtin",
        "def outer(x):\n    global list\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "outer",
        False,
        "TypeError",
    ),
    (
        "source-known helper returns a namespace mutator",
        "import contextlib\ndef get_writer():\n    return exec\ndef patch():\n    writer=get_writer()\n    writer('import builtins, contextlib; builtins.list=contextlib.nullcontext')\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "imported builtin namespace export mutation",
        "from builtins import __dict__ as namespace\nimport contextlib\ndef patch():\n    namespace.update(list=contextlib.nullcontext)\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "builtin namespace reachable through a module getter",
        "import contextlib\ndef patch():\n    contextlib.__builtins__.update(list=contextlib.nullcontext)\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "source-known builtin namespace dictionary update",
        "import builtins\nimport contextlib\ndef patch():\n    builtins.__dict__.update(list=contextlib.nullcontext)\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "enclosing global declaration stops outer cell lookup",
        "def top(list):\n    def middle():\n        global list\n        def outer(x):\n            m=list()\n            with(cs:=m):\n                assert x!=1\n        return outer\n    return middle()\n",
        "global-boundary",
        False,
        "TypeError",
    ),
    (
        "annotated builtin namespace alias mutation",
        "import builtins\nimport contextlib\ndef patch():\n    b:object=builtins\n    b.list=contextlib.nullcontext\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "destructured builtin namespace alias mutation",
        "import builtins\nimport contextlib\ndef patch():\n    b,*tail=[builtins]\n    b.list=contextlib.nullcontext\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "annotated setter alias mutation",
        "import builtins\nimport contextlib\ndef patch():\n    setter:object=setattr\n    setter(builtins,'list',contextlib.nullcontext)\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "caller setup callback can replace builtin constructor",
        "def outer(x,setup):\n    setup()\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "opaque-prefix",
        True,
        "AssertionError",
    ),
    (
        "parent parameter list()",
        "import contextlib\ndef parent(list):\n    def outer(x):\n        m=list()\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-call",
        True,
        "AssertionError",
    ),
    (
        "parent parameter tuple()",
        "import contextlib\ndef parent(tuple):\n    def outer(x):\n        m=tuple()\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-call",
        True,
        "AssertionError",
    ),
    (
        "parent parameter set()",
        "import contextlib\ndef parent(set):\n    def outer(x):\n        m=set()\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-call",
        True,
        "AssertionError",
    ),
    (
        "parent parameter dict()",
        "import contextlib\ndef parent(dict):\n    def outer(x):\n        m=dict()\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-call",
        True,
        "AssertionError",
    ),
    (
        "parent parameter object()",
        "import contextlib\ndef parent(object):\n    def outer(x):\n        m=object()\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-call",
        True,
        "AssertionError",
    ),
    (
        "parent parameter len",
        "import contextlib\ndef parent(len):\n    def outer(x):\n        m=len\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-manager",
        True,
        "AssertionError",
    ),
    (
        "parent parameter int",
        "import contextlib\ndef parent(int):\n    def outer(x):\n        m=int\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-manager",
        True,
        "AssertionError",
    ),
    (
        "parent parameter memoryview",
        "import contextlib\ndef parent(memoryview):\n    def outer(x):\n        m=memoryview\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-manager",
        True,
        "AssertionError",
    ),
    (
        "parent store",
        "import contextlib\ndef parent():\n    list=contextlib.nullcontext\n    def outer(x):\n        m=list()\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-noarg",
        True,
        "AssertionError",
    ),
    (
        "parent import",
        "import contextlib\ndef parent():\n    from contextlib import nullcontext as list\n    def outer(x):\n        m=list()\n        with(cs:=m):\n            assert x!=1\n    return outer\n",
        "parent-noarg",
        True,
        "AssertionError",
    ),
    (
        "class method uses enclosing cell",
        "import contextlib\ndef parent(list):\n    class C:\n        def outer(self,x):\n            m=list()\n            with(cs:=m):\n                assert x!=1\n    return C().outer\n",
        "parent-call",
        True,
        "AssertionError",
    ),
    (
        "module member mutation",
        "import builtins\nimport contextlib\nbuiltins.list=lambda:contextlib.nullcontext()\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "outer",
        True,
        "AssertionError",
    ),
    (
        "module setter mutation",
        'import builtins\nimport contextlib\nsetattr(builtins,"list",lambda:contextlib.nullcontext())\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n',
        "outer",
        True,
        "AssertionError",
    ),
    (
        "called prefix mutation",
        "import builtins\nimport contextlib\ndef patch():\n    builtins.list=lambda:contextlib.nullcontext()\ndef outer(x):\n    patch()\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "outer",
        True,
        "AssertionError",
    ),
    (
        "external source-known mutation",
        "import builtins\nimport contextlib\ndef patch():\n    builtins.list=lambda:contextlib.nullcontext()\ndef outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "patch-first",
        True,
        "AssertionError",
    ),
    (
        "genuine builtin list()",
        "def outer(x):\n    m=list()\n    with(cs:=m):\n        assert x!=1\n",
        "outer",
        False,
        "TypeError",
    ),
    (
        "genuine builtin int",
        "def outer(x):\n    m=int\n    with(cs:=m):\n        assert x!=1\n",
        "outer",
        False,
        "TypeError",
    ),
    (
        "genuine builtin len",
        "def outer(x):\n    m=len\n    with(cs:=m):\n        assert x!=1\n",
        "outer",
        False,
        "TypeError",
    ),
    (
        "genuine builtin object()",
        "def outer(x):\n    m=object()\n    with(cs:=m):\n        assert x!=1\n",
        "outer",
        False,
        "TypeError",
    ),
    (
        "genuine builtin memoryview",
        "def outer(x):\n    m=memoryview\n    with(cs:=m):\n        assert x!=1\n",
        "outer",
        False,
        "TypeError",
    ),
    (
        "enterable memoryview instance",
        'def outer(x):\n    m=memoryview(b"xy")\n    with(cs:=m):\n        assert x!=1\n',
        "outer",
        True,
        "AssertionError",
    ),
]


@pytest.mark.parametrize("label,source,invocation,enforced,outcome", ROWS, ids=[r[0] for r in ROWS])
def test_builtin_alias_entry_matches_executed_binding(label, source, invocation, enforced, outcome):
    namespace = {}
    saved = builtins.list
    try:
        exec(compile(source, label, "exec"), namespace)  # noqa: S102
        if invocation == "parent-opaque":
            call = namespace["parent"](lambda: setattr(builtins, "list", contextlib.nullcontext))
        elif invocation == "parent-call":
            call = namespace["parent"](contextlib.nullcontext)
        elif invocation == "parent-manager":
            call = namespace["parent"](contextlib.nullcontext())
        elif invocation == "parent-noarg":
            call = namespace["parent"]()
        elif invocation == "global-boundary":
            call = namespace["top"](contextlib.nullcontext)
        elif invocation == "opaque-prefix":
            call = lambda x: namespace["outer"](
                x, lambda: setattr(builtins, "list", contextlib.nullcontext)
            )
        else:
            if invocation == "patch-first":
                namespace["patch"]()
            call = namespace["outer"]
        try:
            call(1)
            result = "returned"
        except (AssertionError, TypeError) as error:
            result = type(error).__name__
    finally:
        builtins.list = saved
    assert result == outcome
    module = ast.parse(source)
    function = next(
        n for n in ast.walk(module) if isinstance(n, ast.FunctionDef) and n.name == "outer"
    )
    query = next(n for n in ast.walk(function) if isinstance(n, ast.Assert))
    assert _is_enforced(function, query, module) is enforced
