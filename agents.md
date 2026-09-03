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

The published base is `8727779` (PR #28 documentation merge). The current
isolated candidate additionally contains runtime, lifecycle, TCP, gate,
headless-performance, and TCP-peer-teardown commits `94f4429`, `9ce7c9f`,
`3d04293`, `f046c34`, `8b4847b`, `9453d7a`, and `abf3d27`; this is a `PARTIAL`
publication candidate until the final gate and merge are verified. The release runtime is the bundled PyBoy source
snapshot pinned in `VERSIONS.md` (`2.7.0`, harness revision
`c565df66c3731fad2856169a90f6bbec99925915`) with `mcp==1.29.1`. Source mode
is the documented release default.

The integrated source and explicitly selected Cython gates each collect 730
tests and pass unit 586/586 plus timing 40/40 in each of five repetitions. The
source gate uses vendored Python modules; the Cython gate reports native
extensions for all five required PyBoy modules. With hashed assets supplied,
source and Cython remote tiers pass 15/15, and the Cython local/session tier
passes 47/47. Source strict trade and battle gates each reached 18/19 under
bounded parallel execution; the isolated retry of each failed remote
Yellow-to-Yellow selector passed. Exact native Red-to-Red trade/battle and
Blue Color-to-Red Color trade rows pass, but full native strict qualification
remains open after the historical post-PR #27 native trade result of 16/19. Use explicit ROM, symbol,
and SHA-1 values; never use `POKERED_SKIP_SHA1=1` for release evidence.

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
- The integrated source and Cython remote transport/MCP tiers pass 15/15, and
  the integrated Cython local/session tier passes 47/47. Current source strict
  trade and battle gates each reached 18/19 under parallel execution, with the
  failed remote Yellow-to-Yellow selectors passing isolated retries. Full
  native strict acceptance remains open.
- The current tree declares all canonical Red/Blue/Yellow listener and
  connector orderings over localhost TCP. The PR #19 synthetic concurrency
  probe passes, while real-ROM concurrent-load stability remains open.
- The strict acceptance declaration has a dedicated entry point for every
  canonical ordered local and remote pair (19 trade and 19 battle nodes).
  Collection is declaration evidence only; a LinkMenu milestone is not a
  gameplay acceptance. Complete 19/19 trade and battle results exist for the
  pre-PR #19 source-runtime baseline; the current source gate rows are
  18/19 in each parallel strict matrix, with isolated retries passing. The
  historical native trade qualification is `FAIL` at 16/19; the full native
  matrix remains open.
- Vanilla ordinary and derived fixture rows are `PARTIAL` because their
  source-state provenance is not proven against the vanilla ROM. Stock link
  pairs, other version pairs, and unlisted variants are unsupported or
  unverified until separately gated.
- TCP has no authentication or encryption and is enforced as loopback-only;
  never expose it to a LAN, WAN, or public address.

The release decision remains `PARTIAL`, not `PRODUCTION-READY`. Known open
items include a clean full strict gate at the documented conservative worker
count, native serial handling under the full matrix, a completed broad-suite
release run, vanilla fixture provenance,
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
