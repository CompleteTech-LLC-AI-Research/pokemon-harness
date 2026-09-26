# Combined #153 and #133 harness-local divergence

Current tracked vendor content identity: `acd569fdb5836b58558856ac066102d1e9ccaf96`. This includes the serial
type split and main PyBoy source components, calculated from the complete tracked vendor
manifest. Earlier single-change identity notes below are historical and do not qualify
this combined tree. The native build, full repository gate and exact-head review remain
pending.

---

# Unqualified serial-split candidate for #122 / #153

Candidate content identity: `0a0f7f315e58e1c06a8c2cb9a716b8116fdf92ef`. Prior identity: `eceaa3bb15dedd6847a3a37d3400421e3024cb5c`.
Base harness commit: `3e865d5b0348d0c7c462ed8b40a107ae2b158277`. This is a content digest, not a Git commit.

`pyboy/core/serial.py` is split from 1117 to 996 lines; the new
`serial_types.py` is 168 lines. Eight class definitions move verbatim.
The complete Serial implementation and serial.pxd remain unchanged.
Historical facade class/pickle paths are preserved. The existing digest
algorithm is unchanged and includes the added helper in the tracked manifest.

Status: PARTIAL / UNQUALIFIED. Isolated serial checks are not a complete
PyBoy build, the pinned-toolchain gate, packaging acceptance, full pytest,
production-gate equivalence, or independent review. No historical passing
result below is transferred to this new identity. Other #122 leaves remain open.

## Previous two-split identity record — historical, retained verbatim

# Harness-local divergence record — vendored PyBoy

Status: **RE-PINNED — harness-local divergence revision.** The vendored tree has diverged from the
**harness fork revision** `c565df66c3731fad2856169a90f6bbec99925915` it was vendored from: of the
115 paths it shares with that revision, 98 are byte-identical and the rest carry harness patches
(measured figures and how to reproduce them are under "Provenance of the pre-divergence revision").
The revision pins now carry the harness-local divergence revision
`eceaa3bb15dedd6847a3a37d3400421e3024cb5c`, and the
fork revision is recorded below rather than reused as the pin. The pin is a content identity, not
a git object; its exact definition and recomputation are given under "Harness-local divergence
revision".

Authority: `docs/VENDORED_PYBOY_SPLIT_DECISION.md` (option B, explicit in-fork divergence) and
the `#122` file-split program (`#138`).

## Provenance of the pre-divergence revision

- Upstream project: `Baekalfen/PyBoy`. Upstream tag `v2.7.0` =
  `4627b90b878e91faff443b3acd6d4e4be09a4387` (2026-01-24).
- Pre-divergence revision: `c565df66c3731fad2856169a90f6bbec99925915` — a commit of the harness's own
  PyBoy fork, `CompleteDotTech/pyboy-link-cable-fork` ("Serial: fix 4 init/API edge cases, pass full
  9-trade matrix", 2026-04-23). In that repository it is **32 commits ahead of upstream `v2.7.0`**
  (`compare` reports `status: ahead`, `ahead_by: 32`, `merge_base_commit: 4627b90b…`).
- It is **not** an upstream `Baekalfen/PyBoy` commit: the upstream API returns *422 — No commit found
  for SHA* for it. Nothing here should be read as an upstream revision of PyBoy.
- The vendored tree is **not** that revision's tree either: of the 119 git-tracked files under
  `vendor/pyboy-src/`, **115 paths are common** with that revision and **98 are byte-identical**
  (blob SHA-1). The four paths with no counterpart there are exactly the two split outputs and the
  two identity carriers — `pyboy/plugins/game_wrapper_pokemon_pinball_data.py`,
  `pyboy/core/opcodes_gen_handlers.py`, `POKERED_HARNESS_PYBOY_REVISION` and this record.
- So the vendored tree descends from a **fork line** that contains `c565df66…`; no byte-identity with
  any single fork or upstream commit is claimed, and none should be. This revision is the recorded
  provenance, **not** the current pin. The current pin is the harness-local divergence revision below.

Reproduce the provenance measurements above:

```bash
gh api repos/Baekalfen/PyBoy/commits/c565df66c3731fad2856169a90f6bbec99925915   # 422: not upstream
gh api repos/Baekalfen/PyBoy/commits/v2.7.0 --jq .sha                          # 4627b90b…
gh api repos/CompleteDotTech/pyboy-link-cable-fork/commits/c565df66c3731fad2856169a90f6bbec99925915 \
  --jq '.commit.committer.date + " " + (.commit.message | split("\n")[0])'     # 2026-04-23 …
gh api repos/CompleteDotTech/pyboy-link-cable-fork/compare/4627b90b878e91faff443b3acd6d4e4be09a4387...c565df66c3731fad2856169a90f6bbec99925915 \
  --jq '{status, ahead_by, merge_base_commit: .merge_base_commit.sha}'         # ahead 32 / 4627b90b…
```

The path counts are blob-SHA comparisons between `git ls-tree -r <vendored-head> -- vendor/pyboy-src`
and the `git/trees/<sha>?recursive=1` listing of `c565df66…` in the fork; they move only when the
vendored tree moves, so re-measure them per head rather than copying the numbers forward.

## Harness-local divergence revision

- Value: `eceaa3bb15dedd6847a3a37d3400421e3024cb5c`, carried by `POKERED_HARNESS_PYBOY_REVISION`,
  `pyboy/__init__.py::__pokered_harness_revision__`, `scripts/bootstrap_pyboy.py::EXPECTED_REVISION`
  and `tests/_runtime_packaging_support.py::EXPECTED_PYBOY_REVISION`, plus the CI assertion in
  `.github/workflows/release-hygiene.yml` / `scripts/run_local_ci.sh`.
- Definition (deterministic, verifiable): for every git-tracked file under `vendor/pyboy-src/`,
  in ascending path order, feed `path + "\0" + sha1(file bytes).hexdigest() + "\n"` into a SHA-1.
  Two files are handled specially because they embed the pin: `POKERED_HARNESS_PYBOY_REVISION` and
  this divergence record are excluded, and in `pyboy/__init__.py` every 40-hex run is replaced with
  `@POKERED_REV@` before hashing. No other file is altered, so the identity covers all vendored
  source, resources and configuration.
- Recompute:

  ```python
  import hashlib, re, subprocess
  files = sorted(subprocess.check_output(["git", "ls-files", "vendor/pyboy-src"], text=True).split())
  skip = {"vendor/pyboy-src/POKERED_HARNESS_PYBOY_REVISION",
          "vendor/pyboy-src/POKERED_HARNESS_PYBOY_DIVERGENCE.md"}
  pat = re.compile(rb"[0-9a-f]{40}")
  h = hashlib.sha1()
  for rel in files:
      if rel in skip:
          continue
      body = open(rel, "rb").read()
      if rel == "vendor/pyboy-src/pyboy/__init__.py":
          body = pat.sub(b"@POKERED_REV@", body)
      h.update(rel.encode() + b"\0" + hashlib.sha1(body).hexdigest().encode() + b"\n")
  print(h.hexdigest())
  ```

  It prints `eceaa3bb15dedd6847a3a37d3400421e3024cb5c` for this head. The digest deliberately
  excludes the marker file and this record, so a later in-fork split changes the revision and
  requires the pins above to be refreshed together (decision-doc condition 2).

## Divergence 1 — `#138` split of the Pokemon Pinball plugin

`pyboy/plugins/game_wrapper_pokemon_pinball.py` was 1552 lines, above the repository's
1000-line file-split bound. It is now split into:

| File | Lines | Contents |
|---|---|---|
| `pyboy/plugins/game_wrapper_pokemon_pinball.py` | 791 | facade: `__pdoc__`, imports, `Stage`, `SpecialMode`, `BallType`, `BallSize`, the stage lists, and the `cdef class GameWrapperPokemonPinball` |
| `pyboy/plugins/game_wrapper_pokemon_pinball_data.py` | 875 | relocated `Pokemon` and `Maps` enums, plus the RAM-address / bank-offset constants and the four `*StageMapWildMons*` tables |

The facade re-exports every relocated name with
`from .game_wrapper_pokemon_pinball_data import *`, so the module's public surface is unchanged.

The star import is **guarded by an explicit 92-name `__all__`** in the data module, and that is
load-bearing rather than tidiness: a bare star import also re-exports `Enum`, and inside the
Cython-generated translation unit for the facade that shadows the C type `Enum`, aborting
`import pyboy` with `TypeError: Cannot overwrite C type Enum`. Source mode does not reproduce it —
only the native build does (see "Verification" below).

### Verification performed

Unless noted otherwise these are source-level results, re-measured on the re-landed head; the
native item below was measured at the pilot pin and does **not** transfer to this head.

- **Native ABI (condition 3), demonstrated — at the pilot pin `d78fb725…`, not this head.** Both
  modules were Cythonized and compiled
  (`gcc 12.2.0`, userland-extracted CPython 3.11 headers, `-O3 -DCYTHON_WITHOUT_ASSERTIONS`),
  link rc=0. Loading the resulting tree: `pyboy`, the facade, the data module and
  `pyboy.plugins.manager` all resolve to `.cpython-311-x86_64-linux-gnu.so`. `Pokemon`/`Maps` are
  the same objects in both modules, `Enum` is not shadowed in the facade, and the compiled
  `manager.so` still carries `PluginManager.game_wrapper_pokemon_pinball` (the `cdef public`
  attribute from `manager.pxd`). This tree is a full earlier native build with only these two
  modules recompiled from the split sources; the rest of the runtime was not rebuilt.
- **AST parity.** 48 pre-existing top-level/nested defs+classes present exactly once, 0 altered, 0
  extra. (Plus the new `__all__` assignment, which the base had no counterpart for.)
- **Collection parity.** `pytest tests --collect-only -q` byte-identical to base `3fdd0c9`.
- **Tests.** ROM-free unit lane 63 passed; runtime-packaging contract/deps/hygiene 19 passed
  (82 passed together).
- **Lint.** `ruff check --no-cache` clean on both files.

### Why the class itself could not move

The decision doc expected this leaf to be a "Python-only plugin surface [with] no Cython `.pxd`
sibling". **That expectation is wrong as written.** `game_wrapper_pokemon_pinball.pxd` exists and
declares

```
cdef class GameWrapperPokemonPinball(PyBoyGameWrapper):
    cdef readonly int ball_saver_seconds_left
    ...
    cpdef int start_game(self, timer_div=*, stage=*) except -1
```

and `pyboy/plugins/manager.pxd` does `from pyboy.plugins.game_wrapper_pokemon_pinball cimport
GameWrapperPokemonPinball`, holding it as `cdef public GameWrapperPokemonPinball
game_wrapper_pokemon_pinball`. The class is therefore a **Cython extension type consumed at
compile time by another compiled module**. A `cdef class` cannot be spread across translation
units, and replacing its methods with delegators would change the extension-type surface that
`.pxd` consumers compile against. So the split peels off only the module-level data; the class
stays intact in its original module.

### Observable consequences

- `Pokemon.__module__` and `Maps.__module__` are now
  `pyboy.plugins.game_wrapper_pokemon_pinball_data`. The classes are the same objects the facade
  re-exports, so `import pyboy.plugins.game_wrapper_pokemon_pinball as m; m.Pokemon` still works
  and old pickles that name the original module path still resolve.
- Classification of the change: a **dependency-identity change**, not a mechanical split, so it
  requires the independent review of decision-doc condition 5.

## Divergence 2 — `#142` split of the opcodes generator

`pyboy/core/opcodes_gen.py` was 1457 lines, above the repository's 1000-line file-split bound.
It is now split into:

| File | Lines | Contents |
|---|---|---|
| `pyboy/core/opcodes_gen.py` | 565 | facade: `destination`/`pxd_destination`, `warning`, `imports`, `cimports`, `inline_signed_int8`, the `opcodes` list, `MyHTMLParser`, `Operand`, `Literal`, `Code`, `OpcodeData` (engine: `__init__`, `createfunction`, the four `handleflags*` methods), `update`, `load` |
| `pyboy/core/opcodes_gen_handlers.py` | 923 | `OpcodeDataHandlers`: the 48 opcode handler methods (`NOP` … `SET`) |

Design and constraints, all verified rather than assumed:

- **Verbatim move, no re-authoring.** The 48 handler methods were extracted byte-for-byte from
  lines 470–1372 of the pre-split generator; no handler body was re-formatted or rewritten. The
  facade keeps the four module-level helpers the handlers reference (`Code`, `Operand`, `Literal`,
  `inline_signed_int8`) and the handlers module imports exactly those four names, so every handler
  body resolves the same objects it resolved before the split.
- **Why a mixin rather than an inversion.** `OpcodeData.__init__` builds `self.functionhandlers`
  from all 48 handler names and `createfunction` invokes them, so the engine depends on the
  handlers and the handlers depend on the engine's `handleflags*` methods. A mixin inheritance
  keeps the existing single `self`/single class namespace: `class OpcodeData(OpcodeDataHandlers)`.
  No handler was changed into a delegator, and no second class was introduced to shadow the first.
- **Import modes both supported.** The generator is used as a package module
  (`pyboy.core.opcodes_gen`) and as a standalone script (`python opcodes_gen.py` from `core/`).
  Both the facade and the handlers module try the package-relative import first and fall back to
  the sibling-module import, so script execution and package import resolve the same two objects.
  Under `python opcodes_gen.py` the facade registers itself in `sys.modules` as `opcodes_gen`
  before importing the handlers module, so exactly one facade module instance exists (verified:
  no duplicated module identity, `OpcodeData.__mro__[1] is OpcodeDataHandlers` in both modes).
- **`cimports` and the two `Code` templates stay literal-eval-able in `opcodes_gen.py`.**
  `tests/test_cpu_instruction_counter.py` parses this file with `ast` (it must not import it,
  because importing fetches external opcode data) and requires the `cimports` assignment plus
  exactly two `cdef uint8_t %s_…` string constants. All three still live in the facade, so the
  test's contract is unchanged.
- **Generation output unchanged (condition 1).** Running the split generator reproduced
  byte-identical output to the tracked files:
  `opcodes.py` sha256 `2538898d44bc74deb448b995cc4888b94296fce58ab324f59f8d18ef75b06dd6`,
  `opcodes.pxd` sha256 `b1c0c5ad1298789c13a3eec1aca9cd3d1bc701a1209f96803f03898bfea07cf5`
  (both equal to the pre-split files). This leaf does **not** split the generated pair (#123/#161);
  the generated files are untouched.
- **Build surface unchanged in kind.** `vendor/pyboy-src/setup.py` already excludes
  `opcodes_gen.py` from Cythonization because it is a generator, not runtime source; the new
  handlers module is excluded alongside it (`ignore_py_files`) so the split stays exactly as
  outside the compiled package as the pre-split file was. The generated `opcodes.py`/`opcodes.pxd`
  remain the only build inputs.

### Observable consequences

- `pyboy.core.opcodes_gen.OpcodeData.__mro__` now includes `OpcodeDataHandlers`. That is a change
  to the generator's internal class hierarchy only: the generator is excluded from the built
  package and nothing in the harness or the built runtime imports it.
- One generated-output-neutral refactor of file layout; no generated file, runtime module, public
  entrypoint, MCP name, or CLI flag changes.

## Open items (not done here)

Status of the two divergences recorded above:

1. **Honest re-pin (condition 2).** Executed. The identity is a content identity over the tracked
   manifest, so the tree carrying both Divergence 1 and Divergence 2 hashes to
   `eceaa3bb15dedd6847a3a37d3400421e3024cb5c`; the pin is no longer the pre-divergence fork revision
   `c565df66c3731fad2856169a90f6bbec99925915` (see "Provenance of the pre-divergence revision"
   above: it is a harness-fork revision, not an upstream commit). Both splits were re-landed **together** on the
   post-#234 `master` after the historical-pin guard fix, so the pin advanced `c565df66…` →
   `eceaa3bb…` in one coordinated change rather than through the intermediate steps of the
   reverted pilot merge (`d78fb725…` was never a pin on this history). Every current-identity
   carrier (`POKERED_HARNESS_PYBOY_REVISION`, `pyboy/__init__.py`, `scripts/bootstrap_pyboy.py`,
   `tests/_runtime_packaging_support.py`, `tests/_qualification_runner_support.py`, the
   CI/local-runner assertions, and the prose in `README.md` / `VERSIONS.md` / `agents.md` /
   `docs/RELEASE_CHECKLIST.md` / `docs/LINUX_RESUME_PROMPT.md`) carries that value together; the
   recomputation snippet above prints it for this head. Historical `release-evidence/` records and
   the earlier prose that names `c565df66…` are left untouched on purpose — they record what ran
   under the identity of their time, and they are not current-identity statements.
2. **Native ABI re-verification (condition 3).** `#138`'s native ABI was verified on the loaded
   artifacts at the **pilot** pin `d78fb725…` (the two changed modules recompiled inside an
   existing native tree, plus an independent `Enum`-shadowing control). A from-scratch rebuild and
   `--mode cython --check` **on this head** (pin `eceaa3bb…`) remain owed — see item 4; note the
   head asserts its own `EXPECTED_REVISION` and fails closed until the stage is rebuilt against it,
   so the pilot's `rc=0 --check` cannot be carried onto this pin. `#142` does not touch the
   generated pair or any compiled module: `opcodes_gen.py` and the new handlers module are both
   excluded from Cythonization by `setup.py`, and the generated `opcodes.py`/`opcodes.pxd` are
   byte-identical to the pre-split files, so this divergence adds no new build input and cannot
   change the compiled artifact. A native lane is still required for the leaves that do move
   compiled code (#123/#161/#133/#150/#153/#155); it remains gated on the quiet, non-root,
   CPU-allocated runner.
3. **Independent review (condition 5).** Required for each divergence on its exact head. The `#138`
   pilot review (APPROVE-WITH-FINDINGS at `1f43851`) and the `#142` review (REQUEST-CHANGES at an
   earlier head) were taken on **other** heads and do not transfer; a single non-author review on
   the re-landed head's exact identity is the open item.
4. **Full native rebuild not re-run end to end for `#138`.** Its evidence recompiles the two
   changed modules inside an existing native tree; `bootstrap_pyboy.py --mode cython --check` has
   not been re-executed on that head (a concurrent attempt hit the script's own 1800 s install
   timeout under host contention, unrelated to the change).
5. ROM-gated pinball behaviour is **not** exercised anywhere in this environment (no `.gb`/`.sym`/
   `.sav` assets); a synthetic substitute is not acceptable evidence.
6. **Historical records re-qualified under this pin.** #90.3's Yellow registration and #170's
   runtime record were validated at their own heads and do **not** qualify this one; re-qualifying
   them under `eceaa3bb…` is tracked by #235 and stated in the decision doc's
   "a historical record is validated at its own head" update.
7. **One pin-covered comment still says "upstream base".** `pyboy/__init__.py`'s revision comment
   ends with *"it replaced the upstream base revision `c565df66…`"*, which is the same mis-attribution
   corrected everywhere else in this record: `c565df66…` is a **harness-fork** revision, not an
   upstream `Baekalfen/PyBoy` commit (see "Provenance of the pre-divergence revision"). It is left
   in place deliberately because that comment is **inside the content identity** — rewording it
   moves the pin and would re-open #235's re-qualification target for a prose fix. Fold it into the
   **next** pin-moving change rather than triggering a re-pin for it alone.
