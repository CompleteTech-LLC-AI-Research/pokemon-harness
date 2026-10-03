# Author verification of PR #564 (matrix-concurrency benchmark)

**Not an independent review.** Sub-agent task delivery failed in this
environment (see #489), so this record is the author's own verification. The
independent-review bar for this PR is **not** met by this document.

Date: 2026-10-03
Base: `e2b5b79` (master) Head: `2ffe5b0` (`fix/106-matrix-concurrency-benchmark`)
Reviewer checkout: `/home/agent/rev564`, detached at `2ffe5b0`, own fresh venv
`.scratch/rv` built with `pip install -e ".[dev]"`.

## Verdict

**Mergeable on author verification; independent review outstanding.** No
defect found in the work below. #106 stays **open** — this PR delivers the
measurement harness only, and the measurement cannot be run on this host
(no ROMs, no symbol files, no capacity policy). That matches the PR's own
stated scope.

## Scope check

`git diff --stat e2b5b79 2ffe5b0`:

```
 docs/PRODUCTION_RUNBOOK.md                        |  173 +
 ledger/LEAD_20261003_106_BENCHMARK_IMPLEMENTED.md |  358 +
 scripts/benchmark_matrix_concurrency.py           | 1409 +
 tests/_tier_config.py                             |   1 +
 tests/test_matrix_concurrency_policy.py           | 1524 +
```

The only shared file touched is `tests/_tier_config.py`, and the single
change is registering the new test module in `UNIT_MODULES`:

```diff
         "test_production_gate_diagnostics.py",
+        "test_matrix_concurrency_policy.py",
         "test_production_gate_matrix_manifest.py",
```

`git diff e2b5b79 2ffe5b0 -- scripts/production_gate.py
scripts/production_gate_model.py src/ pyproject.toml` is **empty**. No gate
default, deadline, threshold, skip, xfail or assertion is changed. The shipped
`--matrix-workers 1` default is untouched.

## Verification performed

| check | result |
|---|---|
| fresh venv `pip install -e ".[dev]"` | OK |
| `tests/test_matrix_concurrency_policy.py` | **105 passed** |
| 6 neighbouring gate/capacity suites | **172 passed** |
| `ruff check` on all 3 changed Python files | clean |
| `ruff format --check` | clean |
| `git diff --check` | clean |
| end-to-end harness run vs the real gate | terminal non-passing, selection withheld |

End-to-end on this tree (both runtimes, `workers=1`, `trade,battle`):

```text
arm source/workers-1: passing=0 failed=0 wall=52.559s
arm native/workers-1: passing=0 failed=0 wall=27.489s
selection: unselected
  reason: source/workers-1 is incomplete (incomplete=0 interrupted=0 not_started=62)
  reason: source/workers-1 produced no passing row
  reason: source/workers-1 has no admitted capacity ceiling; supply --capacity-policy ...
  reason: source/workers-1 gate verdict is not PASS (returncode=1)
  ... (same four reasons for native/workers-1)
```

Both arms reached a terminal verdict on absent assets and **no policy was
selected** — the correct outcome.

## Adversarial probes of `select_policy`

I drove the selector directly with synthetic `ArmResult` sets to confirm it
is fail-closed, rather than trusting its own test suite:

| probe | outcome |
|---|---|
| genuine improvement in both runtimes | `selected` (correct) |
| best count is 4 in both runtimes | `selected 4` (correct) |
| runtimes disagree on best count (source 4 / native 2) | `unselected` |
| workers=1 is the fastest | `unselected` |
| fast arm skipped half its rows (`not_started=31`) | `unselected` |
| fast arm failed rows | `unselected` |
| row id swapped inside a tier at identical counts | `unselected` |
| only `trade` measured, not `battle` | `unselected` |
| a declared worker count missing | `unselected` |
| declared counts not starting at 1 | `unselected` |
| duplicate arm at one worker count | `unselected` |
| undeclared worker count present in the results | `unselected` |
| arm-level capacity clamp | `unselected` |
| per-tier clamp only (arm-level looks clean) | `unselected` |
| no admitted capacity ceiling (`effective_workers=None`) | `unselected` |
| gate verdict not PASS | `unselected` |
| only one runtime present | `unselected` |

Every refusal path is reachable and every negative case refuses.

## Mutation testing of the guards

Four mutations injected into `select_policy`/`comparable`:

| mutation | caught? |
|---|---|
| drop the full-matrix requirement (`required_tiers = set()`) | **killed** by 3 tests |
| drop comparability (`if len(identity) != 1` to `if False`) | **killed** by 3 tests |
| rank arms by `clean_passing_per_hour` instead of `required_rows_per_hour` | survived — see below |
| ignore an arm-level capacity clamp | survived — see below |

### The ranking mutant is equivalent, not a gap

`clean_passing_per_hour = passing / wall` and
`required_rows_per_hour = required / wall`, and
`required_rows >= completed_passing` always, because
`required_rows = passing + failed + incomplete + interrupted + not_started`
(`scripts/benchmark_matrix_concurrency.py:258-266`).

But `select_policy` refuses any arm with
`incomplete | interrupted | not_started | failed != 0`
(`:973-980`), so every arm that can reach the ranking has
`passing == required`. The two rates are then equal up to the same constant,
the ordering is identical, and the mutant is unobservable. Confirmed
empirically: with a ranked fixture where the rates differ in raw value, real
code and mutant both returned `selected 4`.

This is defence in depth — `required_rows_per_hour` is the correct metric even
if the completeness refusal were ever weakened — so the current behaviour is
right. Noting it as an observation, not a defect.

### The clamp mutant is unreachable as written

`effective_workers` defaults to `workers` in the test fixture, so injecting a
clamp requires setting `effective=` explicitly. The real code path does set
it from the gate's own report, and I verified the clamp refusal directly
outside pytest:

```text
arm-clamp           -> unselected
tier-clamp          -> unselected
no-capacity-ceiling -> unselected
```

So the guard works; the mutation simply is not exercised by a test that
constructs a clamped arm through the default fixture. Coverage gap in the
test suite, not a defect in the harness.

## `default_arm_timeout_seconds` is fail-closed

The arm bound is derived from the tree's own `tests/_tier_config.py` and
`scripts/production_gate_model.py` rather than from an import, so a benchmark
pointed at another checkout cannot budget against the wrong matrix. Verified
degradation, all returning the conservative floor of 108000.0 s:

| condition | bound |
|---|---|
| missing tree | 108000.0 |
| reconstructed manifest (2 rows x 100 s) | 108000.0 |
| a tier's timeout absent | 108000.0 |
| a tier timeout that is `True` (bool guard) | 108000.0 |
| module raises on import | 108000.0 |

The floor is a floor, not a cap: a grown matrix raises the bound.

## Known limitation (not a defect in this PR)

`required_rows_per_hour` returns `0.0` when `wall_seconds <= 0`
(`:296-297`), which floors an instantaneous arm rather than ranking it
infinite. A zero-wall arm cannot occur through `run_arm` (it measures a real
subprocess), so this is unreachable in practice. The selector's real defence
against a bogus fast arm is the `required_rows < declared_rows` consistency
check at `:996-1007`, which fires long before wall time could matter.

## Disposition

Merge PR #564. Leave #106 open — the benchmark exists, the qualifying
measurement does not, and the PR is explicit that a partial deliverable must
not close its parent.
