# Lead sync record — #567 landed on master, branch reconciled

Recorded after an independent re-verification of live state. `origin/master`
had moved from `fb2d4894` to `eb7f14d8` while this check was running, so every
number below is re-measured against `eb7f14d8` rather than carried over.

## Outcome

- `origin/master` = `eb7f14d8`
- open PRs: **0**
- open issues: **30**
- #567: CLOSED. #573: CLOSED unmerged. #574: MERGED as `e815c6d2`.

The #567 scope did not land via the branch this session was working
(`fix/567-lane-residue-v2`). It landed via #574, which carried the same scope
plus three repair commits. #573 was closed as superseded, not merged.

## The defect this session found, and its disposition

The branch tip `c8943669` did **not** pass the lane the issue is about.
Commit `2dbf4487` had moved six module docstrings above
`from __future__ import annotations` so that `__doc__` would stop being `None` —
a correct repair — but it left a blank line after the `__future__` import in
each file, and that trips `I001`:

```
tests/_pyboy_link_session_roms_battle_support.py:7:1: I001
tests/_pyboy_link_session_roms_support.py:9:1: I001
tests/_pyboy_link_session_roms_trade_support.py:7:1: I001
tests/test_pyboy_link_session_roms.py:22:1: I001
tests/test_pyboy_link_session_roms_diagnostics.py:7:1: I001
tests/test_pyboy_link_session_roms_serial.py:7:1: I001
```

`tests/` is clean on `fb2d4894` and red on `c8943669`, so this was a regression
the branch introduced rather than inherited residue. Because `tests/` reaches the
lane as a directory, the coverage that #567 exists to create immediately
rejected the branch's own tree.

Fix committed as `0efdca9e`: Ruff's own isort output
(`ruff check --select I001 --fix`), one blank line deleted per file, six lines
total. Blank lines between import statements carry no semantics.

**This fix was already on master when checked.** All six files are byte-identical
between `0efdca9e` and `eb7f14d8` — #574 fixed the same residue independently.
The commit is therefore fully superseded and was not merged.

## `b9d0ee08` (local-only, never pushed)

Correctly abandoned. It added 44 `per-file-ignores` E402 entries plus 2 F811
entries, which #567's first acceptance row forbids in as many words ("with no
`per-file-ignores` added to achieve it"). Master carries **zero**
`per-file-ignores`; #574 fixed every finding at the source instead.

The premise behind that commit was also wrong, which is worth recording so it is
not re-derived. `E402` and `E731` are **not** in this repo's enabled rule set.
`pyproject.toml` has no `[tool.ruff.lint] select`, and the pinned `ruff==0.16.5`
does not enable either by default:

```
ruff check --show-settings scripts/production_gate.py    -> 0 occurrences of E402/E731 in linter.rules.enabled
ruff check scripts/production_gate.py                   -> All checks passed!
ruff check scripts/production_gate.py --select E402     -> Found 12 errors
```

The 12 findings are real; they are simply never reported, because the lane does
not enable the code. So the "113 E402" and "22 E731" residue counts came from an
explicit `--select` that the lane does not use, and both codes are inert here
regardless of any allowance. `tests/test_serial_owner_pump.py` produces **no**
F811 even under `--select F811`, so that allowance was guarding against nothing.

## Verification on `eb7f14d8` (project-pinned `ruff==0.16.5`)

```
ruff check scripts tests --no-cache            -> All checks passed!
ruff format --check scripts tests --no-cache  -> 448 files already formatted
pytest tests/test_local_ci_policy.py           -> 24 passed
```

Mutation proof for #567's auto-coverage row, on master: a deliberately
mis-formatted scratch script in `scripts/` was caught by the format lane
(`1 file would be reformatted`), then removed. Removing the `scripts` token from
the runner lane turns **5** policy rows red on the branch:
`test_main_ruff_lanes_cover_every_script_file`,
`test_runner_ruff_file_lists_match_the_workflow_exactly`,
`test_matrix_benchmark_is_linted_by_every_main_ruff_lane`,
`test_main_ruff_lanes_run_in_executable_control_flow`,
`test_main_ruff_lanes_stop_the_local_runner_on_failure`.

Both main lanes in `scripts/run_local_ci.sh` and
`.github/workflows/release-hygiene.yml` pass the `scripts` directory token; the
one remaining `scripts/...` enumeration is in the separate runtime/link lane.

## Regression sweep — no regression from this branch

Full unit tier, each tree with an interpreter bound to that checkout (the
`conftest.py` guard correctly refused a cross-checkout interpreter):

```
fb2d4894 (master at the time) : 35 failures
0efdca9e (branch head)        : 35 failures
```

Deterministic core is identical: 17 distinct failing test names match exactly.
Five names appeared in only one run each; those are load-dependent flakes, not
regressions — repeated runs pass and fail on **both** trees:

```
HEAD  run1 FAILED  run2 FAILED  run3 passed
master run1 passed run2 FAILED  run3 FAILED
```

The 35-row baseline is the pre-existing #253 failure set and is unrelated to
#567. `pytest tests --collect-only` collects 8548 tests cleanly.

## Note on #574's ruff exclusion config

Master's `pyproject.toml` is *more* correct than the branch's. It moves
`scripts/produce_battle_state_fixtures.py` into `extend-exclude` and drops
`force-exclude`, with the reason recorded in the file: `exclude` is not additive
and would replace Ruff's built-in defaults (pulling `.venv/` and `build/` into
directory-traversing lanes), and `force-exclude` would silently drop any future
lane that names the producer directly. Verified: `ruff check scripts
--show-files` resolves 113 files and the producer is not among them.

## Cleanup

- remote branch `fix/567-lane-residue-v2` deleted (fully superseded; PR #573 closed)
- worktrees `wt-sync-c894`, `wt-sync-master`, `wt-sync-verify`, `wt-newmaster` created
  for this verification and removed afterwards
- protected checkout `/home/agent/poke-harness/pokemon` untouched

## Remaining blockers (unchanged)

- **#489** — collaboration sub-agent delivery still unreliable; no independent
  review obtained in this session either.
- **#253** — 35 unit-tier failures, pre-existing, not addressed here.
- **#85/#86/#106** — no operator-controlled CPU allocation obtained.
- **#90–#105, #235** — need real ROM/symbol assets; none exist on this host.

Release status remains **PARTIAL**.
