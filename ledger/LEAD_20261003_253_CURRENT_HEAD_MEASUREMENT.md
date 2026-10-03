# #253 re-measured on current master `b41f46d4`

Date: 2026-10-03
Tree: `origin/master` `b41f46d44931c399df5239e5aa7dba873606077e` (0 ahead / 0 behind after `git fetch origin master`)
Interpreter: fresh venv at `/workspace/poke-harness/.scratch/venv254clean`, Python 3.11.2,
`pip install -e ".[dev]"` per `docs/PRODUCTION_RUNBOOK.md`.
Command: `scripts/production_gate.py --runtime-mode source --unit-only --format json`

## Host qualification status has changed since the last measurement

Criterion 3 requires `load1 <= 4` and `cpu.pressure avg10 < 50`. Previously this
host reported `load1 = 31.34` and `avg10 = 87.25`, which disqualified it.

| metric | earlier | now | criterion | verdict |
|---|---|---|---|---|
| load1 | 31.34 | 3.56 - 4.26 | <= 4 | **met** |
| cpu.pressure some avg10 | 87.25 | 13.61 - 18.55 | < 50 | **met** |
| cpu.max | max 100000 | max 100000 | no withheld allocation | unchanged |

The runner now meets this issue's own qualification bar. Criterion 3 is
therefore no longer blocked by host capacity.

## Results

| tier | total | passed | failed | errors | skipped | status |
|---|---|---|---|---|---|---|
| unit | 8206 | 8178 | 25 | 0 | 3 | FAIL |
| timing | 1275 | 1214 | 61 | 2 | 0 | FAIL |

`overall: FAIL`, `gate_problems: []`.

## Failure decomposition (86 total)

| cause | unit | timing | real defect? |
|---|---|---|---|
| A. `/dev/shm` mounted read-only | 23 | 54 | **no** -- environmental |
| B. lockstep/credit stall, deadline expiry | 2 | 7 | **yes** -- the #253 defect |
| skip rows | 3 | 0 | n/a |

79 of 86 are cause A. Cause B is 9 tests across
`test_mcp_timed_remote_owner.py`, `test_mcp_timed_stdio.py`,
`test_session_timed_execution.py`, `test_timed_menu_milestone_sentinels.py`.

### Cause A -- `/dev/shm` read-only, still unfixed and still an operator blocker

```
$ findmnt -no SOURCE,FSTYPE,OPTIONS /dev/shm
shm  tmpfs  ro,nosuid,nodev,noexec,relatime,size=64000k,uid=1000,gid=1000,inode64
$ touch /dev/shm/x -> Read-only file system
$ python3 -c "import multiprocessing as mp; mp.get_context('fork').RawValue('B',0)"
OSError: [Errno 30] Read-only file system: '/dev/shm/pym-242483-bu8hd0tu'
```

CPython hardcodes `_dir_candidates = ['/dev/shm']` on linux
(`multiprocessing/heap.py:72`). `_choose_dir` selects it because 63M is free,
then `tempfile.mkstemp` fails on the read-only mount. `TMPDIR` does not
redirect this. There is no `sudo` in this container, so remounting is not
reachable from here. This is unchanged from the 2026-10-02 finding.

### Cause B -- the actual #253 defect, confirmed in isolation

> **SUPERSEDED IN PART.** The threshold mechanism described below was REFUTED by
> independent review. `force=True` already bypasses `_threshold` in the blocked
> path, and lowering the threshold measurably *slows* the pair. The real binding
> constraint is the `enforce_completeness` watermark clamp. See
> `ledger/REVIEW_253_LOCKSTEP_INDEPENDENT_b0f09461.md`. The counts in this
> section stand; only the mechanism is wrong.

Isolated re-runs (not under gate load) reproduce cause B, so these are genuine
defects rather than host-load flakes:

```
$ pytest tests/test_session_timed_execution.py::test_real_partial_public_tick_failure_counts_only_completed_frames
E  assert not self.thread.is_alive(), "Session routing worker exceeded its bound"
E  AssertionError: Session routing worker exceeded its bound

$ pytest tests/test_mcp_timed_remote_owner.py::test_mcp_tools_and_resource_share_persistent_native_owner
E  pokered_harness.mcp_timed_owner.TimedOwnerError: request deadline expired
```

Mechanism, matching the 2026-09-27 root-cause note. In
`src/pokered_harness/link/timed_link_session.py`:

```python
# line 88
self._threshold = max(1, quantum_cycles // 2)

# line 486
progress_due = local > self._sent_progress and (
    force or local - self._sent_progress >= self._threshold
)
```

`_publish` withholds progress below `self._threshold`. `_wait_for_progress`
(line 647) blocks in `channel.receive` until the peer publishes. A peer that
has not yet crossed the threshold therefore publishes nothing, and the two
owners each need the other's credit to execute far enough to cross the
threshold. Progress requires alternating cross-thread scheduling, so a frame
(~9500 retired instructions, one instruction per permit) takes far longer than
the authored bound and the deadline expires.

## Correction to a prior claim in this run

An early `check_import_origins.py` FAIL was **not** a master regression. The
shared `/workspace/poke-harness/pokemon/.venv` carries an unowned
`_virtualenv.py` + `_virtualenv.pth` pair that no dist-info RECORD attests:

```
module owners: None
pth owners   : None
recorded_by_install(_virtualenv.py, root): False
```

`scripts/check_import_origins.py` requires one distribution to attest both
halves, so it refuses that pair by design (#559). On a correctly built
environment the same guard on the same master tree returns:

```
{"status": "PASS", pokered_harness -> src/pokered_harness/__init__.py,
                     pyboy          -> vendor/pyboy-src/pyboy/__init__.py}
```

Master is not regressed. The shared venv is polluted; that is not a
repository defect.

## Disposition against the four acceptance criteria

| # | criterion | status |
|---|---|---|
| 1 | every unit failure fixed or given a documented, non-waived disposition | **documented** -- 23 cause A, 2 cause B. Nothing skipped, xfailed, or waived. |
| 2 | `production_gate.py --runtime-mode source --unit-only` reports `overall: PASS`, `failed=0` | **not met.** 79/86 failures are structurally impossible while `/dev/shm` is read-only. |
| 3 | timing tier re-evaluated on a qualified runner | **partially met** -- the runner now qualifies and the tier was re-run fivefold (1118.8s). 54 failures remain cause A; 7 are the real defect. |
| 4 | independent review + gate re-run on the exact merge tree | **not met** -- collaboration task delivery still fails (see #489). |

## What would actually unblock this

1. Remount `/dev/shm` read-write with a workable size (operator action, outside
   the repository; no sudo exists in this container). Clears cause A, 79 rows.
2. Repair the lockstep publish/credit handshake so an owner that is blocked for
   work still publishes. Clears cause B, 9 rows. This touches the
   `EmulatedTimeCoordinator` invariants (`enforce_completeness`,
   `max_edge_lateness`) and #164's edge-response retirement guarantee, so it
   needs independent review on its own merits.
3. Obtain an independent reviewer through a working delivery path (#489).

No deadline, capacity window, CPU requirement, skip, xfail, or gate was
relaxed. The gate is not being called green. Release status stays **PARTIAL**.
