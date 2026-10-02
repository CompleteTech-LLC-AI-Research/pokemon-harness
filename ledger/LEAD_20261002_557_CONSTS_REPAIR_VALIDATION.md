# PR #557 — repair of the `co_consts` blocking finding, validated on the real merge tree

**Head:** `7fc614acd19c9c22490943b7e35d50826bc838e3` (`fix/556-compare-consts`)
**Merge tree:** `git merge-tree --write-tree origin/master 7fc614a` -> tree `5ee4852`, commit `b0ea779`
**Worktree:** `/workspace/poke-harness/.scratch/mt557` (declared, detached)
**Venv:** `/workspace/poke-harness/.scratch/lead556venv`
**Status:** OPEN, **not merged** — awaiting independent review (#489).

## What changed

`_code_matches_source` now compares a recursive `_constant_signature` in addition
to `co_name` / `co_code` / `co_names` / `co_varnames`. Two rows added, one per
direction. Full description and exploit in
`LEAD_20261002_556_BLOCKING_CO_CONSTS_COLLISION.md`.

## Verification on the exact merge tree

Merge tree `b0ea779`, not the branch worktree:

| Check | Result |
|---|---|
| `tests/test_import_origin_guard.py` + `test_fixture_provenance.py` | **119 passed, 0 failed, 0 errors, 0 skipped** |
| CLI `check_import_origins.py --project-root .` | rc=0 |
| `ruff check` both changed files | clean |
| same-shape foreign-constant bypass | hostile finder TRUSTED **False** -> **refused** |
| original forged-`co_filename` attack | forged finder trusted **False**, status **FAIL** -> **defeated** |

Counts parsed from JUnit XML. The repo sets `addopts = "-q"` and prints **no
summary line**, so a clean-looking terminal tail proves nothing.

## Wider selection — no regression, with a measured control

`-k "import_origin or packaging or fixture_provenance or artifact or schema"`:

| Tree | tests | failures | errors | skipped |
|---|---|---|---|---|
| merge tree of #557 | 205 | 7 | 0 | 1 |
| **unmodified `origin/master`** | 190 | 7 | 0 | 1 |

The **failure sets are identical** (compared test-by-test from JUnit XML): the
same 7 rows fail on both.

- `tests/test_probe_owner_phases.py` — 6 rows
- `tests/test_runtime_packaging_bootstrap.py::test_bootstrap_timeout_terminates_posix_process_descendants`

So they are **pre-existing on master and unrelated to this change**. The
205-vs-190 delta is the 15 rows #557 adds to the guard lane. None of the failing
files import or exercise `check_import_origins`.

These are part of #253 (source-mode gate red on master) and remain open; this
entry records that #557 does not add to that set.

## Mutation matrix on the repair

Guard suite, JUnit counts, on `fix556` at `7fc614a`:

| Mutant | Result |
|---|---|
| baseline | 102 tests, 0 failures |
| M1 corroboration neutered | **KILLED** |
| M2 containment check removed | **KILLED** (5 rows) |
| M3 empty-package fail-closed neutered | **KILLED** |
| M4 `_constant_signature` comparison removed | **KILLED** — 1 failure, `test_a_same_bytecode_finder_with_a_foreign_constant_is_refused`, and no other row |

M4 is the important one: the new production line is pinned by exactly the row
written for it, so the line is load-bearing and nothing else absorbs its loss.

## False-red control

`_code_matches_source` recompiles from source, so a stricter comparison risks
refusing legitimate installs. Measured against a **real** `pip install -e` of the
merge tree, not a stub:

    __editable___pokered_harness_0_1_0_finder.py   trusted=True
    _distutils_hack.DistutilsMetaFinder            trusted=True
    untrusted list                                 empty
    check_origins                                  PASS

### Corrections of my own, recorded so they are not re-derived

1. A stale `lead555ctl_paths.pth` left in the verification venv pointed at another
   worktree and produced a **second, unrelated** FAIL (`the interpreter is
   importing a different checkout`). It was my own contamination, removed before
   the numbers above.
2. A `pip install -e` performed for the false-red control later made the master
   control run fail the same way; the editable was uninstalled and the control
   re-run clean.
3. An early version of the collision probe returned a bare string instead of a
   spec, so the foreign import raised rather than loading. Rewritten to build a
   real `spec_from_file_location` before the verdict was recorded.

Each of these produced a wrong-looking number first. None of them changed a
reported result; the table above is post-cleanup.

## Not verified here

- **No ROM is present**, so no real-ROM source/native gate ran.
- **No timing tier** ran; the host is 4 cores under load 15+, and timing failures
  are not distinguishable from contention here.
- **No full `bash scripts/run_local_ci.sh`**; the wide selection above is the
  scoped evidence, plus lint, packaging and CLI on the merge tree.
- **No independent review.** Delegation is blocked by #489 (fifth confirmation).
  The author's own verification does not discharge that rule.

**Release status: PARTIAL**, unchanged.

## Retained evidence

- Probes: `/workspace/poke-harness/.scratch/lead556_probe/` (10 files)
- Venv: `/workspace/poke-harness/.scratch/lead556venv`
- Worktrees: `mt557` (merge tree), `mt557ctl` (master control), `fix556` (branch), `lead556`, `lead555ctl`
