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

---

# Addendum, same day — #421 stacked, and a blocking defect found in it

## #421 on top of the resolved tree

`165b5b3d` auto-merged clean (no conflict) onto the `aaf31e5` tree. Sim commit
`8b12532`.

The 9-row #418/#412 probe stayed green on the new tree. The full suite did
not: **710 tests, 130 failures**.

## Root cause is #421's own code, not the conflict resolution

`_binds_starred_target` applied `name in` to
`_starred_names_in_loop_target(...)`, which already returns a `bool`. Every
non-`ast.Assign` store therefore raised
`TypeError: argument of type 'bool' is not iterable`.

Isolation evidence, because this could easily have been misattributed to my
resolution:

| tree | `_binds_starred_target(for *cs, in ...)` |
|---|---|
| #419 head `318d9a2c` | `False` — clean |
| #421 head `165b5b3d` | `TypeError` |
| simulation, fixed | `True` |

#421's own suite is green on its own head — **358 tests, 0 failures, 0 errors,
0 skipped** — because the crashing branch has no coverage. Its green CI does
not clear it.

One-line fix applied in the simulation only. Result: **710 tests, 0 failures,
0 errors, 0 skipped** (`/tmp/simint_final.xml`). Sim commit `961e586`.
`ruff check tests/` clean.

Filed on the PR as a blocking comment:
<https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/421#issuecomment-5904792850>

Full write-up: `ledger/FINDING_421_BOOL_MEMBERSHIP_20260930.md`.

## Refreshed live state

- `origin/master` **unmoved** at `6b72bf62b5722e1df2c44c988bbdb14803024032`.
- **18 open PRs, 82 open issues** (was 81; #425 opened since the last refresh).
- **All 18 PRs have `reviewDecision` empty — zero reviews anywhere.**
- #392 (`fee41bfbd`) and #379 (`0845ca56a`) are `CONFLICTING/DIRTY`.
- The other 16 are `MERGEABLE/CLEAN`.
- #421's required hosted check reports **pass** in 7m42s while the head is
  broken. That is the concrete demonstration that an empty or green hosted
  rollup is not the gate.

## New issues, all in the same family, none fixed by this tree

- **#425** — a loop target's surviving non-enterable element is not checked
  after the loop. This is the same gap `deliv_probe` reported as the 3
  remaining false-LIVEs in `_single_loop_element`.
- **#423** — a starred store in a loop body is not read when the `with` is a
  later sibling. #421 claims to fix this; my #421 probe covered the loop-*body*
  row via its own tests, but #423's exact framing has not been re-verified
  against the fixed tree.
- **#422** — a suppressor reached through a subscript or attribute is never
  resolved. Fixed by nothing on the board.
- **#417** — a user-defined suppressor as a completed loop's LAST element.

## Still blocked, and it is still only this

**No independent review verdict exists for any head.** `list_agents`,
`wait_agent`, and `spawn_agent` are again returning `unsupported call`; the
`send_message` pointer at `REVIEW_TASK_SIMINT_RESOLUTION.md` produced no
reply. That is the 36th dropped dispatch, recorded in
`INCIDENT_DISPATCH_TEXT_DROPPED_36TH_20260930.md`.

So: nothing merged, no PR marked ready, no issue closed. Release remains
`PARTIAL`. The simulation is now four PRs deep and fully green, which is the
strongest position available without a reviewer.

---

# Addendum 2 — three blocking findings, and one new issue

The simulation itself is complete and green, but the exercise turned up
**three defects on PRs I was preparing to integrate**, and one of them is a
regression in the damaging direction. None is fixed upstream; all are filed.

## F1 — PR #421: `TypeError` on every non-`ast.Assign` store

`_binds_starred_target` applied `name in` to a helper that already returns a
`bool`. 130 failures on the combined tree; #421's own 358-test suite is green
because the branch has no coverage.

Filed: <https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/421#issuecomment-5904792850>
Detail: `FINDING_421_BOOL_MEMBERSHIP_20260930.md`

## F2 — PR #405: introduces a **false-DEAD**

`for cs in (1, contextlib.nullcontext()): pass` then `with cs:` — CPython
fires the assert, #405 reports it dead. #405 fixes the two non-enterable rows
of #425 but over-corrects past the control. Its own suite is **362 green**.

Filed: <https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/405#issuecomment-5904886121>
Detail: addendum in `VERIFY_423_NOT_FIXED_20260930.md`

**Consequence for merge order:** #425 must not be closed by merging #405. That
would trade a weaker defect (false-live) for a stronger one (false-dead).

## F3 — PR #412 / #424: a false-DEAD regression at `b90f985` — NEW ISSUE #429

An `elif` arm's store is treated as unconditionally reached, so

```python
cs = contextlib.nullcontext()
if x: pass
elif True: cs = list()
with cs: assert x != 1
```

is reported **dead** where CPython **fires**. Bisected exactly:
`5535135` correct → `b90f985` wrong → wrong through the rest of the stack.

The regression lives inside a commit whose own message says it was fixing
false-deads, and survived six further review rounds plus the #392 merge. All
669 tests pass with it present.

Filed: <https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/issues/429>
PR comments: [#412](https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/412#issuecomment-5905026690),
[#424](https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/424#issuecomment-5905027249)
Detail: `FINDING_412_B90F985_FALSE_DEAD_20260930.md`

This is the one that matters most. #412 was the prerequisite for the whole
stack, and it is not mergeable as it stands.

## Also verified this round

- **#423** — real, still open on every tree including the simulation. Filed
  measured evidence on the issue.
- **#425** — real, still open on the simulation. See F2.
- **#417** — reproduces its own table exactly, both controls green. Still open.
- **#416** — **stale**: #406 head `eb7c15f7` answers the shape correctly. Not a
  live defect, but should stay open until confirmed on the merge tree.

## A methodology error worth recording

My first #417 probe used a stand-in `Sup` class whose `__exit__` tested
`isinstance(exc[0], AssertionError)`. `exc[0]` is the exception *class* and
`exc[1]` the instance, so the class did not suppress at all, and the probe
reported all three rows **live** — contradicting a correct issue.

Caught by executing the fixture under real CPython (the assert propagated when
the issue says it is swallowed) and by re-reading the issue's own `return True`
contract. Rewriting the probe from the issue's fixture reproduced the issue's
table exactly.

The generalisable lesson: a probe's stand-in object must do what the issue's
does, or the probe measures a different question. This is the same class of
error as the malformed fixtures recorded earlier in this ledger, and it is the
one most likely to manufacture a false "disproved".

## Review transport

Three `spawn_agent` calls this session. One child answered the wrong task
visibly; two produced **silence**. `list_agents` and `wait_agent` continue to
return `unsupported call`. A returned task name is not evidence; silence is
not a pending approval.

Zero of 18 open PRs has an independent review. **Nothing merged. No PR marked
ready. No issue closed.** Release remains `PARTIAL`.

---

# Final state, end of run

## Board

- **18 open PRs. 88 open issues.** (Issues rose 81 → 88 during the run, mostly
  filed by other agents on this board.)
- **Zero of 18 PRs has any review.** `reviews` is empty on every one.
- #392 and #379 remain `CONFLICTING/DIRTY`; the other 16 `MERGEABLE/CLEAN`.

## What was accomplished

1. **Finished the interrupted #412→#424→#419 simulation merge** that the prior
   turn left half-resolved and mislabelled. The inherited hand-resolution had
   dropped #419's starred block from `_entry_is_dead` entirely, so all four
   #418 rows still reported `enforced`. Restored, positioned correctly, and
   proven: **706 tests, 0 failures, 0 errors, 0 skipped**.
2. **Stacked #421** on top — clean auto-merge, **710 tests, 0 failures, 0
   errors, 0 skipped** after a one-line repair.
3. **Found and filed three blocking defects**, none of which any CI had
   caught:
   - **#421** — `TypeError` on every non-`ast.Assign` store. 130 failures on
     the combined tree; green on its own head.
   - **#405** — introduces a **false-DEAD**, so #425 must not be closed by
     merging it.
   - **#412/#424** — a **false-DEAD regression** at `b90f985`, bisected
     exactly, inside a commit whose own message says it was fixing
     false-deads. Filed as **new issue #429**.
4. **Re-measured four open issues** independently rather than trusting their
   reported numbers: #423 and #425 confirmed open with CPython ground truth,
   #417 reproduced exactly, **#416 found stale** (its PR's current head is
   correct).

## What was not accomplished, and why

**Nothing was merged.** The single blocker is that no independent review is
obtainable: four `spawn_agent` calls this session, one child visibly answered
the wrong task and two produced silence, while `list_agents`, `wait_agent`, and
`spawn_agent` themselves began returning `unsupported call`.

Separately, and independently of that: **#412 is not mergeable even with a
reviewer**, because of the `b90f985` false-dead now filed as #429. That is a
substantive finding, not a process excuse.

## Honest assessment of the merge queue

The stack I prepared is four PRs deep and fully green, and I had a concrete,
measured, well-evidenced path to merging it. That path is closed by the
review transport, and the stack's own prerequisite (#412) is closed by #429.
Those are two different blockers and both are real.

The most valuable thing this run produced is probably not the simulation but
the four defects: two of them (#429, #405) are in the **damaging** direction,
and one of those sat inside a commit explicitly written to fix that direction,
surviving six review rounds and every subsequent CI run.

## Cleanup

Removed 13 bisection worktrees created during this run
(`bs_*`, `iso405b`, `iso406b`). Retained: `simint` (the simulation),
`fix413_loopelem`, `audit412`. Pre-existing worktrees untouched.

Protected checkout verified intact at the end: branch
`lead/259-widen-lint-lanes`, HEAD `bd2c167cb1364550da9520e7a596704e28ed0894`,
exactly the three staged files, `stash@{0}` preserved.

`origin/master` unmoved at `6b72bf62b5722e1df2c44c988bbdb14803024032`.

**Release remains `PARTIAL`.**
