# Lead record — #439, an empty string is an empty loop

- Issue: #439
- Base: `2c81f10c165f1367d380680ba7fb9b6df14b3eec` (`origin/master` at start)
- Branch: `fix/439-empty-string-loop`
- Worktree: `/workspace/poke-harness/.scratch/fix439`
- Author: lead integrator. **Not self-approved** — independent review required before merge.

## Defect

`_is_empty_literal_iterable` gated on `ast.List`, `ast.Tuple`, `ast.Set` and
`ast.Dict`. A `for` over an **empty string** iterates zero times just as `[]`
does, so the assert in its body was reported **live** when CPython never runs
it — a false-LIVE, a disarmed contract certified as load-bearing.

The gap was a type gate, not a reasoning gap: `''` is an `ast.Constant`, and
`_literal_value` already reads it exactly. Only the container list excluded it.

## Implementation

One branch ahead of the container check:

```python
if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
    return not node.value
```

`bytes` is admitted by the same test rather than a separate rule: it is the
other literal sequence type with a readable length, and an empty one yields
nothing too. The type test is load-bearing and is pinned as such (see M5).

## Evidence

Ground truth is executed CPython with a **3-way** classification — FIRED /
SWALLOWED / NOT_EVALUATED. Recording "no `AssertionError` escaped" as one
bucket conflates *ran and was swallowed* with *never ran*; those are different
defects, and conflating them makes a correct verdict look wrong. This harness
caught one of my own probe mistakes earlier in the session for exactly that
reason.

Executed shapes, master vs this branch:

| shape | truth | `2c81f10` | this branch |
|---|---|---|---|
| `for _ in '':` | not evaluated | `True` **wrong** | `False` correct |
| `for _ in "":` | not evaluated | `True` **wrong** | `False` correct |
| `for _ in b'':` | not evaluated | `True` **wrong** | `False` correct |
| `for _ in 'a':` | fires | `True` correct | `True` correct |
| `for _ in 'abc':` | fires | `True` correct | `True` correct |
| `for _ in []: / () / {}` | not evaluated | `False` correct | `False` correct |

`for _ in range(0):` stays `True` and `for _ in x:` stays `True` **by design**,
not by oversight: the function's own docstring declines to match calls, because
deciding them means reasoning about builtins rather than reading a literal and
a wrong answer there drops a live contract. The fix keeps that boundary and
touches only literals.

### Lane

```
python -m pytest tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py -q -p no:randomly
  -> 895 tests, 0 errors, 0 failures, 0 skipped (junit /tmp/fix439b.xml)
python -m ruff check tests/           -> All checks passed!
python -m ruff format --check tests/ -> 283 files already formatted
git diff --check                     -> clean
```

## Pinned rows

11 new rows beside the existing `for over empty list/tuple/dict` family:
empty string (both quote styles), empty bytes, unpack over empty string, three
live string controls, and four non-string-constant controls.

## Mutation matrix — 5/5 killed

| Mutant | Change | Killed by |
|---|---|---|
| M1 | remove the string branch entirely | 4 rows |
| M2 | strings always "non-empty" | 4 rows |
| M3 | strings always "empty" | 3 rows |
| M4 | drop `bytes` from the type test | 1 row (`for over empty bytes`) |
| M5 | drop the **type test** (`any constant is empty`) | 3 rows |

**M5 initially SURVIVED.** Dropping `isinstance(node.value, (str, bytes))`
passes every string row, because the string rows only ever supply strings. The
mutant made `for _ in 0:`, `for _ in None:` and `for _ in False:` report as
provably empty, dropping live asserts from the sentinel's view — the damaging
direction, introduced by a repair for a defect in that same direction.

Closed by adding four non-string-constant controls, not by waiving the mutant.
Re-run confirms the kill: `[CONTROL for over an int is not empty]`,
`[... None ...]` and `[... False ...]` fail under M5.

## Rebase onto `02776f8` (PR #448)

The first push was based on `2c81f10`. PR #442 (#378) merged to master as
`02776f8` in the meantime, so a probe against the old head appeared to
"regress" #378 — it was not a regression, it was the pre-#442 state. Rebased
onto `02776f8`; head is now `ab79677`.

Re-verified on the rebased head, both fixes coexist:

* #378 (merged, #442): zero-iteration loop bodies perform no stores — 4/4 shapes correct
* #439 (this branch): empty string/bytes loops — all shapes correct
* Lane: **988 tests, 0 errors, 0 failures, 0 skipped** (`/tmp/fix439-rebase.xml`)
* `ruff check`: All checks passed; `ruff format --check`: 283 files already formatted
* `git diff --check 02776f8..HEAD`: clean

## Status

Not merged. Requires an independent review of the exact head `ab79677` before merge.
