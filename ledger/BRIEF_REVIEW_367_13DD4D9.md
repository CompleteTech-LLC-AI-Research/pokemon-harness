# Review brief — PR #393, head `13dd4d9`, issue #367

**If your dispatch text was dropped, this file is your target.** It is the sole
task source for this slot. Do NOT fall back to `REVIEW_THIS.md`,
`TASK_PICKUP.md`, or any SUPERSEDED board — those name long-dead heads
(`b9902a2`, `082ceae`, `bd78e85`, `d6599c9`) and using them caused a recorded
hijack incident. Verify the head yourself before doing anything.

## Target

- PR: #393 — https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/393
- Branch: `fix/367-tied-store-ambiguity`
- **Head SHA under review: `13dd4d975c9523e5bc50ab6da8dcc8ee6e29180a`**
- Base SHA (`origin/master`): `ed9d9b0daadefca4ce3082d49068890d4949ddc3`
- Issue: #367 — read it with `gh issue view 367` **including comments**. The
  issue author RETRACTED the "#360 regression / damaging" attribution; the
  defect itself is real and pre-existing, and the repair must still land in the
  safe direction.

## Worktree rule (hard)

The head is **already checked out and clean** at
`/workspace/poke-harness/.scratch/fix367`. Treat it as **read-only**: do not
create, move, reset, clean, rebase, commit, or push anything there. For a base
comparison or a mutation run, make your own named scratch tree and **state its
absolute path before use**:

    git -C /workspace/poke-harness/.scratch/fix367 worktree add \
        /workspace/poke-harness/.scratch/rev367_<yourname> <sha-or-branch>

Do **not** use `/workspace/poke-harness/pokemon` as an authoritative checkout
— it is stale (`master`, 22 dirty files) and is another actor's tree.

## Files in the diff (base `ed9d9b0` -> head `13dd4d9`)

- `tests/_timed_menu_milestone_sentinel_support.py` (+692 / -21)
- `tests/test_timed_menu_milestone_sentinels.py` (+161)
- `ledger/LEDGER_367_ED9D9B0.md` (+124)

## The defect

`_binding_order` maps a store to the **top-level statement containing it**, so
two stores in the same block compare **equal by construction**. The pre-#360
`visible[-1]` and the post-#360 `max(...)` both resolve such a tie by **walk
position** — a property of the breadth-first traversal, not of the program.

Minimal case: a `for/else` binds the same name in both branches. Exactly one
branch runs, so no single verdict is right. The analyzer must **decline**
(report the assert defeated — the safe direction, #308 criterion 1) instead of
picking one.

## Acceptance criteria the repair must satisfy

1. A tie in `_binding_order` is not resolved by walk position; the named
   `for/else` case is declined.
2. The gap is covered by **shipped fixtures**. Before the repair the suite was
   fully green with the defect present, i.e. the tie path had **zero**
   coverage.
3. **No new damaging-direction regression in either polarity:**
   - (a) a *live* assert reported **defeated**, and
   - (b) a suppressed / unreachable assert reported **ENFORCED**.

   **(b) is the dangerous direction and matters most.** Scrutinise it hardest.

## What to assess

Judge whether the tie policy is a *source-semantic* answer or merely a
differently-shaped heuristic. Concentrate on `_latest_write_in_block`,
`_store_is_settled_before`, `_runs_before_end_of`, `_stores_of`,
`_NOT_A_SUPPRESSOR`, `_match_capture_pins_value`, `_readable_store_value`,
`_deref_alias`, `_last_store_before`, `_resolve_bindings`,
`_aliased_suppressions`.

Also check for **dead / unreachable code** and for **over-broad rules**. A fix
that simply declines everything would satisfy criterion 1 while destroying the
tool — silent coverage loss is a real and disqualifying risk. Establish with
execution evidence that ordinary positive shapes are still **certified
(enforced)**, not declined.

## Required evidence (minimum)

1. **Independent suite run.** Report counts read from JUnit XML, never console
   progress:

       cd /workspace/poke-harness/.scratch/fix367 && \
       /workspace/poke-harness/pokemon/.venv/bin/python -m pytest \
         tests/test_timed_menu_milestone_sentinels.py \
         tests/test_timed_menu_milestones.py -q \
         --junitxml=/tmp/rev367_<yourname>.xml

   Then also `python -m ruff check`, `python -m ruff format --check` on the two
   python files, and `git diff --check`.

2. **Independent adversarial execution.** Build your **own** shapes (do not
   reuse the author's tables) and EXECUTE each against the support module,
   comparing the analyzer verdict to **true CPython runtime behaviour**
   (`/workspace/poke-harness/pokemon/.venv/bin/python`, CPython 3.12.14).
   Every row needs a **non-suppressor control** so a module that merely
   declines everything is visible rather than passing.

   Report every row where the analyzer says ENFORCED but runtime was live, or
   says DEFEATED but runtime was suppressed.

3. **Mutations.** The ledger claims 7/7 killed. Re-run at least 2 yourself.
   Restore the file byte-identically afterwards (`diff -q` against your own
   pristine copy) and confirm `git status` clean. Note: a mutation that fails
   to *apply* is indistinguishable from one that survives — verify it applied.

4. **Ledger accuracy.** The ledger claims these hashes:

       50aa7b9170e5d921cef1e504f87363e8b1f873599b53cbba088e91d52e611cdf  tests/_timed_menu_milestone_sentinel_support.py
       3bb0756428ca1dce8c279fa5179e03f8b16a4f42506e26e6a3f79d14cd4d69d4  tests/test_timed_menu_milestone_sentinels.py

   Verify them. Report any ledger claim you could not reproduce.

## Hard release constraints

- Do **NOT** use ROM assets, `pyboy`, synthetic gameplay, RAM mutation, relaxed
  deadlines, skips/xfails, partial-matrix acceptance, or paid hosted CI.
- The repository's hosted CI is skipped; an empty check rollup is **not** a
  pass. Judge on local evidence you produce yourself.
- Release status stays **PARTIAL**. Make no real-ROM, native, timing, or
  performance qualification claim.
- Host note: `/workspace` is at 98% with ~7.1G free and load1 ~31 on 4 CPUs.
  No timing-sensitive claim from this host is admissible.

## Handoff format (return exactly this)

- **Verdict**: `APPROVE` / `REQUEST CHANGES` / `REJECT` — one line, plus the
  single strongest reason.
- **SHA reviewed** — must be exactly `13dd4d9...`; if the head has moved, say so
  and stop.
- **Commands and exact terminal results** (counts from JUnit XML).
- **Independent execution table**: `shape | true runtime | analyzer verdict |
  agree?` for every row you built, controls included.
- **Findings** — numbered, each with severity (blocking / non-blocking), file
  and line, and a concrete reproduction.
- **Mutation re-runs** — which, and how each was killed.
- **Ledger accuracy** — claims you could not reproduce.
- **Unresolved risks.**
- **Smallest next action.**

Be blunt. If this is a disguised "decline everything" hack, or has a real
polarity-(b) hole, say `REQUEST CHANGES` and **prove it by execution**. If it
is sound, say `APPROVE` and show the execution evidence that ordinary positive
shapes are still certified.
