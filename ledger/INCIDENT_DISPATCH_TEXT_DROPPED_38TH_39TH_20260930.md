# Incident — dropped dispatch text, 38th and 39th instances

Date: 2026-09-30
Class: the transport accepts a dispatch and returns a real task name, but the
child receives only the environment context and the settings policy.

## What happened

1. `list_agents` **worked** (first success in a long stretch) and showed the tree.
2. `send_message` to the already-running `/root/rev421` with a full inline
   #421 review brief -> accepted, no error.
3. `spawn_agent(fork_turns="none", task_name="indep_433_4916466")` with a
   complete inline #433 review brief -> accepted, returned
   `indep_433_4916466` / "Erdos the 2nd".
4. The child came back with **no task text**: it surveyed the workspace,
   restated the Codex settings policy, and asked what to do.
5. `followup_task` re-sending the whole brief inline -> accepted, no error.
6. The child came back **again** with the same "what would you like me to do".
7. `spawn_agent` with a deliberately minimal probe — "reply with the first 20
   words of any task text you received, or say NO TASK TEXT RECEIVED" ->
   accepted, returned `transport_probe_a` / "Euler the 2nd".
8. The child again reported only the environment context and asked for a task.

## Why this round is worth recording

The inline-brief approach was the one method the incident log listed as
**not yet tried**. It was tried, twice, with a full self-contained brief, and
once with a control probe designed to make dropped text unmistakable. All three
were swallowed.

The control probe is the part that matters: it asked for a 20-word echo, so a
child that had received *anything* would have been able to answer. It could
not.

## Consequence

No independent review was obtained for #421 or for the #429 fix. The lead is
the author of both branches and the objective bars self-approval, so **#435
(#429) and #421 stay unmerged, #429 stays open, release stays PARTIAL.**

## Standing rule, unchanged

A returned task name is **not** evidence of delivery. A child asking "what
would you like me to do" is **not** a verdict. Only a substantive finding on an
exact head SHA counts, and only that may unblock a merge.

## Recovery that did work

The lead's own isolated re-verification did the useful work instead: it found
that #433's branch was built on the simulation tree rather than its declared
base, which is a blocking defect no amount of reading the PR body would have
surfaced. See `FINDING_433_BRANCH_BUILT_ON_SIMULATION_20260930.md`.
