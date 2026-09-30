# Ledger — PR #433, head `7846110`, validation of record

Date: 2026-09-30
PR: https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/433
Fixes: #429 (open)
Worktree: `/home/agent/poke-harness/.scratch/fix429`
Branch: `fix/429-elif-arm-is-conditional`
Base before the fix: `63ab8dd`
Head: `7846110`
Interpreter: `/workspace/.venv-ci/bin/python` (CPython 3.11.2)

## Scope

The **code** fix is 152 lines across exactly two files. Everything after
`39f1b19` is ledger only.

    git diff --stat 63ab8dd..7846110 -- tests/
     tests/_timed_menu_milestone_sentinel_support.py | 56 ++++++++++++++
     tests/test_timed_menu_milestone_sentinels.py      | 96 ++++++++++++++++++++++
     2 files changed, 152 insertions(+)

Commits `d5dcd92`, `6fa5ddc`, `925bb42`, `775c858`, `7846110` are all
`ledger/` only. Verified by `git show --stat` on each.

## Validation on this exact head

| check | command | result |
|---|---|---|
| full sentinel + milestones | `pytest tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py -q` | **714 tests, 0 failures, 0 errors, 0 skipped** |
| JUnit | `/tmp/verify429_head.xml` | tests=714 failures=0 errors=0 skipped=0 |
| mutation | `if _elif_link_is_conditional(...)` -> `if False:` | **killed** |
| lint | `ruff check` both touched files | All checks passed |
| format | `ruff format --check` both touched files | 2 files already formatted |

### Mutation specificity

The mutant is killed by exactly one test:

    test_an_elif_link_is_reached_only_when_every_test_above_failed[
        an elif link with a literal-true test]

and by no control. So the guard is load-bearing **and** narrow: a one-line
revert is caught, and a blanketing change is not needed.

## Two adversarial rows investigated and dismissed as PR defects

Both were resolved against **executed** CPython ground truth. Neither is a
regression from this PR, and neither prompted a code change.

### Case C — the probe's expectation is wrong

Three-link chain, store in the 2nd `elif`. Ground truth:

| call | result |
|---|---|
| `x=0, y=0` | returns normally |
| `x=1, y=0` | **`AssertionError` — the header is entered** |

So the correct verdict is `True`, which is what the tool returns. The probe
only ever executed the call where the store does not run. Recorded as a bad
expectation rather than dropped, because this failure mode has produced
false greens in this repo before.

### Case E — pre-existing, safe direction, filed as #434

`if False: pass` / `elif True: cs = list()` then `with cs: assert x != 1`.
Ground truth: `TypeError` on entry, so the assert is genuinely dead and the
correct verdict is `False`; the tool answers `True`.

Measured **identically on parent `63ab8dd` and head `7846110`**, so this PR
neither introduced nor worsened it. Root cause is the deciding test being
undecidable (`not x` is neither decidable-true nor decidable-false), not the
`elif` handling this PR repairs. A false-LIVE inflates the enforced count
but never certifies a defeated contract as pinned, so it cannot cause the
criterion-1 damage a false-DEAD can. Filed as **#434**; the local finding is
renamed to match.

## Scope differential

Commit `775c858` records a parent-vs-head differential confirming the answers
for #423, #425 and #417 are unchanged by this fix.

## Independent review: NOT obtained

**Zero reviews.** The collaboration transport dropped the task text for the
**33rd** recorded instance; `followup_task` returned `unsupported call`, and
`send_message` with the brief path inline was accepted but never answered.
A returned task name is not a verdict. The author of this PR is the lead
integrator and **may not approve their own change**.

See `ledger/INCIDENT_DISPATCH_TEXT_DROPPED_33RD_20260930.md`.

## Current status

- #433 `7846110`: OPEN, not draft, MERGEABLE, **0 reviews**
- #429: **OPEN**
- #434: **OPEN** (false-LIVE family, safe direction, pre-existing)
- `master`: `6b72bf62` — **unmoved**
- Release: **PARTIAL**

## Smallest next action

Obtain a substantive independent verdict on head `7846110` using the brief at
`ledger/REVIEW_TASK_429_FIX.md`. If it is approved, mark #433 ready and merge
with a head-SHA guard. If any finding is actionable, repair, re-review the new
exact head, and do not transfer approval from `39f1b19`.
