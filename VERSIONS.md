# Version and asset pins

This file records the candidate runtime and ROM inputs used by the harness. A
pin identifies bytes or a dependency version; it is not, by itself, a release
certification. The repository does not distribute ROMs, symbol files, save
states, or other ROM-derived artifacts.

Status: `PARTIAL` at public docs head
`f048870bdbd4837b5494006ae49fe40510832a89` (current public docs head,
2026-09-04), with the last verified pre-fix implementation/runtime snapshot
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7` (code snapshot, 2026-09-04).
Public changes after that snapshot include documentation and the targeted
post-snapshot code fix described below; strict evidence remains tied to the
audited pre-fix runtime unless explicitly labeled otherwise, and the post-fix
serialized source strict-trade gate below is explicitly labeled. The targeted
fix is integrated as `7b4b5b72ad373d2293d51e3d11717314606ee442` (cherry-picked
as `7b4b5b7`): it moves stale `EDGE_RESP` closure outside
`_edge_response_lock` and adds a bounded regression test. Its focused
post-fix network suite passed 38/38, and Ruff/format checks were clean. The
post-fix serialized source strict-trade production gate is recorded below as
an overall `FAIL` at `18/19`; no post-fix strict native result is claimed, and
this source result does not qualify the strict acceptance gates as passing. The
earlier implementation baseline was
`6d541b7867e82fa548c456008e6acd7fb1071586` (PR #42). The prior merged
implementation head was `71ca834d673c52eb74044089e92b09e7e3ae00a0`. This exact
docs-head identity, pre-fix implementation snapshot, and targeted fix establish
the current audit scope; they do not promote superseded gameplay evidence.
Earlier PR #35
introduced remote serial-edge dispatch at an explicit native instruction-batch
boundary. The candidate history also contains serial save-state, bootstrap
ownership and build-metadata cleanup, MCP teardown, gate-accounting, and
partial-initialization cleanup changes. This is a `PARTIAL` publication
candidate, not a `PRODUCTION-READY` release.

The acceptance update merged in PR #42 adds bounded setup and
matrix deadlines, runtime-provenance checks, fail-closed subprocess result
validation, and network close diagnostics. The native/Cython strict-gate result
below predates the targeted fix and remains layer-scoped. The pre-fix
native/Cython strict real-ROM gate recorded at
`/tmp/poke-harness-native-strict-evidence-a8576b5` declared and executed 19/19
entries in each tier, with trade `16/19` and battle `17/19`; its failure rows
are recorded below. The post-fix serialized source strict-trade production
gate below completed 19/19 declared and executed trade entries and finished
overall `FAIL`, with 18/19 passed and 1/19 failed; its exact runtime, failure,
and evidence hashes are recorded below. The source-runtime local strict battle
matrix passed all 9/9
ordered canonical color pairs at the pre-fix implementation/runtime snapshot;
the row-level snapshot is recorded below. Existing MCP evidence remains narrower:
six real MCP integration checks passed in 13.11s with one SDL warning, and 104
dispatch tests passed. Ordinary real-ROM MCP navigation reached the Red
bedroom, house exit, Pallet Town, Oak's Lab, and lab movement, but the bounded
starter attempt ended at map 40 (5,3) with `party.count=0`. MCP-driven
starter/trade/battle remains unproven. These results do not qualify the release
as production-ready; vanilla fixture provenance, platform/concurrency
qualification, and secure cross-host TCP remain `PENDING` or `PARTIAL`.
A previously recorded source unit/timing gate passed 954/954 unit tests and
50/50 timing cases in each of five repetitions; those are unit/timing results
only, not end-to-end acceptance.

The integrated post-fix candidate's asset-free full suite passed 966 tests,
with 141 explicit BYO-asset skips and one SDL warning. This validates the
package and test surface only; skipped ROM-backed rows remain unqualified.

Evidence remains separated by layer. Historical prior-candidate
static/build/runtime evidence at `3399407aa04f6e5e496df628442597c03e0adcc6`
collected 1,094 tests in each source and native unit gate, with 949 unit passes
in each; timing passed 50/50 in each of five repetitions. Its asset-backed
runtime tiers passed source local 47/47 in 531.3477s and native local 47/47 in
38.2974s, plus source remote 16/16 in 35.6044s and native remote 16/16 in
30.8627s. These prior-candidate runtime results are not current-head strict
end-to-end acceptance.
The formerly pending source strict-trade rerun is now complete as the post-fix
serialized source production gate: 19/19 declared and executed, 18/19 passed,
1/19 failed, overall `FAIL`. It is source-runtime trade evidence only; the
sole failure and evidence hashes are recorded below, and it does not qualify
the release as production-ready.
The pre-fix native/Cython gate has an all-entry execution record but is
incomplete at `16/19` trade and `17/19` battle. Unit/timing results, LinkMenu
milestones, declarations, and partial current rows do not upgrade the release.
The source-runtime local strict battle result recorded below is pre-fix evidence
from the last verified implementation/runtime snapshot and does not upgrade the
other open release conditions.
Timing-altered diagnostics are not acceptance evidence. A historical source
trade-tier supervisor started before PR #35 was published; its child runs
loaded the then-current implementation, but it was not a clean post-merge
all-tier sign-off.

The six canonical fixture entries have verified provenance, while the four
vanilla-derived entries have not established vanilla ordinary capture
provenance and remain `PARTIAL`. The stock ROM/SYM pins and existing fixture
bytes validate against the manifest records, but vanilla ordinary capture
provenance cannot be established: replay against the retained source failed at
the 64-step bound, and the manifest's ordinary producer revision `25e231c` is
historical. No current-head reproducibility claim is made for those fixture
bytes. Remote TCP remains loopback-only, unauthenticated, and unencrypted;
secure cross-host use is not supported.

A previous candidate verification used the dual `--unit-only --repeat-timing
5` gate on 2026-09-03 with separate source and Cython interpreters
(`--python` plus `--cython-python`). It collected 771 tests in each mode. Both
passed unit 626/626 and timing 50/50 in all five repetitions; source reported
`python-source` and Cython reported `cython/native-extension`. The clone had no
ROM, symbol, or save-state assets, so fixture schema and matrix declaration
were checked but ROM gameplay was not run. Serial/link hardening did not add
gameplay acceptance. An earlier environment-specific
585/586 ownership result is superseded for these isolated environments. Fresh
uv-managed source and native environments installed the candidate;
`uv pip check` passed in both,
and the native bootstrap verified both `pyboy` and `pokered-harness` package
owners. The vendored source PyBoy runtime remains the documented release
default; Cython is an optional diagnostic build. The host's bare `python3` still
lacks `ensurepip`,
so that alternate standard-library venv path remains open. These are
historical prior-candidate build/runtime observations, not current-head
release evidence.

Historical prior-candidate verification at commit
`3399407aa04f6e5e496df628442597c03e0adcc6` (2026-09-04) passed 5/5 ROM
hashes, 3/3 symbol hashes, 3/3 fixture hashes, and the 10-entry fixture
manifest. The earlier source 15/16 result is invalid shared-dirty-vendor
environment evidence, not a current-head failure; its
`test_subprocess_pair_reaches_link_menu_over_tcp` failure came from that
environment. These static and asset-validation results do not qualify the
current strict trade, battle, or MCP live-gameplay lanes.

Prior integrated source and Cython transport slices passed remote 15/15 and
the Cython local/session slice passed 47/47. Those are historical scoped
follow-up results, not proof of current full native trade or battle parity.
The pre-fix native/Cython strict gate is recorded but incomplete: it executed
19/19 declared entries in each tier and passed `16/19` trade and `17/19`
battle. The post-fix serialized source strict-trade production gate completed
19/19 declared and executed entries and passed `18/19`, with `1/19` failed;
the pre-fix source local strict-battle result is `9/9` as recorded below. MCP live
gameplay,
vanilla ordinary fixture provenance, real-ROM concurrent load, full
native-platform qualification, reproducible clean-install verification,
independent review, and authenticated/encrypted cross-host TCP remain open, so
the release decision remains `PARTIAL`. Remote TCP is enforced as loopback-only
and is unauthenticated and unencrypted; cross-host use is not supported until a
secure transport is added.

Historical PR #17 evidence collected 698 tests and passed unit 554/554, local
real-ROM 47/47, remote transport/MCP 15/15, strict trade 19/19, strict battle
19/19, and timing 40/40 in each of five repetitions with
`DEFAULT_MATRIX_WORKERS=1`. The PR #19 follow-up collected 699 tests and
passed the ROM-free 555/555 unit and 40/40 × 5 timing slice, focused
transport/MCP 167/167, bounded concurrency probe 8/8 × 5, and real-ROM remote
transport 15/15. These results are historical slices, not a full rerun of the
current merged implementation. Earlier post-PR #23 native failures at the
Cython `PyBoy.tick` ownership seam are superseded by PR #26/#27 and are not
current acceptance evidence.

PR #22 adds bounded MCP stdio unpair cleanup and fresh remote lifecycle
generation tracking. Its release-hygiene workflow passed; the full strict
post-change gameplay matrices remain open.

Symbol hashes and fixture byte/provenance records are recorded below and in
[`release-evidence/fixture-manifest.json`](release-evidence/fixture-manifest.json).
The remaining release decision is `PARTIAL` because the pre-fix native/Cython
strict gate contains strict real-ROM failures, the post-fix serialized source
strict-trade gate is `FAIL` at `18/19`, and vanilla source provenance,
broad-suite coverage, full
native-platform qualification, real-ROM load evidence, independent review, and
authenticated / encrypted cross-host TCP remain incomplete. `ruff check .`
is clean only for the
configured product boundary; the broad legacy tree is not silently
reformatted.

## Runtime

| Component | Pin | Source of truth |
|---|---|---|
| Python | `>=3.12` | `pyproject.toml` |
| PyBoy | `2.7.0` + fork `c565df66c3731fad2856169a90f6bbec99925915` | `vendor/pyboy-src/POKERED_HARNESS_PYBOY_REVISION` and `vendor/pyboy-src/pyboy/__init__.py` |
| MCP | `1.29.1` | `pyproject.toml` and `uv.lock` |

The project distribution bundles the pinned PyBoy source runtime. It exposes
the Python-accessible `mb.serial` backend used by the bit-accurate link
coordinator and remote TCP transport. The optional Cython/native build's
compiled serial-contract probe and focused 110/110 serial-link suite are
historical prior-candidate diagnostic evidence. The pre-fix native/Cython
strict real-ROM gate is recorded at
`/tmp/poke-harness-native-strict-evidence-a8576b5`; it used Python `3.12.13`
with PyBoy `2.7.0` and fork `c565df66c3731fad2856169a90f6bbec99925915`, and
declared and executed 19/19 entries in each tier. Its trade result is `16/19`
and its battle result is `17/19`; the failure details and evidence boundary
are recorded below. The post-fix serialized source strict-trade production gate
is `FAIL` at `18/19` after 19/19 declared and executed entries, and the
pre-fix source-runtime local strict battle result is 9/9. A pre-existing
PyBoy wheel must not be allowed to shadow
this package; verify the runtime identity and selected mode before release. See
the
[production runbook](docs/PRODUCTION_RUNBOOK.md).

## ROM pins

ROMs are BYO inputs and belong under `rom/<version>/`, which is gitignored.
The SHA-1 rows below are the pins currently recorded for the candidate files.
`pokered_harness.config.load_versions()` indexes rows by their documented
`Path`, so a launch can be checked against the selected ROM rather than the
first row in this file. Explicit `POKERED_ROM_SHA1` values remain preferred
for release evidence.

### Pokémon Red (UE)

| Field | Value |
|---|---|
| SHA-1 | `ea9bcae617fdf159b045185467ae58b2e4a48b9a` |
| Size | 1,048,576 bytes |
| Path | `rom/red/pokemon-red.gb` |
| Symbols | `rom/red/pokemon-red.sym` |
| Symbol SHA-1 | `03783c86a42588bd77f73bd7814cf8d70e590118` |
| Role | Hash-pinned single-session input; selected checks only; link gameplay not claimed |

### Pokémon Red color variant

| Field | Value |
|---|---|
| SHA-1 | `e1deed63080bc24cad5fba18ecb3184f905d16d4` |
| Size | 1,048,576 bytes |
| Path | `rom/red/pokemon-red-color.gb` |
| Symbols | `rom/red/pokemon-red.sym` only when verified against this variant |
| Symbol SHA-1 | `03783c86a42588bd77f73bd7814cf8d70e590118` |
| Role | Canonical color-Red input; selected source/Cython transport checks are recorded, but strict trade/battle qualification remains incomplete |

### Pokémon Blue (UE)

| Field | Value |
|---|---|
| SHA-1 | `d7037c83e1ae5b39bde3c30787637ba1d4c48ce2` |
| Size | 1,048,576 bytes |
| Path | `rom/blue/pokemon-blue.gb` |
| Symbols | `rom/blue/pokemon-blue.sym` |
| Symbol SHA-1 | `c779a0628cfc97cc9ac9db2520a1e23a2d8b7ed6` |
| Role | Hash-pinned single-session input; selected checks only; link gameplay not claimed |

### Pokémon Blue color variant

| Field | Value |
|---|---|
| SHA-1 | `5f4b05725a860e04077045462176d3e2771c5022` |
| Size | 1,048,576 bytes |
| Path | `rom/blue/pokemon-blue-color.gb` |
| Symbols | `rom/blue/pokemon-blue.sym` only when verified against this variant |
| Symbol SHA-1 | `c779a0628cfc97cc9ac9db2520a1e23a2d8b7ed6` |
| Role | Canonical color-Blue input; selected source/Cython transport checks are recorded, but strict trade/battle qualification remains incomplete |

### Pokémon Yellow (UE)

| Field | Value |
|---|---|
| SHA-1 | `cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1` |
| Size | 1,048,576 bytes |
| Path | `rom/yellow/pokemon-yellow.gbc` |
| Symbols | `rom/yellow/pokemon-yellow.sym` |
| Symbol SHA-1 | `7c4205723943e7722230dcf014e5e8a2012474aa` |
| Role | Canonical Yellow input; selected source/Cython transport checks and mixed native exact-row diagnostics are recorded, but strict trade/battle qualification remains incomplete |

Other localisations, hacks, and variants are out of scope unless they receive
their own ROM hash, matching symbols, fixture provenance, and acceptance
result. File size alone does not establish compatibility.

## Symbol-file contract

Symbol files are generated inputs, not committed release assets. The audited
files byte-match the generated outputs identified in the provenance table
below. The harness loader checks that the requested labels can be parsed; it
does not establish the source provenance of an arbitrary `.sym` file.

The link symbol registry contains required, optional, and reserved labels in
`src/pokered_harness/link/symbols.py`. A symbol-file audit or real-ROM test must
be run for each version before claiming link support. No blanket cross-version
symbol-coverage claim is made here.

## Verified symbol provenance

The symbol files used for the 2026-08-30 audit were compared byte-for-byte
with the matching generated files in the local source checkouts before their
hashes were recorded above. The source repositories and build inputs were:

| Game symbols | Source commit | Generator/toolchain | Build flags |
|---|---|---|---|
| Red `pokered.sym` and Blue `pokeblue.sym` | `pret/pokered` `fbcf7d0e19a3a2db505440d3ccd3d40ca996c15c` | `rgblink` from RGBDS `v1.0.1` | `make DEBUG=1 red blue`; default `RGBASMFLAGS` plus `-Q8 -P includes.asm -E`, with `_RED`/`_BLUE` targets |
| Yellow `pokeyellow.sym` | `pret/pokeyellow` `bfa7170107eea23b89febb60bfb2ce39173bf2e1` | `rgblink` from RGBDS `v1.0.1` | `make DEBUG=1 yellow`; default `RGBASMFLAGS` plus `-Q8 -P includes.asm -E` |

The source repositories are `https://github.com/pret/pokered` and
`https://github.com/pret/pokeyellow`. The color ROMs use the corresponding
base-game symbols because the color patch preserves the symbol-address ABI;
their ROM hashes remain independently pinned above.

## Hash and version enforcement

`Session.from_files(..., expected_rom_sha1=...)` hashes the ROM and raises
`VersionMismatch` on a mismatch. `expected_symbol_sha1` performs the matching
symbol-file check, and `expected_pyboy_version` plus
`expected_pyboy_revision` verify the bundled PyBoy version and exact nonempty
fork revision when a caller supplies them. The MCP entry point:

1. uses `POKERED_ROM_SHA1` when set;
2. otherwise selects the SHA-1 whose `Path` matches the configured ROM; and
3. enforces the matching symbol SHA-1 and exact PyBoy revision when
   `VERSIONS.md` is available; and
4. fails closed when no matching pin is available, unless
   `POKERED_SKIP_SHA1` is explicitly set for diagnostics.

An explicit hash is still required by release policy, and a release run must
not set `POKERED_SKIP_SHA1=1`.

## Link and fixture boundary

Save states are tied to the exact ROM bytes and symbol layout used to capture
them. A fixture named `cable_club.state` is therefore not interchangeable
between stock, color-variant, and Yellow inputs. Keep fixtures local under
`tests/fixtures/link/<version>/`; do not commit them.

The bounded, pinned ordinary-fixture producer is
[`scripts/produce_cable_club_fixture.py`](scripts/produce_cable_club_fixture.py).
It requires a matching, user-generated `cerulean_pc.state` source and accepts
explicit `--version`, `--variant`, `--source`, `--rom`, `--sym`, and `--out`
arguments. It validates ROM and symbol hashes plus the bundled PyBoy version
and fork revision from this file. Its default wall-clock budget is 180 seconds
and its default movement budget is 64 directional inputs; either budget can be
overridden explicitly. There is no separate Yellow producer. A successful
output only establishes a fixture at the expected map/tile; it does not prove
that a trade or battle works. Replay against the retained vanilla source
failed at the 64-step bound, so this producer path does not establish vanilla
ordinary capture provenance or current-head fixture reproducibility.

The strict local acceptance fixtures and remote acceptance/diagnostic fixtures are
distinct from the default Cable Club fixtures. The acceptance matrix defines
the color Red, color Blue, and Yellow ordinary/battle rows below. The pre-fix
source-runtime local strict battle result and native/Cython strict-gate result
are recorded below against the last verified implementation/runtime snapshot
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7`, not the current public docs head.
The post-fix serialized source strict-trade production gate is recorded below
as overall `FAIL` at `18/19` after 19/19 declared and executed entries; pre-fix
native/Cython strict trade and battle remain incomplete at `16/19` and `17/19`,
respectively. MCP
live-gameplay qualification remains `PENDING` or `PARTIAL`; historical rows
outside the results below are not current implementation/runtime evidence.

| Path | ROMs | Required fixture files | Evidence boundary |
|---|---|---|---|
| Local strict trade matrix | color Red, color Blue, or Yellow in every ordered pair | matching `cable_club.state` for each side | Historical source gate: 9/9 local rows. The post-fix serialized source gate collected all 9/9 local-version-pair rows; its overall trade result was `18/19` after 19/19 declared and executed entries, with the sole failure recorded below. The pre-fix native/Cython strict-gate trade result is `16/19` across its 19/19 declared and executed entries in each tier |
| Local strict battle matrix | color Red, color Blue, or Yellow in every ordered pair | matching `cable_club-battle.state` for each side | Last verified pre-fix implementation/runtime snapshot source result: 9/9 ordered canonical color pairs passed and reached Link Battle with move/damage hooks. The pre-fix native/Cython strict-gate battle result is `17/19`; details below |
| Remote strict trade matrix | canonical color Red, color Blue, or Yellow listener/connector in every ordered pair | matching ordinary fixture for each side | Historical source gate: 9/9 remote rows. The post-fix serialized source gate passed `18/19` overall after 19/19 declared and executed entries; its sole failure is recorded below. The pre-fix native/Cython strict gate executed all 19 declared entries in each tier and passed 16/19 |
| Remote strict battle matrix | canonical color Red, color Blue, or Yellow listener/connector in every ordered pair | matching battle fixture for each side | Historical source baseline passed 9/9 remote rows; a historical native direct lane exercised 3/9 under a bounded 155-second pair deadline (one pass, two fail, six unrun), with no test-only bypasses. The pre-fix native/Cython strict gate executed all 19 declared entries in each tier and passed 17/19; details below |

### Post-fix serialized source strict-trade production gate

The post-fix serialized source strict-trade production gate at
`/tmp/poke-harness-source-serial-acceptance-20260904/evidence` used source
Python `3.12.13`, PyBoy `2.7.0` with fork
`c565df66c3731fad2856169a90f6bbec99925915`, bit-accurate serial, and
`matrix-workers=1`. This evidence is after code fix `7b4b5b72` (cherry-picked
as `7b4b5b7`). Its complete matrix audit collected `1107`; the trade tier
declared and executed `19/19` entries and finished overall `FAIL`, with
`18/19` passed, `1/19` failed, `0` skipped, `0` errors, `0` xfail, and `0`
xpass in `2788.895s`.

The sole failure was
`tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_completes_trade_over_tcp[blue_color-listen-blue_color-connect]`,
which took `721.4846s`; the trade-center warp rendezvous did not converge.
Pre-close statistics had `984` inbound/applied edges, `123` IRQ callbacks,
zero owner-edge errors, and no pending edge requests. This is post-fix source
strict-trade evidence for the serialized runtime, but it is a failing gate and
does not establish production readiness.

The evidence bundle's SHA-256 hashes are:

| Artifact | SHA-256 |
|---|---|
| `gate-report.json` | `7de82390f52bdd4b8e3d569204ef8f3951113a8c1706f403e53a6f681e0db453` |
| `gate-report.txt` | `90e5b17706ba87a095a7fa32c317d84d957e2c76550a7d06f2a38720ab451a97` |
| `evidence-manifest.json` | `c6f07eb29dbd272ead265b628bd2d2c41e287ec02ab1229db90716ff95dde45a` |

### Pre-fix native/Cython strict real-ROM gate

The pre-fix gate artifact at
`/tmp/poke-harness-native-strict-evidence-a8576b5` records
Python `3.12.13`, PyBoy `2.7.0`, and fork
`c565df66c3731fad2856169a90f6bbec99925915`. It declared and executed all
19/19 entry points in each tier. No native owner/IRQ errors occurred. Every
failure below is a strict real-ROM failure, not a test-only bypass.

| Matrix | Result | Strict real-ROM failures |
|---|---:|---|
| Trade | `16/19` | `blue_color` listener → `yellow` connector at 83.39s after trade hooks, with an incorrect/malformed party record; `red_color` listener → `yellow` connector at 729.51s, LinkMenu rendezvous timeout after 2,197 balanced/applied edges and one pending request; `yellow` listener → `blue_color` connector at 721.39s, LinkMenu rendezvous timeout after 6,248 balanced edges |
| Battle | `17/19` | `blue_color` listener → `yellow` connector at 376.94s, sync marker 113 did not converge after 18,840 applied edges; `yellow` listener → `blue_color` connector at 142.20s, battle Colosseum warp rendezvous did not converge after 952 balanced edges |

### Last verified source-runtime local strict battle snapshot

At last verified implementation/runtime snapshot
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7` (before current public docs head
`f048870bdbd4837b5494006ae49fe40510832a89`), the source-runtime strict local
battle matrix completed 9/9 ordered canonical color pairs. The first six rows
below are existing same-campaign records; the final three are fresh rows.
Every row reached Link Battle and recorded the move and damage hooks used by
the assertion.

| Pair | Result | Duration |
|---|---|---:|
| `yellow-yellow` | `PASS` | 125.397s |
| `blue-blue` | `PASS` | 303.395s |
| `red-red` | `PASS` | 231.966s |
| `red-blue` | `PASS` | 612.110s |
| `blue-red` | `PASS` | 629.876s |
| `red-yellow` | `PASS` | 179.162s |
| `yellow-red` | `PASS` | 182.89s |
| `blue-yellow` | `PASS` | 443.50s |
| `yellow-blue` | `PASS` | 480.31s |

This is source-local battle evidence only. It does not turn the post-fix
serialized source strict-trade gate above into a passing gate, or close the
incomplete native/Cython strict gate, MCP-driven gameplay, vanilla fixture
provenance, platform/concurrency qualification, or secure cross-host TCP.

The tracked
[`scripts/prepare_battle_cable_club_fixtures.py`](scripts/prepare_battle_cable_club_fixtures.py)
creates a separate derived battle state from an ordinary state. It validates
the selected ROM, creates a legal multi-mon party, and the acceptance runner
loads the resulting immutable bytes without preparing or mutating party state
at runtime. This is deterministic fixture preparation, not proof of a human
captured battle state.

The tracked
[`release-evidence/fixture-manifest.json`](release-evidence/fixture-manifest.json)
is the source of truth for fixture sizes, SHA-1/SHA-256 values, expected ROM
and symbol pins, source-state hashes, runtime identity, and capture command
templates. The states remain operator-managed, ignored, and outside the
repository. Its current entries are:

| Fixture | Observed SHA-1 | Provenance boundary |
|---|---|---|
| `red/cable_club.state` | `546d7edaf7c3a987f86ae86a97066c7d619eefbb` | `verified`, canonical color-Red ordinary reproduction |
| `red/cable_club-vanilla.state` | `affd77c20bf4600b8057ea33683ed58a2ac86157` | `partial`, retained source is not proven vanilla-ROM captured |
| `red/cable_club-battle.state` | `4343278b018187ed4bf7c4eda3b202097456f048` | `verified`, derived from canonical color-Red ordinary state |
| `red/cable_club-battle-vanilla.state` | `63d00b469b07e3969bcd82be1047031265be0b3a` | `partial`, derived from partial vanilla ordinary provenance |
| `blue/cable_club.state` | `0809d2f8e514c7fb714a73a7b13f120b26a40c38` | `verified`, canonical color-Blue ordinary reproduction |
| `blue/cable_club-vanilla.state` | `cff5349b55f8fdf47a5af63d674977103aeb44d6` | `partial`, retained source is not proven vanilla-ROM captured |
| `blue/cable_club-battle.state` | `6aac3aabe0f6ad662dec218682c2254b3954a10a` | `verified`, derived from canonical color-Blue ordinary state |
| `blue/cable_club-battle-vanilla.state` | `442c1497c0398f7e54ceade9739ddd1b5b9456fc` | `partial`, derived from partial vanilla ordinary provenance |
| `yellow/cable_club.state` | `37df4dbdb512cd3febdc2d536281683476291d3b` | `verified`, canonical Yellow ordinary reproduction |
| `yellow/cable_club-battle.state` | `78c7d0b32006b11baa9ac9c71efc73b0a8d807b9` | `verified`, derived from canonical Yellow ordinary state |

The six canonical color Red, color Blue, and Yellow fixture entries have
verified provenance, while the four vanilla-derived entries have not
established vanilla ordinary capture provenance and remain `PARTIAL`.
The stock ROM/SYM pins and existing fixture bytes validate against the manifest
records, but that validation does not establish vanilla ordinary capture
provenance. Replay against the retained source failed at the 64-step bound,
and the manifest's ordinary producer revision `25e231c` is historical. No
current-head reproducibility claim is made for those fixture bytes, and the
vanilla rows are not supported release rows. Manifest byte validation still
requires every listed entry when the manifest is checked with `--fixture-root`.

The repository has strict local and remote entry points for every canonical
Red/Blue/Yellow ordered pair. A historical prior-candidate source trade gate
recorded 19/19 rows (9 local and 9 remote, plus the dedicated assertion). The
native strict-trade matrix has a historical 18/19 result, but exact-row
follow-ups both passed and failed, including a party-record mismatch and phase
stalls, so that historical result's reliability is unproven. The pre-fix
native/Cython strict gate is recorded above at 16/19 trade and 17/19 battle
after executing 19/19 declared entries in each tier; its failures are strict
real-ROM failures. The post-fix serialized source strict-trade production gate
is recorded above at `18/19` after 19/19 declared and executed entries, with
overall `FAIL`, while the last verified pre-fix source-runtime local battle
matrix passed 9/9 as recorded above. The PR #17 source-runtime 19/19 trade and
19/19 battle
results remain historical baseline evidence. Consult the test-surface table in
the [README](README.md) and run the required tiers in the [production runbook](docs/PRODUCTION_RUNBOOK.md)
before using any row as release evidence.

## Performance

No machine-specific throughput floor is pinned here. Performance evidence must
record the exact commit, Python/PyBoy build, host, render mode, workload, warmup
policy, and sample distribution. A single local timing is not a release gate.
