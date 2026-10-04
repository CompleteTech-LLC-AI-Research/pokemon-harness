# #89 deliberately_excluded scope guard — measured evidence

Date: 2026-10-04
Base: `e815c6d2356fea0465018fd9a06a095f8b39c04e` (master, PR #574 merge)
Head: PR #565 `f35554b6`, merged into current master for validation
Worktree: `/workspace/poke-harness/.scratch/wt573-rebase`
Branch: `review565` (local merge of `origin/fix/89-excluded-scope-guard` + `origin/master`)

#565 was based on the older base `b5302d0` and could not merge cleanly as
authored, so `origin/master` was merged in to validate it against the current
tree. The merge is conflict-free and touches only the two files the PR changes.

**Independently reviewed.** Verdict `MERGEABLE` from the external review
harness (`deepseek-v4.1-flash`) against the exact diff reviewed here. This
supersedes the "not independently reviewed" note in the PR body, which was
written while #489 blocked sub-agent delivery.

## The hole

`_validate_move_effects` bounded `deliberately_excluded` from below but not from
above. A family that owns real moves could be re-scoped to
`deliberately_excluded` and still validate, provided the editor moved
`planned_unverified_count` and `deliberately_excluded_count` with it. Every
counter then agrees while the outstanding-mechanics set has silently shrunk --
the damaging direction for #89, where unverified mechanics would read as "out
of scope" rather than "not yet qualified".

## Measured baseline (no pre-existing defect)

```
87 families = 67 planned_unverified + 19 deliberately_excluded + 1 tested
deliberately_excluded == _PINNED_UNUSED_EFFECT_IDS  -> True (19 ids)
```

## The mutation the old checks could not see

```
donor = first planned_unverified family with moves  ->  effect 2 (POISON_SIDE_EFFECT1), 1 move
donor.scope = "deliberately_excluded"; both counters decremented/incremented
counters consistent -> planned 67->66, excluded 19->20, tally verified

before: accepted silently
after:  CoverageError: effect 2 owns moves in the pinned table,
                         so it must not be deliberately_excluded
```

## Exhaustive scope sweep (independent of the PR author's claim)

Every one of the 87 families reassigned to each of the other two scopes with all
three counters recomputed: **174 attempts, 173 rejected**. The single
acceptance is `tested -> planned_unverified` on effect 0, which is the correct
fail-closed direction: it drops a verified claim rather than inventing one.

## Four bypass attempts, all rejected

| attempt | rejection |
|---|---|
| strip `move_ids` from a move-owning family, then exclude it | `move-to-effect mapping does not match the pinned table (missing=[40])` |
| reassign the orphaned move to another family, then exclude the donor | `move-to-effect mapping does not match the pinned table (wrong=[...])` |
| delete the family entry for effect 2 outright | `family_count does not match the family list` |
| rename effect 2 to a fresh id and exclude it | `effect families must cover every pinned effect id exactly once` |
| exclude **all** move-owning families in one edit | new branch: `effect 0 owns moves in the pinned table ...` |
| park move 40 on a pinned-unused family and exclude effect 2 | `move-to-effect mapping does not match the pinned table (extra=[40])` |

The pins are tool-owned in `scripts/_coverage_report_schema.py` and are never
derived from the supplied catalog, so an editor cannot re-scope its way out.

## Gates on the merged tree

```
pytest tests/test_battle_coverage_catalog.py          -> 7 passed
pytest -k "coverage or battle_scenario or catalog or local_ci or release"
                                                         -> 330 passed, 1 skipped, 8219 deselected
ruff check  (both changed files)                       -> All checks passed!
ruff format --check (both changed files)               -> 2 files already formatted
```

The single skip is pre-existing and unrelated. `330 passed` is up from the PR
body's `316` because #574's policy tests are now in the selection.

## Review findings and disposition

Independent review returned `MERGEABLE` and raised two non-blocking notes:

1. `test_validator_rejects_excluding_a_family_that_owns_moves` selects its
   donor with a bare `next(...)`, so if the catalog ever lost its last
   move-owning `planned_unverified` family the test would **error**
   (`StopIteration`) rather than pass vacuously. An error is still a loud
   failure, not a silent pass, so this is a maintenance hazard rather than a
   correctness hole. Accepted as-is: an explicit skip would be worse, since it
   would let the guard go untested exactly when the catalog changed.
2. The error text says "owns moves in the pinned table" while the branch is
   actually keyed on `effect_id not in _PINNED_UNUSED_EFFECT_IDS` -- a strict
   superset of "owns moves". Correct as written for the case that reaches it;
   the review confirmed the set-equality claim in the comments holds given the
   surrounding guards. Not changed.

## Not claimed

This closes one validator hole. **#89 stays open.** Its remaining leaves
(89.2, 89.3, 89.5) require real-ROM battle mechanics qualification, and nothing
here touches that. No release readiness is claimed.
