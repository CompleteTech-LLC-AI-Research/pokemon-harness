# Release checklist

Use this checklist for a proposed release of `pokered-harness`. A checked
source pin or passing unit test is not a substitute for the real-ROM and
runtime evidence required by the relevant capability.

## Current audit status

The baseline at `e219fb5` (2026-08-29) was not production-ready. The
historical audited implementation revision is
`1046a541e0003923aec6000b6b383c6eaafeaa48`; it addressed the collection
boundary, portable `.mcp.json`, bundled PyBoy source runtime, path-aware ROM
pinning, native local/TCP serial attachment, teardown, dependency pinning, and
tiered gate reporting.

Evidence provenance matters: the complete real-ROM gate was rerun at that exact
clean implementation revision after all runtime integration commits. The
current candidate has since changed runtime/test behavior, including the link
scheduler and battle driver, so the full-gate counts below are historical
implementation-revision evidence; they are not current candidate sign-off and
the complete output is not retained in this tree as a release evidence bundle.

Full-gate snapshot:

- unit: 372/372 passed;
- timing: 35/35 passed across five repetitions;
- local: 46/46 passed; remote: 11/11 passed;
- strict acceptance tiers: trade 2/2 and battle 2/2 passed;
- strict local trade: Red/Yellow party-record swap passed;
- strict local battle: Red/Yellow battle-turn resolution passed; and
- remote: transport/LinkMenu smoke plus strict Red/Blue subprocess trade and
  battle passed, including both full party-record swaps and move-turn progress.

Current candidate rerun evidence (2026-08-31, source-hardening, teardown,
packaging, and evidence fixes included):

- unit: 415/415 passed;
- timing: 35/35 passed across five repetitions;
- local: 46/46 passed at the default scheduler slice;
- remote: 11/11 passed for the canonical color-Red listener and color-Blue
  connector roles; and
- strict local Red/Yellow trade and battle passed at the library's default
  scheduler slice (the tighter 64-cycle rerun also passed); and
- an isolated Red/Blue subprocess trade passed 1/1 in 229.37 seconds, while
  the same trade timed out when the stateful tiers ran concurrently; and
- the mechanical battle tier passed 2/2, but its remote case uses a
  deterministic LinkMenu RAM/hook selector and is not user-driven evidence.

These current counts supersede the corresponding historical unit count for
this candidate. The stateful counts were collected at the code-equivalent
`2ac09fb` boundary; the final-head fast unit/remote rerun was at functional
source boundary `23ea392`. Later candidate commits only update the audit
records. The intervening functional commits fix gate parsing, preserve
virtualenv paths, and annotate deliberate cleanup suppression. Sanitized
per-tier evidence bundles were retained outside the checkout with
`--evidence-dir`; a single complete release evidence bundle is still not
attached to this tree.

The local passes are real stateful acceptance evidence for the exact color-Red /
Yellow fixture pair, and the remote transport pass is evidence for the exact
color-Red listener / color-Blue connector subprocess pair. The isolated remote
trade uses native serial payloads and full party-record checks, but the
concurrent timeout means load-stable trade is not certified. The remote battle
driver uses a LinkMenu selection hook; native serial payload checks do not turn
that into fully user-driven gameplay. None of this certifies unrun or
reversed-role rows.

These are evidence boundaries, not waived checklist items. The candidate
includes `tests/__init__.py`, the pinned vendor source files, and the gate
script, and the results were reproduced from the isolated clean checkout.

## Source and artifact hygiene

- [ ] The release commit is identified and the isolated candidate worktree is
  clean. BYO ROM/fixture inputs and ignored local build output remain outside
  the tracked release tree.
- [x] No ROM, `.sym`, `.sav`, `.state`, screenshot, log, cache, or other
  ROM-derived artifact is tracked.
- [ ] The package metadata, README, `VERSIONS.md`, and this checklist agree on
  the final release commit and supported scope.
- [x] No machine-local path, placeholder hash, credential, or unreviewed
  generated file appears in the release documentation.

## Runtime and dependency identity

- [x] Python version is 3.12 or newer and is recorded.
- [x] `python -m pip check` passes in the release environment.
- [x] The installed PyBoy runtime is `2.7.0` with harness revision
  `c565df66c3731fad2856169a90f6bbec99925915`, and the source-runtime build
  mode is recorded.
- [x] If link support is claimed, the exact runtime exposes the serial objects
  used by the link layer and the same runtime is used for every link test.
- [x] No standalone PyBoy wheel shadows the bundled runtime; the resolved
  module path and serial contract are recorded by the production gate.

## ROM and symbol identity

- [x] Every ROM used by the release is legally sourced and matches a SHA-1 in
  [`VERSIONS.md`](../VERSIONS.md).
- [x] `POKERED_ROM_SHA1` is explicit for every launch and test run.
- [x] `POKERED_SKIP_SHA1` is unset for release evidence.
- [x] Each symbol file matches its ROM and records its own SHA-1, generator
  source commit, RGBDS version, and build flags in `VERSIONS.md`.
- [x] Symbol-label checks pass for every claimed game version.

## Test gates

- [x] Both `python -m pytest --collect-only -q` and the
  `pytest` console-script collection path complete without collection errors.
- [ ] Repository-wide Ruff audit is clean (`ruff check .` currently reports
  528 findings under the locked Ruff version, including vendored-runtime and
  legacy code).
- [ ] The broad suite completes with no unexpected failure, skip, xfail, or
  timeout: `python -m pytest -q -ra`.
- [x] ROM-free unit/protocol/transport tests pass.
- [ ] Real-session boot, state, and MCP stdio tests pass for every claimed
  single-session ROM variant. A clean wheel installed outside the checkout
  passed the explicit color-Red MCP smoke 3/3 on 2026-08-31; the wider
  stock/color Red, stock/color Blue, and Yellow five-row result is an earlier
  snapshot. The scripted intro golden path remains specific to Red.
- [x] Local link tests pass with matching ROM-specific fixtures and the
  release runtime.
- [x] Remote transport tests pass with the color-Red listener/internal-clock
  and color-Blue connector/external-clock roles recorded.
- [ ] Reversed listener/connector roles are independently tested and certified.
- [x] Controlled native-serial remote trade acceptance passes in separate
  processes without fixture/runtime skips and asserts both sides received the
  peer's Pokémon in an isolated run; load-stable concurrent execution remains
  open.
- [ ] Remote trade remains green when the required stateful tiers run
  concurrently; the current run timed out the trade tier.
- [x] The canonical local link battle is claimed only after the release-runtime
  test resolves a complete turn on both sides; LinkMenu or transport
  milestones do not count.
- [x] Controlled native-serial remote battle diagnostic resolves move exchange
  and execution on both sides in separate processes; its deterministic
  LinkMenu RAM/hook selector is not production user-driven evidence.
- [ ] A user-driven remote trade and battle path passes without the test-driver
  LinkMenu selection hook.
- [x] Every supported release row has a result or an explicit unsupported
  classification; the failed diagnostic rows are listed below.

## Fixture provenance

- [ ] Every Cable Club state, including the separate battle-start states, was
  captured from the exact ROM bytes it loads with; the current tree lacks a
  retained battle-fixture provenance record.
- [ ] Vanilla, color, and Yellow variants use separate, identified fixtures.
- [ ] Fixture hashes and source-state provenance are in the evidence bundle.
- [x] The ordinary Cable Club fixtures have a documented producer:
  [`scripts/produce_cable_club_fixture.py`](../scripts/produce_cable_club_fixture.py)
  and no missing Yellow-specific producer is cited. The producer does not
  create the separate battle fixtures.
- [ ] Fixture files remain untracked and are available to the release runner
  through a controlled asset mechanism.

## MCP and network operation

- [x] The MCP launch uses explicit ROM, symbol, and SHA-1 environment values.
- [x] The MCP client uses the tested `${PWD}` configuration whose paths match
  the `rom/<version>/` layout and whose hash is explicit.
- [x] Server stdout remains valid MCP JSON-RPC and emulator diagnostics go to
  stderr.
- [x] The MCP remote TCP API is restricted to localhost because the current
  transport has no authentication or encryption; it is not exposed to a LAN,
  public address, or WAN.
- [x] Listener, connector, pair, disconnect, and session teardown are all
  exercised and leave no child process or open socket.

## Documentation and sign-off

- [x] Markdown links resolve against the release tree.
- [x] Claims distinguish implemented source, candidate tests, fixture-gated
  diagnostics, controlled native-serial acceptance, and release-certified
  behavior.
- [ ] The evidence bundle includes exact commands and complete test output,
  not only a pass count or a screenshot.
- [x] Known limitations, skipped rows, and runtime deviations are listed next
  to the sign-off decision.
- [ ] An independent reviewer confirms that no transport milestone, synthetic
  protocol test, or RAM-mutated fixture is described as a completed gameplay
  acceptance.

**Release decision:** `PARTIAL`. The source/runtime and the narrowly defined
local plus controlled remote acceptance paths have passing snapshots, but full
product sign-off remains pending:

- a retained complete implementation-revision real-ROM gate evidence bundle;
- a retained evidence bundle, including battle-fixture hashes/provenance;
- a retained current local/remote gate and strict subprocess evidence bundle,
  plus resolution of the concurrent remote-trade timeout, the remote
  no-bypass battle path, and the Blue↔Blue and Blue→Red diagnostic battle gaps
  or an explicitly limited product scope that excludes them;
- repository-wide lint/broad-suite closure, per-ROM single-session coverage,
  reversed roles, native-platform certification, and independent review; and
- a secure transport decision if cross-host TCP is required. Current TCP is
  loopback-only and unauthenticated/unencrypted.

Do not advertise the failed, unrun, reversed-role, or user-driven remote rows as
supported until their own fixtures and acceptance gates pass.
