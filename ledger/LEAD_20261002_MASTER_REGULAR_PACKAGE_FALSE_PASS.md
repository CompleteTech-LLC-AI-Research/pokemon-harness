# Second live false PASS on master: a regular package's foreign `__path__` portion

Distinct from the meta-path regression in
`LEAD_20261002_MASTER_META_PATH_REGRESSION.md`, and independent of it. Fixing
one does not fix the other.

## The defect

`master`'s `_foreign_namespace_locations` opens with:

```python
module = sys.modules.get(package)
if module is None or getattr(module, "__file__", None) is not None:
    return []
```

Any module that has a `__file__` is skipped entirely. A **regular** package has
`__init__.py` and therefore always has a `__file__`, so regular packages are
exempt from the `__path__` check. Only namespace packages (no `__file__`) are
examined.

That is the false PASS PR #551's `650a8bc` was written to fix. `650a8bc` is
**not an ancestor of master**:

```
git merge-base --is-ancestor 650a8bc origin/master   -> false
git merge-base --is-ancestor c1bef30 origin/master   -> false
```

## Reproduction on current master

`/workspace/poke-harness/.scratch/regular_pkg_probe.py`

```
regpkg.__file__ : .../local/regpkg/__init__.py
regpkg.__path__ : ['.../local/regpkg']
after extend    : ['.../local/regpkg', '.../foreign/regpkg']
guard status    : PASS
imported file   : .../foreign/regpkg/leaked.py
ORIGIN          : foreign
LEAKED_FOREIGN_AFTER_PASS: True
VERDICT: FALSE_PASS_LIVE
```

`pkgutil.extend_path`, an explicit `__path__` extension, or a `.pth`-installed
path entry adds a foreign portion. A submodule missing from the local portion
then imports from the foreign one while the top-level origin still looks
correct. Crediting only the top-level origin is exactly what the guard must not
do.

## PR #555 closes it

PR #555 carries `_foreign_path_locations` from `650a8bc`, which applies the
`__path__` check to regular packages and additionally declines to judge a
module whose own `__file__` does not match the origin being reported.

Same probe against PR #555's head `36d27c9`:

```
guard status : FAIL
detail       : package also resolves outside this checkout: .../foreign/regpkg
VERDICT      : no-false-pass
```

So #555 closes both master defects:

| defect | master | PR #555 |
| --- | --- | --- |
| meta-path finder | PASS + foreign submodule loads | FAIL |
| regular package foreign `__path__` portion | PASS + foreign submodule loads | FAIL |

## Consequence for the other open PRs

- **#551** (`c1bef30`) is not superseded after all. Its `650a8bc` scope is
  unlanded, and #555 carries it only because #555 was built on top of
  `lead/551-combine-553`, which already contained it. #555 is the merge vehicle;
  #551 must not be merged on its own (its head predates `f6a95f6` and would
  revert #553's work) but it also must not be closed as landed-once-#555-merges
  without #555 actually merging, since that is the only thing carrying
  `650a8bc` to trunk.
- **#548** (`1d4c9e1`) is a separate combine of #545 + #546, not covered by
  #555, and still needs its own review.
