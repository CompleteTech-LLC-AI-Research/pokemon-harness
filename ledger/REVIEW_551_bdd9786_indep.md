# Independent review — PR #551 `bdd9786`

Target verified with `git ls-remote origin refs/heads/fix/534-regular-package-path-portion`: `bdd9786b95ccb5db60c5a78d4a3983cd1c90d953` (exact expected SHA). Review performed from a detached worktree at that commit: `/workspace/poke-harness/.scratch/review551/worktree`. The primary checkout was not edited.

## Target verification

The detached worktree was at the requested exact SHA. Fresh venv: `/workspace/poke-harness/.scratch/review551/venv` (Python 3.11.2). Commands included:

```sh
git ls-remote origin refs/heads/fix/534-regular-package-path-portion
git worktree add --detach /workspace/poke-harness/.scratch/review551/worktree bdd9786b95ccb5db60c5a78d4a3983cd1c90d953
python3 -m venv /workspace/poke-harness/.scratch/review551/venv
/workspace/poke-harness/.scratch/review551/venv/bin/python -m pip install -e '.[dev]'
/workspace/poke-harness/.scratch/review551/venv/bin/uv pip install --python /workspace/poke-harness/.scratch/review551/venv/bin/python -e '.[dev]'
```

The install produced `__editable___pokered_harness_0_1_0_finder` under this venv's site-packages. `check_origins(Path(checkout))` returned PASS for both `pokered_harness` and `pyboy`, with their origins in this detached checkout. Observed custom finders in the fresh venv included `_distutils_hack.DistutilsMetaFinder` and the editable finder; both were accepted. The pytest hook `_pytest.assertion.rewrite.AssertionRewritingHook` was loaded when pytest ran and was also accepted. The environment was a venv, so no `_virtualenv._Finder` appeared in its meta path; `_virtualenv` itself is covered by site-packages location policy, but that specific finder was not empirically observed.

## Findings

### 1. Meta-path blocker and attack probes

Independent repro source: `/workspace/poke-harness/.scratch/review551/probe.py`. It builds a local regular package plus foreign `pkg/leaked.py`, registers a `find_spec(self, name, path, target=None)` smuggler, calls `check_origins(root, ('pkg',))`, then imports the submodule. Observed guard result was `FAIL` with `__main__.Finder` identified from the repro source; the subsequent import loaded the foreign file and printed `ORIGIN=foreign`. Thus the prior blocker is closed for an untrusted finder already present during the guard check.

Independent edge probe: `/workspace/poke-harness/.scratch/review551/edge_probe.py`. Each of these was rejected (`FAIL`): a `find_module`-only object, finder class, finder instance, an object that lies with `__module__='json'` while the loaded `json` module's `__file__` points outside site-packages, and a finder whose defining module is unresolvable. The module-name lie does not work because the code uses the defining module object's `__file__`, not the module name as the location evidence.

The guard only certifies the current `sys.meta_path` snapshot. A finder registered after `check_origins()` returns can serve a foreign submodule; the author comment explicitly scopes the claim to the interpreter at check time, and this is honest as stated. This cannot protect later imports from subsequent mutation of interpreter state. The guard snapshots the list with `list(sys.meta_path)` so changes during the scan are not revisited.

`.pth`-installed meta-path finders are caught unless their code is located under this interpreter's site-packages. `sys.path_hooks` and path-entry finders are not enumerated by this change. Those mechanisms generally control resolution of top-level/path entries and cannot preempt `PathFinder` for a missing submodule under an already-loaded regular package's fixed `__path__`; the requested exploit is specific to meta-path ordering. However, a hostile hook able to mutate a package's `__path__` or import state can exceed this narrow check. The current claim does not claim to certify all import hooks, only `sys.meta_path`.

No production route was found to make `importlib.util.find_spec` side effects occur during normal acceptance: finder modules already present in `sys.modules` use `__file__` directly, while unresolved finder modules are discovered through `find_spec`. `_finder_source` catches `ImportError`, `ValueError`, and `AttributeError`, but not arbitrary exceptions raised by a hostile import hook; that can abort the guard rather than return a false PASS. An unresolvable source returns `None`, which is refused.

### 2. `_foreign_path_locations` mismatch skip

The skip is live for divergent inputs: if the resolved reported origin does not match `sys.modules[package].__file__`, the function skips checking `__path__`. In production, `_resolve_origin` imports and returns that same live module's `__file__` (or its `__path__` for a namespace package); therefore its origin and module cannot normally diverge. The alternate installed-metadata origin route exists only in direct/internal testing seams and is explicitly why the comment describes a different ambient module. I found no production exploitable divergence in this checkout. The meta-path gate independently covers foreign submodule finders.

### 3. Mutation tests (each mutation in its own detached worktree)

Baseline command:

```sh
python -m pytest -q tests/test_import_origin_guard.py --junitxml=/workspace/poke-harness/.scratch/review551/baseline.xml
```

Parsed baseline JUnit: **50 tests, 0 failures, 0 errors, 0 skipped**.

Mutation commands used four detached worktrees under `.scratch/review551/mut-*`, one at a time, rebinding the editable install to each worktree before running the same pytest command. Parsed results:

| Mutation | JUnit / collection outcome | Tests that detected it |
|---|---:|---|
| Disable interpreter refusal (`intruders = []`) | 50 tests, 2 failures, 0 errors, 0 skipped | `test_meta_path_finder_cannot_smuggle_a_foreign_submodule`; `test_meta_path_finder_outside_site_packages_is_refused` |
| Trust every custom finder | 50 tests, 2 failures, 0 errors, 0 skipped | Same two targeted refusal tests |
| Remove installation-finder exemption | pytest collection refused with exit 4; no JUnit | The refusal listed pytest's assertion rewrite hook, `_distutils_hack.DistutilsMetaFinder`, and the editable finder. This is an expected over-rejection, directly demonstrating why the exemption is needed. A second dedicated venv install attempt for this mutation was blocked by `OSError: [Errno 28] No space left on device`; the first collection run already produced the rejection. |
| Make `_finder_source` return `None` always | Test run completed; see `finder_source_none.xml` when available | The independent runtime smuggler probe still returned FAIL because unidentifiable custom finders are refused. |

The XML files are in `/workspace/poke-harness/.scratch/review551/`. The baseline and the first two mutation XMLs were parsed directly. The fourth run was still running at the time of this report update; finalize its exact JUnit counters before treating the table's pending file/count as settled.

### 4. Error/false-trust review

- `_site_packages_roots`: `getsitepackages()` exceptions are caught before user-site roots are appended, so already-collected roots are preserved. A direct probe replaced `site.getsitepackages` with a raising function; the user site still appeared in the result.
- `_finder_source`: may raise on adversarial descriptors/metaclasses. A custom metaclass whose `__module__` property raises `RuntimeError` caused `_finder_source`, `_is_installation_finder`, and `_describe_finder` to propagate that exception. Normal classes/instances and the tested import mechanisms do not expose this behavior. This is a denial-of-service/robustness gap, not a demonstrated false PASS: the guard aborts instead of certifying.
- `_is_installation_finder`: relies on the reported defining module object's `__file__` (or `find_spec` origin) and resolved containment within current site-packages. A malicious finder can spoof a module's `__file__` to a site-packages path, since `__file__` is mutable metadata; I have not yet established whether the same finder can reliably pass that check with a live false path while still being defined outside. This is the remaining trust-boundary question to test before verdict.
- `_describe_finder`: descriptive formatting can likewise raise for hostile metaclass properties, but no success result is returned in that case.
- `_foreign_path_locations`: exceptions resolving a location become findings; the origin/module mismatch skip was not exploitable in the ordinary `check_origins` path.

## Blocking findings

Pending final adversarial trust-boundary check.
