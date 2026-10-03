# Independent review brief — PR #565

You are an **independent reviewer**. Do not trust the author's claims.
Re-derive everything yourself with your own commands.

## Ground rules (violating any of these invalidates your review)

1. **Never reuse, copy, or symlink a virtualenv between worktrees.** This repo
   refuses pytest collection across checkouts. Each worktree needs its own venv.
2. **Always run Ruff with `--no-cache`.** A concurrent full pytest suite sharing
   a worktree's `.ruff_cache` will corrupt it and produce a *false* finding.
3. **Do not run Ruff concurrently with a full pytest suite in the same worktree.**
4. If two runs of the same command on the same commit disagree, that is an
   **environment fault, not a code finding.** Say so explicitly.
5. You are reviewing only. Do not push, merge, or modify the branch.

## Revision under review

- `origin/master` = `b5302d091e5c809bd343f0f15996b8a47bdb4d3d`
- PR #565 head = `f35554b6fe53688d952751604d783a7f2a13ba3a` (`fix/89-excluded-scope-guard`)

    cd /home/agent/vpkg
    gh pr view 565 --json headRefOid,isDraft,mergeStateStatus
    git worktree add /home/agent/rv565_$$ origin/fix/89-excluded-scope-guard

## What #565 is for

Issue #89. The guard's purpose: an effect family that **owns moves** must not be
marked `deliberately_excluded` in the catalog, because that silently drops
coverage of real game behavior.

## Claims to verify

1. The guard has **teeth**. These must each be **rejected** by the guard:
   - count-consistent downgrade of a move-owning family to `deliberately_excluded`
   - editing or stripping `move_ids` to make the family look move-free
   - deleting a family entirely
   - unknown scopes
   - count drift
2. The honest direction `tested -> planned_unverified` remains **accepted**.
3. The 7 catalog tests pass.

    cd /home/agent/rv565_$$
    python3 -m pytest tests/ -q -k "catalog or scope or excluded"

The guard is only worth merging if the mutations actually go red. A guard that
accepts every input is worse than no guard, because it manufactures false
assurance. Spend most of your effort on the mutation matrix, not on the green
run.

## Scope discipline — the most important point in this brief

**#565 is only a hardening slice of broad issue #89.** Merging #565 does **not**
satisfy issue #89. Do not let a green #565 be read as closure of #89: every
acceptance row in #89 must be terminal before #89 can close.

Confirm the PR does not itself claim to close #89. If it does, that is a
finding — over-claiming scope is how issues get closed prematurely.

## Deliverable

Report **APPROVE** or **REJECT** with:

- the exact head SHA you reviewed
- your independent mutation matrix (which mutations went red, which stayed green)
- every finding, with severity (blocking / non-blocking)
- an explicit statement on whether #565 alone is enough for #89 (expected: no)
