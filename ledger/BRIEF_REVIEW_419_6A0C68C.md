# PR candidate — #418 starred-target list binding

Date: 2026-09-30
Worktree: `/home/agent/poke-harness/.scratch/fix418`
Branch: `fix/418-starred-target-list`
Head: `6a0c68c95bd11638ee31187f3d494d95e3c94442`
Base: `6b72bf62b5722e1df2c44c988bbdb14803024032` (`origin/master`, unmodified)

**Not merged. Not pushed. Not reviewed. Release remains `PARTIAL`.**

## Diagnosis

Real defect on `origin/master`, not a coverage gap: the analyzer reported a
dead contract `enforced` (false-live, #308 criterion 1 — the damaging
direction) for every starred-target shape.

Root cause is an **ordering error inside `_entry_is_dead`**, not a missing
capability. The module already knew the answer — `_literal_runtime_type`
returns `"list"` for a `Starred`, and `NON_CONTEXT_MANAGER_TYPES` contains
`"list"`. But the general destructuring branch ran *first*:

```python
if _binds_element_of(statement, name):
    return False        # "cannot tell" -> assert stays live
```

`_binds_element_of` flattens a `Starred` (via `_store_target_names`) into the
name inside it, so a starred binding was indistinguishable from a positional
one and fell into the decline. The decline is correct for a *plain* element —
the element's type is genuinely not readable from the container's syntax — and
it errs safe. It is wrong for a starred target, where the list-wrapping is
decided by the target syntax alone and is therefore decidable.

## CPython ground truth (executed, 3.12.14)

| store | bound value | `hasattr(cs,'__enter__')` |
|---|---|---|
| `a, *cs = (suppress(), 2)` | `[2]` | False |
| `b, *c = (1, suppress())` | `[<suppress>]` | False |
| `d, *e = (suppress(),)` | `[]` | False |

Note row 2: a starred tail that *contains* a usable context manager is still a
list, so the element's own kind is irrelevant to the entry decision.

## Fix

Two additions, both keyed on target syntax and independent of the right-hand side:

- `_binds_starred_target` / `_starred_target_names` — walk target lists keeping
  only names reached *through* an `ast.Starred`.
- an `if _binds_starred_target(...)` branch placed **before** the destructuring
  decline, adding `"list"` to the kind set.

The non-starred element decline is untouched, so #413 and #417 are unaffected
and the documented in-body multi-element limit is not widened.

## Validation

| Gate | Result |
|---|---|
| `tests/test_timed_menu_milestone_sentinels.py` + `test_timed_menu_milestones.py` | **426 passed**, 0 failures, 0 errors, 0 skipped |
| new `starred_target` rows only | 5 passed |
| `ruff check tests/` (repo-pinned 0.16.9) | clean |
| `ruff format --check tests/` | 269 files already formatted |
| `git diff --check` | clean |
| differential, 21 executed shapes | **7 changed, all 7 now match CPython, 0 regressions** |
| worktree | clean; `pokemon/` tripwire intact (3 staged files, 167 worktrees) |

### Differential detail

Fixed, base wrong → candidate matches CPython: `star lone suppress`,
`star lone nullcontext`, `star lone int`, `star bind-from-end tail int`,
`star bind-from-end tail sup`, `nested star in tuple`,
`star with int before and after`.

Unchanged and agreeing with CPython: plain assigns (usable / swallowing / int),
non-starred tuple and list element binds, `star alone then reassign usable`,
`star then del`, `import-as` carrier, `def` carrier, walrus, a class *instance*
implementing the protocol, and both loop-target rows.

### Mutation evidence

| Mutant | Change | Result |
|---|---|---|
| MUT-1 | correction removed (`if False`) | **killed** — 4 failures |
| MUT-2 | starred name never recorded in the helper | **killed** — 4 failures |

**Before this change MUT-1 survived all 349 sentinel tests green.** That is
acceptance criterion 5 and it is the reason the defect outlived prior work: the
suite had no row for a starred target, so the branch was both wrong and
invisible.

## One correction worth recording

A second control row was drafted and **removed rather than shipped**:
`cs, other = (suppress(), 2)` (non-starred, suppressor element) is also
reported `enforced` while CPython swallows it. That is the **#336
element-decline family, not this issue**, and is unaffected by this change. It
was caught only because `_assert_entry_contract` *executes* each fixture
instead of trusting the expected column — the table cannot be made stale
silently. Measured ground truth for both:

| store | CPython 3.12.14 | analyzer (base and candidate) |
|---|---|---|
| `cs, other = (nullcontext(), 2)` | fires (live) | `True` — correct |
| `cs, other = (suppress(), 2)` | swallowed | `True` — pre-existing false-live, #336 family |

This should be filed or reconciled with its owning issue; it is deliberately
**not** folded in here, since #418 is scoped to the starred path and #336's
decline is the mechanism under review.

## Not done / next action

- No independent review. The lead authored this change, so lead measurement
  cannot satisfy the merge gate, and collaboration payload delivery is down
  (`BLOCKER_COLLAB_TOOLS_20260930C.md`, occurrence 30). **Unmerged.**
- Needs: exact-head independent review, then a PR, then merge with a head-SHA
  guard.
- No timing or capacity claim is made. Host load was 30 on 4 CPUs
  (CPU PSI `some avg10=90.96`), so timing-sensitive results would be
  inadmissible; the gates above are correctness-only.

---

## Reviewer instructions (added by the lead)

Materialise the **code** head, not this branch tip:

    git fetch origin
    git worktree add <scratch> --detach 6a0c68c95bd11638ee31187f3d494d95e3c94442
    git -C <scratch> rev-parse HEAD    # must print 6a0c68c...

`743bac8` is this brief on top of the code head; `git diff 6a0c68c..743bac8 --
tests/` is empty, so both name the same code.

To be answered:

1. Does `_binds_starred_target` catch every shape CPython binds to a list, and
   no shape it does not? Try nested targets, `(*cs,) = ...`, mixed positional
   before and after, and starred names inside a `for`/walrus/exception store.
2. Is placing the branch *before* the destructuring decline correct, and does
   it leave the non-starred decline — which #336, #413 and #417 depend on —
   intact?
3. Are the 5 new rows genuinely non-vacuous, and do they still fail if
   `_assert_entry_contract` stops executing the fixture?
4. Any shape where the candidate is *wrong in the damaging direction* that
   base got right? (The lead's 21-shape differential found 0, but it was
   written by the author.)
5. Is the claim "pre-existing on `6b72bf6`" independently confirmed?

Verdict line to write at the end of your report:
`VERDICT: APPROVE` / `VERDICT: REJECT` / `VERDICT: INCONCLUSIVE`

---

## Dependency note for reviewers (added by the lead)

While validating this, the lead found that the **non-starred** counterpart of
this defect — `cs, other = (contextlib.suppress(...), 2)`, where the name
receives a swallowing element — is a false-live on `master` `6b72bf6` but is
**already fixed by #405** (`d89616d9`). Measured on four trees, 4 shapes:

| tree | 4 false-live shapes | 2 controls |
|---|---|---|
| `master` `6b72bf6` | 4 wrong | correct |
| #412 `6f388cf1` | 4 wrong | correct |
| #419 `6a0c68c` | 4 wrong | correct |
| #405 `d89616d9` | **0 — fixed** | correct |

So no new issue is needed, and this **confirms the #418 fix was correctly
scoped**: leaving the non-starred decline untouched does not abandon the family,
because #405 owns it.

One reviewer obligation follows. `git merge-base --is-ancestor d89616d9
6a0c68c` is **false** — #419 is based on `master` alone and does not contain
#405. Please confirm specifically that:

- the change under review introduces no regression in the non-starred
  element path, and
- it is genuinely orthogonal to `_value_bound_by` / `_element_for_target`, so
  the two repairs compose rather than conflict.

Both repairs should be reviewed independently; the union should be re-run
before either is called complete.
