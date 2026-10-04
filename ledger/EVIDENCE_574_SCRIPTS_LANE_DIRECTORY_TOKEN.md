# #567 scripts/ lane directory token — measured evidence

Date: 2026-10-04
Base: `fb2d489494bdca0d5ce5af462a413ab8f2658ba0` (master, PR #572 merge)
Head: `cb4206544b1ebd631033154f6b73c6dcce94f049` (PR #574)
Worktree: `/workspace/poke-harness/.scratch/wt573-rebase`
Branch: `fix/567-lane-residue-v3`
Supersedes PR #573 (same scope; branch was rewritten by the rebase and
force-push is not permitted here)

Independent review was performed via the external review harness
(`deepseek-v4.1-flash`) across two rounds. This document records the author's
own measurements and the review outcomes; it does not substitute for them.

## The hole being closed

The main Ruff lanes enumerated `scripts/*.py` one-by-one, so any new script
shipped outside the lint and format gates until a human added it by hand. Both
`scripts/run_local_ci.sh` and `.github/workflows/release-hygiene.yml` now pass
the `scripts` directory token.

## Defects found after the rebase, and fixed

### 1. Silent zero from a `str` bound to `paths` (`9fa9d9a9`)

`_ruff_lint_resolved_files` gained a `paths` parameter in #570. Two call sites
still passed a bare string. `*paths` unpacks a `str` character by character, so
Ruff resolved nothing and returned an **empty set instead of raising** — the
caller read "no scripts are covered" while measuring nothing. The helper now
raises `TypeError` naming the tuple it expects:

```
$ python -c "from tests.test_local_ci_policy import _ruff_lint_resolved_files; _ruff_lint_resolved_files('scripts')"
TypeError: paths must be a tuple of path tokens, not a bare string: pass ('scripts',), not 'scripts'
$ ... _ruff_lint_resolved_files(('scripts',), tree='scripts')   -> 113 files
```

Also fixed in the same commit: six files left with I001 by the docstring move
(one blank line each), and comments claiming the pinned fixture producer was
"still lint-checked" when it is in neither lane.

### 2. `exclude` discarded Ruff's defaults; `force-exclude` did nothing (`5a195ce5`)

Found by review round 1, confirmed by direct experiment against the installed
Ruff. `exclude` is **not additive** — it replaces the built-in defaults:

```
config with only:  exclude = ["tree/producer.py"]
  ruff check tree --show-files  ->  tree/.venv/mod.py, tree/build/out.py, tree/keep.py
config with no `exclude` key:
  ruff check tree --show-files  ->  tree/build/out.py, tree/keep.py, tree/producer.py
```

`force-exclude` only affects paths named **explicitly** on the command line.
Both main lanes pass a directory token, where an exclusion already applies:

```
exclude only,  directory token   ->  keep.py            (producer absent)
force-exclude, directory token   ->  keep.py            (producer absent)  # no change
exclude only,  explicit path     ->  producer.py        (NOT excluded)
force-exclude, explicit path     ->  No Python files found
```

So `force-exclude` was never what held the producer out of the lanes; kept, it
would also silently drop any future lane naming that file directly. The entry
is now folded into the existing `extend-exclude` list:

```
extend-exclude = ['vendor/pyboy-src', 'scripts/produce_battle_state_fixtures.py']
```

Behavior after the change, measured on head: `ruff check scripts --show-files`
resolves 113 files with the producer absent; no `.venv`/`build` path appears in
either lane's resolved set. Note that declaring a *second* top-level
`extend-exclude` key would have been a TOML parse error that breaks every Ruff
invocation — caught by `tomllib.loads` during this work.

### 3. Comments named the wrong key (`cb420654`)

Review round 2 found the same `exclude` vs `extend-exclude` confusion surviving
in the carve-out comments of `scripts/run_local_ci.sh` and
`.github/workflows/release-hygiene.yml`. A maintainer trusting
"`exclude` drops it from the check lane" could relocate the entry into an
`exclude` key and reintroduce defect 2.

## Lint cleanliness of `scripts/` was fixed, not suppressed

`ruff check scripts` on the real base reports **5** errors, not the 113 that
#573's description claimed (that figure was measured on the older `b5302d0`).
All 5 are fixed at the source in `scripts/check_pyboy_components.py`:

| finding | repair |
|---|---|
| B023 loop-variable binding | `def symlink(path, stem=stem)` |
| `lines.index([...][0])` (x2) | `next(ln for ln in lines if ...)` |
| nested `with` | merged into one `with a, b:` |

Zero `noqa` and zero `per-file-ignores` are introduced by this PR.

## Measured gates on head `cb420654`

```
ruff check scripts tests --no-cache            -> All checks passed!
ruff format --check scripts tests --no-cache   -> 448 files already formatted
bash -n scripts/run_local_ci.sh                -> OK
git diff --check origin/master..HEAD           -> clean
tomllib.loads(pyproject.toml)                  -> OK, one extend-exclude key
pytest tests/test_local_ci_policy.py \
       tests/test_stepping_loop_profile.py     -> 50 passed
pytest focused 3-file policy suite             -> 155 passed
```

## Teeth

Dropping `scripts` from the main check lane in **both** CI files turns five
independent guards red:

```
FAILED test_main_ruff_lanes_cover_every_script_file
FAILED test_matrix_benchmark_is_linted_by_every_main_ruff_lane
FAILED test_main_ruff_lanes_run_in_executable_control_flow
FAILED test_main_ruff_lanes_stop_the_local_runner_on_failure
FAILED test_probe_module_and_its_tier_are_part_of_the_ci_contract
```

The #570 lane-wide suppression guard
(`test_no_lane_file_opts_out_of_linting_with_a_blanket_directive`) still passes
and still re-measures all 34 selective allowances.

## Full-suite failures on this host are environmental

Verified against a clean worktree at `origin/master` with its own venv, not
assumed:

- 16 failures are `OSError: Read-only file system: '/dev/shm/...'`.
  multiprocessing allocates its arena there and the mount is `ro` in this
  container; `TMPDIR` does not help because the arena path is not configurable.
- Failure sets for the affected files are **byte-identical** between master and
  this branch, and every failing file is untouched by this PR.
- Three rows differ between full-suite and isolated runs
  (`test_mcp_timed_remote_owner.py`, `test_session_timed_execution.py`); they
  fail on master under load and pass in isolation on both trees. They are
  deadline-sensitive, not broken here.

## Not claimed

No release readiness. Real-ROM qualification is untouched and remains
outstanding. `scripts/produce_battle_state_fixtures.py` remains excluded from
both lanes — it was named by neither lane on master, so this is not a coverage
regression, and
`test_excluded_fixture_producer_still_matches_the_manifest_sha1` pins its SHA-1
against the fixture manifest independently. `src/` stays enumerated in the
format lane (19 files are not format-clean yet).
