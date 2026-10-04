# #253 — root cause: the documented unit-tier timeout truncates the tier on this host

Date: 2026-10-04
Tested commit: `0b3625c9`
Evidence: `target/feature-qualification/253-documented-0b3625c9/`
(run with the **documented defaults** — no `--timeout-seconds` override)

## What the documented command actually does

The asset-free smoke in `docs/PRODUCTION_RUNBOOK.md` (line ~477) is:

    python scripts/production_gate.py \
      --repo-root "$PWD" \
      --python "$(command -v python)" \
      --runtime-mode source \
      --unit-only \
      --repeat-timing 5 \
      --evidence-dir "$EVIDENCE_DIR" \
      --format text

It deliberately passes **no** `--timeout-seconds`, so each tier falls back to
`scripts/production_gate_model.py:80`:

    DEFAULT_TIMEOUT_SECONDS: dict[str, float] = {
        "smoke": 900.0,
        "unit": 900.0,      # <-- this one
        "local": 3600.0,
        "remote": 3600.0,
        "trade": 3600.0,
        "battle": 3600.0,
        "timing": 900.0,
    }

Consumption is at `scripts/production_gate_tiers.py:168,193`
(`tier_timeout = timeout_override or DEFAULT_TIMEOUT_SECONDS[name]`).

## Result of the documented command on this host

| tier | selected | ran | passed | failed | duration | rc |
|---|---:|---:|---:|---:|---:|---|
| `unit` | 8327 | **4142** | 4137 | 5 | 909.3 s | `124` |
| `timing_sensitive` | 425 x5 | 2125 | 2116 | 9 | 1729.9 s | `1, 0, 1, 1, 1` |

`overall: FAIL`

**The unit tier is truncated at 49.7% of its selected scope.** It selected 8327 node IDs and
finished 4142 before `rc=124` killed it at the 900 s ceiling. 4185 selected tests never ran. The
output tail shows pytest's own message, `pytest timed out after 900.0s`.

This is the concrete reason no prior #253 run produced a usable unit verdict: **every run so far has
been reporting a partial failure set as if it were the whole one.** The earlier
`--timeout-seconds 3000` run reached 6684/8327 (80%) before dying; the documented default is worse
still at 4142.

## Why the tier cannot fit in 900 s here

The unit tier needs roughly 2700 s to complete on this 4-CPU host at the contention levels seen
(load1 9-18, with a second independent gate and unrelated tenants running). 900 s is only about a
third of what the tier needs. So **the gate cannot return a complete unit verdict on this host
without an explicit `--timeout-seconds` override** — and the runbook's own smoke command does not
provide one.

This is a measurement-harness defect, not a code defect, and it is separable from the actual test
failures. It should be fixed by one of:

- raising `DEFAULT_TIMEOUT_SECONDS["unit"]` to a value the tier can actually complete in, or
- documenting the required override in `docs/PRODUCTION_RUNBOOK.md` §3b-0 alongside the other
  environment corrections already landed for #577/#578/#579.

Until one of those lands, **no `--unit-only` run on this host can be read as the unit verdict.**

## The timing tier completes — and it is flaky, not deterministically red

Unlike the unit tier, timing ran its full 2125. Per-iteration return codes were
`[1, 0, 1, 1, 1]`: **iteration 2 passed completely.** Distinct failures across the five iterations
was 6:

- `test_real_pair_repeated_public_frames_bounded_wire_volume[asymmetric-L64-half]` (iter 1)
- `test_process_spawn_failure_cancels_waiting_peer[True]` (iter 3)
- `test_real_partial_progress_active_interrupt_is_terminal[cancel|deadline]` (iters 4, 5)
- `test_authored_timed_stdio_pair_frames_and_cleanup[disconnect|peer_eof]` (iters 4, 5)

This is consistent with the instability already recorded on this issue (previously 9 -> 6 -> 5
failures across identical runs, and a 30-failure run before that). A whole iteration reaching
`rc=0` is new and useful: it shows the failures are **contention/timing-sensitive, not a stable
logic defect**, because identical code passed all 425 tests in one iteration and failed 4-6 in
others.

It also means a fix loop needs many iterations before it can distinguish "fixed" from "got lucky."

## The 5 unit failures observed before truncation

All in the timed MCP owner family, and all also present in the timing tier:

- `test_queued_cancel_preserves_active_real_epoch`
- `test_real_partial_progress_active_interrupt_is_terminal[cancel]`
- `test_duplicate_connection_preserves_existing_epoch_and_execution[listen]`
- `test_duplicate_connection_preserves_existing_epoch_and_execution[connect]`
- `test_mcp_tools_and_resource_share_persistent_native_owner`

I verified earlier this session that this exact set fails on **unmodified master** under the
writable-`/dev/shm` recipe, so it is pre-existing and not introduced by any of this session's
changes.

## Status

#253 stays **open**. Release status stays **PARTIAL**. This is measurement evidence about the
harness, not a claim that the gate passes.
