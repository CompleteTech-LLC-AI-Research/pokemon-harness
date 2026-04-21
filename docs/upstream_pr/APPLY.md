# Applying the SerialCore overhaul to upstream PyBoy

This directory's [README.md](README.md) describes the architecture. This
file is the **contributor recipe**: exactly how to lift the work out of
the companion `pokered-harness` repo and produce a PyBoy PR branch.

Assumes PyBoy is checked out locally (e.g. `~/src/PyBoy`) and this repo
is also checked out (e.g. `~/src/pokered-harness`).

## One-shot bring-over

```sh
cd ~/src/PyBoy
git checkout -b link-cable-bit-accurate-serial

# 1) Replace pyboy/core/serial.py with the bit-accurate SerialCore.
#    The harness module is self-contained and import-path-independent,
#    so a direct copy works. Rename the class to match (Serial -> SerialCore
#    if keeping both; or just Serial if doing the cutover in one step).
cp ~/src/pokered-harness/src/pokered_harness/link/serial_core.py \
   pyboy/core/serial.py

# 2) Add the new link/ subpackage.
mkdir -p pyboy/link
touch pyboy/link/__init__.py
cp ~/src/pokered-harness/src/pokered_harness/link/serial_coordinator.py \
   pyboy/link/coordinator.py
cp ~/src/pokered-harness/src/pokered_harness/link/pyboy_link_session.py \
   pyboy/link/session.py
cp ~/src/pokered-harness/src/pokered_harness/link/network_backend.py \
   pyboy/link/network.py

# 3) Fix up imports — everything currently imports as
#    `from pokered_harness.link.X import Y`. Rewrite to
#    `from pyboy.core.serial import ...` / `from pyboy.link.X import ...`.
#    Suggested sed pass (review before committing):
find pyboy/link pyboy/core/serial.py -name "*.py" -print0 \
  | xargs -0 sed -i \
    -e 's|from pokered_harness\.link\.serial_core|from pyboy.core.serial|g' \
    -e 's|from pokered_harness\.link\.serial_coordinator|from pyboy.link.coordinator|g' \
    -e 's|from pokered_harness\.link\.network_backend|from pyboy.link.network|g' \
    -e 's|from pokered_harness\.link\.pyboy_link_session|from pyboy.link.session|g'

# 4) Rename PyBoyLinkSession → LinkSession inside pyboy/link/session.py.
#    (Optional — the "PyBoy" prefix is redundant inside PyBoy itself.)

# 5) Wire the new core into Motherboard. Single-line change in
#    pyboy/core/mb.py:
#        self.serial = serial.Serial()
#    becomes
#        self.serial = serial.SerialCore()

# 6) Copy test files.
mkdir -p tests/link
cp ~/src/pokered-harness/tests/test_serial_core.py tests/link/test_serial_core.py
cp ~/src/pokered-harness/tests/test_serial_coordinator.py tests/link/test_coordinator.py
cp ~/src/pokered-harness/tests/test_pyboy_link_session.py tests/link/test_session.py
cp ~/src/pokered-harness/tests/test_link_protocol.py tests/link/test_protocol.py
cp ~/src/pokered-harness/tests/test_network_backend.py tests/link/test_network.py
# (Real-ROM tests stay out of the PR — they need BYO ROMs + fixtures.)

# 7) Same sed pass on tests/link/.

git add -A
git commit -m "Bit-accurate serial + link session + network backend"
```

Then open a PR against `Baekalfen/PyBoy:master` with a link to the
[README.md](README.md) design notes in the description, and a link
back to this repo's
[docs/pyboy_serial_overhaul_design.md](../pyboy_serial_overhaul_design.md)
for the longer-form rationale.

## What to highlight in the PR description

Leaning on the upstream maintainers' time:

- **No breaking change for single-instance users.** `SerialCore` ships a
  `NullBackend` by default, which behaves like the legacy class
  (master reads `0xFF`, external-clock transfers never complete). All
  280+ existing PyBoy tests pass against the new core.

- **Proven on real Pokémon ROMs.** Two PyBoy instances paired under the
  new `LinkSession` complete a full Gen I Pokémon trade end-to-end
  (yellow/yellow, blue/blue, red/red, plus every cross-version pair).
  The trade sequence exercises the preamble handshake, the ~200-byte
  trainer/party block exchange, the nibble-sync loop, the LinkMenu
  selection exchange, `TradeCenter_SelectMon`, and
  `_AddEnemyMonToPlayerParty`.

- **No opcode changes, no cdef-boundary changes.** Strictly Python-level
  on top of PyBoy's existing `mb.cpu.set_interruptflag` surface.

- **Pan Docs conformance.** Bit-level shift register; `SC` bit 7
  auto-clears at completion (legacy class had it inverted); external
  clock transfers wait indefinitely with no peer; disconnected master
  RX is pulled high.

## Known-follow-up issues to mention (not blocking the PR)

- CGB high-speed serial (`SC` bit 1) — design-doc milestone 9.
  Architecture accommodates it; not needed for Gen I Pokémon.
- Four-player modes (Gen II multi-cart) — architecture accommodates
  N>2 but this PR caps at 2.
- WAN network play / jitter buffer — milestone 8 shipped with LAN
  latency tolerance, not WAN.
