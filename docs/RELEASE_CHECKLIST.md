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

The current merged head for this candidate is
`daa1d72f2cc559a6424067a9088dfae1b7b5f7bb` (2026-09-03). Earlier PR #35
introduced remote serial-edge dispatch at an explicit native instruction-batch
boundary. The candidate also includes serial save-state restoration, native
bootstrap ownership and build-metadata cleanup, bounded MCP teardown,
fail-closed gate accounting, and partial-initialization cleanup. This remains a
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

The clean asset-free source command remains:

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

The corresponding native check uses the same command from the separately
bootstrapped Cython environment with `--runtime-mode cython`. For a
separate-interpreter dual gate, pass the source environment as `--python` and
the Cython environment as `--cython-python`:

```bash
SOURCE_PYTHON="$PWD/.venv/bin/python"
CYTHON_PYTHON="$PWD/.venv-cython/bin/python"
EVIDENCE_DIR="$(mktemp -d)"
"$SOURCE_PYTHON" scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$SOURCE_PYTHON" \
  --cython-python "$CYTHON_PYTHON" \
  --runtime-mode both \
  --unit-only \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

`--runtime-mode both` (with `dual` as an alias) runs the selected tiers under
both explicit runtimes. `--python` selects the source interpreter and
`--cython-python` selects the Cython interpreter; if the latter is omitted it
defaults to `--python`, and it is valid only with `--runtime-mode both`. The
gate does not create either environment. Keep the two install records and
their gate evidence separate for release sign-off.

At the separate-interpreter dual-gate evidence point (`b1134c4`), before the
later mapping-test additions, source (`--python`) and Cython (`--cython-python`)
each collected 753 tests, passed unit 608/608, and
passed timing 40/40 in each of five repetitions. Source reported
`python-source`; Cython reported `cython/native-extension`. The clone
contained no ROM, symbol, or save-state assets, so no ROM-backed tier ran and
the check is not a production sign-off. Both uv-managed environments passed
`uv pip check --python <interpreter>`, and native bootstrap verified the
`pyboy` and `pokered-harness` owners. An earlier environment-specific 585/586
ownership result is superseded for these isolated environments. The host's
bare `python3` still lacks `ensurepip`, so that alternate standard-library venv
path remains open.

After the pinned fork is built with `scripts/bootstrap_pyboy.py --mode cython`,
the optional native path can be checked explicitly. The current focused source
and Cython transport/serial/PyBoy-link suite passes 110/110 in each runtime.
An asset-backed source trade gate recorded 19/19 ordered rows. The historical
native strict-trade gate recorded 18/19; exact-row follow-ups have both passed
and failed, including exact party-record exchange, a party-record mismatch,
and phase stalls, so native strict-trade reliability is unproven. The full
current native battle matrix is not qualified. The source run's supervisor
started before PR #35 was published, so it is useful current evidence but not a
clean post-merge all-tier sign-off. The latest acceptance lane exercised 3/9
direct native remote battle rows under a bounded 155-second pair deadline:
`Blue-color↔Blue-color` passed; `Red-color↔Yellow` and
`Yellow↔Red-color` failed. No bypasses were used; six rows remain unrun, and
the full 19-entrypoint battle set remains unqualified.

Prior integrated source and Cython remote tiers pass 15/15, and the prior
integrated Cython local/session tier passes 47/47. These are scoped follow-ups,
not current full native trade/battle acceptance.

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
timeouts to a bounded 30 seconds. PR #35 moves remote serial-edge dispatch to
the native instruction-batch boundary. The historical native strict-trade
result is 18/19, but exact-row follow-ups have both passed and failed,
including a party-record mismatch and phase stalls, so reliability remains
unproven. Native battle remains unqualified.

The separate-interpreter dual-gate evidence uses managed Linux Python 3.12.13
and Pytest 9.1.1: collection 753 in each mode, unit 608/608, and
timing 40/40 in each of five repetitions. The native probe/build and current
focused 110/110 serial-link suite are separate scoped checks. These gates do
not establish ROM-backed gameplay coverage.

The pre-PR #27 source broad diagnostic remains incomplete: 418/703 tests
completed before a 5,400-second supervisor bound (386 passed, 20 skipped, 12
failed, and 285 not started). The failed cases were remote TCP timing cases
from before PR #27's 30-second game-exchange timeout; this is not a full-suite
pass.

An earlier asset-free `python -m pytest -q -ra` diagnostic completed 604 passed,
141 expected BYO-asset skips, and one SDL warning before the dual-runtime gate
tests were added. This is a completed clean-checkout diagnostic, not a release
gate; ROM-backed trade, battle, and MCP cases were intentionally skipped
because their external assets were absent.

**Release decision: `PARTIAL`.** The canonical color Red, color Blue, and
Yellow fixture bytes have recorded reproduction evidence, the bounded producer
and tracked battle-fixture generator are present, and the source trade gate
recorded 19/19. The historical native strict-trade result is 18/19, but
exact-row follow-ups have both passed and failed, including a party-record
mismatch and phase stalls, so native reliability is unproven; native battle
remains unqualified: the latest direct native remote battle lane exercised
only 3/9 rows (one pass and two bounded failures), leaving six unrun and the
full 19-entrypoint set open. Full sign-off still requires reproducible clean-install
evidence for the documented source/native environments, reliable current
native trade and battle matrices, a complete current source battle matrix, a
completed broad suite, vanilla source provenance, full native-platform and
real-ROM load evidence, independent review, and secure cross-host networking.

## Source and artifact hygiene

- [x] The current merged head is identified as
  `daa1d72f2cc559a6424067a9088dfae1b7b5f7bb`; the candidate records the
  serial, bootstrap, lifecycle, gate-accounting, and cleanup changes, and the
  documentation worktree is isolated from the protected dirty development
  checkout.
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
- [x] The audited environment passes its dependency check (`uv pip check
  --python <interpreter>` for uv-managed environments, or `python -m pip check`
  when pip is installed) and records the interpreter/runtime identity from the
  same environment used by MCP.
- [x] Cython/native-accelerator mode builds and passes its explicit semantic
  serial contract and canonical three-ROM attach/step/close smoke.
- [x] A fresh Windows environment passes install, dependency, source/Cython
  bootstrap, and post-PR #23 canonical three-ROM Cython lifecycle checks;
  earlier scoped source/MCP checks passed with one unrelated WSL-worktree skip
  and MCP stdio 4/4.
- [ ] The integrated Cython build passes the full real-ROM strict gameplay
  matrix. Its focused serial-link suite passes 110/110. The historical native
  strict-trade result is 18/19, but exact-row follow-ups have both passed and
  failed, including a party-record mismatch and phase stalls, so reliability
  remains unproven. The latest direct native remote battle lane exercised 3/9
  rows under a bounded 155-second pair deadline: one passed and two failed
  without bypasses; six rows remain unrun and the full 19-entrypoint battle
  matrix is open.
- [ ] Full Cython trade/battle acceptance is complete; source mode remains the
  documented release default while strict acceptance is unqualified.

## ROM, symbol, and BYO asset identity

- [x] `VERSIONS.md` records SHA-1 pins for stock/color Red, stock/color Blue,
  Yellow, and the three matching symbol files.
- [ ] The operator attests that every supplied ROM and symbol file is legally
  obtained and matches the documented pin.
- [x] Release commands require explicit `POKERED_ROM_SHA1` and do not use
  `POKERED_SKIP_SHA1=1`.
- [x] The full real-ROM gate preflights five ROMs, three symbols, and three
  ordinary Cable Club fixtures; a real-ROM acceptance run additionally
  validates all ten manifest entries, including canonical ordinary and battle
  states for color Red, color Blue, and Yellow.
- [x] The historical PR #17 full gate with those assets recorded exact hashes,
  sizes, deadlines, skips, xfails, failures, errors, and bounded diagnostics in
  a sanitized external evidence bundle; this does not certify the current head.

## Test gates

- [x] Both module and console-script collection paths complete; the recorded
  separate-interpreter source and Cython asset-free gates each collected 753 tests with no
  collection errors.
- [x] Historical scoped unit evidence records 586/586 in integrated source and
  Cython gates (the complete PR #17 baseline passed 554/554).
- [x] The recorded separate-interpreter dual gate passes 608/608 in both source
  and Cython modes, and timing passes 40/40 in each of five repetitions; the
  asset-free run did not exercise ROM-backed gameplay.
- [ ] A fresh standard-library virtual environment and editable install have
  not been independently verified on this host; the system `python3` lacks
  `ensurepip`, while the passing gates used existing managed environments.
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
  current source trade gate recorded 19/19; the historical native strict-trade
  result is 18/19, but exact-row follow-ups have both passed and failed,
  including a party-record mismatch and phase stalls, so reliability remains
  unproven. The latest direct native remote battle lane exercised 3/9 rows
  under a bounded 155-second pair deadline, with one pass and two failures;
  six rows remain unrun, and the current full source battle rerun plus the
  complete 19-entrypoint native strict battle matrix remain unqualified.
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

The production-gate command above is the source-runtime run. Repeat it after
`scripts/bootstrap_pyboy.py --mode cython --check` in the native environment,
changing `--runtime-mode source` to `--runtime-mode cython` and retaining a
separate evidence directory. To run the separate-interpreter dual gate, keep
the source path in `--python` and add the native path with `--cython-python`,
then select `--runtime-mode both`. If `--cython-python` is omitted, the gate
falls back to the `--python` interpreter; it does not create an environment.

The matrix command is collection-only and returns zero when its structural and
strict declaration checks pass. The asset-free gate may return `PASS`
without ROMs because it selects only unit and timing. A full gate must return
`PASS` only after assets, strict matrix, fixture, and all required real-ROM
tiers pass in each selected runtime. Because the CLI default is `source`,
repeat the full command with `--runtime-mode cython` (or use `both` only when
the one selected interpreter satisfies both runtime contracts). Keep the
sanitized evidence bundle outside version control.

Do not describe a skipped, xfailed, timed-out, synthetic, hook-only,
RAM-mutated, or LinkMenu-only result as a completed trade or battle.
