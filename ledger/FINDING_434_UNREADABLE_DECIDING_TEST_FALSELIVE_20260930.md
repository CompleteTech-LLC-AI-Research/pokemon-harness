# Finding — an arm that always runs is read as per-call conditional (false-LIVE)

Date: 2026-09-30
Found by: lead integrator, adversarial enumeration with executed ground truth
Severity: **non-blocking for #433**. False-LIVE = the safe direction.
Status: filed as a new issue. Not fixed by this PR, and not a regression from it.

## The defect

When the test that *decides* an arm is one the analyzer cannot read, the arm
below it is treated as per-call conditional, and a store there settles the name
on "some calls only". In reality the deciding test always fails, so the arm
always runs and the store is unconditional.

```python
import contextlib

def o(x):
    cs = contextlib.nullcontext()
    if False: pass
    elif True: cs = list()     # arm always runs
    with cs:
        assert x != 1          # CPython: TypeError -> DEAD
```

CPython 3.12.14, executed, both `x=0` and `x=1`: `TypeError` on entry. The
assert never runs. Correct verdict is `False`. The tool answers `True`.

## The family

| shape | deciding test | verdict |
|---|---|---|
| E | `if False:` / `elif True: STORE` | false-LIVE |
| K | `if False: / elif False: / elif True: STORE` | false-LIVE |
| M | `if False: pass` / `else: STORE` | false-LIVE |
| C1 | `if not x:` / `else: STORE` | false-LIVE |
| C2 | `if not x:` / `elif True: STORE` | false-LIVE |

C1 has **no `elif` at all**, and it fails on the parent commit `63ab8dd` too.
So this is not an `elif` bug: the root cause is that the *deciding position*
is undecidable.

## Root cause

```
_condition_is_never_true('not x')      = False
_condition_is_always_true('not x')     = False
_condition_is_never_true('x is None')  = False
```

A `not`-negated per-call test is neither decidable-true nor decidable-false,
so `_elif_link_is_conditional` cannot use it and falls through to "reaching
this link depends on the call". But `if not x:` with `x` a parameter *does*
decide per call, and its `else` arm runs on exactly the calls where it is
false — which is a real dependence, so `True` is right for the `if x:` control
and wrong for `not x`. The tool cannot tell a decided-per-call test from an
undecided one, and both read as "conditional".

## Why it is safe, and why it still deserves an issue

A false-LIVE reports an unreachable assert as load-bearing. That never
certifies a defeated contract as pinned, so it cannot cause the #308
criterion-1 damage a false-DEAD causes. But it does inflate the enforced
count, and it is the same *class* of error #429 is about: a branch-reachability
question answered without reading the deciding expression.

## Net effect of `39f1b19` on this family

Enumerated over **532** generated chains (store kinds `list`/`null`/`int`/
`tuple` x 1-3 links x all combinations of 11 deciding expressions above the
store), with every ground truth value obtained by **executing** the fixture on
CPython 3.12.14:

| tree | false-DEAD (damaging) | false-LIVE (safe) |
|---|---|---|
| parent `63ab8dd` | **84** | 84 |
| head `39f1b19` | **0** | 90 |

The fix eliminates every one of the 84 false-deads and introduces 6 rows that
differ, all in the safe direction, all instances of this family:

```
live-bad int   link2 store after [not x]
live-bad int   link3 store after [not x,not x]
live-bad list  link2 store after [not x]
live-bad list  link3 store after [not x,not x]
live-bad tuple link2 store after [not x]
live-bad tuple link3 store after [not x,not x]
```

On the parent those 6 read `False`, which was *accidentally* correct for
C2-shaped rows: the parent read the `elif` arm as unconditional, settled the
name, and reported the header dead — and it is indeed dead. It was right for
the wrong reason. C1 shows the same false-LIVE on the parent, so the family
predates #433 outright.

## Method note — one probe row was a false finding and was dropped

`if x is None: / else: STORE` appeared to be a false-LIVE, but it is an
artifact: the probe only ever calls with `x in (0, 1)`, so `x is None` is
always false and the `else` arm always runs. The tool's answer is wrong for
`x=None` and the probe simply never executed that path. It is **excluded**
from the family above rather than reported, because a defect claim has to
survive an argument domain that can actually reach the branch.

## Evidence

- Enumeration probe: `/home/agent/poke-harness/.scratch/probe/p429_hunt.py`
- Family probe: `/tmp/p429_family.py`
- Differential: `/tmp/fl_parent.txt`, `/tmp/fl_head.txt`
- Both trees: `/tmp/wt429parent` (`63ab8dd`), `/home/agent/poke-harness/.scratch/fix429` (`39f1b19`)
