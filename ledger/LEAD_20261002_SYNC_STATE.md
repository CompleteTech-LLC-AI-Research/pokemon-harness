# Sync state — `master` = `9d2321e`, PR queue drained

Date: 2026-10-02. Author: lead. Request: "sync with main".
The project checkout is `/workspace/poke-harness/pokemon`; the workspace root
is not a Git repository, and the trunk branch is `master` — there is no `main`.
"Sync" therefore means bringing local checkout and branch state into agreement
with the live remote and resolving whatever the remote revealed.

## Final state

| item | value |
|---|---|
| `origin/master` | `9d2321e866074f11361f0bf0592bc5ff887d8de0` |
| local `master` | `9d2321e` (`git rev-list --left-right --count master...origin/master` = `0 0`) |
| open PRs | **0** (was 2 at the start of this sync) |
| open issues | 29 |
| #534 | `closed`, `state_reason=completed`, closed `2026-10-02T10:30:00Z` |

## What the sync resolved

Master had already absorbed the #534 lineage while this session's handoff
summary was being written — the summary was stale in exactly that direction:

1. **PR #537 merged** as `27f1059` (guard itself: import-origin preflight,
   `tests/conftest.py` hook, `direct_url.json` provenance, native-lane
   admission, shared `collected_nodeids` reader, strict staging root).
2. **PR #542 merged** as `9d2321e`, adding the row that pins the
   `pytest_configure` call and repairing the `planned_row` label.
3. **#534 closed** as completed.

Two PRs were still open against the now-stale base `27f1059`: **#540**
(`e2b15b56`) and **#541** (`47a41705`). Both passed hosted CI on their own
heads, and both proposed a fix that #542 had already landed under a different
test name.

## Action taken

Closed **#540** and **#541** as superseded by #542, with the replacement
linked, full supersession evidence, and the non-vacuity mutation check posted to
each PR:

- https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/540#issuecomment-5951440000
- https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/541#issuecomment-5951451327

Discipline applied: superseded, **not** "not planned" — neither was a failed
change, both were duplicates of landed scope.

## Scope check — nothing from either closed PR is left unlanded

`git diff origin/master origin/<branch>` on the single changed file shows the
only differences are the test's name, its docstring, and its local double. Both
branches would also **regress** `master` by restoring the dead
`test_is_within_is_not_a_prefix_test` label that #542 deliberately replaced.

    fix/534-pin-conftest-hook   (PR #540)  NOT in master — superseded by #542
    fix/534-review-followups    (PR #541)  NOT in master — superseded by #542
    fix/534-import-origin-guard (PR #537)  LANDED
    fix/534-pin-conftest-guard  (PR #542)  LANDED

## Verification on the merged tree `9d2321e`

Worktree `/workspace/poke-harness/.scratch/sync9d2321e`, detached at
`origin/master`. Interpreter `venv-534guard` (Python 3.11.2, pytest 9.1.1),
bound to the checkout via
`PYTHONPATH=<root>/src:<root>/vendor/pyboy-src`.

- `scripts/check_import_origins.py --project-root .` -> `status: PASS` when
  bound; `status: FAIL` for both packages when the same interpreter is pointed
  at an unbound checkout, naming
  `/workspace/poke-harness/.scratch/wt534/guard`. The shared-venv hazard #534
  describes is live on this host, so the guard is not vacuous.
- `tests/test_import_origin_guard.py` -> `tests=38 failures=0 errors=0 skipped=0`
  (37 pre-#542 rows plus the row #542 added).
- Mutation `tests/conftest.py:51` -> `pass` -> `tests=38 failures=1`, killed by
  `test_suite_hook_actually_calls_the_import_origin_guard`. Reverted with
  `git checkout --`; worktree verified clean afterwards.
- Hosted CI on the merged head, `9d2321e`, run **36997257886**,
  `completed/success`, `head_sha` matches the merge exactly. I verified this
  run independently rather than relying on the claim in a PR comment.

## Housekeeping

`/tmp` is a 512 MB tmpfs and had reached **94%** (35 MB free); a `git archive`
copy of the tree failed with `No space left on device`. Freed ~56 MB by
deleting only disposable, re-derivable copies of this same #540/#541/#542
lineage (`/tmp/leadverify_1790937759`, `/tmp/mutcheck540`, `/tmp/hook537`,
`/tmp/repro_indep_27f1059`, and this session's own log extracts). **No
repository worktree, branch, ledger, or retained evidence was deleted.** The
large pre-existing `/tmp` population (~129 MB `/tmp/lead541` and ~40 MB
`/tmp/verify540`) is other sessions' evidence and was left untouched. Subsequent
mutation work was done in place in the detached worktree, which needs no copy.

## Not claimed

- No real-ROM qualification is claimed for any of this. No ROM assets exist in
  this workspace, so no real-ROM row was ever run. **Release status stays
  PARTIAL.**
- No independent review was obtained for #540/#541. Not needed for a
  supersession close, and not claimed as a gate they passed.
- #542's merge carries no GitHub-filed review (`pulls/542/reviews` is empty).
  Its content was reviewed on the #534 lineage per
  `ledger/REVIEW_537_27f1059_indep.md`; the row it added was re-verified here.
- The remaining 29 open issues are untouched by this sync and unchanged in
  state. Several remain blocked on things this workspace cannot supply (a
  declared CPU allocation, ROM assets). They were deliberately not addressed
  and are not claimed as resolved.
