"""Execute starred stores before sibling headers and bare contracts."""

import ast
import sys

import pytest

from tests import _timed_menu_milestone_sentinel_support as support

# Unknown source effects keep their existing conservative decline; these
# controls also pin that the new positive witness invents no live path.
ROWS = [
    (
        "432 sibling",
        "for _ in (1,):\n    *cs,=(contextlib.suppress(AssertionError),)\nwith cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "423 prior assertion",
        "assert x==1\nfor _ in (1,):\n    *cs,=(contextlib.suppress(AssertionError),)\nwith cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "430 nested starred loop",
        "if flag:\n    for *cs, in ((1,),):\n        with cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "430 bare following assert",
        "for *cs, in ((1,),):\n    with cs:pass\n    assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "in-body store",
        "for _ in (1,):\n    *cs,=(contextlib.suppress(AssertionError),)\n    with cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "starred loop in-body",
        "for *cs, in ((1,),):\n    with cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "plain store",
        "*cs,=(contextlib.suppress(AssertionError),)\nwith cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "starred for later sibling",
        "for *cs, in ((1,),):pass\nwith cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "nested starred unpack",
        "for _ in (1,):\n    head,(*cs,)=(0,(contextlib.nullcontext(),))\nwith cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "two iteration starred store",
        "for _ in (1,2):\n    *cs,=(contextlib.nullcontext(),)\nwith cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "loop break after starred store",
        "for _ in (1,):\n    *cs,=(contextlib.nullcontext(),)\n    break\nwith cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "later real rebind",
        "for _ in (1,):\n    *cs,=(contextlib.suppress(AssertionError),)\ncs=contextlib.nullcontext()\nwith cs:assert x!=1",
        "AssertionError",
        True,
        True,
    ),
    (
        "in-body plain manager",
        "for _ in (1,):\n    cs=contextlib.nullcontext()\n    with cs:assert x!=1",
        "AssertionError",
        True,
        True,
    ),
    (
        "plain loop manager",
        "for cs in (contextlib.nullcontext(),):\n    with cs:assert x!=1",
        "AssertionError",
        True,
        True,
    ),
    (
        "plain swallowing manager",
        "for cs in (contextlib.suppress(AssertionError),):\n    with cs:assert x!=1",
        "RETURN",
        True,
        False,
    ),
    ("integer control", "cs=1\nwith cs:assert x!=1", "TypeError", False, False),
    (
        "empty loop retains manager",
        "cs=contextlib.nullcontext()\nfor _ in ():\n    *cs,=(contextlib.suppress(AssertionError),)\nwith cs:assert x!=1",
        "AssertionError",
        True,
        True,
    ),
    (
        "break before store retains manager",
        "cs=contextlib.nullcontext()\nfor _ in (1,):\n    break\n    *cs,=(contextlib.suppress(AssertionError),)\nwith cs:assert x!=1",
        "AssertionError",
        True,
        True,
    ),
    (
        "conditional rebind wins",
        "for _ in (1,):\n    *cs,=(contextlib.nullcontext(),)\nif flag:cs=contextlib.nullcontext()\nwith cs:assert x!=1",
        "AssertionError",
        True,
        True,
    ),
    (
        "caught failed header reaches later assertion",
        "for *cs, in ((1,),):\n    try:\n        with cs:pass\n    except TypeError:pass\n    assert x!=1",
        "AssertionError",
        True,
        True,
    ),
    (
        "source-known callback rebind",
        "for _ in (1,):\n    *cs,=(contextlib.nullcontext(),)\ndef change():\n    nonlocal cs\n    cs=contextlib.nullcontext()\nchange()\nwith cs:assert x!=1",
        "AssertionError",
        True,
        True,
    ),
    (
        "known decline: nonlocal helper stays local",
        "for _ in (1,):\n    *cs,=(contextlib.nullcontext(),)\ndef change():\n    cs=contextlib.nullcontext()\nchange()\nwith cs:assert x!=1",
        "TypeError",
        False,
        True,
    ),
    (
        "known decline: empty starred list truth stays false",
        "*cs,=()\nif cs:cs=contextlib.nullcontext()\nwith cs:assert x!=1",
        "TypeError",
        False,
        True,
    ),
    (
        "known decline: nested unpack failure blocks rebind",
        "head,(*cs,)=(0,None)\ncs=contextlib.nullcontext()\nwith cs:assert x!=1",
        "TypeError",
        False,
        True,
    ),
    (
        "continue before store retains manager",
        "cs=contextlib.nullcontext()\nfor _ in (1,):\n    continue\n    *cs,=(contextlib.suppress(AssertionError),)\nwith cs:assert x!=1",
        "AssertionError",
        True,
        True,
    ),
    (
        "plain star collection with assert afterward",
        "*cs,=(contextlib.nullcontext(),)\nwith cs:pass\nassert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "conditional branch star assignment",
        "if flag:\n    *cs,=(contextlib.nullcontext(),)\n    with cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "later integer rebind",
        "for _ in (1,):\n    *cs,=(contextlib.nullcontext(),)\ncs=1\nwith cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "plain tuple target is unenterable on current master",
        "for cs in ((1,),):\n    with cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
    (
        "aliased list binding then real rebind",
        "for _ in (1,):\n    *cs,=(contextlib.suppress(AssertionError),)\ncs=contextlib.nullcontext()\nwith (entered:=cs):assert x!=1",
        "AssertionError",
        True,
        True,
    ),
    (
        "deletion after starredstore",
        "for _ in (1,):\n    *cs,=(contextlib.nullcontext(),)\ndel cs\nwith cs:assert x!=1",
        "UnboundLocalError",
        False,
        False,
    ),
    (
        "known decline: helper callee overwritten before call",
        "*cs,=(contextlib.nullcontext(),)\ndef change():\n    nonlocal cs\n    cs=contextlib.nullcontext()\nchange=0\nchange()\nwith cs:assert x!=1",
        "TypeError",
        False,
        True,
    ),
    (
        "helper function binding is not entered manager",
        "*cs,=(contextlib.nullcontext(),)\ncs=contextlib.nullcontext()\ndef cs():pass\nwith cs:assert x!=1",
        "TypeError",
        False,
        False,
    ),
]


def source_for(body):
    lines = []
    for line in body.splitlines():
        if "with " in line and ":assert " in line:
            header, assertion = line.split(":assert ", 1)
            lines.extend(
                [header + ":", " " * (len(line) - len(line.lstrip()) + 4) + "assert " + assertion]
            )
        else:
            lines.append(line)
    return "import contextlib\ndef outer(x,flag=True):\n" + "".join(
        "    " + line + "\n" for line in lines
    )


@pytest.mark.parametrize("label,body,outcome,reached,enforced", ROWS, ids=[r[0] for r in ROWS])
def test_starred_path_matches_complete_execution(label, body, outcome, reached, enforced):
    source = source_for(body)
    module = ast.parse(source)
    function = module.body[1]
    query = max(
        (n for n in ast.walk(function) if isinstance(n, ast.Assert)), key=lambda n: n.lineno
    )
    namespace = {}
    exec(compile(source, label, "exec"), namespace)  # noqa: S102
    seen = []
    previous = sys.gettrace()

    def trace(frame, event, arg):
        if (
            frame.f_code is namespace["outer"].__code__
            and event == "line"
            and frame.f_lineno == query.lineno
        ):
            seen.append(True)
        return trace

    try:
        sys.settrace(trace)
        try:
            namespace["outer"](1)
            actual = "RETURN"
        except BaseException as error:  # noqa: BLE001
            actual = type(error).__name__
    finally:
        sys.settrace(previous)
    assert actual == outcome
    assert bool(seen) is reached
    assert support._is_enforced(function, query, module) is enforced
    if label.startswith("known decline:"):
        assert support._starred_binding_has_failure_witness(function, query, module) is False


@pytest.mark.parametrize("middle,header", [("", "prior, cs"), ("with prior:pass\n", "cs")])
def test_opaque_prior_entry_effects_provide_no_new_proof(middle, header):
    body = (
        "for _ in (1,):\n    *cs,=(contextlib.nullcontext(),)\n"
        + middle
        + "with "
        + header
        + ":assert x!=1"
    )
    source = source_for(body).replace("outer(x,flag=True)", "outer(x,prior,flag=True)")
    module = ast.parse(source)
    function = module.body[1]
    query = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    namespace = {}
    exec(source, namespace)  # noqa: S102
    import contextlib

    with pytest.raises(TypeError):
        namespace["outer"](1, contextlib.nullcontext())
    assert support._known_starred_binding_blocks_query(function, query, module) is False
    assert support._starred_binding_has_failure_witness(function, query, module) is False


@pytest.mark.parametrize(
    "signature,invoke", [("*x", lambda fn: fn(1)), ("**x", lambda fn: fn(x=1))]
)
def test_collector_parameters_do_not_supply_scalar_failure_witness(signature, invoke):
    body = "*cs,=()\ndef change():\n    nonlocal cs\n    cs=contextlib.nullcontext()\nchange()\nwith cs:assert x!=1"
    source = source_for(body).replace("x,flag=True", signature)
    module = ast.parse(source)
    function = module.body[1]
    query = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    namespace = {}
    exec(source, namespace)  # noqa: S102
    assert invoke(namespace["outer"]) is None
    assert support._starred_binding_has_failure_witness(function, query, module) is False
    # The pre-existing checker declines the collector-dependent predicate.
    # Pin the new witness separately: it must never manufacture scalar x=1.
    assert support._is_enforced(function, query, module) is True


@pytest.mark.parametrize("target", ["a,*cs", "(*cs,)"])
def test_failed_loop_target_unpack_cannot_supply_live_rebind_witness(target):
    body = (
        "for "
        + target
        + " in (1,):pass\ndef change():\n    nonlocal cs\n    cs=contextlib.nullcontext()\nchange()\nwith cs:assert x!=1"
    )
    source = source_for(body)
    module = ast.parse(source)
    function = module.body[1]
    query = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(TypeError):
        namespace["outer"](1)
    assert support._starred_binding_has_failure_witness(function, query, module) is False
