# #551 round 5: the planted-`.pth` bypass is reproduced here, and two fixes are measured and rejected

Date: 2026-10-02
Branch: `lead/551-combine-553`
Head measured: `ba5a8c1`
Verified against `origin/master` `4b7822a`
Status: **release remains PARTIAL. #551 is NOT merged and must not be.**

## The bypass reproduces on this branch

`ledger/LEAD_20261002_551_INDEPENDENT_REVIEW.md` (on `origin/master` at
`3e0791e`) recorded a blocking finding against `90deb71`: a `.pth` written into
site-packages certifies a finder that has no provenance at all, so the guard
answers `PASS` and then serves a submodule from outside the checkout.

Reproduced end to end on `ba5a8c1`, planting two files (`repro_planted_pth.py`
in the review evidence tree):

    _is_installation_finder(forged): True
    guard status: PASS
    foreign submodule loaded: foreign
    *** FULL BYPASS via attacker-written .pth ***

This is the same gap `#558` carries, and the same one that makes round 8 there
reverted. It is shared, not a discriminator between the two branches.

## Round 5 closed the other three review findings

The three round-4 escapes on `5c272ae` are fixed and committed at `ba5a8c1`:
stdlib trust is decided by identity rather than set membership, the
`sys.meta_path` snapshot and the `packages_distributions()` walk are guarded
against a raising `__iter__`, and the `__path__` portion list is materialised
inside its guard. All three are killed by mutation, and `KeyboardInterrupt` /
`SystemExit` are still re-raised.

## Fix attempt A -- require the `.pth` to be co-owned by one distribution

Rationale: the editable shim is installed as a pair, and pip records the
`.pth` and the finder module it imports in the same `RECORD`. Requiring one
distribution to claim both ties the `.pth` to the shim.

**Rejected: it does not discriminate, and it false-reds.**

Measured, co-ownership against each witness:

| witness | co-owned? |
|---|---|
| `_virtualenv.pth` + `_virtualenv.py` (real) | `False` |
| `__editable__.pokered_harness-0.1.0.pth` + its finder (real) | `True` |
| planted shim + self-consistent forged `RECORD` | `True` |
| planted shim, `RECORD` written under a real distribution's name | `True` |

The last two rows are the point. An attacker who can write into site-packages
writes a `RECORD` that claims both planted files, and its digest claim is
self-consistent, so co-ownership is satisfied. It removes no path the attacker
did not already have.

The false red is worse than that. With co-ownership required and no fallback,
the real guard on this checkout refuses the genuine environment shim:

    detail: untrusted sys.meta_path finder(s) ... _virtualenv._Finder
            (from .venv-source/lib/python3.12/site-packages/_virtualenv.py)
    status: FAIL

`_virtualenv.py` is not claimed by any distribution's `RECORD` on this host,
which is exactly the property its own docstring claims for the shims. So the
rule refuses a real installation finder in order to catch an attacker who can
forge the same evidence. Confirmed independently: the editable shim *is*
RECORD-attested here, and `_virtualenv.py` is not.

## Fix attempt B -- accept a `.pth` only inside a virtualenv site-packages

Rationale: `_virtualenv.py` is not in any `RECORD`, so fall back to "the
`.pth` lives in a venv's own site-packages" (`pyvenv.cfg` three levels up).

**Rejected: the real site-packages *is* a virtualenv**, so the fallback admits
every `.pth` planted in it, which is precisely the attacker's position. The
full bypass still reproduced with this rule in place. It distinguishes a venv
from a non-venv; it does not distinguish a legitimate activation hook from one
the attacker wrote beside it.

The first attempt also had an off-by-one, reading `pyvenv.cfg` two levels up
instead of three. Fixing it made the false red in attempt A sharper, not
softer, and is not the reason either attempt was rejected.

## Position

The honest statement is the one the independent review already reached: the
`.pth` channel needs a discriminator that is not an install record at all, and
attesting the `.pth` cannot supply one, because inside the declared model the
attacker controls both the `.pth` and its attestation. Both candidate
discriminators were built and measured here; both fail for the two independent
reasons above.

So the planted-`.pth` gap is **open on #551**, and the existing test
`test_a_planted_pth_cannot_certify_a_hand_written_finder` pins that behaviour
deliberately rather than by accident. It is not closed by this round.

`#551` is not merged, and it must not be, on this head.
