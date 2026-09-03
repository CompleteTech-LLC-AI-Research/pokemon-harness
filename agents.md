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

The published repository head is
`84dc79d8098fe5fa298db6700b5ba0b81610ed53` (PR #36, 2026-09-03). The current
implementation commit is
`54a739be4a5f2d95a924c6f35c2aa695246ddd2f` (PR #35), which moves remote serial-edge
dispatch to an explicit native instruction-batch boundary. This remains a
`PARTIAL` publication candidate, not a production release. The release runtime
is the bundled PyBoy source snapshot pinned in `VERSIONS.md` (`2.7.0`, harness
revision `c565df66c3731fad2856169a90f6bbec99925915`) with `mcp==1.29.1`.
Source mode is the documented release default; Cython is an optional diagnostic
build.

Current controlled evidence: the focused source and Cython
transport/serial/PyBoy-link suite passes 107/107 in each runtime; an
asset-backed source trade gate recorded 19/19 ordered rows; and the native
Cython trade gate recorded 18/19. Its only failed row is
`yellow-listen-blue_color-connect`, which stalls before party exchange at the
configured 720-second bound and reproduced on isolated retry. A timing-altered
diagnostic produced incorrect party records and is not acceptance evidence;
the full native battle matrix is unqualified. Fresh isolated source and Cython
unit/timing gates collected 730 tests in each mode, passed unit 586/586, and
passed timing 40/40 in five repetitions; the asset-free run did not exercise
ROM-backed gameplay. An earlier environment-specific 585/586 ownership result
is superseded for these isolated environments. Use explicit ROM, symbol, and
SHA-1 values; never use
`POKERED_SKIP_SHA1=1` for release evidence.

The source trade run was a trade-tier run whose supervisor started before PR
#35 was published. Its child runs loaded the current implementation, but the
result is not a clean post-merge all-tier sign-off.

The complete all-tier source-runtime gate was collected before PR #19 at the
PR #17 parent `b0b63c8` (698 tests: unit 554/554, local real-ROM 47/47,
remote transport/MCP 15/15, strict trade 19/19, strict battle 19/19, and
timing 40/40 in each of five repetitions, with no skips, xfails, failures,
errors, or timeouts). Post-PR #19 verification is intentionally reported as
separate historical slices: 699 collected, ROM-free unit 555/555, timing
40/40 in each of five repetitions, focused transport/MCP 167/167, localhost
concurrency probe 8/8 in each of five repetitions, and real-ROM remote
transport 15/15.

## Supported-scope boundary

- Canonical color-Red, color-Blue, and Yellow ordinary and derived battle
  fixture bytes have recorded reproduction evidence; the external states are
  never committed and their hashes do not prove gameplay compatibility.
- Prior integrated source and Cython remote transport/MCP tiers pass 15/15,
  and the prior integrated Cython local/session tier passes 47/47. These are
  scoped follow-ups, not current full native trade/battle acceptance. The
  current source trade gate recorded 19/19; current native trade recorded
  18/19, with the exact remote failure named above. The current native battle
  matrix is unqualified.
- The current tree declares all canonical Red/Blue/Yellow listener and
  connector orderings over localhost TCP. The PR #19 synthetic concurrency
  probe passes, while real-ROM concurrent-load stability remains open.
- The strict acceptance declaration has a dedicated entry point for every
  canonical ordered local and remote pair (19 trade and 19 battle nodes).
  Collection is declaration evidence only; a LinkMenu milestone is not a
  gameplay acceptance. Complete 19/19 trade and battle results exist for the
  pre-PR #19 source-runtime baseline. The current source trade gate recorded
  19/19, while the current native trade gate recorded 18/19; the current full
  source battle rerun and native battle matrix remain unqualified. The
  historical native trade qualification of `FAIL` at 16/19 is not current
  evidence.
- Vanilla ordinary and derived fixture rows are `PARTIAL` because their
  source-state provenance is not proven against the vanilla ROM. Stock link
  pairs, other version pairs, and unlisted variants are unsupported or
  unverified until separately gated.
- TCP has no authentication or encryption and is enforced as loopback-only;
  never expose it to a LAN, WAN, or public address.

The release decision remains `PARTIAL`, not `PRODUCTION-READY`. Known open
items include reproducible clean-install verification, a clean full strict gate
at the documented conservative worker count, native serial handling under the
full matrix, a completed broad-suite release run, vanilla fixture provenance,
full native-platform gameplay/load coverage, independent review, and
authenticated/encrypted cross-host transport. Fresh Windows install,
bootstrap, MCP stdio, and three-ROM lifecycle checks are now scoped evidence,
not full native qualification.

## Verification and handoff

Use `scripts/production_gate.py` with the same interpreter used by installation
and MCP. `--unit-only` is an asset-free scoped gate, not a release gate; the
default command additionally requires the five ROMs, three symbols, external
fixture bytes, complete strict matrix declaration, and required real-ROM tiers.
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
