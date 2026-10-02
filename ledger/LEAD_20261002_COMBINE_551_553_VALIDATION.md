# Validation of `lead/551-combine-553` (`d96124d`)

`master` lost the meta-path guard when PR #553 merged (see
`LEAD_20261002_MASTER_META_PATH_REGRESSION.md`). `lead/551-combine-553` is the
branch that restores it on top of the #553 lineage. This is its validation.

## Shape

The branch is a descendant of the merged master commit `f6a95f6` and layers the
reviewed meta-path hardening on top:

```
c7ce858  Record the #553 merge
f6a95f6  Merge pull request #553 (lead/548-unresolvable-paths)
3d7f8af  #534: refuse an empty origin instead of crediting the CWD (#552)
13e5145  independent review reproduced a false PASS before fixing it
1346a69  #534: refuse a meta-path finder that can import from outside
89cd53d  #534: require the finder's file to exist; inspect defensively
3e84152  #534: decide finder trust from compiled code provenance
d96124d  #551+#553: resolve the semantic conflict, keep both hardenings
```

The trust decision at `d96124d` is the reviewed one: `_is_installation_finder`
still calls `_finder_code_file(finder)` and still requires `code_file.is_file()`.
The two guards coexist rather than shadowing each other.

## Verification

Tested in a dedicated worktree with its own editable install, so `sys.meta_path`
carries only this checkout's finders.

- Guard suite + local CI policy: **105 tests, 0 failures, 0 errors.**
  (`c1bef30` alone had 63; the #553 lineage contributes 42 more and none conflict.)
- Live blocker repro (`probe551c.py`): guard returns **FAIL**, `not-leaking`.
  On current master the same script returns PASS and leaks.
- Spoof repro: finder claiming a non-existent site-packages module -> refused.
- M4 forged-code repro -> refused, finder reported as an offender.
- `git diff --check` clean; `ruff format --check` clean.

`ruff check` reports two `SIM117` errors in `tests/test_import_origin_guard.py`
(nested `with` statements at lines 772 and 795). These are **pre-existing on
master** and arrived with the #553 lineage, not with the #551 work. They are
recorded here so the combine is not credited with a clean lint it does not have;
fixing them is a separate, trivial follow-up.

## Mutation matrix

Identical to the `c1bef30` matrix, so the same two gaps are still open here.

| Mutant | Change | Result |
| --- | --- | --- |
| M1 | trust `_finder_source` | survived (accidental kill only) |
| M2 | drop the `is_file()` requirement | survived |
| M3 | `_finder_code_file` always `None` | killed (collection abort) |
| M4 | `_finder_code_file` falls back to `_finder_source` | **survived** |
| M5 | `_is_installation_finder` always `True` | killed (5 failures) |
| M6 | never collect offenders | killed (5 failures) |

M5 and M6 kill the same five rows as before.

M4 remains a real uncaught bypass here. The attacker compiles `find_spec` with
an empty `co_filename` and borrows a real installed module's `__file__`:

```
baseline d96124d: TRUSTED=False, offender reported
M4 mutant:        TRUSTED=True,  zero offenders  -> guard would return PASS
```

The borrow row's premise is still environment-dependent: it prefers
`site-packages/pyvenv.cfg` and then a `*.dist-info` entry, both of which can be
directories, so `is_file()` rejects the borrow and the row never exercises M1.

## Open items before merge

Both are test-coverage gaps, not defects in the guard. `d96124d` refuses every
exploit form that was reproduced against it.

1. Add a row pinning M4: a finder compiled from an empty/absent `co_filename`
   that claims a real installed module must be refused.
2. Make the borrow row select a real `.py` file so M1 is killed by the row's own
   logic.

Then re-run the matrix and obtain an independent review of the exact head before
merging.

## Artifacts

- Worktree: `/workspace/poke-harness/.scratch/comb/tree` at `d96124d`
- Venv: `/workspace/poke-harness/.scratch/comb/venv`
- Matrix runner: `/workspace/poke-harness/.scratch/comb/run_matrix.sh`
- JUnit XML: `/workspace/poke-harness/.scratch/comb/{suite,m*}.xml`
