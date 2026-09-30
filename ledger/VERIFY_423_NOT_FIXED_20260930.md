# Verification — issue #423 is a real, still-open false-live on the simulation tree

Date: 2026-09-30
By: lead integrator
Verdict: **#423 is NOT fixed by #412, #419, #421, or the #421 repair.**
Severity: false-live (analyzer certifies a swallowed assert as load-bearing).
This is the **damaging** direction.

## The shape

```python
import contextlib

def outer(x, _a, _b):
    assert x == 1
    for _ in (1,):
        *cs, = (contextlib.suppress(AssertionError),)
    with cs:
        assert x != 1
```

The starred store is **inside a loop body**; the `with cs:` header is a **later
sibling** in the same function, not nested in the loop.

## Ground truth, executed on CPython 3.12.14

```
TypeError before body -> DEAD: 'list' object does not support the context manager protocol
type of cs is list: list has __enter__: False
```

`cs` is a `list`. `with cs:` raises `TypeError` on entry, so the second
`assert x != 1` is **never evaluated**. The contract is **dead**. Expected
verdict `False`.

## Analyzer results, all three trees

| tree | second-assert verdict | correct? |
|---|---|---|
| simulation `#412+#424+#419+#421+fix` (`961e586`) | `True` | **NO — false-live** |
| #421 head `165b5b3d` | `True` | NO — false-live |
| #419 head `318d9a2c` | `True` | NO — false-live |

Consistent with issue #423's own report, which measured `origin/master`
`6b72bf6` and union `7726b87` at `True` as well. I did not re-derive its
numbers from its author; I re-measured all three trees myself.

## Controls, all correct and worth preserving

| control | verdict | correct? |
|---|---|---|
| plain element store in a loop body, `cs = contextlib.nullcontext()` | `True` | yes — genuinely live |
| plain loop target, `for cs in (contextlib.nullcontext(),):` | `True` | yes — genuinely live |

So the two controls that keep a *narrow* repair honest are green. Any fix must
keep them green; a fix that keyed on "any target inside a loop" would break
them.

## Why the #421 repair does not reach it

#421's own acceptance rows nest the `with` **inside** the loop body. The
starred rule is reached there because the header is read in the same scope as
the store. Here the `with` is a later sibling, so the store has already been
recorded with a value and read through a different path — one that asks
whether the store is *settled before* the header. A starred target is not an
element, so that path declines, and the assert stays live.

The distinction is narrow and real, which is exactly why the issue is filed
separately rather than folded into #421.

## Consequence

- #421 stays blocked on its own `TypeError` defect (see
  `FINDING_421_BOOL_MEMBERSHIP_20260930.md`).
- #423 is a **second, independent** blocker on merging #421 into the stack,
  because the family is not closed. The stack is four PRs deep and green on
  the sentinel suite, but this specific shape is a live defect **on that
  green tree**.
- Do not close #423 on the strength of #421's green CI.

## State

- Simulation worktree `/home/agent/poke-harness/.scratch/simint`, HEAD
  `00eca1d`.
- `origin/master` unmoved at `6b72bf62b5722e1df2c44c988bbdb14803024032`.
- Nothing pushed, merged, marked ready, or closed. Release remains `PARTIAL`.

---

# Addendum — #425 measured, and #405 found to introduce a false-DEAD

Same day, following the #423 verification.

## #425 confirmed open on the simulation tree

```python
def outer(x):
    for cs in (1, None):
        pass
    with cs:
        assert x != 1
```

CPython, executed: `TypeError -> DEAD: 'NoneType' object does not support the
context manager protocol`. Same for `for cs in (1, 2)` → `'int' object does not
support the context manager protocol`. Both correct answers are `False`.

| tree | `(1, None)` | `(1, 2)` | `(1, nullcontext())` control |
|---|---|---|---|
| `master` `6b72bf6` | `True` — false-live | `True` — false-live | `True` — correct |
| `#412` `6f388cf` | `True` — false-live | `True` — false-live | `True` — correct |
| simulation `4dd899d` | `True` — false-live | `True` — false-live | `True` — correct |

## #405 alone: correct on the two rows, and WRONG on the control

| tree | `(1, None)` | `(1, 2)` | `(1, nullcontext())` control |
|---|---|---|---|
| **#405 `d89616d9` alone** | `False` — correct | `False` — correct | **`False` — FALSE-DEAD** |

CPython ground truth for the control, executed:
`AssertionError propagated -> LIVE`. `cs` holds a real `nullcontext()` after
the loop, `with cs:` enters, and the assert is evaluated.

**#405's suite is green on this head: 362 tests, 0 failures, 0 errors, 0
skipped.** The three loop tests it does have are the wrong three; none is "a
loop whose last element *is* a context manager, read after the loop".

## Why this changes the merge-order recommendation

#425 says the merge loses #405's correct non-enterable answer. The stronger
and more useful statement, from my own measurements, is that **#405's answer is
not safe to carry in the first place** — the rule producing it also produces a
false-dead, which is the damaging direction #308 criterion 1 names.

So closing #425 by merging #405 would trade a weaker defect for a stronger
one. The fix needs to be narrower: after the loop, resolve the surviving
element's position *and* its enterability. `None`/`int` → dead;
`nullcontext()` → live. One rule, three rows, all currently enumerated.

Posted as a blocking comment on PR #405:
<https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/405#issuecomment-5904886121>

## Additional worktree created

`/home/agent/poke-harness/.scratch/iso405b`, detached at `d89616d9`, created
solely to measure #405's head. Read-only in effect; no branch, no commit.

## State unchanged

Nothing pushed, merged, marked ready, or closed. `origin/master` unmoved at
`6b72bf62b5722e1df2c44c988bbdb14803024032`. Release remains `PARTIAL`.
