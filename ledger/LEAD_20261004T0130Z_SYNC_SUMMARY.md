# Review pass on all four open PRs — 2026-10-04

## Trunk

    origin/master  b5302d091e5c809bd343f0f15996b8a47bdb4d3d
    master (local) b5302d09   divergence 0/0

No `main` exists on this remote; `origin/HEAD -> origin/master`.

## PR state and what was established this pass

| PR | head | draft | independent verification performed |
|---|---|---|---|
| #565 | `f35554b6` | yes | guard verified against 7 adjacent escapes, all rejected |
| #566 | `43b977d` | no | escape reproduced and shown closed, both directions, 5 spellings |
| #568 | `1cc86f8` | yes | AST-identity measured (39/40), producer SHA-1 confirmed |
| #569 | `11eb719` | yes | gap found: one file missed by both steps |

### #565 — guard has teeth

The #89 hole is real: a count-consistent downgrade of a move-owning family to
`deliberately_excluded` was accepted before this PR. Now rejected. Seven
adjacent escapes were tried and all rejected, including the one that mattered
most — `move_ids` cannot be edited to dodge the guard, because the mapping is
checked against the pinned table first. The honest direction still works
(`tested -> planned_unverified` is accepted), so it is a scope rule rather than a
freeze.

### #566 — escape closed, one scope limit (filed as #570)

Reproduced the escape independently (a file-level directive silences F821 and
makes `ruff check` exit 0) and confirmed it is closed for the benchmark. Five
planted spellings all fail the row; four inert forms stay green. The round-9
commit's two self-corrections were both verified by measurement.

The row pins one file, so the same escape is live on every other lane file.
Implemented and measured the generalization, then set it aside: it turns the
lane red on 32 generated fragments whose ignores are load-bearing (removing one
yields `Found 105 errors`). Filed as **#570** rather than smuggling a policy
decision into a focused #106 fix.

### #567 stack — a real gap

`#568`'s AST-identity claim is exactly right (39 of 40 files AST-identical; the
four edits in `check_pyboy_components.py` are lint-driven and change no
behavior, confirmed by 29/29 on both trees). The producer carve-out is exactly
as described.

But `scripts/full_to_brock.py` is missed by **both** steps: not excluded, clean
under `ruff check`, and still needing reformat. CI agrees — `#568 ... SUCCESS`,
`#569 ... FAILURE`.

Also worth stating plainly: the red `ruff check scripts tests` on #569 (163
findings) is pre-existing, not a regression. Master reports 285, and the set
difference is two `E402`s that shift line number only because #568 reformatted
the file above them.

## Newly filed

**#570** — #566's in-file-suppression guard covers one file; the escape is live
on every other lane file. Includes the measurement and the reason it was not
folded into #566.

## Release status: still PARTIAL

Nothing merged, nothing marked ready, no issue closed. All four PRs remain
draft-pending or unmerged.

- The independent-review bar is **not** met for any PR. Everything above is
  author-side verification. `spawn_agent` returned `unsupported call` on every
  attempt this pass after one early success, so no independent verdict was
  obtained. Author self-verification does not substitute for it.
- Host blockers unchanged: 4 CPUs, no cgroup quota, `/dev/shm` read-only, zero
  real ROMs (all 464 `.gb`/`.gbc` found are 32 KB synthetic fixtures). Real-ROM
  qualification and controlled CPU allocation remain impossible.

32 open issues, 4 open PRs. 313 worktrees intact; protected checkout untouched.
