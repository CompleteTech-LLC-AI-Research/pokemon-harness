# Upstreaming the bit-accurate serial + link-session work into PyBoy

This directory packages the design-doc implementation
(`src/pokered_harness/link/*`) as a drop-in contribution for mainline
[Baekalfen/PyBoy](https://github.com/Baekalfen/PyBoy). It assumes a
clean PyBoy source checkout (current `master`) and provides:

1. **File mapping** — where each harness module should land in PyBoy's tree.
2. **Motherboard wiring** — the small edits to `pyboy/core/mb.py`
   needed so `mb.serial` can be swapped at runtime from `pyboy/link/`.
3. **Tests** — unit tests that ship alongside the new code.
4. **CI expectations** — what a green CI run should look like.

The work is proven end-to-end on real Yellow ROMs via the
`pokered-harness` worktree that developed it: two Yellow PyBoy
instances, paired under `PyBoyLinkSession`, reach the Cable Club
`LinkMenu` through the bit-accurate `SerialCore` within ~500
emulated frames. Same for Blue+Blue. Red+Red + cross-version
pairings follow the same code path once the test fixtures land.

## File mapping

| pokered-harness source | PyBoy target path | Notes |
|---|---|---|
| `src/pokered_harness/link/serial_core.py` | `pyboy/core/serial.py` | Replace the legacy `Serial` class entirely. Rename the current class to `LegacySerial` if a staged cutover is preferred (see design doc's migration section). |
| `src/pokered_harness/link/serial_coordinator.py` | `pyboy/link/coordinator.py` | New module. |
| `src/pokered_harness/link/pyboy_link_session.py` | `pyboy/link/session.py` | New module. Rename class to `LinkSession` (drop the `PyBoy` prefix once inside PyBoy itself). |
| `src/pokered_harness/link/network_backend.py` | `pyboy/link/network.py` | New module. |
| `src/pokered_harness/link/__init__.py` | `pyboy/link/__init__.py` | Re-exports. |

No changes to `pyboy/core/cpu.py`, no changes to opcode handling,
no changes to any cdef boundaries: the design is explicitly
Python-level on top of PyBoy's existing register API.

## Motherboard wiring

`pyboy/core/mb.py` requires exactly the changes listed below.
All other call-sites (`self.serial.set_SB`, `.set_SC`, `.tick`,
`.save_state`, `.load_state`, `._cycles_to_interrupt`) are
duck-type-compatible with the new `SerialCore`, so they work
unchanged.

```diff
 from . import bootrom, cartridge, cpu, interaction, lcd, ram, serial, sound, timer
 ...
     def __init__(...):
         ...
-        self.serial = serial.Serial()
+        # SerialCore ships a NullBackend by default — identical
+        # behaviour to the legacy class for single-instance users.
+        self.serial = serial.SerialCore()
```

Save-state compatibility: the new `SerialCore.save_state` /
`load_state` methods add two extra fields (`_shift_register`,
`_bits_remaining`) at the end of the block. Legacy state streams
simply hit EOF where those fields would be, which `load_state`
tolerates by synthesizing "not in transfer" values. A bumped
`STATE_VERSION` (currently `1` in the harness) lets future migrations
detect the new format cleanly.

## Test plan (ships with the PR)

| Tier | Source file | What it proves |
|---|---|---|
| Register | `tests/test_serial_core.py` (23 tests) | Pan Docs-correct master / slave / disconnect / IRQ / bit-shift semantics. ROM-free. |
| Coordinator | `tests/test_serial_coordinator.py` (16 tests) | Two cores under `LockstepCoordinator` exchange bytes; peer-transfer IRQ fires. |
| Session | `tests/test_pyboy_link_session.py` (11 tests) | `LinkSession.attach` / `detach` / `step` with a fake-PyBoy stub. |
| Protocol | `tests/test_link_protocol.py` (26 tests) | Pokémon constants (`0xFD` preambles, `0xFE` filler, nibble menus, battle actions) relay byte-for-byte. |
| Network | `tests/test_network_backend.py` (7 tests) | TCP bit-exchange, listen/connect, error paths. |
| Integration | `tests/test_pyboy_link_session_roms.py` (2 tests active, 9 parameterized) | Real-ROM smoke + flagship LinkMenu reach (Yellow and Blue confirmed; Red pending fixture). ROM- and fixture-gated. |

That's **83+ new tests**, all green. The real-ROM integration tests
are gated behind BYO-ROM skip-ifs identical to PyBoy's existing
pattern, so CI without ROMs stays green.

## Key design decisions to call out in the PR description

1. **Bit-accurate, not byte-accurate.** Each edge is observable.
   Matches SameBoy's public callback model and lets users building
   link-aware features (hooks, recording, etc.) intercept at the
   right granularity.

2. **Null / Local / Network backends.** Mirrors mGBA's
   driver abstraction. The `Null` backend preserves current
   single-instance semantics exactly (pulled-up line, master reads
   `0xFF`), so existing users see no behavior change.

3. **IRQ on peer-driven completion** (the critical fix discovered
   while integrating against real Pokémon ROMs): when the peer's
   master drives the slave's 8th edge, the slave's CPU needs
   `INTR_SERIAL` raised. The coordinator wires this as a callback
   so the session can trigger the motherboard's interrupt-flag
   setter without the `SerialCore` knowing about CPUs.

4. **Sub-frame interleaving via `breakpoint_singlestep`.**
   Pokémon's tight `Serial_SyncAndExchangeNybble` loop oscillates
   master↔slave faster than per-frame interleaving can follow.
   `LinkSession.step_interleaved` uses PyBoy's existing
   singlestep mode to alternate CPUs at ~256-cycle granularity —
   no new internal API needed.

5. **No opcode or CPU changes.** The design is strictly
   Python-level on top of PyBoy's existing memory- and
   interrupt-register surface. Keeps the PR reviewable and
   preserves performance for non-linked users.

## Validation we did in the companion repo

Single-source-of-truth for "did this actually work?" lives in
[pokered-harness](https://github.com/timothywaynegregg/pokemon)
on the `claude/infallible-torvalds-e877a6` branch. The flagship
test `test_pair_reaches_link_menu_via_pyboy_link_session` asserts
on hook counters for `CableClubNPC`, `SaveGameData`,
`Serial_SyncAndExchangeNybble`, `LinkMenu`, and
`Serial_ExchangeBytes`. Observed on Yellow+Yellow and Blue+Blue:

```
CableClubNPC: [1, 1]
SaveGameData: [1, 1]
Serial_SyncAndExchangeNybble: [1, 1]
Serial_ExchangeBytes: [0, 0]
LinkMenu: [1, 1]        ← target — both sides reached the menu
frames_used: ~500
```

`Serial_ExchangeBytes=0` because the test stops at `LinkMenu`; the
next step (drive past `LinkMenu` into `TradeCenter_SelectMon`) runs
that routine many times.

## Contributor mode — running the new code against mainline PyBoy

Because the wheel-installed PyBoy is Cython-compiled, `mb` and
`mb.serial` aren't Python-accessible. Contributors prototyping the
upstream PR should install PyBoy from source with Cython disabled
(mirrors what this worktree does):

```sh
python -m venv .venv-noncython
.venv-noncython/Scripts/pip install numpy
git clone --depth 1 https://github.com/Baekalfen/PyBoy.git vendor/pyboy-src
# Patch vendor/pyboy-src/setup.py so CYTHON respects PYBOY_NO_CYTHON:
#   CYTHON = platform.python_implementation() == "CPython" \
#       and not os.getenv("PYBOY_NO_CYTHON")
PYBOY_NO_CYTHON=1 .venv-noncython/Scripts/pip install --no-build-isolation -e vendor/pyboy-src
.venv-noncython/Scripts/pip install -e . pytest pytest-asyncio mcp
```

Once the PR lands the Cython build should compile the new code as-is
(the `SerialCore` methods are plain Python and `cdef class` auto-promotes
instance attributes). No special build flags needed for the released wheel.
