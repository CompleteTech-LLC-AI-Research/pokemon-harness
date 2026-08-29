# Release checklist

Use this checklist for a proposed release of `pokered-harness`. A checked
source pin or passing unit test is not a substitute for the real-ROM and
runtime evidence required by the relevant capability.

## Current audit status

As of commit `e219fb5` (2026-08-29), release sign-off is blocked:

- the `pytest` console-script collection path fails eight modules because
  several tests import `tests.*` but `tests/__init__.py` is not committed
  (some `python -m pytest` environments hide this through namespace-package
  behavior);
- the checked-in `.mcp.json` points at flat `rom/` paths that do not match the
  documented `rom/<version>/` layout;
- the default PyBoy Cython wheel does not expose the `mb`/serial attributes
  required by the current Python-side link-session prototype; and
- no current, clean, runtime-pinned evidence bundle demonstrates the complete
  Red/Blue/Yellow trade and battle matrix.

These are blockers, not waived checklist items.

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
- [ ] The installed PyBoy version is `2.7.0` and the build mode is recorded.
- [ ] If link support is claimed, the exact runtime exposes the serial objects
  used by the link layer and the same runtime is used for every link test.
- [ ] Any non-Cython or patched PyBoy build is identified by source commit and
  build instructions; it is not silently substituted for the default wheel.

## ROM and symbol identity

- [ ] Every ROM used by the release is legally sourced and matches a SHA-1 in
  [`VERSIONS.md`](../VERSIONS.md).
- [ ] `POKERED_ROM_SHA1` is explicit for every launch and test run.
- [ ] `POKERED_SKIP_SHA1` is unset for release evidence.
- [ ] Each symbol file matches its ROM and records its own SHA-1, generator
  source commit, RGBDS version, and build flags.
- [ ] Symbol-label checks pass for every claimed game version.

## Test gates

- [ ] Both `PYTHONPATH=src python -m pytest --collect-only -q` and the
  `pytest` console-script collection path complete without collection errors.
- [ ] The broad suite completes with no unexpected failure, skip, xfail, or
  timeout: `PYTHONPATH=src python -m pytest -q -ra`.
- [ ] ROM-free unit/protocol/transport tests pass.
- [ ] Real-session boot, state, and MCP stdio tests pass for every claimed
  single-session ROM variant.
- [ ] Local link tests pass with matching ROM-specific fixtures and the
  release runtime.
- [ ] Remote transport tests pass with listener and connector roles recorded.
- [ ] A full remote trade is claimed only if the separate-process trade test
  passes without fixture/runtime skips and asserts both sides received the
  peer's Pokémon.
- [ ] A link battle is claimed only if a release-runtime test resolves a
  complete turn on both sides; LinkMenu or transport milestones do not count.
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
- [ ] The MCP client uses a tested configuration whose paths match the actual
  ROM layout; the current `.mcp.json` blocker is resolved before relying on it.
- [ ] Server stdout remains valid MCP JSON-RPC and emulator diagnostics go to
  stderr.
- [ ] Remote TCP is restricted to loopback or an approved trusted private
  network because the current transport has no authentication or encryption.
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

**Release decision:** `BLOCKED` until every required item above is checked and
the evidence bundle is attached to the exact release commit.
