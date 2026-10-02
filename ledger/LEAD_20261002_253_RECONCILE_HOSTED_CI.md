# Reconciled state - #253 / #84 / #109 after this run

## Authoritative trunk

`master` == `origin/master` == `397fa611b0684c35da489e5b26cd9926a0399b10`
(fetched and verified; local == remote, 0 ahead/behind).

Trunk moved twice during this run (`27f1059` -> `0639360` -> `397fa61`) as
another session merged #542/#544/#545/#546 (#534 guard hardening).

## Hosted CI is GREEN on trunk - this is the decisive fact

Workflow `Native unit validation`, run `37011504367`,
`head_sha = 397fa61` = current master, `completed`/`success`.
Read from the job log, not the rollup:

    PASS import-origins: returncode=0 duration=0.3s
    PASS python-module: returncode=0 duration=10.3s
    PASS pytest-console: returncode=0 duration=2.5s
    PASS: mode=schema entries=31 returncode=0
    PASS: collected=8312 structural=PASS acceptance-declaration=PASS
    capacity-policy: unavailable
      PASS     unit    total=8089 passed=8089 failed=0 skipped=0 xfailed=0 xpassed=0 errors=0 duration=258.7s
    overall: PASS

Locally corroborated the count: `--collect-only -m unit` under the gate's own
arguments (`--strict-config --strict-markers -p pytest_asyncio.plugin
-p tests._gate_report`) collects exactly **8089**, matching CI's `total=8089`.

## What my local red runs actually were

This container: 4 cores, ~7.9 GB RAM shared with other tenants.

- The full 8,089-test unit tier does **not** complete here. It is OOM-killed
  (`memory.events: oom_kill 1`); run sharded it reaches ~3,057 passing rows
  clean plus the 5 known-slow rows failing.
- `capacity-policy: unavailable` in CI means the CI unit lane had **no CPU
  admission check**. CI did the whole tier in 258.7s; here it needs 15+ min.
- `--durations=12` shows the failures land **on** their budgets:

        12.74s / 12.44s   vs PAIR_WORK_CAPACITY_S = 12.0s
         5.32s / 5.23s / 5.08s  vs BOUND = 5.0s
         1.40s            next-slowest (3.5x margin)

  Marginal-budget rows that a faster runner passes. Not a correctness defect,
  and not a hang (my earlier "livelock" reading was wrong and is withdrawn).

## Two real host facts that were genuine confounds

1. `/tmp` was **100% full** (512M/512M) from this lead's own completed #534
   verification checkouts. Reclaimed ~370M. Every earlier measurement in this
   thread ran under a full `/tmp`.
2. `/dev/shm` is `tmpfs ro` here and there is no root/sudo, but the repo's own
   documented remedy works unprivileged:
   `unshare -Urm --propagation private` + `mount -t tmpfs -o size=2g tmpfs /dev/shm`.
   Verified: `Barrier(2)` fails `OSError: [Errno 30]` outside, succeeds inside.
   This is what made the ~24 POSIX-shm rows measurable. It is a property of
   **this container**, not of trunk.

## Corrected criteria status for #253

| # | criterion | status |
|---|---|---|
| 1 | unit failures fixed or documented non-waived | **met** - trunk CI `37011504367`: `failed=0 errors=0 skipped=0 xfailed=0 xpassed=0` |
| 2 | `--unit-only` reports `overall: PASS`, `failed=0` | **met for the `unit` row** - `overall: PASS`, `total=8089 passed=8089` |
| 3 | timing tier on a qualified runner | **NOT met** - no `timing` row in the CI log at all; `--unit-only` selects `["unit","timing"]`, so the literal command is not fully evidenced. This is #85/#86 |
| 4 | independent review of the repairing head | n/a - no repairing head exists |

**#253 left open.** Criterion 3 needs a qualified CPU allocation (#85/#86), and
the literal `--unit-only` command's `timing` tier has no green evidence.

## Corrections posted (I over-claimed, then withdrew it)

- #109 https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/issues/109#issuecomment-5953867967
- #84  https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/issues/84#issuecomment-5953860279
- #253 https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/issues/253#issuecomment-5953878490

Each withdraws the "this is a per-step performance defect causing the red rows"
claim. The **profile itself stands** as #109's decision input (15,048 governed
steps/frame, each a full cross-socket pump, ~0.75 ms/step against a 5s budget;
`quantum_cycles` 256/1024/4096 makes no difference) - it is simply not the cause
of the currently-red rows.

## State preserved

- `master` == `origin/master`, primary checkout has **0 modified tracked files**.
- No reset, clean, force-push, broad staging, or branch overwrite.
- One disposable detached worktree (`812a852`) created for reproduction and
  removed with `git worktree remove --force`.
- All untracked ledger/scratch evidence retained.
