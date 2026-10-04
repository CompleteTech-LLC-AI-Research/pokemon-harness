# Lead triage — #253 unit-tier failures are one paired timed-owner deadlock

`origin/master` `eb7f14d8` throughout. Worktree `/home/agent/wt253`, branch
`fix/253-unit-tier-failures`, **no source change** — this is measurement only.
Nothing merged, nothing marked ready, no issue closed.

## The headline: the 35 "failures" were mostly three different things

`pytest tests -m unit` on `eb7f14d8`, three conditions, same tree, same command,
interpreter bound to the checkout (`conftest.py` refuses a cross-checkout
interpreter, and it caught me once when I tried to reuse one):

| condition | unit failures |
|---|---|
| host default mount — `/dev/shm` read-only | **35** |
| writable `/dev/shm`, **uid 0** | 6 |
| writable `/dev/shm`, **uid 1000** | **12** |

**(a) `/dev/shm` read-only is an environment artifact, not a product defect.**
`tests/test_probe_owner_phases.py` alone is 7 of the 35:

```
multiprocessing.get_context("spawn").RawArray("B", 16)
  -> heap.Arena -> tempfile.mkstemp(dir="/dev/shm", ...)
OSError: [Errno 30] Read-only file system: '/dev/shm/pym-...'
```

With a writable `/dev/shm` that file is **fully green, 22 passed**,
deterministically. #577's runbook entry already records that a writable
`/dev/shm` is obtainable on this host; it is real and I reproduced it.

**(b) The root-vs-non-root distinction is not recorded anywhere, and it
inverts the conclusion.** Inside `unshare --map-root-user` you are uid 0, which
bypasses mode bits. Three rows then fail for reasons unrelated to the product:

- `test_qualification_runner_lockstate.py::test_immutable_asset_check_rejects_a_symlinked_directory`
  — asserts a mode-0555 root is caught; as root it *is* writable, so the row
  gets `'the asset root is writable by this job'` instead of `'symbolic link'`
- `test_qualification_runner_lockstate.py::test_immutable_asset_check_rejects_an_unlistable_directory`
- `test_qualification_runner_release.py::test_unwritable_host_lock_directory_refuses_to_launch`

All three pass at uid 1000 (**59 passed** across those files). So a
writable-shm harness that runs as root reports 6 and looks *better* than the
correct 12. It is a different, wrong measurement — worth recording so nobody
"fixes" the gate by picking the flattering harness.

The harness that is actually correct (writable shm **and** uid preserved):

```bash
unshare --map-root-user -m --propagation private \
  bash -c 'mount -t tmpfs -o size=1g,mode=1777 tmpfs /dev/shm && \
           exec unshare --map-user=1000 --map-group=1000 -m --propagation private "$@"' _ <cmd>
```

A single `unshare --map-root-user` does **not** give this.

**(c) The residual is one real defect, not twelve.** See below.

## The defect: mutual deadlock in the paired timed-owner path

`tests/test_mcp_timed_remote_cached_failure.py::test_queued_cancel_preserves_active_real_epoch`
fails **3/3 in isolation** on pristine `eb7f14d8`: `12.81s`, `13.16s`,
`13.88s`. `--durations` puts the entire cost in the call (`12.45s call`,
`0.01s setup`).

It is a **liveness deadlock, not a slow path**. Both owners stay `connected` at
`tick=1` for the whole 12 s budget and only flip to `idle` when the deadline
tears them down:

```
[  1.3] a.done=False b.done=False  L={connected, tick:1}  R={connected, tick:1}
[ 11.5] a.done=False b.done=False  L={connected, tick:1}  R={connected, tick:1}
[ 12.5] a.done=True  b.done=True   L={idle, tick:1}       R={idle, tick:1}
```

Thread dump at t=4 s — each owner blocked waiting on the other:

```
timed_wire.py:receive:522          <- left
timed_link_session.py:_wait_for_progress:661
execution_adapter.py:before:349 <- mb.py:_execution_step:596 <- session.py:step:708
timed_wire.py:receive:522          <- right
timed_link_session.py:_wait_for_progress:661
execution_adapter.py:before:349 <- mb.py:_execution_step:596 <- session.py:step:708
```

`timed_wire.receive()` is `while True: if self._queue: return ...;
self._condition.wait(wait)`. Both sides hold an **empty** queue, so neither
wakes. `frames=(1,1)`, expected `(2,2)`.

**Correction to my own first hypothesis.** I initially suspected the test's
cancel/barrier preamble. That was wrong, and the measurement says so: a paired
`step` with *no barrier and no cancel at all* deadlocks identically.

```
barrier=False elapsed=12.37  both_done=False  frames=(1,1) want=(2,2)
barrier=True  elapsed=12.40  both_done=True   frames=(1,1) want=(2,2)
```

A single owner steps fine — `SOLO ok elapsed=0.15s`, `frames 2, want 2`. So the
defect is specific to the **paired** path and needs no special preamble.

## Corroboration from an independent gate run

A concurrent agent's `production_gate.py --runtime-mode source --unit-only
--repeat-timing 5` against the same `eb7f14d8` finished while I was measuring
(PID 1374134, evidence under
`pokemon/target/feature-qualification/253-unit-c2-eb7f14d8/`). I did not start
it and did not touch it. Its result:

```
overall: FAIL
unit    total=8327 passed=8313 failed=14 skipped=0 xfailed=0 xpassed=0 errors=0
timing  total=2125 passed=2090 failed=35 skipped=0 xfailed=0 xpassed=0 errors=0
matrix-audit: PASS collected=8550 structural=PASS acceptance-declaration=PASS
gate-policy: PASS   collection: PASS (3/3)   fixture-manifest: PASS entries=31
assets: 14 MISSING (5 rom, 3 symbol, 6 fixture)
```

Its 14 unit failures are **the same 7 files** as my 12, and its **timing** tier
fails on those same 7 files too:

```
tests/test_mcp_timed_remote_cached_failure.py
tests/test_mcp_timed_remote_cancellation.py
tests/test_mcp_timed_remote_owner.py
tests/test_mcp_timed_stdio.py
tests/test_session_timed_execution.py
tests/test_timed_link_session_control.py
tests/test_timed_link_session_v3.py
```

Every failing signature is the paired form: `submit` to **both** owners under
one shared `pair_deadline`, then `result_until`. That is the same deadlock
reached through six different test modules. Two independently-produced
measurements agreeing on the file set is much stronger than either alone.

## Independent review: unobtainable, new failure mode

I dispatched `/root/indep253_deadlock` with a fully self-contained brief
(absolute paths, five named files with line ranges, one claim, four numbered
questions, required verdict format, explicit "do not ask clarifying questions").

For the first time this run the collaboration tools worked: `spawn_agent`
returned a task name and nickname, `list_agents` returned the full roster, no
`unsupported call`. The agent then replied *"No task was included in your
message — just the shared settings guidance and environment context"* and
listed the five Codex `config.toml` paths. It never opened `timed_wire.py`.

This is a **different and worse** defect than the one already on #489. There,
the call errors and I know no review was requested. Here the call **succeeds**,
a roster entry appears, the agent reaches terminal `completed`, and the only
evidence of failure is the content of the reply. A lead who dispatched and did
not read the reply would record an independent review as obtained. Posted to
#489 with this detail.

So the deadlock finding rests on **my own measurements only**. That is not the
bar the merge contract sets, and I am not treating it as such.

## Why I did not attempt the repair here

The fix is a source change in the paired wait/wakeup path
(`timed_link_session._wait_for_progress` / `timed_wire.receive` symmetry). It
needs its own branch, focused regression tests for the wakeup itself, and an
independent review of that head. Doing it inside a triage pass, with review
dispatch broken, would produce exactly the unreviewed head that #572 already
set a precedent against. Recorded, not attempted.

## Acceptance rows still unmet for #253

1. Non-waived disposition of every unit failure — **partly met**: the
   `/dev/shm` group is dispositioned as environment with a reproducible
   harness; the paired deadlock is identified but **unrepaired**.
2. `overall: PASS` with `failed=0` — **not met**. Authoritative gate says FAIL.
3. Timing tier re-measured on a qualified runner — **not met**. It was measured
   on a host with 4 CPUs and load1 5-11 against the issue's required <=4, with
   another agent's gate run concurrent. Confounded.
4. Independent review of a repairing head — **not met**, #489.

Release status stays **PARTIAL**. Nothing waived, nothing suppressed, no
`xfail`, no skip, no synthetic substitute.

## Housekeeping

- `/home/agent/wt253` at `eb7f14d8`, clean except untracked `*.md` probes and
  `.scratch_probe*.py` scratch files. No source change.
- Protected checkout `/home/agent/poke-harness/pokemon` untouched.
- Evidence published: #253 comments `5979341071` and `5979521106`;
  #489 comment `5979536336`.
