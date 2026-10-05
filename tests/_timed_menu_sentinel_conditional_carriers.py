"""Conditional carrier and entry-scope cases.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced
from tests._timed_menu_sentinel_carrier_attributes import (
    FUNCTION_CARRIER_SUPERSESSION_SHAPES,
)
from tests._timed_menu_sentinel_import_context_managers import (
    _assert_entry_contract,
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    FUNCTION_CARRIER_SUPERSESSION_SHAPES,
    ids=[shape[0] for shape in FUNCTION_CARRIER_SUPERSESSION_SHAPES],
)
def test_a_conditional_function_store_after_a_carrier_is_declined(label, body, live):
    """A function-scope carrier a conditional store may have superseded.

    The carrier is written straight into the function body, so it is an
    *unconditional* store and `_stores_of` keeps it -- even when a later
    store nested in a block may already have replaced the name with an
    enterable value. Answering from the stale carrier reported `defeated` on
    an assert CPython evaluates, dropping a live pinned contract. #388.

    All three controls pin the boundaries of the decline, and none of them
    is optional:

    * An *unconditional* later store is settled -- the last binding wins and
      the assert is live, so a rule that declined every store after a carrier
      would fail here.
    * A conditional store *before* the carrier is genuinely superseded by it.
      The name is a module again, so ``defeated`` is the correct answer and no
      decline is warranted.
    * ``except ... as cs:`` *unbinds* rather than supersedes -- CPython deletes
      the name when the handler exits, so the carrier is what remains in
      force. Counting it as a superseding store would flip a correct
      ``defeated`` to ``enforced``.

    The rows differ from :data:`MODULE_CARRIER_SHAPES` in scope, not in kind:
    the carrier and every competing store are in the *same* function, so the
    function-scope store table is what decides the verdict and no module body
    is consulted at all.
    """
    # The "conditional store after the header" row writes its own `with`, so
    # that it can put a store *after* the assert; every other row gets the
    # header appended. Splitting on the marker keeps one table able to express
    # both positions without a second, near-identical fixture builder.
    if "with cs:" in body:
        source = "import contextlib\ndef outer(x, flag, helper):\n" + body
    else:
        source = (
            "import contextlib\n"
            "def outer(x, flag, helper):\n" + body + "    with cs:\n        assert x != 1\n"
        )
    _assert_entry_contract(label, source, False, live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A conditional "
        f"store after the last carrier can have replaced it with an enterable "
        f"value, so the header must be judged on the last *binding*, not on a "
        f"carrier that may be stale."
    )


#: #388 follow-up: which competing stores the decline must **not** count.
#:
#: A decline reports the assert live, so counting a store that cannot possibly
#: have installed an enterable value reports a *dead* assert as load-bearing.
#: These rows are the four categories that cannot, and the two controls that
#: keep each exclusion from being applied more widely than it was measured.
#:
#: Every row is executed by `_assert_entry_contract`, so `live` is held to
#: CPython rather than asserted about the checker. Two of the rows depend on a
#: class defined in the prelude -- a `with` on an arbitrary manager and a
#: starred unpack -- which is why this table builds its own source rather than
#: reusing the prelude of :data:`FUNCTION_CARRIER_SUPERSESSION_SHAPES`.
FUNCTION_CARRIER_NON_ENTERABLE_SUPERSEDERS = (
    # `with EXPR as cs:` binds `EXPR.__enter__()`, so whether the name still
    # holds something enterable is a question about the *manager*, not about
    # the syntax of the `with`. `nullcontext.__enter__` is `return None`, so
    # the header is entered with `None` and raises before the assert runs --
    # on both paths, since the carrier is a module on the other one. The
    # carrier therefore stays in force and the header is dead.
    (
        "a conditional nullcontext-with after a carrier is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext() as cs:\n            pass\n",
        False,
    ),
    # The same argument for `suppress`, whose `__enter__` is a bare `pass`
    # and so returns None implicitly. Kept as a separate row so the two
    # spellings of "provably None" cannot drift apart silently.
    (
        "a conditional suppress-with after a carrier is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.suppress(AssertionError) as cs:\n            pass\n",
        False,
    ),
    # The control that makes the two rows above *mean* something. A custom
    # manager's `__enter__` returns `self`, which IS enterable, so the same
    # `with ... as cs:` form leaves a live header on the flag=True path. A
    # blanket exclusion of every `With` store -- which is what a first cut
    # did -- answers `defeated` here and drops a live pinned contract. This
    # is the row that rules that implementation out.
    (
        "CONTROL a conditional custom-manager-with after a carrier is live",
        "    import os as cs\n    if flag:\n        with CM() as cs:\n            pass\n",
        True,
    ),
    # `*cs, = (...)` builds a list, and entering a list raises. Note this is
    # the *starred* target only: `cs, *rest = (...)` leaves `cs` holding the
    # first element, which is very often a real context manager, so it is a
    # control below rather than part of this exclusion.
    (
        "a conditional starred-unpack after a carrier is defeated",
        "    import os as cs\n    if flag:\n        *cs, = (contextlib.nullcontext(),)\n",
        False,
    ),
    # The named control for the row above.
    (
        "CONTROL a conditional element store after a carrier is live",
        "    import os as cs\n    if flag:\n        cs, rest = (contextlib.nullcontext(), 2)\n",
        True,
    ),
    # A nested `def` owns its locals, so the store binds *that* scope's `cs`
    # and the carrier is untouched. The header is dead on both paths, exactly
    # as it is for the `nullcontext` rows, but for a different reason: there
    # is no competing binding at all rather than a competing non-binding one.
    (
        "a conditional store in a nested def after a carrier is defeated",
        "    import os as cs\n    if flag:\n        def inner():\n            cs = contextlib.nullcontext()\n",
        False,
    ),
    (
        "a conditional store in a nested lambda after a carrier is defeated",
        "    import os as cs\n    if flag:\n        f = lambda: (cs := contextlib.nullcontext())\n",
        False,
    ),
    # A class body is its own namespace, so `C.cs` is bound and `outer`'s
    # `cs` is still the module. Same conclusion, third spelling of the
    # boundary.
    (
        "a conditional store in a nested class body after a carrier is defeated",
        "    import os as cs\n    if flag:\n        class C:\n            cs = contextlib.nullcontext()\n",
        False,
    ),
    # A `nonlocal` store is the control for the three rows above: it writes an
    # *enclosing* scope's `cs` rather than its own local, so the nested scope
    # is not a boundary for it and the carrier really is superseded. The
    # inner function is a genuine closure here -- `outer` holds `cs` -- so
    # `nonlocal cs` binds the very name the carrier set. This is the case a
    # scope test that stopped at the nested boundary would wrongly exclude,
    # and it is the pair that makes "nested scope" mean "a different local"
    # rather than merely "a nested node".
    (
        "CONTROL a conditional nonlocal-scoped store after a carrier is live",
        "    import os as cs\n    if flag:\n        def _rebind():\n            nonlocal cs\n            cs = contextlib.nullcontext()\n        _rebind()\n",
        True,
    ),
    # `global cs` names the *module* namespace, so it cannot supersede a
    # function-scope carrier -- and this store does not bind a local either,
    # so the nested scope is a real boundary for it. Reading the declaration
    # without asking which scope it governs excluded the store, and the
    # checker answered `enforced` on a header that raises `TypeError` on both
    # paths. Regression on `bee3e78`; a review finding on #388.
    (
        "a global-scoped store in a nested def after a function carrier is defeated",
        "    import os as cs\n    if flag:\n        def _rebind():\n            global cs\n            cs = contextlib.nullcontext()\n        _rebind()\n",
        False,
    ),
    # The same question asked through a *class* body, so the boundary cannot
    # be satisfied by special-casing `def` alone. A class body is its own
    # namespace, so `global cs` there is a module declaration that never
    # touches `outer`'s local -- third spelling of the same answer.
    (
        "a global-scoped store in a nested class body is defeated",
        "    import os as cs\n    if flag:\n        class C:\n            global cs\n            cs = contextlib.nullcontext()\n",
        False,
    ),
    # A declaration in a scope nested *inside* the owner says nothing about
    # the store sitting in the owner. `ast.walk` cannot tell them apart, so
    # this reads `enforced` unless the walk stops at the nested boundary.
    # Regression on `bee3e78`; a review finding on #388.
    (
        "a global declared only in a grandchild does not govern the owner's store",
        "    import os as cs\n    if flag:\n        def inner():\n            cs = contextlib.nullcontext()\n            def grandchild():\n                global cs\n        inner()\n",
        False,
    ),
    # A `nonlocal` in the grandchild fails the same way, and pins that the
    # exclusion is not keyed on the declaration being present at all. The
    # declaration needs a real enclosing binding to name, so `outer` is given
    # a second local of the same name and `inner` is made to read *that* one
    # rather than the carrier -- otherwise `nonlocal cs` inside `inner` would
    # be a `SyntaxError` and the row would test nothing.
    (
        "a nonlocal declared only in a grandchild does not govern the owner's store",
        (
            "    import os as cs\n"
            "    if flag:\n"
            "        def inner():\n"
            "            cs = contextlib.nullcontext()\n"
            "            def grandchild():\n"
            "                nonlocal cs\n"
            "        inner()\n"
        ),
        False,
    ),
    # The grandchild need not be a direct child of the owner. Wrapped in an
    # `if`, the declaration is still the grandchild's alone, and a boundary
    # that only stopped at *directly* nested scopes reached straight through
    # the wrapper. Review finding on `de23eea`.
    (
        "a nonlocal declared in a grandchild under an if is defeated",
        (
            "    import os as cs\n"
            "    if flag:\n"
            "        def inner():\n"
            "            cs = contextlib.nullcontext()\n"
            "            if True:\n"
            "                def grandchild():\n"
            "                    nonlocal cs\n"
            "        inner()\n"
        ),
        False,
    ),
    (
        "a global declared in a grandchild under an if is defeated",
        (
            "    import os as cs\n"
            "    if flag:\n"
            "        def inner():\n"
            "            cs = contextlib.nullcontext()\n"
            "            while False:\n"
            "                def grandchild():\n"
            "                    global cs\n"
            "        inner()\n"
        ),
        False,
    ),
    # `contextlib.nullcontext(CM())` returns its `enter_result`, so the
    # header is entered with `CM()` and the assert fires. Excluding every
    # `nullcontext` call regardless of arguments reported `defeated` on a
    # live contract. Pre-existing before #388; a review finding on #388.
    (
        "a conditional nullcontext-with carrying enter_result is live",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(CM()) as cs:\n            pass\n",
        True,
    ),
    # The same argument with the `enter_result` spelled the only way it can
    # be, since the parameter is keyword-only: a call carrying it has an
    # *empty* positional argument list, so checking `args` alone read it as
    # the no-argument form. Review finding on `de23eea`.
    (
        "a conditional nullcontext-with carrying a keyword enter_result is live",
        (
            "    import os as cs\n"
            "    if flag:\n"
            "        with contextlib.nullcontext(enter_result=CM()) as cs:\n"
            "            pass\n"
        ),
        True,
    ),
    # A literal `None` is the value the no-argument form produces, so the
    # keyword spelling of it is the same null context and must keep the
    # exclusion. Without this the fix above would over-claim and report a
    # dead assert as load-bearing.
    (
        "a conditional nullcontext-with carrying a None enter_result is defeated",
        (
            "    import os as cs\n"
            "    if flag:\n"
            "        with contextlib.nullcontext(enter_result=None) as cs:\n"
            "            pass\n"
        ),
        False,
    ),
    # A second value for the same parameter is a `TypeError` at the call, so
    # the `with` statement raises before the header is entered and the assert
    # under it never runs. Counting the call as a superseding store would
    # report that dead assert as load-bearing.
    (
        "a conditional nullcontext-with bound twice is defeated",
        (
            "    import os as cs\n"
            "    if flag:\n"
            "        with contextlib.nullcontext(CM(), enter_result=CM()) as cs:\n"
            "            pass\n"
        ),
        False,
    ),
    # A starred argument unpacks a literal, so the value is right there in the
    # source and is not the unreadable case a keyword `enter_result` is.
    (
        "a conditional nullcontext-with unpacking its enter_result is live",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(*[CM()]) as cs:\n            pass\n",
        True,
    ),
    # A falsy `enter_result` is not `None`: measured on 3.12.14,
    # `nullcontext(enter_result=0).__enter__()` returns `0`, not `None`. The
    # store is therefore counted -- a *decline* would report the assert live,
    # and here the header raises on `0` -- while the end-to-end verdict is
    # still `defeated`, because `0` cannot be entered either. That is the
    # point of the row: "cannot be entered" and "is the null context" are
    # different questions, and this rule answers the second.
    (
        "a conditional nullcontext-with a falsy enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=0) as cs:\n            pass\n",
        False,
    ),
    # A container holding a manager is still not a manager, and a *literal*
    # one is the case a truthiness test gets wrong: `enter_result=(CM(),)`
    # is truthy and binds a tuple that cannot be entered.
    (
        "a conditional nullcontext-with a tuple enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=(CM(),)) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a list enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=[CM()]) as cs:\n            pass\n",
        False,
    ),
    # A dict *display* binds a dict, exactly as the list and tuple rows above
    # bind a list and a tuple -- including one that holds a manager, which is
    # still a dict. These rows exist because the dict arm of the allowlist was
    # otherwise unexercised: deleting it outright left the whole lane green, so
    # nothing was pinning it. A `**` of a dict is a different question (it
    # names parameters) and is answered by the argument model instead.
    (
        "a conditional nullcontext-with a dict enter_result is defeated",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result={"a": CM()}) as cs:\n            pass\n',
        False,
    ),
    (
        "a conditional nullcontext-with an empty dict enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result={}) as cs:\n            pass\n",
        False,
    ),
    # A `**` unpacking of a *literal dict* is as readable as the keyword
    # itself, and a manager inside one does leave an enterable value bound.
    (
        "a conditional nullcontext-with a dict-literal enter_result is live",
        (
            "    import os as cs\n"
            "    if flag:\n"
            '        with contextlib.nullcontext(**{"enter_result": CM()}) as cs:\n'
            "            pass\n"
        ),
        True,
    ),
    # Naming a parameter that does not exist is a `TypeError` at the call, so
    # the statement raises before it binds anything.
    (
        "a conditional nullcontext-with an unknown keyword is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(foo=CM()) as cs:\n            pass\n",
        False,
    ),
    # An empty star unpacks to nothing, which is the no-argument form.
    (
        "a conditional nullcontext-with an empty star unpacking is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(*()) as cs:\n            pass\n",
        False,
    ),
    # A builtin container call is a *call*, and "a call binds something
    # enterable" is wrong for every one of these -- each binds an object with
    # no `__enter__`, so the header raises. Reading the argument's type
    # rather than its spelling is what tells `list()` from `CM()`.
    (
        "a conditional nullcontext-with a builtin container enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=list()) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a frozenset enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=frozenset()) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a range enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=range(3)) as cs:\n            pass\n",
        False,
    ),
    # --- #388 review findings 2-5: the argument's *type*, not its spelling ---
    #
    # Every row below binds `CM` -- a real manager -- through a spelling the
    # old classifier could not read, and every one of them is **live**. The
    # first implementation read "not a bare `ast.Call`" as "not enterable"
    # and declined all of them, which reports a genuinely live assert dead.
    # That is the damaging direction, so an unreadable value now defaults to
    # "may be enterable" and only a *pinned* non-enterable type may decline.
    (
        "a conditional nullcontext-with a walrus enter_result is live",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=(cm := CM())) as cs:\n            pass\n",
        True,
    ),
    (
        "a conditional nullcontext-with a conditional enter_result is live",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=(CM() if flag else None)) as cs:\n            pass\n",
        True,
    ),
    (
        "a conditional nullcontext-with an or-chained enter_result is live",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=(None or CM())) as cs:\n            pass\n",
        True,
    ),
    (
        "a conditional nullcontext-with a subscripted enter_result is live",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=[CM()][0]) as cs:\n            pass\n",
        True,
    ),
    (
        "a conditional nullcontext-with a named enter_result is live",
        "    import os as cs\n    if flag:\n        cm = CM()\n        with contextlib.nullcontext(enter_result=cm) as cs:\n            pass\n",
        True,
    ),
    # A starred *computed* container pins neither the number of values nor
    # their types, so it may supply exactly one manager.
    (
        "a conditional nullcontext-with an unreadable star is live",
        "    import os as cs\n    if flag:\n        values = [CM()]\n        with contextlib.nullcontext(*values) as cs:\n            pass\n",
        True,
    ),
    (
        "a conditional nullcontext-with an unreadable mapping is live",
        '    import os as cs\n    if flag:\n        values = {"enter_result": CM()}\n        with contextlib.nullcontext(**values) as cs:\n            pass\n',
        True,
    ),
    # A **computed** key builds a mapping the source does not pin, so it may
    # be spelled `enter_result` and bind a manager.
    (
        "a conditional nullcontext-with a computed mapping key is live",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(**{("enter_" + "result"): CM()}) as cs:\n            pass\n',
        True,
    ),
    # Finding 3: the argument count is over *effective* arguments, so an
    # empty star supplies none. Counting it as one falsely declared a
    # duplicate parameter and reported this live contract dead.
    (
        "a conditional nullcontext-with an empty star then a keyword is live",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(*[], enter_result=CM()) as cs:\n            pass\n",
        True,
    ),
    (
        "a conditional nullcontext-with an empty tuple star then a value is live",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(*(), CM()) as cs:\n            pass\n",
        True,
    ),
    # Finding 3, the other direction: a two-element star supplies two
    # arguments, which is a `TypeError` at the call.
    (
        "a conditional nullcontext-with a two element star is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(*[CM(), CM()]) as cs:\n            pass\n",
        False,
    ),
    # Finding 4: a `**` mapping carrying an unexpected key raises at the call
    # even when an enterable value is present, so the store is not one that
    # can be excluded. Each is a distinct spelling of the same validation.
    (
        "a conditional nullcontext-with a positional and a mapped enter_result is defeated",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(CM(), **{"enter_result": CM()}) as cs:\n            pass\n',
        False,
    ),
    (
        "a conditional nullcontext-with a keyword and a mapped enter_result is defeated",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=CM(), **{"enter_result": CM()}) as cs:\n            pass\n',
        False,
    ),
    (
        "a conditional nullcontext-with a keyword enter_result and an unknown one is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=CM(), foo=1) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a positional and an unknown keyword is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(CM(), foo=1) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a mapped unknown keyword is defeated",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(**{"enter_result": CM(), "foo": 1}) as cs:\n            pass\n',
        False,
    ),
    (
        "a conditional nullcontext-with a positional and a mapped unknown keyword is defeated",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(CM(), **{"foo": 1}) as cs:\n            pass\n',
        False,
    ),
    # Finding 5: a dict display keeps the **last** of several duplicate keys.
    # Reading the first reversed both of these -- one reported a live header
    # dead, the other reported a dead header live.
    (
        "a conditional nullcontext-with a duplicate key keeping the manager is live",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(**{"enter_result": None, "enter_result": CM()}) as cs:\n            pass\n',
        True,
    ),
    (
        "a conditional nullcontext-with a duplicate key keeping None is defeated",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(**{"enter_result": CM(), "enter_result": None}) as cs:\n            pass\n',
        False,
    ),
    (
        "a conditional nullcontext-with a duplicate key keeping zero is defeated",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(**{"enter_result": CM(), "enter_result": 0}) as cs:\n            pass\n',
        False,
    ),
    # Finding 6: a nested one-element tuple **crashed** the checker, which
    # recursed with an `ast.Tuple` into logic that reads `.args` off a call.
    # CPython binds a tuple and raises entering it, so the answer is
    # `defeated`; the crash made the question unanswerable instead.
    (
        "a conditional nullcontext-with a nested tuple enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=((CM(),),)) as cs:\n            pass\n",
        False,
    ),
    # The builtin call rows below are the *other* direction of the same
    # rule: `int()`, `dict()` and `set()` are calls whose results have no
    # `__enter__`, so counting every call as enterable declared them live.
    (
        "a conditional nullcontext-with an int enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=int()) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a dict enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=dict()) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a set enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=set()) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a positional builtin is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(list()) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a starred builtin is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(*[int()]) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a mapped builtin is defeated",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(**{"enter_result": int()}) as cs:\n            pass\n',
        False,
    ),
    # A builtin call is decided by *name*, not by whether it is a container.
    # `bool()`, `object()`, `complex()` and `bytearray()` are as fixed as
    # `list()` -- each binds an object with no `__enter__` -- and the first
    # cut of the allowlist listed only containers and numbers, so these four
    # were read as may-enterable and declared a dead header live. Regression
    # found by an adversarial sweep against base `ed9d9b0`, which got all four
    # right.
    (
        "a conditional nullcontext-with a bool enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=bool()) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with an object enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=object()) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a complex enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=complex()) as cs:\n            pass\n",
        False,
    ),
    (
        "a conditional nullcontext-with a bytearray enter_result is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=bytearray()) as cs:\n            pass\n",
        False,
    ),
    # An f-string is a `str` whatever it interpolates, and `ast` gives it an
    # `ast.JoinedStr` rather than folding it into `ast.Constant`. Left to the
    # unreadable default it answered "may be enterable" on a value that is
    # provably a string.
    (
        "a conditional nullcontext-with an f-string enter_result is defeated",
        '    import os as cs\n    if flag:\n        with contextlib.nullcontext(enter_result=f"{x}") as cs:\n            pass\n',
        False,
    ),
    # A `*` of a *readable* dict display unpacks to its keys, so `*{}` unpacks
    # to nothing and supplies no argument at all -- the same no-argument form
    # as `nullcontext()`, binding `None`. Reading it as an unreadable star made
    # it supply a "maybe enterable" value instead.
    (
        "a conditional nullcontext-with an empty dict star is defeated",
        "    import os as cs\n    if flag:\n        with contextlib.nullcontext(*{}) as cs:\n            pass\n",
        False,
    ),
    # A **computed** key is a distinct entry beside a readable `enter_result`
    # and cannot overwrite it, and `nullcontext` takes one parameter -- so the
    # call raises whatever the computed key evaluates to. A `**` *spread*
    # (`{**other}`) is the opposite and can overwrite, so it is not counted as
    # an extra key. Reading both the same way got the first one backwards.
    (
        "a conditional nullcontext-with a computed key beside enter_result is defeated",
        (
            "    import os as cs\n"
            "    if flag:\n"
            '        with contextlib.nullcontext(**{("enter_" + "r"): 1, "enter_result": CM()}) as cs:\n'
            "            pass\n"
        ),
        False,
    ),
    (
        "CONTROL a conditional nullcontext-with a mapping spread is live",
        (
            "    import os as cs\n"
            "    other = {'enter_result': CM()}\n"
            "    if flag:\n"
            '        with contextlib.nullcontext(**{"enter_result": CM(), **other}) as cs:\n'
            "            pass\n"
        ),
        True,
    ),
    # The name is bound by a *different* item of the same `with`, so the
    # non-enterable sibling says nothing about what `cs` received. Searching
    # the whole statement for a known-`None` manager excluded a store that
    # binds an enterable value. Pre-existing before #388.
    (
        "a conditional with whose nullcontext sibling binds another name is live",
        "    import os as cs\n    if flag:\n        with CM() as cs, contextlib.nullcontext() as other:\n            pass\n",
        True,
    ),
    # `asyncio` has no `suppress` in CPython 3.12.14, so the name is whatever
    # the program put there. Inheriting the spelling into the
    # `None`-returning set excluded a store bound to a real manager. The
    # rebind is written in the row itself because that *is* the case: a
    # module attribute can be replaced, and the exclusion has to notice.
    (
        "a conditional with on a rebound asyncio-suppress is live",
        (
            "    import os as cs\n"
            "    import asyncio\n"
            "    asyncio.suppress = CM\n"
            "    if flag:\n"
            "        with asyncio.suppress() as cs:\n"
            "            pass\n"
        ),
        True,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "live"),
    FUNCTION_CARRIER_NON_ENTERABLE_SUPERSEDERS,
    ids=[shape[0] for shape in FUNCTION_CARRIER_NON_ENTERABLE_SUPERSEDERS],
)
def test_a_function_carrier_decline_ignores_non_enterable_stores(label, body, live):
    """The decline must not count a store that cannot bind an enterable value.

    The four exclusions -- unbinding, a known `None`-returning `with`, a
    starred unpack, and a nested-scope store -- all share one property: none of
    them can leave `cs` bound to something `with` can enter. Counting one of
    them would decline the header, and a decline reports the assert live, so
    the error would be a dead assert certified as load-bearing.

    The `with` exclusion is the narrowest of the four and the rows below it are
    what keep it narrow: it applies to the item that binds *the queried name*,
    and only when that item is a `None`-returning call outright. A sibling
    item's manager, an `enter_result` argument, and a rebound `asyncio` spelling
    each put an enterable value on the name, and each of those is a row here
    rather than a gap.

    The controls matter as much as the rows. A `with` on a *custom* manager and
    a `nonlocal`-scoped store both leave an enterable value bound to the
    outer name, so both must be counted; and a non-starred element store
    (`cs, rest = (...)`) leaves `cs` holding a real `nullcontext()`. Together
    they pin each exclusion to the category it was measured on, so the next
    change cannot widen one of them into its neighbourhood.
    """
    source = (
        "import contextlib\n"
        "class CM:\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *exc):\n"
        "        return False\n"
        "def outer(x, flag, helper):\n" + body + "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, live)
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. A store that "
        f"cannot bind an enterable value must not decline the header, and a "
        f"store that can must."
    )
