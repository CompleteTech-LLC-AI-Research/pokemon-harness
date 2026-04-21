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

### Pokémon Yellow — end-to-end supported (2026-04-19)

Yellow is a native Game Boy Color cartridge (`.gbc`) built from the
separate [`pret/pokeyellow`](https://github.com/pret/pokeyellow) repo.
Full intro → rival battle → Pallet → Viridian → Forest → Pewter →
Brock pipeline lives at `scripts/yellow_to_brock.py`.

| Field | Value |
|---|---|
| SHA-1 | `cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1` |
| Size | 1,048,576 bytes (1 MiB) |
| Path | `rom/yellow/pokemon-yellow.gbc` |
| Release | Yellow UE v1.0 |
| `.sym` | `rom/yellow/pokemon-yellow.sym` (built from `pret/pokeyellow`, 24,470 lines) |
| Color patch | Not needed — native CGB cartridge already has per-sprite palettes |

**Yellow symbol audit (2026-04-19):** All 52 WRAM symbols the harness
reads are present in `pokeyellow.sym` under unchanged names. Yellow
shifts most WRAM addresses down by 1 byte (extra Pikachu-follower
state inserted early in the block) — e.g. `wCurMap` is at `0xd35d` on
Yellow vs `0xd35e` on Red/Blue. Since our readers are name-driven via
`symbols.addr_of(...)`, no parser code changes are required. All
hook labels (`DisplayTextID`, `YesNoChoice`, `TryEvolvingMon`,
`SetLastBlackoutMap`) resolve on Yellow too (at Yellow-specific
offsets, handled identically).

**Build notes:** The dev machine currently uses
`ezwinports.make` and `MartinStorsjo.LLVM-MinGW.UCRT` (both winget
packages, user-scope) for the `pret/pokeyellow` build, with a
`clang.exe` → `gcc.exe` copy in the LLVM bin dir because pokeyellow's
`tools/Makefile` hardcodes `gcc`. Built ROM SHA-1 matches stock Yellow
byte-for-byte.

### Local dev-machine paths (not committed)

For the laptop that authored this repo:

- Red ROM: `G:\project\pokemon\PokemonRed.gb`
- Red symbol file: `G:\project\pokemon\PokemonRed.sym` (rgblink-generated, 21,137 lines, ~20,213 harness-loadable symbols)
- Vendored pokered source: `G:\project\pokemon\_vendor\pokered\`
  - Produces `pokered.sym`, `pokeblue.sym`, `pokeblue_debug.sym`.

- Vendored pokeyellow source: `G:\project\pokemon\_vendor\pokeyellow\` (cloned 2026-04-19, depth 1).

These live in a parallel workspace on a different drive and are **not**
part of this repo — record them in your personal `.env` or a local-only
config file, never in a committed file.

## pret upstream (for symbol generation)

| Field | Value |
|---|---|
| pret/pokered | `https://github.com/pret/pokered` — produces both `pokered.sym` (Red) and `pokeblue.sym` (Blue). Commit SHA: `<FILL-IN from: git -C G:/project/pokemon/_vendor/pokered rev-parse HEAD>` |
| pret/pokeyellow | `https://github.com/pret/pokeyellow` — Yellow only. Vendored at `G:\project\pokemon\_vendor\pokeyellow\`. Commit SHA: `bfa7170107eea23b89febb60bfb2ce39173bf2e1` (fetched 2026-04-19, `--depth=1`). |
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

Yellow symbol coverage: **all 52 WRAM symbols + 4 hook labels present
in `pokeyellow.sym`** under unchanged names (verified 2026-04-19).
Addresses differ from Red/Blue but the name-based resolver masks that.

## Performance floor (measured on dev machine)

- Headless throughput: **~13,400 ticks/sec** (render=False, window="null").
- Game Boy native rate: 59.7 fps → ≈ 224× real-time on this machine.
- ADR floor was ≥ 30× real-time; we comfortably clear it.

## Startup assertion

`Session.from_files(..., expected_rom_sha1=...)` hashes the provided ROM
and raises `VersionMismatch` on any deviation from the pins above.

## Link-cable symbol coverage

**Status: unverified — pending real-ROM validation.** The labels below
were taken from the pret community sources (`home/serial.asm` and
`engine/link/*.asm`) and are *believed* stable across Red/Blue/Yellow,
but none have been confirmed against actual `.sym` files produced by
`make DEBUG=1` on each version. When validating: any label that turns
out to be renamed or missing on a given ROM should be patched in
`src/pokered_harness/link/symbols.py` via the entry's `per_version` map.

### BRIDGE (bridge mutates memory when hooked)

| Key | Label | Required | Expected on |
|---|---|---|---|
| `exchange_bytes` | `Serial_ExchangeBytes` | yes | red, blue, yellow |
| `send_zero_byte` | `Serial_SendZeroByte` | no | red, blue, yellow |
| `exchange_nybble` | `Serial_SyncAndExchangeNybble` | no | red, blue, yellow |

### HANDSHAKE (bridge short-circuits to "linked")

| Key | Label | Required | Expected on |
|---|---|---|---|
| `handshake` | `Serial_TryEstablishingLink` | yes | red, blue, yellow |
| `cable_club_return` | `CableClub_DoBattleOrTradeAgain` | no | red, blue, yellow |

### PROGRESS (fires as event, no memory mutation)

| Key | Label | Required | Expected on |
|---|---|---|---|
| `trade_select_mon` | `TradeCenter_SelectMon` | no | red, blue, yellow |
| `trade_load_data` | `LoadTradingData` | no | red, blue, yellow |
| `trade_show_player_mon` | `Trade_ShowPlayerMon` | no | red, blue, yellow |
| `trade_show_enemy_mon` | `Trade_ShowEnemyMon` | no | red, blue, yellow |
| `print_trainer_info` | `PrintTrainerInfo` | no | red, blue, yellow |

### BATTLE (reserved — link-battle milestone, not wired yet)

| Key | Label | Required | Expected on |
|---|---|---|---|
| `link_battle_versus` | `LinkBattleVersusTextString` | no | red, blue, yellow |

### HRAM labels (required on all ROMs)

| Label | Purpose |
|---|---|
| `hSerialSendData` | Outgoing byte cell |
| `hSerialReceiveData` | Incoming byte cell |
| `hSerialConnectionStatus` | Connection status cell (bridge writes 0x01) |

