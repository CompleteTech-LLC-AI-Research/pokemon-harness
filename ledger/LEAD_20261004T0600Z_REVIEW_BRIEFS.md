# Review briefs published to survive dispatch failure

## Why this exists

Every previously dispatched reviewer agent returned a response that was not a
review: generic Codex-settings replies, a workspace inventory, or a stale sync
report for an unrelated PR. The task text was being dropped in transit.

The briefs below are written to disk so the task survives the delivery failure
and any reviewer — human or agent — can execute them verbatim.

## Live state at publication

- `origin/master` = `b5302d091e5c809bd343f0f15996b8a47bdb4d3d`
- No `main` branch exists; trunk is `master`.
- Open issues: 32. Open PRs: 4. Release: PARTIAL.

| PR | head | draft | mergeable |
|---|---|---|---|
| #565 | `f35554b6` | yes | yes |
| #566 | `16d102e` | no | yes (UNSTABLE, checks pending) |
| #568 | `1cc86f8` | yes | yes |
| #569 | `25607fc` | yes | yes |

## Changes since the last ledger entry

- **#566 head moved** `fceb11c3` -> `16d102e`
  ("#106: fail closed on unmeasured probes, and fix a live CI failure").
  Touches `tests/test_local_ci_policy.py` (+51/-14) and its ledger only.
  Any verdict recorded against `fceb11c3` does not cover `16d102e`.
- New review question raised on `16d102e`: a `maybe_directive()` prefix
  short-circuit was added to cut ~130 subprocess spawns. It gates comment
  scanning on a hand-written prefix tuple that reimplements Ruff's directive
  grammar. That is the exact drift class the row exists to catch, so its
  completeness must be measured, not assumed.
- Temporary worktrees `/home/agent/wt567m` and `/home/agent/wt567g` removed.
  Worktree count restored to the protected baseline of 313.
- Protected checkout re-verified unchanged: branch `lead/259-widen-lint-lanes`
  at `bd2c167`, 3 staged files, 2 stashes, untracked `.scratch/` and `ledger/`.

## Files

- `briefs/REVIEW_565.md`
- `briefs/REVIEW_566.md`
- `briefs/REVIEW_569.md` (covers #568 and #569, which are stacked)

## Ordering constraint

`1cc86f8` (#568) is an ancestor of `25607fc` (#569). #568 must merge before
#569 or #569 will conflict.

## Standing rules encoded into the briefs

- Ruff always with `--no-cache`.
- Never run Ruff concurrently with a full pytest suite in the same worktree.
- Never reuse, copy, or symlink a venv between worktrees.
- Two runs of the same command on the same commit that disagree indicate an
  environment fault, not a code finding.
