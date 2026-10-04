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

## Update, 2026-10-04 later pass: still blocked

Retried on a new PR (#571), so this is not a stale-agent problem:

| mechanism | target | result |
|---|---|---|
| `spawn_agent` | `indep569_scripts_lane` | returned a Codex-settings reply; cited `ledger/INCIDENT_STALE_BOARD_HIJACK.md`, which does not exist |
| `followup_task` | `indep569_scripts_lane` | `unsupported call` |
| `spawn_agent` | `indep571_review` | returned a workspace/config inventory; "no task in your message" |
| `list_agents` | — | `unsupported call` |

So a **freshly created** agent also receives no task text. That rules out stale
agent state and points at the dispatch path itself: the brief is not reaching the
subagent at all. All four briefs are readable at dispatch time, so this is not
file access or permissions.

An earlier responding agent also fabricated a `BRIEF_REVIEW_539_1827e8d.md` and
claimed to have verified a "51-entry list" by reading `.github/workflows/*`,
a path that does not exist in this repository. Its output is not usable.

## Structural consequence

Per the run's own rule — "An author may not independently approve their own
change" — no PR can merge while this holds. The lead authored the #566 fix, so
the lead's own measurements on #571, #568, and #569 **cannot** substitute for the
missing independent verdict, no matter how much time is spent re-running them.

Review briefs are on disk and committed, so any reviewer — human or agent — can
execute them verbatim:

- `briefs/REVIEW_565.md`
- `briefs/REVIEW_566.md`
- `briefs/REVIEW_569.md`
- `briefs/REVIEW_571.md`

## What is still actionable without a reviewer

Independent of the dispatch problem, real work continued this pass:

- Found and proved a **blocking defect** on #566 head `16d102e`.
- **Repaired it** on branch `fix/106-directive-normalization`, commit `23333c8`,
  opened as PR **#571**: mutation matrix green across seven directives,
  45 tests pass, lint and format clean.
- Reduced the review bottleneck from four unreviewed PRs to three, since #571 is
  a candidate replacement for #566's rejected head.

Merging still requires the independent verdict the tooling cannot deliver.

## Second blocked audit, 2026-10-04: still blocked, one new item verified

Dispatch retried after the goal was resumed and the environment refreshed.

| mechanism | target | result |
|---|---|---|
| `followup_task` | `indep569_scripts_lane` | `unsupported call` |
| `followup_task` | `indep571_review` | `unsupported call` |
| `followup_task` | `indep561` | `unsupported call` |
| `followup_task` | `indep571_review` (retry, #572) | `unsupported call` |

Six attempts across the session, both before and after resume. The brief files
remain readable and committed, so this is not permissions or file state.

## Progress made without a reviewer

Verification work does not require the dispatch tooling, so this turn was not
idle:

- **#572 appeared** (`fix/570-lane-suppression-guard`, head `c6fbaeea`) — the
  #570 work that was previously only a lead-measured finding. Verified on its
  own merits: inventory exact (53 tokens / 386 whole-lane / 334 tests-only /
  52 outside / 0 blanket / 34 selective = 31xF821 + 3xF401), all seven planted
  blanket directives refused including `#  ruff: noqa`, `#<TAB>ruff: noqa` and
  `##ruff: noqa`, the 105-F821 load-bearing claim confirmed on
  `tests/_sentinel_support_part1.py`, and a deliberately stale directive turned
  the row red. 47 tests pass, lint and format clean, CI run `37174136785`
  successful on `c6fbaeea`, merges cleanly against master.
- #572's head moved once mid-verification (`12aae41f` -> `c6fbaeea`), rewriting
  the grammar logic. The earlier evidence was discarded and re-measured rather
  than carried over.

## Current queue

| PR | head | lead evidence | independent review | mergeable |
|---|---|---|---|---|
| #572 | `c6fbaeea` | yes, CI green | **no** | clean vs master |
| #569 | `25607fc` | yes, on old base | **no** | **conflicts** with master |
| #568 | `1cc86f8` | yes, on old base | **no** | clean vs master |
| #565 | `f35554b6` | yes | **no** | clean vs master |

Four PRs, none independently reviewed, none mergeable. #569 additionally needs a
rebase onto the post-#568 master before any of its evidence applies.

Release status remains **PARTIAL**. Nothing merged by the lead this turn; no
issue closed. Master `a413eeb5`.

## Third audit, 2026-10-04: dispatch has degraded further

| mechanism | result |
|---|---|
| `list_agents` | `unsupported call` |
| `followup_task` (x2 targets) | `unsupported call` |
| `spawn_agent` | `unsupported call` |

`spawn_agent` **worked earlier this session** and now fails, so the tooling is
degrading rather than being uniformly unavailable. That is a change in the
environment, not a stable condition — worth retrying in a later turn rather than
treating as permanent.

## Progress made without a reviewer

Verification and integration work do not depend on dispatch, so this turn was not
idle:

- Rebased both #567 steps onto `a413eeb5` non-destructively. Established that the
  step-2/step-1 stacking constraint is **real**: step 2 alone leaves 39 files
  unformatted, so #569 genuinely cannot precede #568.
- Established that the fully stacked shape is **not green**: 2 files unformatted
  and 163 lint errors across 63 files that the old 51-entry list never covered.
  #569's own premise is what exposed them, which is the PR working as intended
  and still needing a cleanup commit.
- Net effect versus master: 41 -> 2 unformatted files, 285 -> 163 lint errors.

Details in `LEAD_20261004T1700Z_569_REBASE_RESIDUE.md`, posted to #569.

## Queue

| PR | head | lead evidence | independent review | mergeable now |
|---|---|---|---|---|
| #572 | `c6fbaeea` | yes, CI green | **no** | yes, but unreviewed |
| #569 | `25607fc` | yes, on old base | **no** | **no** — residue + conflicts |
| #568 | `1cc86f8` | yes, on old base | **no** | rebases cleanly, but unreviewed |
| #565 | `f35554b6` | yes | **no** | yes, but unreviewed |

Two independent blockers now, not one: the dispatch tooling, **and** real
residue that #568/#569 must clean before they can merge even once review works.

Release status remains **PARTIAL**. Nothing merged by the lead. No issue closed.
Master `a413eeb5`.
