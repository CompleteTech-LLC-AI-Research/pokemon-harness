# #558 round 5: the `__spec__` provenance check was itself bypassable

Date: 2026-10-02
Branch: `lead/556-const-collision` → PR #558
Repair head: `594c8fa` (predecessors `613428b`, `0d7d848`)
Verified against `origin/master` `9803fd1`
Status: **BLOCKING — repaired, awaiting independent review. Not merged.**

## Why this round exists

`613428b` supplied load provenance by asking `sys.modules[name].__spec__` to
name the finder's file. That was ported from #551's *earlier* attempt. #551
itself later found the approach forgeable, in round 4. This round took that
escape seriously, reproduced it against our own head rather than trusting the
description, and replaced the channel.

## The bypass, measured against `613428b`

```
premise spec.has_location: True
premise content matches:   True
TRUSTED:    True
guard status: PASS
foreign submodule loaded: foreign
*** VERDICT: FULL BYPASS ***
```

Construction: build a module with `types.ModuleType`, attach a spec from
`importlib.util.spec_from_file_location` — which sets `has_location=True` — and
every in-memory signal the check relied on is satisfied while the import system
never loaded anything from that file. Nothing about `__spec__` distinguishes
this from a real install.

This is the fifth route by which "the code matches a site-packages file" has
been shown not to imply "the running finder came from it".

## Replacement: provenance decided on disk

Records a running process cannot rewrite into a different claim:

* a distribution's `RECORD` lists the file with a SHA-256 of its contents, so
  the bytes the finder runs from are the bytes an install wrote; or
* a `.pth` file in the same site-packages directory imports the defining module
  by name — how `_virtualenv` and the `__editable__` shim load at all.

## Mutation finding

Replacing the `RECORD` digest comparison with a bare membership test
(`_record_digests(root).get(path) is not None`) **left every row in the suite
green** (109 passed). The hash was not load-bearing as it stood, because
presence in a `RECORD` already said only "an install laid this path down".

Added `test_a_recorded_file_stops_being_recorded_when_its_bytes_change` to pin
the difference. Under that mutation it is now the only failing row.

## Rows retargeted rather than weakened

Provenance is decided from install records, so a source file a test drops into
site-packages is by construction not one any install vouched for. Two existing
rows needed to change:

* `test_a_genuine_install_finder_is_not_refused_by_the_source_corroboration`
  now iterates the finders this interpreter actually has installed
  (`sys.meta_path` minus stdlib), as #551 does.
* `test_a_genuine_finder_with_nested_code_still_matches_its_source` now targets
  the *content* channel it was always about — `_code_matches_source` — and
  builds its source outside the checkout. It never needed install provenance.

Neither row was loosened: each still asserts the direction it was written for.

## Rows added this round
- `test_a_forged_module_and_spec_do_not_certify_an_uncertified_finder`
- `test_a_recorded_file_stops_being_recorded_when_its_bytes_change`

## Evidence

| Check | Result |
|---|---|
| Guard + provenance suites on exact merge tree vs `origin/master` `9803fd1` | 186 passed |
| `tests/test_import_origin_guard.py` alone | 111 passed |
| Attack probes: spec forgery, copied-source `exec`, raising `__file__` | all defeated |
| Real `sys.meta_path` finder (`DistutilsMetaFinder`) still trusted | yes |
| `RECORD` entries parsed in the working venv | 2648 in site-packages, 1062 in `~/.local` |
| `ruff check` / `ruff format --check` | clean |
| CLI `scripts/check_import_origins.py` | rc=0 |

Merge-tree verification: fresh `git worktree add --detach origin/master`, then
`git merge --no-ff lead/556-const-collision` — clean automatic merge. The shared
venv `.pth` was repointed to the merge tree for the run and restored after.

## Mutation matrix (cumulative for #558)

| Mutation | Row killed |
|---|---|
| Remove `_finder_was_imported_from` from trust decision | copied-source row |
| Restore unguarded `getattr(module, "__file__", None)` | raising-getter row |
| Strip `try/except` from `_finder_module` | hostile-metaclass row |
| Provenance returns `True` unconditionally | copied-source + spec-forgery rows |
| `RECORD` digest comparison → membership test | RECORD-bytes row |

## Disposition

Author cannot approve own work. **No independent review of `594c8fa`.** #558
stays open and unmerged. Delegation has returned unrelated reports seven times
(issue #489), so this repair is lead-authored and does not satisfy the
independent-approval requirement.

Note for reconciliation: `origin/lead/551-combine-553` has converged on the same
disk-recorded design via its own review rounds. #558 is ahead of it on the
`__file__` crash and the `_finder_module` boundary. Whichever lands first, the
other must rebase rather than merge both, since both rewrite the same function.
