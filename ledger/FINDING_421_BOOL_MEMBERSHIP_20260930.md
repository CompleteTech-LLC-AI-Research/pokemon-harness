# Finding — PR #421 head `165b5b3d` raises TypeError on every non-`ast.Assign` store

Date: 2026-09-30
Found by: lead integrator, while simulating #421 onto the #412+#424+#419 tree
Severity: **blocking**. Introduced by `165b5b3d`. Not present in `318d9a2c` (#419).
Status: **not fixed upstream. Not pushed. No PR comment posted yet.**

## The defect

`tests/_timed_menu_milestone_sentinel_support.py`, `_binds_starred_target`:

```python
 def _binds_starred_target(statement, name):
     if not isinstance(statement, ast.Assign):
-        return name in _starred_names_in_loop_target(statement, name)
+        return _starred_names_in_loop_target(statement, name)
     return any(
         isinstance(target, (ast.Tuple, ast.List)) and name in _starred_target_names([target])
         for target in statement.targets
     )
```

`_starred_names_in_loop_target` already **returns a bool** — its last line is:

```python
     return name in _starred_target_names([statement.target])
```

So the caller applies `in` to a `bool`, and `bool` is not iterable:

```
TypeError: argument of type 'bool' is not iterable
```

## Blast radius

Every non-`ast.Assign` statement routed through this helper. Measured directly
against `165b5b3d` in `/home/agent/poke-harness/.scratch/leadverify_419`:

| statement | result on `165b5b3d` | after fix |
|---|---|---|
| `for *cs, in ((1,),):` | `TypeError` | `True` |
| `del cs` | `TypeError` | `False` |
| `with cs:` | `TypeError` | `False` |
| `(cs := ...)` walrus | `TypeError` | `False` |
| `*cs, = (1,)` | works | unchanged |
| `cs, x = (1, 2)` | works | unchanged |

On the combined #412+#424+#419+#421 tree this produced **130 test failures**
out of 710 (JUnit `/tmp/simint_412_424_419_421.xml`). Every one is a
`TypeError` raised out of `_is_enforced` via this line, which means the
sentinel module **crashes instead of answering** on any function containing a
walrus, a `for`, a `with`, or a `del` that also has a starred-assignment store
for the same name.

After the one-line fix: **710 tests, 0 failures, 0 errors, 0 skipped**
(`/tmp/simint_final.xml`).

## Why it was not caught

**#421's own test suite passes on its own head: 358 tests, 0 failures, 0
errors, 0 skipped** (`/tmp/pr421_full.xml`). The suite never calls
`_binds_starred_target` with a non-`ast.Assign` statement, so the crashing
branch has no coverage. `pytest -k starred` on `165b5b3d` is also green
(12 passed).

This is the failure mode that makes "CI passed" insufficient on its own: the
defect is only reachable when a *different* PR's code paths route additional
statement kinds through the same helper. #419's head `318d9a2c` returns
`False` cleanly for `for *cs, in ((1,),):`, so the regression is unambiguously
introduced by `165b5b3d` and not by my conflict resolution.

## Why it matters beyond a crash

The module exists to decide whether an assert is load-bearing. Raising
`TypeError` is the worst possible answer: it is neither `enforced` nor dead,
it takes down the whole test, and it is loud rather than silent — so it will
not ship as a false-green. But it does mean #421 cannot merge onto any tree
that exercises the path, and its green CI is misleading about that.

## Fix

Drop the redundant `in`. The one-line change exists in the simulation only.

The better fix is upstream, and it should also add coverage for the
non-`ast.Assign` spellings — the four rows above — so the branch is pinned
rather than merely working. Without a test, the same redundant-`in` mistake is
free to reappear.

## State

- Simulation worktree: `/home/agent/poke-harness/.scratch/simint`
- `origin/master` unmoved at `6b72bf62b5722e1df2c44c988bbdb14803024032`
- Nothing pushed, merged, marked ready, or closed.
- `master` and the protected checkout are untouched.
