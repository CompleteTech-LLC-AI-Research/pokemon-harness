# Local CI on the #555 merge tree: lint fixed, timing tier red pre-existing

The documented local CI command is `bash scripts/run_local_ci.sh`
(`docs/CI_POLICY.md:33`). It requires an activated venv (`VIRTUAL_ENV` set) or
it exits 2 immediately.

## Before the lint repair: master and #555 both fail at lint

On the #555 merge tree (`mt555a`, master + `36d27c9`):

```
$ bash scripts/run_local_ci.sh
SIM117 Use a single `with` statement with multiple contexts ...
  --> tests/test_import_origin_guard.py:772:5
SIM117 [*] ... --> tests/test_import_origin_guard.py:795:5
Found 2 errors.
overall: (exits 1 before running any test)
```

Verified these are **pre-existing on master**: running ruff against master's
own copy of the file (`git show origin/master:tests/test_import_origin_guard.py`)
reports the same two errors. They arrived with PR #553, so **#553 merged a red
local CI to trunk**.

Fix: `89c7152` on `fix/551-553-close-mutation-gaps` merges the two `with`
statements. Mechanical, no semantic change; the affected rows still pass and
`ruff check` / `ruff format --check` are clean afterwards.

## After the lint repair: lint passes, timing tier fails

On the merge tree with `89c7152` (`mt555b`), the run gets past lint, packaging,
the tracked-artifact policy and the network concurrency probe
(`network concurrency probe: PASS (8 probes, 0.493s)`), then fails in the
**timing-sensitive** tier:

```
26 failed, 399 passed, 7939 deselected, 3 warnings in 391.59s (0:06:31)
overall: FAIL
```

Failing files: `test_probe_timed_rom_pair_process.py` (16),
`test_probe_owner_phases.py` (7), `test_probe_timed_rom_pair_startup.py` (6),
`test_session_timed_execution.py` (5), `test_timed_link_session_control.py` (4),
`test_probe_timed_rom_pair_menu.py` (4), `test_mcp_timed_stdio.py` (2),
`test_mcp_timed_remote_owner.py` (2).

## These are not caused by #555

None of these files touch the import-origin guard. Reproduced on **unmodified
master** in its own worktree and its own venv (`mtmaster`):

```
MASTER (no #555): 32 tests, 8 failed, 0 errors
  test_custom_readiness_keeps_order_and_rejects_unknown_phase
  test_none_preserves_trade_phase_names_and_default_readiness_schema
  test_real_pair_repeated_public_frames_bounded_wire_volume[delayed-byte]
  test_spawn_serializes_and_reports_exact_owner_phase_schema[None..phases4]
```

The same tests fail without #555 applied, so the timing-tier redness is
pre-existing on trunk.

## Why the timing tier is red here

Two contributing conditions, both environmental and both matching the
already-tracked blockers:

1. **No ROM in the checkout.** The `*_timed_rom_pair_*` rows need a real ROM;
   objective item "no real-ROM result" applies and release status stays
   **PARTIAL**.
2. **CPU contention.** `nproc` is 4 and load average reached **15.6** during the
   run. A second session's `production_gate.py --unit-only`
   (`/home/agent/probe/r551sync`) was running concurrently on the same host.
   The failures are thread/process timing assertions, which is exactly what
   contention breaks. This is the same resource blocker tracked by #85/#86
   (no declared CPU allocation).

## Status

| item | state |
| --- | --- |
| `ruff check` / `ruff format --check` on the merge tree | clean |
| guard + local-CI-policy suites | 107 tests, 0 failures |
| both false-PASS repros on the merge tree | FAIL (correct) |
| own-checkout CLI | rc=0 |
| local CI unit tier | passed |
| local CI timing tier | 26 failures, **pre-existing on master** |
| real-ROM qualification | **not run**, no ROM present |
| independent review | **blocked on #489** |

#555 is not a regression. It repairs a red lint that #553 introduced, and the
remaining timing-tier redness belongs to trunk and needs either a ROM or a
declared CPU allocation.
