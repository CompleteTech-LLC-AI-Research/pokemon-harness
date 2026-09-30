# Integration candidate: #379 + #392 on master

Worktree: `/home/agent/poke-harness/.scratch/integ379_392`
Candidate: `3fb1acc61d1a924c160f36dfbda5e3261ece9b90`
Base: `origin/master` = `6b72bf62b5722e1df2c44c988bbdb14803024032`
Status: committed locally, **not pushed, not merged**.

## Why this exists

#379 and #392 were both reported `CONFLICTING` against master, and each was
resolved independently. That is not sufficient on its own: they are
**siblings from a common old base**, not a stack, and both rewrite the same
region of `tests/_timed_menu_milestone_sentinel_support.py`. Landing them one
at a time would mean resolving the *same* conflict twice, and the second
resolution is not the first one re-applied.

Confirmed by ancestry:

```
$ git merge-base --is-ancestor 0845ca5 fee41bf   # -> false
$ git merge-base 0845ca5 fee41bf
0529c8a Merge pull request #360 ... #333-prename-walrus
```

Neither PR contains the other, and both branch from `0529c8a`, well behind
master.

## Who carries what

| capability | #379 `0845ca5` | #392 `fee41bf` | master |
|---|---|---|---|
| `_loop_value_source` (#348) | yes | no | no |
| `_nonlocal_suppressors` (#355) | yes | no | no |
| `BINDING_FORM_SHAPES` tests | yes | no | no |
| `settled_is_carrier` guard (#392) | no | yes | no |
| `return settled or tied` tie split (#367) | no | no | yes |

**#379 is the sole carrier of the whole binding-form family.** Merging #392
onto master first and then #379 works, but the reverse order silently drops
eight functions (`_loop_value_source`, `_value_bound_by`,
`_element_for_target`, `_loop_target_names`, `_is_store_statement`,
`_nonlocal_declared`, `_nonlocal_parent_function`, `_nonlocal_suppressors`)
and the 13-row `BINDING_FORM_SHAPES` table. A test count alone does not reveal
it -- an intermediate tree measured 442 tests, the same as #392 alone, because
the dropped rows are #379's.

This is the same failure mode as the `d89616d` finding: green by deleting
coverage.

## Resolution order used

1. #379 + master -- 4 hunks resolved, self-alias tie fix applied. Re-derived
   from a clean worktree and confirmed **byte-identical** to the validated
   `/home/agent/poke-harness/.scratch/c379` tree (4687 / 4880 lines), so the
   resolution is reproducible rather than hand-tuned. Revalidated: **443 /
   0 / 0**.
2. #392 merged on top -- 1 conflict, the same `_stores_of` region, with the
   sides **reversed** relative to the standalone #392 resolution (`ours` is
   master's tie split, `theirs` is #392's carrier guard). Resolved by keeping
   the guard's `return None` ahead of master's tie split.

## A defect ruff caught that the tests did not

The first combined splice placed #392's `return [entry for entry in decidable
...]` *after* the helper definitions that follow in master's region. It parsed
fine and the whole lane was **464 passed / 0 failed** -- the line was simply
unreachable, and no test could see it. `ruff` reported 3x `F821 Undefined name
(decidable / orders / latest)`. Removed.

Worth recording: a green suite did **not** establish correctness here. Lint
was the only signal. The combined tree is now ruff-clean *and* green.

## Fidelity

* support module: parents 115 top-level defs, merged 115, missing 0, no dupes
* no conflict markers; both files `ast.parse` clean

## Validation

* sentinel + milestone lane -- **464 passed, 0 failed, 0 errors, 0 skipped**
  (443 for #379+master, 442 for #392+master; 464 is the union)
* `ruff check` on both files -- clean
* mutation, #379 self-alias fix reverted -- **1 failure**
  (`a self-alias after a loop target keeps the element`)
* mutation, #392 carrier guard reverted -- **4 failures**
  (`test_a_conditional_store_superseding_a_local_carrier_is_declined`)

Both fixes survive integration and are independently pinned by the combined
suite.

## Not done

* Not pushed, not merged. The independent-review gate is closed (see
  `REVIEW_CHANNEL_20260930_FORK_ALL_PROBE.md`): both `fork_turns: "none"` and
  `fork_turns: "all"` dispatch paths fail to deliver a brief, so no reviewer
  can be reached.
* No `reviewDecision` exists on any open PR.
* release remains **PARTIAL**.
