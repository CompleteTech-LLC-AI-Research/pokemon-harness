# Independent review round 2: permit batching REFUTED (1.00x headroom), and #164 does not apply

Date: 2026-10-03
Tree: `master` `eb50e8bb`
Reviewer: independent agent via `codex1 -p cheaperinference` (not the author)
Brief: `.scratch/rev253b.md`; full review `.scratch/r253_indep2.md` (27 KB);
probes `.scratch/r253indep2/probe{3,4,5,6}.py`
Repo untouched by the reviewer; verified `git status` shows no tracked changes.

## Verdict: NOT LEGITIMATE for permit batching

The repair candidate I ranked first in round 1 -- permit batching -- **does not
work**, and the reason is stronger than a #164 conflict: on this path the
batching headroom is **1.00x**. Measured, not estimated.

```
grants=20000  stalls(=round trips)=624  grants per round trip = 32.05
avg permit size = 22.12 cpu cycles   avg used = 4.00 cpu cycles
waste per grant = 18.12 cycles  (81.9 % of reservation)
THEORETICAL MAX grants per round trip = 32.0
observed grants per round trip        = 32.05
headroom a batching repair could exploit = 1.00x
```

The credit already arrives in batches of 32 instructions per round trip. The
stall is not caused by permits being too small, so there is nothing to batch.

## The arithmetic, which I re-derived from source

```
TimedLinkSession line 86 : self._lateness = max_edge_lateness * 2
line 259                 : max_edge_lateness=self._lateness // 2   -> == max_edge_lateness
emulated_time.py:186     : self._max_lateness = max_edge_lateness * 2   (in half-cycles)
emulated_time.py:428     : ceiling = min(ceiling, _watermark + _max_lateness)
vendored PyBoy           : cpu.tick(4)  ->  4 raw cycles -> 8 half-cycles per instruction
=> grants per round trip = _max_lateness / 8
```

**Correction to the reviewer's own numbers.** The reviewer ran `probe6.py` with
`LAT = 128` and reported 32.05 grants/round-trip. The *failing tests* do not use
128: `tests/test_session_timed_execution.py:23` configures
`max_edge_lateness: 32`, which gives `_max_lateness = 64` halves and therefore
**8** grants per round trip, not 32. So the measured "headroom 1.00x" figure was
taken at a lateness the failing path does not use.

The *conclusion* survives, because the reviewer's ratio argument is structural:
grants per round trip equals the theoretical ceiling by construction, at any
lateness. At the tests' real value of 32 the headroom is likewise 1.00x, and the
frame cost per round trip is correspondingly ~4x worse than the reviewer's
numbers imply. The review overstated the round-trip batch size by 4x, which if
anything strengthens the conclusion that round-trip rate -- not permit size --
is the problem.

## #164 is inapplicable, and cannot block or bless this

#164 is `fix/network-edge-pending-retirement-race`, merged as `2c81f10c`: wait
for edge-response retirement before sampling pending edge work. That machinery
lives in `NetworkBackend` and its owner mixin
(`src/pokered_harness/link/_network_backend_owner_mixin.py`), with the
guarantee stated in `tests/_network_edge_retirement_support.py:426-482` and
backed by `tests/test_network_edge_retirement_{wait,reciprocal}.py`.

**`TimedLinkSession` shares no code with it.** Grepping the timed path for
`_edge_pending`, `NetworkBackend`, `_decrement_edge_pending`, and
`_wait_for_edge_requests_retired` returns zero hits. The timed path has its own
mechanism: `on_edge()` (`timed_link_session.py:799-839`) sends an `EdgeRequest`
and blocks the emulator thread in `channel.receive` until the peer's
`EdgeResponse` arrives, returning the bit synchronously; `_deliver()`
(556-597) is the responder and writes the response on the owner thread. There is
no deferred write and no background response worker, so the race #164 fixed does
not exist on this path. Retirement is synchronous.

So #164 neither blocks a change here nor validates one. Any repair must be judged
on its own merits.

## What would actually break a batching repair

The reviewer's three attacks, all of which I consider sound:

1. **Response-arrival race.** A peer `EdgeResponse` arriving while the owner is
   inside a multi-instruction batch hits `_stage`
   (`timed_link_session.py:382-386`) with `_waiting_edge is None` and raises
   `ProtocolError("unexpected or duplicate edge response")`. Admitting the
   message mid-batch would apply an edge inside an unreported interval, which is
   exactly what `execution_adapter.py:315-316` exists to forbid.
2. **Intermediate credit accounting.** `commit()` advances `_local` and
   decrements `_instructions` in one shot (`emulated_time.py:488-489`), while
   `_reserve`'s edge and held-deadline checks (397-424) are written against
   post-commit `_local`. A batch needs correct accounting at *intermediate*
   instruction boundaries, and there is no intermediate-commit hook to hang them
   on without changing `commit()`.
3. **The 82% waste is not a safe free win.** Shrinking `required_cpu_cycles` from
   24 toward the ~4 actually used is unsafe. The vendored PyBoy comment says
   why at `mb_coord_002_596600ba5832.pxi:86`: *"Excess credit is not permission
   to batch; HALT also uses 4."* The worst-case obligation is not bounded by the
   common case; under-reserving turns a HALT or DMA step into a physical permit
   overrun (`emulated_time.py:502`), which is terminal.

Point 3 also explains why the 82% waste is deliberate rather than sloppy: a
granted bound is a worst-case guarantee, not a scheduling quantum.

## The one real finding: `snapshot()` elision

`snapshot()` is 9.116 us and accounts for **62.8%** of governor work per permit.
It builds a 17-field frozen dataclass (`emulated_time.py:118-136`) fresh under
the Condition lock, and the real path calls it ~6+ times per permit:

- `execution_adapter.py:298` -- `before()` first snapshot
- `execution_adapter.py:319` -- re-read inside the wait loop
- `execution_adapter.py:253` -- `_record()`'s `finally`, whose *only* purpose is
  checking `pending_permit`; both `commit()` and `discard_unconsumed_permit()`
  already clear `_pending` under the lock and `notify_all()`
  (`emulated_time.py:498`, `548`)
- `emulated_time.py:525` -- `commit()` returns `self.snapshot()`; the adapter
  **ignores the return value** (`execution_adapter.py:246-251`)
- plus `_safe_pump`'s snapshots at `timed_link_session.py:622, 629, 649, 652`,
  on every `before()` via `_safe_pump()` at `_timed_link_session_support.py:62`

Two changes remove at least 2 of ~6 snapshots per permit with **no bound moved,
no invariant changed, no protocol change**: stop building a `TimeSnapshot` in
`commit()`, and drop the `execution_adapter.py:253` nil-check.

**But it will not fix #253, and the reviewer says so plainly.** Governor work is
~11-14.5 us per instruction; the measured cross-thread floor on this host is
~51 us. Making the slow path cheaper does not make a 22-second frame fast.

## Correction to round 1's inference

Round 1 concluded "this is governor work per permit, not host scheduling
latency", partly from a bare-thread handoff control at 0.02 ms. Round 2 could
**not reproduce that 0.02 ms** and measured a bare Condition round trip at
~51 us on this host -- roughly 4.5x a governor call. So that inference is not
supported here.

The reviewer's replacement conclusion is stronger and does not depend on the
split at all:

> The frame costs 20k round trips because one round trip yields 32 (or, at the
> tests' real lateness, 8) instructions. Reducing the *cost* of a round trip
> cannot help, because that batch size is already the maximum the bounds allow.
> The only lever is the bound itself -- and widening it is a latency bound,
> which is rejected.

Caveat recorded by the reviewer and accepted: arm B and arm C showed 14-169 us
run-to-run variance on this contended host, so the absolute microsecond figures
are min-of-N and directionally reliable only. The 32.05-vs-32.0 agreement is a
ratio taken under identical conditions and is far more robust.

## Disposition

- Round 1's threshold mechanism: **refuted** (see
  `REVIEW_253_LOCKSTEP_INDEPENDENT_b0f09461.md`).
- Round 1's batching candidate: **refuted** here, on measurement.
- Remaining candidate: `snapshot()` elision, worth ~20-30% of governor work.
  **Not proposed for merge** -- it makes a slow path faster and does not make
  #253 pass, so it is an optimisation, not a fix for this issue.
- The actual gate on #253 remains the read-only `/dev/shm` mount for 79 of the
  86 failures. Of the remaining 9, the bound that would need to change is a
  latency bound, which this run must not relax.

No deadline, latency bound, capacity window, CPU requirement, skip, xfail, or
gate was relaxed. Release status stays **PARTIAL**.
