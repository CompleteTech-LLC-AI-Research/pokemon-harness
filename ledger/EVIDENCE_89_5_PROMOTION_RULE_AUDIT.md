# #89.5 promotion-rule audit — the rule already exists, and it holds

Date: 2026-10-04
Master audited: `7d7c2160`
Worktree: `/workspace/poke-harness/.scratch/tmp895/wt` (detached, disposable)
No repository file was changed by this audit.

## Question

#89.5 asks for "promotion and exclusion rules": record planned/excluded
families explicitly, and **require terminal source/native evidence before a case
becomes `tested`**. The exclusion half was closed by #575 (`7d7c2160`). This
audit asks whether the promotion half is missing, which the issue decomposition
suggested ("the promotion half still requires terminal source/native evidence
before a family can become `tested`, and that is not implemented").

**It is implemented.** That earlier characterization was wrong, and is corrected
here.

## Where the rule lives

`scripts/_coverage_report_build.py`, four cooperating gates:

| gate | enforces |
|---|---|
| `_evaluate_case` (l.138) | a case is `tested` only for an accepted terminal outcome, with matching runtime/build identity, pinned PyBoy version+revision, matching commit, and collection evidence |
| `_verified_mechanics_case_ids` (l.223) | a case is verified only when **every** required runtime carries a `tested` record |
| `_check_case_effect` (l.438) | the **observed** move effect must equal the declared `effect_id` |
| `_family_is_verified` (l.267) | a family is verified only when **all** its evidence cases are verified and declared for its own effect |

## Measured promotion matrix

Built through the real public API (`coverage.result_set_from_document` +
`coverage.build_report`) using the repository's own canonical document builder
(`tests/_battle_coverage_support._passing_document`), not by calling internals
with hand-made rows.

Accepted outcome is `passed`; required runtimes are `source` + `cython`.

| scenario | dimension status | tested |
|---|---|---|
| both runtimes passed, effect 0 observed | `PARTIAL` | **1** |
| effect 0 on source only, cython absent | `PLANNED_UNVERIFIED` | 0 |
| observed effect 6 against declared effect 0 | `PLANNED_UNVERIFIED` | 0 |
| passed but no observed effect | `PLANNED_UNVERIFIED` | 0 |
| case omitted from results entirely | `PLANNED_UNVERIFIED` | 0 |
| skipped / xfailed / xpassed / failed | `PLANNED_UNVERIFIED` | 0 |
| status `"tested"` (the internal sentinel) | `PLANNED_UNVERIFIED` | 0 |

Exactly one row promotes a family, and it is the one the leaf requires: a
terminal `passed` record for **every** declared runtime with the observed move
effect matching the declaration. Every downgrade direction is refused. Overall
stays `INCOMPLETE` in all rows because 67 families remain unverified, which is
correct.

## Teeth

Removing the terminal-status requirement from `_verified_mechanics_case_ids`
turns **10 tests red**:

```
FAILED test_mechanics_family_stays_unverified_without_both_runtimes
FAILED test_mechanics_family_stays_unverified_with_wrong_effect
FAILED test_mechanics_family_stays_unverified_with_missing_effect
FAILED test_mechanics_family_stays_unverified_with_non_terminal_evidence[skipped]
FAILED test_mechanics_family_stays_unverified_with_non_terminal_evidence[failed]
FAILED test_mechanics_family_stays_unverified_with_non_terminal_evidence[timed_out]
FAILED test_mechanics_family_stays_unverified_with_non_terminal_evidence[not_run]
FAILED test_mechanics_family_stays_unverified_with_mismatched_hashes
   ... and 2 more
```

A second mutation — emptying the `required` runtime set, so a single runtime
could promote — changed **no** test outcome. That is not a gap: when a required
runtime is absent its case report is `missing`, so the `all(status == "tested")`
conjunct still refuses. The `required` set is a redundant early exit, not the
load-bearing gate, and the tests correctly bind to the load-bearing one. Worth
recording so a future reader does not mistake it for an untested branch.

## Gates

```
pytest tests/test_battle_coverage_mechanics.py \
       tests/test_battle_coverage_accounting.py \
       tests/test_battle_coverage_catalog.py      -> 70 passed
pytest tests/ -k "coverage or battle_scenario or catalog"
                                                     -> 244 passed, 8306 deselected
```

Both mutations reverted; `git status` clean against `7d7c2160`.

## Disposition

**No code change.** The promotion rule is implemented, enforced in both
directions, and has teeth. #89.5 should not be reopened as an implementation
task.

#89 nonetheless stays open: 89.2 and 89.3 need real-ROM battle mechanics
qualification and fixture/observation contracts, and 89.5's *substantive*
requirement — promoting the remaining 67 families — needs terminal
source/native real-ROM evidence that does not exist on this host.
