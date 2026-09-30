# Verification — PR #392 is NOT push-blocked; its conflict is stale metadata

Date: 2026-09-30
PR: https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/392 (fixes #388)
GitHub reports `mergeable=CONFLICTING`, `mergeStateStatus=DIRTY`.

## Prior-round claim, corrected

Earlier rounds recorded #392 as "stalled purely on a missing push" — `e6b57e7` approved and
local, only `git push` outstanding. **That is stale.** The remote head is `fee41bfb`, and it
has genuinely diverged from master:

```
git rev-list --left-right --count origin/master...origin/fix/375-local-carrier-stale
  12	3
merge-base: ed9d9b0  (the #375 merge)
```

So it is neither a fast-forward nor an unpushed-commit situation. The earlier claim should
not be relied on.

## But the merge is textually clean anyway

Both the onto-merge-base and the onto-current-master merges complete with **zero unmerged
files** and no conflict markers:

```
git merge origin/fix/375-local-carrier-stale --no-commit --no-ff
  Automatic merge went well
git diff --name-only --diff-filter=U   ->  (empty)
```

Both sides edit the same support module (`tests/_timed_menu_milestone_sentinel_support.py`,
+220 lines from this branch) and the same test module (+255), and the three-way merge
resolves them. The GitHub `DIRTY` flag is stale metadata, not a real textual conflict.

## Behaviour on the actual merge tree (the part that matters)

Ground truth by **executing** each fixture. Trees: `/home/agent/poke-harness/.scratch/mbase`
(`origin/master` `6b72bf62`) and `/home/agent/poke-harness/.scratch/m392` (#392 merged onto
current master, commit `2fa62c5`).

| shape | master `6b72bf62` | #392 merged | CPython | direction |
|---|---|---|---|---|
| **#388 core** carrier `import os as cs` + conditional `cs = nullcontext()` | `False` ✗ | **`True` ✓** | fires | **fixes a FALSE-DEAD** |
| #388 control: same, but conditional store pins a non-enterable `list()` | `False` ✓ | `True` ✗ | raises | introduces a false-LIVE (safe) |
| #375 starred target `for *cs, in (1,)` | `True` ✗ | `True` ✗ | raises | unchanged, still false-live |
| #425 `for cs in (1, None)` | `True` ✗ | `True` ✗ | raises | unchanged (filed residual) |
| #429 `elif True:` store after per-call `if` | `True` ✗ | `True` ✗ | raises | unchanged (fixed only by #433) |

**Net: the merge repairs the one damaging-direction defect on master and introduces exactly
one safe-direction false-live.** The #388 control flip is the thing to watch — master had it
right and this branch makes it a false-live, so it should get a filed follow-up rather than
being folded silently into this merge.

## Suite and lint on the merge tree

- `pytest tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py`
  -> **407 tests, 0 failures, 0 errors, 0 skipped** (`/tmp/m392.xml`)
- `ruff check tests/` -> All checks passed
- `ruff format --check tests/` -> 269 files already formatted

407 rather than 714 because master does not carry the stacks' extra rows; that is expected
and is not a regression.

## Status: still NOT mergeable

#392 has **zero independent reviews** and the author identity is the same one the lead
holds, so the lead cannot supply the review. Nothing was pushed, merged, or marked ready.

Probes: `/home/agent/poke-harness/.scratch/probe392/m392.py`.
Trees: `/home/agent/poke-harness/.scratch/mbase`, `/home/agent/poke-harness/.scratch/m392`.
