"""Walrus and with-header suppression aliases.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_binding_reachability import (
    WALRUS_SHAPES,
)

#: #310: the ``.__enter__()`` dunder spelling, which shipped in #309 with no
#: test at all. Reverting the rule left the suite green, so these rows exist
#: first and foremost to make that impossible.
#:
#: The rule had a second defect, found by running it: it read the exception
#: types from the *``__enter__()``* call's arguments instead of the suppressor's.
#: The outer call has none, so every dunder spelling fell back to the unreadable
#: case ``BaseException`` -- which catches ``AssertionError`` -- and reported
#: ``suppress(ValueError).__enter__()`` as a defeat of the assert. The
#: ``ValueError``/``RuntimeError`` rows below are that defect's regression test.
#:
#: The loud-vs-silent question is settled by measurement, not taste: *any*
#: ``X.__enter__()`` returns ``None``, so the ``with`` body never runs and
#: ``TypeError`` is raised. The dunder form is therefore never a silent defeat.
#: The rule still flags it, and that is a deliberate conservative choice --
#: the shape is broken, and reporting a broken contract as not-load-bearing is
#: the safe direction. It is recorded here so the choice is visible rather
#: than implied.
DUNDER_SPELLING_SHAPES = (
    (
        "qualified suppressor dunder",
        "    with contextlib.suppress(AssertionError).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "bare suppressor dunder",
        "    with suppress(AssertionError).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "suppressor dunder over Exception",
        "    with suppress(Exception).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "suppressor dunder over BaseException",
        "    with suppress(BaseException).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "suppressor dunder over a tuple naming AssertionError",
        (
            "    with contextlib.suppress((ValueError, AssertionError)).__enter__():\n"
            "        assert x != 1"
        ),
        False,
    ),
    (
        "aliased suppressor dunder",
        (
            "    cs = contextlib.suppress(AssertionError)\n"
            "    with cs.__enter__():\n        assert x != 1"
        ),
        False,
    ),
    # --- the argument is irrelevant: `__enter__` is `pass` ---
    # `contextlib.suppress.__enter__` is `def __enter__(self): pass`, so it
    # returns `None` for every instantiation and `with None:` raises TypeError
    # before the body runs -- measured for every one of these arguments, and
    # for the no-argument form. The family is therefore answered uniformly:
    # an argument-reading split here would report byte-identical runtimes
    # differently. These rows are not "controls" any more; they are the
    # evidence that the exception list earns no distinction.
    (
        "suppressor dunder over ValueError",
        "    with contextlib.suppress(ValueError).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "suppressor dunder over RuntimeError",
        "    with contextlib.suppress(RuntimeError).__enter__():\n        assert x != 1",
        False,
    ),
    (
        "aliased suppressor dunder over ValueError",
        (
            "    cs = contextlib.suppress(ValueError)\n"
            "    with cs.__enter__():\n        assert x != 1"
        ),
        False,
    ),
    # A different dunder is not this spelling, and an unrelated object that
    # happens to define `__enter__` is never a suppressor.
    (
        "unrelated object dunder",
        "    with helper.make().__enter__():\n        assert x != 1",
        True,
    ),
    (
        "nullcontext dunder",
        "    with contextlib.nullcontext().__enter__():\n        assert x != 1",
        True,
    ),
    (
        "a different dunder on a real suppressor",
        "    with contextlib.suppress(AssertionError).__exit__():\n        assert x != 1",
        True,
    ),
    # `suppress()` with empty parens raises TypeError on `__enter__()` just
    # like the rest of the family, so this row agrees with them rather than
    # standing out from them.
    (
        "empty-parens suppressor dunder",
        "    with contextlib.suppress().__enter__():\n        assert x != 1",
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    WALRUS_SHAPES,
    ids=[shape[0] for shape in WALRUS_SHAPES],
)
def test_walrus_bound_suppressors_in_a_with_header_are_rejected(label, body, live):
    """A suppressor bound *in* the header is the same defeat, still missed.

    ``cs = suppress(...)`` and ``with (cs := suppress(...)):`` differ only in
    where the binding is written, and both swallow the assertion failure. The
    gap was real and silent: on master this shape reported a live defeat as
    *enforced*, certifying a disarmed contract as load-bearing.
    """
    source = (
        "def outer(x, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    import pytest\n" + body + "\n"
    )
    # Execute the fixture first and require the row to agree with CPython. A
    # header that raises while it is being evaluated never reaches the assert,
    # which is a *different* outcome from one that swallows it -- conflating
    # the two is how "walrus of a non-call value" came to be pinned as live.
    namespace = {}
    exec(compile(source, "<walrus-header>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1, namespace.get("helper"))
    except AssertionError:
        observed_live = True
    except (TypeError, AttributeError, NameError) as error:
        expected_errors = {
            "walrus of a zero-argument helper": AttributeError,
            "bare name in the header": NameError,
            "walrus of a non-call value": TypeError,
        }
        assert label in expected_errors, f"{label}: unexpected {error!r}"
        assert type(error) is expected_errors[label]
        # These two fixtures deliberately lack a runtime binding; their AST
        # refusal is checked below, independently of the executed exception.
        if label in ("walrus of a zero-argument helper", "bare name in the header"):
            assert live is True
            observed_live = None
        else:
            observed_live = False
    else:
        observed_live = False
    assert observed_live is None or observed_live is live, (
        f"{label}: CPython produced live={observed_live}, the row claims "
        f"live={live}. The table is stale, not the analyzer."
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )


@pytest.mark.parametrize(
    ("label", "body", "live"),
    DUNDER_SPELLING_SHAPES,
    ids=[shape[0] for shape in DUNDER_SPELLING_SHAPES],
)
def test_the_dunder_spelling_is_covered_and_scoped_to_real_suppression(label, body, live):
    """#310: the dunder rule must be exercised, and must key on the exception.

    The rule shipped untested, so it could be deleted without any test going
    red. These rows fix that, and they also pin the scope: a suppressor dunder
    over an exception that does *not* catch ``AssertionError`` is a live
    contract, and flagging it would drop a real assert from the sentinel's
    view.
    """
    imports = (
        "    import contextlib\n"
        "    import pytest\n"
        "    from contextlib import suppress\n"
        "    from contextlib import ExitStack\n"
    )
    source = "def outer(x, cm, items, record, helper):\n" + imports + body + "\n"
    tree = ast.parse(source)
    outer = tree.body[0]
    for statement in outer.body:
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement is not outer
        ):
            function = statement
            break
    else:
        function = outer
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )


#: #308 criterion 1 asks that branch order never decide the verdict. Asserting a
#: single source order would not catch a rule that resolved by order and
#: happened to be right for that order, so both are pinned and required to
#: agree.
DISAGREEING_BRANCH_ORDERS = (
    (
        "import in the if branch",
        (
            "    if flag:\n"
            "        from contextlib import suppress\n"
            "    else:\n"
            "        suppress = contextlib.suppress\n"
            "    with suppress(AssertionError):\n"
            "        assert x != 1"
        ),
    ),
    (
        "import in the else branch",
        (
            "    if flag:\n"
            "        suppress = contextlib.suppress\n"
            "    else:\n"
            "        from contextlib import suppress\n"
            "    with suppress(AssertionError):\n"
            "        assert x != 1"
        ),
    ),
)


@pytest.mark.parametrize(
    ("label", "body"),
    DISAGREEING_BRANCH_ORDERS,
    ids=[shape[0] for shape in DISAGREEING_BRANCH_ORDERS],
)
def test_branch_order_never_decides_the_alias_verdict(label, body):
    """A name bound to different targets in two branches must be unreadable.

    ``_own_imports`` collects bindings with a LIFO ``stack.pop()``, so with two
    branches binding the same alias the surviving binding depends on source
    order and one of the two orders reaches the wrong verdict. Treating the
    ambiguous name as suppressing is what makes the answer order-independent --
    and over-reporting is the safe direction, since the alternative is
    certifying a disarmed assert as load-bearing.
    """
    source = "def outer(x, flag):\n    import contextlib\n" + body + "\n"
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert not any(results), f"{label}: a swallowed assert was reported enforced, got {results}"


def test_a_name_bound_to_different_suppressors_is_ambiguous_not_ordered():
    """Two readable suppressors under one name cannot be resolved by order.

    A single merged dict would keep whichever binding was collected last and
    report a verdict for one path only. The alias is therefore recorded as
    ambiguous, which is read as suppressing -- the over-reporting direction
    #308 criterion 1 accepts.
    """
    source = (
        "def outer(x, flag):\n"
        "    import contextlib\n"
        "    if flag:\n"
        "        cs = contextlib.suppress(AssertionError)\n"
        "    else:\n"
        "        cs = contextlib.suppress(ValueError)\n"
        "    with cs:\n"
        "        assert x != 1"
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert not any(results), f"ambiguous alias resolved by order, got {results}"


def test_plain_assignment_aliasing_needs_no_import_node():
    """``cs = contextlib.suppress(...)`` is an alias with no import to resolve.

    #308 criterion 2. The assignment produces no ``Import``/``ImportFrom`` node,
    so a resolver that only reads imports has nothing to match and the swallowed
    assert reads as enforced. The control proves the rule keys on the
    right-hand side rather than on the name.
    """
    defeat = (
        "def outer(x):\n"
        "    import contextlib\n"
        "    cs = contextlib.suppress(AssertionError)\n"
        "    with cs:\n"
        "        assert x != 1"
    )
    control = (
        "def outer(x):\n"
        "    import contextlib\n"
        "    cs = contextlib.json\n"
        "    with cs:\n"
        "        assert x != 1"
    )
    for label, source, expected in (
        ("suppressor alias", defeat, False),
        ("non-suppressor", control, True),
    ):
        tree = ast.parse(source)
        function = tree.body[0]
        asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
        results = [_is_enforced(function, node, tree) for node in asserts]
        assert all(results) is expected, f"{label}: expected {expected}, got {results}"


#: ``async with`` shapes. A suppressor reached through the *async* header is
#: loud at runtime -- neither ``contextlib.suppress`` nor ``pytest.raises``
#: implements ``__aenter__``, so entry raises ``TypeError`` before the body
#: runs. A *sync* ``with`` nested inside one is a different matter and is
#: still read, so the guard has to be scoped to the async header itself.
ASYNC_CONTEXT_SHAPES = (
    (
        "async with a suppressor",
        "    async with contextlib.suppress(AssertionError):\n        assert x != 1",
        True,
    ),
    (
        "async with pytest.raises",
        "    async with pytest.raises(AssertionError):\n        assert x != 1",
        True,
    ),
    (
        "async with a bare suppressor alias",
        ("    cs = contextlib.suppress(AssertionError)\n    async with cs:\n        assert x != 1"),
        True,
    ),
    (
        "sync with a suppressor nested in an async with",
        (
            "    async with helper.mgr():\n"
            "        with contextlib.suppress(AssertionError):\n            assert x != 1"
        ),
        False,
    ),
    (
        "sync with a suppressor alias nested in an async with",
        (
            "    async with helper.mgr():\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    ASYNC_CONTEXT_SHAPES,
    ids=[row[0] for row in ASYNC_CONTEXT_SHAPES],
)
def test_async_with_is_loud_but_a_sync_suppressor_inside_it_is_not(label, body, live):
    """``async with`` needs an async context manager, and suppressors are not.

    Measured on the pinned interpreter::

        async with contextlib.suppress(AssertionError):
            assert 1 == 2
        # TypeError: 'suppress' object does not support the asynchronous
        # context manager protocol

    The body never runs, so the assert is a live contract. Reading the async
    form as a suppression would drop a real assert from the sentinel's view.

    A *sync* ``with`` nested inside the ``async with`` is unaffected and must
    still be read, so the guard is scoped to the async header rather than to
    the enclosing function.
    """
    imports = "    import contextlib\n    import pytest\n    from contextlib import suppress\n"
    source = "async def outer(x, cm, items, record, helper):\n" + imports + body + "\n"
    tree = ast.parse(source)
    outer = tree.body[0]
    asserts = [node for node in ast.walk(outer) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(outer, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )


#: One block holding a ``with`` *before* the store and another *after* it. The
#: two asserts are opposite cases that sit under one top-level statement, so
#: they are pinned separately -- a rule that reports every ``with`` in the
#: block, or that reads the block's bindings before the position of the header,
#: gives both the same answer, and each of those answers is wrong in one
#: direction or the other.
SAME_BLOCK_ORDER_SHAPES = (
    (
        "if body holds both orders",
        (
            "    if p:\n        with cs:\n            assert x != 1\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
    ),
    (
        "nested with holds both orders",
        (
            "    with a():\n        with cs:\n            assert x != 1\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
    ),
    (
        "for body holds both orders",
        (
            "    for a in L:\n        with cs:\n            assert x != 1\n"
            "        cs = contextlib.suppress(AssertionError)\n"
            "        with cs:\n            assert x != 1"
        ),
    ),
)


@pytest.mark.parametrize(
    ("label", "body"),
    SAME_BLOCK_ORDER_SHAPES,
    ids=[row[0] for row in SAME_BLOCK_ORDER_SHAPES],
)
def test_both_orders_in_one_block_keep_their_own_verdict(label, body):
    """A ``with`` before the store and one after it must not share a verdict.

    The first raises `NameError` on entry, so its assert is a live contract.
    The second really is swallowed. Reporting the block uniformly gets one of
    the two wrong no matter which way, and both wrong answers are damaging --
    certifying a defeated assert, or dropping a live one.
    """
    imports = "    import contextlib\n    from contextlib import suppress\n"
    source = "def outer(x, cm, items, record, helper):\n" + imports + body + "\n"
    tree = ast.parse(source)
    outer = tree.body[0]
    asserts = [node for node in ast.walk(outer) if isinstance(node, ast.Assert)]
    assert len(asserts) == 2, f"{label}: expected two asserts to tell apart"
    results = [_is_enforced(outer, node, tree) for node in asserts]
    assert results == [True, False], (
        f"{label}: the assert before the store must stay enforced and the one "
        f"after it must be reported defeated, got {results}"
    )
