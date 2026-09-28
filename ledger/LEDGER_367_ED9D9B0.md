# Issue #367 — tied stores in one top-level statement

**Issue:** #367 — `#360 follow-on: two stores tied in one top-level statement let
max() pick the wrong one, reporting a live assert as defeated`
**Branch:** `fix/367-tied-store-ambiguity`
**Worktree:** `/workspace/poke-harness/.scratch/fix367`
**Base / parent:** `ed9d9b0` (`origin/master` at start of this work)
**Files owned:** `tests/_timed_menu_milestone_sentinel_support.py`,
`tests/test_timed_menu_milestone_sentinels.py`
**Release status:** PARTIAL (unchanged). No ROM, `pyboy`, native, timing or
performance claim is made by this work.

## Acceptance, as filed

1. A tie in `_binding_order` must not be resolved by walk position. The
   damaging case named in the issue is `for/else` binding one name in both
   branches: exactly one branch runs, so no single verdict is right and the
   name must be **declined** (reported defeated — the safe side, #308
   criterion 1) rather than picked.
2. The gap must be covered by shipped fixtures. The issue records that the
   273-row suite is fully green with the defect present, i.e. the tie path has
   zero coverage.
3. No new damaging-direction regression in either polarity (a live assert
   reported defeated, or an unreachable header reported enforced).

## Diagnosis

`_binding_order` keys a store by the *top-level statement* containing it, so
two stores written in the same block compare equal by construction. The
pre-#360 `visible[-1]` and the post-#360 `max(...)` both resolve such a tie by
walk position, which is a property of the breadth-first traversal rather than
of the program. A later write to a name overwrites an earlier one, so where a
single store in the block is the last write that *ran*, that store is what a
read sees — but a store inside a branch may never have run, and then the source
does not say which one it was.

## Implementation

| helper | role |
|---|---|
| `_latest_write_in_block` | the last source write in a tied set that actually ran; falls back to an unconditional earlier write when one exists; declines when no single last write is provable |
| `_store_is_settled_before` | a conditional store in an **already completed** `with` body is settled, but only for a strictly later top-level statement |
| `_runs_before_end_of` | restricts "settled" to direct `with`/`async with` body chains; a store under `if`/loop/`try`/`orelse`/`except`/`finally` may be skipped |
| `_NOT_A_SUPPRESSOR` | explicit record that a name *is* bound here and carries no suppressor, so `_aliased_suppressions` cannot carry a superseded suppressor forward |
| `_stores_of` | includes settled in-body stores and prefers the settled member of a same-order tie |
| `_match_capture_pins_value` | reads a `match` capture only when a literal list/tuple subject and a whole-element capture pin its value (`match [1]: case [cs]:`) |
| `_readable_store_value` | rejects string carriers (#359's runtime kinds) and unreadable right-hand sides **at the producer** rather than at the consumer |
| `_deref_alias` / `_last_store_before` / `_resolve_bindings` | thread position and function scope, and share one tie policy so the two resolvers cannot disagree |
| `_aliased_suppressions` | handles `AMBIGUOUS_SUPPRESSOR` explicitly and does not append `_NOT_A_SUPPRESSOR` as a value |

An earlier cut carried a second, all-conditional tie branch. Brute force over
630 order/conditional combinations found **zero** reachable cases: with no
unconditional entry at the maximum order, two or more conditional entries at
that order are exactly the *competing* case the existing branch already
declines. The unreachable branch was removed rather than left where no mutation
can kill it.

## Regression cases pinned by the suite

1. Same-block rebind -> `[False, True]`
2. Conditional rebind inside the same `with` body -> `[False, False]` (the
   `with` already captured the `suppress`; the body store does not change the
   manager it entered)
3. Header before store in one block -> `[True, False]`
4. In-header carrier (`import os as cs`, `from m import N as cs`, `def cs`,
   `class cs`, `match [1]: case [cs]:`, `nullcontext()` control) -> `[False]`
5. `for/else` tie -> `[False]`, declined as undecidable

## Commands and terminal results

Interpreter: `Python 3.12.14` (CPython), the pinned source venv.

    $ python -m pytest tests/test_timed_menu_milestone_sentinels.py \
                          tests/test_timed_menu_milestones.py -q --junitxml=/tmp/final367.xml
    391 tests, 0 errors, 0 failures, 0 skipped        (exit 0)

Counts are read from the JUnit XML, not from console progress:
`tests="391" errors="0" failures="0" skipped="0"`.
The 11 warnings are pre-existing (pysdl2 binary warning, and the
`ast.NameConstant` `DeprecationWarning` on a `py<3.8` guarded branch).

    $ python -m ruff check   tests/_timed_menu_milestone_sentinel_support.py \
                           tests/test_timed_menu_milestone_sentinels.py
    All checks passed!
    $ python -m ruff format --check  <same two files>
    2 files already formatted
    $ git diff --check
    (clean)

## Mutation matrix — all seven killed, post-`ruff format`

Each mutation is applied to the formatted file, run against
`tests/test_timed_menu_milestone_sentinels.py`, and the file is restored from a
byte-identical copy afterwards (`diff -q` clean, `RESTORED-CLEAN`).

| id | mutation | result |
|---|---|---|
| A | mixed-tie supersession removed (`_resolve_bindings` always declines the tie) | **killed** |
| B | settled-store handling removed (`_store_is_settled_before` always `False`) | **killed** |
| C | `match` literal pinning removed | **killed** |
| D | `_latest_write_in_block` tie selection removed | **killed** |
| E | `_runs_before_end_of` always `False` | **killed** |
| F | `_stores_of` settled-tie preference removed | **killed** |
| G | forward `_NOT_A_SUPPRESSOR` pass disabled | **killed** |

## Evidence hashes

    50aa7b9170e5d921cef1e504f87363e8b1f873599b53cbba088e91d52e611cdf  tests/_timed_menu_milestone_sentinel_support.py
    3bb0756428ca1dce8c279fa5179e03f8b16a4f42506e26e6a3f79d14cd4d69d4  tests/test_timed_menu_milestone_sentinels.py
    cf87f48b2b602e1f626610a32d6ceb6ac645044f133e0475e575943a16b49d25  /tmp/final367.xml

Diff size: 2 files, 853 insertions, 21 deletions.


## Round 2 — #395 repair (lead finding on the for/else row)

The lead found that `TIED_STORE_ROWS[0]`, the branch's flagship row, rested on
a false statement about Python: a `for`'s `else` runs whenever the loop
completes *without* `break* -- for any iteration count -- so it is not the
"loop was empty" branch. Both arms run for a non-empty iterable, the `else` is
written last, and the assert FIRES on every input. The row pinned `[False]`
and therefore pinned the damaging verdict.

| tree | analyzer | measured | correct? |
|---|---|---|---|
| `ed9d9b0` base | `[False]` | LIVE both inputs | no -- damaging |
| `13dd4d9` | `[False]` | LIVE both inputs | no -- damaging |
| `2df00cd` (this round) | `[True]` | LIVE both inputs | yes |

Filed as #395. Full detail and the process note (two of my own oracles were
wrong before the polarity self-test caught them) in
`ledger/LEAD_20260928_367_FOR_ELSE_FINDING.md`.

### Repair

`_runs_before_end_of` already walked `orelse` but rejected anything not reached
through a `with` body. It now recognises a loop `else` whose loop cannot break:

- `_loop_else_always_runs(loop)` -- is the `else` guaranteed to run?
- `_breaks_own_loop(node)` -- is there a `break` that exits *this* loop? It does
  not descend into a nested loop, whose `break` targets that inner loop.

The row is corrected to the shape it was always meant to be, and the genuinely
undecidable shape it was describing -- the same loop with a `break` -- is now
pinned separately as `[False]`, so the decline is earned rather than assumed.

### Round 2 rows

| row | expected |
|---|---|
| a for/else pair whose else runs on every path settles the name | `[True]` |
| a for/else pair the loop can break out of is declined | `[False]` |
| a break in a nested loop does not suppress the outer loop's else | `[True]` |

### Round 2 commands and results

    $ python -m pytest tests/test_timed_menu_milestone_sentinels.py \
                        tests/test_timed_menu_milestones.py -q -p no:randomly \
                        --junitxml=/tmp/fe_full2.xml
    393 tests, 0 errors, 0 failures, 0 skipped        (exit 0)

    $ python -m ruff check <both files>            All checks passed!
    $ python -m ruff format --check <both files>   2 files already formatted
    $ git diff --check                             clean

### Round 2 mutation matrix -- 10/10 killed

The seven original mutations (A-G) still hold. Three new ones cover the #395
rule:

| id | mutation | result |
|---|---|---|
| H | loop-`else` settling disabled | **killed** |
| I | `_loop_else_always_runs` always `True` (a `break` ignored) | **killed** |
| J | nested-loop guard removed (an inner `break` counts against the outer loop) | **killed after the third row was added** |

J survived the first attempt, which is why the nested-loop row exists. Recorded
because a surviving mutant is a coverage gap, not a pass.

## Independent review

### Round 1 -- `13dd4d9` -- REQUEST CHANGES

`ledger/REVIEW_367_13DD4D9_codex1.md`, reviewer `codex1`, verdict **REQUEST
CHANGES**.

| finding | severity | disposition |
|---|---|---|
| F1 `_runs_before_end_of` judges only the innermost hop | blocking | **repaired** in `99cce88` |
| F2 all five new fixture rows pass against base | blocking | **already repaired** by the #395 round (`2df00cd`) |
| F3 the ancestry comment claims every hop, code checks one | non-blocking | **repaired** with F1 |

Reviewer-measured and lead-reproduced:

* Suite reproduced at 391 from JUnit XML; ruff check and format clean;
  `git diff --check` clean; both ledger SHA-256 hashes match.
* Mutations **B** and **G** re-run and killed (2 failures among the new rows
  each), file restored byte-identically.
* F2 reproduced on `13dd4d9` and **no longer reproduces** at `cf59407`: two
  rows now fail against `ed9d9b0`, including the loop-`else` row that the
  reviewer's base run showed passing. The other five rows still pass on base,
  which is expected -- they pin behaviour base already had.

The reviewer's GitHub access failed, so it could not verify the live PR head or
read the issue comments. The lead verified both.

### F1 disposition -- repaired, and its second half filed as #397

`_runs_before_end_of` decided settlement from the **last** hop alone, so a
store under an `if`/loop/`try` *inside* a `with` body was called settled. The
walk now carries whether **every** edge crossed so far was guaranteed, and
`_edge_is_guaranteed` names the test in one place.

The reviewer's shape also has a `flag=False` half that this repair does **not**
and should not change: with `flag` false the branch store never runs, `cs` is
still the `suppress`, and the assert is swallowed. That disagreement is
**identical on `ed9d9b0`**, where it arrives by a different route, so it is
pre-existing rather than a regression from this repair. Filed as **#397**
rather than folded in, because closing it means treating a single unsettled
conditional store as ambiguous instead of as a supersession -- which
contradicts the pinned **#324 criterion 2** row
(`a later rebind inside an if wins over a carried walrus value`, `[False,
True]`). That row is analysed statically and pins only the `flag=True` input;
its `flag=False` input is already wrong on base. Re-deciding an accepted
criterion is not a repair this PR may make silently.

## Round 3 -- `99cce88` commands and results

    $ python -m pytest tests/test_timed_menu_milestone_sentinels.py \
                        tests/test_timed_menu_milestones.py -q \
                        --junitxml=/tmp/fix367_v3.xml
    393 tests, 0 errors, 0 failures, 0 skipped        (exit 0)

    $ python -m ruff check <both files>            All checks passed!
    $ python -m ruff format --check <both files>   2 files already formatted
    $ git diff --check                             clean

Execution probes over the F1 shape, each with its live control, on
CPython 3.12.14 (`x=2`):

| shape | input | runtime | analyzer | verdict |
|---|---|---|---|---|
| `if` branch holding a nested `with` | `flag=False` | SUPPRESSED | DEFEATED | correct (safe) |
| same | `flag=True` | LIVE | DEFEATED | safe; input-dependent |
| empty loop | `rows=()` | SUPPRESSED | DEFEATED | correct (safe) |
| one iteration | `rows=(1,)` | LIVE | DEFEATED | safe; input-dependent |
| direct `with`-body rebind | -- | LIVE | ENFORCED | **still certified** |
| plain later store | -- | LIVE | ENFORCED | **still certified** |

## Next action

Push `99cce88`, obtain a fresh independent review of that exact head, then
merge with a head-SHA guard after refreshing `origin/master` and re-running the
affected lane. Close #367 only after the merge is verified on master. #397
stays open and is not closed by this PR.
