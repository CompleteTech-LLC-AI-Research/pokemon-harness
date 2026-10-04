# 2026-10-04 ~22:30Z — independent verification of PR #573 head `30f4a933`

## Why the lead did this directly
Two dispatched independent reviewers (`indep573_review`, `indep572_audit`) each misfired twice:
round 1 answered the Codex-settings prompt, round 2 either re-answered it or asked the lead
clarifying questions instead of executing the audit. `indep573_review` did contribute one real
observation before stalling: **PR #573 is `DIRTY` against current master.**
Author self-verification does not meet the contract's bar, so the lead re-derived the checks
by hand from the exact head tree. All numbers below were re-measured this session.

## Head under review
- PR #573, head `30f4a93369a81c14e2201af87e4849c26e2f1a8d`
- Stack: `fd91a1b` (step 1), `ffc680d` + `4680f80` (step 2), `8975846` (step 3a), `30f4a93` (step 3b)
- Read-only worktree: `/home/agent/wt573a` (branch `review/573-head`), left clean.
- Master baseline worktree: `/home/agent/wt567m` (detached at `fb2d4894`), left clean.

## BLOCKING: #573 is unmergeable as it stands
```
gh api .../pulls/573 -> head=30f4a933 base=a413eeb5 mergeable=false state=dirty
git merge-tree --write-tree origin/master 30f4a933
  -> CONFLICT (content): Merge conflict in tests/test_local_ci_policy.py  (3 hunks)
```
Master is now `fb2d4894`; #573 was cut against `a413eeb5`. Both sides edit the same file in
opposite directions: #573's step 2 removes an enumerated 51-entry `scripts/` list
(-542 lines net in that file) while #572 added +503 lines of #570 suppression-guard tests.
A non-destructive rebase is required, and the merged result must be re-reviewed because the
conflict resolution changes the file that carries the coverage rows.
**No merge, no ready flag, no issue closure until that rebase lands and is re-reviewed.**

## VERIFIED CORRECT: step 3a (8975846) restores the six docstrings
Six files were named. All six confirmed `__doc__` non-empty via `ast.get_docstring` on `30f4a933`:
```
OK tests/_pyboy_link_session_roms_support.py          len=362
OK tests/_pyboy_link_session_roms_trade_support.py    len=140
OK tests/_pyboy_link_session_roms_battle_support.py   len=141
OK tests/test_pyboy_link_session_roms.py              len=1041
OK tests/test_pyboy_link_session_roms_diagnostics.py  len=221
OK tests/test_pyboy_link_session_roms_serial.py       len=203
```
AST equivalence also verified. A naive AST diff shows the six files differ, which is expected
(a restored docstring *is* an AST change). Stripping every bare string-expression statement
from both sides makes all six **IDENTICAL**, so nothing but the string's position moved.
`scripts/full_to_brock.py` and `tests/_tcp_trade_peer_drive_setup.py` are SAME-AST (whitespace only).

## VERIFIED CORRECT: step 3b (30f4a93) `_lane_covers()` has real teeth
`tests/test_local_ci_policy.py tests/test_stepping_loop_profile.py` -> **48 passed** (exit 0).

Mutation test (the claim that matters, since the whole point of 3b was to avoid vacuous rows):
dropped `scripts` from the `python -m ruff check` lane in `scripts/run_local_ci.sh`, then restored.
Result — **6 rows go red**, including all three rows step 3b rewrote:
```
FAILED tests/test_local_ci_policy.py::test_main_ruff_lanes_cover_every_script_file
FAILED tests/test_local_ci_policy.py::test_runner_ruff_file_lists_match_the_workflow_exactly
FAILED tests/test_local_ci_policy.py::test_matrix_benchmark_is_linted_by_every_main_ruff_lane
FAILED tests/test_local_ci_policy.py::test_main_ruff_lanes_run_in_executable_control_flow
FAILED tests/test_local_ci_policy.py::test_main_ruff_lanes_stop_the_local_runner_on_failure
FAILED tests/test_stepping_loop_profile.py::test_probe_module_and_its_tier_are_part_of_the_ci_contract
```
The helper shells out to `ruff check --no-cache --force-exclude <token> --show-files` per path
token and matches the resolved file set, so it asks Ruff the real question rather than string-matching.
After restoring the file the suite returned to 48 passed and `git status` was clean.
Conclusion: the three replaced rows were **not** weakened; they fail when coverage is removed.

## Lint residue, independently re-measured (this is what keeps #567 open)
`30f4a933`: `ruff check scripts tests --no-cache` -> **140 errors** (113 E402, 22 E731, 5 F811).
`fb2d4894` master baseline -> **285 errors** (136 E402, 62 E702, 60 E701, 22 E731, 5 F811).
So the stack removes all 122 E701/E702 and 23 E402, and leaves 140. #567 stays open.

## TWO FACTUAL ERRORS IN THE PR BODY, both to be corrected publicly
1. **"still exits 1 with 113 errors (113x E402, 22x E731, 5x F811)" is arithmetically wrong.**
   113 + 22 + 5 = **140**, not 113. The three-code breakdown is right; the total is not.
   The same wrong total appears in `LEAD_20261004T2100Z_573_STEP3.md`.
2. **`ruff format --check scripts tests --no-cache -> 448 files already formatted` is off by one.**
   Measured: **447 files already formatted**. There are 448 `.py` files under scripts/ and tests/;
   the difference is `scripts/produce_battle_state_fixtures.py`, which `pyproject.toml`
   (`force-exclude = true`, `exclude = [...]`) deliberately holds out of *formatting* because
   reformatting it would invalidate the SHA-1 in `release-evidence/fixture-manifest.json` that
   `merge_fixture_manifest_rows.py` re-checks. It is still lint-checked. The exclusion is
   intentional and pinned by `tests/test_local_ci_policy.py`; only the reported number is wrong.
Both corrections are non-blocking for the *code*, but the ledger must not carry false numbers.

## Verified-good summary
- 48/48 focused tests pass on the exact head.
- Format lane clean on the head (447 files).
- Mutation test proves the coverage rows can fail.
- Docstring repair is real and minimal.
- PR body correctly discloses that #567 is NOT closed.
## Remaining blockers for #573
- Rebase onto `fb2d4894`, resolve the 3-hunk conflict in `tests/test_local_ci_policy.py`.
- Re-run the focused suites + mutation test on the rebased head, and obtain a genuine
  independent review of that head (two dispatch attempts have misfired so far).
