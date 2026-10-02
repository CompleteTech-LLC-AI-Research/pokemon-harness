# #253 decomposition: what is a real defect and what is this sandbox

Date: 2026-10-02
Tree: `origin/master` `9101c5f`, worktree `/workspace/poke-harness/.scratch/m253`
Status: **#253's 11 unit-tier failures are not 11 defects on master.**

## Why this was re-measured

#253 was filed against `812a852` with a Python 3.12.14 / pytest 9.1.1 host and
is still open. I re-ran the named failing files on current master rather than
reusing the historical counts.

## Method note (this mattered)

My first pass used the shared scratch venv, which is missing
`pytest-asyncio`. That alone produced `async def functions are not natively
supported` failures that look like repo defects but are not. Re-measured with
the **documented** interpreter `pokemon/.venv` (pytest 9.1.1,
pytest-asyncio 1.4.0) and `PYTHONPATH` bound to the master worktree, so the
measurement is of master's code rather than of my environment.

## Result: the failures split three ways

| cause | count | real defect? |
|---|---|---|
| `/dev/shm` mounted read-only | 20 | **no** — environmental |
| `pytest-asyncio` absent in my scratch venv | 2 | **no** — my measurement error |
| `request deadline expired` under load | at least 1 | **flaky, not deterministic** |
| everything else | 0 identified | — |

### 1. `/dev/shm` is read-only in this sandbox — 20 failures

```
$ mount | grep shm
shm on /dev/shm type tmpfs (ro,nosuid,nodev,noexec,relatime,size=64000k,...)

$ python -c "import multiprocessing as mp; mp.get_context('fork').RawValue('B',0)"
RawValue FAILED: [Errno 30] Read-only file system: '/dev/shm/pym-...'
```

Every failure in these files is that one line:

```
tests/test_probe_owner_phases.py:228 -> scripts/_probe_timed_rom_pair_support.py:604
    cancel = _SharedFlag(context.RawValue("B", 0))
OSError: [Errno 30] Read-only file system: '/dev/shm/pym-...'
```

`multiprocessing` allocates POSIX shared memory via `tempfile.mkstemp` in
`/dev/shm` for POSIX contexts, so `TMPDIR` does **not** redirect it — I
verified that separately. On this host no test that touches `RawValue`,
`Value`, or `Array` can pass, regardless of the code under test.

Measured: `test_probe_owner_phases.py` 7/7, `test_probe_timed_rom_pair_menu.py`
2/2, `test_probe_timed_rom_pair_process.py` 8/8, `test_probe_timed_rom_pair_startup.py`
3/3 — **20 failures, all the same environmental cause.**

### 2. `test_queued_cancel_preserves_active_real_epoch` is load-flaky

The one non-`/dev/shm` failure on the documented interpreter:

```
TimedOwnerError: request deadline expired
  at src/pokered_harness/mcp_timed_owner.py:142
```

Run five times in isolation: **F, F, F, ., .** — it passes, and fails,
intermittently. Host load during the measurements was **10.08 on 4 cores**.
That is the profile of a deadline-budget test losing its budget under
contention, not a deterministic logic defect. I am not calling it clean and I
am not calling it broken: it needs a quiet CPU window to measure, which
#85/#86 say does not exist here.

## Consequence for #253

The issue's acceptance criterion 1 asks that every unit-tier failure be fixed
or given a documented, non-waived disposition. This measurement supplies that
disposition for the failures measured so far:

- **20 are environmental** (`/dev/shm` read-only in this sandbox) and cannot be
  dispositioned as pass or fail on this host. They need a runner with a
  writable `/dev/shm` — the same missing allocation as #85/#86.
- **2 were my measurement error** and disappear on the documented interpreter.
- **1 is load-flaky** and needs a quiet window.

Second pass, on the documented interpreter, added one more unmeasurable case
(`test_duplicate_connection_preserves_existing_epoch_and_execution[listen]`,
`FF` in one worktree and `..` in another minutes apart), bringing the
non-deterministic set to 2.

**Not a single deterministic source defect was found in the measured files.**
That is a materially different position from the issue's "11 unit-tier and 201
timing-tier failures" and should be re-stated before anyone repairs code.

## Addendum: `test_mcp_timed_remote_owner.py` re-measured

The gap flagged above is now closed. On the documented interpreter this file
shows **1 failure, not 30** — the other 29 were the missing async plugin.

```
tests/test_mcp_timed_remote_owner.py   FAILED=1   Read-only=0
  test_duplicate_connection_preserves_existing_epoch_and_execution[listen]
```

I then tried to separate a real defect from host contention, and the evidence
says contention. Same tree, same interpreter, back to back:

```
in the master worktree:   FF   FF   FF   FF     (load 5.18)
in the primary checkout:  FF   ..                (load 4.44)
```

Identical code, identical interpreter, opposite results minutes apart. The
support module budgets each request at `BOUND = 5.0` seconds
(`tests/_mcp_timed_remote_support.py:18`), and the failures are
`TimedOwnerError: request deadline expired` at `mcp_timed_owner.py:142` — the
budget being missed, not a wrong value being produced. On a 4-core host running
at load 4–5, that is contention, and it is the same missing CPU allocation
#85/#86 describe.

I am not recording this as a source defect and I am not recording it as clean.
It is **unmeasurable on this host**, and it needs the same quiet CPU window
that #164 already lacked.

## What is NOT established

- The 201 **timing-tier** failures were not re-measured. They are the larger
  half of #253 and are not covered by this note.
- Nothing here closes #253.

## Disposition

#253 stays open, with a corrected partial diagnosis attached. No code changed.
No skip, xfail, or deadline relaxation was introduced. Release stays `PARTIAL`.
