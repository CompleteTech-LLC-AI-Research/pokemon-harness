# Local CI failure attribution: the #106 benchmark PR causes no gate regression

Date: 2026-10-03
Issue: #106 (stays **open**)
Related open issue: #253 (stays **open**)
Released in: #564, merge commit `1325f139`, landed in `master`

## The claim under review

`bash scripts/run_local_ci.sh` exits **1** with `overall: FAIL` on this host.
That is recorded here as measured. **Local CI does not pass**, before or after
this work, and nothing in this document claims otherwise.

The narrower question is whether the matrix-concurrency benchmark caused any
of it. The answer is no, and this document now carries the full evidence
rather than a partial sample.

## Correction to an earlier version of this record

An earlier revision of this ledger claimed the failures were confined to six
modules. That was **wrong**, and an independent review caught it.

The six-module figure came from grepping the log for `FAILED tests/...` lines.
Those lines are truncated in this log. The gate's own structured
`iteration-failure` and `failure-detail` records name **thirteen** modules, and
seven of them never appear in a `FAILED` line at all:

```
tests/test_mcp_timed_remote_owner.py
tests/test_mcp_timed_remote_cached_failure.py
tests/test_probe_timed_trade_pair_driver.py
tests/test_probe_timed_trade_pair_proof.py
tests/test_timed_menu_milestone_sentinels.py
tests/test_timed_menu_milestones.py
tests/test_runtime_packaging_build_contract.py
```

Because the earlier comparison ran only the six visible modules, it did not
establish the claim it was making. The comparison below now covers all
thirteen.

## Gate totals, as measured on this host

```
unit   7377 total   7345 passed   29 failed
timing 2125 total   2031 passed   94 failed
capacity-policy: unavailable
  reason: no --capacity-policy supplied; capacity admission is not enforced
overall: FAIL
```

These counts are the gate's own. They are reproduced on unmodified `master`;
they are not introduced by this work.

## Full comparison: all thirteen failing modules, master vs this change

Two checkouts were used, each with its own virtualenv built by
`pip install -e ".[dev]"`: a **master baseline** detached at the merged
`1325f139` tree, and this **candidate** tree. Identical invocation on both,
no selection, no skips, no xfail, no synthetic gameplay:

```bash
python -m pytest -p no:randomly -q \
  tests/test_probe_timed_rom_pair_process.py \
  tests/test_probe_owner_phases.py \
  tests/test_probe_timed_rom_pair_startup.py \
  tests/test_probe_timed_rom_pair_menu.py \
  tests/test_session_timed_execution.py \
  tests/test_mcp_timed_remote_owner.py \
  tests/test_mcp_timed_remote_cached_failure.py \
  tests/test_probe_timed_trade_pair_driver.py \
  tests/test_probe_timed_trade_pair_proof.py \
  tests/test_timed_menu_milestone_sentinels.py \
  tests/test_timed_menu_milestones.py \
  tests/test_runtime_packaging_build_contract.py \
  tests/test_mcp_timed_stdio.py
```

Both exit 1. The master baseline fails **19** distinct tests; this candidate
fails **22**. The 19 are identical on both sides. The candidate additionally
showed these three:

```
tests/test_mcp_timed_remote_owner.py::test_real_partial_progress_active_interrupt_is_terminal
tests/test_session_timed_execution.py::test_paired_authored_full_frame_calls_preserve_count_render_buttons_and_events
tests/test_session_timed_execution.py::test_real_partial_public_tick_failure_counts_only_completed_frames
```

These were **not** assumed to be noise. Each was rechecked in isolation on
unmodified `master`, and each fails there too:

| test | isolated runs on unmodified master |
|---|---|
| `test_real_partial_progress_active_interrupt_is_terminal` | fails (run 1), and its sibling also fails |
| `test_paired_authored_full_frame_calls_preserve_count_render_buttons_and_events` | fails (run 1) |
| `test_real_partial_public_tick_failure_counts_only_completed_frames` | fails (run 3), passes (runs 4, 5) |

They are **load-dependent**, exactly the class #253's correction at `96c4c1ea`
identifies. They flip to passing on the same master tree within minutes, so the
difference between a 19-count and a 22-count on this host is contention, not a
code change. None of the three is in a module this candidate touches.

Two further host-dependent failures in the 19 are worth naming for the same
reason: `tests/test_mcp_timed_stdio.py::test_authored_timed_stdio_pair_frames_and_cleanup`
failed **3/3** isolated runs on unmodified master.

**The honest summary:** this candidate's failure set is a superset of master's,
and every member of the difference also fails on unmodified master. Nothing in
the candidate causes a gate regression.

## Why these fail at all

Two host-imposed causes, both tracked by **#253**:

1. `OSError: [Errno 30] Read-only file system` — the probe process family
   needs a POSIX shared-memory segment under `/dev/shm`. This host mounts
   `/dev/shm` read-only at 63 MB, so the segment cannot be created.
2. `TimedOwnerError: request deadline expired` and
   `AssertionError: paired owners did not complete semantic work within 48s
   capacity` — the measured single-frame cost on this 4-CPU host is ~11.48 s
   against a 5 s deadline. #253's correction at `96c4c1ea` classifies the
   split as 2 deterministic and 7 load-dependent, not 3 and 6.

## The change touches none of it

`git diff` between the merged `master` tree and this candidate over
`src/`, `pokered_harness/`, `scripts/production_gate*.py` and
`tests/test_mcp_timed_stdio.py` is **empty**. The timed-owner implementation,
the shared-memory probe and the gate logic are untouched.

The complete candidate diff is three code files plus this ledger and the
release note for the work:

```
.github/workflows/release-hygiene.yml  |  2 +
scripts/run_local_ci.sh                 |  2 +
tests/test_local_ci_policy.py           | 31 +
ledger/LEAD_20261003_106_LOCAL_CI_FAILURE_ATTRIBUTION.md
```

No ROM, `.gb`/`.gbc`/`.sym` file, save state, credential, cache or build
output is included, and the shipped `--matrix-workers` default remains `1`.

## A real defect this review did find

`scripts/` is enumerated explicitly in the Ruff lanes rather than globbed.
When #564 landed, `scripts/benchmark_matrix_concurrency.py` — 1409 lines of
new code — was added to neither the check lane nor the format lane in either
`scripts/run_local_ci.sh` or `.github/workflows/release-hygiene.yml`.

The existing lockstep test could not catch it: it only proves the runner and
the workflow list the *same* files, and both omitted the path together. This
candidate fixes the omission and adds a test that pins the benchmark to each
of the four lane/file combinations. The test was verified to fail when the
entry is removed from the check lane, and again when it is removed from the
format lane.

## Focused suites on this candidate

```
python -m pytest -p no:randomly -q tests/test_matrix_concurrency_policy.py tests/test_local_ci_policy.py
-> 116 passed
python -m ruff check <changed python files>      -> clean
python -m ruff format --check <changed files>    -> clean
bash -n scripts/run_local_ci.sh                  -> clean
```

## Disposition

The benchmark causes **no** local-CI regression. The failures are the
already-open #253 condition, reproduced identically on unmodified master on
this host.

Local CI remains **FAIL**. Release remains **PARTIAL**. #253 stays open and
#106 stays open: the harness ships, but the qualifying measurement still cannot
be run without real ROM and symbol assets and a capacity policy (#85). This
document is an attribution record, not a waiver.
