# Production-readiness orchestration prompt: `poke-harness`

You are the lead delivery agent for the repository containing this prompt.
Resolve its root with `git rev-parse --show-toplevel`; do not hard-code a
machine-specific checkout path. Make this Pokémon Red/Blue/Yellow PyBoy MCP
harness production-ready by orchestrating independent subagents for maximum
safe development speed.

## Non-negotiable operating rules

- Read and obey `AGENTS.md` or `agents.md`, `README.md`, `VERSIONS.md`, and
  the relevant production runbook before changing anything.
- Preserve the shared checkout. Never run `git reset`, `git clean`, destructive
  checkout commands, broad staging, force-push, or merge. Do not overwrite
  unrelated user changes.
- Use an isolated worktree for every implementation lane. Each subagent may
  commit only its own scoped files and must return its commit SHA, changed-file
  list, tests, and blockers. The lead integrates only reviewed commits.
- Do not put ROMs, symbols, save states, screenshots, logs, credentials, or
  machine-local absolute paths in version control.
- Do not call a hook, LinkMenu milestone, synthetic protocol test, RAM-mutated
  fixture, skipped test, xfail, timeout, or partial matrix “production-ready.”
- If a required capability fails, keep the failure visible, record exact
  evidence, and report `BLOCKED`; do not weaken the assertion or hide the test.

## Phase 0 — lead reconnaissance

Before spawning implementation work, the lead must:

1. Record cwd, branch, HEAD, dirty paths, Python/interpreter identity, and
   whether ROM/SYM/fixture assets are present.
2. Read the package metadata, MCP config, runtime/link modules, tests, and
   release documentation.
3. Build a dependency map and identify files owned by each lane. Resolve
   overlapping ownership before launching agents.
4. Create a short baseline report containing the exact commands and observed
   failures. Do not make unrelated formatting changes.

## Phase 1 — parallel subagent lanes

Launch these lanes concurrently when their inputs are independent. Each lane
must work in its own worktree and return a concise handoff using the protocol
below.

### Lane A — runtime and packaging

- Pin and verify the exact PyBoy source/runtime revision required by the link
  layer; ensure the runtime used by tests is the runtime used by MCP.
- Make editable installs and wheels portable; remove local-only path hacks and
  stale standalone-PyBoy assumptions.
- Verify Python 3.11+, dependency resolution, package data, import paths,
  optional native/Cython mode, and fail-closed runtime identity checks.
- Add or repair clean-install and wheel smoke tests without bundling game ROMs.

### Lane B — serial and link reliability

- Audit bit-accurate serial semantics, internal/external clock roles, IRQs,
  re-arm behavior, scheduling, disconnects, deadlines, and teardown.
- Validate in-process local links and TCP links with real subprocesses where
  applicable. Eliminate deadlocks, stale-byte fallbacks, unbounded queues, and
  leaked threads/sockets.
- Keep semantic/RAM shortcuts clearly diagnostic and out of production
  acceptance tests.
- Return a supported ROM/version matrix, timing evidence, and known failures.

### Lane C — MCP and lifecycle hardening

- Audit single-session, local-pair, listener, connector, status, disconnect,
  close, and restart flows.
- Ensure MCP stdout remains valid JSON-RPC and diagnostics go to stderr.
- Validate explicit ROM/SYM/SHA-1 configuration, peer identity, bounded
  operations, error propagation, cleanup, and repeated-session behavior.
- Add focused tests for malformed configuration, version mismatch, timeout,
  disconnect, and resource cleanup.

### Lane D — acceptance gate and CI

- Define deterministic markers and a production gate with separate unit,
  local-ROM, remote-ROM, trade, battle, and timing tiers.
- Missing assets must be `BLOCKED`, not silently green. Required tiers must
  reject skips, xfails, xpasses, failures, errors, and timeouts.
- Add strict stateful acceptance checks: a natural trade must compare the
  game-owned party records before/after; a battle must exchange moves and
  resolve a real turn. No test-only RAM mutation or protocol bypass is
  allowed in these checks.
- Ensure test subprocesses use the exact production interpreter/runtime and
  leave no child process or socket behind.

### Lane E — documentation and release evidence

- Reconcile README, runbook, version pins, MCP examples, fixture instructions,
  security boundaries, supported matrix, and release checklist with observed
  behavior.
- Document exact commands, hashes, runtime/build identity, fixture provenance,
  deadlines, teardown results, and unsupported combinations.
- Separate implemented behavior, diagnostic milestones, candidate paths, and
  release-certified behavior. Do not invent pass counts or timing.

### Lane F — independent security and quality review

- Review all lanes for path traversal, unsafe subprocess/environment use,
  plaintext network exposure, missing authentication boundaries, secret/log
  leakage, resource exhaustion, thread races, and broad filesystem effects.
- Check for hidden absolute paths, generated artifacts, stale docs, dead code,
  flaky sleeps, weak assertions, and accidental test bypasses.
- Produce findings ranked P0–P3 with file/line evidence and concrete fixes.

## Phase 2 — dependency-ordered integration

The lead integrates and verifies in this order:

1. Runtime/package contract (A)
2. Serial/local/remote link behavior (B)
3. MCP lifecycle (C)
4. Acceptance classification and CI gate (D)
5. Documentation/release evidence (E)
6. Independent review fixes and final audit (F)

After every integration step, re-query the working tree and rerun the narrow
tests covering the changed contract. Resolve conflicts by preserving user
work and lane ownership; never take an entire worktree wholesale when files
overlap.

## Required verification matrix

Run from a clean isolated release worktree with the same interpreter used by
the package and MCP:

- dependency install, `pip check`, import/runtime identity, and wheel smoke;
- collection through both `python -m pytest` and the console script;
- all ROM-free unit/protocol/transport tests;
- one-session MCP stdio and explicit SHA-1 validation;
- local real-ROM link construction, stepping, disconnect, and teardown;
- remote TCP listener/connector handshake, LinkMenu, timeout, and teardown;
- natural local trade with before/after party-record equality checks;
- natural local link battle through move exchange and turn resolution;
- natural remote trade with both independent processes receiving the peer’s
  complete party record;
- repeated timing-sensitive runs sufficient to expose scheduling flakes;
- lint, compile checks, and repository artifact/path audit.

Use explicit deadlines for every emulator/network test. Capture complete
output, not only a pass count. Test every claimed ROM/version row or list the
row as unsupported. A required failure leaves the overall result `BLOCKED`.

## Subagent handoff protocol

Every subagent must return:

```text
Lane: A|B|C|D|E|F
Status: PASS | PARTIAL | BLOCKED
Commit: <sha or none>
Owned files changed: <explicit paths>
Tests run: <exact commands and results>
Evidence: <runtime/ROM/SYM/fixture identity and relevant output>
Risks or blockers: <specific, reproducible details>
Next action: <smallest lead action>
```

The lead must maintain a live integration ledger mapping each requirement to
its owner, commit, test evidence, and current status. Do not close the task
until the ledger, gate output, docs, and final dirty-tree audit agree.

## Final response

Report:

1. implemented changes grouped by lane;
2. exact verification results and runtime/asset identity;
3. supported and unsupported capabilities;
4. security and operational limitations;
5. uncommitted/user-owned paths preserved; and
6. a clear final decision: `PRODUCTION-READY`, `PARTIAL`, or `BLOCKED`.

Only use `PRODUCTION-READY` when every required gate is green in a clean
reproducible checkout. If any required natural trade, battle, runtime,
security, teardown, or packaging condition is unproven, say exactly what is
blocked and the smallest next repair.
