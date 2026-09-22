# Ordinary battle medicine: Yellow in both runtimes - evidence bundle

Status: **TERMINAL**

This directory is the sanitized record of the one real-ROM acceptance cell the
medicine workstream already owns - the ordinary battle item-menu Potion
application for Yellow - executed in **both** declared runtimes. Issue #90's
acceptance criterion asks for each admitted item case to pass ordinary real-ROM
execution for Red-color, Blue-color and Yellow in both source and native; this
bundle registers the Yellow half of that matrix. It is not the matrix.

It contains no ROM bytes, no symbol tables, no save states, no traces and no
credentials: only digests, sizes, labels, per-tier outcomes, the runtime
identities that were measured and one counted redaction marker per log. Every
local absolute path was replaced by a bracket placeholder before the files were
written (see `Placeholders` below), and the symbol-table input that PyBoy echoes
while loading is elided (see `Redactions` below).

## Identity

| field | value |
|---|---|
| tested head | `2d876763cdcc42e824a883c3c8d93646ceefbcd3` |
| tested tree | `0445ad29d7f6330debfc13413798c04d0ed13997` |
| acceptance node id | `tests/test_battle_healing_items_rom.py::test_potion_heals_the_active_mon_from_the_battle_item_menu` |
| modules executed | `tests/test_state_bag.py`, `tests/test_state_party.py`, `tests/test_battle_healing_items_rom.py` |
| issue / leaf | #90 / 90.3 (runtime half for Yellow) |

`2d876763` is the merge commit of PR #168, which was the tip of the default
branch when the runs were made, and `0445ad29` is that commit's tree. The
worktree was clean at both runs: the executed bytes are the committed bytes, so
the rows describe the commit rather than a local edit of it. Nothing in this
bundle is a claim about any later commit.

## Runtime provenance (measured, not asserted)

The dual-runtime claim is only meaningful if each tier really is the runtime it
declares, so each tier was probed directly with the same interpreter, the same
`PYTHONPATH` and the same `PYBOY_NO_CYTHON` setting as its pytest run. The probe
imports the package and every one of its submodules and reports the module files
the interpreter actually resolved.

| tier | python | pyboy | fork revision | imported compiled ext | imported from vendored tree | declared runtime |
|---|---|---|---|---|---|---|
| `source` | 3.11.2 | 2.7.0 | `c565df66c373` | 0 | yes | yes |
| `cython` | 3.11.2 | 2.7.0 | `c565df66c373` | 58 | no | yes |

Vendored revision marker: `c565df66c3731fad2856169a90f6bbec99925915`.

- both tiers are their declared runtime: **True**
- both tiers share the pinned fork revision: **True**
- the source tier imported 60 `pyboy` modules and no compiled extension, all of
  them from `vendor/pyboy-src`;
- the cython tier imported 61 `pyboy` modules, 58 of them compiled extensions,
  none of them from `vendor/pyboy-src` - so this is the installed extension
  build, not a source fallback.

The raw probe output, including the import-kind counts and the module list
lengths, is retained under `runtime-identity.json` -> `tiers`. `pyboy_file` and
`python_executable` are recorded there as placeholders rather than as machine
paths.

## Rows

`rows_expected = 2` (the declared module set x {`source`, `cython`}).

| row | log summary | tests | failures | errors | skipped | wall |
|---|---|---|---|---|---|---|
| `source` | `25 passed, 1 warning in 15.65s` | 25 | 0 | 0 | 0 | 15.66s |
| `cython` | `25 passed, 1 warning in 1.24s` | 25 | 0 | 0 | 0 | 1.24s |

- pass: 2
- not_pass: 0
- pass by runtime: `{"cython": 1, "source": 1}`

A row counts as a pass only when its JUnit XML exists with `failures=0`,
`errors=0`, `skipped=0`, and the declared acceptance node id is present in it as
a passing test case. Both conditions hold for both rows. No row was retried, so
there is no earlier attempt to disclose; the two tiers are the two rows.

The one warning is the `pysdl2-dll` SDL2 binary notice emitted by the SDL2
binding on import. It is not a game or harness warning and it is retained rather
than filtered, because filtering output is not evidence.

The same 25 tests ran in both tiers, which is a property worth stating
explicitly: the ROM-free members of this module are mixed into a real-ROM
module, and the tier registry classifies three of its functions as ROM-free
(`tests/_tier_config.py` -> `ROM_FREE_TESTS`). The rows here are the module as
declared by the issue's reproduce block, not a narrowed selection.

## Why the command omits the documented `-q`

The issue's reproduce block passes `-q` to pytest. This repository also sets
`addopts = "-q"` in `pyproject.toml`, so passing `-q` produces `-qq`, and at that
verbosity pytest stops printing the terminal count line. A pass whose count is
not in the log is weaker evidence than one whose count is, so the runs recorded
here omit the extra `-q` and the counts appear both in the logs and in the JUnit
`tests` attribute. Nothing else in the command was changed.

## Retained artifacts

- `junit/source-focused.xml`, `junit/cython-focused.xml` - each tier's own JUnit
  XML. Both are clean `<testcase>` elements with repo-relative node ids.
  pytest writes a container `hostname` attribute into this element; the existing
  merged evidence bundles in this repository keep it, and it is not a path, so it
  is kept here unchanged too.
- `logs/source-focused.log`, `logs/cython-focused.log` - each tier's captured
  stdout/stderr, including the `-rA` PASSED list and the captured emulator
  output. The logs go through the same placeholder rewrite as everything else,
  and the private symbol-table payloads in PyBoy's loader warnings are elided
  (see `Redactions` below).
- `runtime-identity.json` - the measured provenance above, the guardrail
  declaration, the executed node ids and the operator-asset digests.
- `results.txt` - the two rows in the flat form used by the other bundles in this
  directory.

## Placeholders

| placeholder | what it stood for |
|---|---|
| `[worktree]` | the tested worktree |
| `[source-venv]` | the source-tier interpreter's environment |
| `[native-venv]` | the compiled-tier interpreter's environment |
| `[operator-root]` | the operator-supplied private asset root |
| `[workspace]`, `[home]` | the local workspace and home directories |
| `[usr]`, `[tmp]`, `[var]`, `[etc]` | the corresponding system directories |

The rewrite is applied longest-prefix-first and is asserted to round-trip byte
for byte before any file is written; the bundle builder aborts rather than emit a
record it cannot prove it left otherwise untouched. A bundle-wide sweep then
re-scans every emitted file for absolute-path fragments and fails if any survive.
`tests/test_battle_healing_items_rom.py::test_runtime_registration_bundle_is_sanitized_and_consistent`
re-runs that check, so the committed bytes cannot carry a machine path unnoticed.
The guard additionally pins the file set itself, so a stray asset cannot be added
to the directory unnoticed.

## Redactions

Loading the symbol table makes PyBoy emit one `Skipping .sym line` warning per
line it cannot use, and pytest's `-rA` capture writes the warning text into the
log. That text is a byte-exact copy of the operator's private symbol-table input,
so it must not be committed. Both logs therefore carry a single counted marker
where that contiguous warning run was:

```text
pyboy.pyboy                    WARNING  Skipping .sym line: <redacted: 953 private symbol payloads; see README "Redactions">
```

The marker keeps the number of elided payloads (953 per tier; lines 11-963 of the
unredacted log) and nothing else about them. `runtime-identity.json` records the
redaction under `redactions`: per tier the elided count, the line range, the
SHA-256 and size of the private unredacted original, and the SHA-256 and size of
the committed redacted file. The unredacted originals are retained outside the
repository under the `private_original_label` named there, so the elided payloads
remain auditable without being distributed. The guard hashes each committed log
against its recorded redacted digest, requires the marker to appear exactly once
with the recorded count, and fails if any unredacted `Skipping .sym line` payload
is present. Redaction touched only those warning lines: every other line of each
log is byte-identical to the original, which the sanitizer asserts before it
writes.

## Operator assets (digests only)

| label | kind | size | sha1 |
|---|---|---|---|
| `rom/yellow/pokemon-yellow.gbc` | game ROM | 1048576 | `cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1` |
| `rom/yellow/pokemon-yellow.sym` | symbol table | 842120 | `7c4205723943e7722230dcf014e5e8a2012474aa` |
| `tests/fixtures/link/yellow/battle_healing.state` | driven fixture | 200548 | `348ba9aec5a8f4f72c59efc547a09a5c2ff84ef4` |

The ROM and symbol digests are the pins `VERSIONS.md` declares; the fixture
digest is the one `release-evidence/battle-healing-fixtures.json` declares. The
test module verifies all three against those declarations before it loads
anything, so a bundle entry cannot quietly disagree with the harness pins.

## Reproducing

These runs need operator-supplied ROMs, symbol tables and the driven fixture,
none of which can be distributed. With the operator roots exported, each tier is
one command from a clean checkout at the tested head:

```sh
# the operator roots hold rom/{red,blue,yellow}/... and tests/fixtures/link/...
# the fixture is the one scripts/produce_battle_healing_fixture.py drives

# source runtime (the gate's source switch is PYBOY_NO_CYTHON=1):
env -u POKERED_SKIP_SHA1 -u PYTEST_ADDOPTS \
  PYBOY_NO_CYTHON=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTHONPATH="[worktree]/vendor/pyboy-src:[worktree]/src:[worktree]" \
  POKERED_ROM_ROOT=[operator-root]/rom \
  POKERED_FIXTURE_ROOT=[operator-root]/tests/fixtures/link \
  [source-venv]/bin/python -m pytest -rA -p pytest_asyncio.plugin \
  --strict-config --strict-markers --junitxml=source-focused.xml \
  tests/test_state_bag.py tests/test_state_party.py tests/test_battle_healing_items_rom.py

# cython/native runtime (leave PYBOY_NO_CYTHON unset):
env -u PYBOY_NO_CYTHON -u POKERED_SKIP_SHA1 -u PYTEST_ADDOPTS \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTHONPATH="[worktree]/src:[worktree]" \
  POKERED_ROM_ROOT=[operator-root]/rom \
  POKERED_FIXTURE_ROOT=[operator-root]/tests/fixtures/link \
  [native-venv]/bin/python -m pytest -rA -p pytest_asyncio.plugin \
  --strict-config --strict-markers --junitxml=cython-focused.xml \
  tests/test_state_bag.py tests/test_state_party.py tests/test_battle_healing_items_rom.py
```

The 25-test run is seconds in the compiled tier and about 16 seconds in the
source tier; the emulator work is the single acceptance cell, which takes 1.31s
compiled and 15.5s from source.

## What this bundle does not claim

- It does **not** complete leaf 90.3. Red-color and Blue-color are not executed
  at all: no Red or Blue medicine fixture exists yet, so three of the six
  game x runtime combinations of the parent criterion are still open.
- It is **not** dual-runtime evidence for any other module, and it says nothing
  about 90.4 (effect/isolation assertions) or 90.5 (negative and continuation
  cases), which remain unstarted.
- It does **not** mark #90 or any leaf complete, and the parent stays
  `status:qualification-required`.
- It is **not** a release decision. The candidate-level qualification for #72 is
  unaffected.

Guardrails honoured: `POKERED_SKIP_SHA1` was never set, `PYTEST_ADDOPTS` was
unset, no assertion, bound, tolerance, skip or xfail was changed, no runtime
RAM/RNG/party/PP edit was made, and no game input was fabricated.
