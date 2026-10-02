# #551 — review round 3: borrowed-`__file__` bypass fixed at `c1bef30`

## Why this round existed

Independent review of `947e3ec`
(`ledger/REVIEW_551_947e3ec_indep.md`, SHA256
`7177d72ff42dde33b9650957857848bfcb8004dcec3adb1dad87ccea5362e143`) returned REQUEST CHANGES. CI was green
and three of four mutations were killed, but the reviewer found a third route to the same false
PASS. It also pointed out precisely why my previous fix was insufficient:

> "Fix must establish that the actual executing/defining code for the finder is the trusted
> site-packages file; mutable module naming and `__file__` alone cannot establish that."

That was correct, and it is the same class of mistake I had already made once.

## The bypass, reproduced on my own head before changing anything

`947e3ec` required the claimed file to **exist** inside site-packages. But a finder can borrow the
`__file__` of a *genuine* installed module — one that really is in site-packages and really does
exist — and pass both checks:

    borrowed file: <venv>/site-packages/_virtualenv.py     (real, exists)
    finder source: <venv>/site-packages/_virtualenv.py
    trusted: True
    intruders: []
    guard status: PASS
    imported: <tmp>/foreign/probe_pkg/leaked.py
    BYPASS: True

Existence proves the *borrowed* file is real. It says nothing about whether that file defines this
finder. `_virtualenv.py` is a real installed module and has nothing to do with the smuggler.

## The pattern across three rounds

All three bypasses are the same mistake, mine, twice repeated:

| Round | Trust decided from | Defeated by |
|---|---|---|
| `bdd9786` | `module.__file__` containment | claiming a name whose file was never written |
| `947e3ec` | …plus `is_file()` | borrowing a real installed module's file |
| `c1bef30` | **compiled code provenance** | — under test |

Every version read something the finder *controls*. `__module__`, `sys.modules[name]`, and
`module.__file__` are all ordinary mutable state that the finder can set for itself. No amount of
validating a self-report makes it trustworthy.

## The fix at `c1bef30`

Trust now comes from `find_spec.__code__.co_filename` — the file the finder's own executing code
was compiled from. `co_filename` is written by the compiler from the file the code actually came
from, so it cannot be changed after the fact by naming games. A finder is an installation finder
only when the code that will run for it was compiled from a real file inside site-packages.

Verified on the live release lane that this accepts the genuine finders and refuses the smuggler:

    _virtualenv                          install=True  code=<venv>/site-packages/_virtualenv.py
    __editable___..._EditableFinder     install=True  code=<venv>/site-packages/__editable__...py
    Smuggler borrowing _virtualenv.py   install=False (its code is <stdin>/the test file)

Two details that matter:

- A **class** on `sys.meta_path` has `find_spec` called unbound, so the attribute is a plain
  function; an **instance** yields a bound method. `_finder_code_file` unwraps `__func__` for
  both, which is why it works for the editable finder.
- Code defined by an interactive session, `exec()` of a string, or a doctest has a `co_filename`
  of `<stdin>`/`<string>` and **no real file**. That is refused rather than trusted.

`_finder_source` is retained only for diagnostics and for the existing spoof test; it no longer
decides trust. `_untrusted_meta_path_finders` reports the code location, so the FAIL detail can no
longer be laundered by a finder's own `__file__` claim — `test_meta_path_finder_outside_site_
packages_is_refused` now asserts exactly that, i.e. the refused file path is *not* echoed back.

## Verification on the exact head `c1bef30`

    pytest tests/test_import_origin_guard.py                        -> 53 passed
    pytest tests/test_import_origin_guard.py test_local_ci_policy.py -> 63 passed
    ruff check / ruff format --check (both changed files)            -> clean
    git diff --check                                                -> clean

All three exploits, each a separate script, re-run after the final reformat:

    original meta-path exploit -> FAIL
    nonexistent-file spoof    -> FAIL
    real-file borrow          -> FAIL  (BYPASS: False)
    own checkout CLI          -> PASS, exit 0
    foreign worktree CLI      -> FAIL, exit 1

## Test quality — an important correction to my own process

`test_a_finder_cannot_borrow_a_real_installed_modules_file` initially **passed against the broken
implementation**. A class defined inside a test file has its real code in the test file, which is
not in site-packages, so the *other* checks rejected it and the row passed for the wrong reason —
the exact vacuity trap I had already been bitten by once.

I traced which mutation actually enables the bypass and confirmed it is pure lexical `__file__`
containment (the `bdd9786` behaviour), against which `_is_installation_finder` returns `True` and
the row fails:

    AssertionError: borrowing a real installed module's file must not confer trust
    assert not True
    1 failed, 52 deselected

The row now also asserts the *premise* directly — that the borrowed path exists, is inside
site-packages, and equals the real installed file — so it cannot pass unless that premise holds.

## Still open — not claimed as resolved

- **Preflight scope, unchanged:** the guard certifies the `sys.meta_path` snapshot at call time.
  A finder added *after* `check_origins` returns is out of scope. Two independent reviews judged
  this honestly stated in the code rather than an overclaim.
- **`sys.path_hooks`** are not enumerated. They govern path-entry resolution and cannot preempt
  `PathFinder` for a missing submodule under an already-loaded regular package, so they are not the
  reported vector. A hook able to mutate a package's `__path__` still exceeds this check.
- **The origin/module mismatch skip** in `_foreign_path_locations` remains a latent fail-open if its
  two inputs diverge; no production divergence exists in this checkout, and it is recorded rather
  than silently changed.

## Status

CI re-triggered on `c1bef30`. **Not merged** — needs a fresh independent review of `c1bef30`.
Neither prior verdict transfers: REQUEST CHANGES was against `650a8bc` and `947e3ec`.
