# Independent review of the #253 lockstep stall — hypothesis REFUTED, real cause found

Date: 2026-10-03
Tree: `master` `b0f09461`
Reviewer: independent agent via `codex1 -p cheaperinference` (not the author)
Brief: `.scratch/rev253brief.md`; raw transcript `.scratch/r253_indep.log` (492 KB)
Status of review: **REFUTED my stated mechanism**, and found a stronger, measured one.

## Verdict

The brief's hypothesis — that `_publish` withholding progress below
`_threshold` is the throttle — is **wrong**. Two independent refutations.

### Refutation 1: `force=True` already bypasses the threshold in the blocked path

I had written that a blocked owner "publishes nothing" because it is below
`_threshold`. It does publish. `_wait_for_progress` pumps with `force=True`
before it blocks:

```
$ grep -n "_safe_pump(force=True" src/pokered_harness/link/timed_link_session.py
651:        self._safe_pump(force=True, deadline=deadline)
662:        self._safe_pump(force=True, deadline=deadline)
```

and `force` short-circuits the threshold at line 486-488:

```python
progress_due = local > self._sent_progress and (
    force or local - self._sent_progress >= self._threshold
)
```

Measured on a real pair: 5627 blocked waits produced 5016 publishes, i.e. the
blocked owner publishes on nearly every block. The threshold does not gate the
blocked path at all.

### Refutation 2: lowering the threshold makes it measurably slower

| mode | wall clock, 1 frame | publishes | grants | waits |
|---|---|---|---|---|
| baseline (`_threshold = 128`) | **22.05s** | 5016 | 20673 | 5627 |
| `_threshold = 1` | **31.82s** | 15048 | 22734 | 7687 |

Three times the publish traffic, 44% slower. Publish coalescing is load-bearing,
not the bottleneck. The 2026-09-27 root-cause note on #253 made the same error
and its "publish threshold" conclusion does not hold up under measurement.

## The actual binding constraint

The reviewer instrumented `_reserve` to record which term attained the
`ceiling` on every grant. Across three separate runs the answer was unanimous:

```
1 frame  : binding {('granted','watermark'): 20682}   of 20682 grants
3 frames : binding {('watermark'): 62390}            of 62390 grants
```

**100% of grants are bound by `enforce_completeness`, never by `peer + quantum`.**

```python
# src/pokered_harness/link/emulated_time.py:425-428
if self._enforce_completeness:
    # Bound the entire in-flight permit, including all rearm credit.
    # Peer progress alone does not attest to receipt of earlier edges.
    ceiling = min(ceiling, self._watermark + self._max_lateness)
```

So the credit quantum, the rearm budget, and the episode ceiling are all
irrelevant to throughput on this path. The watermark is the only thing that
matters, and it advances only on a signed, contiguous edge-delivery receipt:

```python
# emulated_time.py:645-650
if sequence != self._edge_sequence or sequence < self._watermark_sequence:
    self._fail("watermark requires complete contiguous receipt prefix")
self._watermark = through_half_cycle
```

**The structural bound.** Because an owner's own local time is itself clamped
to `_watermark + _max_lateness`, and `_watermark` only moves when the peer
attests a further edge receipt, each owner can advance at most `max_lateness`
half-cycles past the peer's last attestation. Every cross-thread round trip
therefore yields at most `max_lateness` halves of new credit. The per-instruction
permit (`required_cpu_cycles == 24` for kind `"cpu"`, one `before()` call per
retired instruction) turns that into ~20k round trips per frame.

Confirms the throttle is the round-trip *rate*, not the threshold and not host
load. The reviewer measured a bare-thread handoff control at 0.02 ms, so this is
governor work per permit, not scheduler latency.

## Candidate repairs, ranked (reviewer's analysis)

1. **Reduce per-permit governor cost.** The emulator calls the governor once per
   retired instruction; each call does a `snapshot()`, a Condition-locked
   `_reserve()`, `_observe()`, `_read_counter()`, `commit()`. Batching permits
   would cut round trips by orders of magnitude, but this is where #164's
   edge-response retirement guarantee has to be checked.
2. **Widen the window** (`max_edge_lateness` or quantum). Measured 14.5s vs 27.6s
   at lat=128, roughly 2x. **This relaxes a latency bound and is rejected by the
   brief's own constraints.** Not proposed.
3. **Eager watermark propagation.** Superseded: the refuted-threshold
   experiment shows extra publishes cost more than they buy.

## Why nothing was merged

The reviewer's run was cut off by my dispatch timeout while testing repair
option 1, so I do not have a verified repair, only a verified diagnosis. Options
1 and 3 both touch the `EmulatedTimeCoordinator` invariants and #164's
edge-response retirement guarantee, which the run contract requires to be
independently reviewed on their own merits with the changed head re-reviewed.
That review is not complete.

## Correction to my own earlier ledger

`ledger/LEAD_20261003_253_CURRENT_HEAD_MEASUREMENT.md` states the threshold
mechanism as the cause. **That section is wrong** and is superseded by this
file: the threshold is not the throttle. The unit/timing counts and the
`/dev/shm` decomposition in that ledger are unaffected and still stand.

## What is now known and what is not

- Known: 79 of 86 failures are the read-only `/dev/shm`; the remaining 9 are
  this round-trip-bound stall, whose binding constraint is `enforce_completeness`
  watermark propagation rather than the publish threshold.
- Not known: a verified repair. No repair is proposed for merge here.
- Not obtainable on this host: an independent review through the collaboration
  harness (#489). The `codex1`/`codex2` CLI path works and was used for this
  review, but it is a different mechanism than #489 is about.

No deadline, latency bound, capacity window, CPU requirement, skip, xfail, or
gate was relaxed. Release status stays **PARTIAL**.
