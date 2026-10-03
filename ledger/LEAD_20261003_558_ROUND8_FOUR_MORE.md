# LEAD — #558 round 8: four more escapes, and a pattern worth naming

## Trunk

```
master        = 5d1eae67... (see git log)
origin/master = identical, 0 0
```

## Candidate

```
branch lead/556-const-collision
head   7075660f1056d04f97dddec7691fe456459430e5
```

## Round 8 -> 7075660f

Round 8 returned INCOMPLETE — the filesystem went read-only again, mid-probe —
but it landed four findings first. **All four were reproduced independently by
the lead before any was repaired.** Two of the four were in places no earlier
round had looked.

1. **`_is_trusted_stdlib_finder` hashed the attacker.** The decision was
   `finder in <set>`, which calls `__hash__` and `__eq__` on the candidate. A
   finder whose *metaclass* raises a direct `BaseException` subclass escaped
   `check_origins`. Now identity against the three exact interpreter objects,
   which runs no attacker code at all.

   Subtlety that cost time: this only fires when the finder is a **class**. An
   instance is hashed by its own type and never reaches the hostile
   `__hash__`, so the first version of the row passed on unfixed source. The
   row now puts the class on `sys.meta_path`.

2. **`list(sys.meta_path)` ran unguarded.** An interpreter whose meta-path
   cannot be enumerated cannot be shown free of an injected finder, so the
   repair returns a sentinel offender and the caller's refusal fires.

3. **`getattr(site, "getsitepackages", None)` ran unguarded.** Round 5 guarded
   the getter *call* but not the attribute *lookup*. A replaced `site` module
   can raise from `__getattr__`.

4. **`read_text` on a distribution, a `RECORD` and a `.pth`** was guarded with
   `(OSError, ValueError)`. These are objects an install layout supplies.

Rows, all four proven to FAIL on `677bca44`:

```
test_a_hostile_finder_hash_cannot_abort_the_guard
test_an_unreadable_meta_path_cannot_abort_the_guard
test_a_hostile_site_attribute_lookup_cannot_abort_the_guard
test_an_unreadable_distribution_record_cannot_abort_the_guard
```

## Evidence on the current head

```
guard suite   : 123 passed, rc=0   (115 -> 116 -> 118 -> 119 -> 123)
AC4 repaired  : rc=0 PASS both
AC4 mis-pointed: rc=1 FAIL both
ruff          : 1 finding, the pre-existing import-sort one
repo state    : 0 tracked mods, 137 untracked, 659 worktrees, editable install
                still resolving to /workspace/poke-harness/pokemon
```

## The pattern, stated honestly

Eight rounds. Each round found exactly one more reachable instance of the same
class, and every one was reachable from the public entry point. Rounds 5, 7 and
8 all found that a *previous* repair had been incomplete rather than wrong: the
right call was guarded, the value it returned was not.

Two things follow, and both argue against merging now.

- **The class is not yet closed.** Nothing in the evidence says round 9 will
  find nothing. A repair that fixes a site getter but not the site attribute
  lookup is not a pattern that terminates on its own.
- **A structural fix is indicated, not another site.** Nine individual
  widenings have each left an adjacent read behind. What is wanted is one
  boundary — every value that reaches `check_origins` from outside the process's
  own control passes through a single conversion that either yields a usable
  value or a finding — rather than a ninth `except BaseException`.

This is a judgement worth making explicitly rather than continuing to discover
one gap per review round.

## Environment, recorded because it is now the dominant cost

`/tmp` is a 512 MB tmpfs at 100%, full of this project's own prior review
scratch. `/workspace` has ~50 GB free but **remounts read-only intermittently**
(`errors=remount-ro` on the ext4 mount); it was writable again on every check
after each failure. Six of eight reviews returned INCOMPLETE for this reason
alone. Nothing has been deleted to reclaim space, since none of it was created
by this pass.

## Not merged

Eight reviews: five INCOMPLETE for environmental reasons, three REQUEST CHANGES
with real findings, zero APPROVE. #558 is not merged and #547 is not closed.
Release remains **PARTIAL**.
