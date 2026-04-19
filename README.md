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
works around this by running two `Session` instances and installing
execution hooks at pret serial-routine labels
(`Serial_ExchangeBytes`,
`Serial_TryEstablishingExternallyClockedConnection`, ...); when the
game enters one of those helpers, a `SerialBridge` swaps the pending
HRAM bytes between sides and forces the connection-status cell to
"linked". The two sessions are stepped in lockstep by `LinkPair`.

Label set is validated against real `pokeblue.sym` and `pokeyellow.sym`
by `tests/test_link_symbols_real_roms.py` (skipped when the matching
ROM's `.sym` is absent). Pairing install is smoke-tested against real
ROMs in `tests/test_link_integration.py`.

Peer env vars (all three optional — unset means single-session mode):

```bash
POKERED_PEER_ROM_PATH=rom/blue/pokemon-blue.gb \
POKERED_PEER_SYM_PATH=rom/blue/pokemon-blue.sym \
POKERED_PEER_ROM_SHA1=<see VERSIONS.md>
```

MCP tools:

- `link_pair` — build the bridge and install hooks.
- `link_step {"count": 60}` — advance both sides 60 ticks, interleaved.
- `link_unpair` — drop the bridge (pre-existing session hooks survive).

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

Current limitations: PyBoy 2.7.0 has no `hook_deregister`, so `unpair`
leaves dormant callbacks in place; Mt. Moon → Cerulean progression is
not yet scripted so Cable Club fixtures must be produced manually;
trade-only — link battle is deferred.

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
