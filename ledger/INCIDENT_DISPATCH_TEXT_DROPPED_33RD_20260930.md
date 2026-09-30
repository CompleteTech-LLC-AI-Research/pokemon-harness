# Incident — dropped dispatch text, 33rd instance

Date: 2026-09-30
Class: collaboration transport delivers the shared AGENTS.md block and the
environment context to the child, but not the task message.

## What happened

The transport was partly working this round, which is what makes the round
worth recording.

1. `list_agents` returned normally and showed the whole agent tree.
2. `spawn_agent(task_name="indep429_a", fork_turns="none")` was accepted and
   returned a real task name, `Tesla the 2nd`.
3. The child came back with **no task text**: it reported the standing Codex
   settings policy, noted that `/workspace/poke-harness` is not a Git
   repository, and asked what to do.

The child behaved correctly. It did **not** fall back to `REVIEW_THIS.md` or
`TASK_PICKUP.md`, both of which carry `SUPERSEDED` stamps and are the
documented cause of `INCIDENT_STALE_BOARD_HIJACK.md`, where an agent that
did fall back produced a confident, evidence-backed verdict for the wrong
head. It re-verified live state read-only and returned no verdict.

## Recovery attempts

1. `followup_task` naming the brief path inline -> `unsupported call`.
2. `send_message` naming the brief path inline, with the head SHA, the two
   touched files, the base SHA and the interpreter path -> accepted, no
   error, and **no reply**.
3. `wait_agent` at 240 s -> timed out.
4. `wait_agent` at a further 300 s -> timed out.
5. `list_agents` -> `unsupported call`.

## Counting

Instances 1-32 are catalogued in
`ledger/INCIDENT_DISPATCH_TEXT_DROPPED_32ND_20260930.md` (in that child's
tree) and its predecessors. This is **33** for the lead integrator.

## Standing rule, unchanged

A returned task name is **not** evidence of delivery. A child that asks
"what would you like me to do" is **not** a verdict. Only a substantive
finding on an exact head SHA counts, and only that may unblock a merge.

## Consequence

PR #433 head `7846110` is mergeable and lead-validated but has **zero**
reviews. The author may not approve their own change, so **#433 stays
unmerged, #429 stays open, and release stays PARTIAL.**

The one approach not yet tried is a `followup_task` or `send_message` that
works, with the brief path in the dispatch body. It was tried twice this
round: once unsupported, once silently swallowed.
