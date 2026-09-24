# Agents: `pokered-harness`

This file is the repository-local operating contract for agents working on the
Pokémon Red/Blue/Yellow PyBoy MCP harness. Resolve the checkout root with
`git rev-parse --show-toplevel`; ROMs, symbols, save states, and evidence may
be supplied through ignored or external paths.

## Before changing files

Read `README.md`, `VERSIONS.md`, `docs/PRODUCTION_RUNBOOK.md`, and
`docs/RELEASE_CHECKLIST.md`, then inspect the exact current runtime, assets,
tests, and worktree. Treat observed commands and output as authoritative over
historical prose. Assign disjoint file ownership before using subagents.

## Worktree and artifact safety

- Work in an isolated worktree. Preserve dirty user work; never reset, clean,
  discard, overwrite, or broadly stage unrelated paths.
- Commit only explicitly owned files. Do not push, merge, or modify a shared
  checkout without separate authorization.
- Never commit ROMs, `.sym` files, save states, screenshots, logs, caches,
  credentials, virtual environments, generated build output, or absolute local
  paths.
- Keep test and production evidence separate from diagnostic shortcuts. A
  skipped, xfailed, timed-out, synthetic, hook-only, or RAM-mutated result is
  not a production acceptance.

## Runtime and asset contract

The documented release runtime is the bundled PyBoy source snapshot: PyBoy
`2.7.0`, harness-local divergence revision
`3b4e9d23463f83d62a4f0cccde35dfa045ad9f79` (upstream base
`c565df66c3731fad2856169a90f6bbec99925915`), with `mcp==1.29.1`. Source mode
is the default. Cython/native mode is an explicitly selected optional
diagnostic/runtime path and must prove that the required modules are installed
extensions rather than silently resolving to vendored source. Do not allow an
unmarked standalone PyBoy installation to shadow the bundled runtime.

Use explicit ROM, symbol, and SHA-1 values. Never use
`POKERED_SKIP_SHA1=1` for release evidence. The portable `.mcp.json` contract
uses the installed environment's `python` and workspace-relative `${PWD}`
asset paths; it must not depend on a machine-local `PYTHONPATH` or absolute
path.

## Evidence snapshot

The following claims are deliberately scoped to their named run and do not
change the `PARTIAL` release decision.

### Latest scoped candidate evidence (`1f19707`, 2026-09-05)

- Source and native each passed `1,176/1,176` unit tests and `65/65` total
  timing checks (`13` cases in each of five repetitions), with no failures,
  skips, or errors. Each collected `1,320` tests; `19/19` trade and `19/19`
  battle entrypoints were declared, not executed by this unit-only gate.
- Canonical boot checks passed source `3/3` in `6.74s` and native `3/3` in
  `1.89s`, with zero failures, skips, or errors and one SDL warning each.
  They exercise `120` frames plus state/save/load checks, not gameplay.
- The fresh native build used base `e1686ca` plus the serial fix; its serial
  source hash matches `1f19707`. Build identity and scoped passes do not
  establish a clean full gameplay qualification.
- The native Blue/Yellow replay at `02a8e85` (runtime identical to `1f19707`)
  failed in `721.67s`; both peers exited cleanly with return code `1`.
  Blue recorded no LinkMenu and one `CloseLinkConnection` at local tick
  `1332`; Yellow reached LinkMenu at `628` and input at `660`. There were
  `6,248` balanced native edges, zero errors, and no keepalive traffic.
  Local ticks are not a common clock: this supports investigating time
  coordination, not a proven precise root cause.
- Retain the original `02a8e85` dual-gate failure: native timing was `64/65`
  and a gate bug lost failed test identity and iteration output. Subsequent verified test
  races were corrected before the passing `1f19707` dual gate; the later
  pass does not erase that earlier failure.

The candidate fixes serial idle/external hints to retain `MAX_CYCLES` when
the cycle counter exceeds `2^31`,
dispatch lock ordering and admission deadlines, failure-first gate evidence,
and partial pipe capture. Optional peer diagnostics use a positive finite
`POKERED_PEER_TRACE_AFTER_SECONDS` and, optionally, an existing
`POKERED_PEER_TRACE_DIR`; they schedule at most two one-shot stack dumps and
make no ROM changes. Experimental coordinator `344aa95` is on another branch
and is not included in this candidate. Release status remains `PARTIAL`.

### Historical source-runtime and full-gate evidence

- The historical full-gate snapshot at `2eb21a5` passed unit `980/980` and
  local `47/47`,
  but remote failed `22/23` with a Yellow/Yellow LinkMenu backend-close error.
  Its trade tier failed `18/19`; battle was ongoing at that snapshot, not a
  current running-status claim.
  The follow-up shutdown change passed five consecutive targeted real-ROM
  replays; a full candidate gate is still required.
- The historical native full-gate snapshot at `f4fddfc` passed unit
  `1,060/1,060` and local `47/47`, but failed remote `11/12`. Its trade tier
  was unfinished with failures at that snapshot; no full native pass is claimed.
- The recorded source strict battle gate passed `19/19` local and TCP real-ROM
  entrypoints in `1,237.8s`, with no skips, errors, or test-only protocol
  bypasses. This is battle-tier acceptance, not full candidate acceptance.
- The older source strict trade gate failed at `18/19` with `workers=4`;
  `red_color-listen-blue_color-connect` timed out during Trade Center warp.
  Targeted passing replays do not replace a complete passing trade matrix.
- The historical real MCP stdio suite passed `4/4`, including protocol/tool/resource
  discovery, state observation, save/load round-trip, and a two-process remote
  listen/connect EOF-cleanup lifecycle. It does not prove MCP-driven starter,
  trade, or battle gameplay; the bounded MCP gameplay attempt did not acquire
  a starter within its input bound.
- The supplied asset checks validate the pinned ROM/SYM and fixture bytes in
  the recorded evidence. Six canonical color-Red, color-Blue, and Yellow
  fixture entries have verified provenance. Four vanilla-derived entries remain
  `PARTIAL`; matching bytes are not proof of vanilla capture provenance.

### Historical isolated subagent matrix diagnostics

Subagents ran these rows on exact pre-PR #42 head
`71ca834d673c52eb74044089e92b09e7e3ae00a0`. They are useful diagnostic
evidence for the source runtime, but are not a clean current-head full-gate
sign-off for implementation head
`6d541b7867e82fa548c456008e6acd7fb1071586` or documentation sync
`cbed7ef9cd1e5b645b15724c6655ef6faf7fa2b9`:

- Source local trade: `9/9` ordered rows passed, with actual party-record
  swaps and trade hooks observed.
- Source remote TCP trade: `9/9` ordered rows passed.
- Source remote TCP battle: `9/9` ordered rows passed, with native edge
  traffic and clean peer teardown recorded.
- Source local battle: `6/9` completed rows passed. The remaining local rows
  `yellow-red`, `blue-yellow`, and `yellow-blue` were not completed in that
  run (`yellow-red` was paused; the other two were not started).

Because these diagnostics predate the current merged implementation and do not
cover every required tier in one reproducible run, do not relabel them as a
current strict acceptance matrix. Re-run the complete matrix with the exact
current commit, runtime identity, assets, roles, deadlines, and teardown
records before changing any status.

### Historical evidence that must remain labeled historical

- The retained prior-candidate dual source/native unit-and-timing gate
  collected `1,094` tests in each runtime, passed `949` unit tests in each,
  and passed `50/50` timing cases in each of five repetitions. It did not run
  ROM-backed gameplay.
- Its scoped asset tier passed source/native local `47/47` and remote `16/16`,
  with `5/5` ROM hashes, `3/3` symbol hashes, `3/3` fixture hashes, and the
  ten-entry manifest validated. This is not current-head strict gameplay
  acceptance.
- The historical PR #17 source baseline passed strict trade and battle `19/19`
  in its own all-tier run. The historical native strict-trade result was
  `18/19`, with mixed exact-row follow-ups including a party mismatch and
  phase stalls. A separate historical native remote battle lane covered only
  `3/9` direct rows (one pass, two failures, six unrun). None qualifies the
  current native matrix.
- The last broad source diagnostic is not a full-suite pass: it stopped at its
  supervisor bound after `418/703` tests (`386` passed, `20` skipped, `12`
  failed, and `285` not started). Do not describe unit/timing or focused gates
  as a clean repository-wide suite.

## Supported-scope boundary

The strict declaration contains `19` local/remote entry points per operation:
the nine ordered Red/Blue/Yellow local pairs, nine ordered listener/connector
TCP pairs, and the dedicated Red/Yellow assertion. Collection and declaration
audits prove shape only.

The following remain open for release qualification:

- A clean current-head full source gate, including a passing strict trade
  matrix. The recorded source battle `19/19` pass does not establish full
  acceptance of a later candidate.
- A passing full native strict trade/battle matrix; source-runtime acceptance
  does not qualify the compiled runtime.
- Current-candidate MCP startup/state/action/lifecycle verification, required
  concurrency and cleanup regressions, and independent release review.
- Clean-install and launch evidence for the candidate being qualified. The
  standard-library editable-install path passed at `2eb21a5` on Python 3.12.13,
  including pip dependency checks and source bootstrap identity. A separate
  fresh wheel install passed dependency checks, ten public imports, and
  pinned Red-color MCP startup/EOF cleanup outside the checkout. No MCP
  requests or gameplay were exercised by that installation check.

Separate coverage limits outside the declared canonical loopback scope:

- MCP-driven starter/trade/battle workflows remain unproven.
- Vanilla ordinary fixtures and their derived rows lack complete provenance. The
  retained replay failed at the `64`-movement-step bound, and ordinary-fixture
  producer revision `25e231c` is historical; no current-head reproducibility
  claim may be made.
- Additional native platforms and real-ROM concurrent-load qualification.
- Cross-host networking. TCP is intentionally restricted to loopback
  addresses and currently has no authentication or encryption; never expose it
  to a LAN, WAN, or public address.

The current implementation hardens deadlines, runtime provenance, subprocess
result validation, serial/link lifecycle, MCP teardown, and network-close
diagnostics. Those changes improve safety and observability but do not by
themselves prove gameplay or eliminate the open gates above.

## Verification and handoff

Use `scripts/production_gate.py` with the same interpreter used by installation
and MCP for a single-runtime gate. For the dual gate, pass the source
environment with `--python` and the separately bootstrapped Cython environment
with `--cython-python`; retain both runtime identities and outputs. `--unit-only`
is an asset-free scoped gate, not a release gate; the default command additionally
requires the five ROMs, three symbols, external fixture bytes, complete strict
matrix declaration, and required real-ROM tiers.
The default strict matrix worker count is one because each row runs an emulator
pair; increase `--matrix-workers` only after measuring the host budget.
The clean-install baseline is `python3 -m venv .venv`, activation, `python -m
pip install -e ".[dev]"`, `python -m pip check`, and
`python scripts/bootstrap_pyboy.py --mode source --check`; `uv sync --locked
--extra dev` is the lockfile-resolved alternative when `uv` is available.
Required real-ROM tiers must have no skips, xfails, failures, errors, or
timeouts. Retain complete output with commit, runtime, ROM/SYM/fixture hashes,
fixture provenance, roles, deadlines, and teardown results. The ordinary
fixture producer is pinned and bounded; the tracked battle utility creates
derived immutable states but is not gameplay acceptance. Every subagent
handoff must state its diagnosis, owned files, exact commands/results, risks,
commit/worktree, and smallest next step.

The final report must distinguish implemented behavior, controlled acceptance,
diagnostic evidence, unsupported rows, security limits, and the exact release
decision. Use `PRODUCTION-READY` only after every required gate is green in a
clean reproducible checkout.
