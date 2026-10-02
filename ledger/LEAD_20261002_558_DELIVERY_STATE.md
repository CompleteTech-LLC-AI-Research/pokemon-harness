# Delivery state for the #534 import-guard stack, 2026-10-02

Ledger-only. Records what is open, what carries what, and what is blocking.

## Supersession

PR #558 (`lead/556-const-collision`, head `0b819d9`) supersedes #555, #556 and
#557 as the single carrier for the import-origin guard work. It is a superset,
not a sibling:

```
git merge-base --is-ancestor 89c7152 HEAD  -> yes   (#555 included)
git merge-base --is-ancestor 187b44a HEAD  -> yes   (#556 included)
git merge-base --is-ancestor 7fc614a HEAD  -> no    (#557 superseded)
```

#557 is superseded rather than included because its constants-only signature is
strictly weaker than the nested-code fold in `0b819d9`. Carrying it would merge
a head whose central claim is known to be bypassable.

Diff against `origin/master` at `0b819d9`:

```
 docs/PRODUCTION_RUNBOOK.md        |  25 +
 scripts/check_import_origins.py   | 513 ++++++++++++++++++--
 tests/test_import_origin_guard.py | 949 ++++++++++++++++++++++++++++++++-
 3 files changed, 1437 insertions(+), 50 deletions(-)
```

## Open PRs and their state

| PR | Head | Draft | Mergeable | Reviews | Note |
|---|---|---|---|---|---|
| #558 | `0b819d9` | no | MERGEABLE | 0 | supersedes #555/#556/#557 |
| #557 | `7fc614a` | no | MERGEABLE | 0 | superseded by #558 |
| #556 | `187b44a` | no | MERGEABLE | 0 | included in #558 |
| #555 | `89c7152` | no | MERGEABLE | 0 | included in #558 |
| #551 | `c2a6b47` | no | UNKNOWN | 0 | separate scope |
| #549 | `33cc0e5` | yes | UNKNOWN | 0 | draft |
| #548 | `1d4c9e1` | no | UNKNOWN | 0 | separate scope |

None of these may merge without an independent review of its exact head.

## Blocking findings recorded against this stack

1. `LEAD_20261002_556_BLOCKING_CO_CONSTS_COLLISION.md` — #556 omits `co_consts`
   entirely. Measured full bypass.
2. `LEAD_20261002_557_BLOCKING_NESTED_CODE_GAP.md` — #557's constants-only
   signature still admits a nested-code twin. Measured full bypass, with that
   PR's suite green at 119 passed at the time.

Both are discharged in code by `0b819d9`, subject to independent review.

## Blocker: independent review is unavailable

The objective requires that an author may not approve their own change, and
that a PR be independently reviewed before merge. Delegation has failed
repeatedly (#489, six recorded attempts, most recently
`LEAD_20261001_REVIEW_BLOCKED.md`): `spawn_agent` accepts a task and the child
lifecycle runs, but the child does not receive the assignment. Two children
fabricated work and one never opened the review brief it was given.

This run re-attempted delegation for #558. Whether the child actually receives
and performs the review is recorded below once it returns.

### Seventh occurrence, confirmed on this run

The child was spawned with `fork_turns="none"` and a self-contained brief
naming the exact worktree, the two files, the commit, the venv to run with, the
attacks to try, and the handoff format.

It returned a report about `max_concurrent_threads_per_session` and
`max_depth` in `/home/agent/.codex/config.toml` and four
`.codex-account-*` files. That is unrelated to the import-origin guard, and it
was not a question that had been asked. It created no probe directory
(`/workspace/poke-harness/.scratch/rev558_probe/` does not exist), ran no test,
and returned no finding about `_const_signature`, `_code_matches_source` or
`_is_installation_finder`.

The child did correctly leave the code under review untouched: no tracked
modifications appeared in `/workspace/poke-harness/.scratch/rep557` and the
head is still `0b819d9`. So the failure is argument transport, not filesystem
damage, which matches the earlier six attempts.

Its output is not treated as a review and is not counted toward the independent
approval #558 needs.

Lead self-review does not satisfy the objective and is not offered as a
substitute.

## What was verified on `0b819d9`

Worktree `/workspace/poke-harness/.scratch/rep557`, dedicated venv
`/workspace/poke-harness/.scratch/lead555venv`:

| Check | Result |
|---|---|
| guard + fixture provenance | 120 passed, 0 failed, 0 errors |
| genuine/editable/install rows | 27 passed |
| nested-code bypass probe | hostile TRUSTED False, guard FAIL |
| const-collision bypass probe | hostile TRUSTED False, guard FAIL |
| forged `co_filename` (`exploit`) | ATTACK DEFEATED |
| forged `co_filename` (`exploit2`) | ATTACK DEFEATED |
| real `DistutilsMetaFinder` trusted | True, untrusted list empty |
| own checkout `check_origins` | PASS |
| CLI `--project-root .` | rc=0 |
| `ruff check` both files | clean |
| mutant: nested fold removed | 1 failed, 102 passed (row kills it) |

## Verification on the candidate merge tree

Repeated on the exact tree that merging `0b819d9` into `origin/master`
produces, not only on the branch head. Worktree
`/workspace/poke-harness/.scratch/merge558a`, detached at `origin/master`
(`0c0c6fd`) with `origin/lead/556-const-collision` merged in; the merge was
clean. The venv `.pth` was rebound to this tree for the run and restored to
`rep557` afterwards.

Note the repository's own guard refused the first attempt, correctly: the venv
was still bound to `rep557`, so results would have described another tree.

| Check | Result |
|---|---|
| guard + fixture provenance | 120 passed, 0 failed, 0 errors |
| nested-code bypass probe | hostile TRUSTED False, guard FAIL |
| const-collision bypass probe | hostile TRUSTED False, guard FAIL |
| forged `co_filename` (`exploit`) | ATTACK DEFEATED |
| forged `co_filename` (`exploit2`) | ATTACK DEFEATED |
| CLI `--project-root .` | rc=0 |
| `ruff check` both files | clean |
| mutant: nested fold removed | 1 failed, 102 passed, exactly one row |
| restored | 103 passed |

The merge worktree was removed after the run.

## Completeness audit of the signature

The question an independent review would ask first is whether the signature is
behaviorally complete, or whether an uncompared `co_*` field still lets two
different functions compare equal. Checked directly:

- `async def` against plain `def` with an otherwise identical body: already
  distinguished, because `co_flags` differs and that changes `co_code`.
- A free variable against a fast local with the same body: distinguished,
  `LOAD_DEREF` and `LOAD_FAST` differ in `co_code`.
- Two closures over the same name whose nested bodies differ: `co_code` at the
  top level is identical, and the pair is caught by the nested fold. This is
  exactly the case #557 missed, and it is the case the new row pins.
- A corpus sweep of 28 `find_spec` shape variants produced 12 pairs the guard
  treats as identical. **None** of them differed in any uncompared field
  (`co_flags`, `co_freevars`, `co_cellvars`, `co_argcount`,
  `co_posonlyargcount`, `co_kwonlyargcount`, `co_nlocals`, `co_stacksize`,
  `co_qualname`). On this evidence no additional field is load-bearing.

This is the author's own audit and is offered as evidence, not as the
independent approval the objective requires.

## Closure sequence for #534

Only after #558 has an independent approval and is merged:

1. Rerun the focused post-merge checks on the resulting master.
2. Close #557 and #556 as superseded by #558, linking the carrier.
3. Close #555 as included in #558.
4. Close #551, #548, #549 only when their own full scope is demonstrably
   landed; a partial PR must not close its parent.
5. Close #534 only when every acceptance row is terminal.
6. Recheck the #547 and #554 closure conditions.

Release status stays PARTIAL. Most remaining issues still require a real-ROM
result or an operator-controlled CPU allocation that is not available here.

## Subsequent work on the same head

### `ef61d3d` — PEP 263 decoding, a false FAIL found by comparing with #551

#551 solves the same decision differently. Comparing the two found a defect in
this branch: `read_text(encoding="utf-8")` raises `UnicodeDecodeError` on a
genuine latin-1 module, and the blanket `except` turned it into a refusal.
`tokenize.open` plus `compile(..., dont_inherit=True)` fixed both that and the
`CO_FUTURE_ANNOTATIONS` inheritance problem. Pinned by
`test_a_genuine_latin1_finder_is_still_corroborated`, which fails alone when
reverted.

### `4cf5a61` — adopt #551's full field set

`_const_signature` became `_code_signature` and now compares every
behaviour-bearing field at every level of nesting: `co_flags`, `co_argcount`,
`co_posonlyargcount`, `co_kwonlyargcount`, `co_nlocals`, `co_freevars`,
`co_cellvars`, alongside `co_name`/`co_code`/`co_names`/`co_varnames` and the
constant table. `co_firstlineno`, `co_linetable` and `co_qualname` are
deliberately excluded as behaviour-neutral.

This closes the omitted-field question structurally rather than empirically.

The nested mutation guard had to be sharpened in the same commit. Renaming made
its assertion satisfiable by the top-level fields alone, so it stopped guarding
the nested half — an earlier mutation attempt passed it. It now asserts that
the nested constant tables are equal and that every non-recursive field is
equal, so the decision provably rests on the nested recursion.

An earlier mutation attempt in this session was itself faulty: it removed the
nested field lines but kept the recursive call, so it tested nothing. Redone
correctly, it reproduces #557's shape exactly and is bypassable end to end
(`TRUSTED: True`, guard `PASS`, foreign submodule loaded), and the nested row
kills it alone: `1 failed, 103 passed`.

| Check on `4cf5a61` | Result |
|---|---|
| guard + fixture provenance | 121 passed, 0 failed, 0 errors |
| genuine/editable/install/latin-1 rows | 28 passed |
| nested-code bypass | TRUSTED False, guard FAIL |
| const-collision bypass | TRUSTED False, guard FAIL |
| forged `co_filename` (`exploit`) | ATTACK DEFEATED |
| forged `co_filename` (`exploit2`) | ATTACK DEFEATED |
| own checkout `check_origins` | PASS |
| CLI `--project-root .` | rc=0 |
| `ruff check` both files | clean |

Hosted CI on `0b819d9` completed `success` (run 37057990673). CI on `4cf5a61`
was in progress when this entry was written.

### Still outstanding

The #551 consolidation itself is not done. #551's `__path__` containment scope
for a regular package is real scope that #558 does not contain, and it is the
reason #551 exists. Carrying it across means porting roughly 1500 lines of a
differently structured rewrite of the same guard, which is a larger change than
the field-set adoption above and deserves its own verification pass.

The independent review blocker is unchanged: #558 still has zero reviews.
