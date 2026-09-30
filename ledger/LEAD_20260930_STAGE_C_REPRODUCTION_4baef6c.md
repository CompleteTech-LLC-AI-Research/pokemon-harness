# Stage C reproduced by lead — mutation analysis of `4baef6c`

Date: 2026-09-30
Candidate: `4baef6c5f94d424559803e0c3692881899157fca`
Worktree: `/home/agent/poke-harness/.scratch/lead_mut_e2e` (created fresh, detached
at the exact SHA, verified clean before and after)
Interpreter: CPython 3.12.14
Status: **lead measurement. NOT an independent review and NOT an approval.**

`INDEP_REVIEW_4BAEF6C.md` returned `VERDICT: APPROVE` on correctness but named
its own precondition: its Stage C prose was a *lead reconstruction* over
independent artifacts, so the head was never certified end to end. This file
reproduces that stage from scratch in my own worktree. It closes a
*procedural* gap in my own validation. **It does not substitute for an
independent reviewer**, and it is not offered as one.

## Provenance

| item | value |
|---|---|
| HEAD | `4baef6c5f94d424559803e0c3692881899157fca` |
| pristine support blob | `31cd202540229beb95c4fb6be1967d6dc15f660f` — matches the prior review exactly |
| worktree clean before | yes |
| worktree clean after | yes, blob restored byte-identical to `31cd2025` |

All mutations were applied **uncommitted** in my own worktree. The mutated
blob is recorded each time so a mutated tree can never be mistaken for the
candidate.

## Result

| mutation | mutated blob | two-file suite (XML) | 10-shape | 9-shape | 3-shape | verdict |
|---|---|---|---|---|---|---|
| pristine | `31cd2025` | **698 / 0 / 0 / 0** | 0 | 4 | 2 | — |
| `_starred_names_in_loop_target` -> `False` | `02469b4b` | **698 / 0 / 0 / 0** | 0 | 4 | 2 | **SURVIVES** |
| `_starred_store_decides_kind` -> `False` | `d70f8b3a` | **698 / 1 / 0 / 0** | **1** | 4 | 2 | **KILLED** |

The kill for mutant 2 is exactly the row the prior review named:
`test_a_starred_target_reached_through_a_loop_binds_a_list[a starred store
inside a for body binds a list]`.

Two notes on method. Mutant 2 initially tripped `F841` (the `top` local became
unused), so I re-applied it in a lint-clean form rather than measure a mutant
that also fails lint — a confound, not a signal. And mutant 1's suite run
reproduces the prior review's numbers exactly: 698/0, probes 0/4/2.

## The surviving mutant changes nothing — confirmed, and so is the adjudication

Mutant 1 leaves the suite at 698/0, all three probes unchanged, **and all five
false-live rows unchanged** (T2, H, G2 still `True`; T1, T3, T4 still `False`;
#430G still `False` on this worktree's corrected detector). Disabling the guard
does not make anything worse and does not make anything better.

So the prior review's *observation* is confirmed. Its *explanation* — that the
guard is "behaviourally redundant" with `_starred_store_decides_kind` — is
what I dispute, and the mutant table above is the evidence:

- If the guard were redundant **and reachable**, the uncovered paths would
  still be **correct**, and #430's five false-lives would not exist.
- The guard is instead reached only when the queried `with` header's statement
  **is** the `ast.For` (value-shape loop, line 5011) or after the assign path
  has already declined (`_binds_starred_target`, line 7081). On both routes a
  starred `Assign` in the body is already covered by
  `_starred_store_decides_kind`, which mutant 2 proves is pinned.
- Off those routes the guard is never consulted, which is precisely where the
  five false-lives sit.

Dead code, not redundancy. The two claims differ in consequence: redundancy is
a tidy-up, dead code is a live defect with a coverage symptom.

## What this does and does not establish

Established: the candidate's behaviour is unchanged under both mutations; the
suite pins one guard and not the other; the pristine blob is the one reviewed.

**Still not established: an independent end-to-end review of the whole head.**
The dispatched reviewer channel has dropped its payload 33 times this session
(`BLOCKER_COLLAB_20260930H.md`), so the durable brief
`BRIEF_E2E_4baef6c.md` is written and ready for an operator-run or
out-of-band review. `4baef6c` stays **unmerged**; `origin/master` is `6b72bf6`;
release is **PARTIAL**. No waiver of a finding, test, gate, or review is
authorised, and lead measurement is not one.
