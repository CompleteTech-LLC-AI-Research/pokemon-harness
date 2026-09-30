# ADJUDICATION — `_starred_names_in_loop_target`: neither dead code nor redundant

Date: 2026-09-30
Candidate: `4baef6c5f94d424559803e0c3692881899157fca`
Worktree: `/home/agent/poke-harness/.scratch/lead_trace` (fresh, detached at the
exact SHA, verified clean)
Interpreter: CPython 3.12.14
Status: **lead measurement. NOT an independent review. NOT an approval.**

Two prior ledger entries made opposite claims about the same guard. Both are
wrong. This settles it with a line-level trace, a direct recording test, and an
in-suite call spy, so the next reviewer does not have to re-derive it.

## The two claims

| entry | claim |
|---|---|
| `FIND_20260930_427_GUARD_IS_DEAD_CODE.md` | the guard is **unreachable dead code**; a `For` store is always recorded `value=None`, the `value is None` arm `continue`s at 4998 before the `For` branch at 4999; recommends **deleting** the branch |
| `RETRACT_20260930_427_GUARD_IS_NOT_DEAD.md` | the guard is **live** — branch entered 21 times, guard asked 16 times, `kinds.add("list")` hit 0 times, so it is asked and always answers `False` |

## The measurement

### 1. Line trace over the real 698-test suite (`sys.settrace`, canonical instance)

| line | code | hits |
|---|---|---|
| 4964 | `if value is None:` | 253 |
| 4998 | `continue` | 1 |
| 4999 | `if isinstance(statement, (ast.For, ast.AsyncFor)):` | **21** |
| 5000 | (comment / first arm) | 0 |
| 5011 | `if _starred_names_in_loop_target(statement, name):` | **16** |
| 5012 | `kinds.add("list")` | 0 |
| 5013 | `continue` | 0 |

So the retraction's line numbers are exactly right. But `kinds.add("list")` at
**0** is not evidence the guard answers `False` — it is the *same* line the
dead-code claim would predict, and the two hypotheses are indistinguishable
from a line count alone.

### 2. In-suite call spy — the measurement that separates them

Wrapping the guard and running the real suite:

| quantity | value |
|---|---|
| total guard calls | **39** |
| returned **`True`** | **1** |
| returned `False` | 38 |

Locating the single `True` by stack inspection:

```
test_a_starred_target_reached_through_a_loop_binds_a_list   (tests/.../sentinels.py:4825)
```

**This falsifies the dead-code claim outright.** The guard is reached, it is
asked about a starred loop target, and it answers `True` — and that answer is
what makes the pinned row pass. It is also falsifies the retraction's central
inference: the guard does **not** always answer `False`.

Why line 5012 shows 0 hits while the guard demonstrably returned `True`: the
suite row reaches the guard through the **second** call site
(`_binds_starred_target`, line 7081), not through the value-shape loop at 5011.
The value-shape site's 16 calls are all on **plain** targets and all answer
`False` — so line 5012 legitimately shows 0.

### 3. Direct recording test — the dead-code premise is false

`_raw_store_values` does **not** record a `For` store as `value=None` in general:

| shape | target | recorded value | reaches the `For` branch? |
|---|---|---|---|
| `for *cs, in ((1,),)` | STARRED | **`object`** | diverts (value is not `None`) |
| `for cs in (1,)` | plain | `AST:Constant` | diverts |
| `for *cs, in (1, 2)` | STARRED | *not recorded* | — |
| `for *cs, in holder` | STARRED | *not recorded* | — |
| `for *cs, in g()` | STARRED | *not recorded* | — |
| `for cs in (1, 2)` / `holder` / `g()` | plain | *not recorded* | — |

The `object` sentinel is a **deliberate "recorded but unreadable" marker**, and
it is what routes a starred loop target away from the 4964 arm. The
dead-code claim rested on reading that sentinel as `None`.

### 4. Direct spy on live shapes — the guard decides real verdicts

| shape | guard calls | guard verdict | analyser |
|---|---|---|---|
| `for *cs, in ((suppress(),),): with cs:` | 1 | **True** | `False` (correct) |
| `for *cs, in ((1,),): with cs:` | 1 | **True** | `False` (correct) |
| `for *cs, in holder: with cs:` | 1 | **True** | `False` (correct) |
| `for cs, in ((suppress(),),): with cs:` | 1 | False | `False` |
| `for cs in (suppress(),): with cs:` | 0 | — | `False` |

The guard is what makes the starred rows correct. It is not decorative.

## Verdict

**The guard is live, load-bearing, and pinned by exactly one test row.**
`test_a_starred_target_reached_through_a_loop_binds_a_list` — the row
`INDEP_REVIEW_4BAEF6C.md` cites as the kill for
`_starred_store_decides_kind` — is *also* the row that exercises the loop-target
guard's `True` answer.

Both prior entries are therefore wrong in their conclusion:

- **Do not delete the branch** (the dead-code recommendation). The branch and
  the guard decide real verdicts; deleting them breaks a shipped row.
- **Do not treat the guard as answering `False` everywhere** (the retraction's
  inference). It answers `True` — once in the suite, and on every starred shape
  I tested.

## What actually survives, at the right strength

The real, narrow finding is a **precision** one, not a reachability one: the
suite contains **exactly one** row that exercises the guard's `True` answer
(line 4825), and **zero** rows that exercise it from the value-shape call site
(5011) with a starred target. That is why the disabling mutation survives:
16 value-shape calls all take the `False` arm, and the one `True` answer is
covered by a second guard, `_starred_store_decides_kind`, which absorbs the
outcome. So:

- the mutation-survival observation is **real** (`FINDING_4BAEF6C_...`, #427);
- the "unreachable / delete it" conclusion is **wrong**;
- the "always answers `False`" conclusion is **wrong**;
- the durable gap is that the guard is **pinned at exactly one point** and not
  across the paths #430 exercises.

`FIND_20260930_430_STARRED_LOOP_TARGET_SINGLE_PATH.md` — the five executed
false-lives — **stands unchanged** and remains the primary open defect. Those
are real, with executed-CPython ground truth, and independent of this
adjudication.

## Process note

Three ledger entries in one session made three different claims about one
function, and the two that most confidently asserted a mechanism were both
wrong. The line-level trace is what separated them. Recorded rather than edited
away so the reasoning is auditable and the next reviewer starts from the
measurement instead of re-arguing the interpretation.

## Status

`4baef6c` **unmerged**; `origin/master` `6b72bf6`; release **PARTIAL**. The
end-to-end independent review (`BRIEF_E2E_4baef6c.md`,
`INDEP_E2E_4baef6c.md` — still absent) remains the open merge gate, and the
dispatch channel has dropped its payload **34** times. No waiver is authorised;
lead measurement is not one.
