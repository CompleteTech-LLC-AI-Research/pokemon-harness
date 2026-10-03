# LEAD — #558 rounds 5 and 6: two more BaseException escapes found and repaired

## Trunk

```
master        = 608b223e1bd94aa628929a08330f562d060caf4e
origin/master = 608b223e1bd94aa628929a08330f562d060caf4e
origin/master...master = 0 0
```

There is no `main` branch; trunk is `master`. Trunk unchanged by this pass.

## Candidate

```
branch lead/556-const-collision
head   d39dda1652d72053f600eeef67f32ab3dc207f78
```

Two repairs landed this pass, both on the same defect class, both proven by a
regression row that FAILS on the prior head.

## Round 5 -> b8cd7c57 — `_site_packages_roots`

Round 4's output was contaminated (it replayed round 3's verdict) and aborted
mid-run with ENOSPC. Round 5 was re-dispatched against `d58321fc` and returned
INCOMPLETE — the host filesystem went read-only twice during the run — but it
named a specific site worth checking rather than waving at it.

`_site_packages_roots()` read `site.getsitepackages()` and
`site.getusersitepackages()` under `except Exception`. `site` is an ordinary
module attribute, so a replaced `sys.modules["site"]` can raise a direct
`BaseException` subclass.

Reproduced before the fix, on the entry point itself:

```
RESULT: ESCAPED out of check_origins() Boom: site list boom
```

Repaired to catch `BaseException`, failing closed: no roots means
`_finder_is_installed()` returns False and `_is_trusted_finder()` returns
False, so the guard refuses.

```
RESULT: RETURNED status= FAIL
```

Row: `test_a_hostile_site_module_cannot_abort_the_guard`, proven to FAIL on
`d58321fc`.

## Round 6 -> d39dda16 — descriptors and code objects

Round 6 reviewed `b8cd7c57` and returned INCOMPLETE (filesystem read-only again),
but flagged `except Exception` still standing at `_is_installation_finder`'s
descriptor reads. That claim was **correct and understated**.

Reproduced:

```
RESULT: ESCAPED from _is_installation_finder: Boom: descriptor boom
RESULT: ESCAPED out of check_origins(): Boom: descriptor boom
```

The cluster extended past what was reported:

```
RESULT: ESCAPED from _code_matches_source: Boom: __code__ boom
RESULT: ESCAPED from _code_signature: Boom: co_consts boom
```

`_code_matches_source` read `function.__code__` unguarded, and both walks it
drives — `_code_objects` and `_code_signature` — read `co_consts` and the
per-field attributes unguarded, on a code object a hostile descriptor had just
supplied.

All four sites now fail closed. One design note worth keeping: `_code_signature`
returns a **fresh `object()` per call** when a code object is unreadable, not a
shared sentinel. A shared sentinel would compare equal to itself, so two hostile
objects would corroborate each other into a false match. Verified:

```
two unreadable signatures compare equal? False (must be False)
real signature is a tuple: True
```

Rows: `test_an_unreadable_finder_descriptor_refuses_installation_trust` and
`test_an_unreadable_code_object_cannot_abort_the_guard`, both proven to FAIL on
`b8cd7c57`.

A naming trap cost real time and is recorded so it is not repeated: round 3
already defines `test_a_hostile_finder_descriptor_cannot_abort_the_guard`. The
new row was first written under that same name, and because `_is_installation_finder`
returns False *before* the descriptor read unless the finder already looks
installed, the pre-existing meta-path row passes on unfixed source and cannot
catch this defect. `-k` selection silently ran the wrong test. The new rows use
distinct names.

## Evidence on the current head

```
guard suite        : 118 passed, rc=0   (115 -> 116 -> 118 across the three repairs)
AC4 correct root    : rc=0, status PASS, pokered_harness PASS, pyboy PASS
AC4 wrong root      : rc=1, status FAIL, pokered_harness FAIL, pyboy FAIL
ruff                : 1 finding, the pre-existing import-sort one, also present
                      on 9084db8; not introduced by either repair
```

## Not merged

No independent APPROVE yet. Rounds 5 and 6 were both INCOMPLETE for an
environmental reason — the host filesystem remounted read-only mid-run — not
because of a code objection, but an incomplete review is not an approval.

The `codex2` reviewer path is unreliable for a different reason: it has now been
killed by ENOSPC, by a read-only `/workspace`, and once by a silently dying
background process, in four separate dispatches. Two of those failures happened
while the reviewer was mid-probe, which is exactly where a review earns its
value.

Release remains **PARTIAL**. #558 not merged, #547 not closed.
