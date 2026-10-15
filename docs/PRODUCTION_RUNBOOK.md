# Production runbook

This runbook defines how to prepare and evaluate a clean `pokered-harness`
checkout. The live target is
[`CompleteDotTech/pokemon`](https://github.com/CompleteDotTech/pokemon). The
current integration candidate is based on
`7c207bd` on `sync/20260907-poke-harness` (2026-09-07) plus uncommitted
runtime, packaging, lifecycle, and test changes. External ROMs, symbols, save
states, and sanitized evidence remain operator-managed and are not distributed
by this repository.

The release decision for this candidate is `PARTIAL`, not `PRODUCTION-READY`.
The [release checklist](RELEASE_CHECKLIST.md) is the authority for latest
integrated source/native validation and remaining blockers. Historical
inventories are separate from the latest working-snapshot gate.
MCP admission, locking, mode-exclusion, scheduler, and cleanup checks do not
qualify a complete trade or battle matrix or change the committed runtime pin.

The 2026-09-08 working snapshot integrates 512-T-cycle normal serial timing,
the latched backend fault boundary, physical-time scheduling across CPU-speed
transitions, common raw/managed ownership, and native owner-pump receive. Run
the documented bootstrap check: it verifies actual timing, fault behavior,
installation ownership, and module kind. After `SerialBackendError`, recreate
the emulator sessions; do not retry an edge, replace an attached core, or load
state to clear the fault. Older native builds with the same revision marker can
fail this strengthened contract and require a fresh build in a separate
environment.

The latest admission integration rejects stale or claimless attached cores
before emulator-backend writes or legacy promotion and removes the
`PyBoyLinkSession` role-status seeding path; the native constructor's role
keyword is inert. Native remote receive uses exclusive owner-pump claims and
pre-CPU/HALT polls; sampling, shifts, and IRQs run on the emulator owner, not
transport workers. Bootstrap exercises these behaviors and rejects older builds
with the same revision marker.

Five local MCP guard tests use injected pairs and reject real/native-capable
endpoints before semantic pairing mutates state. This is local admission
evidence only; it is not a new socket guard or gameplay acceptance.

The pre-owner-post diagnostics source/native gates each passed 925 unit tests and
685 timing executions (137 cases repeated five times), with no failures,
errors, skips, xfails, or xpasses. All 14 asset hashes match. Source used
CPython 3.12.3 (32.286/142.790 seconds for unit/timing); native used CPython
3.12.13 (26.218/98.208 seconds). Retained reports are
`shared-diagnostics-source-gate.json` and `shared-diagnostics-native-gate.json`.
These runs follow a test-only descendant-readiness fix and improved bounded
gate failure diagnostics. An intervening source run failed the descendant
timeout test (919/920 unit, 670/670 timing); its retained report is
`shared-docs-source-gate.json`. The original traceback was not retained, so
the startup race found during review is not a proven cause of that failure.
No serial owner-post candidate changes are included in these passing gates.

The preceding source boot gate passed 920 unit tests and 670 timing executions
(134 cases repeated five times) under CPython 3.12.3 (129.961/479.402 seconds),
with zero failures, errors, skips, xfails, or xpasses and all 14 asset hashes
matched. The current native boot gate passes the same 920/670 scope under
CPython 3.12.13 (67.452/229.558 seconds); the prior source/native stdio 919/670
results remain retained as baselines. Both fresh separate Linux environments used the
exact 43-version dependency constraints, `pip check`, bootstrap, and no caller
`PYTHONPATH`; the gate's internal paths selected the shared harness. The native
gates reused the existing claim-fork wheel rather than making a new native
build. These are source/native gate evidence, not a clean committed-clone,
full trade/battle, intro-completion, or broad gameplay qualification. The
retained gate records are `shared-rom-boot-source-gate.json`,
`shared-rom-boot-native-gate.json`, `shared-stdio-source-gate.json`, and
`shared-stdio-native-gate.json`.

The integrated stdio helper passed four single-session cases in each runtime
with an actual child return code of zero under a 120-second deadline. The shared
stdio suite passed 6/6 in source (35.35 seconds) and native (28.83 seconds);
both Red/Blue remote directions used two child servers that exited zero and
reclaimed their ports. The retained records are
`isolated-stdio-teardown-source.xml`, `isolated-stdio-teardown-native.xml`,
`shared-stdio-teardown-source.xml`, `shared-stdio-teardown-native.xml`, and
`stdio-summary.json`. This is MCP lifecycle/input/save-restore evidence, not a
full trade, battle, or intro-completion qualification.

The integrated five-variant ROM boot scope passed 31/31 in source (116.64
seconds) and native (13.85 seconds), covering `red-stock`, `red-color`,
`blue-stock`, `blue-color`, and `yellow` boot/advance/replay cases plus 26 gate
checks. Each XML property records actual ROM and symbol hashes matching the
pinned values; `replay_sha256` equals `expected_replay_sha256` and is distinct
from `checkpoint_sha256`; and `teardown_status=closed`. This
qualifies only boot/advance/replay/save-load/close for the five inputs; it does
not claim intro completion, trade, battle, or broad gameplay. The owner pre/post
callback API and its cross-runtime regressions are now integrated.
The integrated source gate now passes 936 unit tests and 685 timing executions
(137 cases repeated five times), with zero failures, errors, skips, xfails, or
xpasses and all 14 supplied asset hashes matching. The five source ROM
boot/state/replay checks also pass. The retained gate summary is
`shared-owner-post-source-gate-summary.json` (a transcription of completed
stdout, not the full gate JSON); boot evidence is
`shared-owner-post-source-boot.xml` in the operator evidence directory.
The isolated installed-native gate also passes 936 unit tests and 685 timing
executions with zero failures, errors, skips, xfails, or xpasses; all 14 asset
hashes match. All five native ROM boot/state/replay checks pass, including
verified replay hashes and closed sessions. Native evidence is retained as
`shared-owner-post-native-gate.json` and
`shared-owner-post-native-boot.xml`. These results qualify the scoped Linux
source/native runtime checks, not a clean committed release or gameplay. This
API foundation does not implement authentic clock negotiation. The source and
native records are `shared-rom-boot-source.xml` and
`shared-rom-boot-native.xml`. The full source/native gate reports are
`shared-rom-boot-source-gate.json` and `shared-rom-boot-native-gate.json`.

The prior receiver/owner snapshot's 906-unit/660-timing and 150-focused
results are historical; earlier six-case stdio results predate the current
receiver/admission changes.

The default native MCP attachment no longer selects a clock role by writing
`hSerialConnectionStatus` into game RAM. Legacy `SerialBridge` and
`RemoteLinkEndpoint` compatibility paths still contain semantic/RAM shortcuts
outside that default MCP path and require a reachability audit. The isolated
clock-phase/save-v18 experiment has a 290-test source result but no native
qualification. A fresh strict trade or battle matrix remains open.

No complete passing strict trade or battle matrix qualifies this runtime.
Earlier failed native matrices remain evidence, not post-receiver-fix results;
see the checklist's [current result](RELEASE_CHECKLIST.md#current-candidate-result).

The historical pre-clock-repair native local battle matrix (128 T-cycles per
bit) completed in 562.00 seconds: four passed,
five failed, no skips. Red/Yellow and Blue/Yellow passed both orders; all
same-version pairs and both Red/Blue orders failed. Failures include missing
peer exchange, a missing settled boundary, and invalid/nonmatching observed
combatant state. The exact native Red/Yellow party-record trade row passed
separately in 20.50 seconds. A historical source MCP link dispatch lifecycle
smoke also passed 1/1 (listen/connect/status/disconnect), separately from the
stdio checks.

A historical role-seeded source Red-color→Blue-color remote row passed
same-exchange mirrored battle state verification in 455.93 seconds under a
600-second deadline.
Both peers closed with exit zero and 6,032 balanced edges, no synthetic
keepalive bits, and no out-of-band data exchange. Production role seeding
was used in that snapshot, so this is a settled-turn diagnostic and not full
acceptance of the current no-role-RAM path.

Dated installation, gate, platform, and gameplay results—including failed
and timed-out runs—are retained in the checklist's [historical evidence](RELEASE_CHECKLIST.md#historical-evidence-retained-for-comparison).
Use those records for comparison, not as qualification of later changes.

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
| Single session and MCP | Source/native gates and scoped stdio lifecycle pass; gameplay unqualified | See the checklist's [current candidate result](RELEASE_CHECKLIST.md#current-candidate-result) for the integrated gates and four single-session plus six shared-stdio lifecycle results; neither clean-install nor gameplay qualification follows. |
| In-process paired link | Implemented; full matrix unqualified | The checklist retains [historical evidence](RELEASE_CHECKLIST.md#historical-evidence-retained-for-comparison), including narrow passes and failed/timed-out battle rows; the integrated full matrix remains unqualified. |
| Remote TCP transport | Implemented; gameplay unqualified | Transport and lifecycle tests do not establish a real-game trade or battle. TCP is loopback-only and has no authentication or encryption. |
| Remote trade | Unqualified for this candidate | A release claim requires a current strict matrix with matching ROMs, fixtures, runtime identity, bounded deadlines, and clean teardown. |
| Remote battle | Unqualified for this candidate | A release claim requires a current strict matrix for the advertised rows; historical or representative rows are insufficient. |
| Pinned ROM boot/advance/replay/save-load/close | Scoped 5/5 source/native evidence | The integrated XML records five pinned variants, matching hashes, deterministic replay, and closed teardown; it does not establish intro completion, trade, battle, or broad gameplay. |
| Manual fixtures | Required BYO inputs | Operators must supply five pinned ROMs, three symbols, and six canonical color ordinary/battle states; all ten manifest entries are needed for separate operator-managed byte validation. |
| Option-B / RAM-boost walkthroughs | Diagnostic only | Direct game-memory writes and `--option-b` shortcuts are not human-valid gameplay or release acceptance. |

## 1. Start from a clean checkout

Use a fresh clone or an isolated worktree. Do not use a dirty development
checkout as release evidence.

```bash
git clone https://github.com/CompleteDotTech/pokemon.git poke-harness
cd poke-harness
git submodule update --init --depth 1 vendor/pyboy-src
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

For WSL, if dependency imports or startup are blocked by Windows-mounted
filesystem I/O, use a virtual environment and, when practical, a checkout on
the Linux filesystem. Keep the commands relative and portable; this is an
operational workaround, not proof that a particular OS or filesystem is the
underlying cause.

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

The pinned PyBoy source is a submodule. A release commit must include both the
`vendor/pyboy-src` pointer and `.gitmodules`; otherwise a clean clone cannot
materialize the runtime used by the package.

The default installed runtime is PyBoy source mode, verified with:

```bash
python scripts/bootstrap_pyboy.py --mode source --check
```

The pinned fork supports Cython mode. The current native boot gate above
passes the 920-unit/670-timing runtime scope and verifies the compiled serial
contract, while the retained source/native stdio baseline gates each recorded
919/670; neither qualifies gameplay. The historical focused source and Cython
serial-link+network suite passed 78/78 in each runtime. The historical native
strict-trade gate recorded 18/19; exact-row follow-ups have both passed and
failed, including a party mismatch and phase stalls, so reliability is
unproven. The current native battle matrix is not qualified.
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

The checklist retains the [historical evidence](RELEASE_CHECKLIST.md#historical-evidence-retained-for-comparison) for earlier unit/timing
failures and subsequent fixes. The gate retains the earliest failure tail.
With assets present, `--unit-only` still hashes the five pinned ROMs, three
symbols, and six required fixtures during preflight. It selects only `unit` and `timing` test tiers, so it does not load
save states, exercise MCP with a real ROM, or run link gameplay. Missing BYO
assets are permitted for this profile. It is never production sign-off by itself.

For the optional native qualification, first build the pinned fork and then
select Cython explicitly. The gate verifies that all required PyBoy modules are
installed extensions before running any selected tier:

```bash
python scripts/bootstrap_pyboy.py --mode cython
python scripts/bootstrap_pyboy.py --mode cython --check
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --runtime-mode cython \
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
that will be advertised. The post-network stdio suite passed 6/6 in
source mode (99.711 seconds) and 6/6 with the shared harness and native
runtime (21.523 seconds). Earlier results are retained in the
checklist's [historical evidence](RELEASE_CHECKLIST.md#historical-evidence-retained-for-comparison).
Alongside single-session press/step, resources, and stronger save/load replay
hash checks, it starts two real servers for each Red-color/Blue-color
direction and verifies remote connect/disconnect, socket reuse, and zero-exit
teardown. Supply both versions' pinned color ROMs and symbols under `rom/`
or set `POKERED_ROM_ROOT` to the operator-managed asset root; otherwise the
remote rows skip. No save-state fixture is used by those lifecycle rows.
The wheel installation check and these real-ROM stdio checks are separate
evidence. Native stdio selected the shared harness through an explicit
checkout import path; it is not a new clean native installation.
Historical Windows validation separately passed
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

#### Exact-frame inspection checkpoints

For a natural Red/Yellow local trade diagnostic, capture both emulators at
selected completed pair-frame boundaries. Use an explicit directory outside
the checkout; do not commit the generated states or manifests:

```bash
python scripts/diagnose_pair_trade.py \
  --output-dir /tmp/pokered-trade-inspection-unique \
  --capture-frame 200,800,1600 \
  --max-captures 4 --deadline-seconds 600
```

Frame zero is captured automatically and counts toward the limit. The driver
preserves requested input order and splits frame batches into one-quantum
owner calls, also respecting selected capture boundaries. Each checkpoint contains two state blobs plus a manifest
with hashes, CPU/serial/physical-clock metadata, input history, maps, and hook
counters. It saves under both emulator-owner locks and the provider lock, then
writes files after releasing those locks. A manifest is written only after its
two state files. Unavailable native fields are reported as unknown.

These are **inspection-only artifacts**, not resumable linked sessions or
acceptance evidence. Save states omit coordinator, callback, and Python input
queue state. Do not load them into an attached pair. `terminal.json` records the
final stage, reached capture frames, errors, and cleanup outcomes. A provisional
record with `cleanup_pending=true` is written before teardown, then replaced
with final results. SIGINT/SIGTERM are handled cooperatively; a process
killed externally may leave checkpoints without a terminal record. The wall
deadline is checked between emulator operations, not an interrupt of native
execution. Use a separate outer process deadline when supervising a run.

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
out-of-band exchange counter. The battle menu driver now uses normal DOWN/A
input and its verifier now requires same-exchange settled snapshots with
mirrored state and terminal cleanup. Historical role-seeded rows, including
the earlier 335-second hook-only battle row, are diagnostics; they do not
qualify the current no-role-RAM path. The PR #17 baseline
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

Authentic acceptance must not seed `hSerialConnectionStatus` (the ROM-owned
HRAM status populated by its serial ISR) or install semantic exchange hooks.
The default native MCP attachment no longer invokes
`PyBoyLinkSession._seed_network_role_status`, and its constructor role keyword
is inert; the current 21-test source/native session scope observed no role-
status writes. Legacy `SerialBridge` and `RemoteLinkEndpoint` compatibility
paths still contain semantic/RAM shortcuts outside the default MCP path and
require a reachability audit, so this is not a claim about every public API.
The listener and connector provide default clock roles, and versioned HELLO
can select the non-Yellow endpoint as initial internal clock source for a
Yellow/Red or Yellow/Blue pair. These hints are not proof of authentic ROM
negotiation. MCP remote admission requires the pinned serial contract,
including for test doubles; there is no automatic semantic endpoint fallback.
Explicitly injected local test pairs remain test-only and are not production
acceptance evidence. Transport waits have deadlines, but whole-disconnect
completion with an active emulator callback remains unqualified.
`peer_rom_version` can be supplied to reject an unexpected HELLO label.

Managed `Session.close(timeout_s=...)` bounds acquisition of exclusive
emulator access only; it does not bound PyBoy's own `stop` implementation.
A cleanup-lock timeout leaves the session closed to new actions and rejects further
operations; retry close to finish cleanup once the active operation releases
the lock. It is not proof that the emulator stopped or teardown completed.

### Strict-matrix accounting

The collection audit verifies declaration shape only. The PR #17 complete
source-runtime baseline executed every declared row; the PR #19 follow-up did
not silently relabel that baseline as a full strict-matrix rerun. The strict
runtime scope is:

| Operation | Local ordered rows | Remote ordered listener/connector rows | Dedicated assertion | Current status |
|---|---:|---:|---|---|
| Trade | 9 | 9 | Red/Yellow party-record swap | A prior source gate recorded 9/9 local and 9/9 remote rows. The native strict-trade matrix has a historical 18/19 result; exact-row follow-ups both passed and failed, including a party-record mismatch and phase stalls, so reliability is unproven |
| Battle | 9 | 9 | Red/Yellow turn assertion | Historical source baseline passed 9/9 local and 9/9 remote rows. A historical native direct remote lane exercised 3/9 rows under a bounded 155-second per-pair deadline: `Blue-color↔Blue-color` `PASS`; `Red-color↔Yellow` and `Yellow↔Red-color` `FAIL`; that report recorded no bypasses and six remote rows were unrun. The current complete battle matrix and settled-outcome verification remain unqualified |

That is 19 strict entrypoints per operation. The remote rows use canonical color
Red, color Blue, and Yellow profiles; listener/connector order is significant.
The PR #17 baseline completed both strict matrices with no failed rows. The
full baseline gate recorded source-runtime remote rows, listener/connector
roles, bounded deadlines, and clean child teardown. A prior source trade
gate recorded all 19 trade rows. The native strict-trade result is historical
at 18/19, and mixed exact-row follow-ups leave its reliability unproven. The
historical native direct remote battle lane covered only 3/9 rows under a bounded
155-second per-pair deadline, with `Blue-color↔Blue-color` passing and
`Red-color↔Yellow` plus `Yellow↔Red-color` failing; the report recorded no bypasses and
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
tool/resource discovery, input, state round-trip, remote listen/connect/status/
disconnect in both Red/Blue directions, repeat listen/cancel, socket reuse,
and zero-exit stdio subprocess teardown. It does not establish disconnect on
EOF while a remote link is still active, or certify link gameplay.

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

1. Commit a coherent new fork revision containing the integrated 512-T-cycle
   timing, deadline, latched-fault, and admission changes, then reproduce the
   retained source gate and current native boot gate from a clean committed
   clone and a fresh native build. The current source boot gate passes 920 unit
   and 670 timing executions (134 cases repeated five times) under CPython
   3.12.3 (129.961/479.402 seconds). The current native boot gate passes the
   same 920/670 scope under CPython 3.12.13 (67.452/229.558 seconds), with
   zero failures, errors, skips, xfails, or xpasses and all 14 asset hashes
   matched. Both fresh separate environments used the exact 43-version
   dependency set, `pip check`, bootstrap, and no caller `PYTHONPATH`; the
   gate selected its internal shared-harness paths. Native reused the existing
   claim-fork wheel, so this is not a fresh native build. Current records are
   `shared-rom-boot-source-gate.json`, `shared-rom-boot-native-gate.json`,
   `shared-stdio-source-gate.json`, and `shared-stdio-native-gate.json`. These
   remain separate from clean-clone, full
   trade/battle, intro-completion, and broad gameplay qualification.
2. Complete authentic simultaneous clock negotiation and current strict
   source/native trade and battle rows. The default native MCP attachment no
   longer seeds `hSerialConnectionStatus`; the native role keyword is inert.
   Legacy `SerialBridge` and `RemoteLinkEndpoint` compatibility paths still
   contain semantic/RAM shortcuts outside the default MCP route and require a
   reachability audit. The local latched fault boundary and same-exchange
   settled verifier are implemented, but version-1 frames lack timestamp,
   epoch, edge identity, and clock-phase grants; owner confinement alone does
   not prove authentic negotiation or gameplay. The full rows still require
   pinned assets, bounded deadlines, and clean teardown.
   Physical-time scheduling, common raw/managed ownership, native receiver
   owner-thread shifts/IRQs, stale-core admission, and invalid-admission
   pre-write guards are integrated. Failed receiver startup still performs its
   bounded transport stop/restore cleanup.
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

The next steps are to reproduce the retained source and native boot gates from
a clean committed clone and fresh native build, audit reachability of legacy
semantic/RAM compatibility paths,
resolve the isolated clock-phase model's ordinary-ROM boundary failures, and
complete causal timing and authentic negotiation. Native receiver owner
execution, network-deadline hardening, stale-core admission, and invalid-
admission pre-write guards are integrated; failed receiver startup still
performs bounded transport stop/restore cleanup. These scoped results and
historical role-seeded diagnostics do not qualify the full simultaneous-input
gameplay matrix. Then
record a new fork pin and clean-install reproduction, complete current
strict source/native matrices and broad/platform/load qualification, verify
vanilla fixture provenance, and obtain independent release review.
