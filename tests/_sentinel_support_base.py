# Shared-namespace fragment generated from the merged monolith.
# ruff: noqa: F401

"""Structural sentinels that keep #261/#270 assertions load-bearing (#270).

``tests/test_timed_menu_milestones.py`` carries the frame-bound retention
contract, but two of its assertions can be deleted or relaxed without any
behavioural test noticing:

* ``assert_not_deadline_truncated(record)`` is the #261 guard. It is *correct*
  and it does fire when the wall clock wins, but nothing checks that the call
  is still wired into ``run_owner``. Rewriting ``if clock_step == 0.0:`` to
  ``if False:`` leaves the whole file green (see #270 Finding 1).
* Four sites assert an exact call count (``== 300`` / ``== 271``). Relaxing any
  of them to ``>=`` turns the retention contract into "at least one call"
  without failing (see #270 Finding 2).

Neither gap is observable from the *return value* of a passing run, so a
behavioural assertion cannot catch it -- the same reasoning that made #271 pin
the clock read by inspecting source. These helpers therefore parse the test
module's AST and assert on structure. They are the backstop for structure; the
behavioural assertions remain the load-bearing contract.

The structural checks ask whether a pinned assertion is *enforced*, not merely
*present*. Presence alone is satisfiable by an assertion wrapped in
``try/except AssertionError: pass``, which leaves the node in the AST, leaves
the comparison in place, and leaves the owning test unable to fail (#280).
``_is_enforced`` is the shared answer to that question and is used by both the
teeth check and the count check.
"""

# GENERATED_FRAGMENT_IMPORT_GUARD
if __name__ == "tests._sentinel_support_base":
    raise ImportError(
        "tests._sentinel_support_base is a fragment; import "
        "tests._timed_menu_milestone_sentinel_support instead."
    )


import ast
import copy
import inspect

import tests.test_timed_menu_milestones as milestones

#: Dotted paths whose call is a suppression context. Matched by *resolved*
#: name rather than by spelling, so the qualified, from-import and both alias
#: forms all collapse to the same value before the comparison. Matching one
#: concrete spelling and leaving the family open is what #288 did with
#: ``except*``.
SUPPRESSING_CONTEXTS = ("contextlib.suppress", "asyncio.suppress")

#: The *last component* of each suppressing path -- ``suppress`` for both
#: entries in ``SUPPRESSING_CONTEXTS``. This is the set of bare names that can
#: stand in for a suppressor whose binding the enclosing function cannot see
#: (see ``_unreadable_suppressor``).
#:
#: Taking every component of the dotted path would also admit ``contextlib``
#: and ``asyncio``. Those are module names, not suppressors -- ``with
#: contextlib:`` suppresses nothing -- so including them would report live
#: asserts as dead on any code that uses a module object as a context manager.
#: The suppressing callable is always the final component, so that component is
#: the only spelling that can stand in for one.
SUPPRESSOR_SPELLINGS = frozenset(dotted.rsplit(".", 1)[-1] for dotted in SUPPRESSING_CONTEXTS)

#: Sentinel recorded for a name bound to *more than one* readable suppressor, so
#: that which one applies depends on the runtime path taken.
#:
#: #308 criterion 1 requires such a name to be treated as unreadable and
#: reported as a defeat, rather than resolved by source order. It is a distinct
#: object rather than a boolean so that the ambiguity can be told apart from a
#: known alias when the exception names are inspected -- an ambiguous binding
#: suppresses *any* exception, because the rule cannot know which target was
#: applied.
AMBIGUOUS_SUPPRESSOR = object()

#: Sentinel recording that a name *is* bound at this position and deliberately
#: carries something that is not a suppressor.
#:
#: #367. `_assigned_suppressors` records a name only when it resolves to a
#: suppressor, so "carries a `nullcontext`" and "is not bound here" both fall
#: out as a missing key. Those are different answers: the second inherits the
#: previous position's binding, which resurrects a superseded suppressor. The
#: marker separates them.
_NOT_A_SUPPRESSOR = object()

#: Sentinel for a suppressor reached through ``name.__enter__()``. The dunder
#: is ``pass`` on every suppressor, so the entry raises ``TypeError`` before the
#: body runs whatever exception list it was built with. The argument is
#: therefore irrelevant and cannot be read off the binding, so the shape gets
#: its own marker instead of being classified from the suppressor's arguments.
LOUD_DUNDER = object()

#: How many links of alias chain :func:`_deref_alias` will follow before it
#: gives up and reports the value unreadable. A chain that resolves to a
#: suppressor is short in practice, so the bound is generous; it exists so a
#: cycle (``a = b; b = a``) terminates instead of recursing.
_ALIAS_CHAIN_LIMIT = 32

#: A store that binds a name without a right-hand side this module can read
#: (#359). It is a distinct marker rather than ``None`` because ``None`` means
#: "resolved, and the value is not a suppressor" -- a store that *supersedes* a
#: carried one and retires it. Collapsing the two is what made a `for` target
#: invisible and left a swallowed assert reported as live.
UNREADABLE_VALUE = object()

#: Internal "this target does not bind `name`" signal, distinct from a value.
_NO_MATCH = object()

#: Dotted paths whose call turns a caught exception into a *pass*. This is a
#: different mechanism from ``SUPPRESSING_CONTEXTS`` and the distinction is
#: load-bearing, so the two sets stay separate rather than being merged:
#:
#: * ``suppress`` swallows the failure silently -- the test still passes and
#:   nothing is recorded.
#: * ``raises`` *asserts* the failure happened. ``with
#:   pytest.raises(AssertionError): assert 1 == 2`` therefore raises the
#:   AssertionError, ``pytest.raises`` catches it, finds the expected type, and
#:   the block ends normally. The test goes green on a failing assert, which is
#:   the same present-but-dead contract as the other shapes in this family.
#:
#: Measured on the pinned file, ``pytest.raises`` wraps asserts 5 times and
#: never once for ``AssertionError``: the arguments there are ``RuntimeError``,
#: ``BaseExceptionGroup``, and tuples of ``TypeError``/``ValueError``/
#: ``KeyError``/``RuntimeError``, none of which catch an ``assert``. Adding this
#: set therefore drops 0 of the 143 real asserts.
ASSERTION_CAPTURING_CONTEXTS = ("pytest.raises",)

_OPERATORS = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
}
