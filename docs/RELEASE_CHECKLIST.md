# Release checklist

Use this checklist for a proposed release of `pokered-harness`. A checked
source pin or passing unit test is not a substitute for the real-ROM and
runtime evidence required by the relevant capability.

## Current audit status

The baseline at commit `e219fb5` (2026-08-29) was not production-ready. The
current working tree addresses the collection boundary, portable `.mcp.json`,
bundled PyBoy source runtime, path-aware ROM pinning, native local/TCP serial
attachment, teardown, and tiered gate reporting. Its live evidence is still
not release sign-off because the changes are uncommitted and the broad matrix
is not certified.

The latest post-hardening dual-runtime gate used separate source and Cython
interpreters. Each runtime collected 771 tests, passed unit 626/626, and
passed timing 50/50 in each of five repetitions. The current focused
`serial-link+network` scope passed 78/78 in each runtime; this is transport and
runtime-contract evidence, not a trade or battle gameplay result.

Historical evidence from the earlier pinned-interpreter gate:

- unit: 360/360 passed;
- timing: 35/35 passed across five repetitions;
- local: 46/46 passed; remote: 11/11 passed;
- strict acceptance tiers: trade 2/2 and battle 2/2 passed;
- strict local trade: Red/Yellow party-record swap passed;
- strict local battle: Red/Yellow battle-turn resolution passed; and
- remote: transport/LinkMenu smoke plus strict Red/Blue subprocess trade and
  battle passed, including both full party-record swaps and move-turn progress.

The local passes are real stateful acceptance evidence for that exact
Red/Yellow fixture pair, and the remote pass is evidence for the exact
Red/Blue color-variant subprocess pair. They do not certify the unrun variant
rows.

The release status remains `PARTIAL`; the open trade, battle,
fixture-provenance, security, and platform blockers remain.

These are evidence boundaries, not waived checklist items. The release commit
must include `tests/__init__.py`, the pinned vendor source files, and the gate
script before reproducing the results from a clean checkout.

## Source and artifact hygiene

- [ ] The release commit is identified and the worktree is clean.
- [ ] No ROM, `.sym`, `.sav`, `.state`, screenshot, log, cache, or other
  ROM-derived artifact is tracked.
- [ ] The package metadata, README, `VERSIONS.md`, and this checklist agree on
  the release commit and supported scope.
- [ ] No machine-local path, placeholder hash, credential, or unreviewed
  generated file appears in release documentation.

## Runtime and dependency identity

- [ ] Python version is 3.11 or newer and is recorded.
- [ ] `python -m pip check` passes in the release environment.
- [ ] The installed PyBoy runtime is `2.7.0` with harness revision
  `c565df66c3731fad2856169a90f6bbec99925915`, and the source-runtime build
  mode is recorded.
- [ ] If link support is claimed, the exact runtime exposes the serial objects
  used by the link layer and the same runtime is used for every link test.
- [ ] No standalone PyBoy wheel shadows the bundled runtime; the resolved
  module path and serial contract are recorded by the production gate.

## ROM and symbol identity

- [ ] Every ROM used by the release is legally sourced and matches a SHA-1 in
  [`VERSIONS.md`](../VERSIONS.md).
- [ ] `POKERED_ROM_SHA1` is explicit for every launch and test run.
- [ ] `POKERED_SKIP_SHA1` is unset for release evidence.
- [ ] Each symbol file matches its ROM and records its own SHA-1, generator
  source commit, RGBDS version, and build flags.
- [ ] Symbol-label checks pass for every claimed game version.

## Test gates

- [ ] Both `python -m pytest --collect-only -q` and the
  `pytest` console-script collection path complete without collection errors.
- [ ] The broad suite completes with no unexpected failure, skip, xfail, or
  timeout: `python -m pytest -q -ra`.
- [ ] ROM-free unit/protocol/transport tests pass.
- [ ] Real-session boot, state, and MCP stdio tests pass for every claimed
  single-session ROM variant.
- [ ] Local link tests pass with matching ROM-specific fixtures and the
  release runtime.
- [ ] Remote transport tests pass with listener and connector roles recorded.
- [ ] A full remote trade is claimed only if the separate-process trade test
  passes without fixture/runtime skips and asserts both sides received the
  peer's Pokémon.
- [ ] The canonical local link battle is claimed only after the release-runtime
  test resolves a complete turn on both sides; LinkMenu or transport
  milestones do not count.
- [ ] The remote link battle is claimed only after the separate-process
  release-runtime test resolves move exchange and execution on both sides
  without semantic exchange hooks or RAM patches.
- [ ] Every parameterized version/variant row in the claimed matrix ran, or
  the omitted rows are explicitly listed as unsupported.

## Fixture provenance

- [ ] Every Cable Club state was captured from the exact ROM bytes it loads
  with.
- [ ] Vanilla, color, and Yellow variants use separate, identified fixtures.
- [ ] Fixture hashes and source-state provenance are in the evidence bundle.
- [ ] Fixture generation used
  [`scripts/produce_cable_club_fixture.py`](../scripts/produce_cable_club_fixture.py)
  or a documented equivalent; no missing Yellow-specific producer is cited.
- [ ] Fixture files remain untracked and are available to the release runner
  through a controlled asset mechanism.

## MCP and network operation

- [ ] The MCP launch uses explicit ROM, symbol, and SHA-1 environment values.
- [ ] The MCP client uses the tested `${PWD}` configuration whose paths match
  the `rom/<version>/` layout and whose hash is explicit.
- [ ] Server stdout remains valid MCP JSON-RPC and emulator diagnostics go to
  stderr.
- [ ] The MCP remote TCP API is restricted to localhost because the current
  transport has no authentication or encryption; it is not exposed to a LAN,
  public address, or WAN.
- [ ] Listener, connector, pair, disconnect, and session teardown are all
  exercised and leave no child process or open socket.

## Documentation and sign-off

- [ ] Markdown links resolve against the release tree.
- [ ] Claims distinguish implemented source, candidate tests, fixture-gated
  diagnostics, and release-certified behavior.
- [ ] The evidence bundle includes exact commands and complete test output,
  not only a pass count or a screenshot.
- [ ] Known limitations, skipped rows, and runtime deviations are listed next
  to the sign-off decision.
- [ ] An independent reviewer confirms that no transport milestone, synthetic
  protocol test, or RAM-mutated fixture is described as a completed gameplay
  acceptance.

**Release decision:** `PARTIAL`. The release gate remains `BLOCKED` until every
required item above is checked, the claimed matrix is rerun from a clean
checkout, and the evidence bundle is attached to the exact release commit.
