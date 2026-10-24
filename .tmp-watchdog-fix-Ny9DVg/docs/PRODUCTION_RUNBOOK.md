# Production runbook

This runbook defines how to prepare and evaluate a clean `pokered-harness`
checkout for [`CompleteDotTech/pokemon`](https://github.com/CompleteDotTech/pokemon).
ROMs, symbols, save states, and sanitized evidence remain operator-managed.

## Current evidence and release decision

**Release status: `PARTIAL`, not `PRODUCTION-READY`.** The retained baseline
candidate `1f19707` passed the dual source/native unit-and-timing gate and
canonical boot checks. The current source-runtime trade follow-up is recorded
below; no clean full source/native gameplay qualification is established.

| Retained baseline scope at `1f19707` | Recorded result | Evidence boundary |
|---|---|---|
| Source and native unit/timing | Each runtime: `1,176/1,176` unit and `65/65` total timing (`13` cases × five repetitions) | `1,320` collected in each runtime; zero failures, skips, xfails, xpasses, or errors. All `19/19` trade and `19/19` battle entrypoints declared, but not executed by this unit gate. |
| Canonical boot/state/save/load | Source `3/3` in `6.74s`; native `3/3` in `1.89s` | Zero failures, skips, or errors; one SDL warning each. Color Red, color Blue, and Yellow run 120 frames, then state and save/load checks. Not intro completion, MCP, or gameplay acceptance. |

The external unit bundle is `pokemon-qualification-dual-unit-1f19707-20260905`
(`gate-report.json`). Native build provenance is retained in
`poke-serial-native-20260905-V7dHOU/QUALIFICATION.md`: a fresh base-`e1686ca`
build with serial overlays whose serial source hash matches `1f19707`.
That build record alone is not an exact-head full gameplay gate.

The native Blue-color listener / Yellow connector trade replay at `02a8e85`
(runtime identical to `1f19707`) failed after `721.67s`; both peers exited
cleanly with return code `1`. Blue recorded no LinkMenu entry and one
`CloseLinkConnection` at local tick `1332`; Yellow recorded LinkMenu at local
tick `628` and input at `660`. Transport recorded `6,248` balanced native
edges, zero errors, and no keepalive traffic. Local ticks are not a common
clock: this supports investigating time coordination, not a precise root-cause
claim. Retain `pokemon-native-blue-yellow-02a8e85-LMtfZm/output.log` and
`result.json` through the release handoff.

The original dual unit gate at `02a8e85` failed native timing at `64/65`, and
a gate bug lost failed test identity and iteration output. Subsequent verified test races
were corrected before the dual `1f19707` pass; preserve that older failure.

### Reconciliation candidate (2026-09-09)

The candidate based on `ae057bcd` remains `PARTIAL`. Retain the external
bundle `poke-native-source-gate-pr65-A1qh6p` (`gate-report.json` and
`gate-report.txt`) as a failed pre-packaging-update source gate, not as
current-head qualification:

- Both collection entrypoints found the same 4,243 tests.
- The unit tier completed 3,123 of 4,090 selected tests before its 900-second
  deadline: 3,097 passed, 25 failed, and one skipped.
- All five timing repetitions completed: 1,236 passed and 59 failed across
  1,295 executions, with no skips or errors.
- The interpreter lacked `ensurepip` and Python headers. Wheel setup failed,
  and the Cython translation check skipped. Other failures included bounded
  owner, subprocess, and session progress; the observed host was heavily
  loaded. A timed-owner failure reproduced on clean `ae057bcd` as well as the
  candidate, but this does not establish the cause of every failed case.
- This unit-only invocation did not execute any real-ROM acceptance tier.

The subsequent Python 3.11 dependency/CI update and explicit Cython serial
local type passed 155 focused tests on CPython 3.11.15: 37 packaging,
113 serial-core, and five serial-rearm tests. This includes translation and
C compilation of the serial module, not a full native extension build.
The later pre-commit reconciliation run used that fresh CPython 3.11 source
environment and completed all selected unit tests: **4,416 passed, five
failed, zero skipped**, with 158 non-unit tests deselected, in 242.86 seconds.
Retain `reconcile-all-unit-precommit-20260909.xml` as a failed integration
result. Its five failures concern timed-peer error classification, the local
pair cleanup deadline, large finite close timeouts, and two native-wrapped
cancellation cases. Subsequent corrections require a fresh complete run;
individual passing replays do not replace this result.

The reconciled serial runtime also built a complete isolated CPython 3.12
Cython wheel (SHA-256
`8591142c8c6ce8dc77c7f4e9817752635caac78ae1529634deee27fc49567cf1`).
The compiled-module origin and bootstrap contract passed. Focused native
network/serial checks passed 299 tests, and a later shared lifecycle/CPU suite
passed 57 tests in each runtime. The CPU cases use authored synthetic programs
to verify actual serial interrupts and HALT wake-up; they are not Pokémon
gameplay evidence. The native build uses matching serial inputs from the
working reconciliation candidate, not a final clean-commit qualification.

The complete gate and strict source/native trade/battle matrix still require
fresh passing evidence for the final clean candidate. All prior failures
above remain retained even after their individual causes are corrected.

### Historical working-tree validation (2026-09-06)

The recorded isolated working tree had source-runtime real-ROM evidence using
the pinned vendored PyBoy source runtime (`PYBOY_NO_CYTHON=1`) and the
operator-supplied ROM, symbol, and fixture roots:

| Scope | Result | Evidence boundary |
|---|---|---|
| Strict source-runtime trade tier | `19/19` passed in `914.3s` with four workers | All nine local version pairs, all nine TCP role pairs, and the additional strict Red/Yellow party-record assertion passed. Compiled-runtime, MCP, and host/platform claims remain separate. |
| Blue-color ↔ Blue-color | LinkMenu, full trade, and one battle turn passed | Native edge traffic balanced; same-family frame barrier completed without owner or reader errors. Representative replay only. |
| Blue-color ↔ Yellow | LinkMenu, full party-record trade, and one battle turn passed | The affected non-Yellow-listener/Yellow-connector ordering uses a short bounded frame-paced walk rendezvous, then native edge pacing for the serial exchange; the reverse ordering remains native. Representative replay only. |
| Focused source regression slice | `54` non-ROM subprocess-helper tests passed | Vendored/source runtime; no compiled-runtime, MCP, or host/platform claim. Ruff passed for the touched driver. |

The family-specific frame policy is intentional: identical ROM families use a
bounded owner-frame barrier. For cross-family pairs, only the ordered
non-Yellow-listener/Yellow-connector walk boundary receives a short frame-paced
rendezvous; the serial-heavy exchange then returns to native edge pacing, while
the reverse ordering stays on its established native path because the
cartridges expose different polling windows. This historical working-tree evidence
does not qualify a changed candidate or replace the required strict battle
tier, compiled-runtime, MCP, or host/platform evidence.

### Retained historical results

The historical full source snapshot at
`2eb21a5b45e67f47bb89697daeed76509adf4b13` passed unit `980/980` and local
`47/47`, failed remote `22/23` on Yellow/Yellow LinkMenu with
`NetworkBackendError: backend closed`, and failed strict trade `18/19`;
battle was ongoing at that snapshot. The historical native full-gate snapshot
at `f4fddfc` passed unit `1,060/1,060` and local `47/47`, failed remote
`11/12`, and had trade ongoing with two failures recorded. These are historical
snapshots, not live progress reports. The follow-up LinkMenu-only shutdown
change passed five consecutive Yellow/Yellow real-ROM replays; targeted
passes do not erase a failed full gate.

The following older results and those in [README](../README.md#release-status)
remain scoped to their recorded runs, not acceptance of `1f19707`.

| Scope | Recorded result | Evidence boundary |
|---|---|---|
| Standard-library editable install | `PASS`, Python 3.12.13 at `2eb21a5` | Fresh `venv`, pip install of `.[dev]`, dependency check, and source bootstrap identity check passed against an exact-commit archive; not wheel completeness or gameplay. |
| Wheel install and MCP EOF launch | `PASS`, Python 3.12.13 at `2eb21a5` | Fresh non-editable install, dependency check, ten public imports, and pinned Red-color MCP startup/EOF cleanup passed outside the checkout; no MCP requests or gameplay exercised. Optional Pillow support was absent. |
| Source unit/timing | `980/980` unit; `55/55` timing across five repetitions | Collection found 1,132 tests; this is not a full real-ROM gate. |
| Source strict battle | `PASS`, `19/19` local/TCP entrypoints in 1,237.8s | No skips, errors, or test-only protocol bypasses; five ROM hashes, three symbol hashes, and ten fixture entries validated in that run. |
| Source strict trade | `FAIL`, `18/19`, four workers | `red_color-listen-blue_color-connect` timed out at Trade Center warp after 722.6s. Targeted passing replays do not replace the failed matrix. |
| Native/Cython | CPython 3.12 Linux wheel built | The isolated build does not establish strict gameplay using the compiled wheel. |
| MCP gameplay | Unproven for starter acquisition, trade, and battle | Historical integration/navigation and lifecycle results do not establish these flows. |

Older PR #17, PR #42, PR #52, source-local 9/9, and native 16/19 trade /
17/19 battle records are historical only; see the retained evidence in
[README](../README.md). They do not supersede this section.

Status semantics:

- `PASS` is scoped to the named command and means clean collection plus no
  failure, error, skip, xfail, or timeout in that selected scope.
- `PENDING` means the required current evidence is not available; it is not a
  passing result or permission to infer coverage from a declaration.
- `PARTIAL` means that controlled evidence exists but a required release
  condition remains open.
- `PRODUCTION-READY` requires the full gate with all required BYO assets,
  a complete strict matrix declaration with every required row executed in
  each required runtime, retained evidence, and a clean candidate with no open
  blocker.

The state parser has an additive validity contract: `GameState.validity` reports
`valid`, `partial`, or `unknown` status with exact missing-symbol, unknown-field,
and invalid-field metadata. Legacy component fields remain available, but
missing symbols and unrecognized values are exposed as unknown instead of
guessed zero or `False`; a partial or unknown observation must not be promoted
to a valid menu, trade, or battle state. This is state-observation evidence,
not live MCP gameplay evidence.

Candidate `1f19707` includes serial idle/external-clock `MAX_CYCLES` handling
beyond `2^31`, dispatch lock-order and admission-deadline fixes, failure-first
gate evidence, and partial pipe-output capture. These hardening and diagnostic
changes do not modify ROMs or establish gameplay acceptance. Experimental
time coordinator `344aa95` exists on another branch and is not included in
this candidate.

## Capability boundary

The current source-runtime trade follow-up above passes 19/19. The retained
source strict battle result is 19/19, but full source release acceptance
remains pending until the candidate's complete required scope is rerun and
compiled-runtime, MCP, and platform evidence are established.
Native gameplay, MCP starter/trade/battle, vanilla fixture provenance,
broad-suite/platform/load coverage,
and independent release review remain open. Six canonical fixture entries have
verified provenance; four vanilla-derived entries remain partial. TCP is
loopback-only, unauthenticated, and unencrypted.

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

For a historical reproduction, use the revision named by its retained report.
For a current release candidate, record the
actual candidate commit and rerun every affected tier; do not layer new
runtime or test changes over the historical result. The working tree should
be clean; ignored BYO assets may be present outside the tracked source. Use
the same activated interpreter for installation, tests, the gate, and MCP.

The required Python version is 3.11 or newer. NumPy is pinned to 2.4.6 on
Python 3.11 and 2.5.2 on Python 3.12+, including the Cython build dependencies.
On Windows PowerShell, create
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

The prior exact-candidate pinned fork had an optional Cython mode whose semantic
serial-contract probe and three canonical real-ROM attach/step/close smokes
passed, but it was not the documented release default. The historical prior
exact-candidate dual source/native unit/timing gate collected 1,094 tests in
each runtime, had 949 unit tests pass, and passed timing 50/50 in each of five
repetitions. The historical prior-candidate local asset tier passed 47/47 in
source in 531.3477 seconds and 47/47 in native in 38.2974 seconds; the remote
asset tier passed 16/16 in source in 35.6044 seconds and 16/16 in native in
30.8627 seconds. These are scoped tests, not
actual MCP gameplay or strict trade/battle evidence. Do not substitute an
arbitrary standalone PyBoy wheel: record the version, harness revision, and
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

The stock ROM/SYM pins and existing vanilla fixture bytes validate, but vanilla
ordinary capture provenance cannot be established: replay against the retained
source failed at the 64-step bound. The manifest's ordinary producer revision
`25e231c` is historical, so these fixture bytes must not be described as
reproducible from the audited implementation/runtime snapshot.

For the currently controlled stateful scope, the BYO asset set is:

- five pinned ROMs: stock and color Red, stock and color Blue, and Yellow;
- three matching symbol files: Red, Blue, and Yellow; and
- six canonical color-Red, color-Blue, and Yellow ordinary/battle states.

The ten-entry manifest therefore contains six canonical states and four
vanilla-derived states. The canonical six have verified provenance; the four
vanilla-derived entries remain `PARTIAL` because their ordinary source-state
provenance is not established. Supply the two vanilla ordinary/battle pairs as
well when validating the checked-in ten-entry manifest. They are not a
supported release scope until a vanilla-ROM-matching source state is supplied
and reproduced.

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
  --matrix-workers 1 \
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

The `--unit-only` selection runs only unit and timing checks, including
schema validation; it does not validate ROM bytes or execute gameplay.
Use the recorded source/native unit/timing results above only for their named scope.
The standard-library editable-install path passed in a fresh Python 3.12.13
environment at `2eb21a5`, including `ensurepip`, pip dependency validation, and
source bootstrap identity. Retain equivalent evidence for a later candidate;
this does not qualify a wheel install or the unselected gameplay tiers.

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

For an explicit dual-runtime gate, prepare a source environment at `.venv` and
a separately installed Cython environment at `.venv-cython`, then run:

```bash
EVIDENCE_DIR="$(mktemp -d)"
.venv/bin/python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$PWD/.venv/bin/python" \
  --cython-python "$PWD/.venv-cython/bin/python" \
  --runtime-mode both \
  --unit-only \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

`--python` selects the source-runtime interpreter and `--cython-python` selects
the Cython interpreter. The latter is accepted only with
`--runtime-mode both`; omitting it makes both modes reuse `--python`. The gate
runs the selected tiers under each explicit runtime and retains both results in
the dual report. The historical prior exact-candidate post-gate run collected
1,094 tests per runtime, had 949 unit tests pass in each runtime, and passed
timing 50/50 in each of five repetitions. This is scoped runtime evidence, not
ROM gameplay or release sign-off; the audited implementation/runtime snapshot
remains `PARTIAL`.

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

To reproduce the scoped canonical boot checks with operator-selected runtime
and asset roots:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src:vendor/pyboy-src \
POKERED_ROM_ROOT="$ROM_ROOT" POKERED_FIXTURE_ROOT="$FIXTURE_ROOT" \
"$RUNTIME_PYTHON" -m pytest -p pytest_asyncio.plugin -o addopts='' -q \
  tests/test_rom_boot.py
```

For native, select the verified native interpreter and use `PYTHONPATH=src`
so installed extensions remain selected. This checks 120 boot frames and
state/save/load only; the passing `1f19707` results above do not replace MCP
or gameplay tiers.

Use one explicit ROM hash. This example uses stock Red:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=ea9bcae617fdf159b045185467ae58b2e4a48b9a \
POKERED_SYM_SHA1=03783c86a42588bd77f73bd7814cf8d70e590118 \
POKERED_VERSIONS_PATH="$PWD/VERSIONS.md" \
python -m pytest -q -ra \
  tests/test_golden_paths.py \
  tests/test_mcp_stdio_integration.py
```

Repeat with the appropriate path, symbol file, and hash for each release input
that will be advertised. The wheel-install probe in this historical audit
verified clean installation, `pip check`, and bundled runtime identity; it did
not run this real-ROM stdio tier on Linux. The historical fresh Windows
validation separately passed
the selected MCP stdio integration (4/4), but did not replace the full
real-ROM tier. At the last verified pre-fix implementation/runtime snapshot
`a8576b5`, six real MCP integration checks passed in 13.11 seconds with one SDL
warning; the accompanying dispatch
suite passed 104 tests. Public MCP tools reached Red's bedroom, the house exit,
Pallet Town, Oak's Lab, and lab movement. A bounded starter attempt ended at
map 40, position `(5,3)`, with `party.count=0`; no test-only bypass was used.
MCP-driven starter acquisition, trade, and battle remain unproven. Any current
real-ROM result must
identify the tested commit, ROM/SYM hashes, and complete test output. These
tests prove only the tested boot/state/MCP surface; they do not prove link
gameplay.

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
remote play. The historical prior-candidate local asset tier passed 47/47 in
source in 531.3477 seconds and 47/47 in native in 38.2974 seconds. This is scoped
local/session/link evidence and does not qualify strict trade or battle.

### Tier D: local link session, trade, and battle acceptance

The more extensive candidate suite is:

```bash
python -m pytest -q -ra \
  tests/test_pyboy_link_session_roms.py
```

The diagnostic matrix in this module is broader than the release acceptance
assertions. Historical LinkMenu, trade, and battle observations are not evidence
for the audited implementation/runtime snapshot. The strict local acceptance
selectors are parametrized over
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
These selectors and their declaration are not live evidence by themselves.
The retained source strict battle result covers all 19 local/TCP entrypoints;
the current working-tree source trade result covers all 19 strict entrypoints.
Every required row must execute and pass without failures or skips for release
acceptance.
Retain the tested commit, runtime, assets, roles, deadlines, and teardown;
historical source-local 9/9 and native results do not qualify another runtime.

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

The historical prior-candidate remote asset tier selected 16 transport/MCP tests
in each runtime. Source passed 16/16 in 35.6044 seconds and native passed 16/16
in 30.8627 seconds. An earlier source failure report used a dirty shared
checkout and shared vendored runtime, so it is invalid and excluded from the
historical result. These historical transport/MCP checks do not establish
current live MCP gameplay, trade, or battle behavior; a LinkMenu milestone is
not live gameplay proof.

The separate-process acceptance uses the same bundled source runtime as the
normal package and the canonical color Red, color Blue, and Yellow fixtures:

```bash
python -m pytest -q -ra \
  tests/test_pyboy_link_session_subprocess.py
```

The LinkMenu test is a transport smoke test. The strict subprocess trade and
battle tests are acceptance selectors: they use cooperative phase rendezvous,
compare complete party-mon records or execute a battle turn, and are
parametrized over all nine ordered canonical listener/connector pairs. Both
payloads travel through native bit-level serial traffic, and the tests reject
the out-of-band exchange counter. A declaration or collection result does not
execute these gameplay assertions. The current working-tree source strict trade
gate passed 19/19; the retained source strict battle matrix passed 19/19,
while the historical trade gate below failed 18/19 with four workers. These are
separate from transport/lifecycle checks and from MCP-driven gameplay. A
complete source release gate and compiled-runtime gameplay qualification remain
open. Record both child traces and exact deadlines when investigating a
regression.

For opt-in subprocess-peer Python stack diagnostics, set
`POKERED_PEER_TRACE_AFTER_SECONDS` to a positive finite number. Optionally set
`POKERED_PEER_TRACE_DIR` to an existing external directory to retain private,
unique per-process logs; otherwise output goes to stderr and may be truncated
by parent capture. At most two one-shot dump schedules are attempted per peer,
including cleanup. This does not terminate the peer, inspect emulator state,
or guarantee native C stack frames. Keep these diagnostic artifacts outside
version control; stack capture is not gameplay evidence.

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
versioned HELLO selects Red as the initial internal-clock side for a Red/Blue
pair, or the non-Yellow endpoint for a Yellow/Red or Yellow/Blue pair. Same-
family pairs retain their caller-provided orientation. The ROM still owns its
connection-status byte and any later role changes. The `passive_sync()` helper
in `tests/_tcp_trade_peer.py` is test-driver-only pre-drive coordination, not a
production MCP synchronization guarantee or gameplay acceptance evidence. The
semantic endpoint remains a compatibility path for non-native test doubles and
is not production acceptance evidence. A real PyBoy with an incomplete native
serial contract
fails closed. Native socket writes, public listener waits, and worker teardown
are bounded; `peer_rom_version` can be supplied to reject an unexpected HELLO
label.

### Strict-matrix accounting

The collection audit verifies declaration shape only; it does not execute trade
or battle. Each operation declares nine ordered local rows, nine ordered TCP
listener/connector rows, and one dedicated Red/Yellow assertion: 19 total.
Listener/connector order matters, and stock-ROM rows and LinkMenu-only
milestones are outside this strict claim.

The current working-tree source trade result is `PASS` at 19/19. The retained
historical source results include battle `PASS` at 19/19 and trade `FAIL` at
18/19; its failed row was Red-color listener to Blue-color connector. No clean
full gate is established for the current candidate. Rerun affected tiers after
runtime changes and retain complete results; diagnostic retries and historical
native results do not replace a passing matrix.

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
POKERED_SYM_SHA1=03783c86a42588bd77f73bd7814cf8d70e590118 \
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
closing either server. For native Red/Blue pairs, versioned HELLO selects Red
as the initial internal-clock side; for Yellow/Red or Yellow/Blue pairs it
selects the non-Yellow endpoint. Same-family pairs retain their
caller-provided orientation. A teardown is complete
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

Release status remains `PARTIAL`. The current source strict trade follow-up
passes 19/19, but before sign-off retain a completed full source gate for the
candidate under review. The recorded source battle 19/19 pass is scoped
acceptance, not a full-gate result. Also establish compiled-runtime gameplay,
current MCP startup/state/action/lifecycle checks, required concurrency and
cleanup regressions, and independent release review. The standard-library
editable-install path has the scoped passing evidence recorded above; retain
clean-install and launch evidence for the candidate being signed off.

Stock-ROM fixture provenance, additional platforms, MCP-driven
starter/trade/battle workflows, and cross-host networking remain unqualified
extensions, not prerequisites for the declared canonical loopback scope.
Cross-host operation would require authenticated encrypted transport; the
current TCP API must remain restricted to loopback.
