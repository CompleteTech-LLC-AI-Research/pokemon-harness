# #106 matrix-concurrency benchmark implemented; policy selection withheld

Date: 2026-10-03
Branch: `fix/106-matrix-concurrency-benchmark`
Worktree: `/workspace/poke-harness/.scratch/wt106`
Base: rebased onto `origin/master` `df5c7aae`
Interpreter: worktree-local `.venv-wt106` (CPython 3.11.2), `pip install -e ".[dev]"`,
resolving `pokered_harness` and `pyboy` to this checkout.

## Scope delivered

`scripts/benchmark_matrix_concurrency.py` plus
`tests/test_matrix_concurrency_policy.py`, declared in `tests/_tier_config.py`,
documented as a new runbook section "3b. Benchmark matrix-worker concurrency".

The benchmark runs the declared trade/battle matrices at workers 1/2/4 in both
runtimes, records throughput, wall and reaped child CPU time, nearest-rank
per-case percentiles, deadline headroom, resource pressure and failures, and
refuses to select a worker policy unless every declared arm is complete,
passing and comparable and both runtimes agree on the best count.

## Defects found and fixed before first execution

This branch had never been run. Six defects were found by reading the code
against `production_gate.py` and then by executing the benchmark end to end.

| # | defect | consequence if shipped |
|---|---|---|
| 1 | `--runtime-mode native` | `production_gate.py` accepts only `source\|cython\|both`; every compiled arm aborts on argument validation |
| 2 | `--capacity-policy` never passed to the child | the gate reports `capacity admission is not enforced`, so the benchmark measures without the budget it exists to qualify |
| 3 | `POKERED_CAPACITY_POLICY` env var only | the gate reads the policy from the flag; the env var has no effect |
| 4 | compiled arm declared no interpreter | the gate maps `--python` onto the single runtime it runs, so dropping it measures the ambient interpreter instead of the declared native one |
| 5 | `--raw-output-dir` nested inside `--evidence-dir` | the gate rejects this unconditionally with `--raw-output-dir must be outside --evidence-dir`; every arm fails before running |
| 6 | raw directory pre-created by the benchmark | the gate requires `--raw-output-dir` to not yet exist and fails `FileExistsError` otherwise |

Each fix is pinned by a regression test. The gate's own behaviour was verified
by running the exact generated argv, not inferred: defects 5 and 6 were found
only by executing it.

## Two logic defects in the selection and accounting

Found by running the test suite, not by reading.

- `comparable()` included `effective_workers` in its uniqueness key. Effective
  concurrency is the *experiment*, and it differs across arms by construction
  (1, 2, 4), so `comparable()` returned False for every real arm set and
  `select_policy` could never select anything. It now compares runtime and
  required-row count, and a capacity-policy clamp is detected separately and
  reported by name, so a clamp is never folded into a worker-count effect.
- `select_policy` built its agreement vote from only the runtimes that
  *improved* on their own reference. A runtime whose best count differed but
  which merely matched its own reference was silently excluded, so source could
  select `workers=4` while native's best count was `1`, with no disagreement
  ever detected. The vote now spans every runtime with a complete result.

Three test fixtures were also wrong rather than the code: a throughput test
whose data contradicted its own assertion, a row count that did not sum to the
value it asserted, and a disagreement fixture whose second runtime never
improved on its own reference. A stray instance helper used as a function was
removed.

`percentile()` was reviewed and found correct. Nearest-rank p95 of `[5]` is `5`;
the review note suggesting it was wrong was mistaken.

## Verification

```
.venv-wt106/bin/python -m pytest -p no:randomly tests/test_matrix_concurrency_policy.py
44 passed, 1 warning in 0.41s

.venv-wt106/bin/python -m pytest -q -p no:randomly \
  tests/test_production_gate_strict_matrix.py \
  tests/test_production_gate_report_loader.py \
  tests/test_qualification_runner.py
130 passed
```

`ruff check` and `ruff format --check` pass on all three touched Python files.
`git diff --check` is clean.

End-to-end execution, one worker count, one tier, to prove the wiring:

```
$ python scripts/benchmark_matrix_concurrency.py --project-root . \
    --evidence-dir <fresh> --worker-counts 1 --tiers trade \
    --source-python .venv-wt106/bin/python \
    --native-python .venv-wt106/bin/python --arm-timeout-seconds 240
arm source/workers-1: passing=0 failed=0 wall=82.281s
arm native/workers-1: passing=0 failed=0 wall=32.027s
selection: unselected
  reason: source/workers-1 produced no passing row
  reason: native/workers-1 produced no passing row
```

Both arms executed, both produced a terminal non-passing result, and selection
was correctly withheld. The sibling `-raw` directories were confirmed on disk.
The report recorded the tested commit under `identity.head`.

## Fifth review round: three accounting defects and one test gap

A fourth independent review of `0c3a8498` raised three significant findings
and one test gap. All four are fixed here.

| # | finding | defect | consequence if shipped |
|---|---|---|---|
| 1 | arm timeout arithmetic | the default `--arm-timeout-seconds 43200` was below the serial worst case | the live manifest declares 43 trade rows at 900 s and 19 battle rows at 1200 s, so a legitimate `workers=1` arm may legitimately spend 61,500 s. A 43,200 s ceiling interrupts a valid slow baseline and reports an unsupported `unselected` that is indistinguishable from a measured result |
| 2 | worker count could exceed a tier's rows | `effective_workers()` considered only the request and the capacity ceiling | the gate caps `max_workers = min(matrix_workers, len(nodeids))` per tier, so a 44-worker request runs the 19-row battle tier at 19. Reporting 44 claims a concurrency the matrix cannot reach, and the existing clamp check would approve it as a supported count |
| 3 | a union of tier sets could select a full-matrix policy | the full-tier check used the union of tiers across all arms | selection succeeded with `workers=1` measuring only `trade` and `workers=2`/`4` measuring only `battle`. No worker count was ever measured on both tiers, yet a matrix-wide policy was selected |
| 4 | test gap | `test_unstarted_work_is_not_rewarded_as_speed` built a skipped arm but never passed it to `select_policy` | its `unselected` result came from the absent `workers=2`/`4` arms, so it would still have passed if unstarted rows stopped blocking selection |

### Disposition

1. The bound is derived, not hardcoded. `default_arm_timeout_seconds()` sums
   every declared selection-tier row charged at its full per-row budget, reading
   the row manifest from `tests._tier_config` and the timeouts from
   `scripts.production_gate_model` — the two sources the gate itself reads, so
   the bound tracks the live matrix instead of duplicating it. A 1.25 margin
   covers start-up and report writing; a 108,000 s floor applies when either
   source is unreadable, and the derived value is raised above the floor when
   the matrix grows. The CLI default is now `None`, meaning "derive", and
   `--arm-timeout-seconds` remains an explicit override. No per-row deadline is
   relaxed: this bounds only the harness's own child process.
2. `effective_workers()` now takes the declared tiers and returns the smallest
   of the requested count, the admitted pair ceiling, and the smallest tier row
   count. Row counts come from the report — `case_results` for a tier that ran,
   otherwise the declared `selected_nodeids` — so no row total is hardcoded. An
   unreadable row count yields `None`, which is already a selection blocker, so
   an unmeasurable arm can never be read as running at exactly the request.
3. Every arm must now individually contain the full required tier set. The
   per-arm reason names the arm and the missing tiers. `comparable()` also
   carries the tier set in its identity, so arms that measured different tiers
   are not comparable even when they somehow pass the per-arm check. The CLI
   builds one common tier list for every arm, so this is an API-correctness
   repair; the union form was reachable only by calling `select_policy` directly.
4. The test now passes the skipped arm in and asserts the explicit
   `not_started=1` reason, so it fails if unstarted rows stop blocking
   selection.

Seven regression tests were added: tier-clamped effective workers (44 -> 19),
the declared-selection fallback when no case rows exist, unknown row counts,
the union-of-partial-arms reproduction, a single-tier run, differing tier sets
breaking comparability, and the timeout derivation covering the live matrix,
the unreadable-manifest floor, and the CLI override.

Verification on this head:

```
.venv-wt106/bin/python -m pytest -o addopts="" -p no:cacheprovider \
  tests/test_matrix_concurrency_policy.py
73 passed, 1 warning in 1.39s

.venv-wt106/bin/python -m pytest -o addopts="" -p no:cacheprovider \
  tests/test_production_gate_strict_matrix.py \
  tests/test_production_gate_matrix_manifest.py \
  tests/test_production_gate_report_loader.py \
  tests/test_production_gate_run_tier_failures.py \
  tests/test_gate_capacity_policy.py \
  tests/test_gate_capacity_boundaries.py \
  tests/test_gate_capacity_interrupts.py \
  tests/test_gate_capacity_main.py
232 passed
```

`ruff check`, `ruff format --check`, `py_compile`, and `git diff --check` are
clean. End-to-end on this head, both runtimes at `workers=1` over
`trade,battle` with an explicit 240 s arm bound, returned `unselected` with the
expected per-arm reasons and no policy change.

## Policy selection is deliberately withheld

No worker policy is recommended and the `workers=1` default is unchanged.

Every real-ROM arm requires the five ROMs, three symbol files and external
fixture bytes. None are present: `rom/` does not exist and no `.gb`, `.gbc` or
`.sym` file exists anywhere. The capacity admission this benchmark is meant to
be measured against additionally requires the runner described in runbook §3c,
which does not exist (#85). The observed arms returned `BLOCKED` on absent
assets, which is the correct terminal result and not a benchmark failure.

## Release status

Unchanged: **PARTIAL**. Nothing in this branch relaxes a deadline, capacity
threshold, assertion, skip, xfail or gate, and no ROM-backed row was executed.
