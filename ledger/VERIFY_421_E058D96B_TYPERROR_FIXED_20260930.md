# Verify — PR #421 head `e058d96b` repairs the filed TypeError

Date: 2026-09-30
PR: https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/421
Filed finding: `ledger/FINDING_421_BOOL_MEMBERSHIP_20260930.md`
Filed comment: https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/421#issuecomment-5904792850
Withdrawal comment: https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/421#issuecomment-5905932841

## The filed defect, on head `165b5b3d`

`_binds_starred_target` ended its non-`ast.Assign` branch with

    return name in _starred_names_in_loop_target(statement, name)

but `_starred_names_in_loop_target` **already returns a bool**, so the `in`
operator raised

    TypeError: argument of type 'bool' is not iterable

130 failures on the combined stack. The standalone suite stayed green because
the `ast.For` branch had no coverage of its own.

## The repair, on head `e058d96b`

Commit `e058d96b` — "Fix wrong-container bug in the #420 starred loop-target
repair" — returns the helper's bool directly instead of applying `in` to it.

## Independent re-verification on the exact head

Executed on CPython 3.11.2, worktree `/tmp/v421` at `e058d96b`, read-only.

| case | executed ground truth | tool verdict |
|---|---|---|
| `for *cs, in (1,): pass` then `with cs: assert x != 1` | `TypeError` on entry, assert never runs -> genuinely **DEAD** | `False`, **no TypeError raised** |

The verdict is both non-raising and **correct**. The blocking finding is
**withdrawn** for this head.

## What this does and does not change

- Resolved: the TypeError blocker on #421.
- Unchanged: #421 has **zero independent reviews**, so it is not mergeable on
  the author's or the lead's say-so alone.
- Unchanged: #420, #422, #425, #426 remain open, and #425 is explicitly *not*
  to be closed by merging #405.
- Release remains **PARTIAL**.

## Method note

Ground truth was obtained by executing the fixture, not by reading the
analyzer. A reader-only check would have confirmed the `TypeError` disappears
but not that `False` is the right answer; here it is, and that is the part
that matters.
