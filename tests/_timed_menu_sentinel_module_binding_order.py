"""Module binding order and name-entry contracts.

Actual test functions and literal cases from the original collector.
"""

import ast
import contextlib
from types import SimpleNamespace

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_conditional_stores import (
    ROUND_EIGHT_SOURCES,
)
from tests._timed_menu_sentinel_user_exit_classes import (
    USER_EXIT_SWALLOW_SHAPES,
)

#: The round-7 review's findings against `b90f985`. These are **regressions the
#: round-6 repairs introduced**, each caught by execution at `x=2` and each
#: pinned here with a positive control beside it.
ROUND_SEVEN_SOURCES = (
    # Review finding 1. `_statement_always_runs` decides that a store inside
    # `if True:` always runs, but it only looked at the *innermost* block. The
    # store here is also inside a `for _ in ():` that never iterates, so it
    # never runs at all: `cs` is still the `nullcontext()` bound above it, the
    # header is entered, and the assert is reachable. Treating the store as
    # unconditional settled `cs` on the `cs = list()` that never ran and
    # reported a `TypeError`-raising header DEAD. Every block on the path has
    # to run for the rule to fire.
    (
        "an always-true branch inside a loop that never runs",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "for _ in ():\n"
            "    if True:\n"
            "        cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The same defect by the `while False:` spelling of a never-run body.
    (
        "an always-true branch inside a while loop that never runs",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "while False:\n"
            "    if True:\n"
            "        cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The positive control for finding 1. The loop *does* iterate, so the
    # always-true store really does run and really does rebind `cs` to the
    # empty list, which `with` cannot enter. A fix that simply stopped
    # counting always-true stores would report this header LIVE.
    (
        "an always-true branch inside a loop that does run",
        (
            "from contextlib import nullcontext\n"
            "cs = nullcontext()\n"
            "for _ in (1,):\n"
            "    if True:\n"
            "        cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    # Review finding 3. `builtins` is rebound *after* the call, so it is not
    # yet in force where the call is evaluated: `cs` holds the empty list the
    # real builtin returned, and `with cs:` raises. The module rebinding check
    # scanned the whole module, so it counted a store that had not run and
    # read the rebound attribute, reporting the `TypeError` as LIVE.
    (
        "builtins rebound after the qualified call",
        (
            "from types import SimpleNamespace\n"
            "import builtins\n"
            "cs = builtins.list()\n"
            "builtins = SimpleNamespace(list=len)\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    # The positive control for the *other* direction: the same rebinding
    # before the call, but to something that does build a real context manager.
    # The store is shadowed for real here, so the assert is reachable.
    (
        "builtins rebound to a manager before the qualified call",
        (
            "from contextlib import nullcontext\n"
            "from types import SimpleNamespace\n"
            "import builtins\n"
            "builtins = SimpleNamespace(list=nullcontext)\n"
            "cs = builtins.list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
)


#: The round-6 review's findings against `5535135`. Three were **false-dead**
#: -- the tool reported a header DEAD where CPython enters it, which is the
#: damaging direction because it certifies a reachable assert as swallowed --
#: and one was a residual **false-live** inside a single module block.
#:
#: Every row is *executed* first, with `x=2` so that `assert x != 1` is TRUE.
#: A clean return therefore proves the with-body was ENTERED (LIVE) and any
#: exception proves it was not (DEAD). That is the opposite of an
#: `x=1`-plus-`except AssertionError` harness, which cannot tell a body that
#: was entered from one whose failure a suppressor ate.
ROUND_SIX_SOURCES = (
    # Finding 1. The call is inside `outer`'s body, and a function body is only
    # reached *after the whole module has executed*. The `def list()` below has
    # therefore already run by the time `outer(2)` is called, so `cs` holds a
    # `nullcontext` and the assert is reachable. Cutting the module walk at
    # `outer`'s own `def` -- correct for a *module-scope* call -- read the
    # builtin `list` instead and reported the header DEAD.
    (
        "a module binding written after the function definition",
        (
            "from contextlib import nullcontext\n"
            "def outer(x):\n"
            "    cs = list()\n"
            "    with cs:\n"
            "        assert x != 1\n"
            "def list():\n"
            "    return nullcontext()\n"
        ),
        True,
    ),
    # Finding 1, second spelling. An assignment binds the module name exactly
    # as a `def` does, so the position argument is the same and the answer has
    # to be the same.
    (
        "a module assignment written after the function definition",
        (
            "from contextlib import nullcontext\n"
            "def outer(x):\n"
            "    cs = list()\n"
            "    with cs:\n"
            "        assert x != 1\n"
            "list = nullcontext\n"
        ),
        True,
    ),
    # Finding 2. An `except` handler body runs in the scope that encloses it,
    # and this one has plainly run by the time `cs = list()` is evaluated. The
    # module walk opened `try` bodies, `else` and `finally` but not the
    # handlers, so the binding was skipped and the builtin `list` was read.
    (
        "a module binding inside an except handler",
        (
            "from contextlib import nullcontext\n"
            "try:\n"
            "    raise ValueError\n"
            "except ValueError:\n"
            "    def list():\n"
            "        return nullcontext()\n"
            "cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # Finding 3. `builtins.attr` only names the real module while `builtins`
    # itself is unbound. A store that replaces the name makes the attribute
    # lookup reach an arbitrary object, and `builtins.list()` is then
    # `nullcontext()`. Answering `False` for every `builtins.*` callee read
    # the shadowed call as the builtin and reported the header DEAD.
    (
        "a rebound builtins name read through an attribute",
        (
            "from contextlib import nullcontext\n"
            "from types import SimpleNamespace\n"
            "builtins = SimpleNamespace(list=nullcontext)\n"
            "cs = builtins.list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # Finding 3, function-local spelling. The same attribute read, but the
    # store is a local rather than a module one, so it is caught by the
    # function-scope half of the question rather than the module half.
    (
        "a function-local builtins name read through an attribute",
        (
            "from contextlib import nullcontext\n"
            "def outer(x):\n"
            "    from types import SimpleNamespace\n"
            "    builtins = SimpleNamespace(list=nullcontext)\n"
            "    cs = builtins.list()\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # Finding 4, the residual false-live. `cs = list()` runs BEFORE the
    # `def list()` beside it, so the call really is the builtin and binds the
    # empty list, which `with` cannot enter. The order cut used to stop at the
    # enclosing `if` as a whole statement, so the `def` inside that same block
    # read as though it shadowed the call, and a `TypeError`-raising header
    # came back LIVE.
    (
        "a later binding inside the same module block as the call",
        (
            "from contextlib import nullcontext\n"
            "if True:\n"
            "    cs = list()\n"
            "    def list():\n"
            "        return nullcontext()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
    # The positive control for finding 4, and the one that would break if the
    # always-true branch were ignored. The shadow is written *before* the call
    # inside the same block, so the call really is the shadow and the assert is
    # reachable.
    (
        "an earlier binding inside the same module block as the call",
        (
            "from contextlib import nullcontext\n"
            "if True:\n"
            "    def list():\n"
            "        return nullcontext()\n"
            "    cs = list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        True,
    ),
    # The positive control for finding 3. `import builtins` is the ordinary
    # import that brings the *real* module in, so the qualified spelling still
    # reaches the builtin and the empty list cannot be entered. Reading the
    # canonical import as a rebinding would report this header LIVE.
    (
        "the canonical import of builtins read through an attribute",
        (
            "import builtins\n"
            "cs = builtins.list()\n"
            "def outer(x):\n"
            "    with cs:\n"
            "        assert x != 1\n"
        ),
        False,
    ),
)


def _fixture_is_entered(label, source):
    """Does CPython enter the with-body? Executed, never assumed.

    The fixture is called with ``x=2``, which makes ``assert x != 1`` true. A
    clean return therefore means the body ran (LIVE); any exception means it
    did not (DEAD). The assert is therefore never *reached as a failure*: the
    question is only whether entry raised before it, so the harness counts a
    clean return as LIVE rather than catching ``AssertionError`` as success.
    """
    namespace = {"nullcontext": contextlib.nullcontext, "SimpleNamespace": SimpleNamespace}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](2)
    except Exception:  # noqa: BLE001 - any exception means the body was not entered
        return False
    return True


@pytest.mark.parametrize(
    ("label", "source", "expected_live"),
    (*ROUND_EIGHT_SOURCES, *ROUND_SEVEN_SOURCES, *ROUND_SIX_SOURCES),
    ids=[shape[0] for shape in (*ROUND_EIGHT_SOURCES, *ROUND_SEVEN_SOURCES, *ROUND_SIX_SOURCES)],
)
def test_module_scope_order_scope_and_builtins_reading(label, source, expected_live):
    """A function-body call, a handler body and a rebound ``builtins`` are read right.

    #388-5e. The round-6 review found three false-deads and one false-live in
    the module-scope shadowing rule, each from a different place where the walk
    stopped short of the truth:

    * a function body runs only *after* the whole module has executed, so a
      module binding written **after** the function's ``def`` still shadows the
      callee there -- the order cut is only valid for a module-scope call;
    * an ``except`` **handler** body runs in the enclosing scope, so a binding
      in one is a module binding, and the walk has to open the handlers;
    * ``builtins.attr`` only names the real module while ``builtins`` itself is
      unbound, so the qualified spelling needs the base name checked -- while
      ``import builtins`` must keep counting as the real module;
    * and a binding *after* the call **inside the same block** does not shadow
      it, which needs the descent to be order-aware rather than to stop at the
      enclosing statement.

    #388-5f. The round-7 review then found two *regressions* those repairs
    # introduced, and both are the same mistake in opposite directions: a rule
    # that keys on one enclosing block and ignores the rest of the path.

    * ``_statement_always_runs`` read the innermost ``if True:`` and ignored a
      ``for _ in ():`` around it, so a store that never runs settled the name;
    * the ``builtins`` rebinding check scanned the whole module, so a store
      written *after* a module-scope call counted even though it had not run
      yet. Both are checked here, each beside a positive control that fails if
      the rule is dropped rather than narrowed.

    Each row is executed under real CPython with ``x=2`` before its verdict is
    checked, so the expectation is CPython's own answer. A fixture that does
    not behave as the row claims fails here rather than passing vacuously.
    """
    assert _fixture_is_entered(label, source) is expected_live, (
        f"{label}: CPython "
        f"{'entered' if expected_live else 'did not enter'} the with-body, so this "
        f"row cannot pin the opposite verdict. Either the fixture is wrong or "
        f"the expectation is."
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 1, f"{label}: fixture declared {len(asserts)} asserts, expected 1"
    assert [_is_enforced(function, asserts[0], tree)] == [expected_live], (
        f"{label}: expected the header to be reported "
        f"{'LIVE' if expected_live else 'DEAD'}. A callee shadowed by a module "
        f"binding that has already run is a different callable; one that has "
        f"not run yet still reaches the real builtin."
    )


def test_a_self_alias_excludes_its_own_store_and_not_an_earlier_one():
    """The self-alias tie is broken by excluding the *walrus*, not any store.

    A self-alias shares a binding order with the loop target it follows, so
    every position-based test ties and #367's tie branch declines it. Excluding
    the entry whose statement is the store being resolved breaks that tie in
    favour of the loop element -- which is what the interpreter reads.

    The row is here to pin *which* entry gets excluded. The other self-alias
    tests in this file all start from a loop whose element is the first
    binding of the name, so excluding "the first entry with a value" and
    excluding "the self-alias store" are indistinguishable there: the loop
    element is the same entry either way. That mutant survives all 446 tests
    in this module.

    Giving the name a prior store separates them. The loop element retires the
    suppressor, so the assert is live and must stay ``enforced``; the mutant
    excludes the loop element instead, the retired suppressor is adopted, and
    the live assert is reported as defeated -- the damaging direction.
    """
    source = (
        "def probe(x):\n"
        "    import contextlib\n"
        "    cs = contextlib.suppress(AssertionError)\n"
        "    for cs in (contextlib.nullcontext(),):\n"
        "        with (cs := cs):\n"
        '            assert x != 1, "A1"\n'
    )
    tree = ast.parse(source)
    probe = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "probe"
    )
    target = next(node for node in ast.walk(probe) if isinstance(node, ast.Assert))
    assert _is_enforced(probe, target, tree), (
        "the self-alias tie was broken by excluding the loop's own element "
        "rather than the walrus, so a retired suppressor was adopted and a "
        "live assert was reported as defeated"
    )


@pytest.mark.parametrize(
    ("elements", "header", "live"),
    [
        ("contextlib.nullcontext(), contextlib.suppress(AssertionError)", "cs", True),
        ("contextlib.nullcontext(), 1", "cs", True),
        ("contextlib.suppress(AssertionError), contextlib.nullcontext()", "cs", True),
        ("contextlib.nullcontext(), contextlib.suppress(AssertionError)", "(cs := cs)", True),
        ("contextlib.nullcontext(), contextlib.suppress(AssertionError)", "alias", True),
        ("contextlib.nullcontext()", "cs", True),
        ("contextlib.suppress(AssertionError)", "cs", False),
        (
            "contextlib.suppress(AssertionError), contextlib.suppress(AssertionError)",
            "cs",
            False,
        ),
        ("contextlib.nullcontext(), contextlib.nullcontext()", "cs", True),
        (
            "contextlib.suppress(AssertionError), contextlib.suppress(ValueError)",
            "cs",
            True,
        ),
    ],
)
def test_loop_body_header_reads_current_iteration_not_final_element(elements, header, live):
    alias = "        alias = cs\n" if header == "alias" else ""
    source = (
        "import contextlib\ndef outer(x):\n"
        f"    for cs in ({elements},):\n"
        + alias
        + f"        with {header}:\n            assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<loop-body-position>", "exec"), namespace)  # noqa: S102
    fired = False
    try:
        namespace["outer"](1)
    except AssertionError:
        fired = True
    assert fired is live
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is live


@pytest.mark.parametrize(
    ("label", "body", "enforced"),
    [
        (
            "a later terminal try cannot kill an earlier assert",
            "    assert x != 1\n    try:\n        return\n    except Exception:\n        pass\n",
            True,
        ),
        (
            "an exception before return can resume through a handler",
            "    try:\n        helper()\n        return\n    except ValueError:\n        pass\n    assert x != 1\n",
            True,
        ),
        (
            "a return value can raise before returning",
            "    try:\n        return helper()\n    except ValueError:\n        pass\n    assert x != 1\n",
            True,
        ),
        (
            "an exception bypasses a returning else",
            "    try:\n        helper()\n    except ValueError:\n        pass\n    else:\n        return\n    assert x != 1\n",
            True,
        ),
        (
            "all exception paths transfer before a returning else",
            "    try:\n        helper()\n    except ValueError:\n        return\n    else:\n        return\n    assert x != 1\n",
            False,
        ),
        (
            "a finally return overrides a falling through handler",
            "    try:\n        helper()\n    except ValueError:\n        pass\n    finally:\n        return\n    assert x != 1\n",
            False,
        ),
        (
            "a finalizer call preserves a pending return",
            "    try:\n        return\n    finally:\n        helper()\n    assert x != 1\n",
            False,
        ),
        (
            "a nested try can catch an exception from a return value",
            "    try:\n        try:\n            return helper()\n        except ValueError:\n            pass\n    finally:\n        pass\n    assert x != 1\n",
            True,
        ),
        (
            "a finalizer break resumes after the loop",
            "    for unused in (1,):\n        try:\n            return\n        finally:\n            break\n    assert x != 1\n",
            True,
        ),
        (
            "a handler's trailing transfer need not run after an inner catch",
            "    try:\n        helper()\n    except ValueError:\n        try:\n            return helper()\n        except ValueError:\n            pass\n    assert x != 1\n",
            True,
        ),
    ],
)
def test_try_reachability_matches_executed_exception_paths(label, body, enforced):
    """A possible exception path must not silently drop a live assertion."""
    source = "def outer(x, helper):\n" + body
    namespace = {}
    exec(compile(source, f"<try-path:{label}>", "exec"), namespace)  # noqa: S102 - executed fixture

    def raises():
        raise ValueError("the exception path")

    reached = []
    for helper in (lambda: None, raises):
        try:
            namespace["outer"](1, helper)
        except AssertionError:
            reached.append(True)
        except ValueError:
            reached.append(False)
        else:
            reached.append(False)
    assert any(reached) is enforced, f"{label}: the fixture's executed paths disagree"
    tree = ast.parse(source)
    function = tree.body[0]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, assertion, tree) is enforced, label


@pytest.mark.parametrize(
    ("label", "exit_body", "body", "enforced"),
    [row for row in USER_EXIT_SWALLOW_SHAPES if row[0].startswith("inline")],
    ids=[row[0] for row in USER_EXIT_SWALLOW_SHAPES if row[0].startswith("inline")],
)
def test_inline_user_exit_acceptance_matches_execution(label, exit_body, body, enforced):
    indented_exit = "\n".join("    " + line for line in exit_body.splitlines())
    source = (
        "import contextlib\n"
        "def factory(): return contextlib.nullcontext()\n"
        "class Suppressor:\n"
        "    def __enter__(self): return self\n"
        + indented_exit
        + "\ndef outer(x, flag):\n"
        + body
        + "\n"
    )
    namespace = {}
    exec(compile(source, "<inline-exit-control>", "exec"), namespace)  # noqa: S102
    propagated = False
    try:
        namespace["outer"](1, True)
    except AssertionError:
        propagated = True
    assert propagated is enforced, label
    tree = ast.parse(source)
    outer = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(outer) if isinstance(node, ast.Assert))
    assert _is_enforced(outer, target, tree) is enforced, label


@pytest.mark.parametrize("shadow", ("parameter", "later assignment"))
def test_inline_class_name_shadowing_keeps_a_live_context_manager(shadow):
    argument = "Manager" if shadow == "parameter" else ""
    assignment = "    Manager = contextlib.nullcontext\n" if shadow == "later assignment" else ""
    source = (
        "import contextlib\n"
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return True\n"
        + f"def outer({argument}):\n"
        + assignment
        + "    with Manager():\n        assert False\n"
    )
    namespace = {}
    exec(compile(source, "<inline-shadow>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        if shadow == "parameter":
            namespace["outer"](namespace["contextlib"].nullcontext)
        else:
            namespace["outer"]()
    tree = ast.parse(source)
    outer = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(outer) if isinstance(node, ast.Assert))
    assert _is_enforced(outer, target, tree) is True


@pytest.mark.parametrize("shadow", ("annotated assignment", "captured parameter"))
def test_inline_class_shadowing_in_annotations_and_captured_parameters(shadow):
    prefix = (
        "import contextlib\n"
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return True\n"
    )
    body = (
        "def outer():\n    Manager: object = contextlib.nullcontext\n    with Manager():\n        assert False\n"
        if shadow == "annotated assignment"
        else "def outer(Manager):\n    def inner():\n        with Manager():\n            assert False\n    inner()\n"
    )
    source = prefix + body
    namespace = {}
    exec(compile(source, "<inline-captured-shadow>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        if shadow == "captured parameter":
            namespace["outer"](namespace["contextlib"].nullcontext)
        else:
            namespace["outer"]()
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == ("inner" if shadow == "captured parameter" else "outer")
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True


@pytest.mark.parametrize(
    "binding",
    (
        "Manager, = (contextlib.nullcontext,)",
        "for Manager in (contextlib.nullcontext,):\n        pass",
        "from contextlib import nullcontext as Manager",
        "def Manager():\n        return contextlib.nullcontext()",
        "(Manager := contextlib.nullcontext)",
    ),
)
def test_inline_class_constructor_declines_nonclass_lexical_stores(binding):
    source = (
        "import contextlib\n"
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return True\n"
        "def outer():\n    " + binding + "\n"
        "    with Manager():\n        assert False\n"
    )
    namespace = {}
    exec(compile(source, "<inline-store-shadow>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"]()
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True


def test_inline_class_constructor_declines_a_captured_callable_binding():
    source = (
        "import contextlib\n"
        "class Manager:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return True\n"
        "def outer():\n"
        "    def inner():\n"
        "        with Manager():\n            assert False\n"
        "    Manager = contextlib.nullcontext\n"
        "    inner()\n"
    )
    namespace = {}
    exec(compile(source, "<captured-callable>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"]()
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "inner"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True


STARRED_LOOP_ENTRY_UNREACHABLE_ROWS = (
    (
        "a starred store inside a for body binds a list",
        (
            "    for _ in (1,):\n"
            "        *cs, = (contextlib.suppress(AssertionError),)\n"
            "        with cs:\n"
            "            assert x != 1\n"
        ),
        False,
    ),
    (
        "a starred loop target binds a list",
        (
            "    for *cs, in ((contextlib.suppress(AssertionError),),):\n"
            "        with cs:\n"
            "            assert x != 1\n"
        ),
        False,
    ),
    # Controls. Both keep a *narrow* repair honest. A plain loop target binds
    # the next element, which the rule cannot read without running the loop,
    # so #336 declines it and the assert stays live -- CPython agrees, the
    # nullcontext really is entered. A plain store in the same position is
    # likewise not decidable from syntax alone. A fix that keyed on "any
    # target inside a loop" rather than "a starred target" would report these
    # dead, and the interpreter check in `_assert_entry_contract` would catch
    # it.
    (
        "CONTROL a plain loop target over a real context manager",
        (
            "    for cs in (contextlib.nullcontext(),):\n"
            "        with cs:\n"
            "            assert x != 1\n"
        ),
        True,
    ),
    (
        "CONTROL a plain store in a for body over a real context manager",
        (
            "    for _ in (1,):\n"
            "        cs = contextlib.nullcontext()\n"
            "        with cs:\n"
            "            assert x != 1\n"
        ),
        True,
    ),
)
