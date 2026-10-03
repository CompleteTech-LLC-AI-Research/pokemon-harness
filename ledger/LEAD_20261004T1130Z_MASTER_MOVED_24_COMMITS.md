# master moved 24 commits; re-derived mergeability of the remaining PRs

`origin/master` is now `a413eeb5` (see `LEAD_20261004T1100Z_566_MERGED_CONCURRENTLY.md`).
All three surviving branches were authored against `b5302d0` and are therefore
24 commits behind. Mergeability had to be re-derived rather than carried over.

## Merge-tree results against the new master

| PR | head | base | `git merge-tree` | GitHub says |
|---|---|---|---|---|
| #565 | `f35554b6` | master | **clean** (rc=0) | MERGEABLE / CLEAN |
| #568 | `1cc86f8` | master | **clean** (rc=0) | MERGEABLE / CLEAN |
| #569 | `25607fc` | `fix/567-scripts-format-clean` | **3 conflicts** | MERGEABLE / CLEAN |

## #569 conflicts, and why

    .github/workflows/release-hygiene.yml   CONFLICT (content)
    scripts/run_local_ci.sh                 CONFLICT (content)
    tests/test_local_ci_policy.py           CONFLICT (content)

This is expected rather than alarming. #569 is **stacked on #568**, so it merges
into `fix/567-scripts-format-clean`, not into master. The 24 new master commits
— chiefly the merged #566 — edited the same two lane files, `run_local_ci.sh`
and `tests/test_local_ci_policy.py`, plus the workflow.

GitHub currently reports #569 `MERGEABLE`/`CLEAN`. That is stale or computed
against the branch base rather than master; the authoritative local result is
the merge-tree conflict. **#569 must not be merged until it is rebased onto the
post-#568 master and re-verified.** Its prior green CI and lead measurements were
taken on `25607fc` against `b5302d0` and do not transfer to a rebased head —
which is exactly the run's rule that a passing test on an old head is not
automatically transferable.

Encouragingly, #569 does **not** touch the #106 directive-gate region
(`maybe_directive`, `directive_prefixes`, `_BENCHMARK`, `silences`): a grep for
those symbols in its diff returns nothing. So the conflict is ordinary lane-file
drift, not a re-introduction of the bypass.

## Ordering, unchanged and now more load-bearing

1. #568 merges into master first (clean).
2. #569 is rebased onto the new master, its coverage-preservation and
   producer-carve-out evidence is re-measured on the new head, it gets CI, and
   only then merges.
3. #567 closes after both steps land and post-merge checks pass.

## Still blocked

All three remain **without an independent review**, so none may merge. The
dispatch tooling continues to drop task text. Briefs are on disk at
`briefs/REVIEW_565.md` and `briefs/REVIEW_569.md`.
