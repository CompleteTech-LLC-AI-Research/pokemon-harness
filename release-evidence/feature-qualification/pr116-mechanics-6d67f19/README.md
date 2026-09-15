# PR #116 mechanics acceptance evidence (issue #89)

Reviewed head: `6d67f19d93abbe93a8147cafa810f437c312a706`
Branch: `work/coverage-89`
Runtime pin: PyBoy `2.7.0`, fork revision `c565df66c3731fad2856169a90f6bbec99925915`

## What this proves

The first `expanded_mechanics` family (`effect_id: 0`, `NO_ADDITIONAL_EFFECT`)
transitions from `planned_unverified` to `tested` only on terminal, dual-runtime,
real-ROM evidence for the declared strict selector
`tests/test_pyboy_link_session_roms.py::test_pair_completes_battle_turn[red-red]`.

- `coverage-evidence.json` — sanitized normalised evidence produced from the two
  real pytest JUnit runs plus the verified production-gate asset hashes
  (ROM/SYM/fixture SHA-1s, no ROM bytes, no absolute local paths).
- `coverage-report.txt` — the resulting coverage report:
  `expanded_mechanics: PARTIAL [families=87 tested=1 planned_unverified=67 deliberately_excluded=19]`.

Running the same report with `--no-results` reports `tested=0` for the family, so
the catalog declaration alone is never counted as execution evidence.

## How the evidence was produced

Both runtimes were executed on the operator assets
(`ROM_ROOT=/home/agent/.../private-game-states/rom`,
`FIXTURE_ROOT=/home/agent/.../private-game-states/tests/fixtures/link`, manifest
byte validation PASS, 10 entries) inside an unprivileged writable-`/dev/shm`
mount namespace (`unshare --map-root-user -m`).

```sh
# source runtime
PYBOY_NO_CYTHON=1 PYTHONPATH="$PWD/vendor/pyboy-src:$PWD/src:$PWD" \
  "$SOURCE_PYTHON" -m pytest -q -rA -p pytest_asyncio.plugin --strict-config --strict-markers \
  -o junit_logging=out-err --junitxml=junit/source-battle.xml \
  "tests/test_pyboy_link_session_roms.py::test_pair_completes_battle_turn[red-red]"

# cython runtime
PYTHONPATH="$PWD/src:$PWD" "$NATIVE_PYTHON" -m pytest -q -rA -p pytest_asyncio.plugin \
  --strict-config --strict-markers -o junit_logging=out-err \
  --junitxml=junit/cython-battle.xml \
  "tests/test_pyboy_link_session_roms.py::test_pair_completes_battle_turn[red-red]"
```

Observed terminal result (both runtimes): `PASSED`, one settled turn with
`local_move_effect = enemy_move_effect = 0`, move id 22, one PP decrement,
`outcome = settled` at boundary `MainInBattleLoop`.

The raw JUnit stdout is retained unredacted by the orchestrator; this bundle
carries the sanitized normalised form.
