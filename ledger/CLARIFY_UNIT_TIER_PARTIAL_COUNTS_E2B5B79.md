# Clarification: the unit tier's `passed=1919` is a partial-progress artifact, not a pass

Date: 2026-10-03
Tree: `origin/master` `e2b5b79` (0 ahead / 0 behind after `git fetch origin`)
Worktree: `/home/agent/vpkg` on `ledger/clarify-setuptools`, clean tracked tree.

## The claim under review

A local CI run on this host produced a gate report whose unit tier recorded:

```json
{"name": "unit", "status": "FAIL", "duration_seconds": 906.22,
 "counts": {"passed": 1919, "failed": 0, "total": 1919, "errors": 0, "skipped": 0},
 "returncodes": [124],
 "iteration_failures": ["iteration 1: pytest timed out after 900.0s",
                        "iteration 1: pytest returned exit code 124"]}
```

Read naively this says "1919 tests ran, none failed, yet the tier is FAIL".
The apparent contradiction is a **reporting** artifact, not a test defect and
not a green tier.

## Root cause: partial-progress reports carry partial counts

`scripts/production_gate_execution.py:run_pytest_once` runs the tier's pytest in
a child process with two JSON sinks
(`scripts/production_gate_execution.py:727-728`):

- `POKERED_GATE_REPORT` — written only by `pytest_sessionfinish`, i.e. only on a
  clean pytest exit (`tests/_gate_report.py:129-134`).
- `POKERED_GATE_PROGRESS_REPORT` — rewritten every ~32 tests / 2 s from
  `pytest_runtest_logfinish` (`tests/_gate_report.py:106-126`).

When the wall clock expires the child is terminated
(`scripts/production_gate_execution.py:790-795`), so `pytest_sessionfinish`
never runs and the *final* report is absent. The runner then deliberately falls
back to the progress file
(`scripts/production_gate_execution.py:808-813`):

```python
if (timed_out or interrupted) and report.error:
    progress_report = _load_gate_report(progress_path, allow_partial=True)
    if not progress_report.error:
        report = progress_report
```

`_load_gate_report(..., allow_partial=True)` skips exactly one check — the
`collected != len(records)` assertion at
`scripts/production_gate_runtime.py:672-676`. Its docstring says so plainly:
the progress report "is allowed to contain the selected node set plus only the
terminal outcomes observed before termination. It is never accepted as a
complete report."

`Counts.from_report` then totals only the records present, so `total` means
"outcomes observed so far", not "tests selected". `run_tier` aggregates those
partial counts into the tier result (`scripts/production_gate_tiers.py:228-235`).

## Measured: 8206 selected, 1919 completed

Both numbers reproduce exactly on this tree.

Selected set, under the gate's own arguments
(`--strict-config --strict-markers -p pytest_asyncio.plugin -p tests._gate_report`,
with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` as
`scripts/production_gate_runtime.py:74` sets):

```console
$ POKERED_GATE_REPORT=/tmp/cr2.json python -m pytest tests -m unit \
    --strict-config --strict-markers -p pytest_asyncio.plugin \
    -p tests._gate_report --collect-only -q
gate-style collected: 8206
```

The report's own `selected_nodeids` length is **8206**, and the terminal
progress dots in `output_tail` stopped near 22 % of the run — consistent with
1919 completed items out of 8206.

## The "8276" figure from the earlier standalone run does not reproduce

A separate direct `-m unit` run was recorded earlier in this session as
"tests 8276, failures 26, skipped 3, time 1149.5 s". On this tree the same
selection collects **8206**, not 8276:

```console
$ python -m pytest tests -m unit --collect-only
8206/8429 tests collected (223 deselected) in 10.91s
```

8206 + 223 = 8429, which is also the gate's own `python-module` collection
total. So the unit tier selects a stable, fully accounted subset of the whole
suite; there is no hidden extra 70 tests, and no marker-selection bug. The
8276 figure does not correspond to any selection reproducible on `e2b5b79`
and should not be cited. It most likely came from an earlier tree state.

## Is a partial run ever reported as green?

No. The partial fallback is reachable only after the child was already killed
for timeout or interruption, and both of those produce a non-zero return code:

- `run_pytest_once` sets `returncode = 124` on timeout and `130` on interrupt
  (`scripts/production_gate_execution.py:786-795`).
- `run_tier` appends `f"pytest returned exit code {returncode}"` to `problems`
  unconditionally whenever the code is non-zero
  (`scripts/production_gate_tiers.py:244-245`), and non-empty `problems`
  forces `status = "FAIL"` via the `iteration_failures or any(code != 0 ...)`
  branch (`scripts/production_gate_tiers.py:353-354`).

The same file already refuses to let a broken runner go green on stale
progress: the fallback is guarded by "a missing or malformed final report on
an otherwise exited process must remain an error"
(`scripts/production_gate_execution.py:807-810`). So the design intent —
partial evidence is admissible for diagnosis, never for acceptance — holds.

This behaviour is pinned by existing tests:

- `tests/test_production_gate_run_tier_failures.py:241`
  `test_run_tier_timeout_preserves_partial_failed_record`
- `tests/test_production_gate_report_loader.py:539`
  `test_gate_report_loader_accepts_only_explicit_partial_progress`

## One real gap: batch tiers lack the `partial` marker matrix rows have

`MatrixCaseResult` carries an explicit `partial: bool`
(`scripts/production_gate_model.py:366`), set from
`timed_out or interrupted or report_kind == "partial"`
(`scripts/production_gate_matrix.py:421,443`), so a killed matrix row is
labelled in the report (`scripts/production_gate_matrix.py:421` for a killed
row, `:443` for a row never started). `TierResult` has no such field
(`scripts/production_gate_model.py:385-403`): a unit tier that died at 22 % and
one that completed both render as `counts.total = 1919` vs `counts.total = 8206`
with no explicit "this was partial" marker. The signal exists but only as the
implicit ratio `total < len(selected_nodeids)`.

That is a legibility gap in evidence, not a correctness gap: the tier is FAIL,
the return code is recorded as 124, and the timeout reason is in
`iteration_failures`. No change is made here — adding a field would require
reworking the report schema and its loader tests, which is out of scope for a
clarification pass and would not change any gate outcome.

## Disposition

- The unit tier did **not** pass. It was killed at the declared 900 s budget
  after completing 1919 of 8206 selected tests.
- `passed=1919, failed=0` describes only the observed prefix and must never be
  quoted as a tier result.
- The 900 s budget stays as declared. Raising it to fit a loaded 4-core host
  would mask the actual cause, which is host capacity — the same class of
  blocker already recorded for this host in
  `ledger/LEAD_20261003_253_CURRENT_HEAD_MEASUREMENT.md`.
- No repository change is required: the accounting is self-consistent and the
  acceptance path is already FAIL-closed.

Release status stays **PARTIAL**.

## Addendum: what the unit tier actually produces when it is allowed to finish

To settle the tier's real outcome rather than a 22 % prefix, the unit tier was
re-run to completion on this tree, without a wall-clock kill, using the gate's
own plugin and arguments:

```console
$ PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  POKERED_GATE_REPORT=.scratch/unitrun/final.json \
  POKERED_GATE_PROGRESS_REPORT=.scratch/unitrun/final.progress.json \
  .scratch/ci-venv/bin/python -m pytest tests -m unit \
    --strict-config --strict-markers -p pytest_asyncio.plugin \
    -p tests._gate_report -rA --maxfail=0
28 failed, 8175 passed, 3 skipped, 223 deselected, 9 warnings, 70 subtests
passed in 1088.18s (0:18:08)
```

`exitstatus 1`, `collected 8206`, and the final report's own record count is
8206 — `collected == len(records)`, the invariant the partial path is allowed
to break and this complete run satisfies. So the tier **does** complete on
this host in 1088 s; it is the declared 900 s budget, not the machine, that
truncates it.

### Failure decomposition (28 failures)

| cause | count | real defect? |
|---|---|---|
| A. `/dev/shm` mounted read-only | 23 | **no** — environmental, operator blocker |
| B. `duplicate_connection` deadline expiry | 2 | **yes** — the known #253 row |
| C. `mcp_timed_remote_owner` / `mcp_timed_stdio` timing rows | 3 | **no** — load-dependent, flake under contention |

Cause A is the same operator blocker already recorded for this host, and its
signature is identical in all 23 rows:

```text
OSError: [Errno 30] Read-only file system: '/dev/shm/pym-<pid>-<rand>'
```

Cause B is deterministic, not load: isolated three times in a row, the two
`test_duplicate_connection_preserves_existing_epoch_and_execution` rows
(`[listen]` and `[connect]`) fail every time with a 5.0 s
`OwnerRequest.result()` budget expiring. This reproduces the 2-deterministic /
7-load-dependent split already measured for #253, so #253's classification is
unchanged by this pass.

Cause C is load-dependent and does not survive isolation: all three
(`test_mcp_tools_and_resource_share_persistent_native_owner`,
`test_authored_timed_stdio_pair_frames_and_cleanup[disconnect]` and
`[peer_eof]`) **passed** when re-run in isolation with the machine otherwise
idle. Under a repeat run of only those rows, the owner-shared row failed
twice out of two, confirming it sits on a budget boundary rather than being
deterministically broken.

The 3 skips are also environmental, not lost coverage: one needs a CPython
include directory for a Cython `.pxd` compile check, and two need Python 3.12
class type parameters. The active interpreter is 3.11.2.

### Net effect on the 900 s budget question

Completing the tier needs ~1088 s of wall clock under host load average ~8–10
on 4 CPUs, against a declared 900 s budget. That is a ~20 % shortfall caused by
capacity, not by a code defect, and it is the same class of blocker as the
host-load limitation recorded in
`ledger/LEAD_20261003_253_CURRENT_HEAD_MEASUREMENT.md`. The budget stays
as declared; the honest statement is that this host cannot satisfy it, not
that the tier is red for a reason a wider budget would hide.

Even with a raised budget the tier would still be **FAIL** — 28 failures, of
which 5 are real-or-unclassified. So widening the timeout would not turn the
gate green, which is the second reason not to touch it.
