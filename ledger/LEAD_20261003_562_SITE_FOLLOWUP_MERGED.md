# LEAD — the #534 site follow-up merged at c1f900f4

Date: 2026-10-03

```
parent master      : 259b00b27135ee082ef8c7775bf04dd719c7940e
approved head      : 3d0d23b40ac505b1de9b7ef3d29855a97e0369c1  (lead/534-site-followup)
merge commit       : c1f900f43b358707b8de0a0f753affe163d7c926
```

## Why this landed separately

`bf92007f` merged the #534 guard, but #559 kept moving after that head. Two
commits landed on #559 with new patch-ids, and the second repaired three escapes
in `_site_packages_roots` that were reachable on merged trunk. Ancestry is not
the only question -- patch-id is, because a cherry-pick changes the SHA.

The first (`b7d7a721`, comment only) was taken as `b1c71a43`. The second
(`254c5339`) was taken as `3d0d23b4`.

## The defect

Round 4 guarded the two site *getters* and nothing around them:

```
_extend(getattr(site, "getsitepackages", None), candidates)
```

The attribute read sits outside any boundary, and the candidate loop converts
under `(OSError, ValueError, RuntimeError)` while its truth test sits outside
the `try` entirely. So three things escaped `_check_origins()`:

1. a hostile `ModuleType` subclass raising from the attribute read
2. a site entry raising a direct `BaseException` subclass from `__fspath__`
3. a site entry raising one from `__bool__`

## Measured, not asserted

The commit message claims both rows "fail before this commit". That is true
only of `_check_origins()`. End to end they pass on the parent too, because
`check_origins()` is #558's boundary and converts any escaping
`BaseException` into the same FAIL document. Measuring the shipped row through
`check_origins()` therefore cannot tell the two trees apart -- a fact worth
recording, because it is the third time a boundary has masked the shape of the
hole underneath it.

The difference is only visible one level down:

```
PARENT bf92007f  hostile_site_module_attribute: FAILED -- ExplodingAttribute: site attribute escaped
PARENT bf92007f  hostile_site_path:            FAILED -- ExplodingSiteEntry: site path escaped
MERGE  c1f900f4  both passed
```

`/workspace/poke-harness/.scratch/r558s/probe_sites3.py` runs the two shipped
rows verbatim and then calls `analyse = _check_origins` directly.

## Superseded lead probe

`probe_sites.py` is not a valid oracle. It sets `site.__class__` to a bare
metaclass, which CPython rejects:

```
TypeError: __class__ assignment only supported for mutable types or ModuleType subclasses
```

so its "escape1" line raised inside the probe before the guard was reached and
reported the same result on the repaired and unrepaired trees. It has been left
in place but should not be cited. The shipped tests use the mechanism that
works: `monkeypatch.setitem(sys.modules, "site", ...)` with a `ModuleType`
subclass overriding `__getattribute__`.

## Verification on this exact merge tree

```
guard suite   : 134 passed, 0 failed   (parent bf92007f: 132)
test names    : 103 -> 105, none dropped
ruff          : All checks passed
AC4 repaired  : rc 0, PASS both packages
AC4 mis-point : rc 1, FAIL both packages
boundary      : Boom at _allowed_roots -> FAIL report naming Boom, main() rc 1
```

Re-run after committing: suite rc 0, ruff clean, AC4 rc 0.

Real trunk after the merge:

```
.venv/bin/python scripts/check_import_origins.py --project-root /workspace/poke-harness/pokemon
status PASS, rc 0
pokered_harness -> /workspace/poke-harness/pokemon/src/pokered_harness/__init__.py
pyboy           -> /workspace/poke-harness/pokemon/vendor/pyboy-src/pyboy/__init__.py
0 tracked modifications, 137 untracked entries, 665 worktrees
```

## Environment note

The review venv `/workspace/poke-harness/.scratch/merge562venv` is a private
copy whose `.pth` points at the merge tree, never at the main repository, so
`pip install -e` against the main `.venv` was never involved.

## Still open

The broader objective is not met. Release remains PARTIAL. #558 and #559 may now
be closed: every patch-id from both is on master. #555/#556/#557 still need
their own patch-id verification before closure, and #547's acceptance rows
need review against the guard as merged. This ledger records one merge, not
completion.
