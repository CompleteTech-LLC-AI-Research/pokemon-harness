# #558 round 8: a planted `.pth` certified provenance — found while reviewing #551

Date: 2026-10-02
Branch: `lead/556-const-collision` → PR #558
Repair head: `722ecb2` (predecessors `ccb8982`, `594c8fa`, `613428b`, `0d7d848`)
Verified against `origin/master` `9500c97`
Status: **BLOCKING — repaired, awaiting independent review. Not merged.**

## How this was found

Delegation delivered no task for the eleventh time, so `#558` still has no
independent review. `#551` (`origin/lead/551-combine-553`, head `5c272ae`) is
authored by a different author, so reviewing *it* is legitimate under
"An author may not independently approve their own change" — I am not its
author. That review is the work below, plus two findings in `LEAD_20261002_551_INDEPENDENT_REVIEW.md`.

The first thing that review turned up was a bypass that hit `#558` too.

## The bypass

The second provenance channel asks whether some `*.pth` in the same
site-packages directory contains `import <stem>`. A `.pth` is **plain text**, so
that test is satisfied by writing the file.

Measured end to end against `ccb8982`:

```
_is_imported_by_a_pth: True
TRUSTED:     True
guard status: PASS
foreign submodule loaded: foreign
*** FULL BYPASS via attacker-written .pth ***
```

Identical result against `#551` `5c272ae`. Writing one line of text was enough;
nothing about the `.pth` distinguished a planted activation from a real one.

This promotes the guard's own stated residual boundary — *"an attacker who can
write a file into this interpreter's site-packages ... is out of scope"* — from
holding to a full false PASS. That boundary was the reason the design was
considered safe; the `.pth` channel is exactly what an attacker uses to step
over it.

## Fix

A `.pth` counts only when the activation file was **itself** laid down by an
install. The discriminator was already in the file: the environment's real
`distutils-precedence.pth` is listed in a `RECORD` with its hash, a planted one
is not.

```
pth files in .../lead555venv/lib/python3.11/site-packages
  distutils-precedence.pth: "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; ..."
     pth itself RECORD-attested: True
  rep557_paths.pth: <scratch binding>
     pth itself RECORD-attested: False
```

No false red: `DistutilsMetaFinder` stays trusted (it is `RECORD`-attested
through `_distutils_hack`), and the suite is green.

## Evidence

| Check | Result |
|---|---|
| Guard + provenance suites on exact merge tree vs `origin/master` `9500c97` | 189 passed |
| Attack probes: spec forgery, copied-source `exec`, raising `__file__`, `RECORD` CSV, **planted `.pth`** | all defeated |
| Real `sys.meta_path` finder (`DistutilsMetaFinder`) still trusted | yes |
| `ruff check` / `ruff format --check` | clean |
| CLI `scripts/check_import_origins.py` | rc=0 |

Merge-tree verification: fresh `git worktree add --detach origin/master`, then
`git merge --no-ff lead/556-const-collision` — clean automatic merge; shared venv
`.pth` repointed for the run and restored after.

## Cumulative mutation matrix for #558

| Mutation | Row killed |
|---|---|
| Remove `_finder_was_imported_from` from trust decision | copied-source row |
| Restore unguarded `getattr(module, "__file__", None)` | raising-getter row |
| Strip `try/except` from `_finder_module` | hostile-metaclass row |
| Provenance returns `True` unconditionally | copied-source + spec-forgery rows |
| `RECORD` digest comparison → membership test | RECORD-bytes row |
| Drop CSV unquote of quoted names | quoted-CSV row |
| `all(...)` → `any(...)` over claims | contradicting-record row |
| `all(...)` → keep-last | contradicting-record row |
| `all(...)` → keep-first | *not killed — a real conflict refuses either way* |
| **Honour any `.pth` naming the module** | **planted-`.pth` row** |

## Open, and now worse for #551

The planted-`.pth` bypass is **still present on `origin/lead/551-combine-553`
`5c272ae`**, as is the `RECORD` CSV-quoting false red. Both were reported on
that PR. `#551` needs the same two repairs before it can be merged.

## Disposition

Author cannot approve own change, and delegation still delivers no task
(eleventh failure of #489). **No independent review of `#558`'s `722ecb2`.
`#558` stays open and unmerged.**

What *was* obtained this round is an independent review of the *other*
branch: see `LEAD_20261002_551_INDEPENDENT_REVIEW.md`.
