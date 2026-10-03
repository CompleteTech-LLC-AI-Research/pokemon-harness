# #253 re-measured on current master `1bdc1a50` -- determinism reclassification

Date: 2026-10-03
Tree: `origin/master` `1bdc1a50d4a65b066830c9e44b2ba6495dc1f7c8` (0 ahead / 0 behind)
Interpreter: `/workspace/poke-harness/.scratch/venv254clean`, Python 3.11.2,
`pip install -e ".[dev]"`, resolving `pokered_harness` to
`src/pokered_harness/__init__.py`.

## Purpose

The prior run (tree `b41f46d4`, `ledger/LEAD_20261003_253_CURRENT_HEAD_MEASUREMENT.md`)
asserted that the 9 non-`/dev/shm` failures were "confirmed in isolation" and
therefore "genuine defects rather than host-load flakes". This run tests that
claim directly, because the same assertion has now been refuted twice in review.

## Finding: the cause-B set splits into deterministic and load-dependent rows

The four cause-B files, run together, reproduce exactly 9 failures -- the same
count as recorded:

```
$ pytest tests/test_mcp_timed_remote_owner.py tests/test_mcp_timed_stdio.py \
    tests/test_session_timed_execution.py tests/test_timed_menu_milestone_sentinels.py
FAILED test_mcp_timed_remote_owner.py::test_real_partial_progress_active_interrupt_is_terminal[cancel]
FAILED test_mcp_timed_remote_owner.py::test_real_partial_progress_active_interrupt_is_terminal[deadline]
FAILED test_mcp_timed_remote_owner.py::test_duplicate_connection_preserves_existing_epoch_and_execution[listen]
FAILED test_mcp_timed_remote_owner.py::test_duplicate_connection_preserves_existing_epoch_and_execution[connect]
FAILED test_mcp_timed_remote_owner.py::test_mcp_tools_and_resource_share_persistent_native_owner
FAILED test_mcp_timed_stdio.py::test_authored_timed_stdio_pair_frames_and_cleanup[disconnect]
FAILED test_mcp_timed_stdio.py::test_authored_timed_stdio_pair_frames_and_cleanup[peer_eof]
FAILED test_session_timed_execution.py::test_paired_authored_full_frame_calls_preserve_count_render_buttons_and_events
FAILED test_session_timed_execution.py::test_real_partial_public_tick_failure_counts_only_completed_frames[endpoint]
```

But selecting *only those 9* and running them together yields **3** failures, and
a **different** subset. The cause-B rows are therefore not one population.

### Deterministic (3 rows, reproduced on every trial)

| row | trials failing |
|---|---|
| `test_duplicate_connection_preserves_existing_epoch_and_execution[listen]` | 3/3 |
| `test_duplicate_connection_preserves_existing_epoch_and_execution[connect]` | 3/3 |
| `test_mcp_tools_and_resource_share_persistent_native_owner` | 3/3 |

### Load-dependent (6 rows, intermittent)

`test_real_partial_public_tick_failure_counts_only_completed_frames[endpoint]`
**passed** when run as a single node with
`test_mcp_tools_and_resource_share_persistent_native_owner`, and **failed** in the
four-file run. Repeated trials of
`test_real_partial_progress_active_interrupt_is_terminal` passed twice and failed
on the third:

```
=== TRIAL 1 (load: 6.01) ===   (no FAILED)
=== TRIAL 2 (load: 6.31) ===   (no FAILED)
=== TRIAL 3 (load: 5.07) ===   FAILED ...active_interrupt_is_terminal[cancel]
```

**This corrects the prior "confirmed in isolation" claim.** Cause B was not
verified in isolation; it was observed inside a full-file run whose concurrency
and wall-clock pressure are not present in a targeted run. The prior ledger's
disposition table and its "isolated re-runs (not under gate load) reproduce
cause B, so these are genuine defects" sentence are not supported by this
evidence.

## Mechanism for the 3 deterministic rows

The failure is at `result(a)` in
`tests/test_mcp_timed_remote_owner.py:221`, raising
`TimedOwnerError: request deadline expired` from
`src/pokered_harness/mcp_timed_owner.py:142`. Both peers have an outstanding
`step` permit against `BOUND = 5.0` (`tests/_mcp_timed_remote_support.py:18`),
with `max_edge_lateness=32` and `quantum_cycles=256`
(`tests/_mcp_timed_remote_support.py:24-25,152-153`).

In `src/pokered_harness/link/emulated_time.py:425-427` the binding constraint is
the completeness watermark clamp:

```python
if self._enforce_completeness:
    # Bound the entire in-flight permit, including all rearm credit.
    # Peer progress alone does not attest to receipt of earlier edges.
    ceiling = min(ceiling, self._watermark + self._max_lateness)
```

`TimedLinkSession` always constructs the coordinator with
`enforce_completeness=True` (`timed_link_session.py:259-261`), so each side
reserves only up to `watermark + max_edge_lateness`. Two peers each needing the
other's watermark to advance can stall until the 5 s request bound expires.
This is consistent with the prior independent review's finding that permit
batching has 1.00x headroom and the watermark, not `_threshold`, is the real
bound. It remains consistent with that review; it does not depend on it.

## Host state during this run

`/dev/shm` is still `ro,nosuid,nodev,noexec,relatime,size=64000k` with 63 MB --
unchanged, so the 79 cause-A rows are untouched and still not fixable in-repo.
Load ranged 3.53-7.17 with cpu pressure avg10 peaking at 30.58. Those numbers are
above the issue's own criterion 3 bar (`load1 <= 4`, `avg10 < 50`) at the upper
end, which is why the 6 load-dependent rows cannot be resolved here either way:
they need a runner that meets the criterion, which #85 does not yet provide.

## Disposition

| # | criterion | status |
|---|---|---|
| 1 | every unit failure given a documented, non-waived disposition | **improved** -- cause B now split into 3 deterministic and 6 load-dependent rows |
| 2 | `production_gate.py --runtime-mode source --unit-only` reports `overall: PASS` | **not met** -- 79 cause-A rows are structurally impossible while `/dev/shm` is read-only |
| 3 | timing tier re-evaluated on a qualified runner | **not met** -- this host does not meet criterion 3 for the whole run; no qualified runner exists (#85) |
| 4 | independent review + gate re-run on exact merge tree | **not met** -- #489 |

The 3 deterministic rows remain a real open defect and are the honest target for
future work. The 6 load-dependent rows should not be counted as fixed by any
code change until they are observed stable across repeated runs on a runner that
meets criterion 3.

No deadline, capacity window, CPU requirement, skip, xfail, or gate was
relaxed. The gate is not being called green. Release status stays **PARTIAL**.
