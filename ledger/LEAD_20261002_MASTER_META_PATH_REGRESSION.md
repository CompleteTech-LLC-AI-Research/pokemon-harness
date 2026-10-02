# Blocker: master no longer has the meta-path finder guard

## What happened

PR #553 (`lead/548-unresolvable-paths`) merged to master as `f6a95f6`. That
branch was cut before the meta-path hardening existed, so the merge replaced
`scripts/check_import_origins.py` with a version that has no `sys.meta_path`
handling at all.

Current `origin/master` (`42450bc`):

```
grep -c meta_path scripts/check_import_origins.py   ->  0
grep -n "_untrusted_meta_path_finders" ...          ->  no match
```

Every symbol the #551 work introduced is gone: `_trusted_stdlib_finders`,
`_is_trusted_stdlib_finder`, `_finder_code_file`, `_finder_source`,
`_is_installation_finder`, `_untrusted_meta_path_finders`.

The fix is not lost, it is simply unmerged. It exists on two refs, neither of
which is an ancestor of master:

- `bdd9786` (and its successors `947e3ec`, `c1bef30`) on `fix/534-regular-package-path-portion`, PR #551.
- `1346a69` on `lead/551-combine-553`.

`git merge-base --is-ancestor bdd9786 master` -> false.
`git merge-base --is-ancestor 1346a69 master` -> false.

## The blocker is live on master

The original reproduction, run against current master with the dedicated venv:

```
guard status: PASS
imported file: .../foreign/probe_pkg/leaked.py
ORIGIN: foreign
LEAKED_FOREIGN_AFTER_PASS: True
VERDICT: BLOCKER_LIVE
```

The guard certifies an interpreter while a custom `sys.meta_path` finder
serves `probe_pkg.leaked` from outside the checkout. This is the exact failure
#551 exists to close, and it is back on trunk.

## Consequence for the review history

The three-round review of `bdd9786` -> `947e3ec` -> `c1bef30`, and the
`c1bef30` mutation matrix, all describe a guard that master does not have. The
review work is still valid and still needed; it simply has to be re-applied on
top of the #553 lineage rather than merged into a master that already contains
it.

PR #551 cannot be merged as it stands. Its head predates `f6a95f6` and carries
the older guard; merging it would revert #553's unresolvable-path work along
with #552/#551/#546/#545. The correct shape is a combine branch that carries
both: the #553 unresolvable-path handling *and* the #551 meta-path hardening,
with the meta-path trust decision preserved exactly as reviewed at `c1bef30`.

`lead/551-combine-553` already appears to be an attempt at that combine and
carries `1346a69`. It needs the same audit: mutation matrix, independent
review, and a check that the two guards coexist rather than shadow each other.

## Status

- Master regression confirmed and reproduced.
- #551 blocked on rebase onto the #553 lineage.
- This is a master regression, not a defect in `c1bef30`.
