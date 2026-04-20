# pokered-harness

Memory-first automation harness for **Pokémon Red / Blue (UE)** on
**PyBoy 2.7.0**, exposed over MCP. See
[`plans/deep-research-report-glittery-rossum.md`](../.claude/plans/deep-research-report-glittery-rossum.md)
for the ADR that motivates this architecture.

The package name is `pokered_harness` for historical reasons; Red is the
canonical target but Blue (v1.0, UE) also works unchanged — see
[`VERSIONS.md`](VERSIONS.md) for pinned SHA-1s.

## Supported games

| Game | Status | ROM pins | Verified milestone |
|---|---|---|---|
| Pokémon Red (UE) | ✅ Supported | [`VERSIONS.md`](VERSIONS.md) | Boulder Badge end-to-end |
| Pokémon Red + Full Color Hack | ✅ Supported | [`VERSIONS.md`](VERSIONS.md) | Boulder Badge end-to-end |
| Pokémon Blue (UE) | ✅ Supported | [`VERSIONS.md`](VERSIONS.md) | Boulder Badge end-to-end |
| Pokémon Blue + pokeblue_color_vanilla.ips | ✅ Supported | [`VERSIONS.md`](VERSIONS.md) | Boulder Badge end-to-end |
| Pokémon Yellow (UE) | ✅ Supported | [`VERSIONS.md`](VERSIONS.md) | Boulder Badge end-to-end |
| JP Red, other localisations, ROM hacks | ❌ Out of scope | — | — |

Red and Blue share pokered's WRAM layout, so the state parsers and event
hooks work for both. Blue needs its own symbol file (`pokeblue.sym`) and
a handful of Blue-specific navigation tweaks live in
[`scripts/blue_forest_to_brock.py`](scripts/blue_forest_to_brock.py);
everything else is version-neutral.

## BYO-ROM

This repo contains no ROMs and no build-derived artifacts (no `.gb`,
`.gbc`, `.sym`, `.map`, or save states of commercial game content). You
must supply your own legally obtained copies.

ROMs go under `rom/<version>/` (all gitignored):

```
rom/
├── red/pokemon-red.gb
├── blue/pokemon-blue.gb
└── yellow/pokemon-yellow.gbc
```

Record each ROM's SHA-1 in [`VERSIONS.md`](VERSIONS.md) — the session
manager refuses to run without a match.

## Prerequisites

1. **Python 3.11+**.
2. **PyBoy 2.7.0** — pinned in `pyproject.toml`.
3. **Symbol files** built with `DEBUG=1` from:
   - [`pret/pokered`](https://github.com/pret/pokered) — produces both `pokered.sym` and `pokeblue.sym`. Place at `rom/red/pokemon-red.sym` and `rom/blue/pokemon-blue.sym`.
   - [`pret/pokeyellow`](https://github.com/pret/pokeyellow) — produces `pokeyellow.sym`. Place at `rom/yellow/pokemon-yellow.sym`.

### Generating the symbol files

You need [RGBDS](https://rgbds.gbdev.io/), GNU make, and a C compiler
(gcc or clang-with-gcc-alias) on `PATH`. Then:

```bash
# Red + Blue share a source tree
git clone https://github.com/pret/pokered.git
cd pokered
make DEBUG=1                           # produces pokered.sym / pokered.map / pokered.gbc
make clean && make blue DEBUG=1        # produces pokeblue.sym / pokeblue.map / pokeblue.gbc
cd ..

# Yellow is a separate tree
git clone https://github.com/pret/pokeyellow.git
cd pokeyellow
make DEBUG=1                           # produces pokeyellow.sym / pokeyellow.map / pokeyellow.gbc
```

Copy the `.sym` files into the corresponding `rom/<version>/` dirs and
record the upstream commit SHAs in [`VERSIONS.md`](VERSIONS.md).

## Install

```bash
python -m venv .venv
. .venv/Scripts/activate   # Windows bash; use .venv/bin/activate on Unix
pip install -e ".[dev]"
```

## Test

```bash
pytest
```

## Running

The MCP server is wired in [`.mcp.json`](.mcp.json); it reads
`POKERED_ROM_PATH`, `POKERED_SYM_PATH`, and `POKERED_ROM_SHA1` from the
environment. Point those at whichever version you're driving:

```bash
# Red (default in .mcp.json)
POKERED_ROM_PATH=rom/red/pokemon-red-color.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=<see VERSIONS.md>

# Blue
POKERED_ROM_PATH=rom/blue/pokemon-blue-color.gb \
POKERED_SYM_PATH=rom/blue/pokemon-blue.sym \
POKERED_ROM_SHA1=<see VERSIONS.md>

# Yellow (native CGB, no color patch needed)
POKERED_ROM_PATH=rom/yellow/pokemon-yellow.gbc \
POKERED_SYM_PATH=rom/yellow/pokemon-yellow.sym \
POKERED_ROM_SHA1=<see VERSIONS.md>
```

End-to-end scripts live under `scripts/`:

- `scripts/full_to_brock.py` — Red (colorized): intro → Boulder Badge.
- `scripts/blue_forest_to_brock.py` — Blue (colorized): Option-B
  harness that RAM-boosts Bulbasaur past the grind gap, then runs
  forest → Pewter → Brock. Will be replaced by a proper Route 2 heal
  loop in a future iteration.
- `scripts/yellow_to_brock.py` — Yellow: full intro → Pikachu →
  rival battle → Pallet → Viridian → Route 2 → Forest → Pewter →
  Brock. Reuses the Blue harness's A* navigation legs unchanged and
  applies the same Option-B RAM boost (L50 Pikachu with Thunderbolt +
  Double Kick) to clear Brock's Rock/Ground team; replace with real
  Route 2 grind when the heal-loop lands.

## Link cable

PyBoy has no hardware-level link-cable emulation (see
[PyBoy #29](https://github.com/Baekalfen/PyBoy/issues/29)). The harness
works around this by hooking pret's serial-routine labels
(`Serial_ExchangeBytes`, `Serial_ExchangeNybble`,
`Serial_ExchangeLinkMenuSelection`,
`Serial_TryEstablishingExternallyClockedConnection`) and exchanging
bytes at the semantic WRAM layer. Two deployment modes are supported:

### Mode A: single-process pair (`LinkPair`)

One process, two `Session` objects, stepped in lockstep by
[`LinkPair`](src/pokered_harness/link/pair.py). A `SerialBridge` swaps
the pending HRAM bytes between sides in-process. Useful for local
trade-simulation, debugging, and the integration test suite.

Peer env vars (all three optional — unset means single-session mode):

```bash
POKERED_PEER_ROM_PATH=rom/blue/pokemon-blue.gb \
POKERED_PEER_SYM_PATH=rom/blue/pokemon-blue.sym \
POKERED_PEER_ROM_SHA1=<see VERSIONS.md>
```

MCP tools for Mode A:

- `link_pair` — build the bridge and install hooks.
- `link_step {"count": 60}` — advance both sides 60 ticks, interleaved.
- `link_peer_press / link_peer_hold / link_peer_release` — drive the peer.
- `link_unpair` — drop the bridge.

### Mode B: two agents, one ROM each (`RemoteLinkEndpoint`)

Two independent MCP servers, each owning exactly one `Session`, connected
by a TCP `SerialLink`. Each server installs a
[`RemoteLinkEndpoint`](src/pokered_harness/link/remote.py) pointed at
the link; serial routines that fire on one side block on a
`link.exchange(...)` RPC until the other side fires the matching routine.
This is the deployment where **two independent AI agents each drive
their own Pokémon** and trade or battle each other — no shared state, no
shared memory, just the cable.

Cross-version correctness: the RPC `kind` is a *symbol name* resolved on
each side against its own `.sym` file. Blue's `wSerialPlayerDataBlock`
at `0xD152` and Yellow's at `0xD151` both serialize as
`exchange_bytes/wSerialPlayerDataBlock` on the wire, so a Blue ↔ Yellow
trade is wire-compatible and the bytes land at the correct per-version
address on each side.

MCP tools for Mode B:

- `link_listen {"port": 9999}` — bind TCP (internal-clock master role).
  Returns immediately; poll `link_status` for `remote_mode=="connected"`.
- `link_connect {"host": "peer.host", "port": 9999}` — connect to a
  listening peer (external-clock slave role).
- `link_status` — snapshot of local state (paired/listening/connected).
- `link_disconnect` — close the link.

Typical flow for two-agent trading, assuming both servers have
`POKERED_ROM_PATH` set to their respective ROM:

1. Agent A (listener): call `link_listen {"port": 9999}`.
2. Agent B (connector): call `link_connect {"host": "A's host", "port": 9999}`.
3. Both poll `link_status` until `remote_mode == "connected"`.
4. Both agents drive their own sessions into Cerulean Pokémon Center
   → Cable Club attendant using normal `press` / `step` tools. When the
   game runs `Serial_ExchangeBytes` it's transparently wired to the
   peer's matching call over TCP.
5. `link_disconnect` when done.

### Label / symbol validation

Label set is validated against real `pokered.sym`, `pokeblue.sym`, and
`pokeyellow.sym` by
[`tests/test_link_symbols_real_roms.py`](tests/test_link_symbols_real_roms.py)
(skipped when the matching ROM's `.sym` is absent). Mode A handshake is
smoke-tested in
[`tests/test_link_integration.py`](tests/test_link_integration.py);
Mode B is covered end-to-end by
[`tests/test_link_integration_remote.py`](tests/test_link_integration_remote.py).

### Mode B coverage matrix

Listener × connector, with the milestones driven end-to-end over
localhost TCP. Role matters: listener is the internal-clock master,
connector the external-clock slave — a reversed pair is a distinct
wire configuration.

| Listener | Connector | Handshake | Nybble → LinkMenu | RPC-kind observation |
|---|---|---|---|---|
| blue | blue | ✅ | ✅ | ✅ |
| blue | yellow | ✅ | ✅ | — |
| yellow | blue | ✅ | ✅ | — |
| yellow | yellow | ✅ | ✅ | — |
| red | red | ⏭ fixture gap | ⏭ fixture gap | — |
| red | blue | ⏭ fixture gap | ⏭ fixture gap | — |
| blue | red | ⏭ fixture gap | ⏭ fixture gap | — |
| red | yellow | ⏭ fixture gap | ⏭ fixture gap | — |
| yellow | red | ⏭ fixture gap | ⏭ fixture gap | — |

Fixture gaps:

- **Red** — no `tests/fixtures/link/red/cable_club.state` exists.
  Mt. Moon → Cerulean progression is not yet scripted in the Red
  harness, so the fixture has never been produced.
- **Yellow** — `tests/fixtures/link/yellow/cable_club.state` (git-
  ignored along with all `*.state` files per the BYO-ROM policy) is
  produced locally by running
  [`scripts/produce_yellow_cable_club_fixture.py`](scripts/produce_yellow_cable_club_fixture.py).
  That script expects the sibling worktree's
  `walkthrough_to_cerulean/milestones/cerulean_pc.state` as input —
  a state the Yellow walkthrough harness produces via a blackout-warp
  shortcut to the Cerulean Pokecenter with correct CGB palette (via
  the nurse-heal trigger). From there the script encodes the
  `up×4, left×2, down, right-until-x=11, up` route that sidesteps the
  nurse NPC at (4, 3) and lands the player on the only tile where
  pressing A fires `CableClubNPC` on Yellow (discovered by hooking
  `01:7035 CableClubNPC` across x=5..12 — only x=11 triggers).

Transport-layer behaviour past LinkMenu
(`Serial_ExchangeLinkMenuSelection`, `Serial_ExchangeBytes` for the
three RNG/player-data/patch-list blocks inside `CableClub_DoBattleOrTrade`)
is covered separately:

- In-process, end-to-end on real ROMs:
  `test_link_integration.test_link_trade_roundtrip` (blue).
- Remote, unit-level on `InProcessSerialLink`:
  `test_remote_endpoint.test_exchange_menu_selection_exchanges_two_bytes`
  and `test_exchange_bytes_cross_version_translates_via_symbol`.
- Remote, over actual TCP: the kind-observation test above proves the
  RPC routing layer is correct; the generic
  `test_serial_link.test_tcp_exchange_round_trip` and
  `test_tcp_larger_payload` cover arbitrary byte payloads through the
  same transport.

Driving the LinkMenu selection + full trade/battle UI *over the remote
endpoint* is agent-policy work (two MCP-driven Sessions need
coordinated A-press timing). The transport is proven; producing a
well-walked fixture and/or writing the coordinated press scripts is
the next iteration.

### Producing Cable Club save states

Actual trade / link-battle testing needs both sides sitting at the
Cerulean Pokémon Center Cable Club attendant with 2+ Pokémon in party.
The first accessible Cable Club is in Cerulean City (after Brock →
Mt. Moon). The repo ships harness scripts through Boulder Badge; Mt.
Moon → Cerulean progression is not yet automated.

Until that lands, produce each fixture manually:

1. Launch `scripts/walkthrough.py --view` (or any interactive script)
   with the target ROM.
2. Play through to Cerulean Pokémon Center, enter the Cable Club,
   stand in front of the trade attendant.
3. At a Python prompt (or mid-script), call
   `open("tests/fixtures/link/<version>/cable_club.state", "wb").write(session.save_state())`.
4. Repeat for the peer version.
5. Run `python scripts/link_trade_demo.py --primary blue --peer yellow --view`.

Current limitations: PyBoy 2.7.0 has no `hook_deregister`, so unpair
leaves dormant callbacks in place; Mt. Moon → Cerulean progression is
not yet scripted so Cable Club fixtures must be produced manually;
trade-only — link battle reuses the same transport but the UI-side
wiring is deferred.

## Color rendering

The harness constructs `PyBoy(..., cgb=True)`. Stock Red/Blue render
through the CGB auto-palette (uniform tint). For authentic per-sprite
coloring, apply the respective Full Color Hack IPS with
`scripts/apply_color_patch.py` — it auto-recomputes ROM header and
global checksums so PyBoy accepts the output.

The default window driver is `null` (headless) for tests and MCP. Pass
`view=True` to `Session` (or `--view` to `scripts/walkthrough.py`) to
open PyBoy's SDL2 viewer and watch the game live.

## License

The harness code in this repo is LGPL-3.0-only to match PyBoy's license.
No game-derived assets are distributed.
