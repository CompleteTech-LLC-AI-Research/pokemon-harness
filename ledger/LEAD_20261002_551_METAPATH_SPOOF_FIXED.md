# #551 — review round 2: `__file__` spoof bypass fixed at `947e3ec`

## Why this round existed

Independent review of `bdd9786`
(`/workspace/poke-harness/ledger/REVIEW_551_bdd9786_indep.md`, exact SHA verified) found the
meta-path blocker **not** fully closed. CI was green on `bdd9786` and the reviewer's own repro
correctly showed FAIL — but it found a second route to the same false PASS, and it explicitly
declined to issue a verdict rather than certify a partial fix. That was the right call and it is
recorded as a successful review, not a failed one.

## The bypass, reproduced on my own head before changing anything

Trust was decided from `module.__file__` of the module a finder *names*. `__file__` is ordinary
mutable state that the finder itself controls. A finder defined anywhere can register a module
object under a trusted-looking name whose `__file__` names a site-packages path that was never
written, and the lexical containment check believed it.

Reproduced on `bdd9786` with the guard otherwise behaving correctly:

    finder source seen by guard: <venv>/site-packages/spoofed_trusted_module.py
    trusted as installation finder: True
    guard status: PASS
    imported: <tmp>/foreign/probe_pkg/leaked.py
    LEAKED_AFTER_PASS: True

So `bdd9786` was not mergeable, exactly as the reviewer said.

## The fix at `947e3ec`

1. **The file must exist.** `_is_installation_finder` now requires the resolved defining path to be
   a real file (`source.is_file()`) inside site-packages. A spoofed name resolves to nothing and is
   refused. This keeps the trust decision attested by the filesystem rather than asserted by the
   object being judged — the same "provenance is decided by location only, never self-report"
   principle the module's docstring already states, now applied to the finder check as well.

2. **Hostile finders fail closed instead of aborting.** The reviewer also showed that a metaclass
   raising from `__module__`, or a module raising from `__file__`, propagated out of
   `_finder_source`, `_is_installation_finder` and `_describe_finder`. The guard is a preflight, so
   an exception escaping it turns an explicit refusal into a traceback — the same crash mode
   `_resolve_path` was already hardened against for package origins. All finder introspection is
   now individually guarded, and an unlocatable finder is simply untrusted.

## Verification on the exact head `947e3ec`

    pytest tests/test_import_origin_guard.py                        -> 52 passed
    pytest tests/test_import_origin_guard.py test_local_ci_policy.py -> 62 passed
    ruff check / ruff format --check (both changed files)            -> clean
    git diff --check                                                -> clean

Behavioural controls:

    original meta-path exploit  -> FAIL   (was PASS at 89e7305)
    __file__ spoof bypass       -> FAIL   (was PASS at bdd9786)
    own checkout CLI            -> PASS, exit 0
    foreign worktree CLI        -> FAIL, exit 1

## New tests, each proven non-vacuous

- `test_a_finder_cannot_trust_itself_by_claiming_a_site_packages_module` — the reviewer's bypass.
  Asserts the claimed site-packages path does not exist (so the row means something), that the
  finder is not trusted, that the guard FAILs, and that the foreign import really does resolve
  foreign. Removing the `is_file()` check makes this row **fail**.
- `test_a_hostile_finder_metaclass_cannot_abort_the_guard` — a metaclass whose `__module__`
  property raises. Asserts the guard returns a FAIL finding rather than propagating
  `RuntimeError`. Removing the defensive lookup makes this row **fail** with that exact error.

One real CPython subtlety cost a debugging cycle and is now encoded in the test as a comment: a
class installed on `sys.meta_path` has its `find_spec` called **unbound**, so `self` is bound to
the module name. A class-form smuggler therefore needs `@classmethod` (which is how setuptools
actually writes the editable finder). My first draft used an ordinary method and the escape
silently stopped working — the row would have passed for entirely the wrong reason. Any future
finder test must get this right or it proves nothing.

## Still open — not claimed as resolved

- **Scope, stated honestly:** the guard certifies the `sys.meta_path` snapshot at check time. A
  finder registered *after* `check_origins` returns can still serve a foreign submodule. That is
  outside what a preflight can control, and the guard does not claim otherwise. The reviewer
  assessed this as honest as stated rather than a blocking defect.
- **`sys.path_hooks`** are not enumerated. They govern path-entry resolution and cannot preempt
  `PathFinder` for a missing submodule under an already-loaded regular package, so they are not
  the reported vector — but a hook able to mutate a package's `__path__` exceeds this check.
- **The origin/module mismatch skip** in `_foreign_path_locations`: the reviewer and I both found
  no production divergence, because `_resolve_origin` derives the origin from the same live
  `sys.modules` entry. It remains a latent fail-open if those inputs ever diverge, and is recorded
  rather than changed, so a reviewer can rule on it.

## Status

CI re-triggered on `947e3ec`. **Not merged** — it needs a fresh independent review of `947e3ec`.
The REQUEST CHANGES verdict was against `650a8bc`, the incomplete report against `bdd9786`; neither
transfers to this head.

Delegation note: the first `codex2 exec` reviewer was killed mid-run and produced nothing (the
known #489 delivery flakiness). The second, launched with `setsid` so it survives the parent
shell, completed and produced the report above.
