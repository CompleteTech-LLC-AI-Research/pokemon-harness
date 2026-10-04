# Independent review brief — PR #572 (the #570 lane-wide suppression guard)

You are an **independent reviewer**. The lead already measured this PR; your job
is to re-derive it, not to confirm it. Where the numbers below disagree with
your own, trust your own.

## Ground rules (violating any invalidates your review)

1. **Never reuse, copy, or symlink a virtualenv between worktrees.** Create your own.
2. **Always run Ruff with `--no-cache`.**
3. **Do not run Ruff concurrently with a full pytest suite in the same worktree.**
4. Two runs of the same command on the same commit that disagree indicate an
   **environment fault, not a code finding.** Say so explicitly.
5. Reviewing only. Do not push, merge, or modify the branch.

## Revisions

- `origin/master` = `a413eeb5ba4bc8e5407fc31cd84c31efe855f314`
- PR #572 head = `c6fbaeea` (`fix/570-lane-suppression-guard`), base `master`.
  The head moved once during lead verification (`12aae41f` -> `c6fbaeea`),
  which rewrote the grammar logic. Confirm the current head and review THAT.
- Lead's merge-tree says **clean** against master. Confirm it yourself.

    cd /home/agent/vpkg
    gh pr view 572 --json headRefOid,baseRefName,isDraft,mergeable
    git worktree add /home/agent/rv572_$$ origin/fix/570-lane-suppression-guard
    cd /home/agent/rv572_$$
    python3 -m venv .venv && .venv/bin/pip install -e . pytest pytest-asyncio ruff

## What the PR does

Issue #570. `tests/test_local_ci_policy.py` lints every probe on **stdin** with
`--stdin-filename`, which makes probes immune to `extend-exclude`,
`per-file-ignores`, and a narrowed rule set — Ruff resolves all three from the
filename, never the bytes. The mirror-image cost is that a suppression in a
file's *own content* is invisible. #566 closed that for one file
(`_BENCHMARK`); this generalizes it to the whole lane.

## Lead measurements — re-derive each one

**Inventory** (PR claims / lead measured, all matching):

| quantity | claims | lead |
|---|---|---|
| lane path tokens | 53 | 53 |
| resolved files, whole lane (`tree=None`) | 386 | 386 |
| resolved under `tests/` only | 334 | 334 |
| resolved outside `tests/` | 52 | 52 |
| blanket directives | 0 | 0 |
| selective directives | 34 (31xF821, 3xF401) | 34 (31xF821, 3xF401) |

The `tree=None` change is load-bearing: the first cut defaulted to `tree="tests"`,
so it measured 334 of 386 while claiming lane coverage. Verify the row really
passes `tree=None` and that `resolved` is asserted non-empty.

**Teeth.** Prepend each line to `scripts/bootstrap_pyboy.py` as real bytes and
confirm the row
`test_no_lane_file_opts_out_of_linting_with_a_blanket_directive` FAILS for all:

    # ruff: noqa
    #  ruff: noqa      (two spaces)
    #<TAB>ruff: noqa
    # flake8: noqa
    # fmt: off
    # yapf: disable

Build the mutants in **Python**, not shell `printf` — `printf '#\truff'` under
some shells emits a literal backslash-t, which will make you wrongly conclude
the tab form does not silence Ruff. It does.

Also check the whitespace variant matters: a literal-prefix gate would miss
`#  ruff: noqa`. That is the same defect class reported on #566.

**No false positive.** All 34 selective directives are allowed because each is
load-bearing. Verify on `tests/_sentinel_support_part1.py`:

    line 2: # ruff: noqa: F821
    stripped -> exactly 105 F821s

These are generated `tests/_sentinel_support_*` fragments whose names come from
an assembled module, so a blanket "no file-level directive" rule would be wrong,
not merely strict. The allowance is scoped by re-measuring rather than
hard-coding an allowlist — check that is really what the code does.

**Stale allowances caught.** Mutate that directive to
`# ruff: noqa: F821, F841` (F841 is not reported when stripped) and confirm the
row goes RED. A row that could rot into a permanent exemption would be worthless.

**Suites and lint:**

    .venv/bin/python -m pytest tests/test_local_ci_policy.py tests/test_stepping_loop_profile.py -p no:cacheprovider
    .venv/bin/python -m ruff check tests/test_local_ci_policy.py --no-cache
    .venv/bin/python -m ruff format --check tests/test_local_ci_policy.py

Lead reports 46 passed, lint clean, format clean. Confirm `git status` is clean
afterward — every mutation must be reverted.

## Focus your skepticism here

The PR body says the directive regex was measured against `ruff check` rather
than inferred, and lists honoured vs inert forms. **Verify that grammar claim
directly.** An inert form wrongly treated as honoured is only a false positive; an
honoured form wrongly treated as inert is a bypass. Probe the edge cases yourself:
upper-cased values, a trailing colon (`# ruff: noqa:` splits to an empty code
list and disables nothing), leading `-`, hashes interspersed with whitespace or
dashes, and `##` doubling. Decide for yourself which direction is dangerous and
say which cases you tested.

## Scope discipline

Issue #570 may close only after this lands and post-merge checks pass. Closing
#570 does **not** close #106: controlled benchmark / real-ROM qualification
remains outstanding and real-ROM evidence may not be waived. Confirm the PR does
not over-claim.

## Deliverable

Report **APPROVE** or **REJECT** with the exact head SHA, your independently
measured inventory and mutation matrix, your verdict on the regex grammar, and
every finding marked blocking or non-blocking.
