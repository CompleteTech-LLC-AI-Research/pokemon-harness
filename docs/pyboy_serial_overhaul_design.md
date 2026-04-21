# PyBoy serial + link-cable overhaul — design proposal

**Target project:** [Baekalfen/PyBoy](https://github.com/Baekalfen/PyBoy)
**Status:** draft, for upstream discussion issue
**Audience:** PyBoy maintainers, link-cable-interested contributors

## Why this document exists

PyBoy currently does not implement the Game Boy serial/link model needed
for authentic Gen I Pokémon trading or link battles. Historical attempts
([PR #232](https://github.com/Baekalfen/PyBoy/pull/232),
[PR #344](https://github.com/Baekalfen/PyBoy/pull/344)) reached partial
functionality but surfaced the same problems repeatedly — socket
freezes, internal-clock-only operation, mid-byte synchronization issues
— because they extended the existing simplified serial core incrementally
rather than replacing it.

This proposal describes a three-layer rewrite that treats **Game Boy
serial as a synchronous shift register** (per Pan Docs), layers a
**backend abstraction** over it (per SameBoy), and adds a **lockstep
coordinator** for multi-instance emulation (per mGBA). The goal is to
unblock Pokémon Red/Blue/Yellow trading and link battles as the first
customer, with the architecture also supporting later Gen II time-capsule,
stadium-style transfers, and any other Game Boy link protocol.

## The specific gap in mainline PyBoy

From `pyboy/core/serial.py` as of the audit:

| Current behavior | Problem | Impact |
|---|---|---|
| `set_SB()` forces `SB = 0xFF`, discards outgoing byte | Outgoing bytes never leave the instance | Pokémon's real payloads can't be exchanged |
| `set_SC()` schedules 8 × 8192Hz countdown, no peer involvement | Master always "completes" on its own | External-clock code (slave mode) never progresses |
| `tick()` completion does `self.SC &= 0x80` | Pan Docs says bit 7 auto-clears; this keeps it set | Game polling loops see wrong SC state |
| No backend, no peer, no disconnect model | "Cable unplugged" ≠ "pulled-up to 0xFF" | Can't distinguish disconnected master from active slave |
| CGB high-speed serial (`SC` bit 1) is a TODO | Incomplete hardware coverage | Non-blocker for Gen I, blocker for Gen II |

And from `pyboy/core/mb.py`: serial is wired as a single-instance device
polled from the tick path. There is no public API for linking two PyBoy
instances, even though [the main docs](https://docs.pyboy.dk/) confirm
multiple instances can be instantiated.

## Design overview

```
┌──────────────────────┐         ┌──────────────────────┐
│   PyBoy instance A   │         │   PyBoy instance B   │
│  ┌────────────────┐  │         │  ┌────────────────┐  │
│  │  Serial Core   │  │         │  │  Serial Core   │  │
│  │ (bit-accurate) │  │         │  │ (bit-accurate) │  │
│  └────────┬───────┘  │         │  └────────┬───────┘  │
│           │ bits     │         │           │ bits     │
│  ┌────────▼───────┐  │         │  ┌────────▼───────┐  │
│  │ SerialBackend  │◄─┼────┬────┼─►│ SerialBackend  │  │
│  └────────────────┘  │    │    │  └────────────────┘  │
└──────────────────────┘    │    └──────────────────────┘
                            │
                  ┌─────────▼──────────┐
                  │ LockstepCoordinator│
                  │   (edge-driven)    │
                  └────────────────────┘
```

**Serial Core** — a bit-accurate shift register. Owns `SB`, `SC`,
8-edge progress state, clock mode (internal/external + CGB fast),
interrupt scheduling. Replaces the current `Serial` device.

**SerialBackend** — abstract peer-side. Three concrete implementations:
`NullBackend` (disconnected cable, master input pulled to `1` so RX byte
tends toward `0xFF`), `LocalBackend` (two in-process PyBoy cores bridged
bit-at-a-time), `NetworkBackend` (TCP-based peer between processes).

**LockstepCoordinator** — advances two (or more) attached PyBoy
instances to the next serial-edge sync boundary, not at frame
granularity. Edge-driven like mGBA's `GBSIOLockstepNode`.

### Why three layers

The core/backend split is SameBoy's proven design: libretro's 2-Player
subsystem bridges two Game Boys by writing one transferred bit at a
time between them via `GB_set_serial_transfer_bit_start_callback`,
`GB_set_serial_transfer_bit_end_callback`, `GB_serial_get_data_bit`,
and `GB_serial_set_data_bit`. That granularity is what Pokémon's code
actually expects.

The lockstep coordinator layer is mGBA's proven design: `GBSIO` stores
a driver pointer, per-transfer period, remaining bits, and pending `SB`
state; `GBSIOWriteSC` schedules timing events; the Qt frontend attaches
`GBSIOLockstepNode`s to a coordinator and sets them as SIO drivers.
This is what lets two emulators stay deterministically aligned without
one blocking on the other's socket.

Separating backend from coordinator means a network backend (TCP, WAN,
etc.) can slot in without touching the serial core, and conversely a
new coordinator strategy (e.g. "schedule both cores on the same thread
and advance together") can experiment without redesigning the wire
protocol.

## Public Python API

The smallest surface that covers Gen I Pokémon needs:

```python
from pyboy import PyBoy
from pyboy.link import PyBoyLinkSession

# Local two-instance link (same Python process):
a = PyBoy("red.gb", window="null")
b = PyBoy("blue.gb", window="null")
link = PyBoyLinkSession.local()
link.attach(a)
link.attach(b)
while not done:
    link.step()             # advances both to next sync boundary
link.detach_all()

# Network link (two processes, one side listens, one connects):
link = PyBoyLinkSession.listen(port=9876)     # blocks until peer
link.attach(my_pyboy)
# ... or on the other side:
link = PyBoyLinkSession.connect("127.0.0.1", 9876)
link.attach(my_pyboy)
```

Error-handling semantics:

- `PyBoy` without a `link` attached behaves exactly as today: a
  `NullBackend` is implicitly attached. Master-mode transfers complete
  reading `0xFF`, slave-mode transfers time out. No breaking change.
- `link.attach(pyboy)` swaps the backend from `Null` to the
  session's. `link.detach(pyboy)` restores `Null`.
- `link.step()` is non-blocking on the emulation thread: if one side
  is genuinely stuck (slave waiting for clock that will never come,
  master waiting for a peer that hasn't attached), the call returns
  and leaves both instances in a consistent state the caller can poll.

## Behavioral contract per Pan Docs

### Master mode (`SC` bit 0 = 1)

1. Game writes outgoing byte to `SB`, then writes `SC = 0x81`
   (or `0x83` on CGB fast).
2. Over 8 internal-clock edges (8192 Hz or CGB-fast), shift out one
   bit per edge and shift in the corresponding peer bit.
3. On edge 8: clear `SC` bit 7, leave bits 0-1 unchanged, request
   serial interrupt (IF bit 3).
4. If the backend is `Null` or the peer hasn't responded, each shifted-in
   bit is `1` (pulled-up line). Final `SB` = `0xFF`.

### Slave mode (`SC` bit 0 = 0)

1. Game writes outgoing byte to `SB`, then writes `SC = 0x80`.
2. No internal clock; wait indefinitely for peer-supplied edges.
3. On each peer edge, shift one bit out and one bit in.
4. If peer never clocks, transfer never completes. Game must use
   software timeout (Pokémon does this — see
   `pret/pokered/home/serial.asm`'s timeout counters).

### Disconnect

- Disconnected master: RX line pulled high; received byte = `0xFF`.
- Mid-byte disconnect: 1-bit rise time (~20µs on measured hardware)
  may smear bit values; first-order emulation can simply flip
  un-received bits to `1` and complete the transfer.
- Slave with no clock: transfer never completes; software timeout.

### CGB fast (optional for Gen I Pokémon)

- `SC` bit 1 selects the fast internal clock rate.
- In CGB double-speed mode: 524288 Hz fast, 16384 Hz slow.
- In normal speed: 262144 Hz fast, 8192 Hz slow.
- Required for hardware completeness; can ship the first milestone
  without it and add before v1.

## Coordinator semantics

The coordinator's job is to make two PyBoy instances advance together
without one reading stale state from the other. Edge-driven:

```
step():
    # Find the earliest future serial-edge event across all attached instances.
    target_cycles = min(inst.cycles_until_next_serial_edge() for inst in attached)
    if target_cycles is None:
        # No pending edge on either side — advance one frame on each.
        for inst in attached: inst.tick(frames=1)
        return
    # Advance each instance to that edge.
    for inst in attached:
        inst.tick(cycles=target_cycles)
    # Now exchange the pending bit between the two SerialBackends.
    coordinator.exchange_bits(attached)
```

This guarantees: at every backend `exchange_bits` call, both instances
are at the same cycle-count relative to their last common sync point;
neither has seen bits the other hasn't produced yet.

**Non-blocking invariant:** the emulation thread never blocks on a
socket read. For the network backend, a background I/O thread fills
an ordered queue of peer bits; the emulator-side coordinator consumes
ready items or returns from `step()` at a deterministic wait boundary.
This was a specific failure mode in [PR #344](https://github.com/Baekalfen/PyBoy/pull/344)
("Cython freezes after socket connection"), and the non-blocking
discipline prevents it.

## Migration path

The overhaul can land incrementally without breaking existing PyBoy
users:

1. **Keep the current Serial device API intact.** Rename it
   `LegacySerial` internally; keep the same attribute paths on the
   `Motherboard` object. No external observable change.
2. **Add the new Serial Core as a parallel implementation.** Gated
   behind a `PyBoyLinkSession`: instances not attached to a session
   use `LegacySerial`; attached instances use the new core. Zero-cost
   for users who don't use link play.
3. **Over successive releases**, migrate all instances to the new
   core with `NullBackend` as the default. `LegacySerial` becomes
   unused; remove in a later major version.

This sidesteps save-state compatibility concerns during the transition
(the on-disk serial state block format changes once, at the cutover).

## Test plan

Three tiers, matching the research-report recommendation:

### Tier 1 — Register-level (ROM-free)

Small unit tests that drive `SB`/`SC` directly and assert exact hardware
semantics. No ROM required; synthesize instruction sequences via a
minimal test harness.

- Master writes `SB=0xAA`, `SC=0x81`; after 8 × 1024 cycles (8192 Hz
  at 8MHz), assert `SC & 0x80 == 0`, `IF & 0x08 != 0`, received
  `SB == 0xFF` (null backend).
- Two local-backed cores: master writes `0xAA`, slave writes `0x55`;
  after edge cycle, assert master `SB == 0x55`, slave `SB == 0xAA`.
- Slave with no clock: `SC=0x80`, advance many cycles, assert `SC`
  bit 7 remains set and no IF bit 3.
- CGB fast mode (`SC=0x83`): edges complete over shorter cycle count.
- Disconnect mid-transfer: start master transfer, detach backend
  after 3 edges; complete with remaining bits = `1`.

### Tier 2 — Protocol-level (Pokémon constants, synthetic)

Drive the serial core through Pokémon's exact byte patterns without
actually running a Pokémon ROM. Assert the transport correctly relays:

- 7 × `0xFD` preamble bytes (from `SERIAL_PREAMBLE_BYTE` in
  `pret/pokered/constants/serial_constants.asm`).
- Random-number block: 10 bytes, all `< 0xFD`.
- Player data block with `0xFE` filler bytes (from
  `SERIAL_NO_DATA_BYTE`) and patch-list terminator `0xFF`.
- Nibble exchange: cancel=`0x1`, confirm=`0x2`, selection-cancel=`0xF`.
- Battle action nibble domain: `0x0–0x3` move slots, `0x4–0x9` switch
  slots (encoded as `wWhichPokemon + 4`), `0xD` no-action, `0xE`
  struggle, `0xF` run.

### Tier 3 — Game-level (real ROMs, gated by BYO-ROM)

Two linked PyBoy instances driven through real Pokémon flows. Reuses
the [pokered-harness](https://github.com/timothywaynegregg/pokemon)
fixture + symbol machinery developed alongside this proposal:

- Red↔Red trade: load `cable_club.state` on both, drive to
  `TradeCenter_DrawPartyLists`, select a mon, confirm, assert party
  swap via symbol addresses.
- Blue↔Blue trade: same.
- Yellow↔Yellow trade: same, plus verify post-trade Pikachu happiness
  update (Yellow-specific).
- Cross-version Red↔Blue↔Yellow trade: proves `SERIAL_PREAMBLE_BYTE`
  protocol compatibility across versions.
- Blue↔Blue link battle: drive to battle start, submit a move via
  `LinkBattleExchangeData`'s nibble encoding, assert both sides resolve
  the same turn.

### Known Gen I link-battle bugs to preserve (not fix)

From `pret/pokered/engine/battle/core.asm`:

- Metronome/Mirror Move multi-turn interaction can desync the two
  Game Boys — this is a documented pokered bug, and the emulator
  must reproduce it.
- Accumulated-damage clearing desyncs unless damage happens to be
  `0 mod 256` — also a documented pokered bug.

Tests should assert the emulator reproduces these bugs under the
specific trigger conditions, as negative-regression guards against
accidentally "fixing" them via emulation imprecision.

## Reference implementations in-tree

### SameBoy — public bit-level callbacks

```c
// Core/gb.h
void GB_set_serial_transfer_bit_start_callback(GB_gameboy_t *gb,
    GB_serial_transfer_bit_start_callback_t callback);
void GB_set_serial_transfer_bit_end_callback(GB_gameboy_t *gb,
    GB_serial_transfer_bit_end_callback_t callback);
bool GB_serial_get_data_bit(GB_gameboy_t *gb);
void GB_serial_set_data_bit(GB_gameboy_t *gb, bool data);
```

`libretro/libretro.c`'s 2-Player subsystem wires two instances by
capturing the outgoing bit from Game Boy A, reading the current data
bit from Game Boy B, writing the captured bit back into B for the
next edge, and vice versa.

### mGBA — driver abstraction + lockstep coordinator

```c
// include/mgba/internal/gb/sio.h
struct GBSIO {
    struct mTimingEvent event;
    struct GBSIODriver* driver;
    int period;
    int remainingBits;
    uint8_t pendingSB;
    // ...
};
void GBSIOSetDriver(struct GBSIO* sio, struct GBSIODriver* driver);
void GBSIOWriteSC(struct GBSIO* sio, uint8_t value);
```

Qt `MultiplayerController` instantiates `GBSIOLockstepNode`s and
attaches them to a coordinator via `GBSIOSetDriver`.

### pret/pokered and pret/pokeyellow

Byte-exact protocol constants live in `constants/serial_constants.asm`.
Symbol files (`symbols/pokered.sym`, `symbols/pokeyellow.sym`) give
stable bank:addr locations for every routine the test plan hooks.
Routines:

- `Serial` at `00:2125` (Red), `00:1f79` (Yellow) — ISR/handler
- `Serial_ExchangeBytes` at `00:216f` (Red), `00:1fcb` (Yellow)
- `Serial_ExchangeByte` at `00:219a` (Red), `00:1ff6` (Yellow)
- `Serial_ExchangeLinkMenuSelection` at `00:2247` (Red), `00:20a3` (Yellow)
- `Serial_SyncAndExchangeNybble` at `00:227f` (Red), `00:20db` (Yellow)
- `CableClub_DoBattleOrTrade` at `01:5317` (Red), `01:53a5` (Yellow)
- `TradeCenter_SelectMon` at `01:5530` (Red), `01:55ca` (Yellow)
- `TradeCenter_Trade` at `01:5849` (Red), `01:58ef` (Yellow)
- `LinkBattleExchangeData` at `0f:5605` (Red), `0f:5777` (Yellow)
- `_AddEnemyMonToPlayerParty` at `03:749d` (Red), `03:7323` (Yellow)

Yellow-specific divergence: `Serial` handler checks
`wPrinterConnectionOpen` and diverts to `PrinterSerial` (printer
coexistence), and the trade path includes post-trade Pikachu happiness
adjustment. Protocol-compatible otherwise.

## Implementation priority

| # | Task | Effort | Blockers |
|---|---|---|---|
| 1 | Bit-accurate serial core + unit tests | High | — |
| 2 | `NullBackend` + `LocalBackend` | Medium | 1 |
| 3 | `LockstepCoordinator` + attach/detach API | High | 1, 2 |
| 4 | `PyBoyLinkSession.local()` public API | Low | 1–3 |
| 5 | Register-level test suite | Medium | 1 |
| 6 | Protocol-level test suite (Pokémon constants) | Medium | 1–4 |
| 7 | Game-level test suite (real ROMs) | High | 1–6, BYO ROMs |
| 8 | `NetworkBackend` + `PyBoyLinkSession.listen/connect` | High | 1–4 |
| 9 | CGB fast-serial (`SC` bit 1) | Medium | 1 |
| 10 | Retire `LegacySerial` (later major version) | Low | 1–8 in production |

Milestones 1–7 target Pokémon R/B/Y same-process linked play, which
is what the research report identifies as the unblocking win. Milestone
8 extends to separate processes / separate machines. Milestone 9 closes
the hardware-coverage gap for Gen II and beyond. Milestone 10 is
housekeeping.

## Risks and mitigations

**Save-state compatibility.** The serial state format changes. Mitigated
by the `LegacySerial`/new-core coexistence during transition; new core
uses a new save-state block tag.

**Cython boundary and performance.** The serial core is called on every
CPU cycle when a transfer is active. It must stay in Cython or at
least hold a tight inner loop that doesn't cross the Python boundary
per bit. Mitigated by keeping the core in the same module as the rest
of the hot-path devices; callbacks to the Python-side backend fire only
on edge boundaries (up to 8 per byte), not per CPU cycle.

**Python-side attach against wheel PyBoy is blocked.** Confirmed via
the `pokered-harness` companion repo's `PyBoyLinkSession` prototype:
wheel-installed PyBoy is fully Cython-compiled — `cdef Motherboard mb`,
`cdef Serial serial` — so neither `pyboy.mb` nor `pyboy.mb.serial` is
Python-accessible, and a pure-Python `SerialCore` cannot be swapped in
from outside the C extension. Any integration targeting the
wheel-installed PyBoy has to either (a) install PyBoy from source with
the Cython extension disabled, or (b) land the serial overhaul inside
PyBoy itself and ship a new wheel. Option (b) is this document's
intended outcome; option (a) is the near-term development mode for
contributors prototyping the new core.

**Non-blocking network edge.** WAN jitter could bubble up as
frame-rate stutter. Mitigated by a small jitter buffer (documented
in [PyBoy wiki's Student-Projects page](https://github.com/Baekalfen/PyBoy/wiki/Student-Projects)
as a known research area). First milestone targets LAN only.

**Desync edge cases in Pokémon's own code.** Accurately reproducing
the Gen I link-battle bugs (Metronome/Mirror Move, damage-mod-256)
is a goal, not an accident. Tested as invariants.

## Out of scope for v1

- Four-player modes (Gen II supports them via a pass-through multi-cart;
  architecture accommodates N>2 but tests stay at 2).
- Time Capsule (Gen I ↔ Gen II transfers); a later milestone once Gen
  II support lands in PyBoy generally.
- Infrared (Gen II Mystery Gift) — separate peripheral, different wire
  protocol.
- Emulating hardware imperfections beyond the master-pull-up model
  (cable resistance, differential signaling).

## Acceptance criteria for v1

- All three tiers of tests green on CI.
- Two PyBoy instances trade a Pokémon end-to-end on Red/Blue/Yellow
  cross-version pairings.
- Two PyBoy instances complete a link battle turn with correct move
  resolution on both sides.
- Existing single-instance PyBoy users see zero behavior change (no
  regression in the 280+ existing PyBoy tests).
- Documentation covers the public API + a working example under
  `examples/link_trade.py`.

## References

### PyBoy

- [Repository](https://github.com/Baekalfen/PyBoy) and
  [docs](https://docs.pyboy.dk/)
- [Wiki — Student-Projects](https://github.com/Baekalfen/PyBoy/wiki/Student-Projects)
- [Issue #29 — link-cable history](https://github.com/Baekalfen/PyBoy/issues/29)
- [PR #232 — early serial over IP](https://github.com/Baekalfen/PyBoy/pull/232)
- [PR #344 — partial Pokémon Yellow trading](https://github.com/Baekalfen/PyBoy/pull/344)

### Hardware reference

- [Pan Docs — Serial Data Transfer (Link Cable)](https://gbdev.io/pandocs/Serial_Data_Transfer_(Link_Cable).html)

### Game code (Gen I Pokémon)

- [pret/pokered](https://github.com/pret/pokered) —
  `constants/serial_constants.asm`, `home/serial.asm`,
  `engine/link/cable_club.asm`, `engine/battle/core.asm`,
  `engine/pokemon/add_mon.asm`, `ram/wram.asm`, `symbols/pokered.sym`
- [pret/pokeyellow](https://github.com/pret/pokeyellow) — same file set

### Reference emulators

- [SameBoy](https://github.com/LIJI32/SameBoy) —
  `Core/gb.h`, `libretro/libretro.c`
- [mGBA](https://github.com/mgba-emu/mgba) —
  `include/mgba/internal/gb/sio.h`, `src/gb/sio.c`,
  `src/platform/qt/MultiplayerController.cpp`

### Companion / fixture sources

- [pokered-harness](https://github.com/timothywaynegregg/pokemon)
  (this repo) — `pokered_harness.link.*`, fixture scripts, symbol
  machinery ready to plug into the game-level test suite
- [vaguilar/pokemon-red-cable-club-hack](https://github.com/vaguilar/pokemon-red-cable-club-hack)
- [Nitwhiz — Pokémon trade write-up](https://nitwhiz.github.io/)
- [tzwenn/PokeDuino](https://github.com/tzwenn/PokeDuino) — optional
  hardware-relay smoke test for real-line behavior

---

*This document is a design proposal, not a commitment. Discussion on
architecture, API shape, and migration path is expected before
implementation begins. Contributions to the proposal itself — additional
references, clarifications of hardware semantics, or alternative
architectural models — welcome.*
