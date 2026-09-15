# PR #113 real-ROM MCP stdio battle-state acceptance evidence

Commit: `edab6d0192d277f12457992c96faf4e614629b86`  Branch: `work/battle-state-88`

| runtime | game | result | settlement pp before->after | cancelled request |
|---|---|---|---|---|
| native | blue_color | PASS | [[10, 21, 10, 7], [10, 21, 10, 7]] -> [[9, 21, 10, 7], [9, 21, 10, 7]] | bounded |
| native | red_color | PASS | [[2, 32, 7, 0], [2, 32, 7, 0]] -> [[1, 32, 7, 0], [1, 32, 7, 0]] | bounded |
| native | yellow | PASS | [[12, 40, 30, 30], [12, 40, 30, 30]] -> [[11, 40, 30, 30], [11, 40, 30, 30]] | bounded |
| source | blue_color | PASS | [[10, 21, 10, 7], [10, 21, 10, 7]] -> [[9, 21, 10, 7], [9, 21, 10, 7]] | bounded |
| source | red_color | PASS | [[2, 32, 7, 0], [2, 32, 7, 0]] -> [[1, 32, 7, 0], [1, 32, 7, 0]] | bounded |
| source | yellow | PASS | [[12, 40, 30, 30], [12, 40, 30, 30]] -> [[11, 40, 30, 30], [11, 40, 30, 30]] | bounded |

Each case drives a real Cable Club link battle through the public MCP stdio server and reads `pokered://game-state` for both owners. Covered: paired enemy/phase observations, `COMMAND_SELECTION` from the observational menu hooks *while still paired*, one settled move (PP decrement + damage + return to the command boundary), an outstanding `link_step` cancelled client-side with the server remaining responsive, and the documented fail-closed terminal contract (`terminal_result` stays `null`; no fabricated win). Forced replacement and terminal return are not reachable from these fixtures within a reasonable bound and are asserted as fail-closed, not simulated.
