# Finding — merging PR #379 onto master introduces a FALSE-DEAD

Date: 2026-09-30
Severity: **BLOCKING for #379.** Damaging direction (#308 criterion 1: a contract CPython
evaluates is reported dead).
Found by: lead integrator, master-vs-merge-tree differential with executed ground truth.

## The defect

```python
import contextlib

def o(x):
    for cs in (1, contextlib.nullcontext()):   # after the loop cs is a real nullcontext
        pass
    with cs:
        assert x != 1                          # FIRES for x=1
```

Executed on CPython 3.12.14:

| tree | tool verdict | executed |
|---|---|---|
| `origin/master` `6b72bf62` | `True` (live) | `x=1` -> `AssertionError` FIRED — **correct** |
| #379 `0845ca56` merged onto master | **`False` (dead)** | `x=1` -> `AssertionError` FIRED — **wrong** |

The merge makes a live contract unreadable. This is a **regression against master**, not a
pre-existing defect: master answers this row correctly.

## Why CI does not catch it

The #379 merge tree is green:

```
pytest tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py
  -> 367 tests, 0 failures, 0 errors, 0 skipped   (/tmp/m379.xml)
```

367 passing rows while a real pinned contract is certified dead. No existing row covers a
`for`-target holding a real context manager read *after* the loop on this tree.

## Wider damage on the same merge tree

| shape | master | #379 merged | CPython |
|---|---|---|---|
| `for cs in (1, nullcontext())` then `with cs:` | `True` ✓ | **`False` ✗** | fires — **FALSE-DEAD** |
| `for cs in (1, suppress(AssertionError))` then `with cs:` | `True` ✗ | `False` ✓ | swallowed |
| #355 conditional `inner()` rebinding via `nonlocal` | `False` ✓ | `True` ✗ | raises — false-live (safe) |
| #355 control, unconditional `inner()` | `False` ✓ | `True` ✗ | raises — false-live (safe) |
| #356 `nonlocal` alias never assigned | `False` ✓ | `True` ✗ | raises — false-live (safe) |

So the merge trades its intended #355/#356 improvements for one damaging false-dead plus
three safe-direction false-lives. **The #355/#356 rows were already correct on master**, so
the net effect of merging is negative on this evidence.

## Correction to my own first measurement

The first version of this probe called `o(x, flag)` for every fixture, but several take only
`(x)`. The surplus argument raised `TypeError`, which the harness counted as "the assert did
not fire" — so **every path looked dead** and the #379 rows came out looking correct. The
probe was wrong, not the code. Corrected in
`/home/agent/poke-harness/.scratch/probe379/m379.py` with the call signature carried per case.
The false-dead above was found only after fixing that; it would have been missed entirely by
the broken version.

A second row was dropped for the same class of reason: an earlier "control" fixture was
vacuous, because a `nullcontext()` that merely swallows nothing leaves the assert to fire.
The real control uses `contextlib.suppress(AssertionError)`.

## Merge state

GitHub reports #379 `DIRTY`, but that is stale metadata — the three-way merge completes with
zero unmerged files (checked at merge-base `0529c8a` and onto current master). The blocker
here is behavioural, not textual.

## Not done

Nothing pushed, merged, or marked ready. This finding still needs an independent reviewer,
and #379 still has zero reviews.

Trees: `/home/agent/poke-harness/.scratch/mbase` (master), `/home/agent/poke-harness/.scratch/m379` (merge).
Probes: `/home/agent/poke-harness/.scratch/probe379/`.
