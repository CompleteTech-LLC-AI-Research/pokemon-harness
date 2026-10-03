# LEAD — #558 round 7: site-candidate escape repaired, and real AC4 install evidence obtained

## Trunk

```
master        = fda738e33595b0c40e3292d02b61c3b55eb2a5da
origin/master = fda738e33595b0c40e3292d02b61c3b55eb2a5da
origin/master...master = 0 0
```

## Candidate

```
branch lead/556-const-collision
head   677bca4487d3936348b8a0401edd774ab375a857
```

## Round 7 -> 677bca44 — the site-candidate read

Round 7 reviewed `d39dda16` and returned INCOMPLETE — the host filesystem went
read-only again during its venv build — but it did land one real finding before
dying, and the finding was correct: **the round-5 repair of the site getters was
incomplete.**

Round 5 fixed the `site.getsitepackages()` / `getusersitepackages()` calls. It
did not fix what happens to the value they return. Each candidate was then
tested for truthiness and converted with `Path(...).resolve()` outside any
`BaseException` guard, and a candidate is a value the *environment* supplied.

Reproduced before the fix:

```
RESULT: ESCAPED from _site_packages_roots: Boom: site candidate bool boom
RESULT: ESCAPED out of check_origins(): Boom: site candidate bool boom
```

The truthiness test was itself the unguarded read — the raise fired there
before conversion was even reached. Both the test and the conversion now sit
inside one `BaseException` guard that skips an unusable candidate, so an
unreadable layout yields no roots and the guard fails closed.

Row: `test_a_hostile_site_candidate_cannot_abort_the_guard`, proven to FAIL on
`d39dda16`.

This is the seventh repair in one defect class across seven rounds. Each round
found exactly one more instance, and each was reachable from the public entry
point. The class is not yet declared closed.

## AC4 — real install evidence, both directions retained

Four consecutive review rounds returned INCOMPLETE without this evidence, and
the lead has now produced it directly instead of accepting a review that could
not.

```
mis-pointed  foreign tree first on sys.path, guard inspects reviewed checkout
             rc=1  status FAIL
               pokered_harness FAIL  resolves outside this checkout ... and
                                     outside the site-packages allowlist
               pyboy          FAIL  (same)

repaired     reviewed checkout on the path
             rc=0  status PASS
               pokered_harness PASS
               pyboy          PASS
```

Evidence retained at
`/workspace/poke-harness/.scratch/r558s/ac4/mispointed.json` and
`.../ac4/repaired.json`.

**A mistake was made and corrected, and it is recorded here rather than hidden.**
While building the mis-pointed install, `pip install -e ./ac4/foreign` was run
with `/workspace/poke-harness/pokemon/.venv/bin/python` as the installer. That
repointed the repository's own editable install at the foreign copy. It was
detected immediately, restored with a `--force-reinstall -e .` from the repo
root, and verified:

```
pokered_harness resolves to /workspace/poke-harness/pokemon/src/pokered_harness/__init__.py
guard on repo: rc=0
tracked modifications: 0    untracked: 137    worktrees: 659
editable finder contains /workspace/poke-harness/pokemon
```

The isolated `--target` install was then redone correctly. The repository was
never left damaged, but the instructions for this repo are explicit about
preserving its install state, and the safe form is `--target` into scratch, never
`-e` with the repository's own interpreter.

## Evidence on the current head

```
guard suite   : 119 passed, rc=0   (115 -> 116 -> 118 -> 119)
AC4           : mis-pointed rc=1 FAIL both; repaired rc=0 PASS both
ruff          : 1 finding, the pre-existing import-sort one
```

## Not merged

No independent APPROVE. Seven reviews have run; five returned INCOMPLETE for
environmental reasons and two returned REQUEST CHANGES with real findings. None
has returned APPROVE, so #558 is not merged and #547 is not closed.

The reviewer path is the bottleneck now, not the code. `codex2` has been killed
by ENOSPC, by a read-only `/workspace` twice, and once by a silently dying
background process, always mid-probe. Release remains **PARTIAL**.
