# #558 round 8 retracted: the planted-`.pth` fix does not hold, and it false-reds

Date: 2026-10-02
Branch: `lead/556-const-collision` → PR #558
Retracted head: `722ecb2` (round 8). Replacement head: `14aa5b9` (revert).
Verified against `origin/master` `9500c97`
Status: **round 8 WITHDRAWN. #558 still unmerged, still needs independent review.**

## Summary

Round 8 claimed a full bypass and claimed to close it. Both halves of that
claim are wrong, and the change is reverted at `14aa5b9`. The bypass it named
is real; the fix does not close it, and it rejects a case the function exists
to support.

## The bypass was real

Against `ccb8982`, honouring any `.pth` that names the module is satisfied by
writing one line of text into site-packages, and the guard reported `PASS`
with a foreign submodule loading afterwards. That measurement stands.

## Why the fix does not close it

Round 8 required the `.pth` to be `RECORD`-attested. But the guard's own
residual boundary already declares that "an attacker who can write a file into
this interpreter's site-packages" is out of scope. An attacker in that position
can write the `dist-info/RECORD` that attests the `.pth` just as easily.

Measured end to end on `722ecb2`, planting the `.pth` *and* forging the
`RECORD` that attests it:

```
_is_installation_finder(forged): True
guard status: PASS
detail:
foreign submodule loaded: foreign
*** FULL BYPASS ***
```

The same probe against `ccb8982` prints the identical six lines. So round 8
does not change the outcome for the attacker it targets; it only removes a
path the attacker already had a second route around.

## Why it also false-reds

`_is_imported_by_a_pth`'s docstring gives the reason the channel exists:

> `_virtualenv` and the `__editable__` shim are not owned by any
> distribution's `RECORD`: they are dropped into site-packages and activated
> by a `.pth` file that imports them by name.

Round 8 requires precisely the attestation that docstring says those shims do
not have. Measured on the same shim shape, differing only in the head:

| head | non-`RECORD` shim, `_is_imported_by_a_pth` |
|---|---|
| `ccb8982` (pre-round-8) | `True` |
| `722ecb2` (round 8) | `False` |

A genuine virtualenv or editable-install activation would be reported as an
untrusted finder. On this host the one real `.pth`
(`distutils-precedence.pth`) is `RECORD`-attested through `_distutils_hack`, so
the suite does not catch the regression — the shims the channel was written for
are not installed here, so the false red is invisible locally.

## Verification of the revert

`git revert 722ecb2` restores `scripts/check_import_origins.py` and
`tests/test_import_origin_guard.py` byte-identically to `ccb8982`
(`git diff ccb8982` on the source file is empty).

| Check | Result |
|---|---|
| `pytest tests/test_import_origin_guard.py tests/test_fixture_provenance.py` | 130 collected, rc=0, no failures |
| `ruff check` on both changed files | clean |
| `/tmp/probe_forge_spec.py` (forged `__spec__`) | `TRUSTED: False`, `guard status: FAIL` |
| `/tmp/probe558_two.py` (raising `__file__`, forged `co_filename`) | `TRUSTED: False`, `guard status: FAIL` |
| `/tmp/probe_record_csv.py` (comma CSV path, conflicting claims) | `True` / `False` as expected |

Rounds 1–7 are unaffected; this reverts only round 8.

## Consequence for the mutation matrix

The round-8 ledger row "Honour any `.pth` naming the module → planted-`.pth`
row" is withdrawn. There is still no row that kills a planted `.pth` within
the declared model, and the honest statement is the one above: attesting the
`.pth` cannot help, because inside the threat model the attacker controls both
the `.pth` and its attestation. Closing this properly needs a discriminator
that is not an install record at all, not a stronger hash over one.

## Disposition

No independent review of `14aa5b9`. Author may not approve own change and
delegation has delivered no task (eleventh failure of #489). **#558 remains
open and unmerged.** Release stays `PARTIAL`.
