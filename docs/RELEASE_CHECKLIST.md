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

The current integration candidate is based on
`d8060be232dbbb75798fa1ff93e49c2f84747b9f` (2026-09-03) plus uncommitted
runtime, packaging, lifecycle, and test changes. It remains a `PARTIAL`
publication candidate; the base commit and this checklist do not certify the
uncommitted worktree.

### Current candidate result

The latest bounded candidate unit gate used the bundled source PyBoy runtime.
It passed 360/360 unit tests and 35/35 timing tests in each of five repetitions;
asset preflight matched all five pinned ROMs, three symbol files, and six
required ordinary/battle Cable Club fixtures. This is scoped evidence, not release sign-off:
the strict trade gate timed out in its remote Red-color↔Blue-color row at the
600-second bound, and no current battle result is recorded. All older counts in
this checklist are explicitly historical or scoped evidence and must not be
copied into a current release report.

A separate bounded remote diagnostic completed one Blue-color↔Blue-color trade
row (`1/1`) with balanced `6,528` serial edges per direction and no unknown
opcodes. Both peers used the same fixture, so it is diagnostic evidence only;
it does not satisfy strict party-swap acceptance or the full matrix.

Before changing any checkbox below, rerun the relevant command against the
exact candidate commit from a clean install using Python `>=3.11`, the pinned
`mcp==1.29.1`, and the bundled PyBoy runtime. Record the command, interpreter,
runtime mode, asset hashes, deadlines, and teardown outcome.

### Historical evidence retained for comparison

The complete all-tier source-runtime baseline ran from isolated source head
`df0e7424c87c812a57f257286b0dc00e87c498f4`, whose implementation tree is the
PR #17 parent. Its historical gate invocation and sanitized report are kept
outside this checkout; that older CLI had options not present in the current
`scripts/production_gate.py`.

It returned `PASS`: collection 698, unit 554/554, local real-ROM 47/47,
remote transport/MCP 15/15, strict trade 19/19, strict battle 19/19, and
timing 40/40 in each of five repetitions. All ten external fixture-manifest
entries validated; no selected test skipped, xfailed, failed, errored, or
timed out. The sanitized evidence bundle is retained outside version control
because ROMs, symbols, and save states are external BYO assets.

The current clean asset-free command is:

```bash
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --unit-only \
  --repeat-timing 5 \
  --format text
```

Repeat the same command with `--runtime-mode cython` and the separately
bootstrapped Cython interpreter if native qualification is in scope. The
current CLI accepts one interpreter and one runtime mode per invocation; retain
separate reports for separate environments.

At a dated prior separate-interpreter dual-gate check, source (`--python`)
and Cython (`--cython-python`) each collected 771 tests, passed unit 626/626, and
passed timing 50/50 in each of five repetitions. Source reported
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
and Cython serial-link+network scope passes 78/78 in each runtime.
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
declaration checks passed; those collection counts are not gameplay results.

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

The dated prior separate-interpreter dual-gate evidence uses managed Linux
Python 3.12.13 and Pytest 9.1.1: collection 771 in each mode, unit 626/626, and
timing 50/50 in each of five repetitions. The native probe/build and current
focused 78/78 serial-link+network scope are separate scoped checks. These gates do
not establish ROM-backed gameplay coverage.

The current post-hardening asset-backed evidence used pinned ROM/SYM/fixtures
and separate source/native interpreters: local 47/47 source/native
(758.1s/38.1s) and remote 16/16 source/native (75.0s/28.8s). This does not
satisfy the strict trade/battle matrix, provenance, security, platform, or
clean-checkout release items.

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

- [ ] The current candidate is a clean release commit. It is based on
  `d8060be232dbbb75798fa1ff93e49c2f84747b9f` plus uncommitted serial,
  bootstrap, lifecycle, gate-accounting, cleanup, and packaging changes; it
  must be committed and rerun before release evidence can be signed off.
- [x] The checkout is source-only: ROMs, symbols, save states, screenshots,
  logs, caches, and virtual environments are not tracked.
- [x] Documentation keeps BYO assets and external evidence outside the source
  tree and uses relative paths or placeholders rather than machine paths.
- [x] Candidate evidence runs use isolated worktrees and record the exact
  runtime, asset hashes, deadlines, and outcome in the README and runbook;
  external evidence remains outside the source tree.

## Runtime and dependency identity

- [x] Python requirement is `>=3.11`.
- [x] The distribution bundles source PyBoy `2.7.0` at fork revision
  `c565df66c3731fad2856169a90f6bbec99925915`.
- [x] `mcp==1.29.1` is pinned in `pyproject.toml`.
- [x] The bundled source runtime resolves and exposes the bit-accurate serial
  contract; the latest asset-free unit gate passed 360/360.
- [ ] The current audited environment passes its dependency check (`uv pip
  check --python <interpreter>` or `python -m pip check`) with the pinned
  `mcp==1.29.1` environment used by MCP.
- [ ] The current Cython/native build and canonical three-ROM lifecycle smoke
  have not been rerun for this candidate.
- [x] Historical Windows install, bootstrap, MCP, and Cython lifecycle checks
  are retained below as scoped evidence; they do not certify this candidate.
- [ ] The integrated Cython build passes the full real-ROM strict gameplay
  matrix. Its focused serial-link+network scope passes 78/78. The historical native
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
  ordinary Cable Club fixtures. It does not validate the operator-managed
  ten-entry manifest or battle-fixture hashes; those remain a separate release
  evidence task.
- [x] The historical PR #17 full gate with those assets recorded exact hashes,
  sizes, deadlines, skips, xfails, failures, errors, and bounded diagnostics in
  a sanitized external evidence bundle; this does not certify the current head.

## Test gates

- [x] The current asset-free gate collected and passed 360/360 unit tests with
  no collection error; timing passed 35/35 in each of five repetitions.
- [x] Historical scoped unit evidence records 586/586 in integrated source and
  Cython gates (the complete PR #17 baseline passed 554/554).
- [x] The current source timing tier passes 35/35 in each of five repetitions;
  the historical separate-interpreter dual-gate result remains traceability
  evidence only.
- [ ] A fresh standard-library virtual environment and editable install have
  not been independently verified on this host; the system `python3` lacks
  `ensurepip`, while the passing gates used existing managed environments.
- [x] Timing tests pass 40/40 in each of five repetitions in the PR #19
  follow-up gate; the post-PR #23 gate also passed 40/40 × 5.
- [ ] A reproducible Ruff command with an explicit production-file boundary is
  clean. This checkout has no release workflow or checked-in path allowlist;
  broad scans include legacy files and are not release evidence.
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
  a prior source trade gate recorded 19/19; the historical native strict-trade
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

- [ ] An operator-managed fixture manifest (kept outside this source tree)
  records every external state entry with sizes, SHA-1/SHA-256 values,
  expected ROM/SYM pins, source-state records, runtime identity, and command
  templates for the current candidate.
- [x] Canonical color-Red, color-Blue, and Yellow ordinary and derived battle
  fixture bytes have deterministic reproduction evidence.
- [ ] `scripts/produce_cable_club_fixture.py` accepts explicit version/variant,
  source, ROM, symbol, and output paths, but it has no built-in hash or timeout
  validation. Operators must validate pins and bound the diagnostic externally
  before retaining a fixture for release evidence.
- [x] `scripts/prepare_battle_cable_club_fixtures.py` is tracked and produces
  immutable derived battle fixtures; acceptance does not prepare party state
  in emulator RAM.
- [ ] Vanilla ordinary source provenance is verified. The retained Red/Blue
  vanilla source states are not proven to match the vanilla ROM, so vanilla
  ordinary and derived battle rows remain `PARTIAL`.
- [ ] The operator validates all ten external manifest entries against the
  supplied fixture files and keeps the external fixture root available to the
  release runner. This repository does not ship a manifest-validator script.
- [ ] A retained evidence bundle includes the exact source-state hashes,
  capture/generation commands, runtime identity, and original-vs-verification
  timestamp distinction.

## MCP and network operation

- [x] MCP launch examples use explicit ROM and symbol paths plus the ROM
  SHA-1 pin; symbol-file hashes remain an operator verification task.
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
  --modes trade battle \
  --deadline-seconds 210 \
  --outdir "$(mktemp -d)"
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --rom-root "$PWD/rom" \
  --fixture-root "$PWD/tests/fixtures/link" \
  --python "$(command -v python)" \
  --repeat-timing 5 \
  --format text
```

The production-gate command above uses one explicit interpreter and writes its
report to stdout. Repeat it with `--runtime-mode cython` and a separately
bootstrapped Cython interpreter if native qualification is in scope; there is
no dual-runtime switch.

The matrix command executes real subprocess pairs and its manifest is
diagnostic evidence, not a collection-only declaration. The asset-free gate
may return `PASS` without ROMs because `--unit-only` selects only unit and
timing. A full gate must return `PASS` only after assets, strict matrix,
fixture, and all required real-ROM tiers pass. Redirect stdout to a report
outside version control when retaining evidence.

Do not describe a skipped, xfailed, timed-out, synthetic, hook-only,
RAM-mutated, or LinkMenu-only result as a completed trade or battle.
