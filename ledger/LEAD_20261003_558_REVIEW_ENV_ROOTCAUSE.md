# LEAD — #558: why rounds 9 and 10 could not decide, and the structural fix

Date: 2026-10-03

```
trunk          : master = origin/master = 7cb75ba6ac9afae7735db0044eee9acc1704ff46
candidate head : e05e7d1087cfae90e32f6f384750697b0b9fa5fb
```

## Ten reviews, zero APPROVE — and the last two failed for one reason

Rounds 9 and 10 both found **no code defect and no demonstrated escape**.
Both returned INCOMPLETE, and both named the same environmental fault:

```
rounds 9 and 10 : 9 failed, 115 passed
OSError: [Errno 30] Read-only file system:
   /workspace/poke-harness/.scratch/lead555venv/lib/python3.11/site-packages/
   genuine_install_neighbour.py
   benign_shape_neighbour.py
   nested_twin_neighbour.py
   genuine_latin1_finder_row.py
   copied_source_finder_row.py
   forged_spec_finder_row.py
   pokemon_hash_pin_row.dist-info
   pokemon_csv_path_row.dist-info
   pokemon_conflict_row.dist-info
```

Round 10 additionally reported that it hit the fault a second time on a retry,
and that `findmnt` showed the mount as `ro` at that moment. It correctly
refused to approve around a check it could not run, and said so.

## Root cause, measured rather than guessed

```
mount device  : /dev/root
mount options : ext4 rw,relatime,discard,errors=remount-ro,commit=30
/workspace    : /dev/root ext4  (rw now, intermittently ro)
/home/agent   : /dev/root ext4  (rw now, intermittently ro)
/tmp          : tmpfs 512M, 100% FULL
/dev/shm      : tmpfs 64M, read-only  (the known #253 blocker)
```

Two facts matter:

1. `errors=remount-ro` means any ext4 error remounts the whole filesystem
   **read-only**, for both `/workspace` and `/home/agent` — they are one
   device. There is no alternative path that dodges it.
2. The guard suite writes into the **running interpreter's own
   `site-packages`**. Under the shared `lead555venv` that is a directory other
   work in this repo also depends on, and a mid-run remount turns 9 rows into
   `OSError`.

So this was never a code defect and never a bad interpreter choice. It was a
shared mutable directory on a filesystem that is permitted to fail closed.

## The fix is structural, not a retry

A reviewer-private copy of the venv, owned by this review, plus a scratch
`--basetemp`:

```
/workspace/poke-harness/.scratch/r558review/bin/python
  (copy of lead555venv, plus pytest-asyncio so no --override-ini is needed)
```

Measured on the exact candidate head, while the mount was healthy:

```
$ /workspace/poke-harness/.scratch/r558review/bin/python -m pytest \
      --basetemp=/workspace/poke-harness/.scratch/r558s/bt-lead3 \
      tests/test_import_origin_guard.py
124 passed in 119.49s (0:01:59)
rc=0
```

All 124 rows green, **without** the `--override-ini addopts=''` workaround that
rounds 1–10 needed, and without writing into any shared venv. Round 11 was
dispatched against this setup and told explicitly not to use the old shared
venv.

## What this does and does not establish

It does not manufacture an APPROVE. The reviewer still has to complete the
audit and say so. It removes a recurring *environmental* reason for
INCOMPLETE, which is the only reason the last two rounds produced no decision.

Ten rounds: six INCOMPLETE, three REQUEST CHANGES with real findings, **zero
APPROVE**. #558 is not merged, #547 is not closed, release remains **PARTIAL**.

Next action: read round 11's verdict at
`/workspace/poke-harness/.scratch/r558s/out11.txt`. On an actual APPROVE,
build a merge tree against the current `master`, re-run the focused checks on
that exact tree, then merge with a head-SHA guard. On REQUEST CHANGES,
reproduce, repair at the root, add a regression test proven to fail on the
prior head, and review the new head again.
