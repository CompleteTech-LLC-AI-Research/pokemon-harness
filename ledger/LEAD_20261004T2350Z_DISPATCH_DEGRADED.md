# 2026-10-04 ~23:50Z — independent-review dispatch: mechanically available, semantically broken

## What happened
Four attempts to obtain a genuinely independent review of PR #573 head `b21c14a7`. None produced a
review. The tooling itself no longer errors uniformly, which is why this needs writing down
precisely rather than as "dispatch is down".

| attempt | agent | mechanism | outcome |
|---|---|---|---|
| 1 | `indep573_review` | `spawn_agent`, full brief inlined | answered the Codex-settings prompt |
| 2 | `indep573_review` | `followup_task`, "CORRECTION: that was not your task" | re-answered the settings prompt |
| 3 | `indep572_audit` | `spawn_agent`, full brief inlined | re-answered the settings prompt; asked clarifying questions |
| 4 | `indep572_audit` | `followup_task`, "do not ask me questions, execute now" | workspace inventory + offered a menu of next steps |
| 5 | `indep573b21c` | `spawn_agent`, task-first minimal prompt | workspace survey + asked which of three options I wanted |
| 6 | `rev573_v4` | `spawn_agent`, task-first prompt | `unsupported call` returned by `spawn_agent` itself |

Observed directly this session: `spawn_agent` and `list_agents(path_prefix=...)` and
`wait_agent` each return `unsupported call` intermittently, while unfiltered `list_agents` works.
So the failure has two distinct components:

1. **Intermittent transport failure** — `unsupported call` from the collaboration tools.
2. **Task-delivery failure** — when dispatch *does* succeed, the receiving agent does not see the
   assigned task. It answers the standing AGENTS.md settings convention or surveys the workspace
   instead. Attempt 5 is the clearest evidence: a short, task-first prompt with explicit
   "no clarifying questions, do not discuss Codex settings" still produced a workspace survey and a
   request for instructions.

This matches what earlier ledger entries recorded as issue #489. It is **not** fixed by retrying,
and retrying is what produced the misfires. Attempt 4 is the sharpest illustration: after being told
in no uncertain terms to stop asking and execute, the agent returned a menu of options.

## Consequence for the merge contract
The contract requires an independent review per merge and forbids author self-approval. The lead's
own verification does **not** satisfy that bar — the lead authored the repair in `b21c14a7` and
resolved the rebase conflict. So:

- **#573 is NOT merged and NOT marked ready.** `b21c14a7` is unreviewed by an independent party.
- The same applies to #565, #568, #569, none of which has a review.
- Release status stays **PARTIAL**.

## What the lead did instead, and why it is not a substitute
Rather than merge on self-verification, the lead re-derived every load-bearing claim on the exact
head tree (see `LEAD_20261004T2230Z_...`, `..._2300Z_...`, `..._2345Z_...`):
- 50/50 focused tests pass, mutation matrix across three escalating mutations, no false-green found
- six docstrings genuinely restored, AST-identical apart from the string's position
- lint residue re-measured at 140; master baseline 285
This is recorded as **lead verification, explicitly not independent review**, so the record cannot be
mistaken for the approval it is not. The blocker is external and is left open, per the brief's
instruction to identify the exact missing resource rather than describe the state as passed.

## Smallest next action
Obtain one independent review of `b21c14a7` by a route that actually delivers the task: either a
working sub-agent dispatch (issue #489), or a reviewer outside this session's agent tree. The brief
for it is already written and staged at `/home/agent/REV573_TASK.txt` (and, in fuller form,
`/home/agent/REV_573_B21C14A7.md`) with a prepared read-only worktree at `/home/agent/wt573v`.
Nothing else blocks the merge.
