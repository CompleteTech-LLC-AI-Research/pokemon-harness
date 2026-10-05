"""Pattern-match capture and guaranteed bindings.

Actual test functions and literal cases from the original collector.
"""

import ast

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced

#: #364 / #369. A ``match`` subject that is *written out* settles on its own
#: whether a sequence clause is selected, exactly as ``for _ in []:`` settles
#: its own emptiness. Before the repair, ``_capture_always_binds`` answered
#: "may not have run" for every refutable sequence capture, so the carried
#: walrus suppressor survived into a ``with cs:`` that CPython happily
#: entered -- a live assert reported **defeated**.
#:
#: Two spellings are covered, because they are two different sources. #364
#: writes the container in the subject position; #369 binds it to a name a
#: statement earlier and matches on the name.
#:
#: ``runtime`` is measured, not declared. ``_execute_literal_subject`` reports
#: what became of the assert when the fixture is executed, so a row cannot
#: claim "live" without the interpreter agreeing -- which is the check the
#: older capture tables above lacked.
LITERAL_SUBJECT_ROWS = (
    (
        "a written-out one-element subject selects a one-element pattern",
        "    match [contextlib.nullcontext()]:\n        case [cs]:\n            pass\n",
        "live",
        True,
    ),
    (
        "a named one-element list subject selects a one-element pattern",
        (
            "    subject = [contextlib.nullcontext()]\n"
            "    match subject:\n        case [cs]:\n            pass\n"
        ),
        "live",
        True,
    ),
    (
        "a named one-element tuple subject selects a one-element pattern",
        (
            "    subject = (contextlib.nullcontext(),)\n"
            "    match subject:\n        case [cs]:\n            pass\n"
        ),
        "live",
        True,
    ),
    # The matching is decided per element, not on length alone: a constant
    # value pattern beside the capture still selects.
    (
        "a constant sibling pattern is compared, not just counted",
        "    match [1, contextlib.nullcontext()]:\n        case [1, cs]:\n            pass\n",
        "live",
        True,
    ),
    # --- Controls. Each fails for its own distinct reason, so a rule widened
    # past the written-out case is caught rather than shipped.
    (
        "CONTROL an empty subject cannot match a one-element pattern",
        "    match []:\n        case [cs]:\n            pass\n",
        "swallowed",
        False,
    ),
    (
        "CONTROL a two-element subject cannot match a one-element pattern",
        "    match [contextlib.nullcontext(), 2]:\n        case [cs]:\n            pass\n",
        "swallowed",
        False,
    ),
    (
        "CONTROL a one-element subject cannot match a two-element pattern",
        "    match [contextlib.nullcontext()]:\n        case [cs, other]:\n            pass\n",
        "swallowed",
        False,
    ),
    (
        "CONTROL a starred element leaves the length undecidable",
        "    match [*contextlib.nullcontext()]:\n        case [cs]:\n            pass\n",
        "unreachable:TypeError",
        False,
    ),
    (
        "CONTROL an opaque subject is still undecidable",
        "    match helper():\n        case [cs]:\n            pass\n",
        "swallowed",
        False,
    ),
    (
        "CONTROL a name bound to a non-literal is still undecidable",
        "    subject = helper()\n    match subject:\n        case [cs]:\n            pass\n",
        "swallowed",
        False,
    ),
    (
        "CONTROL a later rebinding of the name wins the subject",
        (
            "    subject = [contextlib.nullcontext()]\n"
            "    subject = helper()\n"
            "    match subject:\n        case [cs]:\n            pass\n"
        ),
        "swallowed",
        False,
    ),
    (
        "CONTROL a constant sibling that does not match leaves the suppressor",
        "    match [2, contextlib.nullcontext()]:\n        case [1, cs]:\n            pass\n",
        "swallowed",
        False,
    ),
    (
        "CONTROL a value pattern needing an unreadable value refuses",
        (
            "    match [one, contextlib.nullcontext()]:\n"
            "        case [target.one, cs]:\n            pass\n"
        ),
        "live",
        False,
    ),
    (
        "a named subject survives an inert statement before the match",
        (
            "    subject = [contextlib.nullcontext()]\n"
            "    with contextlib.nullcontext():\n"
            "        pass\n"
            "    match subject:\n        case [cs]:\n            pass\n"
        ),
        "live",
        True,
    ),
    (
        "a named subject survives the carried carrier before the match",
        (
            "    subject = [contextlib.nullcontext()]\n"
            "    with (cs := contextlib.suppress(AssertionError)):\n"
            "        pass\n"
            "    match subject:\n        case [cs]:\n            pass\n"
        ),
        "live",
        True,
    ),
    (
        "CONTROL an intervening statement that rebinds the subject refuses",
        (
            "    subject = [contextlib.nullcontext()]\n"
            "    with contextlib.nullcontext():\n"
            "        subject = []\n"
            "    match subject:\n        case [cs]:\n            pass\n"
        ),
        "swallowed",
        False,
    ),
    (
        "CONTROL a with target that names the subject refuses",
        (
            "    subject = [contextlib.nullcontext()]\n"
            "    with contextlib.nullcontext() as subject:\n"
            "        pass\n"
            "    match subject:\n        case [cs]:\n            pass\n"
        ),
        "swallowed",
        False,
    ),
)


def _literal_subject_source(body):
    return (
        "import contextlib\n"
        "one = 1\n"
        "class target:\n"
        "    one = 1\n"
        "def helper():\n"
        "    return 1\n"
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        pass\n" + body + "    with cs:\n        assert x != 1\n"
    )


def _execute_literal_subject(source):
    """Run a literal-subject fixture and report what became of the assert."""
    namespace = {}
    exec(compile(source, "<literal-subject>", "exec"), namespace)  # noqa: S102
    try:
        namespace["outer"](1, True, namespace["helper"])
    except AssertionError:
        return "live"
    except TypeError as error:
        return f"unreachable:{type(error).__name__}"
    return "swallowed"


@pytest.mark.parametrize(
    ("label", "body", "runtime", "verdict"),
    LITERAL_SUBJECT_ROWS,
    ids=[row[0] for row in LITERAL_SUBJECT_ROWS],
)
def test_a_written_out_match_subject_decides_its_own_selection(label, body, runtime, verdict):
    """A ``match`` on a written-out container is decidable on its own. (#364, #369)

    The refutable-capture rule answers a question about a *runtime* subject, so
    it declines every sequence capture. That is right for ``match helper():``
    and wrong for ``match [contextlib.nullcontext()]:``: in the second the
    subject is written out, its length is known, and the clause is selected
    for certain. Executed, the assert below the capture is **live**; before
    the repair the analyzer called it defeated, which is the damaging
    false-DEAD direction.

    Every row is executed first and the analyzer is checked against that, so a
    row cannot claim "live" on the strength of the checker's own opinion. The
    controls pin the refusal directions: a length that is not written down (a
    starred element), a subject that is not a container literal, a name rebound
    before the match, and a sibling pattern whose value the rule cannot read
    must all keep reporting defeated.
    """
    source = _literal_subject_source(body)
    observed = _execute_literal_subject(source)
    assert observed == runtime, (
        f"{label}: CPython produced {observed!r}, the row claims {runtime!r}. "
        f"The table is stale, not the analyzer."
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, asserts[-1], tree)]
    assert results == [verdict], (
        f"{label}: analyzer says {results}, expected {[verdict]}. CPython produced {observed!r}."
    )


#: #378 / #364 adjacency. A zero-iteration loop makes its body's stores
#: unreachable, which is a *different* question from whether a ``match`` clause
#: is selected. Both repairs live in the same neighbourhood of the walk, so
#: this row pins that neither answers for the other: a capture inside a loop
#: that cannot run must not be treated as a decided capture.
MATCH_IN_UNREACHABLE_LOOP_ROWS = (
    (
        "a decided capture is not reached through a zero-iteration loop",
        (
            "    for _ in []:\n"
            "        match [contextlib.nullcontext()]:\n"
            "            case [cs]:\n"
            "                pass\n"
        ),
        "swallowed",
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "runtime", "verdict"),
    MATCH_IN_UNREACHABLE_LOOP_ROWS,
    ids=[row[0] for row in MATCH_IN_UNREACHABLE_LOOP_ROWS],
)
def test_a_capture_inside_a_zero_iteration_loop_is_not_decided(label, body, runtime, verdict):
    """A capture that cannot run is not a capture that always binds.

    The literal-subject rule in #364/#369 answers whether a *selected* clause
    binds its name. It says nothing about whether control ever reaches the
    ``match`` at all, and a ``for _ in []:`` body never does. The carried
    suppressor therefore survives, and executed at ``x=1`` the assert is
    swallowed -- so the verdict must stay defeated.
    """
    source = _literal_subject_source(body)
    observed = _execute_literal_subject(source)
    assert observed == runtime, (
        f"{label}: CPython produced {observed!r}, the row claims {runtime!r}."
    )
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    results = [_is_enforced(function, asserts[-1], tree)]
    assert results == [verdict], (
        f"{label}: analyzer says {results}, expected {[verdict]}. A capture in "
        f"a body that cannot run binds nothing."
    )


#: Two over-fix guards. The capture rules must not retire a binding they did
#: not make, in either direction -- the first is a *missed* retirement and the
#: second is a *spurious* one, and both were live bugs while this was written.
MATCH_CAPTURE_SCOPE_ROWS = (
    (
        "a capture in a nested function does not retire the outer binding",
        ("    def inner():\n        match flag:\n            case [cs]:\n                pass\n"),
        [False, False],
    ),
    # #350. A class body is a namespace too, and the walk that finds captures
    # used to stop at nested `def`/`lambda` but not at `ClassDef`. So a capture
    # written in a class body retired the *enclosing function's* local and
    # reported the assert under the later `with cs:` as enforced.
    #
    # The subject is `[1]`, so the capture binds the integer 1: entering it
    # raises TypeError before the assert. The nested-function row above is the
    # control -- it was already correct, so the two together prove the class
    # row is not passing for the same reason as a pre-existing decline.
    (
        "a capture in a nested class body does not retire the outer binding",
        ("    class Inner:\n        match flag:\n            case [cs]:\n                pass\n"),
        [False, False],
    ),
    (
        "a capture of a different name does not retire this one",
        "    match flag:\n        case [other]:\n            pass\n",
        [False, False],
    ),
    (
        "a capture after the header does not retire it retroactively",
        None,  # the capture is appended after the header instead
        [False, False],
    ),
    (
        "a capture before the header does retire it",
        # An irrefutable capture is used here so the row still demonstrates a
        # *retirement* under #342. A refutable capture before the header no
        # longer retires anything, which is the defect the neighbouring
        # `test_a_refutable_capture_does_not_retire_a_binding_it_never_made`
        # covers; keeping the refutable spelling in this row would make it a
        # second copy of that test rather than a check on ordering.
        "    match flag:\n        case _ as cs:\n            pass\n",
        [False, True],
    ),
)


#: A capture is not a *statement*, so nothing in the block that holds a header
#: can be found by looking for a store statement -- but a header written inside
#: the capturing ``case`` body is reached only on the path where that clause was
#: selected, so it has had the capture run by then. These rows are what makes
#: that block in ``_bindings_before`` load-bearing rather than dead: without it
#: the capture is invisible to a header in the same block.
#:
#: All three report the second assert as defeated rather than enforced. Under
#: #342 a refutable capture retires the alias only on the path it is selected,
#: and a header written inside the clause body is on exactly that path -- where
#: the captured value is a plain subject, not a context manager, so entering it
#: raises and the assert is unreachable. The path that does *not* select the
#: clause never reaches the header at all, so there is no live reading of it
#: that the header has to preserve.
MATCH_CAPTURE_OWNS_NESTED_HEADER_ROWS = (
    (
        "a header in the capturing clause body reads the capture",
        (
            "    match flag:\n"
            "        case [cs]:\n"
            "            with cs:\n"
            "                assert x != 1\n"
        ),
        False,
    ),
    (
        "a header in a second clause body reads that clause's capture",
        (
            "    match flag:\n"
            "        case [other]:\n"
            "            pass\n"
            "        case [cs]:\n"
            "            with cs:\n"
            "                assert x != 1\n"
        ),
        False,
    ),
    (
        "a header in a clause that captures nothing still reads the suppressor",
        (
            "    match flag:\n"
            "        case [other]:\n"
            "            with cs:\n"
            "                assert x != 1\n"
        ),
        False,
    ),
)


@pytest.mark.parametrize(
    ("label", "body", "enforced"),
    MATCH_CAPTURE_OWNS_NESTED_HEADER_ROWS,
    ids=[row[0] for row in MATCH_CAPTURE_OWNS_NESTED_HEADER_ROWS],
)
def test_a_capture_reaches_a_header_nested_in_its_own_clause_body(label, body, enforced):
    """The capture is not a store *statement*, so the block holding a header
    sees no store at all and would fall back to the carried suppressor.

    Every row here is read as ``defeated``, and that is the point. A
    *refutable* capture cannot be decided from the source: the same program
    admits a subject that selects the capture -- leaving a real, possibly
    live, context manager at the nested header -- and a subject that does not,
    which leaves the carried suppressor in force and swallows the assert.
    Since this module answers a source-level question, one admitting subject
    is enough to refuse to certify the assert, and ``defeated`` is the safe
    direction (#308 criterion 1).

    So a header inside the capturing clause body is *not* promoted back to
    ``enforced`` just because the clause was written to capture, and a header
    in a non-capturing clause is not reported swallowed for the same reason.
    Both are ``defeated`` because the capture may not have run.
    """
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + body
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert len(asserts) == 2, f"{label}: fixture declared {len(asserts)} asserts, expected 2"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == [False, enforced], (
        f"{label}: expected verdicts [False, {enforced}], got {results}. "
        f"The capture binds `cs` before the nested header is reached."
    )


@pytest.mark.parametrize(
    ("label", "extra", "expected"),
    MATCH_CAPTURE_SCOPE_ROWS,
    ids=[row[0] for row in MATCH_CAPTURE_SCOPE_ROWS],
)
def test_a_match_capture_respects_its_own_scope_and_position(label, extra, expected):
    """A capture retires the carried suppressor, and only where it really binds.

    A ``match`` inside a nested ``def`` binds in *that* scope, so it must not
    retire the outer function's name -- that would report a swallowed assert as
    live. A capture of some other name must not retire this one. And a capture
    written *after* the ``with`` header cannot have run when the header is
    read, so it must not retire it either.
    """
    capture_after = (
        "    match flag:\n        case [cs]:\n            pass\n" if extra is None else extra
    )
    after = "    match flag:\n        case [cs]:\n            pass\n" if extra is None else ""
    source = (
        "def outer(x, flag, helper):\n"
        "    import contextlib\n"
        "    from contextlib import suppress, nullcontext\n"
        "    import pytest\n"
        "    with (cs := contextlib.suppress(AssertionError)):\n"
        "        assert x != 1\n" + capture_after + "    with cs:\n        assert x != 1\n" + after
    )
    tree = ast.parse(source)
    function = tree.body[0]
    asserts = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert asserts, f"{label}: fixture declared no assert to check"
    results = [_is_enforced(function, node, tree) for node in asserts]
    assert results == expected, f"{label}: expected verdicts {expected}, got {results}."
