# TCP receive-mailbox investigation — 2026-09-13

[Issue #79](https://github.com/CompleteDotTech/pokemon/issues/79) tracks this
correction; full qualification is tracked separately in issue #72.
The source qualification at `7180cd0d8256455ed4d9db7dc7d5b0ad452e13d3`
passed 18 of 19 strict trade entrypoints. Blue-color listening to Red-color
connecting failed the exact received party-record assertion. Both subprocesses
exited 0 and reached the trade hook. A separate ordinary source replay reproduced
record corruption; passing retries do not erase either failure.

## What the observations establish

A bounded owner-thread diagnostic reproduced an interior missing byte while
retaining the complete expected record in the incoming SB byte stream.
The ROM rearmed external SC inside its serial handler before the main loop
consumed the receive mailbox. The next byte then overwrote that mailbox.

Offsets below are local Blue CPU cycles relative to receipt of byte `0x2d`.
They are not a shared cross-peer clock.

| Event | Cycle offset | Observation |
|---|---:|---|
| Byte `0x2d` completes | 0 | SB is `0x2d`; serial IF is raised |
| External SC rearmed | 404 | Mailbox contains `0x2d` |
| New-data flag set | 436 | Main-loop consumption has not occurred |
| Serial routine returns | 504 | Mailbox still contains `0x2d` |
| Byte `0x16` completes | 912 | New-data flag is already clear; mailbox remains unread |
| Next external SC rearm | 1,316 | Next handler has overwritten mailbox with `0x16` |
| Main-loop mailbox read | 1,632 | Reads `0x16` at the unchanged destination |
| Destination store completes | 2,524 | Stores `0x16`; no intervening store of `0x2d` |

Thus a cleared new-data flag and an armed SC register are insufficient
consumption evidence. Balanced TCP edges also do not establish correct
application data.

This diagnostic changes host scheduling and uses ROM execution hooks.
It is causal evidence for its observed run, not an ordinary acceptance run.
It retained 797 byte completions with zero dropped events or observer errors.
The earlier diagnostic with adjacent instruction hooks stalled in the observer;
that stall is not attributed to the ordinary product.

The separate authored-ROM regression
`test_successive_tcp_bytes_preserve_the_rom_mailbox` fails in both source and
native runtimes before a production fix. It sends two bytes through 16 real
TCP edge requests, using host-thread handoffs to expose the overwrite.
Its ROM uses ordinary CPU/device execution and a serial IRQ which rearms before
main-loop consumption. The observed output is `[0x16, 0x00]`; the required
output is `[0x2d, 0x16]`.

## External implementation references

The user identified Matt Greer's
[GBA link-cable article](https://www.mattgreer.dev/blog/gba-dev-link-cable-networking/).
He is developing his own GBA game and can change its interrupt library,
receive protocol, and transfer interval. These choices operate inside the game.
Our existing Game Boy ROM serial routines cannot acquire those changes merely
by changing the emulator's socket backend.

The linked `afska/gba-link-connection` sources were inspected at
`c61bf351f68ad2d6e1c9d72d70e21bec19adfc0b`:

- [LinkCable.hpp](https://github.com/afska/gba-link-connection/blob/c61bf351f68ad2d6e1c9d72d70e21bec19adfc0b/lib/LinkCable.hpp):
  interrupt reception and application reads use separate queues. Master
  transfers are timer-driven and require hardware readiness. Queueing still
  requires timely application consumption.
- [LinkRawCable.hpp](https://github.com/afska/gba-link-connection/blob/c61bf351f68ad2d6e1c9d72d70e21bec19adfc0b/lib/LinkRawCable.hpp):
  asynchronous completion enters `READY`; `getAsyncData()` returns the stored
  result and restores `IDLE`. Further transfers require `IDLE`.
- [_link_common.hpp](https://github.com/afska/gba-link-connection/blob/c61bf351f68ad2d6e1c9d72d70e21bec19adfc0b/lib/_link_common.hpp):
  a full queue records overflow and discards its oldest entry. This policy
  would not meet our exact-record acceptance requirement.

The article's emulator reference also led to inspection of
[mGBA's Game Boy serial lockstep implementation](https://github.com/mgba-emu/mgba/blob/543a197582c30364584d773a974d7f991892fa43/src/gb/sio/lockstep.c)
at `543a197582c30364584d773a974d7f991892fa43`. Transfer phases coordinate
peer catch-up, cycle credits, and scheduled serial completion, including CPU
speed conversion. This is a closer architectural reference for emulator time
coordination. It is not a TCP implementation or evidence that our harness passes.

## Correction and scope

The transferable requirement is to prevent host socket scheduling from
collapsing the receiving CPU's progress between transfers. The correction
uses a conservative completed-byte response hold in the existing protocol.
Native SB/SC/IF completion remains immediate. The final-bit response stays
pending while a subsequent ordinary owner frame executes, including when the
byte completed at a frame boundary and no ninth request exists.

If the ROM changes clock roles during that continuation, the held response
is sent and its pending accounting settles before the next native master
request is admitted. Stop and failure cancel the held response. Detaching
with one pending closes that unfinished transport. The live and pre-close
snapshots expose whether a byte response remains held.

This policy orders response transmission against the documented owner
progress rule. It is not a shared clock or an observation of arbitrary
application consumption. Frame return, SC rearm, interrupt assertion, and
ROM consumption remain distinct events. A frame delay is not proof of
general hardware timing correctness.

No ROM routines, mailbox memory, wire payloads, or party-record assertions
are changed. Low-level callers retain the previous behavior unless they
explicitly enable byte-response deferral and supply owner-frame progress.

The focused source and native regression slices each passed 257 tests with
zero failures, errors, or skips. Seven cases in the new mailbox module cover
the original overwrite, rearm without continuation, stop, detach, pause,
frame failure, and the transition from external to internal clock. The role
test explicitly verifies that all responses to the prior byte precede the
new master requests.

Ordinary Blue-color-listener / Red-color-connector strict trade replays:

| Runtime | Result | Supervisor elapsed | Peer exits | Balanced edges | Frame turns |
|---|---|---:|---|---:|---:|
| Source | PASS | 135.788 s | 0 / 0 | 6,416 | 2,052 |
| Native | PASS | 20.941 s | 0 / 0 | 6,384 | 4,446 |

Both exact party records swap, both trade hooks execute, and neither endpoint
retains pending work. These scoped passes must be followed by the complete
source/native qualification for issue #72.

## Retained evidence

Artifacts remain under `target/link-issues-20260912/`:
`mailbox-overwrite-79-summary.json`, `serial-order-79-383845-listen.json`,
`source-trade-79-order-probe3-*`, the ordinary `*-trade-79-baseline*` runs,
and the source/native `*-mailbox-79-before2-*` runs.
After-fix evidence includes `*-mailbox-79-after2-*`,
`*-owner-79-after2-*`, and `*-trade-79-after1-*`.

Two validation setup failures remain separately labeled: `*-mailbox-79-after1`
never collected tests because its shell launcher omitted an argument label.
The initial native role-transition test exhausted eight frames after only one
incoming bit, before reaching the transition. Its post-failure diagnostic
confirmed the next request was queued. The corrected test uses an explicit
host-thread handoff; its wire and ROM assertions remain intact.

| Input | SHA-256 |
|---|---|
| Diagnostic implementation | `55e83b757df4963e89bb82a098711292aa7d8b15f99cb2ced6651ade7c727c46` |
| Captured trace | `ce6ee0a95f904eb624c759c123ad902fd0c4c828f4502bb32de07733a0b44ba4` |
| Before-fix authored-ROM regression | `3187392692f2b8f5cdc7df87448d5940f2f46ea4a73421b6268fa6680f9a9d08` |

The native baseline uses the complete 58-extension build documented in the
preceding investigation; this correction changes no vendored declarations.
The external implementation references are source-derived guidance, and the
focused passes above are not complete release qualification.
