# PR #113 real-ROM MCP stdio battle-state acceptance evidence

Commit: `63615e139f5a8c712bd2e2680d4780aaa0e87bc0`  Branch: `work/battle-state-88`

This bundle supersedes `pr113-acceptance-edab6d0` and
`pr113-acceptance-e73c8ce`, whose `native` rows ran vendored source PyBoy
because the stdio child unconditionally prepended `vendor/pyboy-src` to
`sys.path`.  **Every child process in this run asserted its own runtime**: it
resolved `pyboy` and `pyboy.core.serial` and required a compiled `.so` outside
the checkout for `compiled`, or a `.py` inside `vendor/pyboy-src` for `source`.
A mislabelled run fails the test instead of publishing mislabelled evidence.

| runtime | game | result | settlement pp before -> after | duration |
|---|---|---|---|---|
| native | blue_color | PASS | [[10, 21, 10, 7], [10, 21, 10, 7]] -> [[9, 21, 10, 7], [9, 21, 10, 7]] | 170.774s |
| native | red_color | PASS | [[2, 32, 7, 0], [2, 32, 7, 0]] -> [[1, 32, 7, 0], [1, 32, 7, 0]] | 124.971s |
| native | yellow | PASS | [[12, 40, 30, 30], [12, 40, 30, 30]] -> [[11, 40, 30, 30], [11, 40, 30, 30]] | 76.746s |
| source | blue_color | FAIL | None -> None | Nones |
| source | red_color | FAIL | None -> None | Nones |
| source | yellow | FAIL | None -> None | Nones |

Each case drives a real Cable Club link battle through the public MCP stdio
server and reads `pokered://game-state` for both owners.  Covered: paired
enemy/phase observations, `COMMAND_SELECTION` from the observational menu hooks
*while still paired*, one settled move (PP decrement + damage + return to the
command boundary), an outstanding `link_step` cancelled client-side with the
server remaining responsive, and the documented fail-closed terminal contract
(`terminal_result` stays `null` while the battle is live; no fabricated win).

Runtime origin proof for this host: the compiled interpreter resolves
`pyboy/__init__.cpython-311-x86_64-linux-gnu.so` under the venv
`site-packages`, and the source interpreter with `vendor/pyboy-src` on the path
resolves `vendor/pyboy-src/pyboy/__init__.py` inside this checkout.  Feeding the
compiled interpreter the `source` label raises the child's own assertion.
