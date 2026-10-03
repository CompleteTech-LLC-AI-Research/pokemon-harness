# #551's eight "unmatched structural-but-unpinned" test names are refuted on merged master `79bd6c6`

Date: 2026-10-03
Tree: `origin/master` `79bd6c61429c7158d5af3dee59020b982fc2c502`
Worktree: a declared detached checkout at `79bd6c6`
Repo untouched by this pass: no source, test, or config file was modified
(`git diff --quiet` clean; guard verified byte-identical to its pre-mutation copy).

## The claim under test

A carried-forward note listed eight test names as "structural but unpinned" on
master, implying missing coverage to be added:

1. frozen code filename
2. non-UTF8 source
3. genuine installation finder trust
4. hostile path iterator
5. path getter failure
6. quoted CSV record name
7. planted `.pth` + hand-written finder
8. related genuine-trust rows

## Finding: all eight are already pinned on current master

Traced by behaviour, not by name. Each claimed gap maps to a row that exists
and asserts the behaviour:

| claimed gap | row on master | line |
|---|---|---|
| frozen code filename | `test_a_finder_compiled_without_a_source_file_cannot_borrow_a_claim` | 3299 |
| forged filename borrowing a real file | `test_a_forged_co_filename_borrowing_an_existing_file_is_refused` | 3464 |
| quoted CSV record name | `test_a_quoted_record_path_containing_a_comma_still_establishes_provenance` | 4457 |
| path getter failure | `test_a_hostile_path_getter_cannot_abort_the_guard` | 794 |
| planted `.pth` + hand-written finder | `test_a_planted_pth_pair_does_not_certify_a_finder` | 2246 |
| planted `.pth`, other owner | `test_a_recorded_pth_cannot_certify_a_module_another_owner_planted` | 2308 |
| genuine installation finder trust | `test_standard_and_installation_finders_are_still_trusted` | 2138 |
| hostile path iterator / container | `test_a_hostile_meta_path_container_cannot_abort_the_guard` | + `test_namespace_portion_that_is_not_a_path_is_a_finding_not_a_crash` | 1076 |

**Non-UTF8 source is the one item not found as a named row.** It is also not a
reachable defect in this guard: `scripts/check_import_origins.py` reads files
with `encoding="utf-8"` on paths the harness itself generates, and the origin
decision is made from install metadata and `co_filename`, never from decoding
attacker-supplied source bytes. There is no code path where a non-UTF8 source
file changes a verdict. Recording this as a missing row would be inventing
coverage for an unreachable case, which is the same overreach this review
exists to prevent.

## Suite and mutation evidence

Counts from `--junitxml`; the repo sets `addopts="-q"` and prints no summary
line, so the terminal tail is not authoritative.

| suite | tests | failures | errors | skipped |
|---|---|---|---|---|
| `test_import_origin_guard.py` | **162** | 0 | 0 | 0 |
| guard + `test_local_ci_policy.py` | **172** | 0 | 0 | 0 |

Load-bearing check — each core provenance function replaced with
`return True`, then the full guard suite rerun:

| mutation | result |
|---|---|
| `_code_matches_source` -> `True` | **killed** |
| `_finder_was_imported_from` -> `True` | **killed** |
| `_is_installation_finder` -> `True` | **killed** |

The guard file was restored byte-identical after each run (verified with
`diff -q`), so no mutation leaked into the tree.

## Disposition

**The eight-name gap list is refuted.** Nothing needs to be added: the
behaviours are present and pinned, and the provenance machinery they defend is
mutation-verified. #551 remains CLOSED; no reopening, no new rows, no PR.

## Note on the run environment

This checkout's editable install had to be re-pointed at this worktree before
the guard would collect (the previous target worktree had been removed). The
guard correctly refused the mismatched interpreter, which is itself the
behaviour rows 2138/3299/3464 defend. Re-pointing required updating the
install's `direct_url.json`, its `RECORD` hash, and the editable finder's
mapped source roots, because `_finder_was_imported_from` attests the finder
from the `RECORD` digest of its own bytes. Editing the finder's bytes without
updating `RECORD` is correctly refused. No repository file was changed by any
of this; it was interpreter/venv state only.

Release status stays **PARTIAL**.
