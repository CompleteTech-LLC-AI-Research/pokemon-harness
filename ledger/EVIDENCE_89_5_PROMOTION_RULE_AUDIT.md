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
terminal `passed` record for **every runtime the coverage scope mandates**
(`source` and `cython`, keyed to `coverage_version` rather than read from the
case) with the observed move effect matching the declaration. Every downgrade
direction is refused. Overall stays `INCOMPLETE` in all rows because 67
families remain unverified, which is correct.

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
FAILED test_mechanics_family_stays_unverified_with_missing_hashes
FAILED test_complete_pairing_does_not_hide_unverified_mechanics
FAILED test_report_states_one_turn_scope_separately_from_expanded_mechanics
```

A second mutation — emptying the `required` runtime set, so a single runtime
could promote — changed **no** test outcome. That is not a gap, and the reason
is worth stating precisely rather than as a general "redundant" claim, because
the obvious explanation for it is not the true one.

The obvious explanation is that the `all(status == "tested")` conjunct always
sees a report for the absent runtime. It does not. `build_report` iterates
`case["runtimes"]` — the runtimes the **case declares** — so a runtime that is
required but not declared produces no report at all, and an empty report set
would vacuously satisfy `all(...)`. The branch exists for that case.

The true reason is one layer up: `validate_catalog` already refuses a catalog
whose case or dimension omits a mandatory runtime, so `_verified_mechanics_case_ids`
never sees such a catalog in a promoting report. Measured on the real catalog by
deleting `cython` from the effect-0 case's `runtimes`:

```
coverage.required_cases[19].runtimes omits mandatory runtime(s)
for coverage_version 1: cython
```

and `_dimension_status` then zeroes `verified_cases` on `catalog_valid=False`,
so `tested=0` on both the dimension and the summary.

So the branch is belt-and-braces behind a validation gate, not the load-bearing
one, and the tests correctly bind to the conjunct. Its own docstring overstates
its role — it claims the check exists "so a catalog edit that removes a runtime
cannot make a family tested with partial evidence", which is really the
validator's job. That sentence is left alone here: the audit changes no
repository file, and rewording a load-bearing guard's docstring is a separate,
reviewed change.

## Gates

```
pytest tests/test_battle_coverage_mechanics.py \
       tests/test_battle_coverage_accounting.py \
       tests/test_battle_coverage_catalog.py      -> 70 passed
pytest tests/ -k "coverage or battle_scenario or catalog"
                                                     -> 244 passed, 8306 deselected
```

Both mutations reverted; `git status` clean against `7d7c2160`.

## Independent review

An independent reviewer read this document and the source with no prior
context and returned **MERGEABLE**, confirming the four gates are on production
paths, that exactly one input combination promotes, and that every refusal
direction holds. It raised one substantive objection and two wording points,
all three now corrected above:

- it argued the `required` early exit is *load-bearing*, because deleting it
  would let a case that omits a scope-mandated runtime promote on the reports
  that are present. The mechanism is real — `build_report` iterates the
  case's *declared* runtimes, so an omitted runtime yields no report at all.
  But the scenario is unreachable in a promoting report because
  `validate_catalog` refuses such a catalog first. Measured, not assumed.
- "every declared runtime" → "every runtime the coverage scope mandates", since
  the two can differ and the mandatory set is what the gate uses.
- the mutation list's "and 2 more" → both tests named.

## Disposition

**No code change.** The promotion rule is implemented, enforced in both
directions, and has teeth. #89.5 should not be reopened as an implementation
task.

#89 nonetheless stays open: 89.2 and 89.3 need real-ROM battle mechanics
qualification and fixture/observation contracts, and 89.5's *substantive*
requirement — promoting the remaining 67 families — needs terminal
source/native real-ROM evidence that does not exist on this host.
