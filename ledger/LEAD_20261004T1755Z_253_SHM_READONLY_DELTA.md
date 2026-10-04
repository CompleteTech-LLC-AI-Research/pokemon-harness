# #253 — the read-only `/dev/shm` penalty, measured

Date: 2026-10-04
Tree: `/home/agent/wtgate`, branch `gate/253-post580-verify`, HEAD `5a20f89c`
Interpreter: `.venv2`, CPython 3.11, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`,
`-p pytest_asyncio.plugin -p tests._gate_report -rA --maxfail=0` — the same
pytest surface the gate uses (`scripts/production_gate_model.py:218`), so the
only variable changed is the `/dev/shm` mount.

## Question

#253's title records "11 unit-tier and 201 timing-tier failures".
`LEAD_20261004T1645Z_253_GATE_POST580.md` measured 6 unit and 32 timing with a
writable `/dev/shm` and could not reconcile the gap. The runbook
(`docs/PRODUCTION_RUNBOOK.md:985`) attributes #253 to the read-only mount. That
is a testable claim, so this measures the delta directly.

## Host `/dev/shm` is read-only — confirmed

```
shm on /dev/shm type tmpfs (ro,nosuid,nodev,noexec,relatime,size=64000k,uid=1000,gid=1000,inode64)
```

Writing raises `OSError [Errno 30] Read-only file system`. Note `size=64000k` —
64 MB even when writable.

## Measurement

Identical tree, identical command, mount is the only difference:

| `/dev/shm` | unit result | duration |
| --- | --- | --- |
| writable (nested-namespace wrapper) | `total=8327 passed=8318 failed=6 skipped=3` | 1657.0s |
| **read-only (host default)** | `total=8327 passed=8292 failed=32 skipped=3` | 1926.96s |

Collection is 8327 in both, so the mount changes no collection membership. It
changes outcomes only.

**The read-only mount costs +26 unit failures** (6 -> 32). Of the 32, **27 do not
appear in the writable run at all**; 5 are common to both.

The 27 read-only-only failures are dominated by `multiprocessing` /
spawn-bridge surfaces:

- `tests/test_probe_owner_phases.py` — 7 (all `test_spawn_*`,
  `test_custom_readiness_*`, `test_none_preserves_*`)
- `tests/test_probe_timed_rom_pair_process.py` — 7 (all `test_process_spawn_*`,
  `test_process_cancellation_bridge_*`)
- `tests/test_probe_timed_rom_pair_menu.py` — 2
- `tests/test_probe_timed_rom_pair_startup.py` — 3, one of which is explicitly
  named `..._without_shared_event_locks`
- `tests/test_probe_timed_trade_pair_driver.py` — 2
- `tests/test_probe_timed_trade_pair_proof.py` — 1
- `tests/test_session_timed_execution.py` — 1
- `tests/test_timed_link_session_control.py` — 2

237 lines in that log mention `/dev/shm`, `Errno 30`, `Read-only file system`, or
`multiprocessing`. The test names say the same thing: these are POSIX
shared-memory and event-lock paths, which a read-only `shm` cannot serve.

## What this does and does not explain

It **does** explain a large part of #253: the read-only mount alone is worth 26
extra unit failures, and it hits exactly the spawn/shared-memory surfaces that
issue #253 is about. The writable mount is load-bearing, as the runbook states.

It **does not** explain the full 201-vs-32 timing gap. The measured read-only
penalty is +26 unit failures. There is no measurement here producing 201 timing
failures, and I did not reproduce that number. Three candidate explanations
remain open:

1. The 201 figure predates merges that removed failures, and the tree genuinely
   improved. Unverified.
2. The 201 figure was measured with a *different* configuration than the wrapper
   — for example the old `bind`-based recipe the runbook critiques at
   `PRODUCTION_RUNBOOK.md:960-980`, which had different `size=` behaviour, or
   with no wrapper at all plus a different `tmpfs` size. Unverified.
3. The 201 figure counted per-repeat occurrences across the 5 timing repeats
   under a harsher load, rather than distinct failing nodeids. The current tree
   yields 32 occurrences / 15 distinct nodeids; a tree roughly 6x flakier would
   reach 201 occurrences without any single test being systematically broken.
   Plausible and untested.

**The reconciliation is therefore incomplete and I am not claiming it.** What is
established: read-only `/dev/shm` costs +26 unit failures and hits only
shared-memory surfaces; the current tree's genuine residual under a correct
mount is 6 unit failures, all `TimedOwnerError` deadline failures in one family.

## Evidence

```
.scratch/shm-ro-unit/ro-unit.log
  sha256 64319c5cb8a8bc02b5d83d9968c6d83aa9aae48f37a568b7384497ff8e2ba54e
```

Raw log retained in full, with the per-test `FAILED` lines, so the 27-test
classification is checkable rather than asserted.

## Standing constraints, unchanged

Release stays **PARTIAL**. #253 stays **open**. No real ROMs, no Cython
interpreter, no Python 3.12, and no sustained quiet CPU window on this host, so
none of the native, real-ROM, compiled-origin, or 3.12-gated rows can be
qualified here.
