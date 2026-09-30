# Incident — dropped dispatch text, 36th instance

Date: 2026-09-30
Class: collaboration transport delivers the shared AGENTS.md block to the
child instead of the task message.

## What happened

The transport was **working** this round, which is what makes this round
worth recording. `list_agents` returned normally for the first time in
several hours and showed the full agent tree with historical states, and
`spawn_agent` accepted a call and returned a real task name. Both facts had
previously correlated with delivery in only a minority of attempts.

Dispatch: `spawn_agent(task_name="simint_res_reviewer", fork_turns="none")`
with the full self-contained review brief in the message body.

Returned: `{"task_name": "/root/simint_res_reviewer", "nickname": "Euclid the 2nd"}`

Actual child response:

> Noted — I'll treat Codex settings changes as global: apply them to
> `/home/agent/.codex/config.toml` and all `/home/agent/.codex-account-*/config.toml`
> ...
> What would you like to work on in `/workspace/poke-harness`?

The child received **only** the AGENTS.md shared-settings policy and the
environment block. It did not receive the review task, and it explicitly
asked what to do rather than guessing a target from a pointer file. That last
part is correct behaviour and worth crediting: `REVIEW_THIS.md` and
`TASK_PICKUP.md` are both stamped SUPERSEDED and are the documented cause of
`INCIDENT_STALE_BOARD_HIJACK.md`, where an agent that fell back to them
produced a confident, evidence-backed verdict for the **wrong** head.

## Recovery attempt

1. `send_message` with the brief path inline. No reply.
2. Wrote the brief to
   `/home/agent/poke-harness/.scratch/simint/ledger/REVIEW_TASK_SIMINT_RESOLUTION.md`.
3. Second `send_message` naming that path explicitly, with the 9 expected
   rows, the fixture template, the interpreter path, and the read-only
   constraints restated. No reply.
4. `wait_agent` timed out at 240 s. A second `wait_agent` and a
   `list_agents` both returned `unsupported call`.

## Counting

Instances 1–35 are catalogued in
`ledger/INCIDENT_DISPATCH_TEXT_DROPPED_35TH_20260930.md` and its predecessors.
This is **36**.

## Standing rule, unchanged

A returned task name is **not** evidence of delivery. A child that asks "what
would you like me to do" is **not** a verdict. Only a substantive finding on
the exact head SHA counts, and only that may unblock a merge.

## Consequence for this round

The #412 + #424 + #419 conflict resolution at `aaf31e5` is lead-validated and
**unreviewed**, so it is **not merged**. Release remains `PARTIAL`. Details and
the exact next action are in
`ledger/LEDGER_20260930_SIMINT_RESOLUTION_VALIDATED.md`.
