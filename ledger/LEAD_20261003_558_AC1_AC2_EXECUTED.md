# LEAD — #558: AC1 and AC2 closed by execution, not by reading the wiring

Date: 2026-10-03

```
trunk            : master = origin/master = 1f9f68b141b59884d620ebbab90611bfbf5d5ebb
divergence       : origin/master...master = 0 0
candidate branch : lead/556-const-collision
candidate head   : e05e7d1087cfae90e32f6f384750697b0b9fa5fb
review worktree  : /workspace/poke-harness/.scratch/rep557 (clean, at that head)
```

## The gap this closes

Round 9 (`LEAD_20261003_558_ROUND9_BOUNDARY.md`) left exactly two things
between #558 and a merge decision. One was an independent APPROVE. The other
was that #534's AC1 and AC2 had been established by *reading* the call graph in
`run_collection_preflight`, never by running it. This record closes the
second, by execution, at the exact candidate head.

## AC1 — a mis-pointed interpreter is refused

`scripts/production_gate_execution.py:262` shells
`scripts/check_import_origins.py --project-root <repo>` as a preflight, ahead of
any tier. Against a real target install whose packages resolve to a foreign
tree (`/workspace/poke-harness/.scratch/r558s/ac4/target-mispointed`, placed
first on `PYTHONPATH`), with the reviewed checkout declared as the root:

```
preflight status : FAIL
preflight rc     : 1
```

The guard's own machine-readable document, both packages FAIL, each naming the
foreign origin:

```
pokered_harness -> /workspace/poke-harness/.scratch/r558s/ac4/target-mispointed/pokered_harness/__init__.py
pyboy           -> /workspace/poke-harness/.scratch/r558s/ac4/target-mispointed/pyboy/__init__.py
detail          -> resolves outside this checkout ... and outside the
                   site-packages of an install made from it; the interpreter
                   is importing a different checkout
```

**AC1 met, by execution, rc=1 in the refusing direction.**

## AC2 — the gate refuses loudly, before the tier runs

`run_collection_preflight` returns one `CollectionResult` per entry point. With
the same refused interpreter:

```
import-origins  : FAIL         the selected interpreter imports source from
                               outside the checkout under test; every selected
                               tier would measure that other tree. Reinstall
                               the editable package against this checkout, or
                               select an interpreter bound to it.
python-module   : NOT_STARTED  not started because the import-origin preflight
                               reported FAIL
pytest-console  : NOT_STARTED  not started because the import-origin preflight
                               reported FAIL
```

Neither pytest collection entry point ran, and each carries a machine-readable
reason rather than a traceback. **AC2 met, by execution.**

### One honest qualification on the `import-origins` row

`import-origins` is itself listed as a collection result, and it reports
`FAIL`, not `NOT_STARTED`. That is the intended design, not a #558 defect:

```
git diff --name-only origin/master..lead/556-const-collision
  -> scripts/check_import_origins.py
     tests/test_import_origin_guard.py
     docs/PRODUCTION_RUNBOOK.md
     ledger/*
```

`scripts/production_gate_execution.py` is **not** in #558's diff, and
`git show origin/master:scripts/production_gate_execution.py` carries the same
`results: list[CollectionResult] = [origin_result]` and the same `NOT_STARTED`
short-circuit. The preflight is the thing *doing* the refusing, so it must
report its own failure. `NOT_STARTED` is the contract for the tier collection
entry points suppressed *because of* it, and both of those are `NOT_STARTED`.
AC2's requirement — fail loudly before the tier runs — is met, and is now
measured rather than inferred.

## Re-run of the guard suite at the same head

```
/workspace/poke-harness/.scratch/lead555venv/bin/python -m pytest -q \
    --override-ini addopts='' tests/test_import_origin_guard.py
rc=0    124 passed, 0 failed
```

## Evidence

```
AC1+AC2 script    : /workspace/poke-harness/.scratch/r558s/ac2/verify_ac1_ac2.py
AC1+AC2 run log   : /workspace/poke-harness/.scratch/r558s/ac2/ac1-ac2-run.log
AC1+AC2 json      : /workspace/poke-harness/.scratch/r558s/ac2/ac1-ac2-executed.json
guard machine doc : /workspace/poke-harness/.scratch/r558s/ac2/import-origins.log
AC4 repaired      : /workspace/poke-harness/.scratch/r558s/ac4/repaired.json   (rc=0 PASS both)
AC4 mis-pointed   : /workspace/poke-harness/.scratch/r558s/ac4/mispointed.json (rc=1 FAIL both)
```

## What is still open

AC1–AC4 of #534 are now met **by measurement** at `e05e7d10`. #558 is still
**not merged**: nine reviews have run (six INCOMPLETE, three REQUEST CHANGES
with real findings, zero APPROVE), and an author may not approve their own
change. Release remains **PARTIAL**; #547 stays open until #558 lands.

Next action: dispatch review round 10 against the exact head
`e05e7d1087cfae90e32f6f384750697b0b9fa5fb`, requiring a completed audit and an
explicit `APPROVE` or actionable findings. `INCOMPLETE` is not approval.
