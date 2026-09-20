# PR #113 real-ROM status-move and nonterminal-knockout evidence

Commit: `a86eb6248cec83af25d6f3519049bf2b0a032f8c`  Branch: `work/battle-state-88`

Sanitized from the `MCP_BATTLE_BOUNDARY` payloads, the pytest skip records, and
the junit summary of each of the six cells below. Six cells, **3 passed,
9 skipped, 0 failed, 0 errored**. The skips are recorded as the honest
acceptance result: they are fixture-availability limits, not weakened bounds,
and they are listed here rather than hidden behind a green aggregate.

These are the two regressions demanded by round 13 of review:

```
tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_status_move_returns_to_command_selection
tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_nonterminal_knockout_keeps_valid_replacement
```

| runtime | game | status move | nonterminal knockout | cell time |
|---|---|---|---|---|
| native | red_color | SKIP (fixture exposes no move 45) | SKIP (no live party menu) | 10m 25s |
| native | blue_color | **PASS** | SKIP (no live party menu) | 10m 19s |
| native | yellow | **PASS** | SKIP (no live party menu) | 4m 36s |
| source | red_color | SKIP (fixture exposes no move 45) | SKIP (no live party menu) | 38m 01s |
| source | blue_color | **PASS** | SKIP (no live party menu) | 40m 59s |
| source | yellow | **PASS** | SKIP (no live party menu) | 19m 49s |

## What passed

`status_move` settles one real shared Growl turn on the admitted
`<game>/cable_club-pre-terminal.state` pair with public input only
(`press`/`link_peer_press`/`link_step`, every decision taken from a
`pokered://game-state` read) and requires the ROM's own return boundary:
the selected move, the PP decrement on that slot, unchanged HP, the lowered
attack stage, a live command menu, `ACTION_RESOLUTION` observed while the move
executed, and a valid `COMMAND_SELECTION` phase with the bracket closed.

- source blue_color: fixture `blue/cable_club-pre-terminal.state`
  sha1 `7a2ca6a8ed2a6d0e0a5cbd2dbd62d5bb4a4a1cb5`-pinned pair, drive 1592 frames,
  `phases_seen [2, 3]`, slots `[1, 1]`.
- source yellow: fixture `yellow/cable_club-pre-terminal.state`,
  drive 636 frames, `phases_seen [2, 3]`, slots `[1, 1]`.
- native blue_color and native yellow reproduce the same assertions on the
  compiled runtime.

## Why each skip is honest

- **status move, red_color (both runtimes):** the admitted Red pair exposes no
  move 45 at any slot (`[[22, 33, 76, 0], [22, 33, 76, 0]]`), so the test skips
  with that observed move list instead of claiming coverage. Blue and Yellow
  expose Growl at the same slot on both owners and both pass.
- **nonterminal knockout (all six cells):** the admitted pre-terminal pair never
  opened a live battle party menu within the 10,000-paired-frame drive budget.
  The skip record carries the two observed boundary states, each reporting
  `raw_is_in_battle 2`, `player_mon_slot 5`, and `phase 6` (`UNKNOWN`) with
  `phase_valid false`. The `wPartyMenuTypeOrMessageID` /
  `wPartyMenuAnimMonEnabled` symbols appear in `phase_evidence`, so the party-menu
  predicate the round-12 fix introduced was sampled on every frame; it simply
  never became true. The fixture's `terminal_turn_plan` ends the battle on the
  *opponent's* faint, which does not raise `ChooseNextMon` for the owner being
  driven, so no replacement menu exists to observe on this fixture.

That second gap is a real limitation of this bundle, not a pass. The owner-side
knockout scenario is exercised by `tests/test_mcp_battle_phase_rom.py`'s other
rows and by the round-12 forced-replacement boundary evidence
(`release-evidence/feature-qualification/pr113-boundary-3f0f971/`), but this
pair of round-13 regressions, at this head, has **no passing knockout cell**.

## Runtime labels are probed, not assumed

Each cell ran under one interpreter with one `PYTHONPATH` shape. The same shape
resolves to different modules:

```
native: <native-venv>/lib/python3.11/site-packages/pyboy/__init__.cpython-311-x86_64-linux-gnu.so
        <native-venv>/lib/python3.11/site-packages/pyboy/core/serial.cpython-311-x86_64-linux-gnu.so
source: vendor/pyboy-src/pyboy/__init__.py
        vendor/pyboy-src/pyboy/core/serial.py
```

The native cells also run 4x faster on the same drive (native yellow 4m 36s vs
source yellow 19m 49s), which independently corroborates that the two rows used
different runtimes. The probe commands are recorded in
`acceptance-evidence.json` under `runtime_origin_probe`.

## Reproduction

```bash
cd <checkout>
POKERED_ROM_ROOT=<rom-root> POKERED_FIXTURE_ROOT=<fixture-root> \
PYTHONPATH="$PWD/src:$PWD" <native-interpreter> -m pytest -q -o addopts= \
  "tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_status_move_returns_to_command_selection" \
  "tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_nonterminal_knockout_keeps_valid_replacement"

PYBOY_NO_CYTHON=1 POKERED_ROM_ROOT=<rom-root> POKERED_FIXTURE_ROOT=<fixture-root> \
PYTHONPATH="$PWD/vendor/pyboy-src:$PWD/src:$PWD" <source-interpreter> -m pytest -q -o addopts= \
  "tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_status_move_returns_to_command_selection" \
  "tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_nonterminal_knockout_keeps_valid_replacement"
```

Both tests are parametrized over `red_color`, `blue_color`, and `yellow`, so the
two commands cover all six cells. Do not set `POKERED_SKIP_SHA1`: both tests fail
closed on it, and the fixtures are byte-validated against the manifest before
use. Expect roughly 75-165 minutes per command on this host.
