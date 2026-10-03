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

## Sixth review round: four findings on `c26c8148`

A fifth independent review of `c26c8148` raised four findings. All four were
confirmed and fixed; the reviewer reproduced the first two directly.

| # | finding | disposition |
|---|---|---|
| 1 | `select_policy` never rejected arms at undeclared worker counts, so a clean `workers=8` arm could be ranked and selected while the experiment only declared 1, 2 and 4 | fixed — an arm whose `requested_workers` is not in the declared set is refused by name. The CLI only builds declared arms, so this was an API-correctness gap in the selector's own stated contract |
| 2 | `comparable()` compared a combined row total, so a manifest change between sequential arms could hide behind an unchanged total | fixed — comparability is now decided per tier from the arm's recorded `tier_row_totals`, with the combined required-row count still part of the identity. Arms run sequentially, so 43 trade + 19 battle becoming 42 + 20 keeps the total at 62 while trading a cheap row for an expensive one |
| 3 | `_declared_matrix_worst_case_seconds` imported by module name, so it returned whichever copy was already imported rather than reading `project_root` | fixed — this was a genuine bug in the code added minutes earlier. Reproduced: a nonexistent root returned this checkout's 61,500 s. Both sources are now loaded by file path under the requested root, with no `sys.path` mutation and no `sys.modules` leakage. The reviewer was right that the previous floor test passed only because the floor masked the import error |
| 4 | `effective_workers` reported one number for an arm whose tiers genuinely ran at different concurrency | accepted as a reporting defect, fixed as reporting — the smallest per-tier value remains the correct value to govern a matrix-wide policy, and rejecting that arm is correct. But the arm report now also carries `tier_effective_workers`, so a request of 30 records `{"trade": 30, "battle": 19}` instead of presenting 19 as if it described both tiers |

Finding 3 also required a real fix to the load itself: `production_gate_model.py`
executes `@dataclass` at import time, and dataclasses resolve string
annotations through `sys.modules[cls.__module__]`, so a module executed outside
`sys.modules` raises `AttributeError`. The loader registers the module under a
private name for the duration of execution and restores the previous state
afterwards, so nothing shadows the real `tests` or `scripts` packages.

Eight regression tests were added: undeclared worker counts cannot win, a
per-tier row shift is incomparable, matching per-tier rows are comparable, an
arm without recorded rows is incomparable, per-tier concurrency is reported
per tier, per-tier concurrency is unknown without a readable row count, an
unreadable root does not borrow another checkout's manifest, and a synthetic
larger matrix raises the derived bound above the floor.

Verification on this head:

```
.venv-wt106/bin/python -m pytest -o addopts="" -p no:cacheprovider \
  tests/test_matrix_concurrency_policy.py \
  tests/test_production_gate_strict_matrix.py \
  tests/test_production_gate_matrix_manifest.py \
  tests/test_production_gate_report_loader.py \
  tests/test_production_gate_run_tier_failures.py \
  tests/test_gate_capacity_policy.py \
  tests/test_gate_capacity_boundaries.py \
  tests/test_gate_capacity_interrupts.py \
  tests/test_gate_capacity_main.py
313 passed, 1 warning in 14.11s
```

Each of the four findings was re-checked by re-running the reviewer's own
reproduction against the fixed code:

```
finding1 -> unselected ['source/workers-8 ran at workers=8, which is not a declared worker count [1, 2, 4]', ...]
finding2 -> False unselected
finding3 -> 0.0 108000.0
finding4 -> {'trade': 30, 'battle': 19} 19
```

The reviewer confirmed the two findings from the previous round are closed: the
per-arm tier coverage check does close the union-of-partial-arms case, and the
skipped-arm test now asserts a reason tied to that arm. It found no relaxed
deadline, threshold, skip, xfail, or gate in the diff. Its test suite could not
run in its read-only sandbox because pytest could not create a temporary file,
so its counterexamples were reproduced by importing the module directly rather
than by running the suite.

## Seventh review round: three findings on `50f1a19e`

A sixth independent review of the rebased head `50f1a19e` raised three
findings. All three were confirmed and fixed; the reviewer reproduced each one
by importing the module directly.

| # | finding | disposition |
|---|---|---|
| 1 | selection never checked that recorded outcomes covered the declared rows | fixed — an arm declaring 62 rows but recording one passing row passed every other check: nothing failed, nothing was skipped, and the positive-passing test was satisfied by that single row. Selection now refuses an arm whose recorded outcomes fall short of its declared rows |
| 2 | comparability was checked only within a runtime, so the two runtimes could measure different matrices | fixed — the runtimes run sequentially and one worker policy must satisfy both, so a per-runtime check cannot see a manifest change between the source and native blocks. The selector now also requires both runtimes to report the same per-tier row counts |
| 3 | a pre-dispatch `BLOCKED` tier reported zero rows | fixed — `production_gate_matrix` returns `BLOCKED` with `selected_nodeids` and no `case_results` when required assets are missing. Reading only the aggregates returned zero, dropping all 62 declared rows from the denominator and describing an arm that attempted nothing as one that measured nothing. Those rows now count as unstarted |

The reviewer also found that the `_arm` fixture defaulted to 18 passing outcomes
against 62 declared rows, so every positive selection test was passing on
evidence the selector would now refuse. The fixture default is now the full
declared matrix, which makes the positive tests describe arms that really did
produce an outcome for every row they declared.

Each finding was re-checked by re-running the reviewer's own reproduction:

```
finding1 -> unselected ['source/workers-1 recorded 1 outcome(s) for 62 declared row(s); 61 row(s) produced no outcome']
finding2 -> unselected ["runtimes did not measure the same matrix; per-tier row counts differ between them: ..."]
finding3 -> {'completed_passing': 0, 'failed': 0, 'incomplete': 0, 'interrupted': 0, 'not_started': 62}
```

Seven regression tests were added: a single passing row cannot satisfy a 62-row
declaration, a full arm is unaffected by the new check, a partial single-tier
declaration is not flagged, runtimes with different per-tier rows are refused,
matching runtimes remain selectable, a blocked tier counts all its declared
rows as unstarted, and a blocked tier with no declared rows is incomplete.

The reviewer confirmed the derived serial row budget is 61,500 s against a
108,000 s default bound, and found no deadline, threshold, skip, xfail, gate, or
assertion relaxation in the diff.

## Eighth review round: four findings on `29ed5bb4`

A seventh independent review raised four new findings. All four were confirmed
and fixed. Its first finding was against `50f1a19e` and is stale: it confirmed
on the actual head that the declared-row check added in the seventh round
already refuses that case.

| # | finding | disposition |
|---|---|---|
| 2 | comparability used per-tier counts, so a manifest swapping one node id for another at identical counts was still comparable | fixed — the gate reports exact `selected_nodeids`, so comparability now compares the recorded row ids per tier, within a runtime and across the two |
| 3 | outcomes exceeding the declared rows were accepted | fixed — only a shortfall was checked. An arm recording 63 outcomes for 62 declared rows inflated the denominator used to rank arms; the mirror case is now refused too |
| 4 | duplicate arms at the same worker count could fake an improvement | fixed — with only `workers=1` declared, two runs per runtime at 120 s and 60 s ranked the repeat above the reference and reported variance as a concurrency effect. Exactly one result per runtime and worker count is now required |
| 5 | `passing_per_hour` reported a required-row rate | fixed — the value divides every declared row by wall time, so for 61 passes and one failure in 10 s the JSON claimed 22,320 passing/hour against an actual 21,960. Renamed to `required_rows_per_hour`, with `clean_passing_per_hour` now emitted alongside it rather than only existing as an in-memory property |

Each was re-checked by re-running the reviewer's own reproduction:

```
finding2 -> comparable= False selection= unselected
finding3 -> unselected
finding4 -> unselected
finding5 -> required= 22320.0 clean= 21960.0 old_key= False
```

Eleven regression tests were added covering swapped row ids at identical
counts, matching ids, missing ids, reading identity from the gate report,
unreadable identity, outcomes exceeding declarations, duplicate arms, one arm
per count, and both throughput labels.

### A phantom row surfaced by the eighth round's own check

Running the benchmark end to end after the eighth round produced a reason the
code had never emitted before: `recorded 63 outcome(s) for 62 declared row(s)`.
The extra outcome was `run_arm`'s synthetic "incomplete" sentinel, added
whenever the gate exited non-zero without another classification. That
sentinel is not a row the matrix declared, so it inflated the arm's
required-row total above the declaration — and the consistency check added in
the same round correctly reported the inconsistency it created.

The sentinel was redundant. `gate_passed` already blocks selection on its own,
so removing it preserves the real row counts exactly as the gate reported them
without weakening any refusal. The end-to-end run on the fixed head shows both
runtimes at `not_started=62` with no phantom row and no spurious reason. The
test that asserted the sentinel was rewritten to assert the contract it stood
in for: a non-PASS verdict blocks selection, and the required-row total equals
what the gate actually reported.

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
