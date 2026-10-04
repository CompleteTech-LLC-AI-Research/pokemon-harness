# PR #580 — the `max_edge_lateness` widening is not supported by measurement

Date: 2026-10-04
Reviewed head: `e8dfc8ac` (measured head `0eebedee`, unchanged test content)
Verdict at review time: **do not merge as written.** Comment posted to the PR.

> **Partly retracted — see "RETRACTED" at the end.** The outcome findings
> (#253's gate is not cleared, tests still fail) stand and were conceded by the
> author. The *mechanism* claim in this file was **wrong**: `max_edge_lateness`
> does reach the failure, and PR #580's throughput analysis is correct. PR #580
> was merged as `f52b995c`. That correction is recorded here rather than
> quietly edited away.

## What the PR does

Three test files, one line each: `max_edge_lateness` `32` -> `4096`. No
production code. It targets #253's 14 unit-tier and 35 timing-tier failures.

## Measurements (4 CPUs, load 13-16, no allocation)

Run under the nested-namespace wrapper (uid 1000, writable `/dev/shm`), with a
per-worktree editable venv so each head imports its own tree.

`test_queued_cancel_preserves_active_real_epoch`, 3 runs per head:

| head | result | duration | terminal error |
| --- | --- | --- | --- |
| master `fcc0c855` | F, F, F | 12.40 s | `TimedOwnerError: request deadline expired` |
| PR #580 | F, F, F | 12.58 s | same |

0.18 s apart on a 12 s budget. The 128x widening changed nothing measurable.

`tests/test_mcp_timed_stdio.py`, 2 runs per head:

| head | run 1 | run 2 |
| --- | --- | --- |
| master | `FF...` | `FF...` |
| PR #580 | `.....` | `FF...` |

Intermittent on both heads; the same two `test_authored_timed_stdio_pair_frames_and_cleanup`
cases fail whenever it fails.

`tests/test_mcp_timed_remote_cached_failure.py` on the PR head, identical code
twice: **12/12**, then **10/12**. So the body's "12/12 green" is achievable but
is not a stable property of the tree.

## Why the mechanism does not reach the failure

`max_edge_lateness` becomes `EmulatedTimeCoordinator._max_lateness`
(`src/pokered_harness/link/emulated_time.py:186`) and bounds the **edge-arrival**
window at lines 424 and 428 when computing a permit ceiling. The residual
failure is a **request-deadline** expiry raised from
`src/pokered_harness/mcp_timed_owner.py` on the `request_timeout` path. Widening
the edge window does not extend a request deadline. This predicts the null
result, and the null result is what measured.

The PR's 2.6x median (36.69 s -> 14.15 s) was **not** reproduced here and is
treated as unestablished in both directions.

## The change does not match the profile it cites

`docs/TIMED_FRAME_DEADLINE_PROTOCOL_20260921.md` §3 fixes the profile as
`rearm_budget=4096`, `rearm_instruction_cap=1024`, `max_edge_lateness=4096`.
The PR changes only the third, leaving the first two at 32 and 16. So it does
not reproduce the documented profile; it creates a third configuration matching
one field of it.

## Governance

Same document: *"Deadlines, assertions, and the selected profile are not tuning
inputs, and an observed failure is never converted into a pass by a longer
wait."* The PR's payoff is a faster run meeting an authored deadline, and the
alignment argument does not hold because the profile is not matched.

## Also true of this PR

- Based on `eb7f14d8`, six commits behind master; merging as-is reverts #578
  and #579.
- `ledger/LEAD_20261004T1315Z_253_LATENESS_ROOT_CAUSE.md` asserts 12/12 and a
  2.6x median this host does not reproduce. That should not land.

## Update after head `ef8b3797`

The author obtained review through the Claude Code CLI, and **corrected the
`12/12` overclaim** — their own reviewer measured 11/12, and they accepted it.
That correction is honest and is recorded here as such. Their own repeats on
that head: 12/0, 12/1, 12/1.

The test files are byte-identical between `0eebedee` and `ef8b3797`
(`git diff 0eebedee..ef8b3797 -- tests/` is empty), so the earlier
measurements carry over. One further test measured, the one that exercises
the changed `TIMING` dict on the `TimedLinkSession` path:

`test_paired_authored_full_frame_calls_preserve_count_render_buttons_and_events`,
2 runs per head:

| head | run 1 | run 2 |
| --- | --- | --- |
| master `dce56a2c` | F, 48.54 s | F, 48.28 s |
| PR #580 `ef8b3797` | F, 48.72 s | F, 48.72 s |

Identical terminal error on both heads:
`AssertionError: paired owners did not complete semantic work within 48s capacity`
(preceded by `_queue.Empty`).

This is the load-independence claim made concrete. The PR argues the gain is a
round-trip count and therefore contention-independent. If that were the binding
constraint here, this test — the paired end-to-end path through the changed
constant — would be materially faster on #580. It is 0.2–0.4 s **slower**, well
inside noise.

## Scope of this finding

The mechanism analysis has **not** been shown wrong. The clamp at
`emulated_time.py:428` and the unit conversion at
`timed_link_session.py:86`/`:259` behave as described, and a wider window does
allow more cycles per permit. Under a quiet host, or on a workload that is
genuinely round-trip-bound, this change could be a real improvement. What is
established is narrower: on this host it moves no named test, and every test
named in the PR still fails — two of them identically on both heads.

## RETRACTED: the mechanism claim in this finding was wrong

The sections above assert that `max_edge_lateness` "does not reach" the
request-deadline failure, and predict a null result from that. **That reasoning
was wrong, and the prediction was wrong with it.** PR #580's mechanism is
correct: the request deadline is wall-clock, the work needed to meet it is the
number of peer round trips, and the edge window sets how many cycles each
permit carries — so it does determine the round-trip count.

The error: I read `max_edge_lateness` as bounding only the *arrival* of an edge
and concluded it could not affect wall-clock work. It bounds the size of each
in-flight permit (`emulated_time.py:428`), which is the multiplier on round
trips. I never tested that claim directly; I inferred it from the source and
then treated my own null measurement as confirmation of it. Both the inference
and the confirmation were wrong.

### Direct A/B, one process, alternating, work held constant

Driving the repo's own paired authored workload (`authored_session`,
`start_endpoint`, `session.step`), alternating `max_edge_lateness` within a
single process so host-load drift hits both arms equally, and recording
`retired_instructions` so the work is provably the same in both arms:

| configuration | wall min | wall median | retired instructions |
| --- | --- | --- | --- |
| `lateness=32` | 130.51 s | 191.17 s | 90304, 90304 |
| `lateness=4096` | 72.88 s | 83.30 s | 90304, 90304 |

**Identical retired instructions in both arms** — the emulated work does not
change. Only the wall time does, by ~2.3x. That is round-trips and nothing
else, which is exactly the claim PR #580 made and I said could not be true.

### What this does and does not change

It does not rescue the *outcome* findings, which were measured independently
and still stand: the gate is not cleared, the 48 s paired capacity test still
fails on the merged tree, and #253 remains open. `POSTMERGE_580_MEASUREMENT_20261004.md`
already concedes all three.

It does mean the following claims in this file are withdrawn:

- that `max_edge_lateness` "cannot extend a request deadline";
- that the null result on `test_queued_cancel_preserves_active_real_epoch`
  (12.40 s vs 12.58 s) demonstrates the mechanism does not reach the failure;
- that the "third configuration" criticism implies the profile choice is wrong
  for this workload.

The 12.40 s / 12.58 s null measurement was real, but I drew the wrong
conclusion from it. That test's paired deadline is 12 s and **both** arms exceed
it at this load, so a null there is a floor effect, not evidence about the
mechanism. I should have escalated to a controlled A/B before concluding, and
the author did exactly that after I did not.

The profile-framing criticism (only one of three fields changed) remains valid
as a documentation-accuracy point; `POSTMERGE_580_MEASUREMENT_20261004.md`
concedes it, while also showing `rearm_budget`/`rearm_instruction_cap` are
inert for this workload.

## Not claimed

This is **not** a claim that the underlying #253 failures are environmental.
They may be load-induced; that remains unestablished. The finding is narrower:
this PR does not demonstrate that it changed any outcome, and it currently
records numbers the tree does not reproduce.

## Environment repair made while measuring

`/workspace/poke-harness/pokemon/.venv` failed the #534 import-origin guard:
setuptools 77.0.3 installs `_virtualenv.py` + `_virtualenv.pth` into
site-packages, but `_virtualenv.py` is not listed in setuptools' `RECORD`, and
`check_import_origins._is_imported_by_a_pth` requires one distribution to
attest **both** halves of the pair. A fresh venv on setuptools 66.1.1 does not
install those files and passes. Repaired by downgrading to 66.1.1 and removing
the two stale files; backup at
`/workspace/poke-harness/.venv-backup-pre-setuptools-downgrade`. The venv is
untracked, so no repository change. This is a pre-existing environment
artifact (file dates 2026-09-23), not a regression from this session, and it
would have blocked any local test run through the guard.
