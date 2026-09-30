# Review task: #419-on-#412 conflict resolution at `aaf31e5`

You did not author this change. Do not approve it on your own reasoning as
author-adjacent; produce **findings**. The lead integrator merges, never you.

## Absolute paths

- Repository: `/home/agent/poke-harness/pokemon`
- Worktree to review, **read-only**: `/home/agent/poke-harness/.scratch/simint`
- HEAD there: `aaf31e5` (detached)
- Interpreter to use: `/home/agent/poke-harness/pokemon/.venv/bin/python`

**Do not** edit, branch, commit, push, merge, or touch
`/home/agent/poke-harness/pokemon`. That checkout is protected and has staged
files that must not change. Put any throwaway probe in `/tmp`, never in the
repo.

## What the change is

The repo is a test-integrity sentinel suite: it proves assertions in the test
suite are still load-bearing. An assert that cannot fail is a defect.
The support module is `tests/_timed_menu_milestone_sentinel_support.py`.

Two long-lived PR stacks both edit the same function, `_entry_is_dead`:

- PR **#412** (head `6f388cf`) adds builtin-constructor analysis:
  `cs = int()` must be reported **dead**, because `int` has no `__enter__`.
- PR **#419** (head `318d9a2c`) adds starred-target analysis:
  `*cs, = (...)` and `a, *cs = (...)` bind a **list**, which has no
  `__enter__`, so the assert under a later `with cs:` is unreachable.

Merging #419 on top of #412 conflicted inside `_entry_is_dead`. Commit
`aaf31e5` is the resolution and is expected to preserve **both** rules.

## Steps

1. Read the resolution:
   `git -C /home/agent/poke-harness/.scratch/simint show aaf31e5 -- tests/_timed_menu_milestone_sentinel_support.py`
2. Confirm the `_binds_starred_target` block — including its
   `kinds.add("list")` and its `continue` — is present in `_entry_is_dead`'s
   store loop, positioned **after** #412's `_builtin_constructor_kind`
   analysis and **before** the `_binds_element_of` destructuring decline.
3. Confirm #419's helpers `_binds_starred_target` and `_starred_target_names`
   survive, and that #412's body was not truncated. These must all still be
   present and coherent:
   `_unanimous_non_enterable_element`, `_builtin_constructor_kind`,
   `_loop_target_bindings`, `_carrier_may_have_been_superseded`.
4. **Probe behaviour independently.** Do not trust any table below; recompute
   it. Write your own probe in `/tmp`. Put the repo root and its `tests/`
   directory on `sys.path`, then:
   `from _timed_menu_milestone_sentinel_support import _is_enforced`

   Fixture template — the walrus rebind is essential, do not simplify it:

   ```
   def outer(x, flag, helper):
       import contextlib
       from contextlib import suppress, nullcontext
       with (cs := contextlib.suppress(AssertionError)):
           assert x != 1
       <REBIND LINE, 4-space indent>
       with cs:
           assert x != 1
   ```

   Parse with `ast`, find the two `ast.Assert` nodes, call
   `_is_enforced(fn, node, tree)`.

   Expected **second**-assert verdicts:

   | rebind line | expected |
   |---|---|
   | `*cs, = (contextlib.suppress(AssertionError),)` | `False` |
   | `first, *cs = (1, contextlib.suppress(AssertionError))` | `False` |
   | `a, *cs = (contextlib.suppress(AssertionError), 2)` | `False` |
   | `*cs, = (contextlib.nullcontext(),)` | `False` |
   | `*cs, = (1,)` | `False` |
   | `cs, other = (contextlib.nullcontext(), 2)` — CONTROL | `True` |
   | `cs = int()` | `False` |
   | `cs = nullcontext()` — CONTROL | `True` |
   | `cs = suppress()` | `False` |

5. **Verify each verdict against real CPython**, not just the analyzer. For
   every row actually execute the fixture and record whether the second assert
   is reachable: does `with cs:` raise `TypeError`, and does the
   `AssertionError` escape? Report any row where analyzer and interpreter
   disagree. Direction matters — analyzer says `enforced` while the
   interpreter swallows is a **false-live** and is the damaging direction. The
   reverse is milder but still report it.
6. **Check for over-correction.** Does the new rule wrongly report any
   genuinely live assert as dead? Confirm the non-starred destructuring
   control above still reads `True`.
7. Confirm the resolution introduced no syntax or import problems, and that
   `python -m ruff check` and `python -m ruff format --check` pass on the two
   changed test files.

## Lead's own results, for you to try to refute

- Direct probe of the 9 rows above: **9/9 as expected**.
- Sentinel + milestone suite on this exact tree: **706 tests, 0 failures,
  0 errors, 0 skipped** (JUnit `/tmp/simint_412_424_419.xml`).
- `ruff check` and `ruff format --check`: clean.

Treat all of that as a claim to refute, not as evidence. If your probe
disagrees, the probe wins and the defect is mine.

## Deliverable — return exactly this

- `VERDICT:` one of `APPROVE` / `REJECT` / `APPROVE-WITH-FINDINGS`
- A table: each of the 9 rows, analyzer verdict, interpreter verdict, agree?
- Any defect, with the exact line number in the resolved file
- Whether #412's rules and #419's rules genuinely both survive
- An explicit statement of anything you could **not** verify

If you did not receive this task text, say so and stop. Do not self-target from
any other board or pointer file; those are known to be stale.
