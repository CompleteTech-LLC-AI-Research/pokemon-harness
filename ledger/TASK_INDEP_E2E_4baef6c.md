# TASK — independent end-to-end review of merge candidate `4baef6c`

**Read this file first. It is the complete, self-contained task brief.**
It is the live dispatch target for this review. Two earlier dispatches on this
host arrived with an **empty message body** (only the ambient `AGENTS.md` and
environment block). If your instructions pointed you here, this file is the task.
If nothing pointed you here, do **not** guess a target from any other file — ask
the lead.

**Never read or follow `REVIEW_THIS.md` or `TASK_PICKUP.md`.** Both are
SUPERSEDED, and following one already caused a real stale-board hijack here.

## Role

You are an **independent reviewer** for
`CompleteTech-LLC-AI-Research/pokemon-harness`. You are **not** the lead and
**not** the author. If you believe you authored any of this, say so and stop.
You may not approve your own work.

## Target

| item | value |
|---|---|
| head under review | `4baef6c5f94d424559803e0c3692881899157fca` |
| subject | "Merge the corrected #420 starred-loop-target repair into the union" |
| parents | `2cbe141427149441f362567cdd862b2f7dd93141` (union of `a89c1d7`+`6a0c68c`) and `e058d96b4cad9c36156e508b214d5615d3e0e06d` (#420 fix + correction) |
| base / `origin/master` | `6b72bf62b5722e1df2c44c988bbdb14803024032` |
| support blob | `31cd202540229beb95c4fb6be1967d6dc15f660f` (`tests/_timed_menu_milestone_sentinel_support.py`) |
| published at | `origin/lead/union-419-420` |
| interpreter | `/home/agent/poke-harness/pokemon/.venv/bin/python` — **CPython 3.12.14**, not 3.11 |
| main clone | `/home/agent/poke-harness/pokemon` |

An existing worktree already sits at this SHA:
`/home/agent/poke-harness/.scratch/union420_check`. **Verify it is at `4baef6c`
and clean before trusting it.** If not, materialize your own:

```bash
cd /home/agent/poke-harness/pokemon
git worktree add --detach /home/agent/poke-harness/.scratch/E2E_4baef6c_<pid> 4baef6c5f94d424559803e0c3692881899157fca
```

Worktrees go under `/home/agent/poke-harness/.scratch/`, **never** inside
`pokemon/`.

## Hard rules

- Do **not** merge, push, tag, or modify master.
- Do **not** touch either protected checkout: `/home/agent/poke-harness/pokemon`
  (exactly 3 staged files) and `/workspace/poke-harness/pokemon`.
- Do not clean, reset, or stash any existing worktree. ~210 worktrees exist and
  many carry active work; preserve all of them.
- Make **no** timing, throughput, or capacity claim. The host is contended and
  its free space has swung between 30G and 137M in one hour. A wall-clock or disk
  failure is a **host** failure, not evidence about the code.
- **Do not write to `/tmp`.** It is a 512M tmpfs and was at 83% full; a run
  writing there died silently. Use `/home/agent/poke-harness/.scratch/`.
- `pyproject.toml` sets `addopts = "-q"`, so pytest prints **no** `N passed`
  line. Count from `--junitxml` `testsuite` attributes (`tests`, `failures`,
  `errors`, `skipped`).
- Ground truth must come from **executing** fixtures under real CPython 3.12 —
  never by reading the analyser's own tables or a hand-declared expectation
  column. If you use a committed probe, read it first and confirm it declares no
  expectations; if it does, write your own and say so.
- Commit no ROM, trace, cache, build output, credential, or private machine path.

## Already established — do not redo

Durable under `ledger/`: `INDEP_STAGE_A1_4BAEF6C.md`, `INDEP_STAGE_B_4BAEF6C.md`,
`INDEP_REVIEW_4BAEF6C.md`.

The last returned **APPROVE on correctness** but explicitly **refused to certify
the whole head end to end**: its Stage C (mutation / non-vacuity) prose is a
*lead reconstruction* over independently-produced artifacts. **That is the
specific gap you are here to close.**

- Two-file focused suite (`tests/test_timed_menu_milestone_sentinels.py` +
  `tests/test_timed_menu_milestones.py`) is **698 tests, 0 failures**. 626 is
  the sentinels file *alone*; the milestones file adds 72.
- `ruff check tests` clean. One pre-existing `ruff format` drift, present alike
  on `a89c1d7` and `6a0c68c` — not introduced by the union.
- **Monotonicity: 0 regressions.** 35-shape executed set across `master`
  `6b72bf6`, `6a0c68c`, `a89c1d7`, `4baef6c`. The candidate removes 13 of 20
  false-lives; **0 false-dead** rows on any tree.
- Non-vacuity counting proxy over the real 698 tests: `_is_enforced` 853,
  `_starred_store_decides_kind` 192, `_binds_starred_target` 117,
  `_starred_target_names` 49, `_starred_names_in_loop_target` 39,
  `_node_precedes_call` **0** (pre-existing, also 0 on `a89c1d7`).
- The `e058d96` correction is real:
  `tests/_timed_menu_milestone_sentinel_support.py:7080` returns the bool from
  `_starred_names_in_loop_target` directly; the
  `isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set))`
  guard is present at lines 5062 and 5955.
- 13 adversarial shapes against the two #420 guards: 0 breaks, 0 false-lives,
  0 false-dead. Both guards stay narrow.

### One prior claim is already retracted — do not repeat it

`ledger/FIND_20260930_427_GUARD_IS_DEAD_CODE.md` asserted
`_starred_names_in_loop_target` is **dead code** and recommended deleting the
`For` branch. **Both grounds were false and the recommended deletion produces 15
failures.** See `ledger/RETRACT_20260930_427_GUARD_IS_NOT_DEAD.md`: the "0 calls"
figure was an import-alias measurement artifact (the counter was attached to a
module instance the tests never exercise), and `for` targets are not always
recorded with `value=None`.

The surviving, correct claim is weaker: **no test row discriminates the guard.**
Disabling it still leaves the 698-test suite green, and on all 16 shapes where
the suite consults it, it answers `False`. That is what #427 is about.

## Your job — the two things not yet done

### 1. Re-run Stage C independently

Do it in **your own** worktree. Treat a surviving mutant as a **result to
explain**, not a failure to hide.

- Confirm the two disabling mutations and their kill status:
  `_starred_store_decides_kind` -> `False`, and
  `_starred_names_in_loop_target` -> `False`. Apply them **uncommitted**.
- Record the mutated file's blob identity so a mutated working tree is never
  mistaken for the pristine candidate.
- Measure the two-file suite from XML at the pristine tree and again for each
  mutant. Also run each of the three committed probes pristine vs mutant.

Then **adjudicate this specific dispute independently.** The prior review
concluded the surviving `_starred_names_in_loop_target` mutant is "behaviourally
redundant" with `_starred_store_decides_kind`. The lead disagrees and holds the
guard is consulted on only one of two paths, so it is *defensive* rather than
*redundant*:

- the value-shape call site (line 5011) is reached only when the queried `with`
  header's statement **is** the `ast.For`;
- the other call site (`_binds_starred_target`'s non-`Assign` arm, line 7081) is
  reached only after the assign path has already declined.

The lead searched 8 purpose-built shapes for one that distinguishes the mutant
from pristine and found **zero**. Confirm or refute *dead-code vs redundant vs
narrowly-live-but-undiscriminated*, and give the shape table either way. Note the
retraction above: the guard **is** reached (21 hits at 4999, asked 16 times at
5011) — the live question is whether any *input* makes its answer matter.

### 2. Adjudicate the five false-live rows

Ground truth by **executed** CPython 3.12.14. In each, a starred binding means
the `with` raises `TypeError` **before** the body, so the contract is **dead**
and the analyser wrongly answers `True`:

| id | shape | executed | analyser |
|---|---|---|---|
| T2 | starred **store** in a loop body, `with cs:` a later **sibling** of the loop | `TypeError`, dead | `True` |
| G | starred **`for` target** nested in `if flag:`, `with cs:` in that body | `TypeError`, dead | `True` |
| H | starred **`for` target**, `with cs: pass` then a separate `assert` in the same body | `TypeError`, dead | `True` |
| G2 | starred **`for` target** in `if True:`, `with cs:` after the loop | `TypeError`, dead | `True` |
| - | starred `for` target where the assert is a **bare** assert (no `with cs`) | assert **escapes** — genuinely LIVE | `True` (correct) |

Verify each independently, then answer:

1. Are these **merge-blocking** for `4baef6c`, or pre-existing defects correctly
   owned by open issues?
2. Do you agree they are wrong **identically** on `master` `6b72bf6`, `6a0c68c`,
   `a89c1d7` and `4baef6c`?
3. Does the reasoning "the position of the `with` relative to the starred store
   is irrelevant to the answer" actually hold? Adjudicate it.

These are **not** regressions. Anything correct on a parent and incorrect on the
candidate would be, and the lead found none.

## Deliverable — mandatory and durable

Write to a **new** file `ledger/INDEP_E2E_4baef6c.md` (if taken, use `_r2` and say
so). It must contain:

- the exact SHA and how you verified it;
- the mutation table with XML counts for pristine and each mutant;
- your adjudication of dead-code vs redundant, **with the shape table**;
- the five false-live rows, verified by execution;
- an explicit **merge-blocking yes/no** with reasons;
- every defect labelled **merge-induced** or **pre-existing**;
- the disposition of open issues **#418, #420, #422, #425, #426, #427, #430**;
- a statement that you are an independent reviewer and not the lead;
- a final line exactly one of:

```text
VERDICT: APPROVE
VERDICT: REJECT
VERDICT: APPROVE-WITH-CONCERNS
```

**A chat reply without this file on disk does not count.** The reviewer merges
nothing; the lead merges. If you are blocked, say so plainly and name the blocker.
