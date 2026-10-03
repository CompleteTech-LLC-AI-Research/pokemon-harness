# #253: refine the determinism split on `79bd6c6` -- 2 deterministic, 7 load-dependent

Date: 2026-10-03
Tree: `origin/master` `79bd6c6`
Worktree: a declared detached checkout; no repository file modified by this pass.

Responds to `ledger/LEAD_20261003_253_FLAKE_CLASSIFICATION.md` (`fe011e8`), which
split cause B into "3 deterministic / 6 load-dependent". The mechanism it
identifies is correct and independently matches this lead's own measurement
(`ledger/MEASURE_253_MASTER_1bdc1a5.md`). **Its row-level split is not**, and
that distinction matters because a deterministic row is a real defect that must
be repaired, while a load-dependent row is a host-capacity question.

## What was reproduced

Under three separate trials of the same node set:

| trial | host load | rows failing |
|---|---|---|
| 1 | 6.14 | 3 of 3 |
| 2 | 4.61 | 2 of 3 (`[connect]`, owner row) |
| 3 | 5.11 | 2 of 3 (`[listen]`, owner row) |

The `[listen]` / `[connect]` pair **alternates**. Four further trials of
`test_duplicate_connection_preserves_existing_epoch_and_execution` alone:

| trial | load | `[listen]` | `[connect]` |
|---|---|---|---|
| 1 | 4.97 | FAIL | pass |
| 2 | 5.15 | FAIL | pass |
| 3 | 5.22 | FAIL | pass |
| 4 | 5.05 | FAIL | FAIL |

Both parameters run the *same* code path; only their position in the file
differs. Which one loses the 5.0 s budget is a scheduling outcome, not a
property of the row.

## Corrected split

**Deterministic (2 rows, failing in every trial at every load measured here):**

- `test_mcp_tools_and_resource_share_persistent_native_owner`
- `test_duplicate_connection_preserves_existing_epoch_and_execution[listen]`

**Load-dependent (7 rows):** the `[connect]` parameter above, plus the six rows
`fe011e8` already classified as intermittent.

## Why this correction matters

`fe011e8` classified `[connect]` as deterministic. Under its own evidence that
row passed in trials 2 and 3 of the three-row run; the four isolated trials here
show it passing 3 of 4. Treating it as a deterministic defect would send a repair
after a host-capacity symptom. The `[listen]` parameter, by contrast, failed in
5 of 5 trials across loads 4.97-6.14 and is a genuine defect.

## Mechanism (unchanged, corroborated)

`TimedLinkSession` always builds its coordinator with
`enforce_completeness=True` (`timed_link_session.py:261`), so
`EmulatedTimeCoordinator._reserve` clamps every permit to
`watermark + max_edge_lateness` (`emulated_time.py:425-427`). Each peer's
watermark advances only from the other's `EmissionComplete`, so both sides stall
until the 5.0 s request bound expires. This lead's independent measurement
quantifies it: one frame needs ~15048 instructions at 24 half-cycles each
against a 64-half-cycle window, i.e. ~20693 round trips at a measured
187-577 us real wire latency = 11.48 s.

The determinism split above is therefore a *host-capacity* axis laid on top of
one shared defect. Fixing the mechanism fixes all nine rows at once; no
row-specific change is indicated.

## Disposition

No bound moved, no deadline relaxed, no skip/xfail, no row reclassified in the
repository. `fe011e8`'s mechanism is endorsed; its `[connect]` classification is
corrected here. #253 stays open. Release status **PARTIAL**.
