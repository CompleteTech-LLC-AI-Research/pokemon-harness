# PR #555 closes the mutation gaps on the combine branch

`lead/551-combine-553` (`d96124d`) restores the meta-path guard on top of the
merged #553 lineage, but its mutation matrix left M1, M2 and M4 alive. PR #555
(`fix/551-553-close-mutation-gaps`, head `36d27c9`) closes all three with
test-only changes; `scripts/check_import_origins.py` is byte-identical to
`lead/551-combine-553`.

## What was added

1. **Borrow row premise made deterministic.** It preferred
   `site-packages/pyvenv.cfg` and then a `*.dist-info` entry. In a typical venv
   both are directories, so `is_file()` rejected the borrow for a reason
   unrelated to provenance and the row passed no matter how trust was decided.
   It now selects a real `.py` file and asserts the path is a regular file.
2. **New row: no code provenance.** Every other row defeats a finder that has a
   real code object to compare against site-packages. A finder built with
   `compile(src, "", "exec")` has none, and falling back to its self-reported
   `__file__` there hands trust to an attacker with no provenance at all.
   Kills M4.
3. **New row: vanished source file.** Resolution of a missing path succeeds, so
   containment alone accepts a filename that merely reads as installed. A
   finder compiled from a site-packages path that does not exist must still be
   refused. Kills M2.

Each new row asserts its own premise before asserting the outcome, so a row
cannot pass because some unrelated part of the check happened to refuse the
finder.

## Matrix after the change

| Mutant | Change | Result |
| --- | --- | --- |
| M1 | trust `_finder_source` | killed by the borrow row and the no-provenance row |
| M2 | drop the `is_file()` requirement | killed by the vanished-file row |
| M3 | `_finder_code_file` always `None` | killed (collection abort, fails closed) |
| M4 | `_finder_code_file` falls back to `_finder_source` | killed by the no-provenance row |
| M5 | `_is_installation_finder` always `True` | killed (7 failures) |
| M6 | never collect offenders | killed (7 failures) |

Baseline: **107 tests, 0 failures, 0 errors.**

## Verification

Dedicated worktree `/workspace/poke-harness/.scratch/comb/tree` with its own
editable install, so `sys.meta_path` carries only this checkout's finders.

- Suite: 107 tests, 0 failures, 0 errors.
- `probe551c.py`: guard returns FAIL, `not-leaking`. Same script on master
  returns PASS and leaks.
- Own-checkout CLI rc=0; `git diff --check` clean; `ruff format` clean.
- `ruff check` still reports the two pre-existing `SIM117` errors from #553.
  They are not in the lines this PR touches and were left alone to keep the
  diff reviewable.

## State

- PR #555 open against master, mergeable, native unit validation in progress.
- PR #551 (`c1bef30`) is superseded for merge purposes: its head predates
  `f6a95f6` and merging it would revert #553's work.
