# #567 stack rebased onto master; the directory token exposes real residue

Master is `a413eeb5`. Both #567 steps were authored against `b5302d0` and had to
be rebased before anything about them could be re-verified. This records the
rebase and, more importantly, what it exposed.

## Rebase performed (non-destructive, original branches untouched)

    /home/agent/wt568r  fix/rebase-568-onto-master   step 1 only -> ce7f13e, no conflicts
    /home/agent/wt569r  fix/rebase-569-onto-master   step 2 onto step 1 -> ae3f43c

Conflicts were in `.github/workflows/release-hygiene.yml`,
`scripts/run_local_ci.sh`, and `tests/test_local_ci_policy.py`. In every case the
resolution keeps step 2's intent — the enumerated 51-entry script list replaced by
the `scripts` directory token — while preserving master's #566-era format lane
(`src/pokered_harness/_mcp_facade_entry.py` first). All three files re-verified
after resolution: Python parses, YAML loads, `bash -n` clean.

## The stacking constraint is real, not procedural

Rebasing step 2 onto master **without** step 1 leaves the lane broken:

    ruff format --check scripts --no-cache
    -> 39 files would be reformatted, 74 already formatted

So #569 cannot merge ahead of #568 regardless of review status. Step 1 must land
first. This is now measured rather than assumed.

## What the directory token actually exposes

Compared like-for-like with `--no-cache`, whole lane `scripts tests`:

| | master `a413eeb5` | stacked branch `ae3f43c` |
|---|---|---|
| files needing reformat | 41 | **2** |
| files already formatted | 407 | 445 |
| lint errors | 285 | **163** |

A large real improvement, but **the stacked branch is not green.** #569's premise
is that `scripts` as a directory token covers files the 51-entry list never
mentioned, and that turns out to include 63 files carrying 163 errors
(136x E402, 22x E731, 5x F811). Those are genuine defects in newly-covered code,
not regressions from the rebase.

## Two concrete gaps in step 1

Both are a single stray blank line that `ruff format` removes:

    scripts/full_to_brock.py                 line 408: blank line after a def signature
    tests/_tcp_trade_peer_drive_setup.py     line 43:  blank line at the top of a method

`scripts/full_to_brock.py` is worth calling out because a previous finding that
the step-1 work missed this file was **retracted** as a `.ruff_cache` artifact. On
re-measurement with `--no-cache` and in a dedicated worktree the file *is*
touched by step 1 (it is reformatted substantially) but is left one blank line
short of clean. The retraction was right to be cautious about the original
claim; the residual here is a genuinely separate and smaller finding, measured
without cache interference.

## Producer carve-out intact

Only `scripts/produce_battle_state_fixtures.py` is excluded via
`force-exclude = true` in `pyproject.toml`. `full_to_brock.py` is **not** carved
out, so it is inside the lane and must be clean.

## Consequence for the merge queue

#568 and #569 cannot merge as they stand. Closing the residue needs either a
further cleanup commit on top of the stack (reformat the two files, address the
163 errors in the 63 newly-covered files) or a decision to scope the directory
token to a subset. Either way that is new work on top of both PRs, and it needs
its own review — it is not something to slip in during a rebase.

Nothing was pushed and no PR was modified. The original branches
`fix/567-scripts-format-clean` and `fix/567-scripts-dir-lane` are untouched at
`1cc86f8` and `25607fc`.
