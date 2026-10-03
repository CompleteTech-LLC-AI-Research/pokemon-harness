# #554 adjudicated: keep refusing, option (a)

Reproduced on current master `9c5727b7`, then resolved as a documented
decision rather than a defect.

## Reproduction

Built a wheel from this checkout and installed it non-editably into a fresh
venv, then ran the guard from that interpreter against this checkout:

    $ python -m pip wheel --no-deps -w ./house .
    $ ./wvenv/bin/pip install --no-deps ./house/*.whl
    $ cat wvenv/lib/python3.12/site-packages/pokered_harness-*.dist-info/direct_url.json
    {"archive_info": {"hash": "sha256=3d17f19b...", "hashes": {...}},
     "url": "file:///.../.scratch/r554/house/pokered_harness-0.1.0-py3-none-any.whl"}

    $ ./wvenv/bin/python scripts/check_import_origins.py --project-root .
    status: FAIL
      pokered_harness FAIL resolves outside this checkout /workspace/poke-harness/pokemon
                       and outside the site-packages of an install made from it
      pyboy         FAIL (same)

    ./wvenv/bin/python -c 'import pokered_harness; print(pokered_harness.__file__)'
    /workspace/poke-harness/.scratch/r554/wvenv/lib/python3.12/site-packages/pokered_harness/__init__.py

Still live after #559 and #560; the cause described in the issue is unchanged.

## Decision

**Option (a): keep refusing, and document the editable-install-only lane.**

`direct_url.json` for an ordinary wheel install names the *wheel file in the
wheelhouse*, not the source tree. The wheel itself carries no build-time source
provenance: its dist-info holds `METADATA`, `RECORD`, `WHEEL`,
`entry_points.txt`, `top_level.txt` and licenses, and none of them record the
tree it was built from.

Widening `_is_this_checkout` to accept a `.whl` path would make every wheel
anywhere on disk an allowed root, which is exactly the provenance guarantee
#534 exists to provide. Option (b) -- stamping the source tree into the wheel
at build time -- is the only way to admit wheel installs without that
weakening, and it is a packaging change (a custom metadata field written by the
build backend) with its own review, not a guard change.

## This is already documented, and the documentation is accurate

`docs/PRODUCTION_RUNBOOK.md` states the install must be editable, shows the
`direct_url.json` that causes the refusal, and cites this issue. I verified the
runbook's claim that the wheel lane is unaffected rather than assuming it:

    $ grep -rn "check_import_origins" scripts/run_local_ci.sh
    (no match)
    $ grep -rn "check_import_origins" scripts/production_gate_execution.py
    161: str(Path(__file__).resolve().parent / "check_import_origins.py")

The guard runs only as the collection preflight in
`scripts/production_gate_execution.py`, which always runs against an editable
install. The wheel packaging lane uses `bootstrap_pyboy.py --mode source
--check`, which does not invoke the guard. So the documented release workflow is
unaffected, and the refusal is by design rather than by accident -- which is
what the runbook claims.

## What this does not establish

The refusal being correct does not make the wheel lane *qualified*. The
runbook's own qualification table records the wheel row as covering "ten public
imports, and pinned Red-color MCP startup/EOF cleanup ... no MCP requests or
gameplay exercised". Real-ROM gameplay through a non-editable install is not
qualified by this adjudication and remains tracked by #72.
