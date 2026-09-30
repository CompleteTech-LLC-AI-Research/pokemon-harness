# Repair: #378 a zero-iteration loop was counted as a store that ran (false-DEAD)

Candidate repaired: `lead/integ-379-392-on-master` (`767f316`)
Base: `origin/master` = `6b72bf62b5722e1df2c44c988bbdb14803024032`

## The finding, and where it came from

An open comment on PR #371 names a sibling defect to the one it filed, **#378**.
It is the same *kind* of failure as the #377 repair already in this candidate --
a header CPython really enters, certified as defeated -- but a different
consumer of the binding table. The fixture:

```python
cs = contextlib.nullcontext()
if True:
    for x in ():
        cs = contextlib.suppress(AssertionError)   # zero iterations
    with cs:
        assert False                                # FIRES
```

I reproduced it on `e0b95da` by execution before changing anything. It is real.

## Root cause

A `for` was admitted as a store statement on the question *"have this block's
stores run by the time this header is read"*, and `for x in ():` answers yes --
the `for` statement itself was reached. But the guarantee a loop offers is per
**iteration**, and a loop over a literal empty iterable has none. Counting the
loop retired the carried `nullcontext`, answered "the suppressor is in force",
and reported a live contract as swallowed.

Three consumers read the binding table a different way, so all three had to
agree before the fixture was judged correctly:

* `_is_store_statement` now declines a provably empty literal loop, so
  `_bindings_before` keeps the header reading the earlier binding;
* `_is_provably_unreached_store` (new) drops such stores from
  `_assigned_suppressors`, which is also what backs
  `_aliased_suppressions`' bare-name branch;
* `_resolve_bindings` stops them competing, and with nothing able to compete
  falls through to the same tail-store path the ordinary single-store case
  uses.

## Scope, deliberately

Only a **provably empty literal** answers yes. `helper.items()` may yield
nothing, but it may not, so a store in that body keeps competing under the
ordinary ambiguity handling. `range(0)` and `set()` stay excluded for the same
reason `_is_empty_literal_iterable` excludes them: deciding them means
reasoning about builtins rather than reading a literal. `while True:` and any
merely-truthy `while` test are likewise not provably empty.

The pre-existing `_is_empty_literal_iterable` gap (`for _ in ''`,
`for _ in set()`, `for _ in range(0)`) is **out of scope here** and unchanged
by this repair; it reproduces identically on master and is already recorded as
its own finding in `REVIEW_20260930_379_PLUS_392_B087B33.md`. Folding it in
would widen the repair past the fixture it was filed for.

## New coverage

`UNREACHED_LOOP_BODY_ROWS` (7 rows) plus
`test_an_unreachable_loop_body_does_not_rebind_the_name`. Every row is
**executed** on CPython before it is judged, and a row whose expectation
disagrees with the runtime fails as stale rather than passing silently.

The controls are the point of the table. Four unreachable shapes (bare `()`,
bare `[]`, `if True`-nested, nested inside a running loop) must stay LIVE, and
three that do iterate must stay defeated -- `[1]`, the falsy member `(0,)`, and
`(1, 2)`. Without the controls, a fix that simply declined *every* loop-body
store would pass all four unreachable rows.

An execution-grounded matrix of **17 shapes** -- the 7 rows above plus the
#377 after-loop controls, in-loop suppress/nullcontext, `try/except`,
`if False`, and a plain live assert -- is **17/17 correct**.

## Validation at `767f316`

| check | result |
|---|---|
| sentinel + milestone lane | **477 passed, 0 failed, 0 errors, 0 skipped** (was 470; +7 new rows) |
| `ruff check` both candidate files | clean |
| `ruff check` whole `tests/` | 42 findings, all pre-existing and in unrelated files; **0 in the two candidate files** |

### Mutation matrix, all four repairs pinned at the new head

| mutation | result |
|---|---|
| #377 after-loop `elts[-1]` -> `elts[0]` | **KILLED**, 1 failure: `a three-element loop leaves the last element bound` |
| #378 unreachable-loop guards removed (all 4 sites) | **KILLED**, 4 failures: exactly the four unreachable rows |
| #379 first-pass self-alias exclusion removed | **KILLED**, 1 failure: `a self-alias after a loop target keeps the element` |
| #392 settled-store tie-break -> `tied` | **KILLED**, 5 failures: the four `a carrier inside the reading header leaves the name unenterable` rows + `an unconditional walrus beside a later carrier in one block` |

### Two of those four recipes were wrong on the first attempt

Recorded because the failure mode is silent. A first pass guessed both recipes
and **both survived** -- which reads as "untested fix" but was actually "wrong
mutation". The causes:

* `-q` prints only the *first* failing test, so a surviving mutation and a
  killed one can look identical in the exit code's error class. The verdict has
  to be the **set difference** of failing test ids against the clean head, not
  the error class.
* The #379 recipe re-keyed the `seen` bookkeeping, which is an *equivalent
  mutant*. The real fix is the separate `if exclude is None and
  current.id == target:` block that locates and excludes the self-alias store
  on the first pass. #392's own mutation matrix already warned about this one.

Both recipes now revert the **actual pre-fix source**, read out of `8847177`
rather than reconstructed from memory, and both kill.

## Status

#378 and #377 are both repaired **in the candidate**, and neither is merged, so
neither issue is closed. #378 has no PR of its own yet. The candidate still
needs a fresh independent review of this head -- the only review on record
(`b087b33`) was **withdrawn as wrong** in
`REVIEW_20260930_379_PLUS_392_B087B33.md`, and changed-head approval does not
transfer. Release remains **PARTIAL**.
