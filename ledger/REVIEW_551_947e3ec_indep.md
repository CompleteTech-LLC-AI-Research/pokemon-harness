# Independent review — PR #551 head `947e3ec`

Target verified from `origin`: `947e3ec48c1b5768091e0e525d468c857c33f119` (`fix/534-regular-package-path-portion`). This is the requested head, not a newer remote revision.

## Setup and baseline

- Detached worktree: `/workspace/poke-harness/.scratch/review551r3/wt` at `947e3ec48c1b5768091e0e525d468c857c33f119`; primary checkout untouched.
- Fresh venv: `/workspace/poke-harness/.scratch/review551r3/venv`; installed using `python3 -m venv ...` then `venv/bin/python -m pip install -e ".[dev]"`.
- Release lane sanity: `_distutils_hack.DistutilsMetaFinder` and `__editable___pokered_harness_0_1_0_finder._EditableFinder` resolved to existing files under the venv `site-packages`, `_is_installation_finder` returned `True` for both, and `check_origins(Path.cwd())` returned `PASS`.
- Command: `venv/bin/python -m pytest tests/test_import_origin_guard.py tests/test_local_ci_policy.py --junitxml=.../baseline.xml`. Parsed JUnit: 62 tests, 0 failures, 0 errors, 0 skipped. Repeated after mutation trials: 62, 0, 0, 0.
- Added tests use `@classmethod` for class-form finder smuggling, matching CPython's unbound class call behavior.

## Blocking finding — existing site-packages module can authenticate an unrelated finder

`_is_installation_finder` checks whether the path reported by `_finder_source` is an existing file under site-packages. Both inputs to that decision can still be forged: the finder controls `__module__`, and the corresponding `sys.modules[name].__file__` can name any genuine installed module. `is_file()` therefore proves only that the borrowed file exists, not that it defines the finder.

Runnable reproduction from the detached worktree, using the review venv:

```bash
/workspace/poke-harness/.scratch/review551r3/venv/bin/python /workspace/poke-harness/.scratch/review551r3/probe.py
```

The probe registers a fake module with `__file__` set to this venv's real `site-packages/pip/__init__.py`, assigns that module name to a class-form finder, and uses a classmethod `find_spec` to serve `probe_pkg.leaked` from a foreign directory. Observed output:

```text
claim exists True site containment True
finder source .../site-packages/pip/__init__.py trusted True intruders []
guard PASS
leaked foreign .../foreign-.../leaked.py
```

This is a direct PASS followed by a foreign import, so the trust boundary remains bypassable. Fix must establish that the actual executing/defining code for the finder is the trusted site-packages file; mutable module naming and `__file__` alone cannot establish that.

A site-packages symlink whose target is outside site-packages did **not** bypass the current check: `_finder_source` resolves the symlink first, after which containment rejects it. Namespace and regular package foreign path-portion tests are present in the suite. The arbitrary existing-file claim above is sufficient to block approval.

## Mutation results

Each mutation was applied separately to the detached worktree's `scripts/check_import_origins.py`, followed by the two-file pytest command above. Source was restored after each run.

| Mutation | Parsed result | Tests that detect it / note |
|---|---:|---|
| Remove `source.is_file()` check | 62 tests; 1 failure, 0 errors, 0 skipped | `test_a_finder_cannot_trust_itself_by_claiming_a_site_packages_module` |
| `_is_installation_finder` returns `True` for everything | 62; 4 failures, 0 errors, 0 skipped | `test_meta_path_finder_cannot_smuggle_a_foreign_submodule`, `test_meta_path_finder_outside_site_packages_is_refused`, `test_a_finder_cannot_trust_itself_by_claiming_a_site_packages_module`, `test_a_hostile_finder_metaclass_cannot_abort_the_guard` |
| Remove defensive catches around metaclass and module `__file__` lookups in `_finder_source` | 62; 1 failure, 0 errors, 0 skipped | `test_a_hostile_finder_metaclass_cannot_abort_the_guard` |
| `_finder_source` returns `None` always | Pytest did not collect; exit 4 and no JUnit file | The environment's pytest assertion-rewrite, distutils, and editable finders all became unidentifiable and were refused during pytest startup. This does not yield parsed test counts. |
| Set `intruders = []` | 62; 4 failures, 0 errors, 0 skipped | Same four meta-path/refusal tests listed for always-true mutation |

Important non-vacuity gap: the existing `test_a_finder_cannot_trust_itself_by_claiming_a_site_packages_module` exercises only a nonexistent claimed file. It does not try borrowing an existing module file, which is why the blocking repro survives the test suite.

## Scope and regression notes

The guard's comments accurately describe the preflight as a check of `sys.meta_path` at call time; adding a new finder after `check_origins` returns is outside that snapshot. I found no broader promise in the checked code that it monitors later changes.

The normal editable release lane passed, including the guard suite and `tests/test_local_ci_policy.py`. Existing suite coverage includes plain local `direct_url.json` handling, foreign distributions, foreign and local namespace packages, and regular-package foreign portions. A separately built non-editable/wheel install was not run. No such regression signal is needed to decide this review because the foreign-import PASS bypass is blocking.

VERDICT: REQUEST CHANGES
