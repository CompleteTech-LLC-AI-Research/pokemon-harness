# Blocking finding on #558: a drifting `__file__` skips the foreign-portion check

Recorded 2026-10-02 by the lead integrator. Ledger-only.

## How it was found

Not by attacking #558 directly. It came from porting #551's own test row
`test_a_foreign_path_portion_is_reported_even_when_file_changes_between_reads`
and running it against both trees.

The earlier comparison in `LEAD_20261002_551_VS_558_COMPARISON.md` concluded
that #551's `__path__` containment scope was missing from #558 and would need a
~1500-line consolidation. That conclusion was wrong in both directions:

- The regular-package `__path__` scope is already present in #558. A regular
  package whose `__path__` is extended to a foreign directory by
  `pkgutil.extend_path` is reported as `FAIL` with the foreign portion named,
  on both heads.
- What #558 does *not* have is #551's hardening of that check against a
  mutable `__file__`, and that is a live false PASS rather than a missing
  feature.

## The hole

`check_origins` resolved a package's origin from `module.__file__`, then called
`_foreign_path_locations`, which read `module.__file__` **again** and compared
it to that origin. If the two reads disagreed, the function returned no
findings at all.

`__file__` is mutable interpreter state. A path-like that answers locally on
the first read and foreignly on every later read therefore makes the guard skip
the portion check entirely, so a package with a genuinely foreign `__path__`
reports PASS.

Measured on this branch before the fix, with the suite green at 121 passed:

```
status        : PASS
detail        :
__file__ reads : 2   (the row demands <= 1)
VERDICT: FAIL -- SECOND READ, the hole
```

`#551` at `0baf52e`, same probe:

```
status        : FAIL
detail        : package also resolves outside this checkout: .../outside/drifting_pkg
__file__ reads : 1
VERDICT: PASS
```

## The repair

`_resolve_origin` now publishes the module it imported, and the portion check
receives that module instead of looking one up and re-reading `__file__`. The
value is read exactly once, so a drifting answer cannot be used to skip the
check.

Two details that a naive version of this got wrong, both caught during the work:

- Passing `sys.modules.get(package)` unconditionally broke two honest-install
  rows. When the origin came from install metadata rather than a live import,
  the ambient module is a *different* tree's package and its portions must not
  be judged against these roots. The published-module channel distinguishes
  the two cases without an extra `__file__` read.
- The channel had to be cleared at the start of `check_origins`. An entry left
  from an earlier call paired a metadata-resolved origin with that earlier
  tree's module and reported a foreign portion for an honest install.

`test_a_foreign_path_portion_is_reported_even_when_file_changes_between_reads`
is the mutation guard: dropping the `module` argument restores the second read
and that row alone fails (`1 failed, 104 passed`).

## Disposition

Recorded against #558 so it is not merged with the hole open. Discharged by the
commit that ports the fix, subject to the independent review #558 still lacks.

