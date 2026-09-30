# INCIDENT — filesystem exhaustion twice during the `4baef6c` review

Date: 2026-09-30 06:05–06:20 UTC
Effect on the review: **blocked, then recovered.** No data lost.

## What happened

`/` went to **137M free / 100%** while the end-to-end independent review was
being prepared. At that point the review could not start: a worktree checkout,
a pytest run, or a mutation experiment would all fail on write.

## What I did

Reclaimed **only** what was unambiguously mine and disposable:

| action | result |
|---|---|
| removed `/home/agent/poke-harness/.scratch/lead_mut_e2e` — the worktree **I created this session**, verified clean (0 dirty) and restored to the pristine blob `31cd2025` beforehand | +104M |
| measured 140 `__pycache__` dirs under `.scratch` | only 176K total — not a lever |
| free space recovered externally during the window | back to **28G free / 92%** |

## What I deliberately did NOT touch

- **Both protected checkouts.** Lead: `bd2c167`, exactly 3 staged files. Child:
  `2c92d9a`. Re-verified after the incident — both intact.
- **Any other worktree.** The repo has ~210 worktrees, many carrying active
  work. Removing someone else's is not mine to do.
- **`/home/agent/.codex-shared/sessions`** — 11G of Codex session logs, by far
  the largest consumer. It is also the live transcript of this run, and other
  accounts share it. Pruning it is an operator decision, not a lead one.
- **`/home/agent/tmpwork`**, previously proposed for prune and previously
  unauthorised. Still unauthorised.

## Why this is recorded

Disk state is load-bearing for the verification contract. An independent
reviewer needs to create a worktree and run a 698-test suite; at 137M free that
is impossible, and a failure there would be a **host** failure that could be
mistaken for a code result. Recording it so no one reads a resource failure as
evidence about `4baef6c`.

## Standing note

Space is volatile on this host — it moved 30G → 2.5G → 137M → 28G within one
hour across ~210 worktrees and live session logs. Any capacity or throughput
claim made from this host is unreliable, and none is made here.

## Current state

`origin/master` `6b72bf6` — unchanged. `4baef6c` published at
`origin/lead/union-419-420`, **unmerged**. Release **PARTIAL**. The
end-to-end review brief is durable at `BRIEF_E2E_4baef6c.md`; the dispatched
channel has dropped its payload 33 times (`BLOCKER_COLLAB_20260930H.md`).
