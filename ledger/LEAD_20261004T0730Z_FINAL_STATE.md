# Lead state, end of 2026-10-04 pass

## Trunk

`origin/master` = `b5302d091e5c809bd343f0f15996b8a47bdb4d3d`. There is no `main`
branch locally or on the remote; trunk is `master`. The earlier "sync with ma"
request was satisfied by fast-forwarding local `master` to `b5302d0` with zero
divergence and no force-push, reset, or stash.

## PRs — none mergeable

| PR | head | draft | state |
|---|---|---|---|
| #565 | `f35554b6` | yes | awaiting independent review; slice of #89 only |
| #566 | `53b6a97a` | no | **REJECTED by lead**, blocking defect present |
| #568 | `1cc86f8` | yes | green CI, lead evidence, awaiting independent review |
| #569 | `25607fc` | yes | green CI, lead evidence, awaiting independent review |

`1cc86f8` (#568) is an ancestor of `25607fc` (#569), so **#568 must merge before
#569** or #569 will conflict.

## #566 head moved twice during this pass

`fceb11c3` -> `16d102e` -> `53b6a97a`.

- `16d102e` added the `maybe_directive` prefix gate. The lead found and proved a
  bypass in it (see below).
- `53b6a97a` is titled "#106: record the verified #570 scope limit" and touches
  only its ledger file. `tests/test_local_ci_policy.py` is **byte-identical to
  `16d102e`**, so the blocking defect is **still present on the current head**.

## Blocking finding, still open on `53b6a97a`

`maybe_directive()` skips any comment that does not literally start with one of
ten prefix strings. Ruff's directive grammar allows whitespace between the `#`
and the keyword, so `#  ruff: noqa` (two spaces) or `#\truff: noqa` silences the
entire file while the gate never probes it.

Measured end to end on head `16d102e`, with the same mutated benchmark:

- pre-`16d102e` row -> **fails** (correctly reports the file-level suppression)
- `16d102e` row -> **passes**, while `ruff check --select F821` on that file
  exits 0

So `16d102e` removed coverage the branch already had. Posted to the PR:
https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/566#issuecomment-5974312654

Fix direction (verified against 18 spellings): normalize instead of enumerating.

    import re
    def maybe_directive(comment: str) -> bool:
        normalized = comment.strip()
        if not normalized.startswith("#"):
            return False
        return re.match(r"#+\s*(ruff|flake8|fmt|yapf|noqa)\b",
                        normalized, re.IGNORECASE) is not None

Also worth noting for whoever fixes it: on today's benchmark only **1 of 131**
comments survives the gate, so the row currently rests on a single probed
comment.

## Independent review: BLOCKED

No PR has an independent verdict. Three dispatch mechanisms across seven
attempts all failed to deliver task text; one responding agent cited two files
that do not exist in this repository. Details and evidence:
`LEAD_20261004T0700Z_REVIEW_DISPATCH_BLOCKED.md`.

Author self-verification does not meet the bar, so no merge may proceed.

## Issues

32 open. Do **not** close:
- #89 merely because #565 merges — #565 is one slice; every acceptance row must
  be terminal.
- #106 merely because #566 merges — controlled benchmark / real-ROM
  qualification remains. Real-ROM evidence may not be waived.
- #567 until both steps land and post-merge checks pass.

## Host blockers (unchanged, re-measured)

- 4 CPUs, no cgroup quota
- `/dev/shm` read-only
- no real ROMs; all 464 `.gb`/`.gbc` files are 32 KB synthetic fixtures
- no controlled CPU allocation

## Release

**PARTIAL.** Nothing merged this pass. Nothing marked ready. No issue closed.

## Preserved state

- Worktrees: 313, matching the protected baseline.
- Protected checkout `/home/agent/poke-harness/pokemon`: branch
  `lead/259-widen-lint-lanes` at `bd2c167`, 3 staged files, 2 stashes,
  untracked `.scratch/` and `ledger/`. Untouched throughout.
- Ledger branch `ledger/clarify-setuptools` pushed through this commit.

## Standing rules

- Ruff always with `--no-cache`.
- Never run Ruff concurrently with a full pytest suite in the same worktree.
- Never reuse, copy, or symlink a venv between worktrees.
- Disagreeing runs of the same command on the same commit mean environment
  fault, not a code finding.
- Per-file Ruff directives are honored only for real files, not
  `--stdin-filename`; stdin probes alone cannot prove a planted per-file
  directive silences anything.
