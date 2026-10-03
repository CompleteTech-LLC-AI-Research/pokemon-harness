# Verified: every real-ROM asset is absent, so the ROM-gated issues cannot close here

Date: 2026-10-03
Tree: `master` `a260a02b`
Command: `scripts/production_gate.py --runtime-mode source --unit-only --format json`
Report: `.scratch/g253unit2.json`

## Evidence

The gate enumerates 14 required assets. Every ROM has `actual_sha1: null`:

```
{"actual_sha1": null, "expected_sha1": "ea9bcae617fdf159b045185467ae58b2e4a48b9a",
 "kind": "rom", "label": "red-stock",
 "path": "/workspace/poke-harness/pokemon/rom/red/pokemon-red.gb", "size": null}
{"actual_sha1": null, "expected_sha1": "cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1",
 "kind": "rom", "label": "yellow",
 "path": "/workspace/poke-harness/pokemon/rom/yellow/pokemon-yellow.gbc", "size": null}
```

Directly corroborated, rather than inferred from the report:

```
$ ls rom/
ls: cannot access 'rom/': No such file or directory
$ find . -maxdepth 3 \( -name '*.gbc' -o -name '*.gb' \) -not -path '*/.git/*'
(no output)
$ find . -maxdepth 3 -name '*.sym' -not -path '*/.git/*'
(no output)
```

`actual_sha1: null` with a non-null `expected_sha1` means the asset is **absent**,
not merely unverified. There is no ROM, no `.sym`, and no `rom/` directory at
all. The 31-entry fixture manifest still passes `mode: schema`, which is the
point of `agents.md`'s "fail-closed, not green" rule: schema conformance is
checkable without the bytes, so that part is genuinely `PASS`.

## What this settles

This is the exact external blocker named by the issues themselves, now measured
on the current head rather than carried forward as prose:

- **#235** states it is "ROM-gated: it needs real assets (`pokemon-yellow.gbc` +
  `.sym`, Red/Blue ROMs) that are **absent** in this container". Confirmed.
- **#72** needs source/native qualification of the merged link fixes, which
  requires the real-ROM gates. Confirmed unreachable.
- **#87-#105**, **#109-#110**, and the `trade`/`battle`/`timing` acceptance
  matrices all require the same assets. None can be produced here.
- The `timing` tier's 61 failures decompose to 54 `/dev/shm` plus 7 the
  lockstep stall (see `REVIEW_253_LOCKSTEP_INDEPENDENT_b0f09461.md`); neither
  is a ROM-availability failure, so #253 is a separate matter from #235/#72.

No ROM may be committed, and none is present to use. These rows stay **open**
with this exact external dependency recorded, not closed and not called green.
Release status stays **PARTIAL**.

## Two distinct external blockers, do not conflate them

1. **ROM assets absent** -- affects #235, #72, #87-#105, #109-#110, and the
   stateful trade/battle/timing matrices.
2. **`/dev/shm` mounted read-only, 63 MB, no sudo** -- affects 79 of the 86
   #253 unit/timing failures.

Both are outside the repository. Neither has an in-repo fix, and neither was
worked around by skipping, xfailing, or widening a bound.
