# #551 `c1bef30` mutation matrix

The round-4 review of `c1bef30` returned REQUEST CHANGES because the requested
mutation matrix had not been run, not because it had found a new live bypass.
This is that matrix, plus the follow-up needed for the two survivors it exposed.

## Setup

- Worktree: `/workspace/poke-harness/.scratch/mut551/tree` at `c1bef30`, clean.
- Interpreter: a dedicated venv at `/workspace/poke-harness/.scratch/mut551/venv`
  with its own `pip install -e ".[dev]"`, so `sys.meta_path` carries this
  checkout's own `_EditableFinder` and nothing from another worktree.
- Suite: `tests/test_import_origin_guard.py tests/test_local_ci_policy.py`.
- Each mutation is applied to a pristine copy of the guard, the suite is run,
  and the source is restored, so mutants never compose.
- Baseline: **63 tests, 0 failures, 0 errors.**

## Results

| Mutant | Change | Result | Killed by |
| --- | --- | --- | --- |
| M1 | `_is_installation_finder` trusts `_finder_source` | survived as run | none (accidental, see below) |
| M2 | drop the `code_file.is_file()` requirement | **survived** | none |
| M3 | `_finder_code_file` always returns `None` | killed | collection abort (fails closed) |
| M4 | `_finder_code_file` falls back to `_finder_source` | **survived** | none |
| M5 | `_is_installation_finder` returns `True` always | killed | 5 failures |
| M6 | `_untrusted_meta_path_finders` never collects offenders | killed | 5 failures |

M3, M5 and M6 die loudly. M5 and M6 kill the same five rows:
`test_meta_path_finder_cannot_smuggle_a_foreign_submodule`,
`test_meta_path_finder_outside_site_packages_is_refused`,
`test_a_finder_cannot_trust_itself_by_claiming_a_site_packages_module`,
`test_a_hostile_finder_metaclass_cannot_abort_the_guard`, and
`test_a_finder_cannot_borrow_a_real_installed_modules_file`.

## M2 is a weakening the rows do not pin

M2 removes the `is_file()` call. The premise it needs is a finder whose
`co_filename` is lexically inside site-packages but names a path that does not
exist. The spoof row already builds exactly that claim
(`site-packages/spoofed_trusted_finder_module.py`, asserted not to exist), so
the premise is real; what the row does not isolate is the `is_file()` check
itself, because the spoof row is refused earlier and for a different reason.
M2 is therefore not independently exploitable against `c1bef30` today, but it
removes a defence-in-depth check the spoof row's docstring documents as
load-bearing. Recorded as a follow-up, not as a live bypass.

## M4 is a real, uncaught bypass

M4 reintroduces trust in the finder's own claim, and the suite does not catch
it. Reproduction: the attacker compiles its `find_spec` with an empty
`co_filename` and then borrows a real installed module's `__file__`.

```
baseline c1bef30: _finder_code_file=None, TRUSTED=False, offenders=[ForgedFinder]
M4 mutant:        _finder_code_file=<site-packages/...finder.py>,
                  TRUSTED=True,  offenders=[]
```

Under M4 the guard returns PASS while a foreign finder sits on `sys.meta_path`
able to import a checked package's submodule from outside the checkout. That is
the precise failure mode #551 exists to prevent, and M4 is the shortest path
back to it. The gap is a missing row, not a broken guard: baseline already
refuses this finder correctly.

## M1 is killed only by accident

`test_a_finder_cannot_borrow_a_real_installed_modules_file` is written to borrow
a real *file* inside site-packages, but its target selection lands on a
**directory** in this venv:

```
selected borrowed = .../site-packages/rpds_py-2026.6.3.dist-info
is_file()         = False
exists()          = True
is_dir()          = True
```

So under M1 the borrowed claim is a directory, the `is_file()` requirement
rejects it, and the row passes without ever exercising M1. Forcing the row to
borrow an actual `.py` file makes M1 fail:

```
forced real .py borrow + M1:       1 failed
forced real .py borrow + baseline: 1 passed
```

The guard is correct in all three states. The defect is in the test: its
premise is environment-dependent, so its kill of M1 is incidental and will not
hold in a venv whose `site-packages` happens to contain a `pyvenv.cfg` or a
top-level `*.dist-info` directory.

The sibling row `test_a_finder_cannot_trust_itself_by_claiming_a_site_packages_module`
does not share this flaw: it asserts `not claimed.exists()` for an explicit
non-existent file path, so its premise is deterministic.

## Verdict

`c1bef30` itself is sound: baseline is 63/63 green and all three previously
reproduced exploit forms (foreign finder, non-existent claim, borrowed real
file) are refused. Two test-coverage defects are open, and both belong on the
PR branch before this matrix can be called complete:

1. **Add the missing row** that pins M4: a finder whose `find_spec` was
   compiled from an empty or absent `co_filename`, and which claims a real
   installed module, must be refused. Without it the suite cannot detect a
   re-introduction of claim-based trust.
2. **Make the borrow row's premise deterministic** by selecting a real `.py`
   file, so M1 is killed by the row's own logic rather than by a directory
   coincidence.

Neither item is a live bypass of `c1bef30`; both are test coverage. The guard
change is mergeable once the rows exist and the matrix re-runs with every
mutant killed.

## Artifacts

- Matrix runner: `/workspace/poke-harness/.scratch/mut551/run_matrix.sh`
- Mutant patches: `/workspace/poke-harness/.scratch/mut551/apply_m{1..6}.py`
- M4 exploit: `/workspace/poke-harness/.scratch/mut551/m4_exploit.py`
- JUnit XML: `/workspace/poke-harness/.scratch/mut551/{baseline,keep_m*}.xml`
