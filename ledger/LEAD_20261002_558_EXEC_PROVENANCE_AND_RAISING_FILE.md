# #558: load provenance and a raising `__file__` — two more false PASSes

Date: 2026-10-02
Branch: `lead/556-const-collision` → PR #558
Repair head: `613428b` (predecessor `0d7d848`)
Verified against `origin/master` `5f6e7e8`
Status: **BLOCKING — repaired, awaiting independent review. Not merged.**

Both defects below were reproduced live against `0d7d848` before any edit. The
method that found them was porting #551's probe rows, which #558 did not yet
carry.

## 1. Copied-source `exec()` finder was fully trusted

Attack: write the finder's own source into a real site-packages file, then
`exec(compile(source, that_path, "exec"))` without ever importing it.

```
ROW2 premise co_filename==planted: True
ROW2 TRUSTED: True
ROW2 guard status: PASS
ROW2 foreign submodule loaded: foreign
```

A full false PASS. The content channel proved only that equivalent code
*exists* in the named file, never that the running finder was *loaded from*
it. Those are different claims and the gap between them is exactly the bypass.

Fix: `_finder_was_imported_from()` requires the defining module to be a real
`sys.modules` entry whose spec has `has_location is True` and whose resolved
`origin` equals the finder's code file. A bare `exec` produces no such entry.

## 2. Raising `__file__` getter crashed the guard

```
ROW1 CRASH RuntimeError: getter boom
```

The `getattr(module, "__file__", None)` in `_resolve_origin` sat outside the
guard's exception boundary, so a module overriding `__getattribute__` made the
raise escape `check_origins` as a traceback — a crash instead of the FAIL that
names the offending package.

Fix: wrapped, returning `None` plus a `__file__ could not be read: ...` finding.
`KeyboardInterrupt`/`SystemExit` still propagate.

## 3. Latent crash in `_finder_module` (pre-existing in `0d7d848`)

`_finder_module` read `finder.__module__` with no exception boundary. Nothing on
the pre-existing trust path called it, so no row covered it. The new provenance
check does route through it, which surfaced the crash:

```
RuntimeError: metaclass refuses to name itself
```

`test_a_hostile_finder_metaclass_cannot_abort_the_guard` went red on first run
and had to be fixed alongside. Recorded here because the *exposure* is new even
though the *hole* predates this work.

## 4. Two existing rows asserted the attack being closed

`test_a_genuine_install_finder_is_not_refused_by_the_source_corroboration` and
`test_a_genuine_finder_with_nested_code_still_matches_its_source` constructed
their "genuine" finder with `exec(compile(source, path))` — correct content, no
provenance — and asserted it was trusted. Both now `import_module` the finder
genuinely. This is the honest fix for the trusted direction; loosening the guard
was not an option.

## Rows added
- `test_a_module_whose_file_attribute_raises_becomes_a_finding`
- `test_source_copied_into_site_packages_does_not_certify_an_execed_finder`
- `test_a_foreign_portion_that_cannot_be_rendered_is_still_reported`

## Evidence

| Check | Result |
|---|---|
| Guard + fixture provenance suites on exact merge tree vs `origin/master` `5f6e7e8` | 184 passed |
| `tests/test_import_origin_guard.py` alone | 125 passed (was 122) |
| Attack batteries: nested-code twin, const-collision, forged `co_filename`, drifting `__file__`, foreign `__path__`, namespace portions | 16 passed |
| Real `sys.meta_path` finder (`DistutilsMetaFinder`) still trusted | yes |
| `ruff check` / `ruff format --check` | clean |
| CLI `scripts/check_import_origins.py` | rc=0 |

Merge-tree verification used a fresh `git worktree add --detach origin/master`
plus `git merge --no-ff lead/556-const-collision`, clean automatic merge, then
the suite above. The shared venv `.pth` was temporarily repointed to the merge
tree and restored afterwards.

## Mutation matrix

| Mutation | Row killed |
|---|---|
| Remove `_finder_was_imported_from` from trust decision | copied-source row |
| Restore unguarded `getattr(module, "__file__", None)` | raising-getter row |
| Strip `try/except` from `_finder_module` | hostile-metaclass row |

## Disposition

Author cannot approve own work. **No independent review of `613428b` yet, so
#558 stays open and unmerged.** Delegation has returned unrelated reports seven
times (issue #489); this repair was therefore produced and verified by the lead
alone, which does not satisfy the independent-approval requirement.

Pre-existing, unrelated, left alone: `No module named 'mcp'` during collection
in the shared venv. Confirmed absent on the `0baf52e` (#551) worktree too.
