# Lead record — #400, intra-block reachability after a control transfer

- Issue: #400
- Base: `4796c641119a4db2a73deef494eb6902beca70a7` (`origin/master` at start)
- Branch: `fix/400-intra-block-reachability`
- Worktree: `/workspace/poke-harness/.scratch/fix400`
- Author: lead integrator. **Not self-approved** — independent review required before merge.

## Defect

`_is_enforced` reported an `assert` as enforced when it sat after an
unconditional `return` / `raise` / `break` / `continue` in the same block. The
assert stays in the AST, so the presence-based sentinel certified a contract
the interpreter can never evaluate. Existing rules covered a *container* that
may not be entered (`if False:`, `for _ in []:`); this is a statement
positioned after one that cannot fall through.

## Implementation

`_is_after_control_transfer` walks the chain of blocks that hold the target.
At each level the entry that is (or contains) the target is the holder, and only
the siblings strictly before it are candidates. `_is_bare_transfer` restricts
the rule to a transfer written *directly* in the block, so a guarded transfer
(`if flag: return`) or one nested in an earlier statement's body does not fire.
`_BLOCK_FIELDS` (`body`, `orelse`, `finalbody`) confines the walk to lists that
execute as sequential blocks — `with.items` and `for.orelse` are not
interleaved with statements.

### Two defects found and repaired during the repair itself

1. **Shallow sibling walk missed dead containers.** The first implementation
   compared only the block that directly held the assert, so `return` followed
   by a `with` / `try` / `if` / `match` / `for` reported the assert *inside* the
   container as enforced. Fixed by the chain walk.
2. **Unrestricted field match inverted a live verdict.** Scanning every list
   field paired the target against a list it was not in, so a live assert inside
   a `with` body was reported dead (`False`). Fixed by `_BLOCK_FIELDS`.

Both were caught by the probe before pinning, not by review.

## Evidence

Commands (worktree above, venv `/workspace/poke-harness/pokemon/.venv`):

```
python -m pytest tests/test_timed_menu_milestone_sentinels.py -q -p no:randomly
  -> 349 tests, 0 errors, 0 failures, 0 skips
python -m pytest tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py -q -p no:randomly
  -> 421 tests, 0 errors, 0 failures, 0 skipped (junit /tmp/fix400-final.xml)
python -m ruff check tests/           -> All checks passed!
python -m ruff format --check tests/ -> 269 files already formatted
git diff --check                     -> clean
```

Probe `/tmp/t400c.py`: 30 shapes (16 dead, 14 live controls), 0 failures.
Shapes covered: bare/valued return, raise, break, continue, dead `with` /
`try`-`finally` / `if`-`else` (both branches) / `match` / `for` / `async for` /
`async with` / class bodies, five-deep nesting, plus live controls for guarded
transfers, `break` bound to the nearest loop, `return` inside `try`, and
sequential live asserts.

Two probe expectations were initially wrong and were corrected against the
interpreter rather than against the analyzer: a class body after `return` is
genuinely dead, and `ast.walk` does not yield asserts in source order (the
mixed-shape test sorts by `lineno` so the expectation is unambiguous).

## Pinned rows

- `UNREACHABLE_SHAPES`: 20 new rows — the four transfer kinds, dead containers
  of every compound form, deep nesting, class body, handler-block variants, and
  8 live controls.
- New `MIXED_REACHABILITY_SHAPES` +
  `test_reachability_decides_each_assert_by_its_own_position`: 4 rows. The
  single-verdict rows cannot express a function holding one live and one dead
  assert; a rule answering uniformly per function would satisfy either an
  all-True or an all-False fixture while being wrong about half the asserts.

## Mutation matrix — 6/6 killed

All mutants applied to the support module, then the focused suite re-run:

| Mutant | Change | Killed by |
|---|---|---|
| M1 | `if _is_after_control_transfer(...)` -> `if False` | 3 mixed-position rows |
| M2 | helper returns `False` always | 3 mixed-position rows |
| M3 | helper returns `True` always | 3 existing suppression/scope rows |
| M4 | drop the bare-transfer restriction (any node is a transfer) | 3 user-exit rows |
| M5 | `_BLOCK_FIELDS` narrowed to `{"body"}` | `return in finally then assert`, `return in else then assert` |
| M6 | descend into each sibling's descendants | 4 existing carrier / `else`-break rows |

M5 initially **survived** the first pass: the `orelse` / `finalbody` cases were
not pinned. Two rows were added and the mutant re-run to confirm the kill. The
survivor was closed by a test, not by waiving it.

## Status

Not merged. Requires a fresh independent review of the exact head, then a
rebase onto a refreshed `origin/master` and a re-run before merge.
