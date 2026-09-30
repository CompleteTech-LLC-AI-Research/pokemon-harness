# Incident — dispatch text dropped (34th and 35th instances)

Date: 2026-09-30
Status: transport degraded again after an apparent recovery.

## What happened

`list_agents` **succeeded** this round (first success in many rounds), and two
`spawn_agent` calls each returned a real task name:

- `/root/indep429_a` — "Dewey the 2nd"
- `/root/indep429_b` — "Hume the 2nd"

Both were dispatched with `fork_turns="none"` and the **full review brief
path inline in the message**, which the prior rounds identified as the one
approach not yet tried:

> TASK BRIEF (read it fully first):
> /home/agent/poke-harness/.scratch/fix429/ledger/REVIEW_TASK_429_FIX.md
> ... plus the complete contract, the interpreter path, the hard rules, the
> output path, and the specific angle for each reviewer.

**Both children replied with no task.** Each described the workspace, restated
the settings `AGENTS.md`, and asked what to work on. Neither mentioned PR #433,
commit `39f1b19`, `_elif_link_is_conditional`, the brief path, or the word
"review".

A returned task name is not evidence of delivery, and this is direct evidence
that it is not. **Zero review verdicts exist.** No verdict is claimed.

## Consequence

PR #433 has **no independent review**. The objective bars self-approval, so
#433 cannot be merged on this evidence. It stays open and unreviewed.

## What the lead did instead

Rather than stall, the lead ran the independent-review work itself, by
separation of *method* rather than of *author*:

- Every ground-truth value came from **executing fixtures on CPython 3.12.14**,
  not from reading the analyzer's logic. This mattered: an earlier probe
  (`p429_adv.py`) asserted `Case E` expected `False`, and execution showed that
  expectation was **wrong** — the arm is unconditional and the correct verdict
  is `True`.
- Claims from prior rounds were **re-measured, not inherited** (suite counts
  from JUnit XML, mutation re-run, ruff re-run).

This is *weaker* than a second author. It is recorded as such.

## Two errors the lead made and corrected, rather than dropped

1. A probe (`p429_e.py`) hardcoded `fn.body[2].test`, and the store lookup
   returned an `IndexError` because the body index was wrong. A subsequent
   `git stash -q ... && python ...; git stash pop -q` chain left the
   uncommitted edit **in the stash** when the middle command failed and the
   `&&` short-circuited the pop. Recovered with `git stash pop`; the edit is
   now identified as a no-op and was reverted deliberately.
2. That no-op edit existed only because Case E's expected value was taken from
   a static table rather than executed. **A static table produced a confident
   wrong answer that survived a full probe run.** This is the exact failure
   mode `_assert_entry_contract` was written to eliminate in the test suite,
   and it recurred in a throwaway probe.

## State

- Protected checkout `pokemon`: `lead/259-widen-lint-lanes` @ `bd2c167`, exactly
  3 staged files, `stash@{0}` preserved. Unchanged.
- `origin/master` unmoved at `6b72bf6`.
- `.scratch/fix429` clean at `39f1b19` (plus the local brief commit `d5dcd92`).
- Nothing merged, no issue closed, no PR marked ready. Release `PARTIAL`.
