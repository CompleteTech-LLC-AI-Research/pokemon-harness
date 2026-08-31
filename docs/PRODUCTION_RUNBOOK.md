# Production runbook

This runbook defines how to prepare and evaluate a clean `pokered-harness`
checkout. The baseline at `e219fb5` was not certified. Unless labelled
historical, the current candidate facts refer to committed source boundary
`dfee2ec` on 2026-08-31. Uncommitted worktree changes and external BYO assets
are excluded from the tracked source tree.

The latest clean asset-free gate at `dfee2ec` passed unit 507/507 and timing
35/35 in each of five repetitions; its collection preflight collected 648
tests and its scoped result was `PASS`. This proves only the ROM-free and
timing scope. The matrix audit collected nine ordered local pairs, nine ordered
remote role pairs, six reversed-role rows, nine local variant rows, and 19
strict trade plus 19 strict battle entrypoints. Structural and declaration
checks pass; the collection-only matrix runtime is `NOT RUN`.

No complete current-candidate real-ROM trade or battle matrix result is
recorded. Earlier diagnostic runs are historical only: a strict-trade attempt
timed out before a final pytest report, and repeated cross-family remote-battle
probes included a pre-battle warp/phase divergence. Those observations remain
open risks, not release results. Every declared strict row still needs a
bounded, candidate-bound runtime result before sign-off. Symbol hashes and
fixture byte/provenance records are in [`VERSIONS.md`](../VERSIONS.md) and the
tracked [`fixture-manifest.json`](../release-evidence/fixture-manifest.json).
Overall status is `PARTIAL`; the exact open items are listed in [the release
checklist](RELEASE_CHECKLIST.md).

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

The pinned fork also supports a compiled Cython diagnostic mode. In this audit,
`python scripts/bootstrap_pyboy.py --mode cython --check` passed in a seeded
Python 3.12 environment; it reported `cython_compiled=True`, exposed
`mb.serial`, and passed a real-ROM attach/detach/close smoke. Full trade/battle
acceptance was not run in Cython mode, so the Cython result is not full gameplay
sign-off. Do not substitute an arbitrary standalone PyBoy wheel: record the
version, harness revision, and runtime mode, and require the serial contract
before running link tests.

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
  --unit-only \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

At `dfee2ec`, a clean committed worktree collected 648 tests, passed unit
507/507, passed timing 35/35 in each of five repetitions, and returned scoped
`PASS`. It also performed schema-only validation of the ten-entry fixture
manifest. Because
`--unit-only` selects only `unit` and `timing`, it does not validate ROM bytes,
load save states, exercise MCP with a real ROM, or run link gameplay; it is not
a production sign-off.

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

The product Ruff boundary is `src/`, `tests/`, and `scripts/`; `pyproject.toml`
explicitly excludes the pinned third-party `vendor/pyboy-src` tree from the
default `ruff check .` audit. The current configured boundary is clean. The
vendored runtime is covered by revision pinning, compile/import checks, and the
serial contract.

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

These tests do not require commercial ROM bytes. The package bundles the
source PyBoy runtime pinned in `VERSIONS.md`; the gate prepends that runtime
when running from a checkout so a standalone PyBoy wheel cannot silently
change the serial contract. A green Tier A result does not establish
emulator, MCP, trade, or battle compatibility.

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
real-ROM stdio tier. Any real-ROM result must identify the current candidate,
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
counter. Current-candidate runtime results and concurrent-load stability must
still be recorded before release. Record both child traces and the exact
deadline when investigating a regression.

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

The collection audit verifies declaration shape only. The current strict runtime
scope is:

| Operation | Local ordered rows | Remote ordered listener/connector rows | Dedicated assertion | Current status |
|---|---:|---:|---|---|
| Trade | 9 | 9 | Red/Yellow party-record swap | Declared; runtime not run |
| Battle | 9 | 9 | Red/Yellow resolved-turn assertion | Declared; runtime not run |

That is 19 strict entrypoints per operation. The remote rows use canonical color
Red, color Blue, and Yellow profiles; listener/connector order is significant.
The collection-only result is not a gameplay result. A previous strict-trade
attempt timed out before a final report, and repeated cross-family battle probes
included a pre-battle warp/phase divergence; neither observation is a current
pass. Stock-ROM rows and LinkMenu-only milestones are outside this strict
release claim.

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

1. The clean `dfee2ec` asset-free gate passes 507/507 unit tests and 35/35
   timing cases in each of five repetitions, but it does not run ROM-backed
   gameplay. No complete current-candidate strict trade or battle matrix result
   is recorded. A previous strict-trade attempt timed out before a final
   report, and repeated cross-family battle probes included a pre-battle
   warp/phase divergence; both are diagnostic boundaries only.
2. The strict declaration is complete: the collection audit has nine ordered
   local pairs, nine ordered remote role pairs, six reversed-role rows, nine
   local variant rows, and 19 strict entrypoints for each operation. Collection
   runtime is `NOT RUN`; every declared local and remote row still requires a
   bounded current-candidate gameplay result.
3. The canonical color Red, color Blue, and Yellow fixture bytes have recorded
   reproduction evidence, while vanilla ordinary source provenance is
   `PARTIAL`. The manifest and save states are external/operator-managed; a
   retained release bundle must include complete byte validation and sanitized
   evidence.
4. The wheel and both source/Cython runtime identity paths are validated, but
   full Cython trade/battle acceptance is not. The broad suite, all advertised
   real-ROM single-session rows, native-platform coverage, concurrent-load
   stability, and independent review remain open.
5. Remote TCP has no authentication or encryption. Loopback-only operation is
   enforced and is the only supported network boundary; cross-host operation
   is blocked until secure transport is added.

The smallest next actions are to complete or explicitly scope the remaining
strict matrix rows, run and retain a full gate result with all required BYO
assets, establish native-platform and concurrent-load evidence, and obtain
independent release review.
