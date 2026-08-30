# Current audit evidence — 2026-08-30

This record is for the isolated scheduler-fix candidate. ROMs, symbols, and
save states remain operator-managed external inputs; no ROM-derived bytes are
stored in this repository. Replace `<rom-root>` and `<fixture-root>` with the
same external roots for reproduction, and use one Python 3.12+ environment for
the gate and MCP subprocesses.

## Runtime identity

- Python: 3.12.3 for the production gate
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
| Unit + timing | `scripts/production_gate.py --unit-only --repeat-timing 5` | 392/392 unit and 35/35 timing cases passed; both collection entry points passed |
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

- `pytest -x -vv -ra` reaches 49 passed and 5 known fixture-walkability skips,
  then fails at the legacy diagnostic
  `test_remote_trade_reaches_link_menu_via_tcp[blue-blue]` because
  `SaveGameData` remains `[0, 0]`. This diagnostic is outside the required
  acceptance tiers; the full suite is not a release gate until its scope or
  implementation is resolved.
- `ruff check .` reports 546 findings, including vendored and legacy code.
- Battle and vanilla fixture provenance is incomplete in
  `fixture-manifest.json`; hashes establish byte identity, not capture history.
- The tested remote roles are fixed; reversed roles, user-driven remote menu
  control, and native Windows/Cython link attachment remain uncertified.
- TCP is loopback-only and unauthenticated/unencrypted. Cross-host use is not
  supported.
