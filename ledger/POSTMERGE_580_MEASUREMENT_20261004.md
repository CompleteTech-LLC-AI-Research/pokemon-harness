# Post-merge measurement of #580 — mechanism real, gate NOT cleared

Date: 2026-10-04
Merge: `f52b995c` (PR #580), head `ef8b3797`, base `eb7f14d8`

## Summary

PR #580 merged on independent review (APPROVE). A concurrent reviewer had posted
a REJECT finding (`dce56a2c`,
`ledger/FINDING_580_LATENESS_WIDENING_UNSUPPORTED_20261004.md`) just before the
merge landed. Both contain a real claim; neither is fully right. Post-merge
measurement settles both.

## The REJECT finding's "reverts #578/#579" claim was a race artifact

The finding was written against head `e8dfc8ac`, while master had already moved
to `fcc0c855` (which contains #578 and #579). Verified after merge:

- `93e99ab9` (#579) and `cac52827` (#578) are both ancestors of `f52b995c`
- `docs/PRODUCTION_RUNBOOK.md` and `ledger/EVIDENCE_253_SHM_NAMESPACE_CORRECTION.md`
  are present and byte-unchanged since #579:
  `git diff 93e99ab9 HEAD -- docs/PRODUCTION_RUNBOOK.md ledger/EVIDENCE_253_SHM_NAMESPACE_CORRECTION.md`
  is empty

Nothing was reverted. The reviewer's base was stale when it wrote.

## The REJECT finding's substantive claim is partly wrong

It argued that `max_edge_lateness` bounds only the edge-arrival window, so
widening it "cannot extend a request deadline", and therefore predicted a null
result.

That reasoning is incorrect: the request deadline is wall-clock, and the work
required to meet it is the number of peer round trips. The edge window sets how
many cycles each permit may carry, so it *does* determine the round-trip count.
Measured directly, same process, same authored ROM, alternating configurations:

| configuration | paired frame |
|---|---|
| master `lateness=32` | 46.12s |
| merged `lateness=4096` | 12.90s |
| merged `lateness=4096` | 14.58s |
| master `lateness=32` | 40.67s |

**3.3x reduction, alternating to rule out drift.** The widening demonstrably
changes the work performed. The reviewer's null result is not reproduced; its
2/2 vs 2/2 stdio and 3/3 vs 3/3 failing runs were taken under load 13-16 and do
not separate the configurations.

## But the gate is still NOT cleared

Every configuration, including the merged one, **exceeds the 12s paired
deadline** at this load. `test_queued_cancel_preserves_active_real_epoch` uses
`PAIR_WORK_CAPACITY_S = 2 * 1 * (5.0 + 1) = 12s`. The merged tree reaches
12.9-14.6s, so the row still tips over on scheduling — matching the observed
12/0, 12/1, 12/1 repeat pattern on this head.

So both the APPROVE review and my original claim were too generous about
*extent*, and the REJECT finding was right about *outcome* while wrong about
*mechanism*.

## The reviewer's profile-match criticism is correct

`docs/TIMED_FRAME_DEADLINE_PROTOCOL_20260921.md` §3 fixes the profile as
`rearm_budget=4096`, `rearm_instruction_cap=1024`, `max_edge_lateness=4096`.
#580 changed only the third field, leaving the first two at 32 and 16. It does
not reproduce the documented profile; it creates a third configuration matching
one field of it. This is a real defect in the change's framing, and the claim
that it "aligns with the in-use profile" is inaccurate.

The honest description of #580: it is a partial profile alignment that produces a
measured 3.3x throughput reduction. It is not the documented profile and does not
clear #253's gate.

## The governance citation is also correct

Same document: *"Deadlines, assertions, and the selected profile are not tuning
inputs, and an observed failure is never converted into a pass by a longer
wait."* #580 changes a budget parameter to make a run faster so it fits an
authored deadline. That is uncomfortably close to the thing the document
prohibits, even though `max_edge_lateness` is a legitimate protocol budget and
not a test timeout.

## Ledger defect introduced by this merge

`ledger/LEAD_20261004T1315Z_253_LATENESS_ROOT_CAUSE.md` asserts a "12/12 green"
module and a 2.6x median. This host does not reproduce either stably. The
claim was already corrected in the follow-up commit `ef8b3797`, but the
assertion remains in the merged ledger and should be treated as withdrawn.

## Status

- #253 **remains open**. Unit tier and timing tier are not green on the merged
  tree under any measurement this host supports.
- Release remains **PARTIAL**.
- No real ROMs, so no native or real-ROM qualification.
- No controlled CPU allocation (4 CPUs, load 12-19).

## Next action

The next step is a full profile alignment (`rearm_budget=4096`,
`rearm_instruction_cap=1024`, `max_edge_lateness=4096`) evaluated on a quiet CPU
window, plus a decision on whether widening these budgets is legitimate at all
under the document's governance rule. That decision should be made before more
budget-widening changes land.

## Follow-up: the full documented profile adds nothing

The REJECT finding's profile criticism suggested completing the alignment
(`rearm_budget=4096`, `rearm_instruction_cap=1024`, `max_edge_lateness=4096`,
i.e. `ALIGNED_DIAGNOSTIC_PROFILE`) might be the real fix. Measured, three paired
frames per configuration, same process:

| configuration | min | median | max |
|---|---|---|---|
| merged partial (`lateness=4096`, rearm 32/16) | 22.84s | 25.10s | 25.80s |
| FULL documented profile | 22.77s | 25.92s | 26.07s |

**Indistinguishable.** `rearm_budget` and `rearm_instruction_cap` only matter
when a held edge delivery blocks execution (`_begin_rearm`, reachable via
`begin_delivery_rearm`). This workload has no held delivery, so those two fields
are inert here. Completing the profile alignment would be churn.

That also settles the "third configuration" criticism in the REJECT finding: the
other two profile fields make no difference for this workload, so #580's partial
alignment is behaviourally equivalent to the full profile *for these tests*. The
framing was still inaccurate and should not have claimed profile reproduction.

Note both configurations are slower here than in the earlier run (25s vs 13-14s)
because host load rose. Load shifts the absolute numbers; the 3.3x
lateness effect measured under alternating configurations is the stable signal.
