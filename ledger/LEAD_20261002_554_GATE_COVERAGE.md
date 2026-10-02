# LEAD — #554 adjudicated: refusing wheel installs costs no covered lane

Session: 2026-10-02T23:20Z. Trunk `master` = `origin/master` = `a11a100`, 0/0.

`LEAD_20261002_551_WHEEL_FINDING_ADJUDICATED.md` correctly reproduced #554 and
called it pre-existing. It stopped one step short of the question that actually
decides it: **does any lane we run or claim ever invoke the guard against a
non-editable wheel install?** This answers that, from the CI scripts and the
runbook rather than from reasoning about the code.

## The symptom, reproduced again on the #558 head

Built from `9084db8`, installed non-editably, ran the guard:

```
$ pip wheel --no-deps -w ./house .        # -> pokered_harness-0.1.0-py3-none-any.whl
$ wvenv/bin/python -m pip install --no-deps ./house/*.whl
$ wvenv/bin/python scripts/check_import_origins.py --project-root <src>
  status: FAIL
  pokered_harness FAIL .../site-packages/pokered_harness/__init__.py
  "resolves outside this checkout ... and outside the site-packages of an
   install made from it"
```

`direct_url.json` names the wheelhouse copy, exactly as previously recorded:

```
{"archive_info": {"hash": "sha256=a0fce5d6..."},
 "url": "file:///tmp/wh554a/house/pokered_harness-0.1.0-py3-none-any.whl"}
```

One detail worth recording: the wheel **bundles the vendored `pyboy`** (104
entries, `pyboy/` -> `vendor/pyboy-src/pyboy` per `[tool.setuptools.package-dir]`)
under a **single** `pokered_harness-0.1.0.dist-info`. There is no separate
`pyboy` dist-info and no `RECORD`-attested `pyboy`. So both checked packages
inherit one wheelhouse provenance record, and `pyboy` also FAILs with
`ModuleNotFoundError: numpy` in the bare venv -- an artifact of the
`--no-deps` probe, not the bug.

## The decisive evidence: the guard never runs in the wheel lane

`scripts/run_local_ci.sh` and `.github/workflows/release-hygiene.yml` contain
the same two distinct lanes:

| lane | line (`run_local_ci.sh`) | install | runs the guard? |
|---|---:|---|---|
| source/native gate | 169, 223, 306 | `pip install -e ".[dev]"` (line 132) | **yes** -- via `production_gate.py` -> `production_gate_execution.py` -> `check_import_origins.py` |
| clean wheel install | 363 | `pip install --no-cache-dir "$RUNNER_TEMP"/pokered-wheels/*.whl` | **no** |

The wheel lane verifies `pip check`, `bootstrap_pyboy.py --mode source --check`,
and import identity by its own assertions. It never invokes
`check_import_origins.py`; `grep -c check_import_origins scripts/run_local_ci.sh`
returns **0**. The guard's only call site in the whole tree is
`production_gate_execution.py:161`, and that always runs against
`project_root` on the editable interpreter.

The runbook says the same thing in prose:

> "The wheel-install probe in this historical audit verified clean
> installation, `pip check`, and bundled runtime identity; it did not run this
> real-ROM stdio tier on Linux."

and `docs/RELEASE_CHECKLIST.md:341` lists the fresh wheel install as a
**separate** row from the gate.

## Adjudication

**Option (a) — keep refusing — is correct, and this is now an evidence-backed
decision rather than a deferral.**

The refusal costs nothing that is currently claimed or exercised, because the
wheel lane never runs the guard. The trade-off named in #554 is therefore
asymmetric and resolves cleanly:

- Admitting wheelhouse paths would make **any `.whl` on disk** an allowed root.
  That is precisely the property #534 exists to remove, and it would
  **weaken** the guarantee in order to fix a lane nobody runs.
- Refusing costs zero coverage, because no gate, check, or documented
  qualification row pairs a wheel install with the guard.

## What this does and does not settle

- It does **not** close #554. The real design question stands: if a wheel lane
  ever *does* need the guard, option (b) -- recording build-time source in the
  wheel's own metadata, which this build does not do (`METADATA` carries no
  custom provenance field) -- is the sound route. Option (a) simply means no
  such lane exists today.
- It does **not** touch #551 or #558. No guard logic was modified.
- The issue stays open with the narrow question: **should a wheel install ever
  be guard-checkable?** Until that is a yes, (a) holds and nothing needs
  changing.

No finding is waived. This narrows scope and supplies the missing evidence; it
is not a silent "fix" by weakening the provenance guarantee.

Evidence: wheel and clean venv under `/tmp/wh554a` (disposable), built from
`9084db8`.
