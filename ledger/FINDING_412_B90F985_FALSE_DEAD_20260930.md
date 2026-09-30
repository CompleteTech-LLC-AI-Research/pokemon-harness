# Finding — a false-DEAD regression inside PR #412's stack, introduced at `b90f985`

Date: 2026-09-30
Found by: lead integrator, by bisection
Severity: **blocking** for #412 and #424. Damaging direction (#308 criterion 1:
a live contract reported dead).
Status: filed on both PRs. Not fixed upstream.

## The defect

```python
import contextlib

def outer(x):
    cs = contextlib.nullcontext()
    if x:
        pass
    elif True:
        cs = list()
    with cs:
        assert x != 1
```

CPython 3.12.14, executed:

```
AssertionError FIRED -> contract is LIVE
```

With `x = 1` the `if x:` arm runs, the `elif True:` body never executes, `cs`
remains the `nullcontext()`, and the assert fires. Correct verdict `True`.

The `elif` link is reached only when **every** test above it failed. `if x:`
can hold, so the `elif` body settles `cs` only on *some* calls, and the header
is undecidable. Treating the arm as unconditional drops a live contract.

## Bisection

| commit | verdict | |
|---|---|---|
| `origin/master` `6b72bf6` | `True` | correct |
| `5535135` — #388, 5th review round | `True` | correct |
| **`b90f985` — #388, 6th review round** | **`False`** | **false-dead** |
| `31e3b22`, `5a0e166`, `2031d7d`, `6134ddd`, `1168d5d`, `42c06d8`, `9b40218`, `d279594` | `False` | still wrong |
| `30d0479`, `53304ed`, `33ac083`, `0c2ba4d`, `5b46d22`, `6f388cf` (#412) | `False` | still wrong |
| `590ca17f` (#424) | `False` | still wrong |
| `318d9a2c` (#419) | `True` | correct |
| `165b5b3d` (#421) | `True` | correct |
| `eb7c15f7` (#406) | `True` | correct |

The boundary is exact: `5535135` correct, its immediate child `b90f985` wrong.

## Why this one is notable

`b90f985`'s own commit message says three of the four defects it fixed were
**false-deads**. A false-dead was introduced *inside the commit that fixes
false-deads*, and then survived six more review rounds, the #392 merge, and
the entire remaining stack.

The offending diff adds `statement.orelse` to a body walk and adds
`isinstance(enclosing, ast.If)` / `isinstance(node, ast.If)` guards — i.e. it
teaches the walk to descend into `orelse`, which is where `elif` bodies live,
without teaching it that an `elif` arm is conditionally reached.

## Why CI never caught it

`pytest -k "elif or unconditional"` on `b90f985` collects 2 tests, both about
*conditional* stores superseding a carrier. There is no row for a store in an
`elif` arm that is only sometimes reached. #412's head passes **669 tests**
with this defect present; the simulation tree passes 710. Green throughout.

## Relation to #416, and a caution

#416 was filed against #406 for this same shape. #406's current head
`eb7c15f7` answers it correctly, so **#416 as filed is stale with respect to
#406's head** and should stay open only until that is confirmed against the
merge tree.

The two are opposite fixes and that matters for whoever repairs this: #406
correctly makes an `elif` arm conditional, while this stack makes it
unconditional. If #406's approach is adopted, the same row should be used as
the control so the two cannot drift apart again.

## Filed

- <https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/412#issuecomment-5905026690>
- <https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/424#issuecomment-5905027249>

## Worktrees created for the bisection

All detached, all read-only in effect, none branched or committed to:
`bs_5453c51`, `bs_5535135`, `bs_b90f985`, `bs_31e3b22`, `bs_5a0e166`,
`bs_2031d7d`, `bs_6134ddd`, `bs_1168d5d`, `bs_42c06d8`, `bs_9b40218`,
`bs_d279594`, `bis_fa65969`, `bis_b7b28ec`, `bis_6c22c51`, `bis_bee3e78`,
`bis_d482fad`, `bis_de23eea`, plus `iso405b` and `iso406b`.

## State

- `origin/master` unmoved at `6b72bf62b5722e1df2c44c988bbdb14803024032`.
- Nothing pushed, merged, marked ready, or closed. Release remains `PARTIAL`.
