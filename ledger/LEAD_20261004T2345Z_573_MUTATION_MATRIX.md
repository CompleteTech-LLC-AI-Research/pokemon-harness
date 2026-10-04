# 2026-10-04 ~23:45Z — three-way mutation matrix against #573 head `b21c14a7`

## Why
The single most important property of #573 is that the coverage rows are not vacuous. A row that
cannot fail is worse than no row, because it converts a missing gate into a recorded pass. The
branch claims one mutation (dropping `scripts` turns 5-6 rows red). The lead ran three, escalating
from the obvious to the adversarial, since a reviewer had not yet reported.

All mutations were made in `/home/agent/wt573r` (read-only review worktree), never committed, and
restored via `cp` from a pre-mutation backup. Final state verified clean (`git diff` empty) with
the suite green at 50 passed.

## Mutation 1 — drop `scripts` from the check lane (the claimed baseline)
`python -m ruff check scripts tests` -> `python -m ruff check tests`
Result: **6 rows red**, including all three that `360f3add` rewrote to use `_lane_covers()`:
```
test_main_ruff_lanes_cover_every_script_file
test_runner_ruff_file_lists_match_the_workflow_exactly
test_matrix_benchmark_is_linted_by_every_main_ruff_lane
test_main_ruff_lanes_run_in_executable_control_flow
test_main_ruff_lanes_stop_the_local_runner_on_failure
test_probe_module_and_its_tier_are_part_of_the_ci_contract
```
Confirms the claim on this rebased head.

## Mutation 2 — point the lane at a non-existent path (does it catch a lane that covers nothing?)
`python -m ruff check scripts/does_not_exist.py tests`
Result: **6 rows red**, the same set. A lane naming a path Ruff cannot resolve does not
false-green.

## Mutation 3 — the adversarial case: a real path that covers almost nothing
This is the one a literal-matching test would pass and a real coverage test should fail.
`python -m ruff check scripts/production_gate_model.py tests` — one existing script out of 100+.
Result: **5 rows red**, and the failure is explicit rather than a silent shrink:
```
AssertionError: runner `ruff check` lane must pass the `scripts` directory so a new script
cannot escape the gate; got: ('python','-m','ruff','check','scripts/production_gate_model.py','tests')
  tests/test_local_ci_policy.py:262

AssertionError: runner `ruff check` lane does not cover scripts/benchmark_matrix_concurrency.py;
the benchmark would ship unlinted
  assert False
  +  where False = _lane_covers(('python','-m','ruff','check',
       'scripts/production_gate_model.py','tests'), 'scripts/benchmark_matrix_concurrency.py')
  tests/test_local_ci_policy.py:1060
```
Two independent mechanisms fire: a structural row (`"scripts" in lane`) and the behavioral row
(`_lane_covers` asking Ruff and getting False). This is the property that makes the directory token
safe: shrinking the boundary cannot pass quietly.

## Verdict
`_lane_covers()` has **no false-green path found** across three escalating mutations. The replaced
rows are not weakened; they fail on removal, on a nonsense path, and on a real-but-insufficient path.

## Environment note
Two runs returned pytest `exit=120` with an empty output file when both suites were redirected to
a file under `/tmp`. Re-running the identical command without redirection gave
`50 passed, exit=0`, and the single policy file alone gave `24 passed, exit=0`. Treated as a
redirect/environment artifact, not a test result; no conclusion rests on the 120 runs. Worth
re-checking if it recurs, since a silent empty output could otherwise be misread as a pass.
