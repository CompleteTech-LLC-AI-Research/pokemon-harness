# #565 @ `f35554b6` — independent verification of the #89 guard

## Scope note

Issue #89 is a large feature ("declare and enforce battle-mechanics coverage
separately from ROM pairings"). #565 is a narrow hardening slice of it: one
validator branch that bounds `deliberately_excluded` from above. It is judged
here as that slice, not as closing #89.

## The hole is real and the guard has teeth

The pre-existing pair of checks bound `deliberately_excluded` only from below —
an unused effect *must* be excluded — with nothing stopping a family that really
owns moves from being re-scoped that way. Measured on `f35554b6`, with the
declared counters moved alongside so the count cross-checks still agree:

```
downgrade an owned planned family to deliberately_excluded
  -> rejected: effect 2 owns moves in the pinned table,
     so it must not be deliberately_excluded
```

7 rows pass in `tests/test_battle_coverage_catalog.py`.

## Adjacent escapes tried, all rejected

The guard's precondition is that `move_ids` reflect the pinned table, so the
first question was whether that mapping can be edited to dodge it. It cannot:

| mutation | result |
|---|---|
| strip `move_ids` from an owned family, then exclude it | rejected — `move-to-effect mapping does not match the pinned table (missing=[40], ...)` |
| add `move_ids` to a pinned-unused family | rejected — `effect 1 has a non-integer move id` |
| delete an owned planned family outright | rejected — `effect families must cover every pinned effect id exactly once` |
| downgrade the single `tested` effect to `deliberately_excluded` | rejected — `effect 0 owns moves in the pinned table` |
| scope typo `deliberately-excluded` | rejected — `effect 5 has an unknown scope` |
| unknown scope `not_a_scope` | rejected — `effect 7 has an unknown scope` |
| bump `deliberately_excluded_count` without a family | rejected — `deliberately_excluded_count does not match the family list` |

The honest direction still works, which is what makes the guard a scope rule and
not a blanket freeze:

```
tested -> planned_unverified (owning moves)  -> ACCEPTED
```

## Structure of the shipped catalog

```
87 families = 67 (planned_unverified, owns moves)
             + 19 (deliberately_excluded, no moves)
             +  1 (tested, owns moves)
```

Every `planned_unverified` family owns moves and every `deliberately_excluded`
family owns none, so the new branch is currently the *only* thing preventing the
excluded set from being widened by a count-consistent edit. That makes it
load-bearing rather than redundant with the mapping check.

## What this does not establish

Author verification, not an independent review. `gh pr checks 565` and a
mutation pass by me are not the independent verdict the release bar requires, so
#565 stays draft and unmerged.
