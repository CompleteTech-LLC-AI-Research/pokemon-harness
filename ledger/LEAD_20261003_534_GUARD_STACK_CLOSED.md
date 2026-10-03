# #534 import-origin guard: the whole PR stack is closed on master

Closes out every open PR against #534. Master `21abe115` carries the full scope.

## What merged

| PR | head | merge | disposition |
|---|---|---|---|
| #549 | `a35af956` | `00daf280` | merged; ledger corrected |
| #560 | `f5341576` | `14604aca` | merged; **reverted by mistake in `4c06f72`, restored in `c00fbf16`** |
| #559 | `ab8f94f6` | `21abe115` | merged: interrupt propagation, `.pth` attestation, rebound-finder trust |
| #561 | `e6fa9e75` | — | superseded; the format blocker it fixed was `f1c630ee`, inside #560 |
| #563 | `ab534bfa` | — | superseded by `c00fbf16` |
| #548, #555, #556 | — | — | superseded; 0 unique rows on master, and master's shapes are stricter |
| #557, #558 | — | — | superseded; behaviour present on master, verified by harvesting rows |
| #551 | — | — | superseded by #559, strictly stronger |

## The regression, and its cause

`4c06f72` was meant to add one ledger file. It also reverted
`scripts/check_import_origins.py` and `tests/test_import_origin_guard.py` to
their exact pre-#560 blobs (`298c882` / `d617aee`) while GitHub still reported
#560 as MERGED. Caught by #562/#563, not by my own pass.

Cause: after #560 merged I read `git status` reporting "behind 6" as drift and
ran `git reset --soft origin/master` before staging a ledger file. Those 6
commits were #560's merge and its parents, so the reset moved `HEAD` to the
merge while the index still held #560's post-merge content, and the commit made
from that index wrote the code files back.

Fix: `c00fbf16` restores both blobs from `14604aca`. Verified on a fresh venv
bound to the restored tree: guard `status: PASS`, 145 rows green.

## #559: four defects, five rows each verified non-vacuous

1. `_finder_source` swallowed `KeyboardInterrupt`/`SystemExit`.
2. A `.pth` naming a module certified it with no provenance.
3. A recorded `.pth` certified a module no install recorded.
4. A rebound `sys.meta_path` entry stayed trusted.
5. A rewritten `.pth` still certified its module.

Defects 3-5 were each found by an independent reviewer rejecting the previous
round, and each rejection is the reason for one rule in the final code. Defect
4 was found by auditing #551 rather than assuming it superseded.

Independent review (`codex2`, separate from the author): **APPROVE WITH
FINDINGS, zero findings, no bypasses.** All three mutation checks killed.

## Verification on the merged tree `21abe115`

    scripts/check_import_origins.py  status: PASS (pokered_harness, pyboy)
    pytest tests/test_import_origin_guard.py  162 passed, 0 failed, 0 errors, 0 skipped
    ruff check / ruff format --check        clean

Merged blobs were checked against the reviewed head `ab8f94f6` and are
identical, so this is the reviewed tree and not an approximation of it.

## Still open

Release status stays **PARTIAL**. The 31 open issues (#72, #84-#89, #90-#110,
#235, #253, #489, #554) are untouched by this PR stack and remain tracked
separately. #253 needs a quiet host: this machine's `/dev/shm` is a read-only
63 MB and `unshare -m` returns EPERM, so the probe-spawn tier cannot pass here.
