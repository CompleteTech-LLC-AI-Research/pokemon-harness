# PR #113 real-ROM Cable Club boundary and terminal evidence

Commit: `63615e139f5a8c712bd2e2680d4780aaa0e87bc0`  Branch: `work/battle-state-88`

> **SUPERSEDED by `pr113-boundary-3f0f971`.** This run is still an accurate
> record of `63615e1`, but round 11 of review then hardened the epoch, stdio,
> and provenance paths. The current evidence for this branch is
> `release-evidence/feature-qualification/pr113-boundary-3f0f971/`.

Sanitized from the `MCP_BATTLE_BOUNDARY` payloads printed by
`tests/test_mcp_battle_phase_rom.py`, plus the junit summary of each cell.
Twelve cells, twelve passes, zero skips, zero failures.

## Runtime labels are proven, not assumed

Round 9 of review found that the earlier `pr113-boundary-dd20006` matrix
labelled all twelve cells "native" while every cell actually ran **source**
PyBoy: the source runtime prepends `vendor/pyboy-src` to `sys.path`, and that
entry silently won for the Cython interpreter too. That bundle's native label
is wrong and it is superseded by this one.

This matrix pins the label to the interpreter's own imports. Each child process
asserts the origin of its `pyboy` and `pyboy.core.serial` modules against
`POKERED_EXPECT_PYBOY_KIND`: the compiled cells must resolve to a `.so` outside
the checkout and the source cells to a `.py` inside `vendor/pyboy-src`, so a
mislabelled run fails instead of publishing. The compiled cells here run
roughly 4x faster than the source cells on the identical drive
(red 57.96s vs 264.56s), which independently corroborates that the two rows
really did use different runtimes.

| runtime | game | boundary (junit) | terminal drive (junit) | drive frames |
|---|---|---|---|---|
| native | red_color | PASS 1.61s | PASS 59.99s | 1216 |
| native | yellow | PASS 1.91s | PASS 63.50s | 2576 |
| native | blue_color | PASS 1.69s | PASS 77.98s | 1592 |
| source | red_color | PASS 1.65s | PASS 269.06s | 1216 |
| source | yellow | PASS 1.82s | PASS 317.50s | 2576 |
| source | blue_color | PASS 1.63s | PASS 442.86s | 1592 |

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
  each owner reports phase `0` with `terminal_result` back to `null` - the
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
