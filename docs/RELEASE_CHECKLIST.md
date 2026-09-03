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

The published base is `8727779` (PR #28 documentation merge). The current
isolated candidate adds `94f4429`, `9ce7c9f`, `3d04293`, `f046c34`, `8b4847b`,
`9453d7a`, and `abf3d27` for packaging, lifecycle, TCP, gate,
headless-performance, and TCP-peer-teardown hardening. This remains a
`PARTIAL` publication candidate until the remaining verification and review
conditions are complete.
The complete all-tier source-runtime baseline
ran from isolated source head `df0e7424c87c812a57f257286b0dc00e87c498f4`,
whose implementation tree is the PR #17 parent. The complete baseline gate
command was:

```bash
EVIDENCE_DIR="$(mktemp -d)"
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --rom-root "$PWD/rom" \
  --fixture-root "$PWD/tests/fixtures/link" \
  --python "$(command -v python)" \
  --runtime-mode source \
  --repeat-timing 5 \
  --matrix-workers 1 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

It returned `PASS`: collection 698, unit 554/554, local real-ROM 47/47,
remote transport/MCP 15/15, strict trade 19/19, strict battle 19/19, and
timing 40/40 in each of five repetitions. All ten external fixture-manifest
entries validated; no selected test skipped, xfailed, failed, errored, or
timed out. The sanitized evidence bundle is retained outside version control
because ROMs, symbols, and save states are external BYO assets.

The clean asset-free command remains:

```bash
EVIDENCE_DIR="$(mktemp -d)"
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --runtime-mode source \
  --unit-only \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

returns scoped `PASS` on the integrated candidate: collection 730, unit
586/586, and timing 40/40 in each of five repetitions. It uses bundled source PyBoy 2.7.0,
fork `c565df66c3731fad2856169a90f6bbec99925915`, and schema-validates the
ten-entry fixture manifest. No ROM-backed tier ran in this command.

The same unit/timing gate passes with `--runtime-mode cython` after the pinned
fork is built with `scripts/bootstrap_pyboy.py --mode cython`; its runtime
probe reports all five required PyBoy modules as native extensions. Recorded
The integrated source and Cython remote tiers pass 15/15, and the integrated
Cython local/session tier passes 47/47. The source strict trade and battle
gates each reached 18/19 under bounded parallel execution; the isolated exact
retry for each failed remote Yellow-to-Yellow selector passed. Exact native
Red-to-Red trade/battle and Blue Color-to-Red Color trade rows pass, but the
full native strict matrices remain open.

The same collection audit found all nine ordered local pairs, all nine ordered
remote listener/connector pairs, six reversed-role rows, all nine local variant
rows, and 19 strict trade plus 19 strict battle entrypoints. Structural and
declaration checks passed; matrix runtime was `NOT RUN` because the standalone
audit is collection-only.

The PR #19 focused transport/MCP slice passed 167/167, the bounded concurrency
probe passed 8/8 in each of five repetitions, and the post-change real-ROM
remote transport slice passed 15/15. The PR #17 complete source-runtime
baseline passed local 47/47, strict trade 19/19, and strict battle 19/19;
those full strict rows were not silently relabeled as a PR #19 rerun.
Historical 18/19 trade and 17/19 battle snapshots are retained only as
historical context.

A fresh Windows Python 3.12.10 environment passed editable installation,
`pip check`, source bootstrap, and the post-PR #23 pinned Cython build/check.
The post-PR #23 Cython runtime exposed `PyBoy.mb.serial` and passed direct
attach/tick/close smokes for canonical color Red, color Blue, and Yellow
(3/3). Earlier scoped source/fixture checks passed 23 tests with one unrelated
WSL-worktree skip, and MCP stdio integration passed 4/4. This narrows the
native-platform gap but does not complete strict Cython gameplay, real-ROM
concurrent load, the remote trade/battle matrix, or macOS coverage.

PR #22 adds bounded MCP stdio unpair cleanup and fresh remote lifecycle
generation tracking. Its release-hygiene workflow passed. PR #23 fixes native
lockstep timing; PR #26 fixes native `PyBoy.tick` instance ownership; PR #27
adds explicit runtime selection and raises game-driven remote exchange
timeouts to a bounded 30 seconds. The current source strict gates are 18/19
with isolated retries passing; the full native strict post-change gameplay
matrices remain open.

The integrated asset-free source and explicitly selected Cython gates use
managed Linux Python 3.12.13 and Pytest 9.1.1: collection 730, unit 586/586,
and timing 40/40 in each of five repetitions, with no skips, xfails, failures,
errors, or timeouts. The native gate reports all five required PyBoy modules as
extensions. The
pre-PR #27 source broad diagnostic remains incomplete: 418/703 tests completed
before a 5,400-second supervisor bound (386 passed, 20 skipped, 12 failed, and
285 not started). The failed cases were remote TCP timing cases from before
PR #27's 30-second game-exchange timeout; this is not a full-suite pass.

A fresh asset-free `python -m pytest -q -ra` run at the current head completed
566 passed, 140 expected BYO-asset skips, and 2 warnings. This is a completed
clean-checkout diagnostic, not a release gate; ROM-backed trade, battle, and
MCP cases were intentionally skipped because their external assets were absent.

**Release decision: `PARTIAL`.** The canonical color Red, color Blue, and
Yellow fixture bytes have recorded reproduction evidence, the Red fixture is
enabled in the remote diagnostic matrices, and the bounded producer plus
tracked battle-fixture generator are present. The source strict gates each
recorded 18/19 under parallel execution before their isolated
Yellow-to-Yellow retries; full native strict qualification is still open. Full
sign-off still requires a clean conservative-worker matrix, a completed broad suite,
vanilla source provenance, full native-platform evidence, real-ROM load
evidence, independent review, and secure cross-host networking.

## Source and artifact hygiene

- [x] The latest implementation is identified as merged implementation commit
  `b2a4016` (PR #27, merge `b96ee77`, with PR #26 native ABI and PR #22
  lifecycle hardening), and the documentation worktree is isolated from the
  protected dirty development checkout.
- [x] The checkout is source-only: ROMs, symbols, save states, screenshots,
  logs, caches, and virtual environments are not tracked.
- [x] Documentation keeps BYO assets and external evidence outside the source
  tree and uses relative paths or placeholders rather than machine paths.
- [x] Candidate evidence runs use isolated worktrees and record the exact
  runtime, asset hashes, deadlines, and outcome in the README and runbook;
  external evidence remains outside the source tree.

## Runtime and dependency identity

- [x] Python requirement is `>=3.12`.
- [x] The distribution bundles source PyBoy `2.7.0` at fork revision
  `c565df66c3731fad2856169a90f6bbec99925915`.
- [x] `mcp==1.29.1` is pinned in `pyproject.toml`.
- [x] The asset-free source gate resolves the bundled source runtime and the
  bit-accurate serial contract; the native gate resolves the same contract
  through explicitly selected Cython extensions.
- [x] The audited environment runs `python -m pip check` and records the
  interpreter/runtime identity from the same environment used by MCP.
- [x] Cython/native-accelerator mode builds and passes its explicit semantic
  serial contract and canonical three-ROM attach/step/close smoke.
- [x] A fresh Windows environment passes install, dependency, source/Cython
  bootstrap, and post-PR #23 canonical three-ROM Cython lifecycle checks;
  earlier scoped source/MCP checks passed with one unrelated WSL-worktree skip
  and MCP stdio 4/4.
- [ ] The integrated Cython build passes the full real-ROM strict gameplay
  matrix; its unit/timing, remote 15/15, and local/session 47/47 tiers pass.
  Exact native Red-to-Red trade/battle and Blue Color-to-Red Color trade rows
  pass, but the full native matrix remains open.
- [ ] Full Cython trade/battle acceptance is complete; source mode remains the
  documented release default while strict acceptance is unqualified.

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
- [x] The historical PR #17 full gate with those assets recorded exact hashes,
  sizes, deadlines, skips, xfails, failures, errors, and bounded diagnostics in
  a sanitized external evidence bundle; this does not certify the current head.

## Test gates

- [x] Both module and console-script collection paths complete in the
  integrated gate; it collected 730 tests with no collection errors.
- [x] ROM-free unit tests pass 586/586 in the integrated source and Cython
  gates (the complete PR #17 baseline passed 554/554).
- [x] Timing tests pass 40/40 in each of five repetitions in the PR #19
  follow-up gate; the post-PR #23 gate also passed 40/40 × 5.
- [x] The explicit production-file Ruff boundary is clean; broad legacy files
  outside that boundary are not used as release evidence.
- [ ] `python -m pytest -q -ra` completes with no unexpected failure, skip,
  xfail, or timeout on the merged head. The pre-PR #27 bounded source
  diagnostic completed 418/703 tests before its 5,400-second supervisor bound:
  386 passed, 20 skipped, 12 failed, and 285 were not started.
- [x] The PR #17 baseline asset-backed local/session tier passes 47/47 for the
  pinned advertised ROM inputs, with no fixture or runtime skips.
- [x] The PR #17 baseline controlled local evidence covers the complete
  canonical strict trade and battle matrix: 9/9 local ordered rows plus the
  dedicated Red/Yellow assertions passed for each operation.
- [x] The PR #19 post-change remote transport/MCP slice passes 15/15; the PR
  #17 baseline strict trade and battle remote rows passed 9/9 each.
- [x] The strict acceptance declaration has an entrypoint for every canonical
  ordered local and remote Red/Blue/Yellow pair (19 trade and 19 battle node
  IDs).
- [x] The PR #17 baseline local and remote strict runtime rows pass in both
  listener/connector directions; strict trade and strict battle each passed
  19/19 with bounded teardown.
- [ ] A clean full strict rerun of the integrated candidate remains open. The
  source trade and battle gates each reached 18/19 under bounded parallel
  execution; the failed remote Yellow-to-Yellow selectors passed isolated
  retries. Native full strict trade/battle matrices are not yet recorded.
- [x] The PR #19 bounded localhost concurrency/lifecycle probe passes 8/8 in
  each of five repetitions.
- [ ] Real-ROM concurrent-load stability is complete on the merged runtime.
- [ ] Full native-platform/build coverage and an independent release review
  are complete; the current Windows evidence is scoped and macOS remains
  untested.

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
  --runtime-mode source \
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
