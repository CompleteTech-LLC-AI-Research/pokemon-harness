"""Reachability shape fixtures and direct suppression cases.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced

#: Every spelling that disarms an assert without removing it, and the matching
#: live control that must stay enforced. The controls are not padding: a rule
#: that flags everything is indistinguishable from a rule that works, and this
#: repo has already recorded three probes that reported a confident "caught"
#: verdict because the mutation never applied.
UNREACHABLE_SHAPES = (
    # `except*` parses to ast.TryStar, which is not a subclass of ast.Try.
    # Matching only the latter left it an unguarded spelling of the defeat.
    (
        "try star",
        "    try:\n        assert x != 1\n    except* AssertionError:\n        pass",
        False,
    ),
    ("bare except", "    try:\n        assert x != 1\n    except:\n        pass", False),
    (
        "except Exception",
        "    try:\n        assert x != 1\n    except Exception:\n        pass",
        False,
    ),
    # A swallowing `try` around a *sibling* does not disarm an assert outside
    # it; treating it as though it did would drop real pinned sites.
    (
        "try on sibling",
        "    try:\n        helper()\n    except AssertionError:\n        pass\n    assert x != 1",
        True,
    ),
    # The suppression family. Matched by resolved name, so all four spellings
    # collapse to the same value before the comparison.
    (
        "suppress qualified",
        "    with contextlib.suppress(AssertionError):\n        assert x != 1",
        False,
    ),
    (
        "suppress from import",
        "    with suppress(AssertionError):\n        assert x != 1",
        False,
    ),
    (
        "suppress aliased module",
        "    with c.suppress(AssertionError):\n        assert x != 1",
        False,
    ),
    (
        "suppress aliased name",
        "    with sq(AssertionError):\n        assert x != 1",
        False,
    ),
    # Cannot swallow a failing assert, so it must stay enforced.
    (
        "suppress other error",
        "    with contextlib.suppress(ValueError):\n        assert x != 1",
        True,
    ),
    (
        "suppress other error from import",
        "    with suppress(KeyError):\n        assert x != 1",
        True,
    ),
    # A context manager that is not a suppressor.
    (
        "unrelated context",
        "    with open('f') as fh:\n        assert x != 1",
        True,
    ),
    # Statically dead: the body never runs, so the assert is never evaluated.
    ("if False", "    if False:\n        assert x != 1", False),
    ("while False", "    while False:\n        assert x != 1", False),
    # The `else` of a falsy `if` is precisely the branch that *does* run.
    (
        "if False else",
        "    if False:\n        helper()\n    else:\n        assert x != 1",
        True,
    ),
    # A genuine runtime condition is not statically dead.
    ("runtime condition", "    if flag:\n        assert x != 1", True),
    # An assert moved into a nested def nothing calls never evaluates. A nested
    # def whose name is *loaded* is the callback shape and stays enforced.
    (
        "uncalled nested def",
        "    def inner():\n        assert x != 1",
        False,
    ),
    (
        "nested def used",
        "    def inner():\n        assert x != 1\n    return inner",
        True,
    ),
    # #400. A `return` / `raise` / `break` / `continue` that is unconditional
    # within its own block never falls through, so every statement after it is
    # dead -- while the assert stays lexically present, so a presence-based
    # check certifies a contract that can no longer fail. Each of the four
    # transfer kinds is its own spelling of the same defeat.
    ("after return", "    return\n    assert x != 1", False),
    ("after return value", "    return x\n    assert x != 1", False),
    ("after raise", "    raise ValueError\n    assert x != 1", False),
    (
        "after continue",
        "    for _ in [1]:\n        continue\n        assert x != 1",
        False,
    ),
    (
        "after break",
        "    for _ in [1]:\n        break\n        assert x != 1",
        False,
    ),
    # The transfer is not a *statement* in the dead block but kills it anyway:
    # the interpreter never begins evaluating the `with` / `try` / `if` / `for`
    # that follows, so the asserts nested under their bodies never run either.
    # A rule that only looked at the top-level siblings would miss all of these.
    (
        "return then with body",
        "    return\n    with helper():\n        assert x != 1",
        False,
    ),
    (
        "return then try finally body",
        "    return\n    try:\n        pass\n    finally:\n        assert x != 1",
        False,
    ),
    (
        "return then both if branches",
        "    return\n    if flag:\n        assert x != 1\n    else:\n        assert x != 2",
        False,
    ),
    (
        "return then match case",
        "    return\n    match x:\n        case 1:\n            assert x != 1",
        False,
    ),
    (
        "return then for body",
        "    return\n    for _ in [1]:\n        assert x != 1",
        False,
    ),
    # Deeply nested under a dead compound statement: the reachability of the
    # assert follows the *enclosing* block, not just its immediate parent.
    (
        "return then nested in while try",
        (
            "    return\n    for _ in [1]:\n        while True:\n"
            "            try:\n                assert x != 1\n"
            "            except AssertionError:\n                pass"
        ),
        False,
    ),
    # A class body after a `return` is never executed either, so the assert it
    # holds is just as dead as one under a plain `if`.
    (
        "return then class body",
        "    return\n    class Inner:\n        assert x != 1",
        False,
    ),
    # #400, the same defeat *inside* a handler block rather than before one.
    # A `finally` and an `else` are separate statement lists, so a rule that
    # only ever scans a `body` misses the transfer that sits in the sibling
    # list directly before the assert.
    (
        "return in finally then assert",
        "    try:\n        pass\n    finally:\n        return\n        assert x != 1",
        False,
    ),
    (
        "return in else then assert",
        "    if flag:\n        pass\n    else:\n        return\n        assert x != 1",
        False,
    ),
    # The live controls for #400. A transfer nested in an earlier statement's
    # own body is conditional with respect to the enclosing block, so it says
    # nothing about what follows -- and `break` / `continue` bind to the
    # nearest loop, not to the function.
    (
        "guarded continue then assert",
        ("    for _ in [1]:\n        if flag:\n            continue\n        assert x != 1"),
        True,
    ),
    (
        "guarded break then assert",
        "    for _ in [1]:\n        if flag:\n            break\n        assert x != 1",
        True,
    ),
    (
        "loop with break then assert",
        "    for _ in [1]:\n        if flag:\n            break\n    assert x != 1",
        True,
    ),
    ("guarded return then assert", "    if flag:\n        return\n    assert x != 1", True),
    (
        "guarded raise then assert",
        "    if flag:\n        raise ValueError\n    assert x != 1",
        True,
    ),
    # #402. This row was a *live control* for #400 and pinned `True`, on the
    # reading that a transfer nested in an earlier statement's body says
    # nothing about what follows. That reading is right for `break` and
    # `continue`, which leave the *enclosing* loop and resume after it, and
    # wrong for a `try` whose body is a bare `return`: there is nowhere for
    # the `return` to resume, so the statements after the whole `try` are
    # never reached. Measured on CPython 3.12.14 the assert is never
    # evaluated, so the correct verdict is `False`, and the row moves from
    # the control block to the dead block above.
    (
        "return in try then assert",
        "    try:\n        return\n    except Exception:\n        pass\n    assert x != 1",
        False,
    ),
    # The `raise` counterpart is the live control that keeps this rule from
    # degenerating into "a `try` body that ends in a transfer is dead". A
    # `raise` in the `try` body is exactly what the handlers exist to catch,
    # so the no-exception path -- or the handler itself falling through --
    # resumes after the `try` and the assert is reached.
    (
        "raise in try then assert",
        "    try:\n        raise ValueError\n    except ValueError:\n        pass\n    assert x != 1",
        True,
    ),
    # The `else` clause runs on the no-exception path, which is the ordinary
    # one, so a `return` there leaves the same way a `return` in the `try`
    # body does. The `try` body and every handler both fall through here, so
    # the `else` is the only remaining path out.
    (
        "return in try else then assert",
        "    try:\n        pass\n    except Exception:\n        pass\n    else:\n        return\n    assert x != 1",
        False,
    ),
    # A `finally` that returns overrides every path out of the `try`, so the
    # statements after it are dead even though both the `try` body and the
    # handler fall through.
    (
        "return in try finally then assert",
        "    try:\n        pass\n    finally:\n        return\n    assert x != 1",
        False,
    ),
    # Two handlers where only one falls through: the falling handler is a real
    # path out of the `try`, so the assert is reached. This is what makes the
    # handler test `all` and not `any` -- an `any` reading would call this
    # dead and drop a live contract.
    (
        "one of two handlers falls through",
        (
            "    try:\n        pass\n"
            "    except ValueError:\n        return\n"
            "    except TypeError:\n        pass\n"
            "    assert x != 1"
        ),
        True,
    ),
    # A single handler that re-raises is also live: the re-raise only happens
    # when an exception occurred, so the no-exception path still reaches the
    # assert below. `raise` in a handler is not a fall-through.
    (
        "handler reraise then assert",
        "    try:\n        pass\n    except Exception:\n        raise\n    assert x != 1",
        True,
    ),
    # The mirror of the row above, and the one place a bare `raise` in a
    # handler *is* load-bearing: here the `try` body always raises, so there
    # is no ordinary path at all and the re-raising handler is the only exit.
    # The `_block_falls_through` guard is what separates the two rows -- with
    # it removed, the row above would be answered dead and this one right.
    (
        "raise in try reraise handler then assert",
        "    try:\n        raise ValueError\n    except ValueError:\n        raise\n    assert x != 1",
        False,
    ),
    # #414. A handler that *returns* is only the sole exit when the body
    # cannot fall through on its own. Here the body is `pass`, so it
    # completes normally and control reaches the assert; the handler never
    # runs at all. Deciding from the handlers alone answered `defeated` and
    # dropped a live contract, while the `raise` rows above are unaffected
    # because they already require a body that cannot fall through.
    #
    # This is the `return` counterpart of "handler reraise then assert":
    # same ordinary path, opposite reason for the handler not to matter.
    # The pair pins that the body guard applies to a returning handler too,
    # not only to a re-raising one.
    (
        "handler returns but body falls through then assert",
        "    try:\n        pass\n    except Exception:\n        return\n    assert x != 1",
        True,
    ),
    # The discriminator against the row above: make the BODY the returning
    # half as well and the `try` genuinely has no way out, so the assert is
    # dead. These two rows differ only in the body, which is exactly the term
    # the fix adds -- with the body guard removed, this row is still right
    # and the one above is wrong, so neither alone can pass by accident.
    (
        "body and handler both return then assert",
        "    try:\n        return\n    except Exception:\n        return\n    assert x != 1",
        False,
    ),
    # A `try/finally` whose `finally` merely falls through is decided by the
    # `try` body alone, and a body that may raise leaves the `finally` and
    # then the statement after it reachable. This is the live control for the
    # `finally` clause: without it, a rule that treated any `finally` as
    # terminal would drop this assert.
    (
        "try finally falls through then assert",
        "    try:\n        helper()\n    finally:\n        pass\n    assert x != 1",
        True,
    ),
    # `except*` is the same statement with a different handler type, and the
    # walk must not skip it by testing for `ast.Try` alone.
    (
        "return in try star then assert",
        "    try:\n        return\n    except* Exception:\n        pass\n    assert x != 1",
        False,
    ),
    # The transfer kills the statements after the `try` *statement*, so
    # anything nested under a later compound statement is dead too -- the
    # interpreter never begins evaluating it.
    (
        "return in try then with body",
        "    try:\n        return\n    except Exception:\n        pass\n    with helper():\n        assert x != 1",
        False,
    ),
    # A bare string expression is not a transfer, so it must not decide
    # whether a block ends in one. `_block_falls_through` skips a trailing
    # `Expr` constant and keeps looking, because a string literal is a
    # statement that can neither transfer nor fall out of the block in the way
    # a `return` or `raise` does.
    #
    # These three rows are the only things that exercise that skip, and the
    # first of them is load-bearing: with the skip disabled,
    # `_block_falls_through` answers from the trailing string instead of from
    # the `raise` beneath it, the re-raise clause stops firing, and this row
    # flips to `True` -- certifying as ENFORCED an assert that CPython never
    # evaluates. That is the blocking direction of the #308 criterion, so the
    # skip is a real rule and not decoration.
    (
        "trailing string after raise then reraise",
        (
            "    try:\n        raise ValueError\n        'dead'\n"
            "    except ValueError:\n        raise\n    assert x != 1"
        ),
        False,
    ),
    # The same shape with a body that *completes* rather than raises. Here the
    # block genuinely can fall out of its bottom -- `helper()` returns, the
    # string is evaluated, and control leaves after the string -- so the
    # re-raise handler is not the sole exit and the assert is reached. The
    # trailing string must not be mistaken for a transfer that stops it.
    (
        "trailing string after call then reraise",
        (
            "    try:\n        helper()\n        'tail'\n"
            "    except Exception:\n        raise\n    assert x != 1"
        ),
        True,
    ),
    # And the filed #402 shape carrying the same trailing string, which keeps
    # the `return` answer stable when the body is a bare transfer followed by
    # a non-transfer statement.
    (
        "return then trailing string in try",
        (
            "    try:\n        return\n        'dead'\n"
            "    except Exception:\n        pass\n    assert x != 1"
        ),
        False,
    ),
    ("plain live assert", "    assert x != 1", True),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    UNREACHABLE_SHAPES,
    ids=[shape[0] for shape in UNREACHABLE_SHAPES],
)
def test_reachability_rejects_exactly_the_shapes_that_cannot_fail(label, body, live):
    """An assert the interpreter can never fail is not an enforced contract.

    ``try/except``, ``with suppress(...)`` and a statically dead branch all keep
    the assert in the AST while making it incapable of failing, so a
    presence-based check certifies a contract that no longer exists (#287). The
    live controls carry equal weight: a wrong "defeated" verdict *removes* a
    real contract from the sentinel's view, which is the more damaging error.
    """
    # The body lines are already written with the indentation they need
    # relative to the function, so the header is the only line added here.
    # Indenting the whole body again is what produces the IndentationError this
    # repo has already mistaken for a caught mutation once.
    imports = (
        "    import contextlib\n"
        "    from contextlib import suppress\n"
        "    import contextlib as c\n"
        "    from contextlib import suppress as sq\n"
    )
    source = "def probe(x, flag, record, helper):\n" + imports + body + "\n"
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert all(results) is live, (
        f"{label}: expected every assert to be "
        f"{'enforced' if live else 'unenforced'}, got {results}"
    )


#: #400 also has to be *positional*: one function can hold a live assert and a
#: dead one, and answering uniformly either way certifies a real contract as
#: defeated or hides a defeated one. These rows are checked in source order, so
#: they catch a rule that gets the direction right on a whole function but the
#: cut point wrong within it.
MIXED_REACHABILITY_SHAPES = (
    (
        "live then return then dead",
        "    assert x != 1\n    return\n    assert x != 2",
        (True, False),
    ),
    (
        "live in with then return then dead",
        "    with helper():\n        assert x != 1\n    return\n    assert x != 2",
        (True, False),
    ),
    (
        "dead with body then live after return in branch",
        "    with helper():\n        return\n        assert x != 1\n    assert x != 2",
        (False, True),
    ),
    (
        "two live asserts around a helper call",
        "    assert x != 1\n    helper()\n    assert x != 2",
        (True, True),
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    MIXED_REACHABILITY_SHAPES,
    ids=[shape[0] for shape in MIXED_REACHABILITY_SHAPES],
)
def test_reachability_decides_each_assert_by_its_own_position(label, body, expected):
    """A live and a dead assert in one function must get opposite verdicts.

    The single-verdict rows above cannot express this: a rule that answers one
    way for the whole function would satisfy either all-True or all-False
    fixtures while being wrong about half the asserts in the mixed case. The
    rows are ordered by source position, so a rule that picks the wrong
    cut point is caught rather than averaged away.
    """
    source = "def probe(x, flag, record, helper):\n" + body + "\n"
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = sorted(
        (node for node in ast.walk(function) if isinstance(node, ast.Assert)),
        key=lambda node: node.lineno,
    )
    assert len(asserts) == len(expected), (
        f"{label}: fixture declared {len(asserts)} asserts, expected {len(expected)}"
    )
    results = tuple(_is_enforced(function, node, tree) for node in asserts)
    assert results == expected, f"{label}: expected {expected}, got {results}"
