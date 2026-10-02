# #551 — meta-path finder BLOCKER fixed at `bdd9786`

## The finding being closed

Independent review of `650a8bc` returned REQUEST CHANGES with a BLOCKING finding: a custom
`sys.meta_path` finder can supply a foreign submodule *after* the guard reports PASS. I
reproduced it on the synced head `89e7305` before touching any code:

    guard status: PASS
    imported file: <tmp>/foreign/probe_pkg/leaked.py
    LEAKED_FOREIGN_AFTER_PASS: True
    VERDICT: BLOCKER_LIVE

The origin checks and the `__path__` portion checks are both blind to this. `PathFinder` only
searches a parent's `__path__`, but `importlib._bootstrap` consults `sys.meta_path` **in order and
before** `PathFinder`, so a finder placed earlier can return a spec for a submodule from any
directory in the filesystem. No amount of location checking on the package itself observes that.

Two cheaper fixes were considered and rejected on evidence, not taste:

- **Probe each finder with a canary submodule name.** Defeated immediately. A finder that
  special-cases one name returns `None` for any other, so the probe passes while the real import is
  still hijacked. Measured: with a finder serving `probe_pkg.leaked`, a `probe_pkg.__canary__`
  probe returned `None` from every finder.
- **`sys.addaudithook` to observe the import.** Useless for a preflight. The relevant events
  (`open`, `compile`, `exec`) fire *during* the import, i.e. after the guard has already returned
  PASS, and the `import` audit event does not fire for a submodule whose parent is already loaded.

## The fix

The guard now refuses to certify an interpreter whose `sys.meta_path` contains a finder this
checkout did not install, and reports it as a FAIL finding naming the finder and its source file.

Trust is decided by **identity and location, never by name**:

- `_is_trusted_stdlib_finder` — the exact `BuiltinImporter`, `FrozenImporter`, and `PathFinder`
  objects, compared by identity. Nothing a site-packages package defines can impersonate them.
- `_is_installation_finder` — a custom finder whose defining module resolves inside this
  interpreter's own site-packages. This is what keeps the supported release lane working:
  `pip install -e ".[dev]"` installs `__editable___pokered_harness_0_1_0_finder`, and a virtual
  environment installs `_virtualenv._Finder`.
- Everything else is refused.

Two implementation details that are load-bearing:

- The editable-install finder is placed on `sys.meta_path` as a **class**, and its defining module
  is usually not yet in `sys.modules` (setuptools writes a stub that inserts the class directly).
  `_finder_source` therefore falls back to `importlib.util.find_spec(name)` to locate the file
  *without* importing the guarded packages. Reading `type(finder).__module__` instead of
  `finder.__module__` yields the metaclass (`builtins`) and misclassifies it — that mistake is
  recorded because I made it while probing.
- Site-packages is gathered defensively: an exception from `site.getsitepackages()` or
  `site.getusersitepackages()` must not discard the directories already collected, or the release
  lane would be refused on some layouts.

## Mutation matrix — each mutant killed, in its own run

| Mutation | Change | Result |
|---|---|---|
| M1 | `intruders = []` (refusal disabled) | **killed** — both new meta-path rows FAIL |
| M2 | `_is_installation_finder` returns `True` for everything | **killed** — both new rows FAIL |
| M3 | installation finders no longer trusted | **killed** — the suite refuses to collect at all |

M3 is the important one, and it is why the design is location-based rather than "reject anything
custom". Removing the installation-finder exemption makes `pytest_configure` abort with:

    ERROR: refusing to collect: ... untrusted sys.meta_path finder(s) ... 
    _pytest.assertion.rewrite.AssertionRewritingHook (from <venv>/site-packages/_pytest/...)
    _virtualenv._Finder (from <venv>/site-packages/_virtualenv.py)
    __editable___pokered_harness_0_1_0_finder._EditableFinder (from <venv>/site-packages/...)

That is the guard refusing the very environment it exists to protect. Verified separately that
under a real pytest run `_untrusted_meta_path_finders() == []` and `check_origins` still returns
PASS, i.e. pytest's own assertion-rewriting hook is correctly treated as an installation finder.

## Verification on the exact head `bdd9786`

    pytest tests/test_import_origin_guard.py                        -> 50 passed
    pytest tests/test_import_origin_guard.py test_local_ci_policy.py -> 60 passed
    ruff check / ruff format --check (both changed files)            -> clean
    git diff --check                                                -> clean

Behavioural controls, all re-run after the final refactor:

    meta-path exploit repro   -> guard status FAIL   (was PASS at 89e7305)
    own checkout CLI           -> PASS, exit 0
    foreign worktree CLI       -> FAIL, exit 1  (pokered_harness and pyboy both refused)
    guard under a live pytest run -> PASS, zero untrusted finders

## Tests added

- `test_meta_path_finder_cannot_smuggle_a_foreign_submodule` — the reviewer's exact vector. Builds
  a local regular package, a foreign-only submodule, and a finder that serves only that one name;
  asserts FAIL, then imports the submodule to prove the escape is real and the row is not
  vacuous. Carries an explicit comment warning about the `target` shadowing trap, because an
  earlier draft of the probe reused the name for the foreign path and made the row pass
  vacuously.
- `test_meta_path_finder_outside_site_packages_is_refused` — the refusal keys on location, not
  name: a finder defined outside site-packages is refused and named in the detail.
- `test_standard_and_installation_finders_are_still_trusted` — the control that keeps this from
  degenerating into "reject every custom finder"; it asserts the real editable install *is*
  recognised as an installation finder.

## Status

CI re-triggered on `bdd9786`. The earlier REQUEST CHANGES also noted that the origin/module
mismatch skip in `_foreign_path_locations` is a latent fail-open if its two inputs diverge; in
this checkout `_resolve_origin` derives the origin from the same `sys.modules` entry, so no
production release-lane mismatch was established. I have not touched that skip — it is a
separate judgement call, and it is recorded here so the fresh review can rule on it rather than
discover it.

This head is **not merged**. It needs a fresh independent review of `bdd9786` specifically; the
REQUEST CHANGES verdict was against `650a8bc` and does not transfer.
