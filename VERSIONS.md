# Pinned versions

This file is the single source of truth for every version the harness depends
on. The session manager reads the expected values from here (currently by
caller code; a loader is planned) and refuses to run if the loaded ROM or
PyBoy version does not match.

## Emulator

| Component | Pin | Notes |
|---|---|---|
| PyBoy | `2.7.0` | v2 API — `window="null"`, `tick(n, render=...)`, `memory[...]`, `hook_register`, `symbol_lookup`. |

## Target ROM

**Pokémon Red (UE) — "POKEMON RED"** (internal cartridge title).

| Field | Value |
|---|---|
| SHA-1 | `ea9bcae617fdf159b045185467ae58b2e4a48b9a` |
| Size | 1,048,576 bytes (1 MiB) |
| Internal title (ROM header) | `POKEMON RED` |
| Release | Red UE v1.0 (canonical mainline) |

> BYO-ROM. This repo ships neither the ROM nor any build-derived artifact.

### Local dev-machine path (not committed)

For the dev laptop that authored this repo:

- ROM: `G:\project\pokemon\PokemonRed.gb`
- Symbol file: `G:\project\pokemon\PokemonRed.sym` (rgblink-generated,
  21,137 lines, ~20,213 harness-loadable symbols)
- Vendored pokered source: `G:\project\pokemon\_vendor\pokered\`

These paths live in a parallel workspace on a different drive and are
**not** part of this repo — record them in your personal `.env` or a
local-only config file, never in a committed file.

## pret/pokered (for symbol generation)

| Field | Value |
|---|---|
| Repository | `https://github.com/pret/pokered` |
| Commit SHA | `<FILL-IN from: git -C G:/project/pokemon/_vendor/pokered rev-parse HEAD>` |
| Build flag | `make DEBUG=1` (produces `pokered.sym` and `pokered.map`) |
| Toolchain | rgbds — bundled at `_vendor/rgbds-1.0.1-src/` on the dev machine. |

The generated `pokered.sym` (or its `PokemonRed.sym` alias) is **not**
committed to this repo.

## Symbol coverage (verified end-to-end)

Every symbol the v1 harness reads or plans to hook has been confirmed
present in the generated `.sym`:

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
`wEventFlags`, `wNumBagItems`, `wBagItems`.

Hooks: `DisplayTextID` (bank 0, $2920), `YesNoChoice` (bank 0, $35ec),
`TryEvolvingMon` (bank 0x0e, $6d0e), `SetLastBlackoutMap` (bank 1, $7078).

## Performance floor (measured on dev machine)

- Headless throughput: **~13,400 ticks/sec** (render=False, window="null").
- Game Boy native rate: 59.7 fps → ≈ 224× real-time on this machine.
- ADR floor was ≥ 30× real-time; we comfortably clear it.

## Startup assertion

`Session.from_files(..., expected_rom_sha1=...)` hashes the provided ROM
and raises `VersionMismatch` on any deviation from the pin above.
