# #567 stack (#568 `1cc86f8`, #569 `11eb719`) — independent verification

## #568: claims confirmed

AST-identity measured directly, comparing `ast.dump(ast.parse(...))` between
`origin/master` and the head for all 40 changed files:

```
files: 40   AST-identical or unchanged: 39   differing: 1
  scripts/check_pyboy_components.py -> AST DIFFERS
```

The four edits there are lint-driven rather than correctness fixes:
`symlink(path, stem=stem)` (B023 late-binding capture), two
`[...][0]` -> `next(...)` rewrites (next-iterator preference), two nested
`with` blocks merged into one (SIM117), and mode `100644` -> `100755`. None
change behavior, which was checked rather than assumed:
`scripts/check_pyboy_components.py` passes 29/29 on master and 29/29 on the
head.

Reproducing that needs a per-checkout venv. A symlinked `.venv` yields 5 bogus
failures in `test_imported_split_modules_match_the_pre_split_public_surface`
because the import resolves into the other tree.

The producer carve-out is exactly as claimed:

```
git diff --stat origin/master HEAD -- scripts/produce_battle_state_fixtures.py -> empty
sha1sum -> e2e9676594415148293909087bbb02863ed0db49
recorded in release-evidence/battle-scenarios.json:387 (+4 more rows)
```

## #568 alone does not make `scripts/` clean — by design, and it matters

```
ruff format --check scripts   (on 1cc86f8 alone)
  -> Would reformat: scripts/full_to_brock.py
  -> Would reformat: scripts/produce_battle_state_fixtures.py
```

The exclusion #568 depends on lands in #569, so #568 is not independently
mergeable. Merging it alone leaves `scripts/` red and takes CI with it. This
matches its draft state and is expected for a stacked pair, but it should not be
read as "step 1 delivers a clean `scripts/`".

## The gap neither step covers: `scripts/full_to_brock.py`

Not excluded, clean under `ruff check`, and still needing reformat. Master does
not list it among its 41 unformatted `scripts`+`tests` files, and widening the
glob to `scripts` is precisely what pulls it into the format lane. With the
producer carve-out in place the lane is down to this one new offender, so the
fix is one more file in step 1 rather than a second exclusion.

## The red `ruff check` on #569 is pre-existing, not a regression

```
ruff check scripts tests -> 163 findings on the head, 285 on master
comm -13 master head    -> scripts/_grind_support.py:781:1: E402
                           scripts/_grind_support.py:782:1: E402
```

Two `E402`s that shift line number only because #568 reformatted the file above
them. The lane was already failing on master before this stack.

## CI independently agrees

```
#568 Native build and complete unit tier (Python 3.12): SUCCESS
#569 Native build and complete unit tier (Python 3.12): FAILURE
```

Both PRs remain draft and unmerged. Findings posted publicly on each PR. These
are author-side verifications, not the independent review the release bar
requires.
