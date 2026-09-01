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

The current merged candidate is `f5c766e` (PR #7), rooted at the live target
`master` tip `1e885df` and using the conservative
`DEFAULT_MATRIX_WORKERS=1` setting. Its asset-free gate collected 656 tests,
passed 514/514 unit tests, and passed 40/40 timing cases in each of five
repetitions. The asset-backed local tier passed 47/47, the remote transport/MCP
tier passed 13/13, and the strict trade tier passed 19/19. The strict battle
matrix is still being executed separately; no complete 19/19 battle claim is
made until that run exits. The release runtime is the bundled PyBoy source
snapshot pinned in `VERSIONS.md` (`2.7.0`, harness revision
`c565df66c3731fad2856169a90f6bbec99925915`) with `mcp==1.29.1`. Source mode is
the documented release default. The pinned Cython build also passed its runtime
identity and serial-contract checks, exposed `mb.serial`, and passed a
real-ROM attach/detach/close smoke; full trade/battle acceptance has not been
run in Cython mode. Use
explicit ROM, symbol, and SHA-1 values; never use `POKERED_SKIP_SHA1=1` for
release evidence.

## Supported-scope boundary

- Canonical color-Red, color-Blue, and Yellow ordinary and derived battle
  fixture bytes have recorded reproduction evidence; the external states are
  never committed and their hashes do not prove gameplay compatibility.
- The current candidate passed the completed 10 local/dedicated trade rows, all
  10 local/dedicated battle rows, and all 9 remote trade rows. Independent
  remote Red/Blue and Red/Yellow battle samples passed 5/5 each; the strict
  remote battle matrix remains in progress.
- The current tree declares all canonical Red/Blue/Yellow listener and
  connector orderings over localhost TCP. The current runtime result is green
  for trade and partial for battle until the strict matrix completes;
  concurrent-load stability remains open.
- The strict acceptance declaration has a dedicated entry point for every
  canonical ordered local and remote pair (19 trade and 19 battle nodes).
  Collection is declaration evidence only; a LinkMenu milestone is not a
  gameplay acceptance. Current execution recorded strict trade 19/19; strict
  battle remains an in-progress runtime requirement.
- Vanilla ordinary and derived fixture rows are `PARTIAL` because their
  source-state provenance is not proven against the vanilla ROM. Stock link
  pairs, other version pairs, and unlisted variants are unsupported or
  unverified until separately gated.
- TCP has no authentication or encryption and is enforced as loopback-only;
  never expose it to a LAN, WAN, or public address.

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
