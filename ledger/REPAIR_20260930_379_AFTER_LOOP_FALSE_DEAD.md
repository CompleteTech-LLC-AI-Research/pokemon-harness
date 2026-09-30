# Repair: #379's loop-target rule read the wrong element (false-DEAD)

Candidate repaired: `lead/integ-379-392-on-master` (was `b087b33`)
Base: `origin/master` = `6b72bf62b5722e1df2c44c988bbdb14803024032`

## The finding, and where it came from

An open review comment on PR #379 reported a **blocking** finding: merging
#379 onto master introduces a *false-DEAD* — a header CPython really enters
certified as defeated, the damaging direction. It named the fixture:

```python
for cs in (1, contextlib.nullcontext()):   # after the loop, cs is a nullcontext
    pass
with cs:
    assert x != 1                          # FIRES
```

I reproduced it on both trees by execution. It is real, and it is a
**regression against master**, not a pre-existing defect.

## Root cause

`_loop_value_source` returned the iterable's **first** element:

```python
if isinstance(iterable, (ast.Tuple, ast.List)) and iterable.elts:
    return iterable.elts[0]
```

That is right for a target read *inside* the loop body, where the name holds
whatever the current iteration is on. It is wrong for a target read *after*
the loop, where the iterable is exhausted and the name holds the **last**
element. So `for cs in (1, nullcontext())` was read as binding the `1` — an
int that cannot be entered — and the live assert was reported defeated.

## Why the suite was green

Every shipped loop row uses a **one-element** iterable, where first and last
are the same node. The distinction is unobservable in the existing tables, so
464 passing rows said nothing about it. A test count could not have caught
this; only executing a multi-element fixture did.

## The fix

Return the last element:

```python
        return iterable.elts[-1]
```

The in-loop rows are unaffected because a bare target takes the whole
right-hand side either way, and every shipped in-loop row is single-element.
The docstring is corrected to state which question the rule answers and to
record the regression.

## New coverage

`AFTER_LOOP_TARGET_ROWS` (6 rows) plus
`test_a_loop_target_reads_the_last_element_after_the_loop`. Each row is
**executed** before it is judged, so a `defeated` expectation is only
credible when CPython really swallows the assert, and a row whose expectation
disagrees with CPython fails as stale rather than passing silently.

Note on the harness: the first version of this test treated a *fired* assert
as a broken fixture — the same class of error this repo has hit repeatedly.
For these rows a fired assert **is** the live answer, so the test now records
the runtime outcome and requires it to match the row's expectation.

## Validation

| check | result |
|---|---|
| sentinel + milestone lane | **470 passed, 0 failed, 0 errors, 0 skipped** (was 464; +6 new rows) |
| `ruff check` both files | clean |
| mutation: fix reverted (`elts[-1]`→`elts[0]`) | **5 failures**, all 5 multi-element rows |
| mutation: #379 self-alias fix reverted | 1 failure (unchanged) |
| mutation: #392 carrier guard reverted | 4 failures (unchanged) |

## Execution-grounded A/B in the regression zone

After-loop reads, judged by what CPython does:

| iterable | truth | master | candidate |
|---|---|---|---|
| `(nullcontext(),)` | live | live ✓ | live ✓ |
| `(suppress(),)` | dead | live ✗ | dead ✓ |
| `(1, nullcontext())` | live | live ✓ | live ✓ |
| `(1, suppress())` | dead | live ✗ | dead ✓ |
| `(nullcontext(), suppress())` | dead | live ✗ | dead ✓ |
| `(suppress(), nullcontext())` | live | live ✓ | live ✓ |
| `(nullcontext(), 1, suppress())` | dead | live ✗ | dead ✓ |

**master 3/7, unfixed candidate 3/7, repaired candidate 7/7.** Before the fix
the candidate was wrong on 4 of these, including the two damaging false-DEADs.

Regression guards all still correct (executed): plain live assert, simple
suppress, nullcontext, `if False`, `try/except` swallow, in-loop single
suppress, in-loop single nullcontext, self-alias after a loop target, and
tuple-unpack. **9/9 pass.**

## Status

This repairs the blocking finding on #379. The candidate is still **not
merged**: the independent-review gate remains closed, and this repair is a
changed head that needs its own fresh review — old-head approval does not
transfer. Release remains **PARTIAL**.
