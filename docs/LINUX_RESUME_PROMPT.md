# Resume prompt: full-Linux production qualification

You are the lead engineering agent. Finish making the source-only Pokemon
Red/Blue/Yellow PyBoy MCP harness production-ready on a stable, full Linux
machine. Do not redefine success as passing unit tests or one gameplay row.

## Repository and starting points

- Repository: `https://github.com/CompleteDotTech/pokemon.git`
- Start from `origin/sync/linux-resume-20260911`.
- Its starting upstream base was `64479917393871beeeaaa3484b80928a0975c651`.
- Pending network work: `origin/wip/linux-resume-network-20260911`, commit
  `3684594` (six files; deliberately NOT integrated or release-qualified).
- Pending documentation: `origin/wip/linux-resume-pacing-docs-20260911`, commit
  `56f59d7` (apply only with the matching network changes).
- Archived unpublished regressions: `origin/archive/linux-resume-tests-20260911`,
  commit `2dc3777`. Read `docs/ARCHIVED_TESTS_20260911.md` on that branch. Its four
  older tests are preserved for review/porting, not integrated or qualified;
  do not switch the production resume base to this archival branch.
- Read `agents.md`, `VERSIONS.md`, `docs/PRODUCTION_RUNBOOK.md`, and
  `docs/RELEASE_CHECKLIST.md`. Older results in those files are historical,
  not proof for this checkpoint. The release remains PARTIAL.

Use a fresh checkout and isolated worktrees. Preserve existing user work;
never reset, clean, stash, or broadly stage a dirty checkout. Do not merge or
modify `master` without explicit operator authorization. The checkpoint
publication did not authorize a merge. Do not replace the resume branch with
an older shared-checkout snapshot or a WIP branch.

Follow the repository's managed-delegation policy: coordinator at Level 1,
disjoint subagents at Level 2, bounded independent verifiers at Level 3, and
no Level 4. Default subagents to `gpt-5.6-luna`, maximum reasoning, fastest
available inference. If descendant tools are unavailable, report that limitation
honestly; do not claim a three-level review occurred. Integrate only after
independent, evidence-backed review.

## Portable setup and private inputs

```bash
git clone --branch sync/linux-resume-20260911 \
  https://github.com/CompleteDotTech/pokemon.git poke-harness
cd poke-harness
git fetch origin
python3 -m venv .venv-source
. .venv-source/bin/activate
python -m pip install -e '.[dev]'
python -m pip check
python scripts/bootstrap_pyboy.py --mode source --check
```

The contract is Python >=3.11, MCP 1.29.1, and the bundled patched PyBoy at
the revision in `VERSIONS.md` (checkpoint: harness-local divergence revision
`eceaa3bb15dedd6847a3a37d3400421e3024cb5c`; upstream base `c565df66c3731fad2856169a90f6bbec99925915`).
Stock PyBoy is not a substitute for its serial backend API. Create a SEPARATE
clean environment for native Cython; use `scripts/bootstrap_pyboy.py --mode
cython` and its `--check` command there. Follow the runbook for build prerequisites.
Verify actual module paths: source must use the bundled Python modules;
native must use the installed extension modules. Record interpreter versions.

The checkpoint already tracks `vendor/pyboy-src` as ordinary source files,
not a Git submodule. No separate PyBoy checkout is needed. Native compilation
also requires Python development headers and working POSIX semaphores on
Linux: a read-only `/dev/shm` prevents the pinned Cython build from starting
its compiler workers. Treat that as a host prerequisite failure, not a
successful native installation.

The Git branches contain no ROMs, symbols, saves, screenshots, or raw gameplay
logs. Obtain legally supplied operator assets separately. Do not download ROMs,
commit generated game data, or manufacture fixtures by writing game RAM.
Expected layout is `rom/{red,blue,yellow}/` and
`tests/fixtures/link/{red,blue,yellow}/`, as documented in `VERSIONS.md`.
Trade and battle require the matching Cable Club fixtures, including battle
variants; vanilla and color save states are not interchangeable.

Validate ALL supplied ROM, symbol, and fixture bytes against the pins and
provenance manifest before live tests:

```bash
python scripts/validate_fixture_manifest.py \
  --fixture-root "$PWD/tests/fixtures/link"
```

Also independently verify the ROM and symbol hashes against `VERSIONS.md`.
Direct local acceptance tests validate ROM hashes but do not independently
hash every symbol/save input. Do not mistake schema-only validation for byte
validation. Missing private inputs are an operator dependency, not grounds
to silently skip production acceptance or invent passing evidence.

## What this checkpoint actually contains

The resume branch integrates the reviewed FRAME_DONE latch regression fix,
native owner-pump and explicit-pair epoch optimizations, pair-owner-only local
acceptance stepping, transactional session creation and teardown, live demo
cleanup, MCP environment-isolation regression, deterministic queue-admission
tests, timing/request-capacity test repairs, and the Unix socket test-path fix.

The receptionist test driver now uses ordinary inputs with this cadence:
press A on endpoint A, pair-owner advance 4 frames, press A on endpoint B,
pair-owner advance 16 frames. It preserves the 20-frame attempt and 2400-frame
budget, chooses the serial/coarse phase once per attempt, and rejects invalid
attempt sizes. No game RAM or serial role registers are written by this driver.

The local battle driver now reads menu cursor/readiness fields and sends
ordinary directional input only to an endpoint not yet on Colosseum. It
preserves the 60-frame settle plus at most 40 cursor-navigation frames and
keeps the strict cursor assertion. Advancement remains through the pair owner.

## Retained evidence and its limits

These are completed results, not predictions for a fresh installation:

- Post-Unix-fix UNIT tier: source Python 3.11.15, **4492 passed**, zero failed,
  skipped, xfailed, xpassed, or errors; 233.5 seconds.
- Same UNIT tier: native Python 3.12.13, **4492 passed**, the same zero counts;
  220.0 seconds. Runtime, collection, and unit policy checks passed.
- Those full unit results PRECEDE the final battle-driver addition. After
  that addition, its 26 contract cases passed independently under both runtimes;
  integration-local native verification also passed 26/26. Rerun the full gate.
- `tests/test_timed_remote.py`: **135 passed** under each runtime after the
  Unix path fix. The old absolute-path mutant failed before the intended socket
  rejection when the temporary parent path exceeded AF_UNIX's address limit.
- Native `test_red_yellow_trade_swaps_real_party_records`: **1 passed in
  215.86 seconds**. Different initial species, both received exact 44-byte
  party records, unchanged counts, and terminators were asserted. Normal
  detach/close cleanup completed. ROM, symbol, and fixture hashes were checked
  separately. This proves one local row, not source parity or TCP.
- Native `test_red_yellow_battle_turn_is_resolved`: **1 passed in 282.43
  seconds**, using the reviewed battle-driver files and the integration
  checkout's production modules. The subsequently integrated driver hashes
  matched the reviewed/tested files. This is the test's stated hook-based
  acceptance boundary; independently audit whether it proves every battle
  settlement invariant required by the final release.
- Earlier native Blue/Blue trade-flow testing passed, but the starting parties
  were identical. It is NOT distinct-party swap proof and does not replace the
  strict Red/Yellow result or the full matrix.

The earlier battle failure was concrete: both peers reached LinkMenu, but
after a fixed simultaneous DOWN input the cursors were Red=1, Yellow=0.
The reviewed cursor-feedback driver fixed that observed case. Do not weaken
the acceptance assertions or use forced menu hooks to obtain a pass.

Repeated environment restarts interrupted longer gates and removed live
processes. Completed disk reports were retained; incomplete runs must not be
counted as complete gates. A previous interrupted source run had 4486 passes
and two Unix-path setup failures; both were subsequently fixed. Do not rerun
an observed process solely because a polling call times out: first inspect its
actual handle/PID or terminal report. On Linux, keep reports on durable disk,
outside Git, and capture terminal exit status as well as stdout.

## Immediate work: pending network candidate

Inspect `git show 3684594` and compare it with the resume branch. Do NOT
overwrite whole files from the WIP branch or blindly resolve conflicts in its
favor. Its base lacks important fixes already on the resume branch.

The candidate removes unconditional host writes to `SB`/`SC` on network attach,
HELLO, and public role negotiation. Pacing roles remain metadata: Red leads
differing Red/Blue pairs; non-Yellow leads Yellow cross-family pairs; identical
versions retain the supplied orientation. ROM code must own native serial
registers, clock changes, and `hSerialConnectionStatus`.

Independent review found and reproduced a deadlock when those hardware roles
oppose pacing metadata: the pacing leader sends FRAME_DONE and waits for ACK
while the peer waits for an EDGE_RESP queued after the leader's last pump.
The candidate services owner edges during ACK waiting and supplies owner
progress/re-arm callbacks for both pacing roles. It also adds bounded
cancellation and a two-byte synthetic owner regression.

Remaining integration conditions:

1. PRESERVE the resume branch's `frame_done_received` latch while incorporating
   the new callback/cancellation hunks. The WIP branch alone consumes FRAME_DONE
   before pending edge accounting drains; the root latch regression produced
   **1 failed, 2 passed** against that standalone candidate.
2. Preserve the resume branch's owner-pump empty-queue optimization and explicit
   local-pair epoch-check optimization in `pyboy_link_session.py`.
3. Fix new regression-test teardown in `test_network_backend.py` and
   `test_serial_ownership.py`: guard joins on unstarted threads; attempt cleanup
   of both peers even if one operation fails; fail on cleanup errors after an
   otherwise successful test; preserve primary failures with cleanup notes or
   aggregation. Current warning-only detach failures are insufficient.
4. Independently review those cleanup corrections, then apply only the intended
   candidate hunks and rerun the combined affected suites.
5. Focused WIP tests were 135/135 under source and native (backend 72, CPU owner
   2, link session 55, serial ownership 6). These do NOT include the resume
   branch's additional regressions and are not a qualification of the overlay.
6. `_Barrier*Core` tests use fakes. Do not label them native-ROM, live TCP,
   authentic trade, or battle proof. Run real TCP after integration.
7. Apply the reviewed `56f59d7` documentation changes only after matching code
   is integrated. They distinguish pacing metadata from ROM clock ownership
   and retain PARTIAL/not-production-ready boundaries.

## Verification commands and full completion scope

First rerun the narrow changed modules under BOTH runtimes, including local
driver contracts, network backend/latch, serial ownership, PyBoy link session,
CPU owner, MCP lifecycle, and timed-remote tests. Then run the production gate:

```bash
# Set these to the two fresh interpreters and private inputs on THIS machine.
SOURCE_PYTHON="$PWD/.venv-source/bin/python"
NATIVE_PYTHON="$PWD/.venv-native/bin/python"
POKE_EVIDENCE_PARENT="${XDG_STATE_HOME:-$HOME/.local/state}/poke-harness-evidence"
mkdir -p "$POKE_EVIDENCE_PARENT"
EVIDENCE_DIR="$(mktemp -d "$POKE_EVIDENCE_PARENT/run-XXXXXX")"
"$SOURCE_PYTHON" scripts/production_gate.py \
  --repo-root "$PWD" --rom-root "$PWD/rom" \
  --fixture-root "$PWD/tests/fixtures/link" \
  --python "$SOURCE_PYTHON" --cython-python "$NATIVE_PYTHON" \
  --runtime-mode both --repeat-timing 5 --matrix-workers 1 \
  --evidence-dir "$EVIDENCE_DIR"
```

Increase matrix parallelism only with enough CPU/memory and retained evidence
that scheduling-sensitive tests remain valid. Do not trade correctness for a
shorter wall-clock run. Use `--tier` selections to diagnose failures, but do not
present those as a full successful gate. The default tiers are unit, local,
remote, strict trade, strict battle, and fivefold timing.

Retest both dedicated local Red/Yellow acceptance nodes, all supported ordered
Red/Blue/Yellow local and TCP rows, reversed listener/connector directions,
ROM variants supported by valid fixtures, and MCP stdio lifecycle. The current
declaration is 19 strict trade and 19 strict battle entrypoints PER runtime:
nine local, nine TCP, and one dedicated Red/Yellow assertion for each operation.
Collection counts alone are not execution evidence.

Finish every original lane:

- Runtime/packaging: reproducible clean install and launch, portable `.mcp.json`,
  correct default patched runtime, explicit source/native contract, clean-install
  verification, and no hidden machine paths or accidental asset packaging.
- Serial/local: authentic handshake, nybble exchange, menu and data blocks,
  save/load state, callback deregistration, ownership, trade and battle behavior.
- TCP: bounded synchronization/backpressure/cancellation; no EOF, duplicate,
  timeout, reconnect, or shutdown deadlocks; no host game-state bypasses.
  Keep localhost-only enforcement unless authenticated encrypted cross-host
  support is deliberately authorized and implemented.
- MCP: startup, initialize, list_tools, step, press, state resources, save/load,
  local pair/unpair, listen/connect/status/disconnect; serialized emulator access,
  structured errors, and teardown with no retained sessions/threads/sockets/hooks.
- Tests: zero unexplained failures/xfails/skips in acceptance; repeat timing
  tests at least five times; exact asset/runtime/fixture and teardown evidence.
- Documentation/release: reconcile all current claims with fresh results,
  distinguish single-session/local/transport/trade/battle readiness, and keep
  Option-B/RAM-writing walkthroughs explicitly diagnostic, never human-valid
  gameplay evidence. Review whether battle hooks alone prove settled damage;
  strengthen acceptance if needed rather than merely trusting test names.

Final qualification requires a clean committed candidate, clean-machine setup,
green current full gates, complete supported matrices, matching private asset
hashes, bounded remote teardown, no known leaks/flakes/deadlocks, and accurate
documentation. Report exact commits, commands, pass/fail/skip counts, supported
matrix, changed files, and unresolved limitations. Keep local, pushed, merged,
and runtime-verified states separate. If blocked by missing inputs or machine
access, request the smallest explicit operator action and continue independent
work; never declare readiness from historical results or mocks.
