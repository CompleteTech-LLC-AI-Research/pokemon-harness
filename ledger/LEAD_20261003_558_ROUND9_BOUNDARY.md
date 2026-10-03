# LEAD — #558 round 9: the structural boundary, and the first clean review run

## Trunk

```
master        = 294a683b110e4311a9315f6f680717a9fc75efee
origin/master = 294a683b110e4311a9315f6f680717a9fc75efee
origin/master...master = 0 0
```

## Candidate

```
branch lead/556-const-collision
head   e05e7d1087cfae90e32f6f384750697b0b9fa5fb
```

## Why this round is different

Eight rounds had each found exactly one more unguarded read, and rounds 5, 7
and 8 all turned out to have repaired an *adjacent* read rather than the
reported one -- the right call was guarded while the value it returned was not.
Widening read N never demonstrated read N+1 was safe, so a ninth widening
would have been the same bet for the ninth time.

`check_origins()` is now a thin wrapper around `_check_origins()`. The wrapper
converts **any** `BaseException` escaping the analysis into the same
machine-readable FAIL document every other refusal produces.
`KeyboardInterrupt` and `SystemExit` deliberately still propagate.

The guard now has a property it did not have: *it always answers*. A caller
gating on `report["status"]` can no longer receive nothing at all.

Row: `test_the_guard_refuses_rather_than_traceback_on_any_escape`, which
poisons a read nobody guarded and asserts a FAIL document still comes back.

## Round 9 verdict: INCOMPLETE, no code finding

Round 9 found **no attacker-controlled path escaping `check_origins()`**, and
established **no code defect**. It returned INCOMPLETE for two reasons, both
worth recording precisely because neither is a product problem:

1. It could not complete an exhaustive end-to-end hostile-input audit.
2. Its suite run reported `9 failed, 115 passed`, attributed to read-only
   writes under the review venv's `site-packages`.

**The lead re-ran the exact command and the attribution does not hold as a
description of the code.** On this host, at this head:

```
/workspace/poke-harness/.scratch/lead555venv/bin/python -m pytest -q \
    --override-ini addopts='' tests/test_import_origin_guard.py
rc=0    124 passed, 0 failed
```

Zero failures. The nine were a transient ext4 remount hitting that venv during
the reviewer's run -- the same intermittent fault that has truncated six earlier
dispatches. The reviewer's own guard output on the real checkout was PASS for
both packages, it read the retained AC4 files, and it accepted both directions.

So: no defect established, and the environmental excuse verified as
environmental.

## Evidence on the current head

```
guard suite     : 124 passed, rc=0  (115 -> 116 -> 118 -> 119 -> 123 -> 124)
AC4 repaired    : rc=0 PASS both
AC4 mis-pointed : rc=1 FAIL both
reviewer verdict: INCOMPLETE, zero code findings
ruff            : 1 finding, the pre-existing import-sort one
repo state      : 0 tracked mods, 137 untracked, 659 worktrees, editable install intact
```

## Still not merged

INCOMPLETE is not APPROVE. Nine reviews have run: six INCOMPLETE, three
REQUEST CHANGES with real findings, **zero APPROVE**. #558 is not merged and
#547 is not closed. Release remains **PARTIAL**.

The remaining gap to a merge is a review that completes its audit and returns
APPROVE on `e05e7d10`, plus AC1 and AC2 verified by execution rather than by
reading the wiring. Both are lead-doable now that the boundary makes an escape
reportable instead of fatal.
