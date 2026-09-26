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

**Release decision: `PARTIAL`, not production-ready.** On 2026-09-11, the
resumed candidate at `3c96537` passed the isolated dual-runtime unit-only gate:
source and native/Cython each completed `4508/4508` unit tests and
`1910/1910` timing cases (five repetitions), with zero failures, skips,
xfails, xpasses, or errors. The gate report is retained outside the checkout as
`dual-unit-final-20260911/gate-report.txt` (SHA-256
`58e8e34efc28e4af0df6af742153a7b03eee1aeb8ca3cc94dac9e07dc11dcf2d`). It
also audited the declared `9/9` local and `9/9` remote matrix rows and the
`19/19` strict trade/battle entrypoints, but did not execute gameplay.

The same isolated environments passed six real-ROM MCP stdio/lifecycle
integration checks and four Red golden-path checks in each runtime. Stock Red,
Blue, and Yellow assets were available for the run. Both color ROMs and all
ten external link fixtures remain absent, so the validator and the full
asset-backed source/native trade and battle gate have not run. Those missing
inputs prevent production qualification; unit, timing, MCP lifecycle, and
golden-path evidence do not establish Cable Club gameplay. See
[`VERSIONS.md`](../VERSIONS.md#current-linux-isolated-runtime-evidence) for
the exact runtime and asset record.

### Historical baseline

At `1f19707`, the
dual source/native unit gate passed `1176/1176` unit tests and `65/65` total
timing checks per runtime (`13` cases in each of five repetitions), with zero
failures, skips, xfails, xpasses, or errors. Each runtime collected `1320`
tests and declared `19/19` trade and `19/19` battle entrypoints; neither
gameplay tier was executed by this unit-only gate. Retained evidence:
`pokemon-qualification-dual-unit-1f19707-20260905/gate-report.json`.

Canonical boot/state/hash checks at `1f19707` passed source `3/3` in `6.74s`
and native `3/3` in `1.89s`, with zero failures, skips, or errors and one SDL
warning per runtime. They step `120` frames and check state/save/load; they
are not gameplay acceptance. The fresh native build is documented in
`poke-serial-native-20260905-V7dHOU/QUALIFICATION.md`: its base is `e1686ca`
with serial overlays, and its serial input hash matches `1f19707`. Build
provenance and these scoped passes do not establish a clean full gameplay gate.

The native Blue-listener/Yellow-connector trade replay at `02a8e85` (runtime
identical to `1f19707`) failed after `721.67s`; both peers exited cleanly
with return code `1`. Blue recorded no LinkMenu entry and one
`CloseLinkConnection` at local tick `1332`; Yellow recorded LinkMenu at
`628` and input at `660`. Transport recorded `6248` balanced native edges,
zero errors, and zero keepalive traffic. Local ticks are not a common clock:
this supports investigating time coordination, not a precise root-cause claim.
Retain `pokemon-native-blue-yellow-02a8e85-LMtfZm/output.log` and
`result.json` as failed diagnostic evidence. The original `02a8e85` dual unit
gate also had a native timing failure (`64/65`); a gate-reporting bug lost
failed test identity and iteration output. Runtime identity remained known
as native. Subsequent verified test races and reporting were corrected;
the `1f19707` dual pass does not erase that earlier failure.

### Prior scoped evidence

The preceding reconciliation started from documentation
head `f4fddfc6dd45fa8b130ac9409d533b8945c5538c`. Completed scoped evidence at
`86b66577856780b2d880222a1d6986d38af88333` is:

- Source unit `1060/1060` and timing `55/55` total (11 cases in each of five
  repetitions), in `pokemon-linkmenu-source-unit-86b6657-20260905`.
- Native unit `1060/1060` and timing `55/55` total across five repetitions,
  in `poke-native-qualification-bkFdAX/evidence`; the report identifies
  `cython/native-extension` and the bit-accurate serial contract.
- Both unit gates collected `1201` tests, declared `19/19` trade and `19/19`
  battle entrypoints, and validated the ten-entry manifest schema. Neither
  asset-free gate executed ROM gameplay.
- Asset-backed source remote `12/12` in `77.9s`, in
  `pokemon-linkmenu-source-remote-86b6657-20260905`, with five ROM hashes,
  three symbol hashes, and ten manifest entries byte-validated. The changed
  tier selection moves ROM-free cases into unit coverage; this is not the
  same selection as the parent remote tier or strict gameplay acceptance.

All named completed tiers have zero failures, skips, xfails, xpasses, or errors.
Each bundle retains `gate-report.txt`, `gate-report.json`, and
`evidence-manifest.json`. Reports identify the PyBoy revision, not the harness
Git SHA: retain the release handoff linking each bundle to its exact harness
head. The retained PR #56 body `pokemon-pr-linkmenu-epnTPu.md` associates the
source runs with that head and records five passing Yellow/Yellow LinkMenu
replays; it is a replay summary, not raw replay output. Native head association
and clean archive scope are recorded in `poke-native-qualification-bkFdAX`'s
`result-86b6657.txt` and `identity.txt`.

The parent full source run at `2eb21a5` passed unit `980/980` and local `47/47`
but failed remote `22/23` with a Yellow/Yellow LinkMenu backend-close error.
Its historical strict-trade result was `18/19` (failed), with battle still
ongoing at that snapshot; later passes cannot erase the failures. The full
native run at `f4fddfc`, with bundle name
`pokemon-full-native-f4fddfc-20260905`, passed unit `1060/1060` and local
`47/47` but failed remote `11/12` on Yellow/Yellow LinkMenu.
Independent integration verification records these historical results. Native
trade was ongoing with two failures at that snapshot, not a current live count; the
remote failure prevents full-gate qualification regardless of their results.
These scoped passes do not establish native gameplay acceptance.
The separately recorded source battle `19/19` pass and four-worker source
trade `18/19` failure (Red-color listener to Blue-color connector at Trade
Center warp) remain scoped results, not full candidate acceptance.

Required full candidate source/native, MCP, authenticity,
concurrency, cleanup, install/launch, and independent review gates remain open
until their complete current evidence passes. Enforced loopback-only operation
is allowed. Vanilla link fixtures, additional platforms, MCP-facing
starter/trade/battle workflows, and cross-host networking are separate
unqualified extensions; they do not replace or weaken the required gates.

## Historical evidence boundary

The pre-reconciliation public documentation baseline for this checklist is
`f048870bdbd4837b5494006ae49fe40510832a89` (the prior public head,
2026-09-04). The last verified pre-fix implementation/runtime snapshot under
audit is
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

Fixture provenance remains partial: nine canonical entries are verified, while
four vanilla-derived entries remain `PARTIAL` because their vanilla source
provenance is not established.

Static provenance review confirms that the documented stock ROM/SYM pins and
the existing vanilla fixture bytes validate. Vanilla ordinary capture
provenance cannot be established: replay against the retained source failed at
the 64-step bound, and the manifest's ordinary producer revision is historical
(`25e231c`). Those fixture bytes must not be described as reproducible from the
audited code snapshot.

At the historical implementation/runtime snapshot
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
`poke-harness-source-serial-acceptance-20260904/evidence` and ran after
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

The historical serialized source strict-trade gate is `FAIL` at `18/19`, with the
sole failure and retained evidence recorded above. A LinkMenu or transport
milestone is not a completed trade or battle. Actual MCP-driven gameplay is
only partially scoped: public calls reached Red's bedroom, the house exit,
Pallet Town, Oak's Lab, and lab movement, but a bounded starter attempt ended
at map `40`, position `(5,3)`, with `party.count=0`; no bypass was used, so MCP
starter/trade/battle remains unproven.

These historical results do not supersede the current audit snapshot or close
any required candidate gate.

The acceptance update merged in PR #42 recorded source-runtime representative
in-process and TCP Red-color/Yellow trade and battle checks, the source-local
strict battle matrix at 9/9, 954/954 unit tests, and 50/50 timing cases in each
of five repetitions. Its scoped MCP suite passed 6/6 real integration checks
in `13.11s` with one SDL warning, and 104 dispatch tests passed. This is
scoped candidate evidence, not full strict trade, native-runtime, current
remote-matrix, or MCP starter/trade/battle sign-off, so it does not change the
release decision.

## Change summary

Candidate `1f19707` includes serial idle/external-hint `MAX_CYCLES` handling
beyond `2^31`, dispatch lock ordering and admission deadlines, failure-first
gate evidence, partial pipe capture, and corrected timing-test ordering.
Peer stack diagnostics are opt-in through positive finite
`POKERED_PEER_TRACE_AFTER_SECONDS`; optional `POKERED_PEER_TRACE_DIR` must
already exist. Capture is bounded to two one-shot dumps and makes no ROM
changes. Experimental coordinator `344aa95` is on another branch and is not
included in this candidate or its qualification claim.

The state-validity, transport, and release-gate hardening represented by the
audited code snapshot, plus the targeted post-snapshot `EDGE_RESP` closure
fix, are additive: they improve classification, lifecycle behavior, and
observability but do not themselves establish MCP gameplay, trade, battle, or
release readiness.

## Source and artifact hygiene

- [x] The pre-reconciliation public documentation baseline is identified as
  `f048870bdbd4837b5494006ae49fe40510832a89` (the prior public head), and the
  historical implementation/runtime snapshot is
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

- [x] Python requirement is `>=3.11`; NumPy is pinned separately for Python
  3.11 and 3.12+. Historical Python 3.12 gate results do not establish a
  complete Python 3.11 release gate.
- [x] The distribution bundles source PyBoy `2.7.0` at the harness-local
  divergence revision `7ecd4b73db822340467a28796b39504aad8d66c5` (pre-divergence
  harness fork revision `c565df66c3731fad2856169a90f6bbec99925915`).
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
- [ ] The current native/Cython strict real-ROM gate passes. Historically, at
  code snapshot `a8576b5ecb8e7039eefe0e02865b0bfc031387a7`, all 19/19
  entrypoints were declared and executed in each tier, with trade at 16/19 and
  battle at 17/19. The failure details and absence of native owner/IRQ errors
  are recorded above; these are strict real-ROM outcomes, not bypasses.
- [ ] Full candidate source/native trade and battle acceptance passes every
  required row. Recorded source battle passed 19/19; four-worker source trade
  failed 18/19. Native gameplay remains unqualified. Unit, timing, transport, and
  LinkMenu results do not substitute for gameplay acceptance.

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
- [x] Scoped source and native unit gates at `1f19707` each passed `1176/1176`
  and timing `65/65` total across five repetitions; see the named bundle above.
  The `86b6657` results remain historical scoped evidence.
- [x] Parent `2eb21a5` passed fresh standard-library venv/editable installation,
  dependency and source identity checks, plus a separate fresh wheel install,
  ten public imports, and pinned Red-color MCP startup/EOF cleanup. No MCP
  requests or gameplay were exercised by that packaging probe.
- [ ] The release candidate installs and launches with the documented commands,
  using the same runtime for tests and MCP; complete evidence is retained.
- [x] Historical scoped fixture-free Red, Blue, and Yellow boot/state/hash tests passed
  source `3/3` in `6.74s` and native `3/3` in `1.64s`, with only an SDL warning,
  with no external fixture loading or direct RAM edits. Normal save/load
  restores self-captured emulator state. Independent integration verification
  attributes these runs to the uncommitted evidence worktree. Both commands
  exited `0`; this is not commit qualification.
- [x] Fixture-free canonical color Red, color Blue, and Yellow boot/state/hash
  checks passed at `1f19707`: source `3/3` in `6.74s`, native `3/3` in
  `1.89s`, with zero failures/skips/errors and one SDL warning each. These
  `120`-frame state/save/load checks do not establish gameplay acceptance.
- [x] Candidate timing-sensitive tests passed five repetitions at `1f19707`
  in source and native mode, with controlled ordering and worker completion.
- [ ] Every required tier has no skips, xfails, failures,
  errors, or timeouts. Missing assets cause skips only in optional diagnostics.
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
  established. The 9/9 source-local battle result at
  `a8576b5ecb8e7039eefe0e02865b0bfc031387a7` is historical; the newer recorded
  source battle result covers 19/19 local/TCP entrypoints. Full candidate
  source and native acceptance still requires passing trade and battle tiers.
- [x] Scoped source remote at `86b6657` passed `12/12` with asset validation;
  see the named bundle above. This does not qualify strict gameplay.
- [ ] Current remote strict trade and battle tiers pass in both required
  runtimes with bounded teardown and every ordered listener/connector pair.
- [x] The strict acceptance declaration has an entrypoint for every canonical
  ordered local and remote Red/Blue/Yellow pair (19 trade and 19 battle node
  IDs).
- [x] The historical PR #17 baseline local and remote strict runtime rows pass in both
  listener/connector directions; strict trade and strict battle each passed
  19/19 with bounded teardown.
- [ ] A clean current full source/native gate passes. The parent source run
  at `2eb21a5` and full native run at `f4fddfc` both failed remote; their
  ongoing strict-row records above are historical snapshots.
  Scoped passes and diagnostic retries cannot qualify either full gate.
- [ ] Authentic handshake, nybble exchange, menu selection, post-menu blocks,
  complete trade records, and battle turns pass on the candidate. Production
  paths do not depend on test-only auto-select hooks, forced RAM writes, or
  fixture-specific bypasses; immutable fixture preparation is distinguished
  from acceptance execution.
- [x] The historical PR #19 bounded localhost concurrency/lifecycle probe passes 8/8 in
  each of five repetitions.
- [ ] Serialized emulator access, concurrent calls, shutdown, and cancellation
  invariants pass on the candidate under the declared scope.
- [ ] The opt-in capacity policy (`--capacity-policy`, issue #86) is exercised
  on a real declared allocation; its enforcement defaults and end-to-end
  admission/telemetry evidence remain open, so it does not gate releases yet.
- [ ] Required native build/runtime coverage and independent release review
  are complete. Unadvertised load and platform extensions are coverage limits,
  not additional blockers; historical Windows checks do not qualify the candidate.
- [ ] The qualification job was bound to a pinned, immutable allocation
  descriptor whose host-wide lease lock, allocation cgroup membership, reserved
  cpuset, or operator-created exclusive marker was re-observed on the host, the
  holder was the only lock holder, the complete pinned ROM/SYM/fixture input set
  for the declared scope was present and hash-validated, and retained evidence
  from the fresh native build procedure tied the pinned inputs and completed
  build to the installed outputs. A declaration that only matched deployment
  IDs, a writable descriptor, a job-private lock, a checker-generated marker,
  competing cgroup members, a missing required input, or a mixed build without
  retained evidence does not count as a provisioned runner.

## Fixture provenance and generation

- [x] `release-evidence/fixture-manifest.json` records thirty-one external state
  entries with sizes, SHA-1/SHA-256 values, expected ROM/SYM pins, source-state
  records, runtime identity, and command templates: ten ordinary/battle rows
  plus eighteen `captured` boundary rows driven from the admitted battle
  fixtures by `scripts/produce_battle_state_fixtures.py`.
- [ ] Deterministic reproduction of the four vanilla-derived fixture bytes is
  established. Static validation confirms the stock ROM/SYM
  pins and existing vanilla fixture bytes, but vanilla ordinary capture
  provenance cannot be established: replay against the retained source failed
  at the 64-step bound, and the manifest's ordinary producer revision is
  historical (`25e231c`).
- [x] `scripts/produce_cable_club_fixture.py` validates pins and has bounded
  defaults of 180 seconds and 64 movement steps.
- [x] `scripts/prepare_battle_cable_club_fixtures.py` is tracked and produces
  entries: the immutable derived battle fixtures, the six-member `slots`
  fixtures whose party records are pairwise distinct, and the eighteen
  captured boundary fixture rows; acceptance does not prepare party state in
  emulator RAM.
- [x] Provenance is verified for nine canonical fixture entries.
- [x] Provenance is captured for the eighteen boundary fixture entries: each
  row names the admitted battle fixture it was driven from by SHA-1 and
  carries its runtime identity, capture timestamp, and verification method.
- [ ] Provenance for the four vanilla-derived entries is verified. Their
  vanilla source states are not proven to match the vanilla ROM; replay against
  the retained source failed at the 64-step bound, and the manifest's ordinary
  producer revision is historical (`25e231c`), so those rows remain `PARTIAL`.
- [ ] The operator validates all 28 manifest entries with
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
- [ ] Current-candidate tests verify enforced loopback binding/connection and
  rejection of unsafe destinations. Authentication/encryption is required only
  if cross-host support is added; enforced localhost-only is allowed.
- [ ] MCP startup, initialize, list_tools, step, press, state resources,
  save/load, link_listen, link_connect, link_status, and link_disconnect pass
  on the candidate, including single-session and in-process pair operation.
- [ ] Emulator access is serialized and thread-safe; structured errors cover
  missing ROMs/symbols, bad hashes, unavailable peers, timeouts, and invalid
  state transitions.
- [ ] Save-state version handling and callback deregistration pass. Disconnect,
  EOF, cancellation, peer timeout, duplicate messages, reconnect, and shutdown
  complete within deadlines without deadlocks or leaked workers, sockets,
  callbacks, or sessions, including concurrent calls and shutdown overlap.
- Coverage limit: actual MCP-driven starter/trade/battle remains unproven. Unit,
  transport, lifecycle, and LinkMenu evidence do not prove MCP-driven starter,
  trade, or battle. Historical MCP evidence is 6/6 real integration checks in
  `13.11s` with one SDL warning, plus 104 passing dispatch tests;
  public calls reached Red's bedroom, the house exit, Pallet Town, Oak's Lab,
  and lab movement, but the bounded starter attempt ended at map `40`,
  position `(5,3)`, with `party.count=0`, without a bypass.
- [ ] Documentation distinguishes single-session, paired-link, transport,
  trade, battle, manual fixtures, and Option-B/RAM-boost diagnostics, with no
  broken links or machine-specific launch paths.

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

After each integration, rerun the narrow affected suite and then the full
production gate. Retain exact harness commit and clean-worktree identity,
runtime and asset identity, commands, counts, roles, deadlines, and teardown
results. When sanitized reports omit the harness SHA, retain an explicit
handoff linking the bundle to that SHA; never infer it from the PyBoy pin.

Do not describe a skipped, xfailed, timed-out, synthetic, hook-only,
RAM-mutated, or LinkMenu-only result as a completed trade or battle.
