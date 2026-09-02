# Production runbook

This runbook defines how to prepare and evaluate a clean `pokered-harness`
checkout. The baseline at `e219fb5` was not certified. The live target is
[`CompleteDotTech/pokemon`](https://github.com/CompleteDotTech/pokemon); the
current audited repository head is `8727779` (PR #28 documentation merge), and
the latest merged implementation commit is `b2a4016` (PR #27, merge `b96ee77`),
with the native Cython ABI fix in `94f103e` (PR #26), MCP lifecycle hardening
`7f3c2f4` (PR #22), and implementation candidate `dfc0b2a` (PR #19, following
PR #17 and PR #18). External BYO assets are excluded from the tracked source
tree.

The complete all-tier source-runtime production gate passed collection 698,
unit 554/554, local real-ROM 47/47, remote transport/MCP 15/15, strict trade
19/19, strict battle 19/19, and timing 40/40 in each of five repetitions. That
is the PR #17 baseline. The PR #19 follow-up additionally passed a 555/555
ROM-free unit slice, timing 40/40 in each of five repetitions, focused
transport/MCP 167/167, a bounded concurrency probe 8/8 in each of five
repetitions, and a 15/15 real-ROM remote transport slice. The ten-entry
fixture-manifest schema passed. Sanitized evidence is retained outside version
control because ROMs, symbols, and save states are operator-supplied assets.

Fresh Windows scoped evidence also covers install, source/Cython bootstrap, MCP
stdio, and three-ROM Cython lifecycle checks; it does not complete strict
Cython gameplay, real-ROM concurrent load, the remote trade/battle matrix, or
macOS coverage.

PR #22 adds bounded MCP stdio unpair cleanup and fresh remote lifecycle
generation tracking. Its release-hygiene workflow passed; the full strict
post-change gameplay matrices remain open.

The merged-head source-runtime gate used managed Linux Python 3.12.13 and
Pytest 9.1.1: collection 706, unit 562/562, and timing 40/40 in each of five
repetitions, with no skips, xfails, failures, errors, or timeouts. The same
unit/timing scope passes under the explicitly selected Cython runtime, whose
probe resolves all five required PyBoy modules as native extensions. ROM-backed
tiers were not run by either asset-free command.

A fresh `python -m pytest -q -ra` run in the same clean asset-free checkout
completed 566 passed, 140 expected BYO-asset skips, and 2 warnings. It proves
the test suite can complete without ROMs; it is not a release result because
the skipped real-ROM tiers still require their assets.

The baseline gate ran from isolated source head
`df0e7424c87c812a57f257286b0dc00e87c498f4`, whose implementation tree is the
merged PR #17 candidate. Recorded tier durations were unit 17.8 seconds,
local 1,078.8 seconds, remote 96.2 seconds, trade 5,037.4 seconds, battle
6,909.8 seconds, and timing 18.7 seconds. The post-PR #19 ROM-free follow-up
ran from the merged candidate with collection 699; its release-hygiene check
also runs the bounded concurrency probe.
Recorded post-PR #27 source and Cython `blue-blue` remote LinkMenu smokes pass
after PR #27 changes game-driven exchange pacing to a bounded 30-second
timeout. The post-PR #27 native strict trade
qualification returned `FAIL`: 16/19 rows passed and three color-variant
subprocess rows failed in the trade-center/rendezvous path. The native strict
battle rerun has not been run, and the full post-fix source matrix and broad
runtime evidence remain open. Overall status is `PARTIAL`; the exact open
items are listed in [the release checklist](RELEASE_CHECKLIST.md).

Status semantics:

- `PASS` is scoped to the named command and means clean collection plus no
  failure, error, skip, xfail, or timeout in that selected scope.
- `PARTIAL` means that controlled evidence exists but a required release
  condition remains open.
- `PRODUCTION-READY` requires the full gate with all required BYO assets,
  complete strict matrix declaration and runtime coverage, retained evidence,
  and a clean candidate with no open blocker.

## 1. Start from a clean checkout

Use a fresh clone or an isolated worktree. Do not use a dirty development
checkout as release evidence.

```bash
git clone <repository-url> poke-harness
cd poke-harness
git status --short
git rev-parse HEAD

python3 --version
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip check
python -c 'import pokered_harness, pyboy; print(pokered_harness.__file__); print(pyboy.__version__, pyboy.__pokered_harness_revision__)'
python scripts/bootstrap_pyboy.py --mode source --check
```

The standard-library `venv` module must include `ensurepip`. On Debian or
Ubuntu, install the matching OS package (for example, `python3.12-venv` or
`python3-venv`) first if `python3 -m venv` reports that `ensurepip` is
unavailable. These commands assume the resulting environment provides
`python -m pip`; an environment created by another tool must provide the same
pip/install contract before it is used for the release gate.

For a historical reproduction, revision
`1046a541e0003923aec6000b6b383c6eaafeaa48` must be present before collecting
the snapshot described above. For a current release candidate, record the
actual candidate commit and rerun every affected tier; do not layer new
runtime or test changes over the historical result. The working tree should
be clean; ignored BYO assets may be present outside the tracked source. Use
the same activated interpreter for installation, tests, the gate, and MCP.

The required Python version is 3.12 or newer. On Windows PowerShell, create
the same environment with `py -3 -m venv .venv`, activate with
`.venv\Scripts\Activate.ps1`, and use `python -m pip` for the remaining
commands. Run the tests and the MCP server with the same interpreter.

If `uv` is the environment manager, the lockfile-resolved equivalent is:

```bash
uv venv --seed .venv
uv sync --locked --extra dev
uv pip check
uv run python scripts/bootstrap_pyboy.py --mode source --check
```

The standard-library path is the portable baseline. The `uv` path additionally
uses `uv.lock` for transitive dependency resolution. Neither path installs ROMs,
symbols, or save states; those remain operator-supplied BYO inputs.

The default installed runtime is PyBoy source mode, verified with:

```bash
python scripts/bootstrap_pyboy.py --mode source --check
```

The pinned fork has an optional Cython mode. Its pinned build, semantic
serial-contract probe, and three canonical real-ROM attach/step/close smokes
pass. PR #23 exposes the CPU and LCD timing fields required by native lockstep
scheduling, and PR #26 makes native `PyBoy.tick` instance ownership writable.
PR #27's gate selects the requested runtime explicitly and fails closed if the
interpreter resolves the wrong PyBoy module kind. The merged-head Cython
unit/timing gate passes; a recorded `blue-blue` remote LinkMenu smoke also
passes. The post-PR #27
native strict trade qualification is `FAIL` at 16/19, with three
color-variant subprocess failures in the trade-center/rendezvous path; native
strict battle has not been run. A
fresh Windows Python 3.12.10 environment also passed the
earlier Cython build/check and three-ROM attach/tick/close smokes. This is
scoped Windows evidence, not full native gameplay, concurrent-load,
remote trade/battle, or macOS qualification. Do not substitute an arbitrary
standalone PyBoy wheel: record the version, harness revision, and runtime mode,
and require the serial contract before running link tests.

The repository is source-only. Obtain ROMs and symbols legally and keep them
outside version control. The `.gitignore` intentionally excludes ROMs, symbol
files, save states, and walkthrough output.

## 2. Install matching ROM and symbol inputs

Create this layout:

```text
rom/
├── red/
│   ├── pokemon-red.gb
│   ├── pokemon-red-color.gb       # optional candidate variant
│   └── pokemon-red.sym
├── blue/
│   ├── pokemon-blue.gb
│   ├── pokemon-blue-color.gb      # optional candidate variant
│   └── pokemon-blue.sym
└── yellow/
    ├── pokemon-yellow.gbc
    └── pokemon-yellow.sym
```

The real-ROM acceptance tiers additionally require these ignored save-state
files:

```text
tests/fixtures/link/
├── red/
│   ├── cable_club.state
│   ├── cable_club-battle.state
│   ├── cable_club-vanilla.state
│   └── cable_club-battle-vanilla.state
├── blue/
│   ├── cable_club.state
│   ├── cable_club-battle.state
│   ├── cable_club-vanilla.state
│   └── cable_club-battle-vanilla.state
└── yellow/
    ├── cable_club.state
    └── cable_club-battle.state
```

The default `cable_club.state` files support the ordinary Cable Club path; the
`cable_club-battle.state` files are separate derived battle-start states. The
two `*-vanilla.state` pairs are recorded in the manifest but remain partial
because their ordinary source-state provenance is not established. The
production gate preflight checks the three canonical ordinary files, strict
acceptance tests require the three canonical battle files, and the manifest
byte validator checks all ten listed entries. A missing required file or a
manifest hash mismatch is a blocked/failed release result, never a passing
skip.

For the currently controlled stateful scope, the BYO asset set is:

- five pinned ROMs: stock and color Red, stock and color Blue, and Yellow;
- three matching symbol files: Red, Blue, and Yellow; and
- six canonical color-Red, color-Blue, and Yellow ordinary/battle states.

Supply the two vanilla ordinary/battle pairs as well when validating the
checked-in ten-entry manifest. They are not a supported release scope until a
vanilla-ROM-matching source state is supplied and reproduced.

Compare every ROM used by a test or launch to the corresponding SHA-1 in
[`VERSIONS.md`](../VERSIONS.md). For example:

```bash
sha1sum rom/red/pokemon-red.gb rom/red/pokemon-red-color.gb
sha1sum rom/blue/pokemon-blue.gb rom/blue/pokemon-blue-color.gb
sha1sum rom/yellow/pokemon-yellow.gbc
sha1sum rom/red/pokemon-red.sym rom/blue/pokemon-blue.sym rom/yellow/pokemon-yellow.sym
sha1sum tests/fixtures/link/*/*.state
```

Then validate the manifest's exact sizes and SHA-1/SHA-256 values:

```bash
python scripts/validate_fixture_manifest.py \
  --fixture-root "$PWD/tests/fixtures/link"
```

The matching `.sym` file must come from the matching game source/build. Use the
source commits and RGBDS `v1.0.1` provenance recorded in `VERSIONS.md`, and
record the symbol-file hash, source commit, RGBDS version, and build flags in
the release evidence. A symbol file being readable is not proof that its
labels match the ROM.

## 3. Run the clean-tree gate

Run collection first so missing dependencies and import failures are visible:

```bash
python -m pytest --collect-only -q
python -m pytest -q -ra
```

The release gate requires collection to complete without errors. It also
requires no unexpected skips, xfails, failures, or timeouts in any tier marked
required below. Fixture-gated tests may be skipped during development, but a
skip is not a passing release result.

The complete gate command uses the same interpreter as the activated
environment and keeps its report outside the checkout:

```bash
EVIDENCE_DIR="$(mktemp -d)"
POKERED_ROM_ROOT="$PWD/rom" \
POKERED_FIXTURE_ROOT="$PWD/tests/fixtures/link" \
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --rom-root "$PWD/rom" \
  --fixture-root "$PWD/tests/fixtures/link" \
  --python "$(command -v python)" \
  --runtime-mode source \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

For the asset-free smoke used to validate a source-only checkout, run the
scoped gate explicitly:

```bash
EVIDENCE_DIR="$(mktemp -d)"
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --runtime-mode source \
  --unit-only \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

The merged-head clean asset-free source gate collected 706 tests, passed unit
562/562, passed timing 40/40 in each of five repetitions, and returned scoped
`PASS`. The native Cython gate passes the same unit/timing scope after its
runtime probe resolves all five required PyBoy modules as native extensions. It
also performs schema-only validation of the ten-entry fixture manifest. Because
`--unit-only` selects only `unit` and `timing`, it does not validate ROM bytes,
load save states, exercise MCP with a real ROM, or run link gameplay; it is not
a production sign-off.

For the optional native qualification, first build the pinned fork and then
select Cython explicitly. The gate verifies that all required PyBoy modules are
installed extensions before running any selected tier:

```bash
python scripts/bootstrap_pyboy.py --mode cython
python scripts/bootstrap_pyboy.py --mode cython --check
EVIDENCE_DIR="$(mktemp -d)"
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --runtime-mode cython \
  --unit-only \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

The source default and native command are separate evidence scopes. Do not
describe a source result as native parity, or a native unit/timing result as
real-ROM gameplay coverage.

`--evidence-dir` writes `gate-report.json`, `gate-report.txt`, and
`evidence-manifest.json`. The bundle contains metadata, asset hashes and
sizes, runtime identity, tier results, and bounded/redacted diagnostics; it
does not copy ROM or fixture bytes, inherit environment variables into the
report, or retain absolute local paths. The manifest hashes the two report
files so a retained bundle can be checked for accidental modification. A
failed or blocked run still writes its status and diagnostics. The default
gate checks five pinned ROM paths, three symbol paths, the ten manifest state
entries, and all required real-ROM tiers. It also fails closed when the strict
acceptance matrix declaration is incomplete.

The release workflow uses an explicit Ruff boundary for the production files it
owns and explicitly excludes the pinned third-party `vendor/pyboy-src` tree.
That configured CI boundary is clean; the repository still contains legacy
files outside it, so a broad `ruff check src tests scripts` result is not used
as release evidence. The vendored runtime is covered by revision pinning,
compile/import checks, and the serial contract.

The release tree includes `tests/__init__.py`; otherwise environments that do
not treat `tests/` as a namespace package can fail collection. Run both
invocation forms for every release candidate.

The standalone matrix command is collection-only. It returns zero only when
the required node IDs and strict declaration are present; a zero result still
does not execute ROM gameplay:

```bash
python scripts/tcp_link_matrix.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --format text
```

It proves presence of ordered/parameterized node IDs only. Runtime is always
reported as `NOT RUN`; it must not be described as gameplay coverage.

For a machine-readable report plus the human-readable report and manifest, run
the gate from the repository root:

```bash
EVIDENCE_DIR="$(mktemp -d)"
python scripts/production_gate.py --evidence-dir "$EVIDENCE_DIR"
```

The command fails closed when required ROMs, symbols, fixtures, or acceptance
tests are missing. Keep the evidence directory outside version control. The
stdout `--format json` output remains available for callers that need it, but
the evidence directory is the retained, sanitized bundle.

## 4. Run the evidence tiers

Run the tiers in order and save the complete output with the commit and
interpreter identity.

### Tier A: ROM-free behavior and runtime contract

This tier exercises configuration, state parsing, serial semantics, protocol,
transport, and symbol-loader behavior without commercial game assets:

```bash
python -m pytest -q \
  tests/test_agent_sync.py \
  tests/test_config.py \
  tests/test_game_state.py \
  tests/test_link_protocol.py \
  tests/test_link_symbols.py \
  tests/test_link_transport.py \
  tests/test_network_backend.py \
  tests/test_serial_core.py \
  tests/test_serial_coordinator.py \
  tests/test_serial_link.py \
  tests/test_state_bag.py \
  tests/test_state_battle.py \
  tests/test_state_menu.py \
  tests/test_state_overworld.py \
  tests/test_state_progress.py \
  tests/test_state_status.py \
  tests/test_state_text.py \
  tests/test_symbol_loader.py
```

These tests do not require commercial ROM bytes. In source mode, the gate
prepends the vendored PyBoy runtime pinned in `VERSIONS.md`; in Cython mode it
deliberately omits that vendored path so the selected interpreter's extension
modules are tested. A green Tier A result does not establish emulator, MCP,
trade, or battle compatibility.

### Tier B: one real session and MCP stdio

Use one explicit ROM hash. This example uses stock Red:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=ea9bcae617fdf159b045185467ae58b2e4a48b9a \
python -m pytest -q -ra \
  tests/test_golden_paths.py \
  tests/test_mcp_stdio_integration.py
```

Repeat with the appropriate path, symbol file, and hash for each release input
that will be advertised. The wheel-install probe in this audit verified clean
installation, `pip check`, and bundled runtime identity; it did not run this
real-ROM stdio tier on Linux. The fresh Windows validation separately passed
the selected MCP stdio integration (4/4), but did not replace the full
real-ROM tier. Any real-ROM result must identify the current candidate,
ROM/SYM hashes, and complete test output. These tests prove only the tested
boot/state/MCP surface; they do not prove link gameplay.

### Tier C: real symbol and local-link smoke

With all candidate ROMs and matching symbols available:

```bash
python -m pytest -q -ra \
  tests/test_link_symbols_real_roms.py \
  tests/test_link_integration.py
```

The local integration module's active matrix is fixture-gated and covers
transport milestones. Its pair-smoke test proves construction, hook
installation, a short step, and teardown; its trade test is a fixture-gated
round-trip. Neither result should be generalized to every ROM variant or to
remote play.

### Tier D: local link session, trade, and battle acceptance

The more extensive candidate suite is:

```bash
python -m pytest -q -ra \
  tests/test_pyboy_link_session_roms.py
```

The diagnostic matrix in this module is broader than the release acceptance
assertions. Historical LinkMenu, trade, and battle observations are not current
candidate evidence. The current strict local acceptance is parametrized over
all nine canonical Red/Blue/Yellow ordered pairs:

```bash
python -m pytest -q \
  tests/test_pyboy_link_session_roms.py::test_pair_completes_trade_end_to_end \
  tests/test_pyboy_link_session_roms.py::test_pair_completes_battle_turn
```

They require the pinned source-compatible PyBoy runtime, ROM-specific Cable
Club fixtures, and matching symbols. The trade case compares the complete
game-owned party-mon records before and after the exchange; the battle case
uses a legal derived three-mon fixture and requires both sides to reach move
exchange and turn execution. It does not require the optional damage
calculation hook. Neither case writes party or battle state during acceptance.
A skipped or partially parameterized matrix is not full Red/Blue/Yellow
coverage. The dedicated Red/Yellow assertions remain useful focused checks,
but the production gate also requires every parametrized local row and every
remote row below to execute without skips.

### Tier E: remote transport and subprocess acceptance/diagnostics

First run the transport/serial milestones:

```bash
python -m pytest -q -ra \
  tests/test_link_integration_remote.py
```

This module exercises remote TCP plumbing and selected real-ROM milestones.
Some cases stop at LinkMenu or use controlled menu/fixture setup; that is not
the same as a user-driven full trade or battle.

The separate-process acceptance uses the same bundled source runtime as the
normal package and the canonical color Red, color Blue, and Yellow fixtures:

```bash
python -m pytest -q -ra \
  tests/test_pyboy_link_session_subprocess.py
```

The LinkMenu test is a transport smoke test. The strict subprocess trade and
battle tests use cooperative phase rendezvous, compare complete party-mon
records or execute a battle turn, and are parametrized over all nine ordered
canonical listener/connector pairs. Both payloads travel through native
bit-level serial traffic, and the tests reject the out-of-band exchange
counter. The PR #17 baseline passed the strict trade and battle matrices; the
PR #19 follow-up reran the 15-row remote transport slice and the focused
transport/lifecycle checks, not the full strict trade/battle matrix. Record
both child traces and the exact deadline when investigating a regression.

The ROM-free concurrency/lifecycle probe is:

```bash
python scripts/network_concurrency_probe.py
```

It covers concurrent exchange load, shutdown overlap, receiver start/stop
races, terminal timeouts, resolver rechecks, BYE ordering, and raw-socket
boundaries. It passed 8/8 probes in each of five repetitions for PR #19. This
is synthetic localhost evidence; real-ROM concurrent-load stability remains a
separate release requirement.

The native MCP link path only installs the pinned PyBoy serial backend and
transport callbacks. It must not seed `hSerialConnectionStatus` (the ROM-owned
HRAM status populated by its serial ISR) or install semantic exchange hooks.
The listener and connector provide default native clock roles, then the
versioned HELLO may select the non-Yellow endpoint as the initial internal
clock source for a Yellow/Red or Yellow/Blue pair. The ROM still owns its
connection-status byte and any later role changes. The semantic endpoint
remains a compatibility path for non-native test doubles and is not production
acceptance evidence. A real PyBoy with an incomplete native serial contract
fails closed. Native socket writes, public listener waits, and worker teardown
are bounded; `peer_rom_version` can be supplied to reject an unexpected HELLO
label.

### Strict-matrix accounting

The collection audit verifies declaration shape only. The PR #17 complete
source-runtime baseline executed every declared row; the PR #19 follow-up did
not silently relabel that baseline as a full strict-matrix rerun. The strict
runtime scope is:

| Operation | Local ordered rows | Remote ordered listener/connector rows | Dedicated assertion | Current status |
|---|---:|---:|---|---|
| Trade | 9 | 9 | Red/Yellow party-record swap | Historical PR #17 source baseline: 19/19; post-PR #27 native qualification: `FAIL`, 16/19 with three color-variant subprocess failures in the trade-center/rendezvous path; post-fix source rerun not run |
| Battle | 9 | 9 | Red/Yellow resolved-turn assertion | Historical PR #17 source baseline: 19/19; post-PR #27 native qualification not run; post-fix source rerun not run |

That is 19 strict entrypoints per operation. The remote rows use canonical color
Red, color Blue, and Yellow profiles; listener/connector order is significant.
The PR #17 baseline completed both strict matrices with no failed rows. The
full baseline gate recorded source-runtime remote rows, listener/connector
roles, bounded deadlines, and clean child teardown. Historical incomplete
18/19 trade and 17/19 battle snapshots are retained only as historical context;
rerun the full strict matrices after any further runtime change.
Stock-ROM rows and LinkMenu-only milestones are outside this strict release
claim.

## 5. Generate link fixtures safely

Save states are emulator artifacts and are tied to the exact ROM bytes. Keep
them local under `tests/fixtures/link/<version>/` and never commit them.

The bounded, pinned ordinary-fixture producer is:

```bash
python scripts/produce_cable_club_fixture.py --help
```

Pass a source state captured against the same ROM variant, rather than relying
on automatic sibling-worktree discovery. Example:

```bash
python scripts/produce_cable_club_fixture.py \
  --version yellow \
  --variant cgb \
  --source <yellow-cerulean-pc.state> \
  --rom rom/yellow/pokemon-yellow.gbc \
  --sym rom/yellow/pokemon-yellow.sym \
  --out tests/fixtures/link/yellow/cable_club.state \
  --timeout-seconds 180 \
  --max-movement-steps 64
```

For stock Red or Blue use `--variant vanilla`, a vanilla-ROM-captured source,
and an output named `cable_club-vanilla.state`. For the default Red/Blue color
variant use `--variant color` and `cable_club.state`. There is no separate
Yellow producer. Successful fixture generation proves only that the state
lands at the producer's expected map/tile; it does not prove a trade or battle.
The producer validates the selected ROM and symbols against `VERSIONS.md`,
uses the pinned source PyBoy version and fork revision, and fails when either
the wall-clock or movement budget is exhausted. It does not set or require
`POKERED_SKIP_SHA1=1`.

The tracked battle-fixture utility derives immutable battle-start states from
ordinary fixtures. It copies the lead record into a legal multi-mon party,
repairs zero-PP lead moves, validates the result, and closes the emulator
before writing the output. Acceptance loads the resulting file and does not
perform this preparation in emulator RAM. Prepare the canonical color rows in
an external output root with:

```bash
BATTLE_OUTPUT_ROOT="$(mktemp -d)"
python scripts/prepare_battle_cable_club_fixtures.py \
  --repo-root "$PWD" \
  --rom-root "$PWD/rom" \
  --fixture-root "$PWD/tests/fixtures/link" \
  --output-root "$BATTLE_OUTPUT_ROOT" \
  --variants red_color blue_color yellow
```

The two vanilla battle rows may be generated with `red_gb` and `blue_gb`, but
they remain `PARTIAL` until their ordinary source state is proven to match the
vanilla ROM. The exact ten-entry hashes, source-state records, and status
values are in the fixture manifest. Validate them with:

```bash
python scripts/validate_fixture_manifest.py --schema-only
python scripts/validate_fixture_manifest.py \
  --fixture-root "$PWD/tests/fixtures/link"
```

## 6. Launch MCP explicitly

Use explicit paths and the matching hash. The server also indexes the
per-ROM `Path`/`SHA-1` rows in `VERSIONS.md` and fails closed when no matching
pin exists:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red-color.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=e1deed63080bc24cad5fba18ecb3184f905d16d4 \
python -m pokered_harness.mcp_server
```

For an in-process peer, add all three `POKERED_PEER_*` variables with the
peer's matching paths and hash. The peer is created at startup but must be
paired explicitly with `link_pair`; it is not proof of a working game flow.

For a normal MCP lifecycle, initialize the server, list tools, and then use
`step`, `press`, state resources, and save/load as needed. For a local pair,
call `link_pair`, use `link_step`, and finish with `link_unpair`. For TCP,
call `link_listen` and `link_connect` using their default roles, poll
`link_status` until `remote_mode=connected`, and call `link_disconnect` before
closing either server. For native Yellow/Red or Yellow/Blue pairs, the
versioned HELLO selects the non-Yellow endpoint as the initial internal-clock
side. A teardown is complete
only when status is idle and no child peer, worker thread, socket, or callback
remains.

The stdio server treats input EOF and request cancellation as lifecycle events.
Thread-backed tool and resource work remains owned until it finishes; a
cancellation first signals remote teardown and then waits within the bounded
cleanup deadline. When the transport itself closes, the server repeats that
remote teardown, unpairs any local pair, and drains tracked request workers
before the owning sessions are closed. A stubborn native call cannot be
force-killed by Python, so a worker that misses the deadline is reported as a
cleanup failure rather than being presented as a clean idle transition. The
real-asset `tests/test_mcp_stdio_integration.py` coverage exercises startup,
tool/resource discovery, input, state round-trip, remote listen/connect/status,
and connector EOF cleanup; it does not certify link gameplay.

The checked-in `.mcp.json` is a portable configuration template, not a
self-installing launcher. The MCP client must expand `${PWD}` to the checkout
root (or substitute its documented workspace variable), and `python` must be
the same environment used by the clean-install command. The config does not
search for or create a virtual environment. If a client does not expand
`${PWD}`, use the explicit launch command above from the repository root or
configure an equivalent client-specific working directory and substitution.
No machine-local `PYTHONPATH` is required. A wheel launched outside a checkout
can omit `VERSIONS.md` when it supplies explicit primary and peer SHA-1 values;
the bundled PyBoy runtime identity is still checked.

The MCP remote TCP tools enforce localhost-only hosts (`127.0.0.1`,
`localhost`, or `::1`). They provide no authentication or encryption; do not
expose the raw transport or server to a public address, untrusted LAN, or WAN.
The optional `peer_rom_version` argument checks the announced ROM label but is
not cryptographic authentication.

## 7. Record release evidence

For every required result, record:

- repository commit and clean-worktree status;
- Python executable/version and PyBoy package/version/build mode;
- operating system and CPU/GPU details when timing is relevant;
- exact ROM and symbol paths plus SHA-1 values;
- fixture filenames, hashes, source-ROM identity, and generation command;
- exact pytest commands, full output, duration, skips, xfails, and timeouts;
- MCP launch environment with secrets and personal paths removed;
- remote listener/connector roles, address scope, and teardown result; and
- known limitations or deviations from the checklist.

Do not summarize a skipped matrix as “all versions passed,” and do not report a
transport or LinkMenu milestone as a completed trade or battle.

## 8. Current sign-off blockers

The candidate remains `PARTIAL`, not `PRODUCTION-READY`. The observed blockers
are:

1. The documented release runtime is source mode. The pinned Cython build,
   semantic serial probe, three-ROM lifecycle smoke, native unit/timing gate,
   and the recorded representative `blue-blue` remote LinkMenu smoke pass. PR #26 fixes
   native `PyBoy.tick` ownership, and PR #27 makes runtime selection explicit.
   The post-PR #27 native strict trade qualification is `FAIL` at 16/19, with
   three color-variant subprocess failures in the trade-center/rendezvous path;
   native strict battle has not been run.
2. The strict declaration is complete: nine ordered local pairs, nine ordered
   remote role pairs, six reversed-role rows, nine local variant rows, and 19
   strict entrypoints for each operation all collect. The PR #17 baseline
   passed the strict trade and battle runs 19/19 each. PR #19 adds synthetic
   concurrency/lifecycle evidence and a 15/15 remote transport rerun, but
   broad-suite (`pytest -q -ra`) evidence is currently pre-PR #27 bounded
   diagnostic evidence: 418/703 tests completed before the 5,400-second bound
   (386 passed, 20 skipped, 12 failed, and 285 not started). The failed rows
   were remote TCP cases that reached the old 5-second exchange deadline;
   representative source and native flows pass with the PR #27 pacing fix; a
   complete post-fix broad rerun remains open.
   Real-ROM load stability, full native-platform qualification, and independent
   review remain open. Scoped Windows install/bootstrap/MCP/Cython lifecycle
   evidence is recorded above; full Windows gameplay/load coverage and macOS
   coverage remain open.
3. The canonical color Red, color Blue, and Yellow fixture bytes have recorded
   reproduction evidence, and the Red color fixture is now enabled for the
   remote diagnostic matrices. Vanilla ordinary source provenance is `PARTIAL`.
   The manifest and save states are external/operator-managed; a retained
   release bundle must include complete byte validation and sanitized evidence.
4. Remote TCP has no authentication or encryption. Loopback-only operation is
   enforced and is the only supported network boundary; cross-host operation
   is blocked until secure transport is added.

The smallest next actions are to complete the full strict matrices against the
merged runtime, establish native-platform and real-ROM load evidence, verify
vanilla fixture provenance, rerun the broad suite to completion, and obtain
independent release review.
