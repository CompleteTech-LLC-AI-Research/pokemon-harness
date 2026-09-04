# Production runbook

This runbook defines how to prepare and evaluate a clean `pokered-harness`
checkout. The live target is
[`CompleteDotTech/pokemon`](https://github.com/CompleteDotTech/pokemon). The
current public/docs head is
`f048870bdbd4837b5494006ae49fe40510832a89` (PR #45, docs-only,
2026-09-04). The last verified implementation/runtime snapshot under audit is
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7`; documentation-only public changes
after that snapshot do not change the audited code. Its merged implementation
baseline is
`6d541b7867e82fa548c456008e6acd7fb1071586` (PR #42, 2026-09-04). The prior
merged head `daa1d72f2cc559a6424067a9088dfae1b7b5f7bb` (2026-09-03) and prior
exact candidate snapshot `3399407aa04f6e5e496df628442597c03e0adcc6`
(2026-09-04) are historical references, not current-head release evidence.
External ROMs,
symbols, save states, and sanitized evidence remain operator-managed and are
not distributed by this repository.

The prior exact-candidate snapshot included serial save-state restoration,
native bootstrap ownership and build-metadata cleanup, bounded MCP teardown,
fail-closed gate accounting, and safe cleanup for partially initialized PyBoy
objects. Earlier PR #35 introduced remote serial-edge dispatch at an explicit
native instruction-batch boundary. These implementation notes are historical
context and do not establish current-head runtime or end-to-end behavior.

The release decision for the audited implementation/runtime snapshot remains
`PARTIAL`, not `PRODUCTION-READY`. Current open-gate status is:

- current strict trade remains `PENDING` until the clean source strict-trade
  gate in another isolated worktree supplies a terminal artifact. The valid
  native/Cython strict gate at
  `/tmp/poke-harness-native-strict-evidence-a8576b5` executed all 19 declared
  rows in both its trade and battle tiers, with trade at `16/19` and battle at
  `17/19`; neither result is a full passing strict-matrix sign-off;
- source-local strict battle at the audited implementation/runtime snapshot is
  `PASS` for 9/9 ordered canonical pairs. The native/Cython battle result is
  `17/19`, and no current full remote-matrix pass is established;
- current live MCP gameplay is `PARTIAL`. Six real MCP integration checks passed
  in 13.11 seconds with one SDL warning, and 104 dispatch tests passed. Public
  tools reached Red's bedroom, the house exit, Pallet Town, Oak's Lab, and lab
  movement; bounded starter acquisition ended at map 40, position `(5,3)`, with
  `party.count=0`. No bypass was used, and MCP starter/trade/battle remains
  unproven;
- fixture provenance is `PARTIAL`: the stock ROM/SYM pins and existing vanilla
  fixture bytes validate, six canonical manifest entries have verified
  provenance, and four vanilla-derived entries remain `PARTIAL`. Vanilla
  ordinary capture provenance cannot be established: replay against the
  retained source failed at the 64-step bound, and the manifest's ordinary
  producer revision `25e231c` is historical. Do not claim reproducibility from
  the audited implementation/runtime snapshot for those fixture bytes;
- TCP remains loopback-only, unauthenticated, and unencrypted;
- platform and real-ROM concurrency qualification remain open, as does secure
  cross-host TCP qualification;
- timing-altered diagnostics are not acceptance evidence.

At the last verified implementation/runtime snapshot
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7`, source-local strict battle
acceptance passed all 9/9 ordered canonical pairs. Three fresh rows were
`Yellow↔Red` in 182.89 seconds,
`Blue↔Yellow` in 443.50 seconds, and `Yellow↔Blue` in 480.31 seconds; the
remaining six are prior rows from the same campaign. Every row reached Link
Battle and the required move and damage hooks. This is source-local evidence
from the audited implementation/runtime snapshot only; it does not qualify
remote or native battle, or the full strict trade gate.

The valid native/Cython strict gate for the same implementation/runtime snapshot
is recorded at `/tmp/poke-harness-native-strict-evidence-a8576b5`. It used
Python `3.12.13` and PyBoy `2.7.0`, fork revision
`c565df66c3731fad2856169a90f6bbec99925915`. Trade and battle each had 19/19
declared rows executed: trade passed `16/19`, and battle passed `17/19`. Trade
failures were `blue_color listener -> yellow connector` at 83.39 seconds after
the trade hooks with an incorrect/malformed party record; `red_color listener ->
yellow connector` at 729.51 seconds with a LinkMenu rendezvous timeout after
2,197 balanced/applied edges and one pending request; and `yellow listener ->
blue_color connector` at 721.39 seconds with a LinkMenu rendezvous timeout after
6,248 balanced edges. Battle failures were `blue_color listener -> yellow
connector` at 376.94 seconds because sync marker 113 did not converge after
18,840 applied edges; and `yellow listener -> blue_color connector` at 142.20
seconds because the battle Colosseum warp rendezvous did not converge after 952
balanced edges. No native owner/IRQ errors were observed. All five failures are
strict real-ROM failures, not bypasses. This is partial runtime evidence, not
production sign-off.

The implementation baseline merged in PR #42 has source-runtime representative
evidence for in-process and TCP Red-color/Yellow trade and battle. Its source
unit/timing gate passed 954/954 unit tests and 50/50 timing cases in each of
five repetitions. These results validate scoped candidate behavior only; they
do not close the full strict trade matrix, native-runtime, remote full-matrix,
MCP gameplay, fixture-provenance, platform, or security gates.

Historical prior exact-candidate evidence retained for context is:

- a clean dual source/native `--unit-only --repeat-timing 5` gate collected
  1,094 tests in each runtime; each runtime had 949 unit tests pass and timing
  pass 50/50 in each of five repetitions. This is unit/timing evidence, not
  ROM-backed gameplay evidence;
- exact asset preflight matched 5/5 ROMs, 3/3 symbol files, and 3/3 fixture
  hashes by SHA-1, and the ten-entry fixture manifest byte validation passed;
- the local asset tier passed 47/47 in source in 531.3477 seconds and
  47/47 in native in 38.2974 seconds. This is scoped local/session/link
  evidence, not strict trade or battle proof;
- the remote asset tier passed 16/16 in source in 35.6044 seconds and
  16/16 in native in 30.8627 seconds. This is scoped remote transport/MCP
  evidence, not strict trade or battle proof. An earlier source failure report
  used a dirty shared checkout and shared vendored runtime, so it is invalid
  and excluded from the prior-candidate result.

The historical prior exact-candidate separate-interpreter dual source/native
`--unit-only --repeat-timing 5` gate collected 1,094 tests in each runtime.
Each runtime had 949 unit tests pass and timing pass 50/50 in each of five
repetitions; source
reported `python-source` and Cython reported `cython/native-extension`. This
selection does not execute ROM-backed tiers, strict trade, or battle. Fresh
uv-managed source and native environments installed the candidate, passed
`uv pip check`, and the native bootstrap verified both `pyboy` and
`pokered-harness` owners. An earlier environment-specific 585/586 ownership
result is superseded for these isolated environments. Source mode is the
documented release runtime; Cython is an optional diagnostic build. The host's
bare `python3` still lacks `ensurepip`, so that alternate standard-library venv
path remains open.

The historical prior exact-candidate asset-backed reports used pinned ROM/SYM/
fixtures and separate source and native (Cython) interpreters. The local asset
tier passed 47/47 in source in 531.3477 seconds and 47/47 in native in 38.2974 seconds;
the remote transport/MCP tier passed 16/16 in source in 35.6044 seconds and
16/16 in native in 30.8627 seconds. The earlier source remote failure report
used a dirty shared checkout and shared vendored runtime, so it is invalid and
excluded. These results are scoped local/session and remote transport/MCP
evidence only; they do not qualify strict trade or battle. The exact SHA-1
matches establish supplied asset identity, not strict trade/battle
qualification.

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
complete without assets; skipped real-ROM tiers remain unverified. Historical
fresh Windows validation covers install, source/Cython bootstrap,
MCP stdio, and three-ROM Cython lifecycle checks, but not full native
gameplay, real-ROM concurrent load, the remote trade/battle matrix, or macOS.

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

## Capability boundary

| Capability | Current status | Evidence boundary |
|---|---|---|
| Single session and MCP | `PARTIAL` for scoped live MCP navigation; release remains `PARTIAL` | Six real MCP integration checks passed in 13.11 seconds with one SDL warning, and 104 dispatch tests passed. Ordinary real-ROM navigation reached Red's bedroom, the house exit, Pallet Town, Oak's Lab, and lab movement; the bounded starter attempt ended at map 40, position `(5,3)`, with `party.count=0`. MCP starter/trade/battle remains unproven. Historical Windows MCP stdio separately passed 4/4. |
| In-process paired link | `PASS` for source-local strict battle at `a8576b5`; trade remains `PENDING`; release remains `PARTIAL` | Source-local strict battle passed 9/9 ordered canonical pairs at the audited implementation/runtime snapshot. The historical prior-candidate local asset tier passed 47/47 in source in 531.3477 seconds and 47/47 in native in 38.2974 seconds. This does not establish native or remote parity, or a passing strict trade result. |
| Remote TCP transport | `PASS` for scoped historical transport evidence; live gameplay remains `PENDING`; release remains `PARTIAL` | The historical prior-candidate remote asset tier passed 16/16 in source in 35.6044 seconds and 16/16 in native in 30.8627 seconds. TCP is loopback-only and has no authentication or encryption. |
| Remote trade | `PENDING` | The clean source strict-trade gate is still running in another isolated worktree. The native/Cython strict gate executed all 19 declared rows but passed 16/19; its three strict real-ROM failures are recorded above. Historical source `19/19` and native `18/19` rows are not current full-runtime sign-off. |
| Remote battle | `PARTIAL` for native/Cython strict evidence; release remains `PARTIAL` | Source-local strict battle passed 9/9, but that is not remote evidence. The native/Cython strict gate executed all 19 declared rows and passed 17/19; its two strict real-ROM failures are recorded above. No current full remote-matrix pass is established. Historical source/native rows and LinkMenu milestones are not current live battle proof. |
| Manual fixtures | `PASS` for supplied-byte identity; `PARTIAL` for provenance | Historical prior-candidate preflight verified the pinned ROM/SYM and existing fixture bytes. Six canonical manifest entries have verified provenance; four vanilla-derived entries remain `PARTIAL` because vanilla ordinary capture provenance cannot be established. The historical producer revision `25e231c` and failed 64-step replay do not support reproducibility from the audited implementation/runtime snapshot. |
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

The historical prior exact-candidate dual `--unit-only --repeat-timing 5` check collected
1,094 tests in each runtime, had 949 unit tests pass, and passed timing 50/50 in
each of five repetitions. Source reported `python-source`; Cython reported
`cython/native-extension`. The gate also performs schema-only validation of the
ten-entry fixture manifest. Because `--unit-only` selects only `unit` and
`timing`, this check does not validate ROM bytes, load save states, exercise MCP
with a real ROM, or run link gameplay; it is never a production sign-off by
itself. Both uv-managed environments passed `uv pip check`, and the native
bootstrap verified the `pyboy` and `pokered-harness` owners. An earlier
environment-specific 585/586 ownership result is superseded for these isolated
environments. The host's bare `python3` still lacks `ensurepip`, so that
alternate standard-library venv path remains open.

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
real-ROM tier. At the last verified implementation/runtime snapshot
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
These selectors and their declaration are not live evidence by themselves; a
current complete run must provide the retained result for every row. A skipped
or partially parameterized matrix is not full Red/Blue/Yellow coverage. The
last verified snapshot's source-local battle run passed 9/9 ordered pairs: fresh
`Yellow↔Red` (182.89 seconds), `Blue↔Yellow` (443.50 seconds), and
`Yellow↔Blue` (480.31 seconds), plus the existing six campaign rows. All rows
reached Link Battle and the required move and damage hooks. This does not close
the current strict trade gate, which remains `PENDING` pending the clean source
terminal artifact; the native/Cython strict gate is also not a pass at trade
`16/19` and battle `17/19`; and no current remote full-matrix pass is
established. The dedicated Red/Yellow assertions remain useful focused checks,
but the production gate still requires every strict row to execute and pass
without failures or skips.

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
execute these gameplay assertions. The PR #17 baseline passed the strict trade
and battle matrices; the PR #19 follow-up reran the 15-row remote transport
slice and the focused transport/lifecycle checks, not the full strict
trade/battle matrix. The current remote full matrix remains `PENDING` for
release sign-off: the native/Cython artifact above is a partial strict result,
and the source-local 9/9 battle result above must not be relabeled as remote
evidence. Record both child traces and the exact deadline when investigating a
regression.

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

The collection audit verifies declaration shape only; it does not execute trade
or battle. The valid native/Cython strict artifact at
`/tmp/poke-harness-native-strict-evidence-a8576b5` executed all 19 declared rows
in each strict tier, with trade `16/19` and battle `17/19`; the five failures
are strict real-ROM failures detailed above, not bypasses. The clean source
strict-trade gate remains `PENDING` until its terminal artifact is supplied.
The PR #17 complete source-runtime baseline executed every declared row; the
PR #19 follow-up did not silently relabel that baseline as a full strict-matrix
rerun. The strict runtime scope is:

| Operation | Local ordered rows | Remote ordered listener/connector rows | Dedicated assertion | Current status |
|---|---:|---:|---|---|
| Trade | 9 | 9 | Red/Yellow party-record swap | `PENDING`: the clean source strict-trade gate is still running; native/Cython executed 19/19 declared rows and passed 16/19. Historical source `19/19` and native `18/19` rows are not current full-runtime sign-off |
| Battle | 9 | 9 | Red/Yellow resolved-turn assertion | `PARTIAL`: source-local strict battle passed 9/9 ordered pairs; native/Cython executed 19/19 declared rows and passed 17/19. No current full remote/runtime battle pass is established |

That is 19 strict entrypoints per operation. The remote rows use canonical color
Red, color Blue, and Yellow profiles; listener/connector order is significant.
The PR #17 baseline completed both strict matrices with no failed rows, but
that is historical evidence. A historical separate source trade run recorded
all 19 rows, and the historical native strict-trade result was 18/19 with mixed
exact-row follow-ups; those are historical scoped results, not current
full-runtime sign-off. A
historical native direct remote battle lane covered only 3/9 rows under a
bounded 155-second per-pair deadline, with `Blue-color↔Blue-color` passing and
`Red-color↔Yellow` plus `Yellow↔Red-color` failing; no bypasses were used and
six rows remained unrun. These historical observations, the current remote
LinkMenu result, and the declaration itself do not qualify current trade or
battle. Resolve the current failed rows and rerun the complete strict matrices
at the documented conservative worker count after any further runtime change.
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

The audited implementation/runtime snapshot
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7` remains
`PARTIAL`, not `PRODUCTION-READY`. The observed blockers are:

1. The historical prior exact-candidate dual source and Cython unit/timing
   gate collected 1,094
   tests in each runtime, had 949 unit tests pass, and passed timing 50/50 in
   each of five repetitions. This historical scoped result does not close
   audited implementation/runtime snapshot's ROM-backed gameplay, platform, or
   release-sign-off requirements. The explicit dual
   invocation uses `--python` for the source environment and
   `--cython-python` for the native environment; its green unit/timing result
   does not replace the required real-ROM tiers. A new standard-library
   virtual environment was not created for that run because its `python3`
   lacked `ensurepip`; the passing gates used existing managed environments.
   Source mode remains the documented release runtime; Cython is optional and
   must be explicitly selected.
2. The historical prior-candidate remote asset tier passed 16/16 in source in
   35.6044 seconds and 16/16 in native in 30.8627 seconds. An earlier source failure
   report used a dirty shared checkout and shared vendored runtime, so it is
   invalid and excluded from the historical result. This is historical remote
   transport/MCP evidence, not current live MCP gameplay, trade, or battle
   proof. The valid native/Cython strict artifact at
   `/tmp/poke-harness-native-strict-evidence-a8576b5` executed all 19 declared
   rows in both strict tiers, passing `16/19` trade and `17/19` battle. Its
   failures are strict real-ROM failures, not bypasses, and no native owner/IRQ
   errors were observed. The clean source strict-trade gate remains `PENDING`
   pending a terminal artifact; source-local strict battle is `PASS` for 9/9,
   but no current full remote/runtime pass is established. Timing-altered
   diagnostics do not close either matrix.
3. The strict declaration is complete: nine ordered local pairs, nine ordered
   remote listener/connector pairs, six reversed-role rows, nine local variant
   rows, and 19 trade plus 19 battle entrypoints collect. Collection is
   structural evidence only; it does not execute trade or battle. The valid
   native/Cython artifact executed all 19 rows in each strict tier but passed
   only 16/19 trade and 17/19 battle. The PR #17 source baseline's 19/19 trade
   and 19/19 battle results are historical. The broad
   suite remains an incomplete pre-PR #27 diagnostic: 418/703 tests completed
   before the 5,400-second bound (386 passed, 20 skipped, 12 failed, and 285
   not started). Real-ROM concurrent-load stability, full native-platform
   qualification, independent review, and a complete post-fix broad rerun
   remain open. Scoped Windows install/bootstrap/MCP/Cython lifecycle evidence
   does not replace full Windows gameplay/load or macOS coverage.
4. The historical prior-candidate asset preflight verified exact SHA-1 matches
   for 5/5 ROMs, 3/3 symbols, and 3/3 fixture hashes, with the ten-entry
   manifest byte validation passing. The historical prior-candidate local asset
   tier passed 47/47 in source in 531.3477 seconds and 47/47 in native in
   38.2974 seconds; the historical prior-candidate remote asset tier passed
   16/16 in source in 35.6044 seconds and 16/16 in native in 30.8627 seconds.
   This establishes supplied asset and scoped runtime evidence only; the ten
   states remain external/operator-managed. Current fixture provenance is
   `PARTIAL`: six manifest entries are canonical with verified provenance,
   while four are vanilla-derived with `PARTIAL` provenance; derived vanilla
   battle states inherit that status. The stock ROM/SYM pins and existing
   vanilla fixture bytes validate, but vanilla ordinary capture provenance
   cannot be established: replay against the retained source failed at the
   64-step bound, and the manifest's ordinary producer revision `25e231c` is
   historical. Do not claim reproducibility from the audited
   implementation/runtime snapshot for those fixture bytes. A release run must
   retain sanitized evidence with the ROM, symbol,
   fixture, runtime, and teardown identities.
5. Remote TCP is enforced as loopback-only and provides no authentication or
   encryption. Cross-host operation is blocked until a secure transport is
   added; `peer_rom_version` is a label check, not authentication.

The smallest next actions are to obtain the terminal source strict-trade result,
resolve and rerun the failed native/Cython strict trade and battle rows, establish
current live MCP gameplay, native-platform and real-ROM load evidence, verify
vanilla fixture provenance, finish the broad suite, and obtain independent
release review. Retain the historical prior-candidate local and
remote reports and their runtime, timing, asset-hash, and teardown identities
when reproducing the scoped evidence above.
