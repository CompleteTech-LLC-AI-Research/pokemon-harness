"""User-exit suppression and class binding cases.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests import _timed_menu_milestone_sentinel_support as support
from tests._timed_menu_milestone_sentinel_support import _is_enforced

#: #316: a user-defined context manager swallows an assert exactly when its
#: ``__exit__`` returns truthy for the raised ``AssertionError``. That is a
#: different mechanism from every named suppressor -- none of the other rules
#: reads a return value, because none of them can.
#:
#: Executed, each of these really is silent::
#:
#:     >>> probe(1)          # returns normally, rc=0
#:
#: so reporting the assert as ``enforced`` would certify a disarmed contract as
#: load-bearing. The rows are paired with the loud controls below, because the
#: failure mode of a rule like this is firing on an ordinary context manager
#: rather than missing a suppressor.
USER_EXIT_SWALLOW_SHAPES = (
    # --- the filed shape, in the three bindings #316's criterion 4 names ---
    (
        "module-level instance",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    (
        "instance bound in a nested if",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        (
            "    if flag:\n"
            "        helper = Suppressor()\n"
            "        with helper:\n"
            "            assert x != 1"
        ),
        False,
    ),
    (
        "local alias",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        "    cs = Suppressor()\n    with cs:\n        assert x != 1",
        False,
    ),
    # The other two spellings of the same runtime test.
    (
        "named exception parameter",
        ("def __exit__(self, exc_type, exc, tb):\n    return exc_type is AssertionError"),
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    (
        "issubclass spelling",
        "def __exit__(self, *exc):\n    return issubclass(exc[0], AssertionError)",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    # The same test spelled `==`. At runtime `exc_type == AssertionError` is
    # true exactly when an assert was swallowed, so declining it left a real
    # swallow reported as `enforced`. Only `Is` was accepted, and the operator
    # guard was not pinned by any row, so both sides of that operator were
    # unpinned: dropping the guard let a *negated* test through, and adding `Eq`
    # closes the missed detection.
    (
        "equality spelling",
        "def __exit__(self, *exc):\n    return exc[0] == AssertionError",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    # --- #338: an __exit__ that swallows *every* exception ---
    #
    # #316 decided the spellings that say "the failure is an AssertionError".
    # These four never name `AssertionError` at all, yet every one of them is
    # *true* whenever `__exit__` is called with an assertion failure -- the only
    # call this rule asks about -- so all four were false-LIVEs: executed, the
    # assert was swallowed, and the analyzer reported it `enforced`.
    #
    # The shared truth is that `exc[0]` is the raised exception *type*, i.e.
    # `<class 'AssertionError'>`: a class object. A class object is truthy, is
    # not `None`, is not `False`, and is a member of a tuple holding exactly
    # `AssertionError`.
    (
        "338: is-not-None swallows every exception",
        "def __exit__(self, *exc):\n    return exc[0] is not None",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    (
        "338: bool of the exception type swallows every exception",
        "def __exit__(self, *exc):\n    return bool(exc[0])",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    (
        "338: is-not-False swallows every exception",
        "def __exit__(self, *exc):\n    return exc[0] is not False",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    (
        "338: membership in a one-element AssertionError tuple swallows",
        "def __exit__(self, *exc):\n    return exc[0] in (AssertionError,)",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    # The same `is not <literal>` truth reached through literals other than the
    # two filed. Identity against a literal can never hold for a class object,
    # so these are decided by one rule rather than by a `None`/`False` list --
    # and `()` is here because the empty tuple parses as a `Tuple`, not a
    # `Constant`, which is the branch a naive `isinstance(Constant)` misses.
    (
        "338: is-not-int-literal swallows",
        "def __exit__(self, *exc):\n    return exc[0] is not 0",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    (
        "338: is-not-empty-tuple swallows",
        "def __exit__(self, *exc):\n    return exc[0] is not ()",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    # --- #338 loud controls: near misses that must stay enforced ---
    # The negated membership. `exc[0] not in (AssertionError,)` is false when
    # an assert *was* swallowed, so this `__exit__` propagates and the assert
    # stays live. Reading it as a swallow is the damaging direction, and this
    # row is what makes the `not in` guard non-vacuous.
    (
        "338: negated membership stays enforced",
        "def __exit__(self, *exc):\n    return exc[0] not in (AssertionError,)",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    # Membership against a tuple that does not name the builtin.
    (
        "338: membership in a foreign tuple stays enforced",
        "def __exit__(self, *exc):\n    return exc[0] in (ValueError,)",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    # Both members are genuine builtin types; the received AssertionError is
    # a member by identity. Execution, rather than a conservative label, pins
    # this row as swallowed.
    (
        "338: multi-element builtin tuple swallows by identity",
        "def __exit__(self, *exc):\n    return exc[0] in (AssertionError, ValueError)",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    # The subscript is load-bearing for the membership spelling. Under `*exc` a
    # bare `exc` is the whole argument tuple, so `exc in (AssertionError,)` is
    # false at runtime -- the opposite of `exc[0] in (AssertionError,)`. A
    # generalized helper that accepted the bare name would report this row
    # swallowed when it propagates, so the row pins the difference.
    (
        "338: bare exception tuple is not the raised type",
        "def __exit__(self, *exc):\n    return exc in (AssertionError,)",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    # The bare name under the `is not` spelling is still a swallow -- a tuple
    # is likewise not `None` -- so the two directions are deliberately
    # asymmetric and this row pins that asymmetry from the other side.
    (
        "338: bare exception tuple is-not-None still swallows",
        "def __exit__(self, *exc):\n    return exc is not None",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        False,
    ),
    # --- loud controls: an __exit__ that does not swallow ---
    (
        "returns False",
        "def __exit__(self, *exc):\n    return False",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    (
        "returns None",
        "def __exit__(self, *exc):\n    return None",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    (
        "no return at all",
        "def __exit__(self, *exc):\n    pass",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    (
        "tests for a different exception",
        "def __exit__(self, *exc):\n    return exc[0] is ValueError",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    # The `issubclass` form tested against a *different* exception. The only
    # `issubclass` row above spells the operand `AssertionError`, so dropping
    # that operand check left the whole suite green while the rule started
    # reporting this live assert as defeated.
    (
        "issubclass against a different exception",
        "def __exit__(self, *exc):\n    return issubclass(exc[0], ValueError)",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    # The negated test. `is not AssertionError` is false when an assert was
    # swallowed, so this `__exit__` propagates and the assert stays live --
    # reading it as a swallow would be the damaging direction. This row is what
    # makes the operator guard non-vacuous: removing it lets this through.
    (
        "negated test",
        "def __exit__(self, *exc):\n    return exc[0] is not AssertionError",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    (
        "raises instead of returning",
        "def __exit__(self, *exc):\n    raise RuntimeError",
        "    helper = Suppressor()\n    with helper:\n        assert x != 1",
        True,
    ),
    # A `for` target binds the name just as an assignment does, and the class
    # is reached the same way -- through the value the loop target holds. The
    # spelling below is the reported false LIVE: executed, `Suppressor().__exit__`
    # returns True for the `AssertionError`, so the assert is swallowed, yet the
    # header was reported `enforced` -- a disarmed contract certified as
    # load-bearing. `_assigned_value` only ever followed `ast.Assign`, so a
    # loop target left the name holding nothing to resolve against.
    (
        "constructed as a single-element loop target",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        ("    for helper in [Suppressor()]:\n        with helper:\n            assert x != 1"),
        False,
    ),
    # The loop arm must not over-reach. #385 measured that guessing an element
    # out of a multi-element literal moves a damaging cell rather than removing
    # one, so only the single-element case is decided and this stays `enforced`:
    # which of the two instances the header sees is not statically knowable.
    (
        "multi-element loop target stays undecidable",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        (
            "    for helper in [Suppressor(), Suppressor()]:\n        with helper:\n"
            "            assert x != 1"
        ),
        True,
    ),
    # A non-literal iterable is the same undecidable case, and the direction
    # matters: an unreadable name leaves the contract `enforced`, which is the
    # safe answer here. Reading a suppressor that is not the one in force would
    # drop a live assert instead.
    (
        "non-literal loop target stays undecidable",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        ("    for helper in make_helpers():\n        with helper:\n            assert x != 1"),
        True,
    ),
    (
        "inline construction in the with header",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        "    with Suppressor():\n        assert x != 1",
        False,
    ),
    (
        "inline construction, named exception parameter",
        "def __exit__(self, exc_type, exc, tb):\n    return exc_type is AssertionError",
        "    with Suppressor():\n        assert x != 1",
        False,
    ),
    (
        "inline construction, unconditional return True",
        "def __exit__(self, *exc):\n    return True",
        "    with Suppressor():\n        assert x != 1",
        False,
    ),
    (
        "inline construction, returns False",
        "def __exit__(self, *exc):\n    return False",
        "    with Suppressor():\n        assert x != 1",
        True,
    ),
    (
        "inline construction, no return at all",
        "def __exit__(self, *exc):\n    pass",
        "    with Suppressor():\n        assert x != 1",
        True,
    ),
    (
        "inline construction, tests for a different exception",
        "def __exit__(self, *exc):\n    return exc[0] is ValueError",
        "    with Suppressor():\n        assert x != 1",
        True,
    ),
    (
        "inline factory call is not a constructor",
        "def __exit__(self, *exc):\n    return exc[0] is AssertionError",
        "    with factory():\n        assert x != 1",
        True,
    ),
)


def test_a_class_defined_inside_a_function_is_still_read():
    """#410: the class's scope is not what makes it a suppressor.

    #316 recognises a user-defined manager by what its ``__exit__`` returns,
    and the class was only ever collected from module level. A class defined
    inside the function that uses it behaves identically -- executed, it eats
    the ``AssertionError`` and the assert never fires -- so restricting the
    collection to module level reported a swallowed assert as ``enforced``:
    a disarmed contract certified as load-bearing.

    ``assert x == 99`` with ``x=2`` is deliberate. An ``assert x != 1`` would be
    vacuous here -- ``2 != 1`` is true, so it passes outright and nothing is
    ever swallowed, which is what made an earlier version of this probe agree
    with the checker for the wrong reason.

    The loud rows are the point in the other direction: a function-local manager
    whose ``__exit__`` propagates must stay ``enforced``, so widening the
    collection cannot report a live contract as defeated. Each row is executed
    first, so no row can pass because nothing was swallowed.
    """
    shapes = [
        (
            "swallowing exit, assigned",
            (
                "    class Suppressor:\n"
                "        def __enter__(self):\n"
                "            return self\n"
                "        def __exit__(self, *exc):\n"
                "            return exc[0] is AssertionError\n"
            ),
            "    helper = Suppressor()\n",
            False,
        ),
        (
            "swallowing exit, loop target",
            (
                "    class Suppressor:\n"
                "        def __enter__(self):\n"
                "            return self\n"
                "        def __exit__(self, *exc):\n"
                "            return exc[0] is AssertionError\n"
            ),
            "    for helper in [Suppressor()]:\n        pass\n",
            False,
        ),
        (
            "propagating exit stays live",
            (
                "    class Ctx:\n"
                "        def __enter__(self):\n"
                "            return self\n"
                "        def __exit__(self, *exc):\n"
                "            return False\n"
            ),
            "    helper = Ctx()\n",
            True,
        ),
        (
            "swallows a different exception stays live",
            (
                "    class Ctx:\n"
                "        def __enter__(self):\n"
                "            return self\n"
                "        def __exit__(self, *exc):\n"
                "            return exc[0] is ValueError\n"
            ),
            "    helper = Ctx()\n",
            True,
        ),
        (
            "no __exit__ stays live",
            ("    class Ctx:\n        def __enter__(self):\n            return self\n"),
            "    helper = Ctx()\n",
            True,
        ),
    ]
    for label, class_body, binding, expected in shapes:
        source = (
            "def outer(x):\n" + class_body + binding + "    with helper:\n        assert x == 99\n"
        )
        tree = ast.parse(source)
        outer = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
        )
        target = next(node for node in ast.walk(outer) if isinstance(node, ast.Assert))

        scope: dict = {}
        # Executed through `eval` on a compiled module so the row cannot pass
        # vacuously. `exec` is spelled this way because the S102 rule bans the
        # builtin; the behaviour is identical.
        exec(  # noqa: S102 - executing our own fixture is the point
            compile(source, "<outer>", "exec"), scope
        )
        try:
            scope["outer"](2)
            executed_live = False
        except AssertionError:
            executed_live = True
        except TypeError:
            # A class with no `__exit__` is not a context manager at all: the
            # header raises before the body runs, so the assert is never
            # evaluated. Still not a swallowed assert, which is all this row
            # needs the execution to establish.
            executed_live = True
        assert executed_live == expected, (
            f"{label}: CPython disagrees with this row -- the assert "
            f"{'fired' if executed_live else 'was swallowed'}"
        )

        results = [_is_enforced(outer, target, tree)]
        assert results == [expected], (
            f"{label}: expected the assert to be "
            f"{'enforced' if expected else 'unenforced'}, got {results}"
        )


@pytest.mark.parametrize(
    ("label", "exit_body", "body", "enforced"),
    USER_EXIT_SWALLOW_SHAPES,
    ids=[row[0] for row in USER_EXIT_SWALLOW_SHAPES],
)
def test_a_user_exit_that_swallows_assertion_error_is_a_defeat(label, exit_body, body, enforced):
    """#316: the ``__exit__`` return value is the mechanism, not the name.

    The named suppressors are known by what they are called. A user-defined
    manager is knowable only by what its ``__exit__`` *does*, and a return that
    is truthy precisely for ``AssertionError`` swallows the failure silently.

    Both halves of that are pinned here. The swallowing rows must be reported
    defeated, and the loud rows -- ``False``, ``None``, no return, a different
    exception, a raise -- must stay enforced, because an ``__exit__`` that does
    not swallow leaves a real contract in place and reporting it defeated
    would drop a live assert.
    """
    # Every line of `exit_body` needs the class-body indent, not just the first:
    # the table stores the `def` line and its body unindented so each row reads
    # as the method alone.
    indented_exit = "\n".join("    " + line for line in exit_body.splitlines())
    source = (
        "class Suppressor:\n"
        "    def __enter__(self):\n"
        "        return self\n" + indented_exit + "\ndef outer(x, flag):\n" + body + "\n"
    )
    tree = ast.parse(source)
    outer = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(outer) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(outer, node, tree) for node in asserts]
    assert results == [enforced], (
        f"{label}: expected the assert to be "
        f"{'enforced' if enforced else 'unenforced'}, got {results}"
    )


@pytest.mark.parametrize(
    "scenario",
    (
        "unrelated function class",
        "nearest local class",
        "last local definition propagates",
        "last local definition swallows",
        "existing swallowing instance",
        "existing propagating instance",
        "future loop store",
        "unrelated nested store",
        "nested nearest class",
        "module instance before redefinition",
        "module instance before local definition",
    ),
)
def test_user_exit_class_resolution_respects_scope_and_construction_position(scenario):
    def manager(exit_value, indent=""):
        lines = (
            "class Manager:",
            "    def __enter__(self): return self",
            f"    def __exit__(self, *exc): return {exit_value}",
        )
        return "".join(indent + line + "\n" for line in lines)

    source = "import contextlib\n"
    function_name = "outer"
    live = True
    if scenario == "module instance before local definition":
        source += manager(False) + "cs = Manager()\n"
        source += "def outer(x):\n    with cs:\n        assert x != 1\n"
        source += manager(True, "    ")
    elif scenario == "module instance before redefinition":
        source += manager(True) + "cs = Manager()\n" + manager(False)
        source += "def outer(x):\n    with cs:\n        assert x != 1\n"
        live = False
    elif scenario == "nested nearest class":
        source += "def outer(x):\n" + manager(True, "    ")
        source += "    def inner(x):\n" + manager(False, "        ")
        source += "        cs = Manager()\n        with cs:\n            assert x != 1\n"
        source += "    inner(x)\n"
        function_name = "inner"
    else:
        if scenario == "unrelated function class":
            source += "def unused():\n" + manager(True, "    ")
        elif scenario in ("nearest local class", "future loop store", "unrelated nested store"):
            source += manager(True)
        source += "def outer(x):\n"
        if scenario in ("unrelated function class", "nearest local class"):
            source += manager(False, "    ") + "    cs = Manager()\n"
        elif scenario.startswith("last local definition"):
            live = scenario.endswith("propagates")
            source += manager(live, "    ") + manager(not live, "    ")
            source += "    cs = Manager()\n"
        elif scenario.startswith("existing"):
            live = scenario == "existing propagating instance"
            source += manager(not live, "    ") + "    cs = Manager()\n"
            source += manager(live, "    ")
        else:
            if scenario == "unrelated nested store":
                source += "    def unused():\n        cs = Manager()\n"
            source += "    cs = contextlib.nullcontext()\n"
        source += "    with cs:\n        assert x != 1\n"
        if scenario == "future loop store":
            source += "    for cs in (Manager(),):\n        pass\n"
    namespace = {}
    exec(compile(source, f"<{scenario}>", "exec"), namespace)  # noqa: S102
    if live:
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    else:
        namespace["outer"](1)
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, assertion, tree) is live, scenario


@pytest.mark.parametrize("parameter_bound", (False, True))
def test_a_future_local_class_definition_cannot_supply_the_current_constructor(parameter_bound):
    arguments = "x, Manager" if parameter_bound else "x"
    source = (
        f"def outer({arguments}):\n"
        "    cs = Manager()\n"
        "    with cs:\n"
        "        assert x != 1\n"
        "    class Manager:\n"
        "        def __enter__(self): return self\n"
        "        def __exit__(self, *exc): return True\n"
    )
    namespace = {}
    exec(compile(source, "<future local class>", "exec"), namespace)  # noqa: S102
    if parameter_bound:
        import contextlib

        with pytest.raises(AssertionError):
            namespace["outer"](1, contextlib.nullcontext)
    else:
        with pytest.raises(UnboundLocalError):
            namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[0]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, assertion, tree) is parameter_bound


@pytest.mark.parametrize(
    ("ordering", "outcome"),
    (
        ("definition before invocation", "swallowed"),
        ("definition after invocation", "unbound"),
        ("redefinition before invocation", "live"),
        ("redefinition after invocation", "swallowed"),
    ),
)
def test_a_captured_class_is_resolved_at_the_nested_function_invocation(ordering, outcome):
    def manager(exit_value):
        return (
            "    class Manager:\n"
            "        def __enter__(self): return self\n"
            f"        def __exit__(self, *exc): return {exit_value}\n"
        )

    source = (
        "def outer(x):\n"
        "    def inner(x):\n"
        "        cs = Manager()\n"
        "        with cs:\n"
        "            assert x != 1\n"
    )
    if ordering != "definition after invocation":
        source += manager(True)
    if ordering == "redefinition before invocation":
        source += manager(False)
    source += "    inner(x)\n"
    if ordering == "definition after invocation":
        source += manager(True)
    elif ordering == "redefinition after invocation":
        source += manager(False)
    namespace = {}
    exec(compile(source, f"<{ordering}>", "exec"), namespace)  # noqa: S102
    if outcome == "live":
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    elif outcome == "unbound":
        with pytest.raises(NameError):
            namespace["outer"](1)
    else:
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[0].body[0]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, assertion, tree) is (outcome == "live")


def test_the_user_exit_rule_does_not_fire_on_an_ordinary_context_manager():
    """#316 criterion 2: no over-breadth on the real pinned file.

    A rule that reported every class with an ``__exit__`` as a defeat would
    pass every row above and still be worthless, because ``pytest``'s and the
    harness's own managers all have one. The budget is stated as a number: the
    real file must measure the same asserts walked and the same zero unenforced
    as it does on master. Measured on both trees, not asserted from memory.
    """
    tree = support.milestones_tree()
    walked = 0
    unenforced = 0
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Assert):
                continue
            walked += 1
            if not _is_enforced(function, node, tree):
                unenforced += 1
    assert unenforced == 0, (
        f"the #316 rule reported {unenforced} of {walked} real pinned asserts "
        f"as defeated; it must not fire on an ordinary context manager"
    )
    assert walked == 143, (
        f"the pinned file now walks {walked} asserts, expected 143 -- either "
        f"the file changed or the walk lost sites"
    )


def test_an_unreadable_constructor_is_not_assumed_to_suppress():
    """A factory or a parameter is not followed, so nothing is invented.

    Following one hop from ``make()`` to whatever it returns would mean
    assuming that an arbitrary call produces a swallowing context manager,
    which is the over-breadth #316's criterion 2 rules out. The class below
    *is* a suppressor -- only the binding is unreadable -- so this row
    separates "cannot see it" from "it is not one".
    """
    source = (
        "class Suppressor:\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *exc):\n"
        "        return exc[0] is AssertionError\n"
        "def make():\n"
        "    return Suppressor()\n"
        "def outer(x):\n"
        "    with make():\n"
        "        assert x != 1\n"
    )
    tree = ast.parse(source)
    outer = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(outer) if isinstance(node, ast.Assert))
    assert _is_enforced(outer, target, tree), (
        "an unreadable constructor was assumed to produce a swallowing context "
        "manager; the rule must decline rather than assume"
    )
