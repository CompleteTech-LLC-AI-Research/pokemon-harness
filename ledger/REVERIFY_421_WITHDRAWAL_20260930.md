# Re-verification — the #421 withdrawal, checked independently

Date: 2026-09-30
Reviews `ledger/VERIFY_421_E058D96B_TYPERROR_FIXED_20260930.md`, which **withdraws**
the lead's own blocking finding on PR #421.

A withdrawal of a blocking finding is exactly the kind of claim that must not be
accepted on the withdrawer's word, so it was re-measured from scratch.

## The code change is real and is what the commit says

```
-        return name in _starred_names_in_loop_target(statement, name)
+        return _starred_names_in_loop_target(statement, name)
```

`_starred_names_in_loop_target` is a predicate returning `bool`, so the old
`name in <bool>` raised. Confirmed present at `165b5b3d:3135` and repaired at
`e058d96b:3135`.

## The TypeError reproduces, and only where the branch is reachable

Called the helper directly on the `for *cs, in (1,):` loop target:

| tree | `_starred_names_in_loop_target` | `_binds_starred_target` |
|---|---|---|
| old `165b5b3d` | `True` | **`TypeError: argument of type 'bool' is not iterable`** |
| new `e058d96b` | `True` | `True` |

Note why a naive end-to-end fixture shows nothing on the old head: `_binds_starred_target`
is reached only on a lineage where the `ast.For` branch is live — the
#405/#408/#409/#412 stack merged with #419. On #421's standalone head the branch
is short-circuited before it, which is precisely why that suite stayed green
with the defect present. The commit message says this, and it is correct.

## The new head is not merely non-raising, it is right

| shape | tool | executed CPython | |
|---|---|---|---|
| `for *cs, in (1,):` then `with cs:` | `False` | `TypeError` on entry, assert never runs | correct |
| CONTROL `for cs in (1, nullcontext()):` | `True` | assert fires | correct |

The control still answers `True`, so the fix did not over-correct into a
false-DEAD. That is the check that matters: a reader-only test would have shown
the `TypeError` disappearing without establishing that `False` is the right
answer.

## #423 still open, correctly

| shape | tool | CPython | |
|---|---|---|---|
| starred store in a loop body, `with` a later sibling | `True` | `TypeError` on entry | false-live, still filed |

Unchanged on both heads, so #423 is **not** fixed by this repair and stays open.

## Verdict

The withdrawal is **sound**. PR #421's blocking TypeError is genuinely repaired at
`e058d96b`, and the repair is verified against executed CPython rather than by
absence of an exception.

#421 still has **zero independent reviews** and is therefore not mergeable on the
lead's say-so. #420, #422, #423, #425, #426 remain open. Release `PARTIAL`.

Probes: `/home/agent/poke-harness/.scratch/probe421/`.
Trees: `/home/agent/poke-harness/.scratch/v421old` (`165b5b3d`),
`/home/agent/poke-harness/.scratch/v421new` (`e058d96b`).
