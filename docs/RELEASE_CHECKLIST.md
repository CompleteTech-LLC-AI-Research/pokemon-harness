# Release checklist

Use this checklist for a proposed `pokered-harness` release. A source pin,
fixture hash, or passing unit test is not a substitute for the real-ROM and
runtime evidence required by the capability being advertised.

## Status semantics

- `PASS` is scoped to one named command or tier. It requires clean collection
  and no failure, error, skip, xfail, or timeout in that scope.
- `PARTIAL` means that controlled evidence exists but one or more release
  conditions remain open.
- `PRODUCTION-READY` requires a clean candidate, all required BYO assets, a
  complete strict acceptance declaration and runtime matrix, retained evidence,
  and no open blocker.

## Current audit snapshot

The latest candidate gate ran from the tree rooted at `ae8d63d` (2026-08-31)
with `DEFAULT_MATRIX_WORKERS=1`. The earlier clean asset-free command:

```bash
EVIDENCE_DIR="$(mktemp -d)"
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --unit-only \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

returned scoped `PASS` from a clean worktree: collection 648, unit 507/507,
and timing 35/35 in each of five repetitions. It used bundled source PyBoy
2.7.0, fork
`c565df66c3731fad2856169a90f6bbec99925915`, and schema-validated the
ten-entry fixture manifest. No ROM-backed tier ran in this command.

The same collection audit found all nine ordered local pairs, all nine ordered
remote listener/connector pairs, six reversed-role rows, all nine local variant
rows, and 19 strict trade plus 19 strict battle entrypoints. Structural and
declaration checks passed; matrix runtime was `NOT RUN` because the standalone
audit is collection-only.

The current candidate strict trade matrix passed 19/19 with no skips, xfails,
or errors. The strict battle matrix passed 17/19 with no skips, xfails, or
errors; the two failures were remote `red_color` listener rows connecting to
color Blue and Yellow at the bounded LinkMenu phase. Earlier selected runs are
historical diagnostics and do not replace this result.

**Release decision: `PARTIAL`.** The canonical color Red, color Blue, and
Yellow fixture bytes have recorded reproduction evidence, and the bounded
producer plus tracked battle-fixture generator are present. Full sign-off is
pending the two failed remote battle rows, Cython gameplay coverage, vanilla
source provenance, broad-suite coverage, native-platform evidence, retained
complete evidence, and independent review.

## Source and artifact hygiene

- [x] The candidate evidence boundary is identified as the tree rooted at
  `ae8d63d` plus the committed conservative matrix scheduling change; this
  documentation is reviewed against that boundary.
- [x] The checkout is source-only: ROMs, symbols, save states, screenshots,
  logs, caches, and virtual environments are not tracked.
- [x] Documentation keeps BYO assets and external evidence outside the source
  tree and uses relative paths or placeholders rather than machine paths.
- [x] The candidate evidence run used an isolated worktree and its exact
  runtime, asset hashes, deadlines, and outcome are recorded in the README and
  runbook; external evidence remains outside the source tree.

## Runtime and dependency identity

- [x] Python requirement is `>=3.12`.
- [x] The distribution bundles source PyBoy `2.7.0` at fork revision
  `c565df66c3731fad2856169a90f6bbec99925915`.
- [x] `mcp==1.29.1` is pinned in `pyproject.toml`.
- [x] The asset-free gate resolved the bundled source runtime and the
  bit-accurate serial contract.
- [ ] Any release environment runs `python -m pip check` and records the
  interpreter/runtime identity from the same environment used by MCP.
- [x] Cython/native-accelerator mode builds and passes its explicit runtime
  serial contract in a seeded disposable Python 3.12 environment.
- [x] The pinned Cython build exposes `mb.serial` and passes a real-ROM
  attach/detach/close smoke.
- [ ] Full Cython trade/battle acceptance is complete; source mode remains the
  documented release default and neither mode has full strict-matrix sign-off.

## ROM, symbol, and BYO asset identity

- [x] `VERSIONS.md` records SHA-1 pins for stock/color Red, stock/color Blue,
  Yellow, and the three matching symbol files.
- [ ] The operator attests that every supplied ROM and symbol file is legally
  obtained and matches the documented pin.
- [x] Release commands require explicit `POKERED_ROM_SHA1` and do not use
  `POKERED_SKIP_SHA1=1`.
- [x] The default real-ROM gate expects five ROMs and three symbols; the
  acceptance scope additionally requires the canonical ordinary and battle
  states for color Red, color Blue, and Yellow.
- [ ] The operator runs the full gate with those assets and records exact
  hashes, sizes, deadlines, skips, xfails, failures, and teardown results.

## Test gates

- [x] Both module and console-script collection paths complete in the scoped
  `dfee2ec` gate; 648 tests were collected with no collection errors.
- [x] ROM-free unit tests pass 507/507 in the scoped gate.
- [x] Timing tests pass 35/35 in each of five repetitions in the scoped gate.
- [x] `ruff check .` is clean at the committed source candidate boundary.
- [ ] `python -m pytest -q -ra` completes with no unexpected failure, skip,
  xfail, or timeout.
- [ ] Real-session boot, state, and MCP stdio tests pass for every advertised
  ROM variant. The current asset-free gate does not exercise this tier.
- [ ] The current candidate rerun of the real-ROM local and remote tiers passes
  with no fixture or runtime skips; strict trade is green, but two remote
  Red-listener battle rows still fail at LinkMenu.
- [x] Current-candidate controlled local evidence covers the complete canonical
  matrix: 9/9 ordered trade rows, 9/9 ordered battle rows, and both dedicated
  Red/Yellow assertions passed.
- [ ] Current-candidate controlled remote evidence covers every ordered
  listener/connector pair: trade is 9/9, while battle is 7/9 because the two
  Red-listener rows remain failing.
- [x] The strict acceptance declaration has an entrypoint for every canonical
  ordered local and remote Red/Blue/Yellow pair (19 trade and 19 battle node
  IDs); every declared row was executed in the current candidate gate.
- [ ] Current-candidate local and remote strict runtime rows pass in both
  listener/connector directions; local and trade rows are green, but the two
  remote Red-listener battle rows remain open and concurrent-load stability is
  not certified.
- [ ] Native-platform/build coverage and an independent release review are
  complete.

## Fixture provenance and generation

- [x] `release-evidence/fixture-manifest.json` records ten external state
  entries with sizes, SHA-1/SHA-256 values, expected ROM/SYM pins, source-state
  records, runtime identity, and command templates.
- [x] Canonical color-Red, color-Blue, and Yellow ordinary and derived battle
  fixture bytes have deterministic reproduction evidence.
- [x] `scripts/produce_cable_club_fixture.py` validates pins and has bounded
  defaults of 180 seconds and 64 movement steps.
- [x] `scripts/prepare_battle_cable_club_fixtures.py` is tracked and produces
  immutable derived battle fixtures; acceptance does not prepare party state
  in emulator RAM.
- [ ] Vanilla ordinary source provenance is verified. The retained Red/Blue
  vanilla source states are not proven to match the vanilla ROM, so vanilla
  ordinary and derived battle rows remain `PARTIAL`.
- [ ] The operator validates all ten manifest entries with
  `python scripts/validate_fixture_manifest.py --fixture-root ...` and keeps
  the external fixture root available to the release runner.
- [ ] A retained evidence bundle includes the exact source-state hashes,
  capture/generation commands, runtime identity, and original-vs-verification
  timestamp distinction.

## MCP and network operation

- [x] MCP launch examples use explicit ROM, symbol, and SHA-1 values.
- [x] The bundled runtime is the default path and no machine-local
  `PYTHONPATH` is required by `.mcp.json`.
- [x] The portable `.mcp.json` contract is documented: the client expands
  `${PWD}` (or an equivalent workspace variable), launches the installed
  environment's `python`, and resolves the selected BYO assets from the
  workspace root.
- [x] MCP stdout remains the JSON-RPC channel; diagnostics are kept off the
  protocol stream.
- [x] TCP binding and connection are restricted to loopback addresses.
- [x] Lifecycle code has controlled pair/listen/connect/disconnect/close
  coverage, but final real-ROM teardown must still be recorded in the full
  evidence bundle.
- [ ] Cross-host TCP is not enabled without adding authentication and
  encryption; the current transport is unauthenticated and unencrypted.

## Required release commands

Run from a clean checkout with the same interpreter used for installation and
MCP:

```bash
python -m pytest --collect-only -q
python -m pytest -q -ra
python scripts/tcp_link_matrix.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --format text
python scripts/validate_fixture_manifest.py --schema-only
python scripts/validate_fixture_manifest.py \
  --fixture-root "$PWD/tests/fixtures/link"
EVIDENCE_DIR="$(mktemp -d)"
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --rom-root "$PWD/rom" \
  --fixture-root "$PWD/tests/fixtures/link" \
  --python "$(command -v python)" \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

The matrix command is collection-only and returns zero when its structural and
strict declaration checks pass. The asset-free gate may return `PASS`
without ROMs because it selects only unit and timing; the default gate must
return `PASS` only after assets, strict matrix, fixture, and all required
real-ROM tiers pass. Keep the sanitized evidence bundle outside version
control.

Do not describe a skipped, xfailed, timed-out, synthetic, hook-only,
RAM-mutated, or LinkMenu-only result as a completed trade or battle.
