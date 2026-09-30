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
