# 2026-10-04 13:15Z — #253 root cause found; PR #580 opened; not merged

## Summary

The "paired timed-owner deadlock" recorded against #253 is a misdiagnosis.
Both owners make continuous progress. The defect is a throughput collapse:
one peer round trip per CPU instruction, because the test-side edge-lateness
budget is 128x smaller than the documented in-use profile.

## Root cause

`TimedLinkSession` hardcodes `enforce_completeness=True`, so
`EmulatedTimeCoordinator._reserve` bounds every permit by

    ceiling = min(peer + quantum, watermark + max_edge_lateness * 2)
    cycles  = min(max_cpu_cycles, (ceiling - local) // rate)

`before()` requests exactly one instruction (`required_cpu_cycles == 24`), so
`max_cpu_cycles = 24`. At `max_edge_lateness=32` the completeness window is
64 half cycles, so each permit grants one instruction and cannot be reissued
until the peer republishes its emission watermark.

Measured: 7832 permits in an 8s paired step, **zero** exceeded 48 half cycles.
`WAIT-PROGRESS` mean 2ms, max 73ms. `RESERVE` never returned `None`.

The step completes; it is ~2500x slower than ungoverned, so it lands at the
authored 12s pair deadline and reports expiry.

## Budget conflict

`docs/TIMED_FRAME_DEADLINE_PROTOCOL_20260921.md` §3 pins the owner policy at
`max_edge_lateness=4096` and calls it "the explicitly non-default profile
already in use"; `tests/test_mcp_timed_rom.py:51` defines it as
`ALIGNED_DIAGNOSTIC_PROFILE`. The shared helper shipped 32.

## Repair

PR #580, commit `0eebedee`, branch `fix/253-unit-tier-failures`, base `eb7f14d8`.
Three one-line test-side changes, `32` -> `4096`:

- `tests/_mcp_timed_remote_support.py`
- `tests/test_mcp_timed_stdio.py`
- `tests/test_session_timed_execution.py`

Source untouched: `git diff --stat eb7f14d8..HEAD -- src/ vendor/ scripts/` is empty.

## Evidence

Paired frame, four frames per configuration, same process, same authored ROM:

| configuration | min | median | max |
|---|---|---|---|
| pristine `lateness=32` | 25.90s | 36.69s | 38.15s |
| fixed `lateness=4096` | 11.75s | 14.15s | 14.41s |

Non-overlapping. A round-trip count is independent of host load.

`tests/test_mcp_timed_remote_cached_failure.py`: 12/12 pass (fails on `eb7f14d8`).

Guarantee not weakened — mutation check: disabling the `receive_edge` lateness
guard (`if self._local - at_half_cycle > self._max_lateness:` -> `if False:`)
breaks 6 tests in that green module. Mutant reverted; `git diff -- src/` empty.

Rejected hypothesis, recorded so it is not retried: a publication-cadence
invariant (`_threshold = quantum // 2` vs `max_edge_lateness * 2`). Equality at
`lateness=64` gave 11.68s vs 12.97s — equality does not restore throughput and
the response is monotonic across orders of magnitude. There is no threshold.

## Blockers — release remains PARTIAL

- **No independent review.** Three dispatches failed; every subagent received
  the Codex-settings AGENTS.md boilerplate as its whole task and replied about
  `config.toml` without opening a repository file. A fourth returned
  `unsupported call`. Reproduces the #489 failure mode: the call succeeds and
  the agent reaches `completed`, and only reply text reveals the task never
  arrived. Author may not self-approve, so #580 is NOT ready and NOT merged.
- **No controlled CPU allocation.** 4 CPUs, load average 13-17, concurrent
  agents. Whole-suite pass/fail counts are contention-dominated and change
  between runs on identical code.
- **No real ROMs.** No native or real-ROM qualification.
- Timing tier not requalified. #253 stays open.
