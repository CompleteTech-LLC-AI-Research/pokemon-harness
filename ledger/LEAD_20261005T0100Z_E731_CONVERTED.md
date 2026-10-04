# 2026-10-05 ~01:00Z — #573 head `c8943669`: residue 140 -> 118; E402 fully classified

## Increment
Converted all 22 E731 findings to `def` across 9 files, committed as `c8943669`, pushed to PR #573.
Residue on `scripts` + `tests`: **140 -> 118** (113 E402, 5 F811, 0 E731).

## Verification on the pushed head
```
ruff check scripts tests --no-cache --statistics -> 118 errors (113 E402, 5 F811)
ruff format --check scripts tests --no-cache   -> 447 files already formatted
pytest tests/test_local_ci_policy.py tests/test_stepping_loop_profile.py -> 50 passed
pytest 6 modules touched by the conversion      -> 127 passed
mutation: drop `scripts` from the check lane   -> 6 rows red (guards still have teeth)
```
AST comparison against the parent: each edited file's AST dump changes by 1-7 characters
(`lambda` keyword -> `def`/`return`), confirming shape-only edits.

## Branch hygiene correction
The E402-classification ledger commit (`9359d555`) had briefly been committed onto the PR branch
because the edit ran from `/home/agent/wt567e`, whose `ledger/` is untracked scratch rather than the
ledger repo. Rebased it off (`git rebase --onto b21c14a7 9359d555`) and force-pushed with lease.
The PR branch is now code-only: 59 files under `scripts/`+`tests/`, plus
`.github/workflows/release-hygiene.yml` and `pyproject.toml`. **Lesson: write ledger files from
`/home/agent/vpkg`, never from a worktree.**

## E402 classification (the remaining 118)
All 113 classified by AST structure; none is accidental.
- 90 `BOOTSTRAP` in 33 files — a `sys.path` mutation executes earlier at module level
- 23 `REEXPORT` in 11 files — deliberate trailing re-exports
- 0 accidental

Load-bearing proven by removal, not assumption: deleting the `sys.path` loop in
`scripts/diagnose_pair_trade.py` breaks collection with
`ModuleNotFoundError: No module named '_diagnose_pair_trade_support'`. Notably the direct
`--help` run still exited 0 because of an `except ModuleNotFoundError` fallback, so a
"does it still run" check would have wrongly cleared it. Only test collection exposed the break.

## F811 (5) — pytest fixture idiom
`emulator` x4 in `tests/test_serial_owner_pump.py`, `prepared` in
`tests/test_normal_club_journey_capture.py`. Both are `@pytest.fixture` functions re-exported so
the fixture is visible; Ruff reads the test function's same-named parameter as a redefinition. The
parameter *is* the fixture. Fix is to widen the existing `# noqa: PLC0414` to cover F811 and pin it
with a policy test — not a rename.

## What closes #567, and what does not
Still required, and **not done**:
1. A pinned E402 allowance: `per-file-ignores` or targeted `# noqa: E402` for the 33 + 11 files,
   plus a policy test that re-lints each allowed file with the allowance removed and **fails if the
   allowance does not reveal the findings it claims to cover**. Dead config must fail.
2. The F811 suppression widening, pinned the same way.
Neither is started. #567 stays open at 118 findings.

## Blockers unchanged
- **#489**: independent review still unobtainable after 7 dispatch attempts (`spawn_agent` and
  `list_agents` both returning `unsupported call`; earlier successful dispatches delivered no
  task). I authored `c8943669`, so lead verification is self-verification. **#573 not merged.**
- No real ROMs; no controlled CPU allocation; `/dev/shm` read-only.
- Release status **PARTIAL**.
