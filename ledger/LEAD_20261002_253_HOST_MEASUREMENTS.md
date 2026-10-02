# #253 findings on master `27f1059`

## Host-level blockers that WERE actionable (resolved this run)

1. **`/tmp` 100% full** (512M/512M) — caused by this lead's own completed #534
   scratch checkouts. Reclaimed ~370M; now 28% used. This was a real confound
   that no earlier run had noticed.
2. **`/dev/shm` read-only** (`tmpfs ro,nosuid,nodev,noexec,size=64000k`) —
   `mp.Barrier(2)` fails `OSError: [Errno 30]`. No root/sudo, so a direct
   remount is impossible. **Remedy verified**: `unshare -Urm --propagation
   private` + `mount -t tmpfs -o size=2g tmpfs /dev/shm` -> `BARRIER OK`.
   This is the pattern already documented in `docs/VENDORED_PYBOY_SPLIT_DECISION.md`
   and `docs/PRODUCTION_RUNBOOK.md`.

Effect: the ~24 POSIX-shm unit failures recorded on #253 **all cleared** under
the namespace. Not waived — actually fixed by supplying the required resource.

## Remaining 5-6 failures: genuine deterministic defect, NOT host load

Classification evidence:

- **Deterministic**: failed 3/3 in isolation on a quiet host (load1 ~4-6).
- **Not CPU-bound**: a bare `session.step(1)` costs 60-500 ms measured
  directly, against a 12 s `PAIR_WORK_CAPACITY_S` budget.
- **Not a deadlock**: at t=8s both sockets are still healthy
  (`readable/writable=True`) and frames keep flowing at ~200/s
  (`out_seq` 1820 / `in_seq` 1818). It is a **livelock**: the pair exchanges
  progress frames forever and neither side ever completes its `step`.
- **Where it blocks** (`faulthandler.dump_traceback` at t=3s, budget 9s left):
  `session.step` -> `timed_link_session._wait_for_progress`
  (`link/timed_link_session.py:661`) -> `timed_wire.receive` (`:522`),
  with the peer simultaneously in `send_complete_progress` -> `_send`.
- **Permits are granted**: `EmulatedTimeCoordinator.reserve` returns OK
  (~8500 calls, 2 refusals) — so this is not a permit-starvation defect.

## Pre-existing, not a regression

- `git diff --name-only 7587bba 27f1059` shows PR #537 (#534) touched **no**
  link/ or `mcp_timed` or `session` file.
- `git diff --stat 812a852 origin/master -- src/pokered_harness/link/
  tests/_mcp_timed_remote_support.py` is **empty**: byte-identical to the
  original #253 report point.
- Reproduced at a clean detached checkout of `812a852` (the SHA named in the
  issue body): `test_queued_cancel_preserves_active_real_epoch` FAILED.

So #253's unit-tier failures are a long-standing defect in the timed-link
step/progress exchange, previously masked by the read-only `/dev/shm` masking
the whole subprocess-spawning cluster.

## Remaining external blocker (criterion 3, unchanged)

Timing tier needs a qualified runner (`load1 <= 4`, `cpu.pressure avg300`
under the declared policy). This host is 4 cores and never sustained a quiet
window. That is #85/#86, both open.

## Corrected diagnosis of the remaining failures (measured, not inferred)

The earlier "livelock" reading was wrong. The pair is **not** spinning forever.
With a large policy timeout the step **always completes** — it is simply far
slower than the budgets the tests allow.

Measured cost of one paired `step(1)`, using the pair built from the *authored*
ROM in `tests/_mcp_timed_remote_support.py` (no real ROM needed), n=10:

    min=5.78s  median=8.61s  max=20.44s

Test budgets in force:

| Test | budget | result |
|---|---|---|
| `test_duplicate_connection_...[listen/connect]` | `BOUND` = 5.0s | FAIL (max 20.4s) |
| `test_queued_cancel_preserves_active_real_epoch` | `PAIR_WORK_CAPACITY_S` = 12.0s | FAIL (max 20.4s) |

So the budgets sit **below the operation's real cost**, and the outcome flips
with host load. This is a genuine defect in the operation, not noise.

### Where the time goes (cProfile, one paired step, elapsed 11.37s)

    15048/3  mb.py:565(_execution_step)             cumtime 11.364s   <- 15,048 CPU steps
    27458    timed_link_session.py:619(_safe_pump)     4.239s
    5018     timed_wire.py:405(send_complete_progress) 1.918s
    173590   emulated_time.py:269(snapshot)            1.859s
    6204     timed_wire.py:514(receive)               11.653s (cumulative, blocking)

Mechanism:

1. A Game Boy frame is ~70,224 CPU cycles. The vendored governor
   (`mb_components/mb_coord_002_596600ba5832.pxi:29`) calls
   `execution_before(...)` with `required=24` cycles for **every** instruction
   batch, so one frame becomes ~15,048 governed steps.
2. Each governed step calls `_TimedAdapter.before` -> `session._safe_pump()`
   -> `_wait_for_progress` -> `TimedWireChannel.receive`, i.e. a full
   cross-socket round trip per step.
3. `_publish` gates transmission on
   `self._threshold = max(1, quantum_cycles // 2)`, so it does send progress
   during the frame — the sockets stay busy (~200 frames/s observed) — but the
   per-step pump/receive overhead (~0.75 ms/step) dominates.

Raising `quantum_cycles` does **not** fix it (measured):

    quantum=256   times=[9.12, 7.30, 7.04]
    quantum=1024  times=[5.88, 13.99, 9.13]
    quantum=4096  times=[13.80, 18.40, 13.31]

so the cost is in the unconditional per-step pump, not in permit granularity.

### Scope note

This is **performance**, and the repo already owns it:
`#109 Profile and optimize the Python instruction-stepping loop with a bounded
compiled path`. The same exchange is also the subject of `#84` (timed MCP frame
deadlines under qualification). Neither is closed. Repairing #253's unit tier
therefore means fixing the per-step pump cost in `src/pokered_harness/link/`,
which is #109/#84 scope — not a #253-local change, and not something to land as
an unreviewed drive-by.

## Verified execution results on master `27f1059` (this run)

Environment: `unshare -Urm --propagation private` + private 2G tmpfs on
`/dev/shm`, source runtime, CPython 3.12.14, `--basetemp` on durable storage.

| scope | result |
|---|---|
| heaviest 12 unit files (3,057 tests) | **100%, 0 failed, 0 errors** |
| `test_mcp_timed_remote_owner.py` + `test_pyboy_link_session.py` + `test_execution_adapter.py` | 5 failed / 220 passed |

The 5 failures are exactly the diagnosed set:

    test_real_partial_progress_active_interrupt_is_terminal[cancel]
    test_real_partial_progress_active_interrupt_is_terminal[deadline]
    test_duplicate_connection_preserves_existing_epoch_and_execution[listen]
    test_duplicate_connection_preserves_existing_epoch_and_execution[connect]
    test_mcp_tools_and_resource_share_persistent_native_owner

Plus, measured separately and deterministically (3/3):

    test_mcp_timed_remote_cached_failure.py::test_queued_cancel_preserves_active_real_epoch
    tests/test_mcp_timed_stdio.py::test_authored_timed_stdio_pair_frames_and_cleanup[peer_eof|disconnect]

### Full unit tier cannot be completed on this host

Two hard environment limits, both external and both operator-owned:

1. **Memory.** `memory.events` records `oom_kill`; ~7.9 GB shared with other
   tenants (e.g. an unrelated `tsc` build was resident during measurement).
   The 8,089-test unit tier does not complete in one process here. It was run
   sharded (heaviest-first) to get the results above.
2. **CPU admission.** 4 cores; `load1` did not stay at or below the repo's
   `load1 <= 4` qualification threshold for a sustained window. That is #85/#86.

Neither is waivable, and neither is a repository defect.

## Posted evidence

- #109: profile + decision-shaping evidence
  https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/issues/109#issuecomment-5952990111
- #253: corrected classification and criteria status
  https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/issues/253#issuecomment-5953024086
- #84: root cause for the timed-frame deadline family
  https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/issues/84#issuecomment-5953057472
