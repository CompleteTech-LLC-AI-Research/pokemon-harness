# Independent review brief — exact head `a89c1d7` (round 2, clone-agnostic)

**Do not use `REVIEW_THIS.md` or `TASK_PICKUP.md` as a task source.** Both are marked
SUPERSEDED and have caused a real hijack incident. This brief is authoritative.

## Read this first: you may be in the WRONG clone

There are two clones of this repo on this host. Both share the same `origin`.

| | Lead's | Possibly yours |
|---|---|---|
| Path | `/home/agent/poke-harness` | `/workspace/poke-harness` |
| `pokemon` HEAD | `bd2c167` | `2c92d9a` |
| worktrees | 165 | 501 |

**This is why earlier reviews failed: the candidate worktree existed only in
`/home/agent`.** That is now FIXED — see next section. Do not report a clone
mismatch as a blocker; materialise the candidate yourself as instructed.

## Target

- Repository: `https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness`
- Branch: `lead/412x409-probe` — **pushed to `origin`, so fetchable from any clone**
- **EXACT head SHA to review: `a89c1d754a9d51f47fbf43ad4e3e2af569039e0b` (`a89c1d7`)**
- Base: `origin/master` = `6b72bf62b5722e1df2c44c988bbdb14803024032` (`6b72bf6`)

### Step 1 — materialise the exact tree (do this first)

Pick a scratch path under whichever clone root you are in, e.g.
`/workspace/poke-harness/.scratch/rev-a89c1d7` or
`/home/agent/poke-harness/.scratch/rev-a89c1d7`. Use absolute paths.

    git -C <your-clone>/pokemon fetch origin lead/412x409-probe
    git -C <your-clone>/pokemon worktree add <abs-scratch-path> \
        a89c1d754a9d51f47fbf43ad4e3e2af569039e0b

If that clone already has a worktree at that path, or the object is missing
(missing the tip is a real blocker — report it; do NOT substitute another SHA),
`git -C <clone>/pokemon fetch origin` first.

**Step 2 — VERIFY BEFORE READING ANYTHING:**

    git -C <abs-scratch-path> rev-parse HEAD

must equal `a89c1d754a9d51f47fbf43ad4e3e2af569039e0b`. If it does not, STOP and
report a mismatch. A verdict against any other SHA is void.

If that clone has no `.venv`, use the lead's interpreter read-only:
`/home/agent/poke-harness/pokemon/.venv/bin/python`. If that path is also
unavailable, report it — do not install or mutate toolchains.

## Protected state — do not touch

- Do NOT run `git checkout/reset/clean/stash/add` in any shared checkout.
  A shared checkout may be staged at a deliberate tripwire.
- Never create a worktree inside a `pokemon/` directory.
- Preserve all existing worktrees (165 or 501 — do not prune). Remove ONLY the
  scratch worktree you created, when done.
- Do not merge, push, close issues, or mark anything ready. You are a reviewer.
- Never commit ROMs, symbols, save states, traces, credentials, caches, build
  output, or private machine paths.
- Do not make any timing or capacity claim: the host is contended (CPU PSI ~67
  vs policy max 20). Wall-clock admission failures are NOT evidence about code.

## Stack under review

Four contributor PRs integrated by the lead, in order:

1. #409 `53304ed9` — `#370b`: reach a loop-bound suppressor through a nested
   header or an else arm
2. #412 `6f388cf1` — `#385`: read a completed loop's target as its last element
3. #408 `0bce85b6` — `#330/#338/#339`: read a suppressor the `with` header
   constructs inline
4. #405 `d89616d9` — merge of #379: a `nonlocal` name is an alias for the
   enclosing function's binding (destructuring semantics)

The lead resolved the #405 × #412 collision. Relevant code is in
`tests/_timed_menu_milestone_sentinel_support.py`. Resolution records:
`ledger/FIND_20260929_405x412_RESOLUTION.md` (available on branch
`fix/385-position-aware-loop-value` and on `origin/lead/stack-f23f1f0` if not in
your materialised tree) and `ledger/LEDGER_20260929_COMBINED_TREE.md`.

The central collision was in `_store_bindings`. Resolution: `_store_bindings`
records the REDUCED destructuring element for suppression analysis, while
`_store_may_bind_enterable` recovers the ORIGINAL container from `statement.value`.
`_loop_value_source` was removed intentionally (no remaining references).

## Lead's own validation (self-verification — NOT independent review)

Recorded so you know what is already claimed; you must NOT take it as a verdict.

- Focused suite on `a89c1d7`: **696 passed, 0 failed**
- `ruff check tests`: clean
- Resolved support module independently dropped onto #412, #405, #408, and
  master: 0 failures each (contributor worktrees restored afterward)
- CPython ground-truth probes on destructuring, carrier, chained-assignment,
  starred-target, nested-pattern, loop-target, and control shapes: corrected
  with zero merge-induced mismatches

## What you must determine

The primary method is **differential execution against real CPython** — not
reading the tool's own tables, and not reading the lead's conclusion.

For each fixture, decide ground truth by EXECUTING it under CPython 3.11 and
observing what actually happens:

- `True` from the analyzer means the contract is retained as LIVE.
- LIVE only when the assert actually FIRES.
- If the assert is swallowed, or a `TypeError`/other exception is raised before
  entry, the contract is DEAD and the correct answer is `False`.
- Disagreement in the direction "tool says DEAD, CPython enters the body" is the
  DAMAGING direction and is a blocking finding.

Write fixtures in your own scratch path, exec them under the venv interpreter,
and record the ACTUAL outcome. Never treat the analyzer's own answer as ground
truth.

### Questions

1. **Correctness of the merge resolution.** For the four PR families, does
   `a89c1d7` agree with CPython ground truth on destructuring semantics
   (positional, starred, bind-from-end-after-star), carrier suppression, chained
   assignment, loop targets read in-body and after-loop, nested patterns, and the
   `nonlocal` chain?
2. **Independence of the three families.** Is there any input where the merged
   result differs from BOTH the individual PRs AND master — a merge-induced
   disagreement no single PR introduced alone? The lead claims zero. Test for it;
   do not assume it.
3. **Isolation / no cross-contamination.** Does the resolved support module still
   behave correctly when dropped onto #412, #405, #408, and master alone? You may
   re-verify a sample; the lead claims 0 failures each.
4. **Vacuity.** Are the helpers the merge relies on actually REACHED by the
   suite? A prior review round found a helper (`_node_precedes_call`) invoked
   ZERO times across 401 tests while five live regressions were invisible to the
   suite. Check specifically that the #405 × #412 machinery
   (`_single_loop_element`, `_loop_element_for_read_after`,
   `_loop_target_bindings_after_loop`, `_value_bound_by`, `_element_for_target`,
   `_store_bindings`, `_store_may_bind_enterable`, `_aliased_suppressions`) is
   exercised. Name any helper with no coverage, and any live disagreement the
   suite cannot detect. Instrument with coverage or tracing — do not eyeball.
5. **Non-regression.** Run the focused suite and `ruff check tests` on the exact
   head. Report terminal counts from JUnit/XML or captured output, not prose.

    <abs-scratch-path>/.venv/bin/python -m pytest \
        tests/test_timed_menu_milestone_sentinels.py -q

## Known pre-existing defect — do NOT re-report as a merge blocker

The lead found, and confirmed identically on master `6b72bf6`, #408, #405, #412
and `a89c1d7`:

    *cs, = (contextlib.suppress(AssertionError),)
    with cs:
        assert x != 1

`cs` binds a list; CPython raises `TypeError`, so the contract is DEAD. The
analyzer reports `True` (false-live). NOT introduced by this merge; do not fold
it into a verdict on `a89c1d7`. Mention only to confirm you reproduced it as
pre-existing.

## Deliverable

Write your report to:

`/home/agent/poke-harness/ledger/INDEP_REVIEW_405x412_a89c1d7.md`

If that path is not writable from your clone, write to
`/workspace/poke-harness/ledger/INDEP_REVIEW_405x412_a89c1d7.md` and say so.

The report MUST end with a single unambiguous line, exactly one of:

`VERDICT: APPROVE`
`VERDICT: REJECT`

Include:
- the exact head SHA you reviewed, re-verified at the END of your work
- base SHA and merge-base
- scope and owned paths (must be empty — you are a reviewer, not an implementer)
- every finding, each with: minimal reproducer, CPython ground truth, what the
  tool answered, and the DIRECTION of disagreement
- disposition of each lead claim (confirmed / not confirmed / could not test + why)
- exact commands run and terminal counts
- helper-coverage result for the #405 × #412 machinery
- confirmation the pre-existing starred-target defect reproduces as pre-existing
- confirmation the protected shared checkout and all worktrees are unchanged
- unresolved risks and the single smallest next action

Do NOT invent a verdict to be helpful. `REJECT` with concrete, reproducible,
merge-induced findings is a complete and acceptable deliverable. `APPROVE` only
if you actually executed the ground truth and found NO merge-induced
disagreement in either direction.
