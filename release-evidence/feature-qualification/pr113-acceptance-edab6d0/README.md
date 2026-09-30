# PR #113 real-ROM MCP stdio battle-state acceptance evidence

Commit: `edab6d0192d277f12457992c96faf4e614629b86`  Branch: `work/battle-state-88`

> **SUPERSEDED - the `native` label in this bundle is wrong.** At this commit
> the stdio child unconditionally prepended `vendor/pyboy-src` to `sys.path`,
> so the `native` rows ran the vendored source PyBoy exactly like the `source`
> rows. Nothing here proves a compiled-PyBoy run. The runtime-origin-verified
> re-run at a later head is
> `release-evidence/feature-qualification/pr113-acceptance-native-3f0f971/`, where
> each child asserts its own `pyboy`/`pyboy.core.serial` module origins against
> `POKERED_EXPECT_PYBOY_KIND`. This directory is retained only as the
> historical record of the pre-fix run.

| runtime | game | result | settlement pp before->after | cancelled request |
|---|---|---|---|---|
| native | blue_color | PASS | [[10, 21, 10, 7], [10, 21, 10, 7]] -> [[9, 21, 10, 7], [9, 21, 10, 7]] | bounded |
| native | red_color | PASS | [[2, 32, 7, 0], [2, 32, 7, 0]] -> [[1, 32, 7, 0], [1, 32, 7, 0]] | bounded |
| native | yellow | PASS | [[12, 40, 30, 30], [12, 40, 30, 30]] -> [[11, 40, 30, 30], [11, 40, 30, 30]] | bounded |
| source | blue_color | PASS | [[10, 21, 10, 7], [10, 21, 10, 7]] -> [[9, 21, 10, 7], [9, 21, 10, 7]] | bounded |
| source | red_color | PASS | [[2, 32, 7, 0], [2, 32, 7, 0]] -> [[1, 32, 7, 0], [1, 32, 7, 0]] | bounded |
| source | yellow | PASS | [[12, 40, 30, 30], [12, 40, 30, 30]] -> [[11, 40, 30, 30], [11, 40, 30, 30]] | bounded |

Each case drives a real Cable Club link battle through the public MCP stdio server and reads `pokered://game-state` for both owners. Covered: paired enemy/phase observations, `COMMAND_SELECTION` from the observational menu hooks *while still paired*, one settled move (PP decrement + damage + return to the command boundary), an outstanding `link_step` cancelled client-side with the server remaining responsive, and the documented fail-closed terminal contract (`terminal_result` stays `null`; no fabricated win).

Status of the two boundaries this bundle could not reach: they are no longer
asserted as merely unreachable. Admitted immutable fixtures now capture the
forced-replacement boundary (`cable_club-battle-faint.state`), the loaded
terminal state (`cable_club-terminal.state`), and the last command boundary
before the deciding knockout (`cable_club-pre-terminal.state`) for red, blue,
and yellow. The loaded-boundary reads stay fail-closed by contract, while the
pre-terminal pair is *driven* through the ROM turns to `EndOfBattle`, where the
first post-battle read reports `TERMINAL_RETURN` with a promoted
`terminal_result` and the next read reports the plain `INACTIVE` phase (the
documented single-shot falling edge). That drive passes on both runtimes for
all three families; see the boundary evidence bundles recorded against later
heads.
