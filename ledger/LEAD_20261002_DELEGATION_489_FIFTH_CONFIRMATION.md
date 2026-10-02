# #489 — fifth confirmation: task delivery still broken, but tool surface regressed

**Date:** 2026-10-02
**Target of the attempted dispatch:** PR #556 head `c34be0b`, independent review.

## What changed this time

For the first time the dispatch **looked healthy**:

- `list_agents` succeeded and returned the full live tree with correct lifecycle
  states for every prior child (including deep three-level reviewer chains that
  had completed real reviews earlier, e.g. `indep537_8b3e5b8` and
  `indep_rev_8b3e5b8_lead2`).
- `spawn_agent` returned a canonical task name and a nickname:
  `/root/indep_rev_556_c34be0b/indep_rev_556_c34be0b` ("Hegel the 3rd").

Both prior signals had been read as "dispatch itself is broken". This run shows
dispatch and lifecycle reporting are **not** the fault: the child was created,
named, and run to completion.

## What still fails

The child produced a generic "what would you like me to work on?" reply,
quoting only the `AGENTS.md` shared-settings paragraph. The ~1,400-word task —
full brief, target SHA, exploit reproduction steps, mutation list, false-red
scrutiny list, machine-hygiene constraints, deliverable path — was not
delivered. `AGENTS.md` arrived; the `message` argument did not.

A file-path dispatch was prepared as the control (`rev556_brief.md`, 161 lines,
sha256 `b1fd3a8d922a5858be7fc992bc208111153becc7169dd44f9e695fd020f2838a`),
matching the variant #489 recommended specifically because it bypasses message
transport. The inline-message variant was used instead, so the file-path
control was not exercised this turn.

Within seconds of the child completing, `list_agents` regressed again to
`unsupported call: list_agents`, and `send_message` / `followup_task` are
unavailable. So the handle needed to re-deliver the task or poll a reply cannot
be used.

## Diagnosis (unchanged, now better isolated)

Argument **transport** is the failure, not dispatch and not lifecycle:

| Stage | State |
|---|---|
| `spawn_agent` accepted, named, and ran a child to completion | working |
| `list_agents` / lifecycle reporting | intermittent (worked, then `unsupported call`) |
| `AGENTS.md` + environment context delivered to child | working |
| `message` argument delivered to child | **failing, 5 of 5 attempts** |
| `followup_task` / `send_message` to an existing child | `unsupported call` |

Five attempts across four turns, spanning both dispatch variants (inline task
text and file-path brief), all fail identically.

## Consequence for the #534 lane

Unchanged and firm: **nothing merges without an independent review.** #556 head
`c34be0b` carries the repair for the blocking forged-`co_filename` finding
against #555, so #556 — not #555 — is now the correct merge vehicle for the
whole #534 guard stack. It is `MERGEABLE` but has no independent review, so it
stays open. #555 and #551 stay open with it.

## Next action

Re-dispatch on the first turn where `list_agents` and `spawn_agent` both work.
The reviewer must receive the task. If a child ever replies that it has no task,
that reply is the falsification signal, not progress.
