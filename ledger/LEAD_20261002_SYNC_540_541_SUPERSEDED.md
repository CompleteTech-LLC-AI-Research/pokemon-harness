# #540 / #541 superseded by merged #542 — sync verification at `9d2321e`

Date: 2026-10-02. Author: lead. This is a sync record, not a review verdict
and not an approval of either open PR. The substance of both open PRs is
already on `master`; what remains open is duplicate delivery, not missing work.

## Authoritative identities at check time

| item | value |
|---|---|
| `origin/master` | `9d2321e` — `Merge pull request #542` |
| local `master` | `9d2321e` (`git rev-list --left-right --count master...origin/master` = `0 0`) |
| PR #537 | merged as `27f1059`, closes #534 |
| PR #542 | merged as `9d2321e`, base `27f1059` |
| PR #540 | OPEN, head `e2b15b56`, base `27f1059` (one merge behind) |
| PR #541 | OPEN, head `47a41705`, base `27f1059` (one merge behind) |

Both open PRs are `base.sha = 27f1059`, i.e. they predate the `9d2321e` merge.
Each is `ahead=1 / behind=2` against current master.

## What #542 already landed

`tests/test_import_origin_guard.py` on master carries both changes:

1. `test_suite_hook_actually_calls_the_import_origin_guard` (line 797) — pins
   the `_enforce_local_import_origins()` call **inside** `pytest_configure`.
   This is the substance of #540's `test_suite_hook_calls_the_origin_guard` and
   #541's `test_pytest_configure_enforces_local_import_origins`, which are two
   differently-named rows for the same single mutant.
2. `test_capacity_planning_ignores_the_nodeidless_origin_row` (line 940) now
   builds `planned_row` from `test_is_within_resolves_before_comparing` — the
   second half of #541, replacing the never-existing
   `test_is_within_is_not_a_prefix_test` label.

## Non-vacuity proof (the part that decides supersession)

Superseding a PR on the claim "master already has it" would be worthless if
master's row did not actually kill the mutant. It does.

Environment: worktree `/workspace/poke-harness/.scratch/sync9d2321e`, detached
at `origin/master` `9d2321e`. Interpreter `venv-534guard` (Python 3.11.2,
pytest 9.1.1), bound to this checkout by
`PYTHONPATH=<root>/src:<root>/vendor/pyboy-src` — the checker admits a
`PYTHONPATH` checkout as this checkout's origin, so the run is legitimate
rather than an accident.

First, the guard proves it is live and the shared-venv hazard is real:

    $ scripts/check_import_origins.py --project-root .      # unbound venv
    status: FAIL
      pokered_harness -> /workspace/poke-harness/.scratch/wt534/guard/src/...
      pyboy           -> /workspace/poke-harness/.scratch/wt534/guard/vendor/pyboy-src/pyboy/...
    status: PASS                                        # with PYTHONPATH bound

Guard suite at master, counts from `--junitxml` (`addopts="-q"` hides them):

    tests/test_import_origin_guard.py   tests=38 failures=0 errors=0 skipped=0
    (37 pre-#542 + the row #542 added)

Mutation, applied in place and reverted (`git checkout -- tests/conftest.py`,
worktree confirmed clean afterwards):

    tests/conftest.py:51  _enforce_local_import_origins()  ->  pass
    => tests=38 failures=1 errors=0 skipped=0
       FAILED tests/test_import_origin_guard.py::test_suite_hook_actually_calls_the_import_origin_guard

That is precisely the mutant #540 and #541 each exist to kill, and it is killed
by the row already on master. The three rows carrying the two PRs' intent all
pass together on master: `tests=3 failures=0 errors=0 skipped=0`.

## Hosted CI on the two open heads (already run, both real passes)

    #540  head e2b15b56  run 36993967331  completed/success
          PASS import-origins  unit total=8082 passed=8082 failed=0 skipped=0 xfailed=0 xpassed=0 errors=0  overall: PASS
    #541  head 47a41705  run 36994684041  completed/success
          PASS import-origins  unit total=8082 passed=8082 failed=0 skipped=0 xfailed=0 xpassed=0 errors=0  overall: PASS

Both are genuine single-workflow successes with real `import-origins` rows, not
an empty check rollup. So CI is not the reason to close them; duplication is.

## Disposition

Close #540 and #541 as **superseded by #542**, not as failures. Neither carries
scope that master lacks, and merging either would add a redundant near-duplicate
row for the same mutant. No code change is proposed here.

Recorded limits:

- No independent review was obtained for #540/#541. None is needed for a
  supersession close, but it is also not claimed as a gate they passed.
- The #542 merge itself has no GitHub-filed review (`pulls/542/reviews` is
  empty). Its content was reviewed on the #534 lineage per
  `ledger/REVIEW_537_27f1059_indep.md`; the row it added is verified above.
- No real-ROM qualification is claimed; no ROM assets exist here. Release
  status stays PARTIAL.
