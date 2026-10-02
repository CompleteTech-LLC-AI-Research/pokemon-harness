# #558 merge-tree verification against current master

Date: 2026-10-02
Merge candidate: `origin/master` `b368691` + `9084db8`, merge commit `ab1a867`
Worktree: `/workspace/poke-harness/.scratch/merge558` (detached, removed after)
Status: **merge is clean and the tree is green. Not merged — no independent review.**

## Merge

```
$ git worktree add --detach .scratch/merge558 origin/master      # b368691
$ git merge --no-ff 9084db8
Automatic merge went well; stopped before committing as requested
```

Clean, no conflicts, no manual resolution.

## Guard on the exact merge tree

The shared venv's `.pth` was repointed at the merge tree for the run and
restored afterwards (`rep557_paths.pth` verified back to the `rep557` paths).

| Condition | Result |
|---|---|
| venv bound to the merge tree, `--project-root` = merge tree | `"status": "PASS"`, rc=0, both packages PASS |
| the same venv, `--project-root` = a tree it does not import | `"status": "FAIL"`, rc=1, `resolves outside this checkout` |

Both directions, on the merge candidate itself — AC4's "fail on mis-pointed,
pass on repaired, both retained".

A second true positive worth noting: running the merge tree's guard under
`pokemon/.venv` (which correctly imports `pokemon`) also FAILs, naming
`pokemon/src/pokered_harness/__init__.py` as the origin. That is correct
behaviour, not a regression — it is the exact #534 condition, detected on a
tree it was not written for.

## Tests on the merge tree

```
$ pytest tests/test_import_origin_guard.py tests/test_fixture_provenance.py
rc=0, 130 tests, 0 failures, 0 errors
```

## What this does and does not establish

It establishes that `#558` merges cleanly onto current master and that the
guard behaves correctly in both directions on the resulting tree.

It does **not** discharge the review requirement. Every open #534 PR —
#548, #549, #551, #555, #556, #557, #558 — is authored by `CompleteDotTech`,
the account I push from. An author may not approve their own change, and
GitHub rejects `--request-changes` on these PRs for that reason. Delegation
failed again this session (13th probe, including the file-path workaround that
#489 recommends), so no independent reviewer has been reached.

## Disposition

- Nothing merged, nothing marked ready.
- #534 is **not** closed: its criteria are met by the merge candidate, but the
  PR carrying them is unreviewed.
- Release remains `PARTIAL`.
