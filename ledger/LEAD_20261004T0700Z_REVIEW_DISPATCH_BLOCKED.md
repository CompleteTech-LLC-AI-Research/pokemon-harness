# Independent review is blocked: task text is not reaching subagents

## Status

**No PR has an independent verdict. Nothing is mergeable. Release: PARTIAL.**

Author self-verification does not meet the bar, so no merge may proceed.

## Measured failure mode

Three dispatch mechanisms were tried on 2026-10-04 against the briefs in
`briefs/`. Every one that returned a response returned something that was not a
review:

| mechanism | target | result |
|---|---|---|
| `followup_task` | `/root/indep559_fmt` | returned a Codex-settings reply; cited a nonexistent brief |
| `followup_task` | `/root/indep566_review` | `unsupported call` (x2) |
| `followup_task` | `/root/indep565_scope_guard` | `unsupported call` |
| `followup_task` | `/root/indep89_scope_guard` | `unsupported call` |
| `followup_task` | `/root/indep559_try4` | `unsupported call` |
| `spawn_agent` | `indep569_scripts_lane` | created; returned a Codex-settings reply, no task text |
| `spawn_agent` | (second) | returned "I don't see an actual task", offered a menu |

The brief files are readable at the dispatch moment (`test -r` passes, mode
`0600`, owner `agent`), so the failure is not file access. The task text itself
is being dropped before it reaches the subagent, which then receives only the
`AGENTS.md` settings policy and the environment block.

`followup_task` additionally fails outright with `unsupported call` about half
the time; `spawn_agent` succeeds in creating the agent but does not deliver
either.

## Confirmed hallucination

`/root/indep559_fmt` cited `ledger/INCIDENT_STALE_BOARD_HIJACK.md` and
`ledger/BRIEF_REVIEW_539_1827e8d.md` as guidance. Neither file exists:

    ls ledger/ | grep -iE 'INCIDENT|BRIEF_REVIEW_539'   -> only BRIEF_REVIEW_367/392/419
    find /home/agent -maxdepth 3 -name INCIDENT_STALE_BOARD_HIJACK.md  -> nothing
    find /home/agent -maxdepth 4 -name 'BRIEF_REVIEW_539*'            -> nothing

It also asserted a "51-entry list of enumerated scripts" that it claimed to have
verified by reading `.github/workflows/*` — that file does not exist in this
repository either. Its response is not a review and is not evidence for
anything. This is the #489 delivery-failure pattern.

## Consequence for the queue

#565, #566, #568, #569 all remain **unreviewed**. The correct action is to stop
dispatching and record the blocker, not to keep retrying and treating whatever
comes back as a verdict.

## What is still legitimately established

Lead-measured evidence recorded independently of any review:

- **#566 head `16d102e` has a blocking defect**, found and proven by the lead,
  posted to the PR. The `maybe_directive` prefix gate skips `#  ruff: noqa`,
  which does silence Ruff. The pre-`16d102e` row rejects that same mutation and
  `16d102e` accepts it. See
  `LEAD_20261004T0630Z_566_MAYBE_DIRECTIVE_BYPASS.md`. #566 is therefore
  rejected on its merits regardless of the dispatch problem, but it should still
  get an independent review after the fix.
- #567 step 1 (#568) and step 2 (#569) have lead-measured evidence and green CI
  on `25607fc`, but still lack the independent verdict required to merge.
- #565 has lead-measured mutation evidence, and is only a slice of #89.
- Issue #570 is open and confirmed by two separate lead measurements.

## Remedy for the fix on #566

Normalize instead of enumerating prefixes. Verified against the 18 spellings
that matter:

    import re
    def maybe_directive(comment: str) -> bool:
        normalized = comment.strip()
        if not normalized.startswith("#"):
            return False
        return re.match(r"#+\s*(ruff|flake8|fmt|yapf|noqa)\b",
                        normalized, re.IGNORECASE) is not None

This probes every whitespace variant that genuinely silences Ruff, and still
skips the ones that do not (`# type: ignore`, `# nosec`,
`# pylint: disable=all`, ordinary prose). Re-verify by planting `#  ruff: noqa`
and confirming the row goes red.

## Preserved state

- Worktrees: 313 (scratch review worktree `/home/agent/wt566p` removed).
- Protected checkout unchanged: `lead/259-widen-lint-lanes` at `bd2c167`,
  3 staged files, 2 stashes, untracked `.scratch/` and `ledger/`.
- `origin/master` unchanged at `b5302d0`.
