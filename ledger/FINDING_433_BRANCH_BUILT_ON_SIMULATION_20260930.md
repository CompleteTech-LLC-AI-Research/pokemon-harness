# Finding — #433's branch was built on the simulation tree, not its declared base

Date: 2026-09-30
Found by: lead integrator
Severity: **BLOCKING for #433**; the underlying #429 fix is sound.
Status: #433 closed as superseded by **#435**, which carries the identical fix
on a correctly-built base.

## The defect

PR #433 declared base `fix/385-position-aware-loop-value` (`6f388cf`). Its diff
against that base was **1,125 lines** across the two test files. The #429 commit
itself is **152 lines**.

The remaining ~973 lines were **#413** and **#420** code inherited from the
simulation merge tree:

```
* 4916466 (head)
* ...
* 39f1b19 #429: an elif link is reached only when every test above it failed
* 63ab8dd SIMULATION: final state of run
*   8b12532 SIMULATION: merge #421 (165b5b3) onto the #412+#424+#419 tree
*   7540e29 Merge commit '590ca17 (#413) into HEAD
*   c19603f Merge commit '6f388cf (#412) into HEAD
```

plus 16 `ledger/` working-note files.

## Why it mattered

1. **Double-landing.** Merging #433 as it stood would land #413 and #420 twice
   — once here, and again when #424 and #421 merge.
2. **The reviewed diff is not the merged diff.** A reviewer reading a
   1,125-line diff that includes two other PRs' code is not reviewing the
   #429 fix. Every claim in the PR body — "repairs only #429", the four
   control rows, the 673/714 counts — was stated about a change that was not
   what the base-to-head diff contained.
3. **It was invisible from the PR page.** `mergeable: MERGEABLE`, CI-shaped
   counts, and a well-written body all read as healthy. Only
   `git log --graph <base>..<head>` exposes a simulation merge sitting between
   the base and the fix.

## Why it survived earlier rounds

The ledger on that branch repeatedly described the branch as validated and
mergeable. Those statements were made against the *simulation* tree, where
#413 and #420 were already present, so the extra code never showed up as a
surprise in a test count. The isolation test — build base + fix alone — is what
exposed it, and it had not been run.

## The fix

Rebuilt as base `6f388cf` + cherry-pick of `39f1b19` only:

- `36cf0e8` on `fix/429-elif-clean-only` -> PR **#435**
- `git diff d32114f..36cf0e8` is **empty** — byte-identical to the tree that
  was verified.

The old branch is **preserved**, not deleted or force-pushed. GitHub does not
support re-pointing an existing PR's head branch, so a replacement PR was
required rather than a retarget.

## The fix itself, verified independently

| check | result |
|---|---|
| full sentinel + milestones | **673 tests, 0 failures, 0 errors, 0 skipped** |
| `ruff check tests/` | clean |
| `ruff format --check tests/` | 269 files already formatted |
| mutation: disable the guard | killed by exactly the regression row, no control row |
| 4 rows after restore | 4 passed, worktree clean |

Ground truth was obtained by **executing** each fixture on CPython 3.12.14 and
comparing to `_is_enforced`. Parent-vs-head over 10 shapes: the two damaging
false-DEADs are repaired, the first-link control is unchanged, and the
false-LIVE residue is the pre-existing #434 family.
