# Production runbook

This runbook defines how to prepare and evaluate a clean `pokered-harness`
checkout. The live target is
[`CompleteDotTech/pokemon`](https://github.com/CompleteDotTech/pokemon). The
current integration candidate is based on
`d8060be232dbbb75798fa1ff93e49c2f84747b9f` (2026-09-03) plus uncommitted
runtime, packaging, lifecycle, and test changes. External ROMs, symbols, save
states, and sanitized evidence remain operator-managed and are not distributed
by this repository.

The release decision for this candidate is `PARTIAL`, not `PRODUCTION-READY`.
The latest bounded candidate unit gate used the bundled source PyBoy runtime
and passed 360/360 unit tests plus 35/35 timing tests in each of five
repetitions. Current asset preflight matched all five pinned ROMs, three symbol
files, and six required ordinary/battle Cable Club fixtures. This is scoped evidence, not
release sign-off: the strict trade gate timed out in its remote
Red-color↔Blue-color row at the 600-second bound, and no current battle result
is recorded.

A separate bounded remote diagnostic completed one Blue-color↔Blue-color trade
row (`1/1`) with balanced `6,528` serial edges per direction and no unknown
opcodes. Both peers used the same fixture, so this is a transport/game-flow
diagnostic, not strict party-swap acceptance or full-matrix evidence.

The following records are historical or scoped evidence retained for
traceability. They describe other heads or narrower tiers and are not current
release sign-off:

- a dated focused source and Cython serial-link+network suite passed
  78/78 in each runtime;
- an asset-backed source trade gate recorded 19/19 ordered local and remote
  party-swap rows. Its supervisor started before PR #35 was published, so this
  is recorded trade-tier evidence, not a clean post-merge all-tier sign-off;
- the historical native Cython strict-trade gate recorded 18/19. Exact-row
  follow-ups have both passed and failed, including exact party-record
  exchange, a party-record mismatch, and phase stalls, so native strict-trade
  reliability is unproven;
- timing-altered diagnostics are not acceptance evidence;
- the latest native remote battle acceptance lane exercised 3/9 direct strict
  remote rows under a bounded 155-second per-pair deadline:
  `Blue-color↔Blue-color` passed, while `Red-color↔Yellow` and
  `Yellow↔Red-color` failed;
  no bypasses were used, six remote rows remain unrun, and the full
  19-entrypoint battle set is unqualified;
- a dated source and Cython packaging/runtime contract was 27/27 in each
  runtime, including native bootstrap ownership and transient metadata cleanup.

A dated prior dual source and Cython `--unit-only --repeat-timing 5` gate on
2026-09-03, using separate source and Cython interpreters, collected 771 tests
in each runtime. Both passed unit 626/626 and timing 50/50 in all five
repetitions; source reported `python-source` and
Cython reported `cython/native-extension`. The clone had no ROM, symbol, or
save-state assets, so fixture schema and matrix declaration were checked but
ROM gameplay was not run. Fresh uv-managed source and native environments
installed the candidate, passed `uv pip check`, and the native bootstrap verified
both `pyboy` and `pokered-harness` owners. An earlier environment-specific
585/586 ownership result is superseded for these isolated environments. Source
mode is the documented release runtime; Cython is an optional diagnostic build.
The host's bare `python3` still lacks `ensurepip`, so that alternate
standard-library venv path remains open.

A dated prior post-hardening asset-backed check used pinned ROM/SYM/fixtures and
separate source and native (Cython) interpreters. The local/session checks
passed 47/47 in source mode (758.1s) and 47/47 in native mode (38.1s); the
remote transport/MCP checks passed 16/16 in source mode (75.0s) and 16/16 in
native mode (28.8s). These results are scoped to local/session and remote
transport/MCP checks only; they are not strict trade/battle qualification.

The complete all-tier source-runtime gate is historical PR #17 baseline
evidence: collection 698, unit 554/554, local real-ROM 47/47, remote
transport/MCP 15/15, strict trade 19/19, strict battle 19/19, and timing
40/40 in each of five repetitions. Later scoped follow-ups recorded remote
transport 15/15, local/session 47/47, focused transport/MCP 167/167, and a
bounded localhost concurrency probe 8/8 in each of five repetitions. These
results must not be relabeled as a current full native gate.

A historical asset-free `python -m pytest -q -ra` diagnostic completed 604 passed, 141
expected BYO-asset skips, and one SDL warning after the partial-initialization
destructor guard was added. It proves clean-checkout test collection can
complete without assets; skipped real-ROM tiers remain unverified. Fresh Windows evidence covers install, source/Cython bootstrap,
MCP stdio, and three-ROM Cython lifecycle checks, but not full native
gameplay, real-ROM concurrent load, the remote trade/battle matrix, or macOS.

Status semantics:

- `PASS` is scoped to the named command and means clean collection plus no
  failure, error, skip, xfail, or timeout in that selected scope.
- `PARTIAL` means that controlled evidence exists but a required release
  condition remains open.
- `PRODUCTION-READY` requires the full gate with all required BYO assets,
  complete strict matrix declaration and runtime coverage, retained evidence,
  and a clean candidate with no open blocker.

## Capability boundary

| Capability | Current status | Evidence boundary |
|---|---|---|
| Single session and MCP | Implemented; scoped gate passes | The latest asset-free source unit/timing tier passed 360/360 and 35/35 × 5. Real-ROM checks require BYO assets and the pinned runtime. |
| In-process paired link | Implemented; acceptance unqualified | Focused and fixture-gated tests exercise attach/serial/lifecycle behavior; no current candidate full local trade/battle result is recorded. |
| Remote TCP transport | Implemented; gameplay unqualified | Transport and lifecycle tests do not establish a real-game trade or battle. TCP is loopback-only and has no authentication or encryption. |
| Remote trade | Unqualified for this candidate | A release claim requires a current strict matrix with matching ROMs, fixtures, runtime identity, bounded deadlines, and clean teardown. |
| Remote battle | Unqualified for this candidate | A release claim requires a current strict matrix for the advertised rows; historical or representative rows are insufficient. |
| Manual fixtures | Required BYO inputs | Operators must supply five pinned ROMs, three symbols, and six canonical color ordinary/battle states; all ten manifest entries are needed for separate operator-managed byte validation. |
| Option-B / RAM-boost walkthroughs | Diagnostic only | Direct game-memory writes and `--option-b` shortcuts are not human-valid gameplay or release acceptance. |

## 1. Start from a clean checkout

Use a fresh clone or an isolated worktree. Do not use a dirty development
checkout as release evidence.

```bash
git clone https://github.com/CompleteDotTech/pokemon.git poke-harness
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

The required Python version is 3.11 or newer. On Windows PowerShell, create
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

The pinned fork has an optional Cython mode. Its historical semantic
serial-contract probe and three canonical real-ROM attach/step/close smokes
pass, but it is not the documented release default. The historical focused
source and Cython serial-link+network suite passed 78/78 in each runtime. The
historical native strict-trade gate recorded 18/19; exact-row follow-ups have
both passed and failed, including a party-record mismatch and phase stalls, so
reliability is unproven. The current native battle matrix is not qualified.
Prior integrated remote 15/15 and local/session 47/47 results are scoped
follow-ups, not current full native gameplay sign-off. Do not substitute
an arbitrary standalone PyBoy wheel: record the version, harness revision, and
runtime mode, and require the serial contract before running link tests.

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

`sha1sum` is the GNU coreutils form. On macOS use `shasum -a 1`; in
PowerShell use `Get-FileHash -Algorithm SHA1`. A shell-independent alternative
from the activated environment is:

```bash
python -c "from hashlib import sha1; from pathlib import Path; files=[p for root in ('rom', 'tests/fixtures/link') for p in Path(root).rglob('*') if p.is_file()]; [print(sha1(p.read_bytes()).hexdigest(), p) for p in files]"
```

The repository does not ship a fixture-manifest validator. Fixture states are
operator-managed external evidence; record their sizes and SHA-1/SHA-256
values alongside the run and keep that record outside version control.

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
POKERED_ROM_ROOT="$PWD/rom" \
POKERED_FIXTURE_ROOT="$PWD/tests/fixtures/link" \
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --rom-root "$PWD/rom" \
  --fixture-root "$PWD/tests/fixtures/link" \
  --python "$(command -v python)" \
  --repeat-timing 5 \
  --format text
```

For the asset-free smoke used to validate a source-only checkout, run the
scoped gate explicitly:

```bash
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --unit-only \
  --repeat-timing 5 \
  --format text
```

The latest candidate `--unit-only --repeat-timing 5` check used the bundled
source runtime and passed 360/360 unit tests plus 35/35 timing tests in each
of five repetitions. The candidate worktree had no ROM, symbol, or save-state
assets, so this check did not validate ROM bytes, load save states, exercise
MCP with a real ROM, or run link gameplay. It is not a production sign-off.
Rerun the required real-ROM tiers after installing the pinned dependency set;
do not copy the historical dual-runtime counts below into a current report.
Because `--unit-only` selects only `unit` and `timing`, it does not validate ROM
bytes, load save states, exercise MCP with a real ROM, or run link gameplay; it
is never a production sign-off by itself.

For the optional native qualification, first build the pinned fork and then
select Cython explicitly. The gate verifies that all required PyBoy modules are
installed extensions before running any selected tier:

```bash
python scripts/bootstrap_pyboy.py --mode cython
python scripts/bootstrap_pyboy.py --mode cython --check
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --unit-only \
  --repeat-timing 5 \
  --format text
```

The current gate has one explicit `--python` interpreter and a runtime mode per
invocation. The default source run is:

```bash
python scripts/production_gate.py --runtime-mode source --unit-only \
  --repeat-timing 5 --format text
```

If a separately bootstrapped Cython environment is available, run the same
command with `--python /path/to/cython/python --runtime-mode cython` and retain
the two reports separately. The gate checks that `pyboy.core.serial` matches
the requested mode, so a source path cannot silently shadow a native build.
The historical dual-runtime report is asset-free runtime evidence, not ROM
gameplay or release sign-off; the release decision remains `PARTIAL`.

The source default and native command are separate evidence scopes. Do not
describe a source result as native parity, or a native unit/timing result as
real-ROM gameplay coverage.

The gate writes a human-readable report to stdout; use shell redirection to
retain it outside the checkout. `--format json` is available for a structured
report. The default gate checks five pinned ROM paths, three symbol paths, and
the required Cable Club fixtures before running each selected tier. A failed
or blocked run still reports its status and diagnostics, and required tiers
fail closed on missing assets or skips.

This checkout does not contain a release workflow or a checked-in Ruff path
allowlist. A broad `ruff check .` (or `ruff check src tests scripts`) includes
legacy files and is not a clean release result; any future scoped check must
record its exact file list and output. The vendored runtime is covered by
revision pinning, compile/import checks, and the serial contract.

The release tree includes `tests/__init__.py`; otherwise environments that do
not treat `tests/` as a namespace package can fail collection. Run both
invocation forms for every release candidate.

The standalone matrix command executes bounded subprocess pairs and writes a
manifest plus per-peer traces under `--outdir` (which should point outside the
checkout for release evidence):

```bash
python scripts/tcp_link_matrix.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --modes trade battle \
  --deadline-seconds 210 \
  --outdir "$(mktemp -d)"
```

It is gameplay evidence only when its manifest reports `ok: true`, both peers
emit the required phase artifacts, and the run is retained with the exact ROM,
fixture, runtime, and teardown metadata. A failed or zero-row filtered run is
not evidence.

For a machine-readable gate report, run the gate from the repository root:

```bash
python scripts/production_gate.py --format json > "$(mktemp /tmp/pokered-gate-XXXXXX.json)"
```

The command fails closed when required ROMs, symbols, fixtures, or acceptance
tests are missing. Keep the report outside version control and do not treat a
diagnostic matrix manifest as a release sign-off by itself.

## 4. Run the evidence tiers

Run the tiers in order and save the complete output with the commit and
interpreter identity.

### Tier A: ROM-free behavior and runtime contract

This tier exercises configuration, state parsing, serial semantics, protocol,
transport, symbol loading, and the runtime contract without commercial game
assets. The direct marker selection is:

```bash
python -m pytest -q -ra -m unit
```

The marker is assigned by `tests/conftest.py` from the explicit module manifest
in `tests/_tier_config.py`, including tests in nested directories. These tests
do not require commercial ROM bytes. When the production gate runs this tier,
source mode prepends the vendored PyBoy runtime pinned in `VERSIONS.md`; Cython
mode deliberately omits that vendored path so the selected interpreter's
extension modules are tested. A green Tier A result does not establish
emulator, MCP, trade, or battle compatibility.

### Tier B: one real session and MCP stdio

Use one explicit ROM hash. This example uses stock Red:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=ea9bcae617fdf159b045185467ae58b2e4a48b9a \
POKERED_VERSIONS_PATH="$PWD/VERSIONS.md" \
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
candidate evidence. The following commands exercise the nine canonical
Red/Blue/Yellow ordered pairs:

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
coverage. These broad parametrized functions are not the same selection as
the production gate's `trade_acceptance` and `battle_acceptance` expressions:
the current gate selects one dedicated local Red/Yellow assertion and one
dedicated Red-color/Blue-color subprocess assertion for each operation. Run
the commands above, and the complete remote matrix below, separately when
establishing full matrix evidence; a green narrow gate is not a nine-row pass.

### Inspection-only paired checkpoints

Use an empty output directory outside the checkout to retain paired states:

```bash
python scripts/diagnose_pair_trade.py \
  --output-dir /tmp/pokered-trade-inspection-unique \
  --capture-frame 200,800,1600 \
  --max-captures 4 --deadline-seconds 600
```

Frame zero counts toward the capture limit. The driver preserves input order
and advances one public scheduler quantum per owner call, checking its deadline
between calls. Local paired stepping uses cumulative physical-time horizons;
LCD display completion does not stop one CPU while its peer continues.

Each checkpoint includes two state blobs and a manifest with hashes, runtime
metadata, input history, and hook counters. These are inspection-only artifacts,
not trade/battle acceptance or resumable linked sessions. State files omit the
coordinator and Python input queues; never load them into an attached pair.

`terminal.json` is first written with `cleanup_pending=true` before teardown,
then replaced with final cleanup results. SIGINT/SIGTERM are handled cooperatively.
A hard kill or native call that never yields may still prevent reporting or
cleanup; use an outer process deadline and do not treat a missing or provisional
terminal record as verified cleanup.

### Tier E: remote transport and subprocess acceptance/diagnostics

First run the transport/serial milestones:

```bash
python -m pytest -q -ra \
  tests/test_link_integration_remote.py
```

This module exercises remote TCP plumbing and selected real-ROM milestones.
Some cases stop at LinkMenu or use controlled menu/fixture setup; that is not
the same as a user-driven full trade or battle.

The separate-process diagnostics and narrow acceptance use the same bundled
source runtime as the normal package:

```bash
python -m pytest -q -ra \
  tests/test_pyboy_link_session_subprocess.py
```

The LinkMenu test is a transport smoke test. The file's strict subprocess
trade and battle rows are fixed color-Red listener → color-Blue connector
checks, not the complete nine-row ordered matrix. Both payloads are intended to
travel through native bit-level serial traffic, and the trade row rejects the
out-of-band exchange counter. The battle driver currently contains diagnostic
menu-state forcing and is not strict acceptance evidence. The PR #17 baseline
passed historical strict matrices; the PR #19 follow-up reran the 15-row
remote transport slice and focused lifecycle checks, not the full strict
trade/battle matrix. Use `scripts/tcp_link_matrix.py` plus the explicit
party/battle postconditions for a full remote matrix, and record both child
traces and the exact deadline when investigating a regression.

The ROM-free concurrency and lifecycle coverage is included in the unit tier;
the timing-sensitive subset can be repeated with:

```bash
python scripts/production_gate.py --unit-only --repeat-timing 5
```

This is synthetic localhost evidence; real-ROM concurrent-load stability
remains a separate release requirement.

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
| Trade | 9 | 9 | Red/Yellow party-record swap | A prior source gate recorded 9/9 local and 9/9 remote rows. The native strict-trade matrix has a historical 18/19 result; exact-row follow-ups both passed and failed, including a party-record mismatch and phase stalls, so reliability is unproven |
| Battle | 9 | 9 | Red/Yellow resolved-turn assertion | Historical source baseline passed 9/9 local and 9/9 remote rows. The latest native direct remote acceptance lane exercised 3/9 strict remote rows under a bounded 155-second per-pair deadline: `Blue-color↔Blue-color` `PASS`; `Red-color↔Yellow` and `Yellow↔Red-color` `FAIL`; no bypasses were used and six remote rows remain unrun. The full 19-entrypoint battle set remains unqualified |

That is 19 strict entrypoints per operation. The remote rows use canonical color
Red, color Blue, and Yellow profiles; listener/connector order is significant.
The PR #17 baseline completed both strict matrices with no failed rows. The
full baseline gate recorded source-runtime remote rows, listener/connector
roles, bounded deadlines, and clean child teardown. A prior source trade
gate recorded all 19 trade rows. The native strict-trade result is historical
at 18/19, and mixed exact-row follow-ups leave its reliability unproven. The
latest native direct remote battle lane covered only 3/9 rows under a bounded
155-second per-pair deadline, with `Blue-color↔Blue-color` passing and
`Red-color↔Yellow` plus `Yellow↔Red-color` failing; no bypasses were used and
six rows remain unrun. Native battle and a complete current source battle rerun
remain unqualified. Rerun the complete strict matrices at the documented
conservative worker count after any further runtime change.
Stock-ROM rows and LinkMenu-only milestones are outside this strict release
claim.

## 5. Generate link fixtures safely

Save states are emulator artifacts and are tied to the exact ROM bytes. Keep
them local under `tests/fixtures/link/<version>/` and never commit them.

The ordinary-fixture producer is:

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
  --out tests/fixtures/link/yellow/cable_club.state
```

For stock Red or Blue use `--variant vanilla`, a vanilla-ROM-captured source,
and an output named `cable_club-vanilla.state`. For the default Red/Blue color
variant use `--variant color` and `cable_club.state`. There is no separate
Yellow producer. Successful fixture generation proves only that the state
lands at the producer's expected map/tile; it does not prove a trade or battle.
The current producer accepts explicit paths but does not itself validate ROM or
symbol hashes, PyBoy revision, or a wall-clock/movement budget; it also enables
`POKERED_SKIP_SHA1=1` for this diagnostic path. Validate the selected inputs
separately and run the producer under an external supervisor before retaining
any output as release evidence.

The tracked battle-fixture utility derives immutable battle-start states from
ordinary fixtures. It accepts `--repo-root` and `--variants`, writes the
derived files under that repository's `tests/fixtures/link/<version>/`
directories, copies the lead record into a legal multi-mon party, repairs
zero-PP lead moves, validates the result, and closes the emulator before
writing the output. It does not accept separate `--rom-root`, `--fixture-root`,
or `--output-root` options, and it does not validate ROM or symbol hashes.
Acceptance loads the resulting file and does not perform this preparation in
emulator RAM. Prepare the canonical color rows with:

```bash
python scripts/prepare_battle_cable_club_fixtures.py \
  --repo-root "$PWD" \
  --variants red_color blue_color yellow
```

The two vanilla battle rows may be generated with `red_gb` and `blue_gb`, but
they remain `PARTIAL` until their ordinary source state is proven to match the
vanilla ROM. The exact ten-entry hashes, source-state records, and status
values belong in the operator-managed fixture manifest kept outside version
control. This repository does not ship a manifest-validator script; record and
review those values as part of the retained evidence bundle.

## 6. Launch MCP explicitly

Use explicit paths and the matching hash. The server also indexes the
per-ROM `Path`/`SHA-1` rows in `VERSIONS.md` and fails closed when no matching
pin exists:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red-color.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=e1deed63080bc24cad5fba18ecb3184f905d16d4 \
POKERED_VERSIONS_PATH="$PWD/VERSIONS.md" \
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

1. The latest candidate source gate passed 360/360 unit tests and its timing
   tier passed 35/35 in five repetitions. The run was asset-free, so it does
   not close ROM-backed gameplay, platform, or release sign-off requirements.
   A current clean dual-runtime result is also open.
2. No current candidate trade or battle acceptance result is recorded. The
   strict entry points and historical results below are not substitutes for a
   current matrix run with pinned ROMs, matching fixtures, bounded deadlines,
   and clean teardown. Representative rows and timing-altered diagnostics do
   not close either matrix.
3. Real-ROM concurrent-load stability, full native-platform qualification,
   independent review, a complete post-fix broad rerun, and clean-install
   reproduction remain open. Scoped historical Windows evidence does not
   replace current Windows gameplay/load or macOS coverage.
4. Canonical color Red, color Blue, and Yellow fixture bytes have historical
   reproduction records, but all states remain external/operator-managed.
   Vanilla ordinary source provenance is `PARTIAL`; derived vanilla battle
   states inherit that status. A release run must validate every manifest entry
   and retain sanitized evidence with the ROM, symbol, fixture, runtime, and
   teardown identities.
5. Remote TCP is enforced as loopback-only and provides no authentication or
   encryption. Cross-host operation is blocked until a secure transport is
   added; `peer_rom_version` is a label check, not authentication.

The smallest next actions are to complete reproducible clean-install
verification, complete current native trade and battle matrices, rerun a
complete source battle matrix, establish native-platform and real-ROM load
evidence, verify vanilla fixture provenance, finish the broad suite, and obtain
independent release review.
