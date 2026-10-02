# Independent review — PR #551 round 4

Status: INCOMPLETE — the requested six mutation runs were not completed, so no approval verdict is issued.

## Target verification

- `git ls-remote origin refs/heads/fix/534-regular-package-path-portion` returned `c1bef303d4ec331b8dbe5b49ccf62663ea5f2858`.
- Detached worktree `/workspace/poke-harness/.scratch/review551r4/tree` was created at that exact commit. The primary checkout was not edited.
- Fresh venv: `/workspace/poke-harness/.scratch/review551r4/venv`; `pip install -e ".[dev]"` succeeded.

## Regression run

Command:

```sh
/workspace/poke-harness/.scratch/review551r4/venv/bin/python -m pytest tests/test_import_origin_guard.py tests/test_local_ci_policy.py -q --junitxml=/workspace/poke-harness/.scratch/review551r4/baseline.xml
```

Result: **63 tests, 0 failures, 0 errors, 0 skipped** (JUnit). This includes namespace portions, regular packages with foreign `__path__`, direct local distribution metadata, and foreign distribution rejection tests.

## Blocking finding

### Wheel install built from the checkout is rejected

A fresh wheel was built and installed non-editably into `/workspace/poke-harness/.scratch/review551r4/wheelvenv`. Since pip's ordinary wheel installation has no local `direct_url.json` source link, `_allowed_roots()` does not admit that distribution's site-packages package paths. Running `check_origins(Path("/workspace/poke-harness/.scratch/review551r4/tree"))` under that interpreter with the checkout on `sys.path` returned `FAIL` for both `pokered_harness` and `pyboy`, explicitly saying each resolves outside the checkout and outside the site-packages of an install made from it.

Repro commands:

```sh
python3 -m venv /workspace/poke-harness/.scratch/review551r4/wheelvenv
/workspace/poke-harness/.scratch/review551r4/venv/bin/python -m pip wheel --no-deps --no-build-isolation -w /workspace/poke-harness/.scratch/review551r4/wheelhouse .
/workspace/poke-harness/.scratch/review551r4/wheelvenv/bin/python -m pip install --no-deps /workspace/poke-harness/.scratch/review551r4/wheelhouse/*.whl
/workspace/poke-harness/.scratch/review551r4/wheelvenv/bin/python -m pip install numpy==2.4.6 cython==3.0.12
/workspace/poke-harness/.scratch/review551r4/wheelvenv/bin/python - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "/workspace/poke-harness/.scratch/review551r4/tree")
from scripts.check_import_origins import check_origins
print(check_origins(Path("/workspace/poke-harness/.scratch/review551r4/tree")))
PY
```

Observed `FAIL` packages: `pokered_harness` at `wheelvenv/site-packages/pokered_harness/__init__.py` and `pyboy` at `wheelvenv/site-packages/pyboy/__init__.py`.

## Trust boundary probes

- Confirmed `functools.partial` and a callable object as `find_spec` have no readable Python code and are refused by `_is_installation_finder` (`False` / `_finder_code_file=None`).
- A straightforward attempt to overwrite a finder function's `__code__` with a chosen site-packages filename was not sufficient: it was rejected because that claimed filename did not exist.
- The shipped `trust_itself` test uses a `@classmethod` and calls it with the actual foreign submodule name; it does not have the class-form unbound-method vacuity described in the brief. The `borrow` test also uses `@classmethod` and asserts the borrowed existing file and the independent code filename.
- A descriptor-based class finder probe executed from `/workspace/poke-harness/.scratch/review551r4/descriptor_probe.py` served a foreign module after `check_origins` returned FAIL (not PASS). It is not a finding.
- `exec` and interactive `<string>`/`<stdin>` code are excluded by the explicit angle-bracket filename check.
- The implementation snapshots `list(sys.meta_path)` and documents this as preflight. Behavior added after the call is outside the stated guarantee.
- `.pyc`-only and zip/egg finders, symlink escape, a genuine hostile installed finder, wheel-lane correctness beyond the failure above, and a robust finder-replacement-during-scan attack were not fully resolved.

## Mutation checks

Not completed. The required separate JUnit mutation runs (revert to `_finder_source`; revert to lexical `__file__` containment; return `None`; fallback to `_finder_source`; always trust; disable refusal) remain outstanding. Therefore this report is not an approval and does not certify the current trust boundary.

VERDICT: REQUEST CHANGES
