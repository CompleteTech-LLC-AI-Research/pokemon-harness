# Release checklist

Use this checklist for a proposed `pokered-harness` release. A source pin,
fixture hash, or passing unit test is not a substitute for the real-ROM and
runtime evidence required by the capability being advertised.

## Status semantics

- `PASS` is scoped to one named command or tier. It requires clean collection
  and no failure, error, skip, xfail, or timeout in that scope.
- `PARTIAL` means that controlled evidence exists but one or more release
  conditions remain open.
- `PENDING` means that a declared gate is still running or lacks an accepted
  terminal artifact; it does not count as `PASS`.
- `PRODUCTION-READY` requires a clean candidate, all required BYO assets, a
  complete strict acceptance declaration and runtime matrix, retained evidence,
  and no open blocker.

## Current audit snapshot

The current public documentation head for this checklist is
`f048870bdbd4837b5494006ae49fe40510832a89` (2026-09-04). The last verified
pre-fix implementation/runtime snapshot under audit is
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7`; public changes after that snapshot
include the targeted network fix and documentation, so strict evidence below is
pre-fix unless explicitly labeled otherwise. The earlier merged implementation baseline was
`6d541b7867e82fa548c456008e6acd7fb1071586` (PR #42, 2026-09-04), and the
prior merged head was `71ca834d673c52eb74044089e92b09e7e3ae00a0`. No
production sign-off is claimed here; the release gate remains `PARTIAL`.

The exact prior-candidate evidence at
`3399407aa04f6e5e496df628442597c03e0adcc` recorded `1,094` tests collected in
each source/native unit gate, with `949` unit tests passing. Timing passed
`50/50` in each of five repetitions in both runtimes. This is historical,
asset-independent unit/timing evidence only; it is not a current-head
ROM-backed gameplay sign-off.

The prior-candidate asset checks recorded `5/5` ROM hashes, `3/3` symbol
hashes, `3/3` fixture hashes, and the 10-entry fixture manifest. These are
historical external-BYO-asset checks; matching hashes do not establish fixture
provenance or gameplay success.

The prior-candidate asset-backed local real-ROM tier passed source `47/47` in
`531.3477s` and native `47/47` in `38.2974s`. Its remote real-ROM tier passed
source `16/16` in `35.6044s` and native `16/16` in `30.8627s`. These are
historical scoped runtime results, not current-head strict gameplay evidence.

Fixture provenance remains partial: six canonical entries are verified, while
four vanilla-derived entries remain `PARTIAL` because their vanilla source
provenance is not established.

Static provenance review confirms that the documented stock ROM/SYM pins and
the existing vanilla fixture bytes validate. Vanilla ordinary capture
provenance cannot be established: replay against the retained source failed at
the 64-step bound, and the manifest's ordinary producer revision is historical
(`25e231c`). Those fixture bytes must not be described as reproducible from the
audited code snapshot.

At the last verified implementation/runtime snapshot
(`a8576b5ecb8e7039eefe0e02865b0bfc031387a7`), source-local strict battle
acceptance is `PASS`: all 9/9 ordered canonical Red/Blue/Yellow pairs reached
Link Battle and move/damage hooks. The fresh rows were Yellow-Red (`182.89s`),
Blue-Yellow (`443.50s`), and Yellow-Blue (`480.31s`); six prior same-campaign
rows complete the 9/9 set.

The retained native/Cython strict real-ROM artifact
`native-strict-evidence-a8576b5` (external to the checkout; its
`gate-report.txt` and `gate-report.json` are retained) used Python `3.12.13`,
PyBoy `2.7.0` fork
`c565df66c3731fad2856169a90f6bbec99925915`, and the bit-accurate native
serial contract. It declared and executed 19/19 entrypoints in each tier, but
the gate is `FAIL`: trade passed 16/19 and battle passed 17/19. Trade failed
for `blue_color` listener -> `yellow` connector at `83.39s` after trade hooks
but with an incorrect/malformed party record; `red_color` listener -> `yellow`
connector at `729.51s` on a LinkMenu rendezvous timeout after `2,197`
balanced/applied edges with one pending request; and `yellow` listener ->
`blue_color` connector at `721.39s` on a LinkMenu rendezvous timeout after
`6,248` balanced edges. Battle failed for `blue_color` listener -> `yellow`
connector at `376.94s` when sync marker `113` did not converge after `18,840`
applied edges, and `yellow` listener -> `blue_color` connector at `142.20s`
when the battle Colosseum warp rendezvous did not converge after `952`
balanced edges. No native owner/IRQ errors were recorded; these are strict
real-ROM outcomes, not bypasses.

A targeted post-snapshot integrated code fix is recorded as
`7b4b5b72ad373d2293d51e3d11717314606ee442` (cherry-picked as `7b4b5b7`). It
moves stale `EDGE_RESP` closure outside `_edge_response_lock` and adds a
bounded regression test. The focused post-fix network suite passes `38/38`,
with Ruff and formatting clean. This is targeted network/regression evidence
only: the retained native strict gate above is pre-fix at code snapshot
`a8576b5`, and the source-local strict battle result above is also pre-fix; the
post-fix source strict-trade result is recorded below. The focused suite does
not change the native trade `16/19`, native battle `17/19`, or source-trade
acceptance boundaries.

The post-fix serialized source strict-trade production gate is retained at
`/tmp/poke-harness-source-serial-acceptance-20260904/evidence` and ran after
code fix `7b4b5b72ad373d2293d51e3d11717314606ee442` (cherry-picked as
`7b4b5b7`). It used source Python `3.12.13`, PyBoy `2.7.0` fork
`c565df66c3731fad2856169a90f6bbec99925915`, the bit-accurate serial contract,
and `matrix-workers=1`. The complete matrix audit collected `1,107` tests;
the trade tier declared and executed `19/19` entrypoints. Its overall result
is `FAIL`: `18/19` passed, `1/19` failed, `0` skipped, `0` errors, `0` xfail,
and `0` xpass, with duration `2788.895s`. The sole failure was
`tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_completes_trade_over_tcp[blue_color-listen-blue_color-connect]`
at `721.4846s`: the trade-center warp rendezvous did not converge. Its
pre-close stats recorded `984` inbound/applied edges, `123` IRQ callbacks,
zero owner-edge errors, and no pending edge requests. The evidence hashes are
`gate-report.json=7de82390f52bdd4b8e3d569204ef8f3951113a8c1706f403e53a6f681e0db453`,
`gate-report.txt=90e5b17706ba87a095a7fa32c317d84d957e2c76550a7d06f2a38720ab451a97`,
and
`evidence-manifest.json=c6f07eb29dbd272ead265b628bd2d2c41e287ec02ab1229db90716ff95dde45a`.
This is a complete post-fix source-runtime trade artifact, not a passing gate;
it does not upgrade the retained native/Cython strict gate, which remains
pre-fix at `a8576b5` with trade `16/19` and battle `17/19`, or the source-local
strict battle result, which remains pre-fix at `a8576b5` with `9/9`.

The integrated post-fix candidate's asset-free full suite passed 966 tests,
with 141 explicit BYO-asset skips and one SDL warning. This is regression
evidence for the package/test surface only; it does not qualify the skipped
ROM-backed tiers.

The post-fix source strict-trade gate is a terminal `FAIL` at `18/19`, with the
sole failure and retained evidence recorded above. A LinkMenu or transport
milestone is not a completed trade or battle. Actual MCP-driven gameplay is
only partially scoped: public calls reached Red's bedroom, the house exit,
Pallet Town, Oak's Lab, and lab movement, but a bounded starter attempt ended
at map `40`, position `(5,3)`, with `party.count=0`; no bypass was used, so MCP
starter/trade/battle remains unproven.

**Release decision: `PARTIAL`.** Sign-off remains open for the failing post-fix
source strict-trade gate, the failing pre-fix native strict trade/battle
matrix, the current remote release matrix, vanilla fixture provenance,
clean-install and broad-suite coverage, platform and real-ROM load coverage,
actual MCP starter/trade/battle gameplay, independent review, and secure
networking.

The acceptance update merged in PR #42 recorded source-runtime representative
in-process and TCP Red-color/Yellow trade and battle checks, the source-local
strict battle matrix at 9/9, 954/954 unit tests, and 50/50 timing cases in each
of five repetitions. Its scoped MCP suite passed 6/6 real integration checks
in `13.11s` with one SDL warning, and 104 dispatch tests passed. This is
scoped candidate evidence, not full strict trade, native-runtime, current
remote-matrix, or MCP starter/trade/battle sign-off, so it does not change the
release decision.

## Change summary

The state-validity, transport, and release-gate hardening represented by the
audited code snapshot, plus the targeted post-snapshot `EDGE_RESP` closure
fix, are additive: they improve classification, lifecycle behavior, and
observability but do not themselves establish MCP gameplay, trade, battle, or
release readiness.

## Source and artifact hygiene

- [x] The current public documentation head is identified as
  `f048870bdbd4837b5494006ae49fe40510832a89`, and the last verified
  implementation/runtime snapshot under audit is
  `a8576b5ecb8e7039eefe0e02865b0bfc031387a7`; this checklist is maintained in
  an isolated worktree separate from the protected dirty development checkout.
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
- [x] The recorded Cython/native-accelerator check builds and passes its explicit semantic
  serial contract and canonical three-ROM attach/step/close smoke.
- [x] Historical Windows validation passed install, dependency, source/Cython
  bootstrap, and post-PR #23 canonical three-ROM Cython lifecycle checks;
  earlier scoped source/MCP checks passed with one unrelated WSL-worktree skip
  and MCP stdio 4/4. This is scoped build/runtime evidence, not current-head
  full native gameplay evidence.
- [ ] The native/Cython strict real-ROM gate is complete but does not pass: at
  code snapshot `a8576b5ecb8e7039eefe0e02865b0bfc031387a7`, all 19/19
  entrypoints were declared and executed in each tier, with trade at 16/19 and
  battle at 17/19. The failure details and absence of native owner/IRQ errors
  are recorded above; these are strict real-ROM outcomes, not bypasses.
- [ ] Full source/native trade and battle acceptance is complete. Source-local
  strict battle remains pre-fix `PASS` at 9/9, the post-fix source strict-trade
  result is a terminal `FAIL` at 18/19, and the retained native strict trade
  and battle results remain pre-fix at 16/19 and 17/19. Unit, timing,
  transport, and LinkMenu results do not substitute for gameplay acceptance.

## ROM, symbol, and BYO asset identity

- [x] `VERSIONS.md` records SHA-1 pins for stock/color Red, stock/color Blue,
  Yellow, and the three matching symbol files.
- [ ] The operator attests that every supplied ROM and symbol file is legally
  obtained and matches the documented pin.
- [x] Release commands require explicit `POKERED_ROM_SHA1` and do not use
  `POKERED_SKIP_SHA1=1`.
- [x] Static provenance review confirms the documented stock ROM/SYM pins and
  the existing vanilla fixture bytes validate; these checks do not certify
  vanilla ordinary capture provenance, reproducibility from the audited code
  snapshot, or gameplay.
- [x] The historical PR #17 full gate with those assets recorded exact hashes,
  sizes, deadlines, skips, xfails, failures, errors, and bounded diagnostics in
  a sanitized external evidence bundle; this does not certify the current head.

## Test gates

- [x] Both module and console-script collection paths complete for the recorded
  gate scopes without collection errors.
- [x] Historical scoped unit evidence records 586/586 in integrated source and
  Cython gates (the complete PR #17 baseline passed 554/554).
- [ ] A separate current source/native unit gate has not been independently
  rerun. The earlier documented source gate records `954/954` unit tests and
  timing `50/50` in each of five repetitions; the exact prior-candidate evidence
  recorded `1,094` tests collected in each gate, `949` unit tests passing, and
  timing `50/50` in each of five repetitions in both runtimes. Neither scoped
  unit result establishes ROM-backed native gameplay.
- [ ] A fresh standard-library virtual environment and editable install have
  not been independently verified on this host; the system `python3` lacks
  `ensurepip`, while the passing gates used existing managed environments.
- [x] Historical timing tests pass 40/40 in each of five repetitions in the
  PR #19 follow-up gate and the post-PR #23 gate; the prior-candidate timing
  result is recorded above as 50/50 × 5.
- [x] The targeted post-snapshot `EDGE_RESP` closure fix
  (`7b4b5b72ad373d2293d51e3d11717314606ee442`, cherry-picked as `7b4b5b7`)
  passes the focused post-fix network suite at 38/38, with Ruff and formatting
  clean. This is scoped regression/network evidence and does not rerun or
  upgrade strict real-ROM acceptance.
- [x] The explicit production-file Ruff boundary is clean; broad legacy files
  outside that boundary are not used as release evidence.
- [ ] `python -m pytest -q -ra` completes with no unexpected failure, skip,
  xfail, or timeout on the audited implementation snapshot. The pre-PR #27
  bounded source diagnostic completed 418/703 tests before its 5,400-second
  supervisor bound: 386 passed, 20 skipped, 12 failed, and 285 were not
  started.
- [x] The historical PR #17 asset-backed local/session tier passes 47/47 for
  the pinned advertised ROM inputs, with no fixture or runtime skips.
- [x] The PR #17 baseline controlled local evidence covers the complete
  canonical strict trade and battle matrix: 9/9 local ordered rows plus the
  dedicated Red/Yellow assertions passed for each operation.
- [x] The historical PR #19 post-change remote transport/MCP slice passes 15/15; the PR
  #17 baseline strict trade and battle remote rows passed 9/9 each.
- [ ] A complete current local real-ROM trade/battle release tier has not been
  established. The post-fix source strict-trade gate is a terminal `FAIL` at
  `18/19`, with all `19/19` trade entrypoints declared and executed; its sole
  failure and evidence hashes are recorded in the Current audit snapshot. The
  source-local strict battle sub-tier remains pre-fix at code snapshot
  `a8576b5ecb8e7039eefe0e02865b0bfc031387a7` and is `PASS` at 9/9 ordered
  canonical pairs, including fresh Yellow-Red (`182.89s`), Blue-Yellow
  (`443.50s`), and Yellow-Blue (`480.31s`) rows plus six prior same-campaign
  rows; all reached Link Battle and move/damage hooks. The prior-candidate
  asset-backed evidence passed source `47/47` and native `47/47`; neither
  result qualifies full current trade/native acceptance.
- [ ] A current remote real-ROM strict tier is recorded but is not a passing
  release gate. The retained pre-fix native/Cython artifact executed all 19/19
  declared trade and battle entrypoints: trade passed 16/19 and battle passed
  17/19, with the exact failures recorded in the Current audit snapshot.
  Historical asset-backed remote evidence passed source `16/16` and native
  `16/16`; that scoped result does not qualify current strict gameplay
  acceptance.
- [x] The strict acceptance declaration has an entrypoint for every canonical
  ordered local and remote Red/Blue/Yellow pair (19 trade and 19 battle node
  IDs).
- [x] The historical PR #17 baseline local and remote strict runtime rows pass in both
  listener/connector directions; strict trade and strict battle each passed
  19/19 with bounded teardown.
- [ ] A clean current strict trade and battle sign-off is complete. Source-local
  strict battle remains pre-fix `PASS` at 9/9, the post-fix source strict-trade
  gate is `FAIL` at 18/19, and the retained native/Cython strict gate remains
  pre-fix at 16/19 for trade and 17/19 for battle; historical strict results
  do not qualify those current gates.
- [x] The historical PR #19 bounded localhost concurrency/lifecycle probe passes 8/8 in
  each of five repetitions.
- [ ] Real-ROM concurrent-load stability is complete on the current runtime;
  platform and load evidence remain open.
- [ ] Full native-platform/build coverage and an independent release review
  are complete; the current Windows evidence is scoped and macOS remains
  untested.

## Fixture provenance and generation

- [x] `release-evidence/fixture-manifest.json` records ten external state
  entries with sizes, SHA-1/SHA-256 values, expected ROM/SYM pins, source-state
  records, runtime identity, and command templates.
- [ ] Deterministic reproduction of the four vanilla-derived fixture bytes is
  established. Static validation confirms the stock ROM/SYM
  pins and existing vanilla fixture bytes, but vanilla ordinary capture
  provenance cannot be established: replay against the retained source failed
  at the 64-step bound, and the manifest's ordinary producer revision is
  historical (`25e231c`).
- [x] `scripts/produce_cable_club_fixture.py` validates pins and has bounded
  defaults of 180 seconds and 64 movement steps.
- [x] `scripts/prepare_battle_cable_club_fixtures.py` is tracked and produces
  immutable derived battle fixtures; acceptance does not prepare party state
  in emulator RAM.
- [x] Provenance is verified for six canonical fixture entries.
- [ ] Provenance for the four vanilla-derived entries is verified. Their
  vanilla source states are not proven to match the vanilla ROM; replay against
  the retained source failed at the 64-step bound, and the manifest's ordinary
  producer revision is historical (`25e231c`), so those rows remain `PARTIAL`.
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
- [ ] Cross-host TCP is supported securely; the current transport is
  loopback-only, unauthenticated, and unencrypted.
- [ ] Actual MCP-driven gameplay is complete and independently verified. Unit,
  transport, lifecycle, and LinkMenu evidence do not prove MCP-driven starter,
  trade, or battle. Scoped MCP evidence is 6/6 real integration checks in
  `13.11s` with one SDL warning, plus 104 passing dispatch tests;
  public calls reached Red's bedroom, the house exit, Pallet Town, Oak's Lab,
  and lab movement, but the bounded starter attempt ended at map `40`,
  position `(5,3)`, with `party.count=0`, without a bypass.

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
repeat the full command with `--runtime-mode cython`, or use `--runtime-mode
both` with `--python` set to the source interpreter and `--cython-python` set to
the native interpreter. Keep the sanitized evidence bundle outside version
control.

Do not describe a skipped, xfailed, timed-out, synthetic, hook-only,
RAM-mutated, or LinkMenu-only result as a completed trade or battle.
