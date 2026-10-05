"""Literal match and import-bound manager witnesses.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_canonical_module_import, _is_enforced


@pytest.mark.parametrize(
    "clause,error",
    (
        ("        case [cs] if missing(): pass\n", NameError),
        ("        case [cs] if 1 / 0: pass\n", ZeroDivisionError),
        ("        case [cs]: cs = contextlib.suppress(AssertionError)\n", None),
        ("        case [cs]: return\n", None),
        ("        case [cs]: raise ValueError\n", ValueError),
        ("        case [cs] if False: pass\n        case _: raise ValueError\n", ValueError),
    ),
)
def test_literal_match_declines_guard_or_body_effects(clause, error):
    source = (
        "import contextlib\ndef outer(x):\n    cs = contextlib.suppress(AssertionError)\n"
        "    match [contextlib.nullcontext()]:\n" + clause + "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<effectful-literal-case>", "exec"), namespace)  # noqa: S102
    if error is None:
        namespace["outer"](1)
    else:
        with pytest.raises(error):
            namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


@pytest.mark.parametrize(
    "prefix,suffix,error",
    (
        ("    if x == 1: return\n", "", None),
        ("    missing()\n", "", NameError),
        ("", "    if x == 1: return\n", None),
        ("", "    missing()\n", NameError),
    ),
)
def test_literal_match_declines_unreachable_assertion_witness(prefix, suffix, error):
    source = (
        "import contextlib\ndef outer(x):\n    cs = contextlib.suppress(AssertionError)\n"
        + prefix
        + "    match [contextlib.nullcontext()]:\n        case [cs]: pass\n"
        + suffix
        + "    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<unreachable-match-witness>", "exec"), namespace)  # noqa: S102
    if error is None:
        namespace["outer"](1)
    else:
        with pytest.raises(error):
            namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


@pytest.mark.parametrize("module_patch", (False, True, "alias"))
def test_literal_match_declines_contextlib_member_monkeypatch(module_patch):
    patch = "contextlib.nullcontext = lambda: contextlib.suppress(AssertionError)\n"
    if module_patch == "alias":
        patch = (
            "alias = contextlib\nalias.nullcontext = lambda: contextlib.suppress(AssertionError)\n"
        )
    source = (
        "import contextlib\n"
        + (patch if module_patch else "")
        + "def outer(x):\n    cs = contextlib.suppress(AssertionError)\n"
        + ("    " + patch if not module_patch else "")
        + "    match [contextlib.nullcontext()]:\n        case [cs]: pass\n"
        + "    with cs:\n        assert x != 1\n"
    )
    import contextlib

    original = contextlib.nullcontext
    try:
        namespace = {}
        exec(compile(source, "<patched-literal-manager>", "exec"), namespace)  # noqa: S102
        namespace["outer"](1)
    finally:
        contextlib.nullcontext = original
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


@pytest.mark.parametrize(
    "prefix,subject,pattern,assertion",
    (
        ("    x = 0\n", "[contextlib.nullcontext()]", "[cs]", "x != 1"),
        ("    import contextlib as x\n", "[contextlib.nullcontext()]", "[cs]", "x != 1"),
        ("    y = 0\n", "[contextlib.nullcontext()]", "[cs]", "y != 1"),
        ("", "[contextlib.nullcontext(), 0]", "[cs, x]", "x != 1"),
        ("", "[contextlib.nullcontext()]", "[cs]", "True"),
    ),
)
def test_literal_match_requires_reachable_assertion_failure(prefix, subject, pattern, assertion):
    source = (
        "import contextlib\ndef outer(x):\n    cs = contextlib.suppress(AssertionError)\n"
        + prefix
        + "    match "
        + subject
        + ":\n        case "
        + pattern
        + ": pass\n"
        + "    with cs:\n        assert "
        + assertion
        + "\n"
    )
    namespace = {}
    exec(compile(source, "<match-failure-witness>", "exec"), namespace)  # noqa: S102
    for value in (0, 1, 2):
        namespace["outer"](value)
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


#: #369 bare-name residual. The filed fixture spells its subject element with a
#: from-import alias (``nullcontext()``) rather than the qualified
#: ``contextlib.nullcontext()`` that #364/#455 accepted. Executed, the assert
#: below the capture fires; the analyzer called it defeated, which is the
#: damaging false-DEAD direction.
#:
#: The carrier spelling is deliberately varied across these rows so the fix
#: cannot be satisfied by looking at the ``with`` instead of the subject: only
#: the *subject element* spelling decided the verdict when this was measured.
FROM_IMPORT_SUBJECT_ROWS = (
    (
        "both the subject and the carrier use from-import aliases",
        (
            "from contextlib import suppress, nullcontext\n\n"
            "def outer(x, flag, helper, items):\n"
            "    subject = [nullcontext()]\n"
            "    with (cs := suppress(AssertionError)):\n"
            "        pass\n"
            "    match subject:\n        case [cs]:\n            pass\n"
            "    with cs:\n        assert x != 1\n"
        ),
    ),
    (
        "a from-import subject beside a qualified carrier",
        (
            "from contextlib import nullcontext\nimport contextlib\n\n"
            "def outer(x, flag, helper, items):\n"
            "    subject = [nullcontext()]\n"
            "    with (cs := contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    match subject:\n        case [cs]:\n            pass\n"
            "    with cs:\n        assert x != 1\n"
        ),
    ),
    (
        "a qualified subject beside a from-import carrier",
        (
            "import contextlib\nfrom contextlib import suppress\n\n"
            "def outer(x, flag, helper, items):\n"
            "    subject = [contextlib.nullcontext()]\n"
            "    with (cs := suppress(AssertionError)):\n"
            "        pass\n"
            "    match subject:\n        case [cs]:\n            pass\n"
            "    with cs:\n        assert x != 1\n"
        ),
    ),
    (
        "a from-import subject with no intervening carrier at all",
        (
            "from contextlib import nullcontext\n\n"
            "def outer(x, flag, helper, items):\n"
            "    subject = [nullcontext()]\n"
            "    match subject:\n        case [cs]:\n            pass\n"
            "    with cs:\n        assert x != 1\n"
        ),
    ),
)


@pytest.mark.parametrize(
    ("label", "source"),
    FROM_IMPORT_SUBJECT_ROWS,
    ids=[row[0] for row in FROM_IMPORT_SUBJECT_ROWS],
)
def test_a_from_import_subject_alias_settles_its_own_selection(label, source):
    """A ``from``-imported name is a written-out subject like any other. (#369)

    ``from contextlib import nullcontext`` binds ``nullcontext`` to
    ``contextlib.nullcontext``, so ``subject = [nullcontext()]`` is exactly the
    one-element literal the literal-subject rule already accepts when it is
    spelled ``contextlib.nullcontext()``.

    It was not accepted, because ``_module_rebinds_name`` counted that very
    import as a rebinding *away* from the module: ``_is_canonical_module_import``
    recognised only ``import <name>``, never ``from M import x``. The gate then
    refused the subject element, the selection proof was abandoned, and a fired
    assert was reported defeated.

    Every row is executed first and the analyzer is checked against that, so no
    row can claim "live" on the strength of the checker's own opinion.
    """
    namespace = {}
    exec(compile(source, "<from-import-subject>", "exec"), namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1, True, namespace.get("helper"), [])
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True, (
        f"{label}: the assert fires under CPython, so the analyzer must report it enforced."
    )


def test_a_from_import_of_an_unexported_name_is_still_a_rebinding():
    """An exported attribute does not establish canonical module identity.

    The shared qualified-builtin gate must reject both from-import spellings;
    the dedicated literal-subject callee proof handles genuine contextlib
    members without weakening the shared module check.
    """
    exported = ast.parse("from contextlib import nullcontext\n").body[0]
    missing = ast.parse("from contextlib import not_a_real_attribute\n").body[0]
    assert _is_canonical_module_import(exported, "nullcontext") is False
    assert _is_canonical_module_import(missing, "not_a_real_attribute") is False


FROM_IMPORT_CARRIER_ROWS = (
    (
        "the filed reproduction: from-import, walrus carrier, named subject",
        (
            "    from contextlib import suppress, nullcontext\n"
            "    subject = [nullcontext()]\n"
            "    with (cs := suppress(AssertionError)):\n"
            "        pass\n"
            "    match subject:\n        case [cs]:\n            pass\n"
        ),
        True,
    ),
    (
        "a from-import in the body decides a directly written subject",
        (
            "    from contextlib import suppress, nullcontext\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    match [nullcontext()]:\n        case [cs]:\n            pass\n"
        ),
        True,
    ),
    (
        "CONTROL a renamed from-import is a rebinding, not a plain import",
        (
            "    from contextlib import suppress, nullcontext as nc\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    match [nc()]:\n        case [cs]:\n            pass\n"
        ),
        False,
    ),
    (
        "CONTROL an alias that even spells the same name is still a rewrite",
        (
            "    from contextlib import nullcontext as nullcontext\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    match [nullcontext()]:\n        case [cs]:\n            pass\n"
        ),
        False,
    ),
    (
        "CONTROL a from-import of another module is not a contextlib import",
        (
            "    from decimal import Decimal\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    match [Decimal()]:\n        case [cs]:\n            pass\n"
        ),
        False,
    ),
    (
        "CONTROL a callee rebound after the from-import is not decided",
        (
            "    from contextlib import suppress, nullcontext\n"
            "    nullcontext = int\n"
            "    cs = contextlib.suppress(AssertionError)\n"
            "    match [nullcontext()]:\n        case [cs]:\n            pass\n"
        ),
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "enforced"),
    FROM_IMPORT_CARRIER_ROWS,
    ids=[row[0] for row in FROM_IMPORT_CARRIER_ROWS],
)
def test_a_from_import_in_the_body_is_a_plain_import_for_this_walk(label, body, enforced):
    """#369: a ``from contextlib import`` in the body is a canonical binding.

    The pre-match walk already let ``import contextlib`` through. ``from
    contextlib import ...`` is a different ``ast`` node, so it fell through to
    ``return False`` and the capture was never decided -- for the issue's own
    reproduction, and for every carrier form. This rows it with the qualified
    spelling on exactly its own terms: the module is ``contextlib`` and nothing
    is renamed, since an ``as`` alias is a rebinding this walk cannot follow.

    Each ``True`` row is executed before the analyzer is consulted, so it
    cannot pass on the checker's own opinion. The ``False`` rows are the
    controls that keep the widened gate from swallowing a genuine rebinding.
    """
    source = "import contextlib\ndef outer(x):\n" + body + "    with cs:\n        assert x != 1\n"
    if enforced:
        namespace = {}
        exec(compile(source, "<from-import-carrier>", "exec"), namespace)  # noqa: S102
        try:
            namespace["outer"](1)
        except AssertionError:
            fired = True
        else:
            fired = False
        assert fired is True, f"{label}: the captured plain manager must leave the assert live"
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is enforced


@pytest.mark.parametrize(
    "imports",
    (
        # #369. The `from` spelling is the filed reproduction, and it is the
        # one the callee-intact rule exists for: `_name_is_rebound_away_from_module`
        # answers the `builtins.attr` question, so it counted this ordinary
        # import as a rebinding and declined every subject element.
        "from contextlib import suppress, nullcontext",
        "import contextlib",
    ),
    ids=("from-import", "qualified"),
)
@pytest.mark.parametrize("named", (False, True), ids=("literal-subject", "named-subject"))
def test_literal_match_supersedes_a_carried_suppressor(imports, named):
    """#369: a decided capture holds its captured value, not the carried one.

    The carried ``suppress(AssertionError)`` is installed by a plain store and
    then *superseded* by the capture. When the capture is decided, the header
    CPython enters is the captured ``nullcontext``, so the carried suppressor
    is no longer in force and the assert is live. Answering ``False`` here is
    the damaging direction for #369: a live contract is dropped from the
    sentinel's view.

    Every row is executed before the analyzer is consulted, so no row can pass
    by being vacuous.
    """
    qualified = imports == "import contextlib"
    manager = "contextlib.nullcontext()" if qualified else "nullcontext()"
    preamble = "import contextlib\n" if not qualified else ""
    setup = "    subject = [" + manager + "]\n" if named else ""
    source = (
        preamble
        + imports
        + "\ndef outer(x):\n"
        + "    cs = contextlib.suppress(AssertionError)\n"
        + setup
        + "    match "
        + ("subject" if named else "[" + manager + "]")
        + ":\n        case [cs]: pass\n    with cs:\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<literal-supersedes-carried>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1)
    except AssertionError:
        fired = True
    else:
        fired = False
    assert fired is True, "the captured plain manager must leave the assert live"
    tree = ast.parse(source)
    function = tree.body[-1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True


@pytest.mark.parametrize(
    "prefix",
    (
        # The decline must survive a genuine rebind. A parameter of the same
        # name replaces the import for the whole call, so the subject element
        # is not `contextlib.nullcontext` and the proof must not be made.
        "def outer(nullcontext, x):\n    cs = contextlib.suppress(AssertionError)\n",
        "def outer(x):\n    cs = contextlib.suppress(AssertionError)\n    nullcontext = int\n",
    ),
    ids=("parameter", "local-store"),
)
def test_literal_match_declines_a_rebound_callee(prefix):
    """#369: fixing the `from` spelling must not accept a rebound callee.

    These are the shapes the rule is *not* allowed to decide. They are held to
    the conservative verdict rather than to a runtime outcome, because the
    capture is left undecidable -- which is the safe direction.
    """
    source = (
        "import contextlib\n"
        + prefix
        + "    match [nullcontext()]:\n        case [cs]: pass\n"
        + "    with cs:\n        assert x != 1\n"
    )
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


@pytest.mark.parametrize("local", (False, True))
def test_from_import_attribute_is_not_a_canonical_builtin_module(local):
    import contextlib

    source = (
        "import contextlib\n"
        + (
            ""
            if local
            else "from contextlib import nullcontext as builtins\nbuiltins.int = contextlib.nullcontext\n"
        )
        + "def outer(x):\n"
        + (
            "    from contextlib import nullcontext as builtins\n    builtins.int = contextlib.nullcontext\n"
            if local
            else ""
        )
        + "    cs = builtins.int()\n    with cs:\n        assert x != 1\n"
    )
    had_attribute = hasattr(contextlib.nullcontext, "int")
    prior = getattr(contextlib.nullcontext, "int", None)
    try:
        namespace = {}
        exec(compile(source, "<from-import-module-identity>", "exec"), namespace)  # noqa: S102
        with pytest.raises(AssertionError):
            namespace["outer"](1)
    finally:
        if had_attribute:
            contextlib.nullcontext.int = prior
        else:
            del contextlib.nullcontext.int
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is True


@pytest.mark.parametrize(
    "body,error",
    (
        (
            "    subject = [nullcontext()]\n    from contextlib import nullcontext\n    match subject:\n        case [cs]: pass\n",
            UnboundLocalError,
        ),
        (
            "    from .contextlib import nullcontext\n    match [nullcontext()]:\n        case [cs]: pass\n",
            ImportError,
        ),
    ),
)
def test_literal_subject_callee_import_must_be_absolute_and_precede_call(body, error):
    source = (
        "import contextlib\ndef outer(x):\n    cs = contextlib.suppress(AssertionError)\n"
        + body
        + "    with cs:\n        assert x != 1\n"
    )
    namespace = {"__name__": "callee_order_fixture", "__package__": ""}
    exec(compile(source, "<callee-import-order>", "exec"), namespace)  # noqa: S102
    with pytest.raises(error):
        namespace["outer"](1)
    tree = ast.parse(source)
    function = tree.body[1]
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


@pytest.mark.parametrize("parameter", (False, True))
def test_literal_subject_callee_declines_enclosing_shadow(parameter):
    source = (
        "import contextlib\nfrom contextlib import nullcontext\n"
        + (
            "def parent(nullcontext):\n"
            if parameter
            else "def parent():\n    nullcontext = lambda: contextlib.suppress(AssertionError)\n"
        )
        + "    def outer(x):\n        cs = contextlib.suppress(AssertionError)\n"
        + "        match [nullcontext()]:\n            case [cs]: pass\n"
        + "        with cs:\n            assert x != 1\n    return outer\n"
    )
    namespace = {}
    exec(compile(source, "<enclosing-callee-shadow>", "exec"), namespace)  # noqa: S102
    import contextlib

    function = (
        namespace["parent"](lambda: contextlib.suppress(AssertionError))
        if parameter
        else namespace["parent"]()
    )
    function(1)
    tree = ast.parse(source)
    outer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    target = next(node for node in ast.walk(outer) if isinstance(node, ast.Assert))
    assert _is_enforced(outer, target, tree) is False


def test_literal_subject_declines_modified_from_import_manager_class():
    import contextlib

    source = (
        "import contextlib\nfrom contextlib import nullcontext\n"
        "nullcontext.__exit__ = lambda *args: True\n"
        "def outer(x):\n    cs = contextlib.suppress(AssertionError)\n"
        "    match [nullcontext()]:\n        case [cs]: pass\n"
        "    with cs:\n        assert x != 1\n"
    )
    original = contextlib.nullcontext.__exit__
    try:
        namespace = {}
        exec(compile(source, "<modified-from-import-manager>", "exec"), namespace)  # noqa: S102
        namespace["outer"](1)
    finally:
        contextlib.nullcontext.__exit__ = original
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    target = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, target, tree) is False


@pytest.mark.parametrize(
    ("value", "live"),
    [
        ("lambda: None", False),
        ("1", False),
        ("[]", False),
        ("{}", False),
        ("()", False),
        ("{1}", False),
        ("json.live", True),
    ],
)
def test_walrus_literal_entry_declines_shadowed_module_attributes(value, live):
    source = (
        "import contextlib\n"
        "class Holder:\n    live = contextlib.nullcontext()\n"
        "def outer(x):\n    json = Holder()\n"
        f"    with (cs := {value}):\n        assert x != 1\n"
    )
    namespace = {}
    exec(compile(source, "<walrus-literal-entry>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1)
    except AssertionError:
        observed_live = True
    except TypeError:
        observed_live = False
    else:
        observed_live = False
    assert observed_live is live
    tree = ast.parse(source)
    function = tree.body[-1]
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert))
    assert _is_enforced(function, assertion, tree) is live


#: #445. The rows above settle both polarities of the `elif` link when the
#: carried binding is spelled as a plain `cs = contextlib.nullcontext()`
#: assignment. The filed shape for this issue reaches the same walk by a
#: different route -- the carrier arrives through a `with` header's assignment
#: expression -- and there the polarity flipped:
#:
#:     with (cs := contextlib.suppress(AssertionError)):
#:         pass
#:     if x:
#:         pass
#:     elif True:
#:         cs = contextlib.nullcontext()
#:     with cs:
#:         assert x != 1
#:
#: `assert x != 1` can only fail at `x == 1`, and that is exactly the call that
#: takes the `if x:` arm, so the `elif` never runs and `cs` is still the
#: `suppress(AssertionError)` the walrus bound. Executed on CPython 3.12 the
#: assert never fires on either call, so the header is genuinely defeated --
#: yet the analyzer reported it *enforced*, which certifies a swallowed sentinel
#: as load-bearing.
#:
#: The reason is symmetric with the live rows above and worth stating plainly:
#: the `elif` arm is demoted correctly in both directions, because an `elif`
#: runs only when every test above it failed. What the demotion must not do is
#: *retire the carried binding* on the calls that skip the arm. In the live
#: rows the carried value is a plain manager, so declining the arm's suppressor
#: is enough. Here the carried value is itself a suppressor, and declining the
#: arm's plain manager has to leave that suppressor standing.
#:
#: The last row is the converse of the first and decides whether the repair is
#: correct rather than merely cautious: a carried `suppress(ValueError)` does
#: *not* catch `AssertionError`, so a call that skips the suppressing arm lets
#: the failure escape and the header is live after all.
#: Every row is executed across the swept domain by
#: :func:`_assert_suppression_contract` before the analyzer's verdict is
#: compared, so CPython decides each row rather than this table.
CARRIED_ELIF_SUPPRESSOR_SHAPES = (
    (
        "a carried walrus suppressor survives an elif arm that binds a plain manager",
        "suppress(AssertionError)",
        "nullcontext",
        False,
    ),
    (
        "a carried walrus base-exception suppressor survives an elif binding a plain manager",
        "suppress(BaseException)",
        "nullcontext",
        False,
    ),
    (
        "a carried walrus exception suppressor survives an elif binding a plain manager",
        "suppress(Exception)",
        "nullcontext",
        False,
    ),
    # The converse control. The carried value does not swallow, so the call
    # that skips the suppressing arm really does let the assert fire.
    (
        "a carried walrus value-error suppressor leaves the elif header live",
        "suppress(ValueError)",
        "suppress(AssertionError)",
        True,
    ),
    (
        "a carried walrus key-error suppressor leaves the elif header live",
        "suppress(KeyError)",
        "nullcontext",
        True,
    ),
    # Both arms swallow: every call that reaches the header is defeated, and
    # the name holds a suppressor whichever arm ran.
    (
        "a carried walrus assertion suppressor plus an elif assertion suppressor is defeated",
        "suppress(AssertionError)",
        "suppress(AssertionError)",
        False,
    ),
)
