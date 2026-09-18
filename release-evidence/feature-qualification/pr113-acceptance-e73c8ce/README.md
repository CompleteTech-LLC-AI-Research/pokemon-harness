# PR #113 real-ROM MCP stdio battle-state acceptance evidence

Commit: `e73c8ce879062968ed9d2478ec69f57a933153ba`  Branch: `work/battle-state-88`

> **SUPERSEDED - the `native` label in this bundle is wrong.** At this commit
> the stdio child unconditionally prepended `vendor/pyboy-src` to `sys.path`,
> so the `native` rows ran the vendored source PyBoy exactly like the `source`
> rows. Nothing here proves a compiled-PyBoy run. The runtime-origin-verified
> re-run at a later head is
> `release-evidence/feature-qualification/pr113-acceptance-native-<tag>/`, where
> each child asserts its own `pyboy`/`pyboy.core.serial` module origins against
> `POKERED_EXPECT_PYBOY_KIND`. This directory is retained only as the
> historical record of the pre-fix run.

| runtime | game | result | selected phase_valid | menu_open |
|---|---|---|---|---|
| native | blue_color | PASS | True | True |
| native | red_color | PASS | True | True |
| native | yellow | PASS | True | True |
| source | blue_color | PASS | True | True |
| source | red_color | PASS | True | True |
| source | yellow | PASS | True | True |

Each case drives a real Cable Club link battle through the public MCP stdio server and reads `pokered://game-state` (primary and peer). Observed across all six runtime/game runs: battle `kind=2`, `enemy_mon_valid=true`, `raw_battle_result=0`, `terminal_result=null`, and a fail-closed phase during settlement; after re-entering the move menu, `menu_open=true`, `phase=2` (COMMAND_SELECTION), `phase_valid=true`.
