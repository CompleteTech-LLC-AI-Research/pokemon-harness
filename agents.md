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

The release runtime is the bundled PyBoy source snapshot pinned in
`VERSIONS.md` (`2.7.0`, harness revision
`c565df66c3731fad2856169a90f6bbec99925915`) with `mcp==1.29.1`. The source
runtime is the only link-acceptance mode currently exercised in the passing
evidence. A Cython build
that hides `mb.serial` is diagnostic until it passes its own attachment and
acceptance checks. Use explicit ROM, symbol, and SHA-1 values; never use
`POKERED_SKIP_SHA1=1` for release evidence.

## Supported-scope boundary

- The passing local stateful evidence is color Red + Yellow for the strict
  trade and battle cases; this does not certify every version pairing.
- The passing remote transport evidence is color Red as listener/internal-clock
  and color Blue as connector/external-clock, using localhost TCP; the current
  remote transport tier is 13/13. The strict remote trade and battle paths also
  pass, but concurrent-tier load stability is open.
- The exact-head remote battle smoke selects LinkMenu with ordinary directional
  and A input and exercises native serial move exchange; it does not certify
  every remote battle row or repeated-load stability.
- Blue/Blue and Blue/Red local battle rows are known diagnostic stalls. Stock
  link pairs, other version pairs, reversed roles, and unlisted variants are
  unsupported or unverified until separately gated.
- TCP has no authentication or encryption and is enforced as loopback-only;
  never expose it to a LAN, WAN, or public address.

## Verification and handoff

Use `scripts/production_gate.py` with the same interpreter used by installation
and MCP. Required real-ROM tiers must have no skips, xfails, failures, errors,
or timeouts; `--unit-only` is not a release gate. Retain complete output with
commit, runtime, ROM/SYM/fixture hashes, fixture provenance, roles, deadlines,
and teardown results. Every subagent handoff must state its diagnosis, owned
files, exact commands/results, risks, commit/worktree, and smallest next step.

The final report must distinguish implemented behavior, controlled acceptance,
diagnostic evidence, unsupported rows, security limits, and the exact release
decision. Use `PRODUCTION-READY` only after every required gate is green in a
clean reproducible checkout.
