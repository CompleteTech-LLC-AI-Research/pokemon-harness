# Root cause of the 7 pre-existing failures in the #253 scoped selection

**Scope.** While validating PR #557, a scoped selection
(`-k "import_origin or packaging or fixture_provenance or artifact or schema"`)
failed 7 rows identically on both the #557 merge tree and unmodified
`origin/master`. This entry establishes *why*, so the set is not
re-investigated as a code defect on every future run.

The 7 rows split into **two different causes**. Neither is caused by #557, and
neither is caused by #555/#556 either.

## Cause 1 — 6 rows: `/dev/shm` is read-only in this sandbox (hard blocker)

Rows, all in `tests/test_probe_owner_phases.py`:

    test_spawn_serializes_and_reports_exact_owner_phase_schema[None]
    test_spawn_serializes_and_reports_exact_owner_phase_schema[phases1]
    ... [phases2] [phases3] [phases4]

Every one fails with:

    OSError: [Errno 30] Read-only file system: '/dev/shm/pym-<pid>-<rand>'
      /usr/lib/python3.11/multiprocessing/heap.py:83: in __init__
      /usr/lib/python3.11/tempfile.py:496: in mkstemp

The tests call `multiprocessing.get_context("spawn").RawArray(...)`
(`tests/test_probe_owner_phases.py:113,132,199`). On Linux CPython allocates
`RawArray` through `multiprocessing/heap.py`, whose arena is created by
`tempfile.mkstemp` **hardcoded to `/dev/shm`** — `TMPDIR` is not consulted.

The mount is read-only in this environment:

    $ mount | grep /dev/shm
    shm on /dev/shm type tmpfs (ro,nosuid,nodev,noexec,relatime,size=64000k,...)

Proven directly, independent of the test suite:

    $ python -c "import multiprocessing; multiprocessing.get_context('spawn').RawArray('B',8)"
    RawArray FAILED: [Errno 30] Read-only file system: '/dev/shm/pym-...-...'

    $ TMPDIR=/workspace/poke-harness/.scratch/tmpdir python -c "same"
    RawArray FAILED: [Errno 30] Read-only file system: '/dev/shm/pym-...-...'

So this is **an environmental blocker, not a defect**. `RawArray` is unusable in
this sandbox by any code path. It cannot be fixed in the repository without
changing what the test asserts (the test deliberately pins that readiness must
not depend on multiprocessing Event locks, `tests/test_probe_owner_phases.py:210`).

Note `scripts/qualification_runner_facts.py:70` already carries
`shm_path="/dev/shm"` and `_probe_shm()` (`:79`) reports `shm_writable=False`
for exactly this case. The repository **already knows how to detect it**; the
gate has no runner that could use the answer, because no runner can allocate shm
here.

## Cause 2 — 1 row: load-sensitive, passes in isolation

Row:

    tests/test_runtime_packaging_bootstrap.py::test_bootstrap_timeout_terminates_posix_process_descendants

This row **passes when run alone** on the merge tree:

    $ python -m pytest tests/test_runtime_packaging_bootstrap.py::test_bootstrap_timeout_terminates_posix_process_descendants -q
    .                                                                        [100%]

and fails in the wider run. Host at measurement time:

    $ cat /proc/loadavg
    7.26 10.63 12.32 5/1186 2940812
    $ nproc
    4

Load 7.26 (and 10.63 / 12.32 on the 1- and 5-minute averages) against 4 cores is
roughly 2x oversubscribed. The row asserts that a **timed** bootstrap kills
POSIX descendants; under contention the timing margin is consumed by scheduling
delay, not by product code.

Per the objective, a longer deadline is not an acceptable fix, and neither is a
skip or xfail. This row needs a **qualified runner** — the same external resource
#85/#86 request and that blocks #253's timing tier.

## Consequence for #253

The issue's acceptance criterion 2 requires
`scripts/production_gate.py --runtime-mode source --unit-only` to report
`overall: PASS` with `failed=0`. On **this host** that is unachievable: 6 of the
11 unit-tier failures cannot pass here at all, because the sandbox mounts
`/dev/shm` read-only and the tests allocate through it. That is a missing
external resource, not a code change, and must not be presented as either a pass
or a fix.

What is now established, and should save the next session the re-derivation:

- these 7 rows are **not** a regression from any open PR;
- 6 of them are **impossible to pass on this host** for a reason outside the repo;
- 1 is **load-sensitive** and needs the qualified runner #85/#86 asks for;
- the same 7-row set fails identically on unmodified `master`, measured
  test-by-test from JUnit XML (`LEAD_20261002_557_CONSTS_REPAIR_VALIDATION.md`).

## Next action

Report the exact missing resource on #253 and #85: a runner with a writable
`/dev/shm` and a declared CPU allocation at or under the required threshold.
Without both, criteria 2 and 3 of #253 cannot be evaluated here.
