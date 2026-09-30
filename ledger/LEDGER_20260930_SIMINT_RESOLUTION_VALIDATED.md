# Ledger — #412 + #424 + #419 conflict resolution, lead-validated

Date: 2026-09-30
Status: **LEAD-VALIDATED, NOT INDEPENDENTLY REVIEWED. NOT MERGED.**
Release impact: none. Release remains `PARTIAL`.

## What this is

A **simulation** of stacking three PRs whose branches all edit the same
function. Nothing here is pushed, merged, marked ready, or closed. `master`
is unmoved at `origin/master` = `6b72bf62b5722e1df2c44c988bbdb14803024032`.

## Worktree and heads

| item | value |
|---|---|
| protected checkout | `/home/agent/poke-harness/pokemon`, branch `lead/259-widen-lint-lanes`, HEAD `bd2c167cb1364550da9520e7a596704e28ed0894` — untouched |
| simulation worktree | `/home/agent/poke-harness/.scratch/simint` (detached) |
| sim HEAD | `aaf31e5` |
| sim parent | `7540e29` = merge of #424 `590ca17f` |
| grandparent | `c19603f` = merge of #412 `6f388cf` |
| merged in this sim | #419 `318d9a2c361b20fd5a8bb999ecbbaa16f0ef3e79` |

## The defect found in the inherited hand-resolution

The prior turn left a partial hand-resolution of the #419-after-#412 merge
that was **wrong**, and recorded it as merely "incomplete". It was worse:
conflict hunk 1 had dropped #419's `_binds_starred_target` block from
`_entry_is_dead`'s store loop **entirely**. The helper function existed in the
file, so a grep for it looked healthy, but nothing in the store loop ever
called it, so all four #418 rows still reported `enforced`.

The prior turn's direct probe reported `True` for the three #418 shapes and
attributed it to a still-unknown defect further down the
`_is_enforced` → `_entry_is_dead` → `_stores_of` path. That inference was
wrong in a way worth recording: the prior turn's own probe *fixture* was also
malformed (it had no walrus rebind and inconsistent indentation), so it could
not have produced a trustworthy verdict for any row — including the
`cs = suppress()` and `cs = int()` rows it reported as passing or failing.
I hit the same trap and caught it the same way: rewriting the probe from the
test module's own fixture template made `cs = suppress()` and `cs = int()`
come out wrong, which is what exposed that the earlier numbers were not
measurements of the real contract.

## The resolution now in `aaf31e5`

Conflict hunk 1, `_entry_is_dead` store loop. #419's block restored in full,
including its `continue`, placed:

1. after #412's `_builtin_constructor_kind` analysis (so `cs = int()` still
   reads as dead), and
2. before the `_binds_element_of` destructuring decline (so the non-starred
   control `cs, other = (nullcontext(), 2)` still reads as live).

Order is load-bearing. The starred check must precede the destructuring
decline, because that decline exists to answer "cannot tell" and keep a live
assert live; a starred target is decidable and must not be swallowed by it.

Conflict hunk 2, support-module body. #412's newer body kept; #419's
`_binds_starred_target` and `_starred_target_names` helpers retained.

## Lead-measured results on `aaf31e5`

Direct probe, 9 rows, fixture taken from the test module's own
`test_a_starred_target_binds_a_list_so_the_assert_is_unreachable` template
(walrus rebind included). **9/9 as expected, 0 failures.**

| rebind | analyzer | expected |
|---|---|---|
| `*cs, = (contextlib.suppress(AssertionError),)` | `False` | `False` |
| `first, *cs = (1, contextlib.suppress(AssertionError))` | `False` | `False` |
| `a, *cs = (contextlib.suppress(AssertionError), 2)` | `False` | `False` |
| `*cs, = (contextlib.nullcontext(),)` | `False` | `False` |
| `*cs, = (1,)` | `False` | `False` |
| `cs, other = (contextlib.nullcontext(), 2)` — CONTROL | `True` | `True` |
| `cs = int()` | `False` | `False` |
| `cs = nullcontext()` — CONTROL | `True` | `True` |
| `cs = suppress()` | `False` | `False` |

Both #412's constructor rules and #419's starred rules therefore coexist; the
two controls confirm neither over-corrects.

Full sentinel + milestone suite, this exact tree:

```
tests/test_timed_menu_milestone_sentinels.py
tests/test_timed_menu_milestones.py
/home/agent/poke-harness/pokemon/.venv/bin/python -m pytest -q \
  --junitxml=/tmp/simint_412_424_419.xml
```

**706 tests, 0 failures, 0 errors, 0 skipped** (read from the JUnit XML, not
from the exit line). Progression across the stack, for context:

| tree | tests |
|---|---|
| #412 alone (`6f388cf`) | 669 |
| #412 + #424 (`590ca17f`) | 701 |
| #412 + #424 + #419 (`aaf31e5`) | 706 |

The +5 is #419's four `STARRED_TARGET_ENTRY_UNREACHABLE_ROWS` plus the new
test function, consistent with #419's own delta.

`ruff check`: clean. `ruff format --check`: 2 files already formatted.
Module parses under `ast`.

Host caveat: load was 30–35 on 4 CPUs throughout. Per the standing host rule
**no timing-sensitive measurement is admissible from this run.** The counts
above are pass/fail counts and are unaffected; nothing here is a benchmark.

## Review status

**No independent review verdict exists.** `spawn_agent` returned
`/root/simint_res_reviewer`; that child received the shared AGENTS.md settings
block instead of the task text and replied asking what to do. That is the
**36th** recorded dropped-dispatch instance. The returned task name is **not**
evidence of anything. A `send_message` pointing at
`ledger/REVIEW_TASK_SIMINT_RESOLUTION.md` was sent and produced no reply
before the transport began returning `unsupported call` for
`list_agents` and `wait_agent`.

## Blockers

1. **Independent review is not obtainable.** The collaboration transport
   flapping is the only reason this is unmerged. The brief is written and
   complete; a later dispatch needs only its path.
2. The stack is not a single PR. #412 is 32 commits carrying #375, #388, #392,
   #370, #316, #410, #385. #424 adds one commit on top. #419 is a separate
   base. Any merge needs approval for the exact resulting head, not for the
   three commits named in the individual issues.
3. #421 (on #419) is not yet in this simulation, and #421/#371/#405 were
   previously measured as fixing a *different* disjoint family. #421 has not
   been simulated on this tree.

## Next action, smallest first

1. Simulate #421 onto `aaf31e5` and run the same 9-row probe plus the full
   suite. Do not merge anything yet.
2. Keep retrying independent review of `aaf31e5` and of the real PR heads
   `6f388cf`, `590ca17f`, `318d9a2c`, `165b5b3d`.
3. Refresh `gh pr list` and `gh issue list --limit 500` immediately before any
   merge; the last refresh was 18 PRs and 81 issues.

## Not done, deliberately

No merge. No push. No PR marked ready. No issue closed. No branch or worktree
deleted. No review self-approved. No fabricated evidence. `master` unmoved.
