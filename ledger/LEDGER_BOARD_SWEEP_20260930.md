# Board sweep — merge-tree behaviour of the mergeable PRs

Date: 2026-09-30
Method: for each PR, build the real merge tree (merge-base, then the branch head), then
compare `_is_enforced` against **executed** CPython for a common fixture set. Textual merge
success is not evidence of correctness, and neither is a green suite.

## Live board

19 open PRs, **all with `reviewDecision=NONE`** — zero reviews anywhere. 89 open issues.
Every PR shares one author identity (`CompleteDotTech`), which is also the lead's git
identity, so "an author may not independently approve their own change" binds across the
**whole board**, not just per-PR. No PR can be merged this run without an external reviewer.

`origin/master` unmoved at `6b72bf62`.

## The two "CONFLICTING" PRs are not textually conflicting

| PR | GitHub | actual |
|---|---|---|
| #392 `fee41bfb` | `CONFLICTING` / `DIRTY` | diverged from master 12 ahead / 3 behind (merge-base `ed9d9b0`), but the three-way merge completes with **zero unmerged files** |
| #379 `0845ca56` | `CONFLICTING` / `DIRTY` | diverged 19 ahead / 3 behind (merge-base `0529c8a`), merge also completes with **zero unmerged files** |

Both flags are stale metadata. This also **corrects** an earlier-round claim that #392 was
"stalled purely on a missing push" — it is not; the remote head is `fee41bfb` and it has
genuinely diverged.

## Behaviour per candidate

| PR | head | verdict summary |
|---|---|---|
| #433 | `4916466b` (code `39f1b19`) | **fixes #429.** 532-chain differential: false-deads 84 -> 0. 714 tests green. Leaves a false-LIVE family filed as #434. |
| #421 | `e058d96b` | **fixes the filed `TypeError`.** 714-row suite green on the #433 tree; lead re-verified independently. #423 still open. |
| #406 | `eb7c15f7` | **5/5 correct**, including Case E (`elif` after `if False`) which #433's branch gets wrong. 615 tests green, ruff clean. **Best-behaved candidate measured.** |
| #392 | `fee41bfb` | merged tree **repairs** the #388 false-dead (`False` -> `True` on the core row) and introduces one safe-direction false-live on the #388 control. 407 tests green, ruff clean. |
| #379 | `0845ca56` | merged tree **introduces a FALSE-DEAD** — see `FINDING_379_MERGE_INTRODUCES_FALSE_DEAD_20260930.md`. 367 tests green and still wrong. |

## Notable divergence between #406 and #433

They are **opposite fixes** to the same shape, and neither is a superset:

- #406 answers `if False: / elif True: STORE` **correctly** (`False`, the arm is
  unconditional) — #433's branch answers `True` there.
- #433 answers `if x: / elif True: STORE` correctly and is the only one measured to drive
  false-deads to zero over 532 generated chains.

Any integration of the `elif` fix should reconcile these two rather than pick one and lose
the other's row. #406 is draft; #433 is ready-for-review but unreviewed.

## Why "green suite" is not sufficient here

Three separate trees were fully green while carrying a wrong answer:

- #379 merge tree: **367 passed**, one real contract reported dead.
- #412 head: **669 passed**, the #429 false-dead present.
- #433 parent: **710 passed**, 84 false-deads present across the generated chains.

The row that catches a defect has to exist. That is the recurring failure mode on this board
and the reason these rows are executed against CPython rather than asserted from a table.

## Status

Nothing merged, nothing marked ready, no issue closed. Release `PARTIAL`.
