# LEAD — #253 timing tier re-measured: 18 failures, one environmental cause

This closes the outstanding measurement gap in
`LEAD_20261002_253_FAILURE_DECOMPOSITION.md`, which decomposed the **unit**
tier but left the **201 timing-tier failures** unmeasured. They are now
measured, classified, and disposed with no waiver.

## Setup

```
worktree   : /workspace/poke-harness/.scratch/m253timing   (detached at origin/master)
HEAD       : 2cd4bb1c137bc7d586e0590f75c79e1bb2d6bda2
interpreter: /workspace/poke-harness/pokemon/.venv/bin/python   (the documented one)
PYTHONPATH : <worktree>/src : <worktree>/vendor/pyboy-src
```

`PYTHONPATH` is bound to the worktree so the primary editable install is not
touched and no shared `.pth` had to be modified.

Host facts, re-read at run time:

```
/dev/shm : tmpfs ro,nosuid,nodev,noexec,size=64000k   <-- READ-ONLY
nproc    : 4
loadavg  : 3.16 4.00 4.51
```

## The tier is 425 rows, not 201

```
$ pytest -m timing_sensitive --collect-only
425/8354 tests collected (7929 deselected) in 5.09s
```

The issue title's "201" is stale. The tier as declared by
`_TIER_MATCHERS["timing"] = (("timing_sensitive",), ())` selects **425** rows
across 63 files. The headline number in #253 should be corrected; the row
count did not change the disposition.

## Result: 18 failures, not 201

```
$ pytest -m timing_sensitive -q --junitxml=target/lead253/timing.xml
rc=1     18 failed, 407 passed
```

Re-run to test determinism — **identical 18 rows** both times:

```
$ pytest -m timing_sensitive -q          # second run
rc=1     18 failed, 407 passed
diff run1 run2  ->  IDENTICAL
```

### Every one of the 18 is the same cause

Classified straight from the JUnit XML, not from reading the summary:

```
total failures=18   with read-only-shm signature=18   other=0
```

All 18 abort at **process spawn, before any assertion runs**:

```
scripts/_probe_timed_rom_pair_support.py:604: in run_process_pair
    cancel = _SharedFlag(context.RawValue("B", 0))
multiprocessing/sharedctypes.py:41: in _new_value
OSError: [Errno 30] Read-only file system: '/dev/shm/pym-3037493-hzuclzq4'
```

By file:

| file | rows |
|---|---:|
| `tests/test_probe_owner_phases.py` | 5 |
| `tests/test_probe_timed_rom_pair_process.py` | 8 |
| `tests/test_probe_timed_rom_pair_startup.py` | 3 |
| `tests/test_probe_timed_rom_pair_menu.py` | 2 |

## Mechanism — proven, not inferred

`multiprocessing/heap.py` hard-codes the shared-memory directory on Linux:

```python
if sys.platform == 'linux':
    _dir_candidates = ['/dev/shm']
```

`Arena._choose_dir` returns the first candidate with enough free space, then
`tempfile.mkstemp` creates the file. The mount is `ro`, so the create fails
with `EROFS` (errno 30). `statvfs` still reports free space — the mount is
read-only, not full — which is why the choice is made and then fails.

Isolated counterfactual, changing only the arena directory:

```
BEFORE redirect: RawValue FAILED -> [Errno 30] Read-only file system
AFTER redirect:  RawValue OK, value=7
```

## A second, independent blocker sits behind the first

Redirecting the arena off `/dev/shm` is a real fix for arenas — and it exposes
the *next* wall. POSIX semaphores come from the C extension
`_multiprocessing.SemLock`, not from `heap.py`, so the pure-Python redirect
cannot reach them:

```
$ SemLock(1, 1, 1, ctx=spawn)
SemLock FAILED -> OSError(30, 'Read-only file system')
.../multiprocessing/synchronize.py:57: OSError
```

Re-running the four affected files with the arena redirected still fails, now
at `synchronize.py:57` instead of `sharedctypes.py:41`. Same 18 rows, second
mechanism.

**Both are host-level.** Neither can be fixed from inside the repository, and
neither is a code defect. This is exactly the missing external resource the
authorization anticipated.

## Disposition — no waiver

The 18 timing-tier rows are **environmental**, caused by a read-only
`/dev/shm`, in two independent layers:

1. `multiprocessing.heap.Arena` -> `tempfile.mkstemp` in `/dev/shm`
2. `_multiprocessing.SemLock` -> POSIX named semaphore in `/dev/shm`

No deterministic source defect was found in any of the 425 timing rows. The
other 407 pass on this host under load average ~3-4 on 4 CPUs, which also
argues against the earlier "timing contention" framing for the residue.

This is **not** a claim that these rows pass. They are unverified. Per the
merge contract, no finding, required test, or resource requirement is waived:

- #253 **stays open.**
- These 18 rows are **not** recorded as passing, and are not marked xfail/skip.
- Closing #253 requires a runner with a **writable `/dev/shm`** (plus, for
  the timing question proper, a controlled CPU allocation). Neither is
  available here.

Combined with the unit tier, the picture for #253 is now complete and
consistent: 20 unit + 18 timing = 38 rows blocked on the same read-only
`/dev/shm`, and **zero** deterministic defects found in either tier.

## Evidence

Disposable worktree, removed after measurement. Hashes (sha256, first 16):

```
83d8aa0c7d918452  target/lead253/timing.log          (run 1)
ceadab8f0f424a97  target/lead253/timing_rerun.log   (run 2, identical failures)
72dd34676b0f0722  target/lead253/timing.xml         (run 1 JUnit)
9c5912de13b1a6c5  target/lead253/timing_shm.log     (redirect attempt, layer 2)
364bbabd8368fb36  target/lead253/arena_probe.py     (the counterfactual)
```

No repository source or test file was modified. The redirect was applied via
`sitecustomize` under the worktree's own `target/`, so the shared install and
every other worktree were left untouched.
