"""Import-bound context managers and entry contracts.

Actual test functions and literal cases from the original collector.
"""

import ast
import asyncio

import pytest

from tests import _timed_menu_milestone_sentinel_support as support
from tests._timed_menu_milestone_sentinel_support import _is_enforced

#: #324: every binding form Python has must retire a carried walrus, not just
#: ``ast.Name`` targets.
#:
#: A walrus-bound suppressor is only correct while it is the *live* binding of
#: the name. Rebinding that name to a ``nullcontext`` on every path makes the
#: later ``with cs:`` enter something that does not suppress, so the assert
#: under it is live and must be reported enforced. Reading only ``ast.Name``
#: targets left every nested and non-``Assign`` store invisible, so a stale
#: suppressor outranked the rebinding and a **live** assert was reported
#: defeated -- the damaging direction.
#:
#: ``expected`` is the exact per-assert verdict list, not ``all(...)``:
#: collapsing to the first verdict is what hid this defect in the first place.
WALRUS_REBINDING_SHAPES = (
    (
        "a plain store retires the walrus",
        "    cs = contextlib.nullcontext()",
        False,
        True,
    ),
    (
        "a tuple-unpack store retires the walrus",
        "    cs, other = (contextlib.nullcontext(), 2)",
        False,
        True,
    ),
    (
        "a starred-unpack store retires the walrus",
        "    cs, *rest = (contextlib.nullcontext(), 2, 3)",
        False,
        True,
    ),
    (
        "a list-target store retires the walrus",
        "    [cs] = [contextlib.nullcontext()]",
        False,
        True,
    ),
    (
        "an annotated store retires the walrus",
        "    cs: object = contextlib.nullcontext()",
        False,
        True,
    ),
    (
        "a loop target retires the walrus",
        "    for cs in (contextlib.nullcontext(),):\n        pass",
        False,
        True,
    ),
    (
        "a with-as target retires the walrus",
        "    with nullcontext() as cs:\n        pass",
        False,
        False,
    ),
    (
        "an async with-as target retires the walrus",
        "    async with nullcontext() as cs:\n        pass",
        True,
        False,
    ),
    (
        "an async loop target over a context manager is undecided by the rule",
        "    async for cs in agen():\n        pass",
        True,
        True,
    ),
    (
        "an except-as target retires the walrus",
        "    try:\n        raise ValueError()\n    except ValueError as cs:\n        pass",
        False,
        False,
    ),
    (
        "a del retires the walrus",
        "    del cs",
        False,
        False,
    ),
)


#: #359: a ``with``-header bound by a carrier that is not a target.
#:
#: Three binding forms bind their name as a *string field* of a node rather
#: than as a target: ``import os as cs`` (``Import.asname``), ``def cs()`` and
#: ``class cs``. The name therefore holds a module, a class or a function --
#: none of which has ``__enter__`` -- so ``with cs:`` raises ``TypeError``
#: while evaluating the header, before the assert under it is ever reached.
#:
#: ``from M import N as cs`` is **not** here. It looks identical but binds an
#: arbitrary attribute of ``M``; see :data:`UNDECIDABLE_IMPORT_FROM_SHAPES`
#: and #376.
#:
#: This table exists separately from ``WALRUS_REBINDING_SHAPES`` because that
#: table's fixture *wraps* its rebind in a preceding
#: ``with (cs := contextlib.suppress(...))``. A carrier row added there is
#: **vacuous**: the walrus already makes the second assert's verdict
#: ``[False]``, so deleting the whole carrier implementation leaves the row
#: green. These rows carry no walrus, so the carrier alone decides the
#: verdict and the fixture fails if the rule stops reading them.
#:
#: ``live`` is the per-row entry contract, held to CPython by
#: ``_assert_entry_contract`` rather than merely asserted about the checker.
CARRIER_ONLY_SHAPES = (
    (
        "an import-as carrier leaves a module",
        "    import os as cs",
        False,
    ),
    (
        "a def carrier leaves a function",
        "    def cs():\n        pass",
        False,
    ),
    (
        "a class carrier leaves a class",
        "    class cs:\n        pass",
        False,
    ),
    (
        "CONTROL a plain store leaves a real context manager",
        "    cs = nullcontext()",
        True,
    ),
)


#: #359: the same carriers bound at **module** scope.
#:
#: A module-scope carrier binds the name for the whole file, so a ``with``
#: header inside a function reads it as a *free* name. The function-scoped
#: walk behind :func:`_carrier_runtime_kinds` cannot see it, so before #359
#: the store table was silent and the header read as ``enforced`` -- on an
#: assert the interpreter never evaluates. This is the residue that was left
#: behind when #354's factual claim was refuted and closed.
#:
#: These rows are separate from :data:`CARRIER_ONLY_SHAPES` because the name
#: is bound in a *different scope*, and that is the whole difference: the same
#: source gives two different verdicts on master depending only on where the
#: carrier is written. ``MODULE_CARRIER_SHAPES`` is in the module body;
#: ``CARRIER_ONLY_SHAPES`` is inside the function.
MODULE_CARRIER_SHAPES = (
    (
        "a module-scope import-as carrier leaves a module",
        "import os as cs",
        False,
    ),
    (
        "a module-scope def carrier leaves a function",
        "def cs():\n    pass",
        False,
    ),
    (
        "a module-scope class carrier leaves a class",
        "class cs:\n    pass",
        False,
    ),
    (
        "a later module-scope def supersedes an earlier carrier",
        "import os as cs\ndef cs():\n    pass",
        False,
    ),
    # --- #359 supersession family -------------------------------------
    # A name's value is settled by the last *binding*, not the last carrier.
    # Each row below binds a module-scope carrier and then rebinds the name
    # to a real `nullcontext()` by a *different* module-level store form.
    # Because the later store wins at runtime, `with cs:` succeeds and the
    # assert is live -- but a rule that reads only carriers still sees the
    # earlier `import os as cs`, reports the name as a module, and answers
    # `defeated`, dropping a real pinned contract. Master already answers
    # these correctly, so they are regression rows, not new repairs.
    (
        "a later module-scope plain store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\ncs = contextlib.nullcontext()",
        True,
    ),
    (
        "a later module-scope walrus store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\n(cs := contextlib.nullcontext())",
        True,
    ),
    (
        "a later module-scope tuple-unpack store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\ncs, other = (contextlib.nullcontext(), 2)",
        True,
    ),
    (
        "a later module-scope annotated store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\ncs: object = contextlib.nullcontext()",
        True,
    ),
    (
        "a later module-scope for-target store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\nfor cs in (contextlib.nullcontext(),):\n    pass",
        True,
    ),
    (
        "a later module-scope del-then-store supersedes an earlier carrier",
        "import os as cs\nimport contextlib\ncs = contextlib.nullcontext()\ndel cs\ncs = contextlib.nullcontext()",
        True,
    ),
    (
        "CONTROL a module-scope store of a real context manager is live",
        "import contextlib\ncs = contextlib.nullcontext()",
        True,
    ),
    (
        "CONTROL a function-local store shadows the module carrier and is live",
        "import os as cs",
        True,
    ),
)

#: #376, revised by #389: ``from M import N as cs`` binds an attribute, and the
#: attribute is now *resolved* rather than declined.
#:
#: It looks exactly like ``import os as cs`` -- a name carried in a string
#: field of an import node -- but the two are not the same claim. ``import
#: os as cs`` binds the module ``os``, and the language gives that spelling
#: one meaning. ``from M import N as cs`` binds whatever attribute ``N`` is on
#: ``M``, and one spelling produces every runtime type (measured):
#:
#:     from os import path as cs         -> os.path   a module
#:     from os import sep as cs          -> os.sep    a str
#:     from decimal import Decimal as cs -> a class
#:     from mymod import ctx as cs       -> WHATEVER mymod.ctx is
#:
#: The last row is why the shape cannot be answered from the *syntax* alone:
#: ``mymod.ctx`` is a real ``nullcontext()``, so ``with cs:`` **succeeds** and
#: the assert is live, while ``os.path`` raises ``TypeError`` and the assert is
#: dead. Recording every ``from ... import ... as`` as a module would report
#: that live assert as dead and drop a real pinned contract -- the damaging
#: direction, introduced by the very rule meant to fix it.
#:
#: The first cut therefore declined the whole shape, which made the analyzer
#: answer ``enforced`` for all three rows above. Two of those are genuinely
#: dead, so the decline certified a contract CPython never applies.
#:
#: #389 answers the question the decline was standing in for: not "is this an
#: ``ImportFrom``" but "what does *this* attribute resolve to". The real
#: interpreter is consulted through ``_from_import_kind``, which returns the
#: runtime type name only for a value that cannot implement the context
#: manager protocol, and ``None`` for an enterable one. The live control in
#: :func:`test_an_import_from_as_can_bind_a_real_context_manager` is what keeps
#: this honest: it is the same spelling, over a module that exports an actual
#: context manager, and it must stay ``enforced``.
UNDECIDABLE_IMPORT_FROM_SHAPES = (
    (
        "an import-from-as binding a module is resolved to its runtime type",
        "from os import path as cs",
    ),
    (
        "an import-from-as binding a str is resolved to its runtime type",
        "from os import sep as cs",
    ),
    (
        "an import-from-as binding a class is resolved to its runtime type",
        "from decimal import Decimal as cs",
    ),
)


@pytest.mark.parametrize(
    ("label", "bind"),
    UNDECIDABLE_IMPORT_FROM_SHAPES,
    ids=[shape[0] for shape in UNDECIDABLE_IMPORT_FROM_SHAPES],
)
def test_an_import_from_as_is_resolved_rather_than_declined(label, bind):
    """``from M import N as cs`` is answered by what ``N`` resolves to.

    Each row is a real ``import from`` whose bound value is genuinely
    unenterable: ``os.path`` is a module, ``os.sep`` a ``str``, and
    ``Decimal`` a class. None of them has ``__enter__``, so entering one
    raises ``TypeError`` on the header, the assert under it is never
    evaluated, and ``dead`` is the only correct verdict. #389 resolves the
    attribute through the real interpreter instead of declining the shape.

    The earlier cut declined the whole shape, so all three read ``enforced``:
    three dead contracts certified as load-bearing, the damaging direction.

    This is still not a coverage hole because of the control in
    :func:`test_an_import_from_as_can_bind_a_real_context_manager`: the same
    spelling, over a module that exports an actual context manager, is live
    and must stay ``enforced``. That control is what forces the resolution to
    be per-attribute rather than per-spelling, and it is the row that breaks
    under a rule answering "module" for the whole shape.
    """
    source = (
        "import contextlib\n"
        "from contextlib import suppress, nullcontext\n"
        "def outer(x, flag, helper):\n    " + bind + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False], (
        f"{label}: expected [False] -- the bound value cannot be entered, so "
        f"the assert is unreachable -- got {results}."
    )


def test_an_import_from_as_can_bind_a_real_context_manager():
    """The control that makes the decline above necessary rather than cautious.

    ``mymod.ctx`` is a real ``contextlib.nullcontext()`` instance, so
    ``from mymod import ctx as cs`` binds an **enterable** value, ``with cs:``
    succeeds, the assert is live, and the verdict must be ``enforced``.

    Nothing in the source distinguishes this from
    ``from os import path as cs``, which binds an unenterable module. That is
    the whole argument for declining the shape: the syntax fixes the value for
    ``import ... as`` and does not fix it for ``from ... import ... as``.

    Executed rather than asserted about the analyzer, so the row cannot pass
    by the checker and the claim being wrong together.
    """
    source = (
        "import contextlib\n"
        "def outer(x, flag, helper):\n"
        "    from tests._import_from_carrier_support import ctx as cs\n"
        "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract("an import-from-as binding a real context manager", source, False, True)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"expected [True] -- the assert is live -- got {results}. A rule that "
        f"answered 'dead' here would drop a real pinned contract."
    )


#: A context-manager **class** is not itself a context manager. #457.
#:
#: ``from M import N as cs`` is answered by what ``N`` resolves to (#389), and
#: the first cut of that resolution asked ``hasattr(value, "__enter__")`` of
#: the *value*. That question is asked in the wrong place. ``with`` performs
#: its special-method lookup on ``type(value)``, so a class is enterable only
#: when its *metatype* implements the protocol. A class instead exposes
#: ``__enter__`` as the unbound function its own instances will use, which is
#: precisely the attribute the class is not entitled to:
#:
#:     >>> from contextlib import suppress
#:     >>> hasattr(suppress, "__enter__")        # the instances' method
#:     True
#:     >>> with suppress:                        # ... on the class itself
#:     TypeError: 'ABCMeta' object does not support the context manager protocol
#:
#: So asking the value reported every context-manager class as enterable and
#: certified the assert underneath as load-bearing, when the interpreter had
#: already raised ``TypeError`` on the header. Measured 5/5 on the tree this
#: repairs, and the direction is the damaging one.
#:
#: Both rows below bind an abstract context-manager base through the very
#: ``import from ... as`` spelling #389 already resolves, so they are decided
#: by the same rule and isolate the lookup's *subject*.
CONTEXT_MANAGER_CLASS_SHAPES = (
    (
        "an import-from-as binding a context-manager class is unenterable",
        "from contextlib import suppress as cs",
    ),
    (
        "an import-from-as binding the other context-manager class too",
        "from contextlib import nullcontext as cs",
    ),
)


@pytest.mark.parametrize(
    ("label", "bind"),
    CONTEXT_MANAGER_CLASS_SHAPES,
    ids=[shape[0] for shape in CONTEXT_MANAGER_CLASS_SHAPES],
)
def test_a_context_manager_class_is_not_itself_enterable(label, bind):
    """Entering a context-manager *class* raises; entering an instance does not.

    #457. The class exposes ``__enter__`` because its **instances** define
    one, but ``with cs:`` looks the protocol up on the metatype, which does
    not define it. The header therefore raises ``TypeError``, the assert under
    it never runs, and ``dead`` is the only correct verdict.

    This is decided by asking ``type(value)`` rather than ``value`` -- and
    the control in :func:`test_a_context_manager_instance_stays_enterable` is
    what forces that choice. A test that merely asserted "no class is
    enterable" would pass a rule hard-coding that, and would then be wrong
    about the very next row.
    """
    source = (
        "import contextlib\n"
        "def outer(x, flag, helper):\n    " + bind + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, False)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False], (
        f"{label}: expected [False] -- a class whose metatype cannot be "
        f"entered is not a context manager -- got {results}. Asking the "
        f"value for __enter__ reads the unbound instance method and certifies "
        f"a dead contract as live."
    )


def test_a_context_manager_instance_stays_enterable():
    """The control: the *instance* of the same class is genuinely enterable.

    ``contextlib.nullcontext()``'s own type defines ``__enter__``, so the
    metatype lookup finds it and the header succeeds. Nothing in the source
    says "class" -- the difference is entirely in what the attribute
    resolves to, which is the property the fix reasons about.

    This is the row that breaks under either shortcut around the real rule:
    a hard-coded "every class is dead" (which would drop a contract a class
    with an enterable metaclass genuinely honours) and a hard-coded "the
    value has ``__enter__``" (the defect #457 files).
    """
    source = (
        "import contextlib\n"
        "def outer(x, flag, helper):\n"
        "    from contextlib import nullcontext\n"
        "    with nullcontext():\n        assert x != 1\n"
    )
    _assert_entry_contract("a context-manager instance stays enterable", source, False, True)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"expected [True] -- an instance of a context manager is enterable -- got {results}."
    )


def test_a_local_class_with_an_enterable_metaclass_enters_the_bare_header():
    """#468. The *class-carrier* path must consult the metatype, not identity.

    #457 repaired the ``cs = CM`` store spelling, and this is deliberately the
    other spelling. A bare ``with CM:`` header reaches a different rule: the
    local ``class CM`` is recorded by ``_carrier_runtime_kinds`` as the kind
    ``"type"``, and ``"type"`` is unconditionally unenterable, so an enterable
    metaclass was never consulted and a firing assert was reported defeated --

        def probe(x):
            class Meta(type):
                def __enter__(cls): return cls
                def __exit__(cls, *exc): return False
            class CM(metaclass=Meta): pass
            with CM:
                assert x != 1        # fires at x=1

    ``with CM:`` performs the protocol lookup on ``type(CM)``, which is
    ``Meta``, and ``Meta`` defines both dunders, so entry succeeds and the body
    runs. The two spellings are the same program with different verdicts, and
    both are live at runtime.

    Executed, so the row cannot pass by the checker and the claim being wrong
    together: at ``x=1`` the assert must fire.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    class Meta(type):\n"
        "        def __enter__(cls):\n"
        "            return cls\n"
        "        def __exit__(cls, *exc):\n"
        "            return False\n"
        "    class CM(metaclass=Meta):\n"
        "        pass\n"
        "    with CM:\n"
        "        assert x != 1\n"
    )
    _assert_entry_contract("a local class with an enterable metaclass", source, False, True)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"expected [True] -- `with CM:` looks the dunder up on `type(CM)`, "
        f"which is `Meta`, and `Meta` implements the protocol -- got {results}. "
        f"Recording every local `class` as the unconditionally-unenterable kind "
        f'`"type"` drops a genuinely live contract.'
    )


def test_a_local_class_whose_metaclass_is_unreadable_is_declined():
    """Plain/unresolved classes retain the prior conservative verdict.\n\n    The exact runtime controls raise TypeError before the assertion.\n    Unresolved metaclasses may have other behavior and are not proven live.\n"""
    for label, body, expected in (
        ("a plain local class", "    class CM: pass\n", False),
        ("a local class inheriting type", "    class CM(type): pass\n", False),
        ("an explicit metaclass keyword", "    class CM(metaclass=type): pass\n", False),
    ):
        source = f"def outer(x, flag, helper):\n{body}    with CM:\n        assert x != 1\n"
        namespace = {}
        exec(source, namespace)  # noqa: S102
        with pytest.raises(TypeError):
            namespace["outer"](1, False, lambda: None)
        tree = ast.parse(source)
        function = tree.body[-1]
        asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
        results = [_is_enforced(function, node, tree) for node in asserts]
        assert results == [expected], (
            f"{label}: expected {[expected]} -- got {results}. A class with no "
            f"bases, or one inheriting `type`, has metatype `type`, which does "
            f"not implement the protocol, so these executed controls raise `TypeError` "
            f"before the assert. Unknown metaclasses keep the conservative "
            f"verdict; only a source-proven live protocol widens it."
        )


def test_a_class_with_an_enterable_metaclass_is_enterable():
    """The discriminating row a "classes are never enterable" rule gets wrong.

    ``with`` consults the metatype, so a class whose metaclass defines
    ``__enter__``/``__exit__`` really does support the protocol: entering it
    runs the body's assert and binds the class itself. The fix therefore
    looks the dunder up on ``type(value)`` rather than deciding on class
    identity.

    Executed, so the row cannot pass by the checker and the claim being wrong
    together: at ``x=1`` the assert must fire.
    """
    source = (
        "class Meta(type):\n"
        "    def __enter__(cls):\n"
        "        return cls\n"
        "    def __exit__(cls, *exc):\n"
        "        return False\n"
        "class CM(metaclass=Meta):\n"
        "    pass\n"
        "def outer(x, flag, helper):\n"
        "    from pickle import PickleError as _unused\n"
        "    cs = CM\n"
        "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract("a class with an enterable metaclass", source, False, True)
    tree = ast.parse(source)
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [True], (
        f"expected [True] -- the metatype implements the protocol, so entering "
        f"the class succeeds -- got {results}. A rule that answered 'every "
        f"class is unenterable' would drop a genuinely live contract."
    )


def test_the_unenterable_class_kind_is_the_one_the_module_already_uses():
    """``_from_import_kind`` reports a class as ``"type"``, not ``"ABCMeta"``.

    The metatype of ``contextlib.suppress`` is ``ABCMeta``, so the naive
    ``type(value).__name__`` returns a name ``NON_CONTEXT_MANAGER_TYPES`` does
    not list. The caller would then treat the header as *unreadable* and
    report the assert live -- the same false LIVE, reintroduced one line
    below the fix.

    ``_carrier_runtime_kinds`` already records a ``ClassDef`` as ``"type"``,
    so the two producers of that kind have to agree or the shape is answered
    by whichever one is wrong.
    """
    assert support._from_import_kind("contextlib", "suppress") == "type"
    assert support._from_import_kind("contextlib", "nullcontext") == "type"
    # The ordinary unenterable kinds are untouched.
    assert support._from_import_kind("os", "path") == "module"
    assert support._from_import_kind("os", "sep") == "str"
    assert support._from_import_kind("decimal", "Decimal") == "type"
    # An enterable attribute still answers None, and so does an unresolvable
    # one -- declining is the safe direction when the value cannot be read.
    assert support._from_import_kind("contextlib", "suppress_cascade") is None
    assert support._from_import_kind("no_such_module_at_all", "anything") is None


@pytest.mark.parametrize(
    ("label", "bind", "live"),
    CARRIER_ONLY_SHAPES,
    ids=[shape[0] for shape in CARRIER_ONLY_SHAPES],
)
def test_a_with_header_bound_by_a_carrier_is_dead_entry(label, bind, live):
    """A module, class or function in a ``with`` header defeats the assert.

    The fixture binds the name in one syntactic form and immediately enters
    it, with **no** walrus suppressor anywhere -- so the carrier is the only
    thing that can make the assert unreachable.

    The four carrier rows are dead entry: ``with cs:`` raises ``TypeError``
    in the header, so the assert under it never runs and reporting it as
    load-bearing certifies a contract the interpreter never applies. The
    ``CONTROL`` row binds a real context manager with a plain store and is
    therefore live, and exists so that the four rows cannot be made to pass
    by simply declaring every carrier-shaped header dead -- the control is
    the row that would break under that shortcut.
    """
    source = (
        "import contextlib\n"
        "from contextlib import suppress, nullcontext\n"
        "def outer(x, flag, helper):\n" + bind + "\n"
        "    with cs:\n        assert x != 1\n"
    )
    _assert_entry_contract(label, source, False, live)
    tree = ast.parse(source)
    # Two module-level imports precede `def outer`, so it is the last body
    # node, not `tree.body[1]`.
    function = tree.body[-1]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    expected = [live]
    assert results == expected, (
        f"{label}: expected verdicts {expected}, got {results}. The carrier "
        f"alone decides this verdict, so the two must agree."
    )


@pytest.mark.parametrize(
    ("label", "prelude", "live"),
    MODULE_CARRIER_SHAPES,
    ids=[shape[0] for shape in MODULE_CARRIER_SHAPES],
)
def test_a_module_scope_carrier_defeats_the_assert_below_it(label, prelude, live):
    """A carrier bound at module scope defeats an assert in any function.

    The carrier is written in the module body and the assert lives in
    ``outer``, so the name reaches the ``with`` header as a free name. Nothing
    inside the function binds it, which is exactly the case the function-scoped
    walk cannot see.

    The five dead rows are held to CPython by ``_assert_entry_contract``. The
    two controls pin both ways a carrier must *not* be over-read: a
    module-scope store of a real context manager is live, and a function-local
    store shadows the module carrier and is live again. Without the second
    control a fix that simply declared every free-name header dead would pass.

    The six **supersession** rows are the other direction, and they are
    regression rows rather than new repairs: master already answers them
    correctly. Each binds a module-scope carrier and then rebinds the same
    name with a different module-level store form, so the *last binding* --
    not the last carrier -- settles the value, and ``with cs:`` succeeds. A
    rule that reads only carriers still sees the ``import os as cs``, reports
    the name as a module, and answers ``defeated``, dropping a live contract.
    They are what keeps the module rule honest about *which* binding wins.
    """
    if "shadows" in label:
        body = "    from contextlib import nullcontext\n    cs = nullcontext()\n"
    else:
        body = ""
    source = (
        prelude + "\ndef outer(x, flag, helper):\n" + body + "    with cs:\n        assert x != 1\n"
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
        f"{label}: expected verdicts {expected}, got {results}. A module-scope "
        f"carrier is a real store that runs at import time, so the header "
        f"must be judged on it rather than declined."
    )


def _assert_entry_contract(label, source, is_async, second_assert_live):
    """Run the fixture and hold CPython to the row's declared entry contract.

    This is the check the table did not have. Every row ends with a
    ``with cs:``, and what CPython does on entry -- succeed and run the body,
    or raise before the body -- is the ground truth the analyzer's verdict is
    checked against. Pinning it here means a future row cannot claim "live"
    without the interpreter agreeing, and cannot claim "dead" either.

    Called with ``x=1``, so ``assert x != 1`` is **false** and the assert
    fires if and only if it is reachable. That distinguishes the two outcomes
    cleanly: a live row must raise ``AssertionError``, and an unreachable row
    must raise something *else* (``TypeError`` for a non-manager, or
    ``UnboundLocalError`` for an unbound name). Running with a value that
    makes the assert trivially true would let a swallowed row and an
    unreachable row look identical, which is the confusion this replaces.
    """
    namespace = {}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    outer = namespace["outer"]
    try:
        if is_async:
            asyncio.run(outer(1, True, None))
        else:
            outer(1, True, None)
    except AssertionError:
        if second_assert_live:
            return
        raise AssertionError(
            f"{label}: the second assert fired, so the name was enterable. "
            f"The row claims it is unreachable."
        ) from None
    except (TypeError, UnboundLocalError, NameError):
        if second_assert_live:
            raise AssertionError(
                f"{label}: entering `with cs:` raised instead of running the "
                f"body, so the second assert is unreachable. The row claims "
                f"it is live."
            ) from None
        return
    raise AssertionError(
        f"{label}: the fixture returned normally, so the second assert was "
        f"swallowed rather than reachable. Row is stale."
    )
