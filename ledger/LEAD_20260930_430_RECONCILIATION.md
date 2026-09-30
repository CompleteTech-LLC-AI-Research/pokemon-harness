# Reconciliation — #430, #427, and the false-live inventory

Date: 2026-09-30
Candidate: `4baef6c5f94d424559803e0c3692881899157fca`
`origin/master`: `6b72bf6` — unchanged. Release **PARTIAL**.

## Why this file exists

Three separate sessions filed or refined findings about the same underlying
defect while the candidate sat unreviewed. A fourth (`INDEP_REVIEW_4BAEF6C.md`)
concluded the guard is "behaviourally redundant". My measurement contradicts
that, so the claims had to be reconciled rather than averaged. Recording which
claim is correct and which is not.

## Claim-by-claim

| claim | source | verdict |
|---|---|---|
| The disabling mutation of `_starred_names_in_loop_target` survives the 698-test suite | `FINDING_4BAEF6C_...`, reproduced independently, #427 | **TRUE** |
| The mutation changes **no verdict** on any tested shape | `FINDING_4BAEF6C_...`, #427, my 8-shape search | **TRUE** |
| The guard is "behaviourally redundant" with `_starred_store_decides_kind` | `INDEP_REVIEW_4BAEF6C.md` §3, §4.1 | **FALSE — see below** |
| The guard is exercised by the suite | implied by the mutant surviving | **TRUE — 39 invocations** |
| The guard is **dead code** | my measurement, `FIND_20260930_430_...` | **TRUE** |
| Five false-live rows sit on the paths the guard does not cover | my measurement, filed on #430 | **TRUE** |

The two claims that look contradictory — "the mutation survives" and "the
guard is invoked 39 times" — are both true and are the signature of dead code.
A guard that is *redundant* with a live sibling would also survive, but it
would be reachable; here it is reached only on paths where a **different**
helper already produced the answer, so disabling it is unobservable. The
distinction is not cosmetic: on a redundant guard the uncovered paths would
still be correct, whereas here they are **wrong**.

## The five false-live rows

All measured on `4baef6c`; ground truth by executed CPython 3.12.14. In every
row a starred binding means `with` raises `TypeError` **before** the body, so
the contract is dead and `True` is wrong.

| id | where the `with` sits relative to the starred store | executed | analyser |
|---|---|---|---|
| **T2** | starred **store** in a loop body; `with cs:` a later **sibling** of the loop | dead | `True` |
| **G** | starred **`for` target** nested in `if flag:`; `with cs:` in that body | dead | `True` |
| **H** | starred **`for` target**; `with cs: pass` then a separate `assert` in the same body | dead | `True` |
| **G2** | starred **`for` target** in `if True:`; `with cs:` after the loop | dead | `True` |
| — | starred `for` target, **bare** `assert` (no `with cs`) | **assert escapes — LIVE** | `True` — correct |

The controlling rule: the fix fires only when the `with` header's statement
**is** the `For`/`Assign` carrying the starred target (T1, T3, T4 — all
correct). Every other relative position falls through to an unconditional
keep. Position is irrelevant to the answer, because a starred target always
binds a `list` and `with` on a `list` always raises. One defect, several
spellings.

## Disposition

- **All five are pre-existing**, wrong identically on `master` `6b72bf6`,
  `6a0c68c`, `a89c1d7` and `4baef6c`. The union neither introduced nor
  worsened them. Verified by execution on all four trees.
- **Not merge-blocking for `4baef6c`** — 0 regressions, 0 false-dead rows on
  the 35-shape executed comparison. The candidate strictly improves on every
  parent and never loses a correct verdict.
- **Owned.** #430 carries the root cause, all five rows, and the acceptance
  criteria. #427 remains open as the durability/coverage track; its
  recommended fix is now #430's criterion 2. Neither is closed.

## A measurement error I made and corrected

My first pass reported T2, H and G2 as "OK". The detector was wrong: it
flagged `analyzer=True` only when the runtime string contained `ASSERT`, and
`TypeError(DEAD)` does not — so every **false-live** scored as correct. Every
row in the tables above is from the corrected detector
(`analyzer=True` and runtime `DEAD` => mismatch). I record this because a
silently-wrong harness is the exact failure these reviews exist to catch, and
it briefly produced a false all-clear on three real defects.

## Open issue inventory (all verified OPEN this session)

| issue | subject | state |
|---|---|---|
| #418 | starred **store** target receives a list | OPEN |
| #420 | starred target reached through a **loop** still binds a list | OPEN |
| #422 | suppressor reached through a **subscript or attribute** | OPEN |
| #425 | loop target's surviving **non-enterable** element after the loop | OPEN |
| #426 | starred unpack **in the `with` header** | OPEN |
| #427 | loop-target guard is **not pinned** by any test (durability) | OPEN |
| #430 | starred loop target recognised on **one path only** — the five rows | OPEN |
| #428 | duplicate of #426 | CLOSED as duplicate, **not** as resolved |

## Merge gate

`4baef6c` remains **unmerged**. The prior review's own precondition — an
end-to-end independent review, since its mutation stage was a lead
reconstruction — is being pursued out-of-band via
`ledger/BRIEF_E2E_4baef6c.md`. No waiver of a finding, test, gate, or review is
authorised, and lead validation is not a substitute for one.

## Protected state

Both checkouts untouched. Lead checkout: 3 staged files
(`.github/workflows/release-hygiene.yml`, `scripts/run_local_ci.sh`,
`tests/test_local_ci_policy.py`). All pre-existing worktrees preserved.
