# Session — 2026-10-04 — the writable-`/dev/shm` record corrected end to end

Start: `origin/master` `eb7f14d8`. End: `origin/master` `93e99ab9`.
Open PRs at end: **0**. Open issues at end: **30**. Release status: **PARTIAL**.

## Landed

### PR #578 -> `cac52827` — the #577 recipe is wrong (evidence only)

- Adds `ledger/EVIDENCE_253_SHM_NAMESPACE_CORRECTION.md`. No source/test/config
  touched. Hosted run `37201870904` pass on head `2e0f3b83`.
- The recipe #577 recorded maps uid 1000 to **0**. Five tests require a
  non-root uid; measured 5/5 pass at uid 1000 and 5/5 fail at uid 0.
- Working form is a nested user namespace: mount as root outside, `exec` into
  an inner namespace mapping uid 1000, which inherits the writable `/dev/shm`.

### PR #579 -> `93e99ab9` — the runbook text itself corrected

- `docs/PRODUCTION_RUNBOOK.md` §3b-0 rewritten; `EVIDENCE_253_...md` corrected
  to match. Hosted run `37203680189` pass on head `b548b36c`.
- Four independent adversarial rounds. Rounds 1–3 found real defects; round 4
  found none. Two findings were rejected **on evidence**, not waved off:
  `§3b-0` is a real heading in the same file, and the `collected` guard is not
  circular because `_gate_report.py` snapshots `collected` in
  `pytest_collection_finish`, before any test runs.

## Three claims that measurement contradicted

These were wrong in the repo before this session and are now corrected.

1. **The failing primitive is not only the POSIX semaphore.** Against the
   read-only mount, `mp.Queue`, `mp.Lock`, and
   `shared_memory.SharedMemory(create=True)` all raise `OSError: [Errno 30]`;
   anonymous `mmap` and `TMPDIR` `tempfile` succeed. The old text blamed
   "arena allocation", which is wrong; the first draft of the fix then named
   only the semaphore, which is too narrow.
2. **The 14 unit failures are not all `timed_deadline`.** Terminal errors read
   from the gate report: 8 `TimedOwnerError`, 1 `ChannelClosed`, 1 a 48 s
   completion bound, 1 a deadlock, 3 other assertion diffs. **No load figure was
   recorded for that run**, so the previous "under host load" attribution was
   unsupported and is removed.
3. **The 9/6/5 series is not comparable to the 14.** It was measured on
   `7d09ac7e` under the *root* recipe, which injects five extra uid failures.
   Different tree *and* recipe, so no directional comparison is valid. The 14 is
   higher than anything recorded before and is **unexplained**.

Also measured and recorded: the nested recipe's bind target is **persistent**,
so `/dev/shm` keeps leftover `sem.mp-*` semaphores between runs (ten
accumulated, verified) where `mount -t tmpfs` gave a fresh empty one each time.
Not ruled out as a contributor to the intermittency.

## Gate on `eb7f14d8`, source runtime, corrected wrapper

```
FAIL  unit    total=8327 passed=8313 failed=14 skipped=0 xfailed=0 xpassed=0 errors=0
FAIL  timing  total=2125 passed=2090 failed=35 skipped=0 xfailed=0 xpassed=0 errors=0
overall: FAIL
```

`returncodes=[124]` is not truncation: `collected` is a collection-time
snapshot, `selected_nodeids` independently carries 8327, and the guard's limit
(it would not catch a short collection) is stated rather than glossed.

## Blocked — unchanged, no action available in this environment

- **#253** — stays **open**. 14 unit and 35 timing failures remain; the gate is
  FAIL. The 14 need a quiet host or a real allocation to classify, and the
  recipe defect that was masking five more is now fixed.
- **#84/#85/#86, #72, #108** — need an operator-declared CPU allocation.
- **#90–#105, #235** — need real ROM/symbol/fixture assets; none exist.
- **#89** — needs terminal real-ROM battle-mechanics evidence; 67 families
  unverified (from PR #576).
- **#489** — collaboration sub-agent task delivery still broken; not
  re-exercised this session. Independent review used the external harness at
  `/tmp/rev-b3484c68/run_review.py` (`deepseek-v4.1-flash`), which is **not**
  the required multi-agent lane structure.
- **#110** — needs #88 acceptance plus a declared allocation.

## Method note

Three review findings in round 1 looked right and were not: `§3b-0` is a real
heading, `--unit-only` legitimately selects both tiers, and the `collected`
guard is not self-referential. Each was checked against the code before being
accepted or rejected. Round 3 hit its token cap mid-verdict; its one surviving
point (an unsupported load attribution) was correct and is fixed.
