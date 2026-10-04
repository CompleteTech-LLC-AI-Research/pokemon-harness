# #253 — the writable-`/dev/shm` recipe from #577 is wrong as written

Date: 2026-10-04
Master measured: `eb7f14d8`
No repository file was changed. This is a measurement record and a
correction to `docs/PRODUCTION_RUNBOOK.md` §3b-0 as written by #577.

## The defect

#577 documented this recipe:

```sh
unshare --map-root-user -m --propagation private \
  sh -c 'mount -t tmpfs -o size=256m tmpfs /dev/shm; <pytest>'
```

It produces a writable `/dev/shm`, which is the point. It also maps the
caller's uid 1000 to **0**, so the whole run executes as root — and five
tests in this repo *require* a non-root uid.

Those five build an unwritable location and assert the production code
refuses to proceed. As root the write succeeds, the refusal never fires, and
the test fails:

```
tests/test_qualification_runner_release.py::test_unwritable_host_lock_directory_refuses_to_launch
tests/test_qualification_runner_lockstate.py::test_immutable_asset_check_rejects_an_unlistable_directory
tests/test_qualification_runner_lockstate.py::test_immutable_asset_check_rejects_a_symlinked_directory
tests/test_qualification_runner_allocation.py::test_write_job_run_record_is_terminal_when_the_record_cannot_be_written
tests/test_gate_capacity_policy.py::test_stalled_live_observer_cannot_admit_using_stale_health
```

Measured, same tree, same interpreter, each test run twice:

| condition | result |
|---|---|
| no namespace (`id -u` = 1000) | **5/5 pass** |
| `--map-root-user` namespace (`id -u` = 0) | **5/5 fail** |

So #577's recipe **trades 8 environment failures for 5 different ones.**
It is not a clean fix, and this record supersedes it.

## Why the obvious repairs do not work

Each was attempted and measured, not assumed:

| attempt | result |
|---|---|
| `unshare --mount` without `--map-root-user` | `unshare failed: Operation not permitted` — mounting needs `CAP_SYS_ADMIN`, which only exists inside the userns |
| `mount --bind /tmp/pokered-shm /dev/shm` | shm is rw and owned by uid 1000, but `id -u` is **still 0**; bind ownership ≠ process uid |
| `setpriv --reuid=1000` / `os.setuid(1000)` | `setresuid failed: Invalid argument` — `uid_map` is the single entry `0 1000 1`, so uid 1000 does not exist inside the namespace |
| write `/proc/self/uid_map` from inside | fails; already locked |
| `unshare --map-auto` | `no line matching user "agent" in /etc/subuid` |
| `unshare --map-users=1000,1000,1 --map-users=0,0,1` | `failed to execute newuidmap: No such file or directory` |

## The correction: a nested user namespace

Mount in the outer namespace as root, then `exec` into an inner user
namespace that maps uid 1000. The inner process keeps the inherited mount
namespace, so it still sees the writable `/dev/shm`.

```sh
mkdir -p /tmp/pokered-shm
mount --bind /tmp/pokered-shm /dev/shm
exec unshare --map-user=1000 --map-group=1000 --mount --propagation private "$@"
```

invoked as `unshare --map-root-user -m --propagation private <script> <cmd>`.

Measured inside the result:

```
id -u                          -> 1000
/dev/shm                       -> tmpfs (rw,nosuid,nodev,...)
multiprocessing fork + Queue   -> OK
the five non-root tests        -> 5/5 pass
tests/test_probe_owner_phases.py (fails on the read-only mount) -> green
```

## Full gate on `eb7f14d8`, source runtime, corrected wrapper

```
scripts/production_gate.py --runtime-mode source --unit-only \
  --repeat-timing 5 --timeout-seconds 1800

FAIL  unit    total=8327 passed=8313 failed=14 skipped=0 xfailed=0 xpassed=0 errors=0 duration=1809.8s
FAIL  timing  total=2125 passed=2090 failed=35 skipped=0 xfailed=0 xpassed=0 errors=0 duration=1711.4s
overall: FAIL
```

None of the 14 unit failures are the five namespace-sensitive tests —
verified programmatically: **0** matches for `qualification_runner` or
`gate_capacity_policy` in the failure list.

For contrast, the same gate under the **#577 recipe** ran the unit tier at
`total=3925` and hit the default 900 s pytest timeout, reporting five of
those five regressions plus a skip that the corrected run does not produce.

## `returncodes=[124]` is not truncation — verified, not assumed

The unit tier records `returncodes=[124]` and "pytest timed out after
1800.0s", which invites the reading that results were cut short. They were
not. Two independent reasons:

1. `total = passed + failed = 8313 + 14 = 8327`, equal to the tier total.
2. The counts do not come from pytest's human summary — they come from
   `tests/_gate_report.py`, a plugin that checkpoints one terminal record per
   test to `POKERED_GATE_REPORT` every 32 tests / 2 s. `scripts/production_gate_runtime.py`
   refuses the report unless `collected == len(records)`, so a truncated run
   cannot present full-looking counts.

The absent pytest summary line in `raw/source/unit-1.log` is consistent with
this: the process was killed after the last record was checkpointed, while
the terminal summary had not yet been rendered. **What is not established**
is *why* teardown ran long — that is a separate question and no claim is made
about it.

## Independent review

A fresh independent reviewer confirmed the causal story (uid 0 + these five
tests is the only explanation consistent with the measurements), judged the
nested fix sound, and returned **MERGEABLE WITH MINOR NOTES**. Its four notes
were each checked and are reflected here:

- confirm the wrapper really begins with `set -e` (it does);
- *state how* 124-vs-teardown was distinguished rather than inferring it (see
  the two independent reasons above);
- tighten "arena allocation" to the primitive that actually fails — the
  POSIX semaphore created under `/dev/shm`. Anonymous `mmap` succeeds and
  `TMPDIR`-backed `tempfile` succeeds on a read-only `/dev/shm`;
- note that the bind target `/tmp/pokered-shm` is disk-backed, not tmpfs.
  That does not weaken the result: the failing primitive needs a *writable*
  `/dev/shm`, not a tmpfs-backed one.

## What this does not change

- **Capacity remains BLOCKED.** Remounting `cgroup2` inside the same private
  namespace still returns `EPERM`; `cpu.max` remains `max 100000` on 4 CPUs
  with no writable leaf. No allocation is claimed.
- **#253 stays open.** 14 unit-tier and 35 timing failures remain. The gate
  is FAIL.
- **No gate is claimed as passing, no test skipped or xfailed, no deadline
  enlarged** to obtain any number above.
- The 14 unit failures carry the same `timed_deadline` profile recorded
  before; their cause is still not established, and the non-reproducibility
  noted in the previous session still stands.

## Next action

Fix the §3b-0 recipe in `docs/PRODUCTION_RUNBOOK.md` to the nested-namespace
form, then re-measure the 14 residual failures on an operator-declared
allocation. Until that exists, #253's acceptance criterion 1 is unmet.
