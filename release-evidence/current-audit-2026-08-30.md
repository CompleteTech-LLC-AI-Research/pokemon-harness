# Current audit evidence — 2026-08-30

This record is for the isolated scheduler-fix candidate. ROMs, symbols, and
save states remain operator-managed external inputs; no ROM-derived bytes are
stored in this repository. Replace `<rom-root>` and `<fixture-root>` with the
same external roots for reproduction, and use one Python 3.12+ environment for
the gate and MCP subprocesses.

## Runtime identity

- Python: 3.12.13 for the production gate
- PyBoy: 2.7.0, fork revision
  `c565df66c3731fad2856169a90f6bbec99925915`
- Runtime mode: bundled Python source; serial contract
  `bit-accurate-backend`
- Dependencies: `uv.lock`, `uv lock --check`, and `uv pip check` pass
- Wheel: `uv build --wheel` succeeds and contains the bundled `pyboy` package

## Required gate results

All commands below were run from the isolated candidate with
`PYTHONPATH=src:vendor/pyboy-src`, matching external ROM/fixture roots, and
`POKERED_SKIP_SHA1` unset.

| Tier | Command/result | Outcome |
|---|---|---|
| Unit + timing | `scripts/production_gate.py --unit-only --repeat-timing 5` | 394/394 unit and 35/35 timing cases passed; both collection entry points passed |
| Local real-ROM | `scripts/production_gate.py --tier local` at the default 256-cycle slice | 46/46 passed; no skips, xfails, or errors |
| Remote real-ROM | `scripts/production_gate.py --tier remote` | 11/11 passed for color Red listener/internal-clock and color Blue connector/external-clock |
| Strict trade | `scripts/production_gate.py --tier trade` | 2/2 passed: local Red/Yellow and controlled Red/Blue subprocess |
| Strict battle | `scripts/production_gate.py --tier battle` | 2/2 passed: local Red/Yellow and controlled Red/Blue subprocess |

The strict local trade and battle tests also pass directly with the library's
default 256-cycle slice. A tighter `POKERED_LINK_CHUNK_CYCLES=64` rerun passes
as well, but that override is not required for the validated local path.

## Asset and single-session evidence

The production gate validated all five ROM hashes, all three symbol hashes, and
the three ordinary Cable Club fixture hashes against `VERSIONS.md`. The fixture
manifest validator passed all 10 manifest entries. Five separate explicit-ROM
MCP stdio runs passed tool discovery, stepping, game-state resource parsing,
and save/load roundtrip for:

- stock Red and color Red;
- stock Blue and color Blue; and
- Yellow.

## Known non-gates and limitations

- The formerly failing Blue↔Blue natural LinkMenu diagnostic now passes in
  targeted reruns, as do the Blue↔Yellow and Yellow↔Blue natural paths. The
  reverse-direction past-LinkMenu diagnostic
  `test_remote_rpc_flow_past_link_menu_over_tcp[yellow-blue]` still does not
  reliably complete: its independent runners can stall in cross-version
  serial/game-state synchronization. It remains outside the required
  acceptance tiers; the broad suite is not a release gate until this scope or
  implementation is resolved.
- `ruff check .` reports 540 findings, including vendored and legacy code.
- Battle and vanilla fixture provenance is incomplete in
  `fixture-manifest.json`; hashes establish byte identity, not capture history.
- The tested remote roles are fixed; reversed roles, user-driven remote menu
  control, and native Windows/Cython link attachment remain uncertified.
- TCP is loopback-only and unauthenticated/unencrypted. Cross-host use is not
  supported.
