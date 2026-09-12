# TCP serial timing investigation — 2026-09-12

Issue [#71](https://github.com/CompleteDotTech/pokemon/issues/71) is resolved
for its documented Blue/Blue failure by the preceding fixes, particularly
the owner continuation in [#76](https://github.com/CompleteDotTech/pokemon/pull/76).
Current evidence does not demonstrate a remaining defect requiring a new
TCP scheduler. It also does not establish a shared emulated clock or rule
out every possible mailbox overrun. Full source/Cython matrix qualification
remains [#72](https://github.com/CompleteDotTech/pokemon/issues/72).

## Inputs and ordering evidence

The investigated code is `0121457485f71b58da1912b18b250af671af42fb`,
integrated at `b378c85977238c5e965e521d79ddfbd0501f84b0`. Both runtimes use
CPython 3.11.2. The complete native build contains 58 extensions from
staged-input SHA-256
`680a5ad13aec5aa9bde9722a07342f60e75d6df8789f59eb823816ff63457ad4`.
The source runtime loads the bundled Python modules.

The Blue-color ROM SHA-1 is `5f4b05725a860e04077045462176d3e2771c5022`;
its symbol SHA-1 is `c779a0628cfc97cc9ac9db2520a1e23a2d8b7ed6`.
Direct inspection of those pinned bytes verifies external SC rearm at
`$213C`, the received-data flag write at `$2164`, and the exchange loop's
destination store at `$2192`. Thus serial rearm precedes notification and
main-loop consumption. The upstream [serial routine](https://github.com/pret/pokered/blob/master/home/serial.asm)
explains that sequence; it is not a substitute for the pinned input checks.

Hardware byte completion clears SC's transfer bit and requests the serial
interrupt. It does not mean the interrupt handler or application has run.
External pulses also need not have regular intervals. These distinctions
follow the [serial hardware reference](https://github.com/gbdev/pandocs/blob/master/src/Serial_Data_Transfer_%28Link_Cable%29.md).

## Observed failure and correction

After the frame-boundary and wakeup fixes, an ordinary source Blue/Blue
trade still lost one byte from the connector's party record. Both children
exited 0 and completed the trade hook; 6,408 edges and 2,062 frame turns
balanced. Completion counters alone did not detect the corruption.

The authored-ROM regression
`test_byte_completed_at_frame_barrier_resumes_cpu_without_a_ninth_edge`
then demonstrated a specific missing continuation in both runtimes. After
eight genuine TCP edges, the byte and interrupt flag were committed, but
the halted CPU had not run its interrupt handler. The correction executes
one ordinary owner frame after that completion. It preserves normal device
and hook execution and does not recursively step the CPU in the IRQ callback.
The regression fails before the change and passes afterward, including an
explicit check for one additional emulator frame.

Ordinary, uninstrumented strict replay results after the correction:

| Runtime | Pytest result/time | Child exits | Balanced edges | Completed frame turns |
|---|---|---|---|---|
| Source | PASS, 131.185 s | 0 / 0 | 6,416 | 2,057 |
| Cython | PASS, 21.768 s | 0 / 0 | 6,408 | 4,157 |

Both runs pass the existing exact party-record assertions and finish with
zero pending edges, owner deferrals, and owner errors. Each runtime also
passes the 231-test ownership/network/cancellation regression slice.

## Bounded observation of the corrected code

A temporary diagnostic delegated each external-edge application unchanged
and captured values only at byte boundaries on the emulator owner thread.
It performed no ROM writes or emulator steps. Its arrays were capped at
2,048 completions per process. Captured fields included monotonic time, PC,
CPU and motherboard physical clocks, CGB speed, frame count, SC/SB/remaining
bits, IF, map/link phase, and symbol-resolved ROM mailbox fields. Physical
clock reads use the motherboard's supported lazy synchronization getter.

| Runtime | Completed bytes / dropped | Pytest result/time | Minimum/median local physical interval |
|---|---|---|---|
| Source | 794 / 0 | PASS, 133.589 s | 896 / 140,448 |
| Cython | 793 / 0 | PASS, 21.788 s | 140,428 / 140,452 |

Intervals use the motherboard's half-normal-T units. Both samples stayed
in CGB double-speed mode and one physical-clock epoch. These are local
intervals, not synchronized cross-peer timestamps or performance benchmarks.
Each byte latch left CPU cycles unchanged and set the serial IF bit.
The complete expected 44-byte party record occurred in each received stream.
Its 44 pre-completion mailbox flags were zero, and final party assertions
passed. A cleared flag by itself is insufficient to prove consumption;
the raw stream and final record checks provide the additional evidence here.

The diagnostic itself can affect host scheduling: a preceding instrumented
source run passed while ordinary execution still failed. Consequently its
passing result is supplemental evidence, not the acceptance result. The
ordinary replays in the preceding table provide that evidence.

## Decision and limits

The documented failure no longer reproduces after the verified continuation
fix. The broader scheduling hypothesis remains a design possibility, rather
than an observed unresolved cause in this investigation. No additional
scheduler change is justified by these samples. The existing frame recovery
does not order response transmission against ROM consumption or guarantee
equal emulated time; its accounting is described in the
[serial design](pyboy_serial_overhaul_design.md#coordinator-semantics).

Retained local artifacts under `target/link-issues-20260912/` include the
ordinary `source-trade-70` / `native-trade-70` logs, XML, terminal result JSON
and parsed peer results; the corresponding `*-trade-71-probe` files;
`serial-probe-71-337572-connect.json` and
`serial-probe-71-337567-connect.json`; `rom-serial-order-71.json`; and
`timing-diagnosis-71-summary.json`. The temporary probe's SHA-256 is
`aee618e6f34faebad994296cb0f5b819ab80e9d57f77487d23709152c11e7feb`.

To repeat ordinary acceptance, select one verified interpreter at a time,
set `POKERED_PYTHON` to that interpreter, provide the pinned ROM/fixture
roots, and run:

```sh
python -m pytest -q -s \
  'tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_completes_trade_over_tcp[blue_color-listen-blue_color-connect]'
```

Source selection must include the bundled `vendor/pyboy-src` path; native
selection must load the installed extensions without that path shadowing them.
Any new strict-matrix failure needs its own terminal results and time series
before attributing it to mailbox consumption or shared-time coordination.
