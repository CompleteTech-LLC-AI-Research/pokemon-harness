# #253 Cause B measured directly on merged master `1bdc1a5`

Date: 2026-10-03
Tree: `origin/master` `1bdc1a50d4a65b066830c9e44b2ba6495dc1f7c8`
Worktree: a declared detached checkout at `1bdc1a5`, verified source interpreter
Probes: a declared task scratch path (instrumentation only, not fix evidence)
Repo untouched by this pass: no source, test, or config file was modified.

Target row:
`tests/test_mcp_timed_remote_owner.py::test_mcp_tools_and_resource_share_persistent_native_owner`
Fails on current master with `TimedOwnerError: request deadline expired`.

## Correction to the published diagnosis so far

Two mechanisms were published as the cause and are both refuted here:

1. The `_threshold` publish-gate mechanism (refuted in `a260a02`).
2. Permit batching as the remaining repair (refuted in `1bdc1a5`).

This pass measured the actual bound instead of deriving it, and the real
binding term is **neither of those**. It is the
`enforce_completeness` watermark ceiling in `EmulatedTimeCoordinator._reserve`
(`src/pokered_harness/link/emulated_time.py:425-429`):

```python
if self._enforce_completeness:
    # Bound the entire in-flight permit, including all rearm credit.
    # Peer progress alone does not attest to receipt of earlier edges.
    ceiling = min(ceiling, self._watermark + self._max_lateness)
```

## Measured facts

| quantity | value |
|---|---|
| `_max_lateness` (`max_edge_lateness*2`, `POLICY`=32) | 64 half-cycles |
| `required_cpu_cycles` per instruction | 24 (48 halves at rate 2) |
| grants issued for **one frame** | 20693 |
| instructions retired in that frame | 15048 |
| instructions per grant | **0.727** |
| grant rate | 1802 /s |
| wall time, one frame, **both** peers stepping | **11.48 s** |
| owner `request_timeout` | 5.0 s |

A frame is **2.3x over budget on its own**. 20693 round trips at 1802/s is
~11.5 s, which is the entire cost.

## The arithmetic, checked against the measurement

`ceiling - local <= _max_lateness = 64` half-cycles, and one instruction
consumes 24 half-cycles, so a single grant can carry at most **2**
instructions. The observed 0.727 is *worse* than that ceiling because a
grant is sized for the worst case (`required_cpu_cycles`, deliberately, per
the vendored PyBoy comment at `mb_coord_002_596600ba5832.pxi:86`:
"Excess credit is not permission to batch; HALT also uses 4") and because
most instructions in a frame are short of the reservation.

This also corrects the review in `1bdc1a5`, which derived ~8 or ~32
instructions per round trip. Those figures came from `cpu.tick(4)` => 8
half-cycles per instruction. That is not the number the governor charges:
`required_cpu_cycles` is 24, so the governor charges 24 half-cycles per
instruction at rate 1. The measured 0.727 instructions/grant is the
authority; the derived 8 and 32 are both wrong for this path.

## Progress is genuinely lockstep, and that is structural

`probe7` logs every `record_peer_progress` / `advance_watermark` / `reserve`.
Credit only advances when the **peer** publishes:

```
PEERPROG  seq=2 32      AW  seq=0 through=32   RESERVE got local=32
AW        seq=0 through=56              PEERPROG seq=3 56   RESERVE got local=56
```

The owner's own watermark advances only from a peer's `EmissionComplete`.
`probe10` confirms an idle peer never publishes at all: across a
single-sided `step(1)` there is exactly **one** publish that advances
`sent_progress`, and it is on the owner's own thread. So a single-sided
execution cannot make progress at all, independent of speed.

## Consequence for the failing test

`tests/test_mcp_timed_remote_owner.py:301` runs

```python
await call("press", {"button": "a", "duration": 2})   # left only
```

before the lockstep `asyncio.gather(...)` on the next line. Per the
measurement above that press is a single-sided execution, so it depends on
credit the idle peer never issues. `probe11` shows the press itself returns
in 0.003 s (it is not the failing call); the failing call is the later
lockstep step, which needs 11.48 s against a 5.0 s deadline.

Note `probe6` initially appeared to show lockstep succeeding in 5.0 s. That
was an artifact of `asyncio.gather(..., return_exceptions=True)`, which
swallowed a `TimedOwnerError` and let the timer be read as success. With the
exception unswallowed (`probe12`), the lockstep step also fails. Only
`probe13`, which widened `request_timeout` purely to measure, produced a
truthful 11.48 s.

## What this rules out

- Not `_threshold`: `_wait_for_progress` already calls `_publish(force=True)`.
- Not permit batching: the binding term is the watermark ceiling, not permit size.
- Not wall-clock/scheduling latency: 11.48 s is 20693 round trips of real
  protocol work, not host contention. Reducing per-round-trip cost cannot
  help when the number of round trips is set by a 64-half-cycle window over
  24-half-cycle instructions.

## Disposition

No bound moved, no deadline relaxed, no skip or xfail added, no policy
changed to fit the test. The measured cause is a hard credit ratio:
**~2 instructions per round trip, ~15048 instructions per frame, ~20693 round
trips, ~11.5 s per frame against a 5 s deadline.**

Any repair that does not increase instructions-per-round-trip must either
(a) relax `max_edge_lateness`/`_max_lateness` — a latency bound, which is
out of bounds for this run, or (b) make one round trip carry more than one
instruction's accounting — which is exactly the batching candidate already
refuted for independent protocol reasons in `1bdc1a5`.

Release status stays **PARTIAL**. #253 stays open.

## Addendum: bare-wire control measured on this host

The review in `1bdc1a5` asserted a ~51 us cross-thread Condition round trip and
used it to argue the governor dominates per round trip. That control was not
reproducible. Measured directly with a real `TimedWireChannel` (revision 3,
real `handshake()`, 3000 `Progress` ping-pongs, Unix socketpair):

```
bare wire round trips n=3000
  median=187us  mean=577us  p10=120us  p90=980us
```

Against the in-path measurement (one frame, 11.48 s, ~20693 per-peer round
trips = 555 us/round trip):

| term | us |
|---|---|
| bare wire, median | 187 |
| bare wire, mean | 577 |
| in-path governor, wall/round-trip | 555 |

So the in-path cost is **1.0x the bare mean** and **3.0x the bare median**.
Bare wire alone accounts for essentially the whole frame. The frame is
round-trip-bound.

This **confirms the review's structural conclusion** — the frame costs 20k
round trips and making each round trip cheaper cannot fix it, because the
round-trip count is set by the credit ratio, not by per-round-trip cost —
while **correcting its numbers**. The floor on this host is ~187 us median /
~577 us mean, not ~51 us. The review's `LAT=128` batch-size figure (32
instructions/round trip) is also not this path's value; the measured
instructions-per-grant is 0.727.

Also measured: `snapshot()` is 166105 calls per frame, 8.82 us each,
1.465 s total (13% of summed peer CPU). The `1bdc1a5` review proposed eliding
some of those as an optimisation. That remains a true optimisation but is
arithmetically incapable of closing an 11.48 s -> 5.0 s gap.

## Why #253 cannot be closed on this host

For the target row to pass, one frame must complete inside the owner
`request_timeout` of 5.0 s. Measured cost is 11.48 s. The gap is 2.3x and is
produced by:

```
15048 instructions / frame
x 24 half-cycles per instruction (required_cpu_cycles)
= 361152 half-cycles demanded
/ 64 half-cycles per round trip (_max_lateness = max_edge_lateness*2)
= 5643 minimum round trips
observed 20693 (2 instructions/grant -> actually under 1)
x ~187-577 us real wire latency
= 11.48 s measured
```

The two admissible levers are both closed:

- raise `_max_lateness` (i.e. relax `max_edge_lateness`) — a **latency
  bound**, explicitly out of bounds for this run;
- raise instructions per round trip — the **batching** candidate, already
  refuted on independent protocol grounds in `1bdc1a5` (mid-batch
  `EdgeResponse` hits `_stage` with `_waiting_edge is None` and raises
  `ProtocolError`; `commit()` has no intermediate hook; and the 82% waste is a
  deliberate worst-case reservation, not a free win).

Third option, not a latency bound and not batching: **reduce the number of
instructions the frame has to retire**. That is emulator work, not a
governor or wire change, and is out of scope for a timing-bound repair.

Release status stays **PARTIAL**. #253 remains open with a measured,
non-speculative cause.
