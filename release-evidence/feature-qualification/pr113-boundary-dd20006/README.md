# PR #113 real-ROM Cable Club boundary and terminal evidence

Commit: `dd2000624a8913e59e56c30d6cc78570e867afdc`  Branch: `work/battle-state-88`

> **SUPERSEDED - the `native` label in this bundle is wrong.** Round 9 of
> review proved that all twelve cells below ran **source** PyBoy: the source
> runtime prepends `vendor/pyboy-src` to `sys.path` and that entry won for the
> Cython interpreter too, so the two rows are the same runtime. The
> `native`/`source` split and the `native` durations in this table must not be
> read as evidence of a compiled-PyBoy run. The re-run at `925a38a`, whose
> labels are asserted by each child process against its own module origins, is
> `release-evidence/feature-qualification/pr113-boundary-925a38a/`. This
> directory is retained only as the historical record of the pre-fix run.

Sanitized from the `MCP_BATTLE_BOUNDARY` payloads printed by
`tests/test_mcp_battle_phase_rom.py`, plus the junit summary of each cell. The
source matrix ran under the source interpreter (`PYBOY_NO_CYTHON=1`) and the
native matrix under the Cython interpreter, both at the pinned PyBoy
`2.7.0` / revision `c565df66c3731fad2856169a90f6bbec99925915`. Twelve cells,
twelve passes, zero skips, zero failures.

| runtime | game | boundary (junit) | terminal drive (junit) | drive frames |
|---|---|---|---|---|
| native | red_color | PASS 1.67s | PASS 259.33s | 1216 |
| native | yellow | PASS 1.90s | PASS 271.82s | 2576 |
| native | blue_color | PASS 1.55s | PASS 336.52s | 1592 |
| source | red_color | PASS 2.49s | PASS 259.45s | 1216 |
| source | yellow | PASS 2.05s | PASS 271.26s | 2576 |
| source | blue_color | PASS 1.67s | PASS 346.60s | 1592 |

## What each cell observes

All three boundaries are admitted immutable fixtures loaded through the public
MCP stdio `load_state` tool, so every read below is a resource read of a real
link battle rather than a synthetic memory double.

- `forced_replacement` (`<game>/cable_club-battle-faint.state`): the owner's
  own combatant is at zero HP with living replacements left
  (`party_hp [0,0,0,0,0,148]` red, `[0,0,0,0,0,152]` blue/yellow),
  `wInHandlePlayerMonFainted` is set, `player_mon_slot` is 4, and the derived
  phase is `4` with `phase_valid=true` on the fainted owner while the peer
  remains in the live battle. `terminal_result` stays `null`: a load cannot
  fabricate a battle end.
- `terminal_loaded` (`<game>/cable_club-terminal.state`): both owners report
  `wIsInBattle == 0` with a surviving non-zero `wBattleResult` on the owner
  whose combatant fainted (`raw_results [0,1]` red, `[1,0]` blue), yet
  `terminal_result` remains `null` and the phase is the plain `0`
  `INACTIVE`. This is the documented fail-closed contract: promotion requires
  an observed battle end in this session.
- `terminal_drive` (`<game>/cable_club-pre-terminal.state`): the last ROM
  command boundary before the deciding knockout is *driven* through the ROM
  turns with `press`/`link_peer_press`/`link_step` only, every decision taken
  from a `pokered://game-state` read. Both owners' first post-battle read
  reports phase `5` `TERMINAL_RETURN` with `phase_valid=true` and the
  `wIsInBattle` + `wBattleResult` evidence; the owner whose surviving
  `wBattleResult` byte is non-zero also gets it promoted to `terminal_result`
  (`1` in every cell), while the owner reading a zero byte correctly keeps
  `terminal_result = null` rather than fabricating a win. The next read of
  each owner reports phase `0` with `terminal_result` back to `null` — the
  documented single-shot falling edge. Bounded drive: 1216 frames red (boundary
  turn 36), 2576 yellow (turn 20), 1592 blue (turn 44).

## Reproduction

```bash
cd <checkout>
POKERED_ROM_ROOT=<rom-root> POKERED_FIXTURE_ROOT=<fixture-root> \
PYTHONPATH="$PWD/src:$PWD" <interpreter> -m pytest -q -o addopts= \
  "tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_boundary_reads_fail_closed" \
  "tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_terminal_return_drive_reads"
```

Run it once with the source interpreter (`PYBOY_NO_CYTHON=1`, source runtime)
and once with the Cython interpreter. Both tests are parametrized over
`red_color`, `yellow`, and `blue_color`, so the four commands cover all twelve
cells. Do not set `POKERED_SKIP_SHA1`: both tests fail closed on it, and the
fixtures are byte-validated against the manifest before use.
