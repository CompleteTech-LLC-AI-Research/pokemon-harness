# `lead/src-dir-lane` @ `adb6969` — full-suite result vs master

## Head and composition

```
adb6969  tests/: prove the probe is covered by resolving the scripts token
fdf4e0d  src/: replace the enumerated boundary and drop the runtime/link lane
753725e  Merge origin/fix/567-scripts-dir-lane into lead/src-dir-lane
0e34a2c  src/: make the product tree format-clean
11eb719  #567 step 2: scripts/ directory token        (PR #569 head)
1cc86f8  #567 step 1: scripts/ format-clean           (PR #568 head)
b5302d0  origin/master
```

Both open `#567` PR heads are ancestors of this head, so the lane work composes
with the stack rather than forking it:

```
git merge-base --is-ancestor 1cc86f8 HEAD -> 0   (true)
git merge-base --is-ancestor 11eb719 HEAD -> 0   (true)
```

Branch remains local and unpushed pending independent review.

## Gates

```
python -m ruff check scripts src tests          -> All checks passed!
python -m ruff format --check scripts src tests -> 513 files already formatted
pytest tests/test_stepping_loop_profile.py      -> 26 passed
pytest tests/test_local_ci_policy.py            -> 14 passed
git status --short                              -> clean (only untracked .scratch/)
```

## Full `tests/` suite against the master baseline

```
pytest tests/ -q -p no:randomly   ->  136 unique FAILED rows
master baseline (/tmp/master_full.log.fails) -> 140
```

Set difference, both directions:

```
comm -23 srcglob master   ->  (empty)     zero new failures
comm -13 srcglob master   ->  5 rows
```

The five rows that fail on master and pass here:

| row | class |
|---|---|
| `test_local_ci_policy.py::test_main_ruff_lane_tests_directory_covers_every_test_file_on_disk` | real fix — the collapsed `tests` token satisfies a row master cannot pass |
| `test_mcp_timed_remote_cached_failure.py::test_queued_cancel_preserves_active_real_epoch` | wall-clock flake |
| `test_mcp_timed_remote_cancellation.py::test_invalid_local_input_preserves_connected_epoch[invalid_button]` | wall-clock flake |
| `test_mcp_timed_remote_owner.py::test_real_partial_progress_active_interrupt_is_terminal[cancel]` | wall-clock flake |
| `test_mcp_timed_remote_owner.py::test_real_partial_progress_active_interrupt_is_terminal[deadline]` | wall-clock flake |

The remaining 136 are the known red source/timing gate (#253) and unrelated
`test_probe_timed_*` / `test_normal_red_link_timing` rows. None are attributable
to this branch.

## The one regression found and repaired

`tests/test_stepping_loop_profile.py::test_probe_module_and_its_tier_are_part_of_the_ci_contract`
counted literal occurrences of `scripts/stepping_loop_profile.py`. The directory
token supplies that coverage, so the literal count was zero while the guarantee
was still real — a row that could only ever pass for the wrong reason once the
boundary was collapsed.

`adb6969` replaces the count with actual resolution: ask Ruff which files the
`scripts` token expands to, assert the probe is among them, and assert both main
lanes in both CI files carry the token. Mutation proofs: adding the probe to
`extend-exclude` fails the row and names the exclusion; dropping the `scripts`
token from the runner lane fails the row and explains the coverage loss.

## Status

Release remains **PARTIAL**. Nothing merged, nothing marked ready, no issue
closed. `#565`, `#566`, `#568`, `#569` and this unpushed branch all await an
independent verdict on their exact heads; author self-verification does not meet
that bar.
