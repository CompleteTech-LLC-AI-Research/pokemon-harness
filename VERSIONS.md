# Pinned versions

This file is the single source of truth for every version the harness depends
on. The session manager reads the expected values from here (currently by
caller code; a loader is planned) and refuses to run if the loaded ROM or
PyBoy version does not match.

## Emulator

| Component | Pin | Notes |
|---|---|---|
| PyBoy | `2.7.0` | v2 API — `window="null"`, `tick(n, render=...)`, `memory[...]`, `hook_register`, `symbol_lookup`. CGB mode is used to enable color rendering for stock DMG Red/Blue. |

## Target ROMs

BYO-ROM — this repo ships neither any ROM nor any build-derived artifact.
ROM files live under `rom/<version>/` (gitignored).

### Pokémon Red (UE) — internal title `POKEMON RED`

| Field | Value |
|---|---|
| SHA-1 | `ea9bcae617fdf159b045185467ae58b2e4a48b9a` |
| Size | 1,048,576 bytes (1 MiB) |
| Path | `rom/red/pokemon-red.gb` |
| Release | Red UE v1.0 (canonical mainline) |

#### Optional: Pokémon Red Full Color Hack (vanilla variant)

For authentic colorized playback the harness also accepts the **Full
Color Hack v1.2** vanilla IPS applied via `scripts/apply_color_patch.py`.
Patch source: `romhacking.net/hacks/1385/`. The patch explicitly avoids
data shifting, so WRAM addresses and the pret-generated `.sym` are
unchanged — the same memory parsers work against this variant.

| Field | Value |
|---|---|
| SHA-1 | `e1deed63080bc24cad5fba18ecb3184f905d16d4` |
| Size | 1,048,576 bytes (1 MiB, unchanged) |
| Path | `rom/red/pokemon-red-color.gb` |
| Base | Stock Red UE + `pokered_color_vanilla.ips` |
| `.sym` | Same `pokered.sym` as stock |

### Pokémon Blue (UE) — internal title `POKEMON BLUE`

Blue is built from the same `pret/pokered` tree as Red (via
`make blue DEBUG=1`) and shares WRAM addresses. All harness state
parsers and event hooks work on Blue unchanged.

| Field | Value |
|---|---|
| SHA-1 | `d7037c83e1ae5b39bde3c30787637ba1d4c48ce2` |
| Size | 1,048,576 bytes (1 MiB) |
| Path | `rom/blue/pokemon-blue.gb` |
| Release | Blue UE v1.0 (canonical mainline) |
| `.sym` | `rom/blue/pokemon-blue.sym` (from `pokeblue.sym`, 21,137 lines) |

#### Optional: Pokémon Blue color IPS (vanilla variant)

`scripts/apply_color_patch.py` applies
`rom/blue/patch/pokeblue_color/pokeblue_color_vanilla.ips` and
recomputes the ROM header checksum at `0x14D` and global checksum at
`0x14E-0x14F` automatically — the shipped IPS leaves both stale, which
PyBoy rejects. Output is emulator-ready.

| Field | Value |
|---|---|
| SHA-1 | `5f4b05725a860e04077045462176d3e2771c5022` |
| Size | 1,048,576 bytes (1 MiB, unchanged) |
| Path | `rom/blue/pokemon-blue-color.gb` |
| Base | Stock Blue UE + `pokeblue_color_vanilla.ips` (+ checksum fix) |
| `.sym` | Same `pokeblue.sym` as stock Blue |

### Pokémon Yellow — in progress (2026-04-19)

Not yet supported by the harness. Adding this is the current work
stream. Yellow is a native Game Boy Color cartridge (`.gbc`) built from
the separate [`pret/pokeyellow`](https://github.com/pret/pokeyellow)
repo, which shares most of its WRAM layout with pokered but not all —
the parsers will need a symbol-driven audit pass when we stand up
Yellow support.

| Field | Value |
|---|---|
| SHA-1 | `cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1` |
| Size | 1,048,576 bytes (1 MiB) |
| Path | `rom/yellow/pokemon-yellow.gbc` |
| Release | Yellow UE v1.0 |
| `.sym` | Pending — needs `pret/pokeyellow` checkout + `make DEBUG=1` |

### Local dev-machine paths (not committed)

For the laptop that authored this repo:

- Red ROM: `G:\project\pokemon\PokemonRed.gb`
- Red symbol file: `G:\project\pokemon\PokemonRed.sym` (rgblink-generated, 21,137 lines, ~20,213 harness-loadable symbols)
- Vendored pokered source: `G:\project\pokemon\_vendor\pokered\`
  - Produces `pokered.sym`, `pokeblue.sym`, `pokeblue_debug.sym`.

Yellow-related paths will be added here once the Yellow symbol build lands.

These live in a parallel workspace on a different drive and are **not**
part of this repo — record them in your personal `.env` or a local-only
config file, never in a committed file.

## pret upstream (for symbol generation)

| Field | Value |
|---|---|
| pret/pokered | `https://github.com/pret/pokered` — produces both `pokered.sym` (Red) and `pokeblue.sym` (Blue). Commit SHA: `<FILL-IN from: git -C G:/project/pokemon/_vendor/pokered rev-parse HEAD>` |
| pret/pokeyellow | `https://github.com/pret/pokeyellow` — Yellow only. Not yet vendored; add commit SHA when Yellow support lands. |
| Build flag | `make DEBUG=1` (produces `.sym` and `.map`) |
| Toolchain | rgbds — bundled at `_vendor/rgbds-1.0.1-src/` on the dev machine. |

Generated `.sym` files are **not** committed to this repo.

## Symbol coverage (verified end-to-end on Red + Blue)

Every symbol the v1 harness reads or plans to hook has been confirmed
present in both `pokered.sym` and `pokeblue.sym`:

Overworld/menu/text: `wCurMap`, `wXCoord`, `wYCoord`, `wWalkCounter`,
`wSpritePlayerStateData1FacingDirection`, `wCurrentMapScriptFlags`,
`wCurMapScript`, `wStatusFlags5`, `wCurrentMenuItem`, `wMaxMenuItem`,
`wMenuWatchedKeys`, `wListScrollOffset`, `wPartyAndBillsPCSavedMenuItem`,
`wBagSavedMenuItem`, `wBattleAndStartSavedMenuItem`, `wTextDest`,
`wDoNotWaitForButtonPressAfterDisplayingText`.

Battle/party: `wIsInBattle`, `wBattleType`, `wEngagedTrainerClass`,
`wEngagedTrainerSet`, `wMoveMenuType`, `wPlayerSelectedMove`,
`wEnemySelectedMove`, `wPlayerMonNumber`, `wActionResultOrTookBattleTurn`,
`wPartyCount`, `wPartyMons`.

Progress/bag: `wObtainedBadges`, `wPlayerID`, `wPlayerMoney`,
`wPlayTimeHours`, `wPlayTimeMaxed`, `wPlayTimeMinutes`, `wPlayTimeSeconds`,
`wEventFlags`, `wNumBagItems`, `wBagItems`, `wRepelRemainingSteps`.

Hooks: `DisplayTextID` (bank 0, $2920), `YesNoChoice` (bank 0, $35ec),
`TryEvolvingMon` (bank 0x0e, $6d0e), `SetLastBlackoutMap` (bank 1, $7078).

Yellow symbol coverage will be audited against `pokeyellow.sym` before
adding it to this table.

## Performance floor (measured on dev machine)

- Headless throughput: **~13,400 ticks/sec** (render=False, window="null").
- Game Boy native rate: 59.7 fps → ≈ 224× real-time on this machine.
- ADR floor was ≥ 30× real-time; we comfortably clear it.

## Startup assertion

`Session.from_files(..., expected_rom_sha1=...)` hashes the provided ROM
and raises `VersionMismatch` on any deviation from the pins above.
