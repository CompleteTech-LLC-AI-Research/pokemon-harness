# PR #580 — independent re-review of the retraction, and the legitimacy question

Date: 2026-10-04
Base: master `368d8262` (which already records the retraction)
Reviewed head of #580: `ef8b3797`, merged as `f52b995c`

## Why this file exists

`368d8262` retracts the mechanism claim in
`FINDING_580_LATENESS_WIDENING_UNSUPPORTED_20261004.md` and supplies the
controlled A/B. This file does not repeat that work. It records three things
the retraction left open: an independent confirmation of the mechanism from a
reviewer who had not seen the A/B, a reading of the protocol rule that decides
whether the change is legitimate, and the accuracy defects in #580's own commit
message that remain uncorrected.

## 1. Independent confirmation of the mechanism

Obtained via `claude1 -p` on a fresh prompt against `2a5c2bc5`, read-only. The
reviewer was given the code and asked to adjudicate the disagreement, not shown
the A/B numbers as evidence. Verdict: the retraction is **technically correct**.

Chain confirmed at `file:line`:

| step | location |
| --- | --- |
| `self._max_lateness = max_edge_lateness * 2` | `src/pokered_harness/link/emulated_time.py:186` |
| `if self._enforce_completeness:` | `src/pokered_harness/link/emulated_time.py:427` |
| `ceiling = min(ceiling, self._watermark + self._max_lateness)` | `src/pokered_harness/link/emulated_time.py:428` |
| `cycles = min(max_cpu_cycles, max(0, ceiling - self._local) // self._rate)` | `src/pokered_harness/link/emulated_time.py:430` |
| `enforce_completeness=True` on the paired path | `src/pokered_harness/link/timed_link_session.py:261` |
| one 24-cycle instruction reserved per callback | `src/pokered_harness/link/execution_adapter.py:293` |

So `max_edge_lateness` is a multiplier on instructions-per-permit and therefore
on peer round trips. 32 -> 4096 is 64 -> 8192 half-cycles, 128x.

The reviewer also confirmed the null result was a floor effect:
`PAIR_WORK_CAPACITY_S = 2 * 1 * (5.0 + 1) = 12s`
(`tests/_mcp_timed_remote_support.py:18-20`) and both arms exceed 12s at this
load, so the measurement could not have expressed a difference either way.

**Not verified by that review**, and still resting on terminal runs rather than
source: that `90304` retired instructions is what the paired workload actually
reaches, and that the 130.51s/72.88s figures came from an identical
configuration in both arms.

## 2. Is the change legitimate under the protocol rule?

`docs/TIMED_FRAME_DEADLINE_PROTOCOL_20260921.md:298-300`:

> Deadlines, assertions, and the selected profile are not tuning inputs, and an
> observed failure is never converted into a pass by a longer wait.

The rule names exactly three things. This change touches none of them:

- **Deadlines** — untouched. `PAIR_WORK_CAPACITY_S`, `BOUND`, `CALL_BOUND`,
  `EXIT_BOUND`, `PAIR_BOUND` are all unchanged.
- **Assertions** — untouched. None relaxed, none deleted.
- **The selected profile** — the change moves the test *toward* the documented
  profile (`max_edge_lateness=4096`, doc line 286); it does not select a new one.

The documented profile is described as the aligned diagnostic profile,
"explicitly non-default" and "already in use" (doc line 279). #580 is a
correction of a test that was running off-profile, not a relaxation of the
profile. Verdict: **legitimate as a test-configuration correction**.

This is a narrower claim than "the gate got faster". It is not the same as
saying the change is sufficient — see §4.

## 3. Accuracy defects that remain uncorrected

Both are in #580's commit message, not in the code.

**The commit message claims profile alignment it does not deliver.** It says the
change "aligns timed owner edge lateness with the in-use diagnostic profile",
but it changes one of the three fields, leaving `rearm_budget=32` and
`rearm_instruction_cap=16` against the documented `4096`/`1024`. The result is a
third configuration, matching one field of the profile.

Mitigating, and measured: the two untouched fields only apply to *held* edge
deliveries (`self._held_deadline` in `_reserve()`), and this workload has none,
so the half-aligned profile is behaviourally identical to the fully aligned one
for this test. It is still an inaccurate commit message, and the fully aligned
profile is the correct destination if the profile is ever completed.

**A superseded measurement is still asserted in the ledger.**
`ledger/LEAD_20261004T1315Z_253_LATENESS_ROOT_CAUSE.md` retains a "12/12 green"
claim. Its own author withdrew it (comment on #253, `2026-10-04T13:41Z`), and
repeat runs on this host give `12/0`, `12/1`, `12/1`. The withdrawal is public;
the ledger file still reads as a claim.

## 4. What this does not establish

#580 is legitimate and materially faster (~2.3-3x, confirmed twice by
alternating A/B and once by an independent reviewer). It is **not sufficient**,
and it does not clear #253.

- On merged master `test_queued_cancel_preserves_active_real_epoch` still fails:
  a 12.33s call against `PAIR_WORK_CAPACITY_S = 12.0s`.
- `test_paired_authored_full_frame_calls_preserve_count_render_buttons_and_events`
  still fails at the 48s paired capacity bound.
- The target bounds are 12s and 48s; the widened tree reaches 12.9-14.6s and
  ~48.7s. Faster, and still over the line.

That gap is the decision that matters, and it is a governance decision rather
than a measurement problem: a ~3x faster run still misses an authored deadline,
so the only remaining levers are widening the budget again or changing the
deadline — and the second is explicitly prohibited. Whether further budget
widening is legitimate is a question for a decision-maker, not something more
measurement can settle on this host.

Every timing above was taken at host load 13-24 on 4 CPUs while other agents
were running. Absolute seconds from those runs are not qualification-grade; only
the alternating A/B ratios are, because drift hits both arms equally. The issue
requires load <= 4 for timing qualification, which this host cannot currently
provide.

Release status stays **PARTIAL**. No real ROMs are available, so no native or
real-ROM qualification is possible.
