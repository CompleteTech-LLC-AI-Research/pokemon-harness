# Harness-local divergence record — vendored PyBoy

Status: **RE-PINNED — harness-local divergence revision.** This tree is *no longer* byte-identical
to the upstream base revision `c565df66c3731fad2856169a90f6bbec99925915`. The revision pins now
carry the harness-local divergence revision `3b4e9d23463f83d62a4f0cccde35dfa045ad9f79`, and the
upstream base is recorded below rather than reused as the pin. The pin is a content identity, not
a git object; its exact definition and recomputation are given under "Harness-local divergence
revision".

Authority: `docs/VENDORED_PYBOY_SPLIT_DECISION.md` (option B, explicit in-fork divergence) and
the `#122` file-split program (`#142`).

## Upstream base

- Repository: `Baekalfen/PyBoy`, PyBoy `2.7.0` source snapshot.
- Base revision: `c565df66c3731fad2856169a90f6bbec99925915`.
- This is the revision the tree was forked from, **not** the current pin. The current pin is the
  harness-local divergence revision below.

## Harness-local divergence revision

- Value: `3b4e9d23463f83d62a4f0cccde35dfa045ad9f79`, carried by `POKERED_HARNESS_PYBOY_REVISION`,
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

  It prints `3b4e9d23463f83d62a4f0cccde35dfa045ad9f79` for this head. The digest deliberately
  excludes the marker file and this record, so a later in-fork split changes the revision and
  requires the pins above to be refreshed together (decision-doc condition 2).

## Divergence 1 — `#142` split of the opcodes generator

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

1. **Honest re-pin (condition 2).** Executed here: the identity is a content identity over the
   tracked manifest, so the `#142` divergence moved `c565df66…` → `3b4e9d23463f83d62a4f0cccde35dfa045ad9f79`.
   Every current-identity carrier (`POKERED_HARNESS_PYBOY_REVISION`, `pyboy/__init__.py`,
   `scripts/bootstrap_pyboy.py`, `tests/_runtime_packaging_support.py`,
   `tests/_qualification_runner_support.py`, the CI/local-runner assertions, and the prose in
   `README.md` / `VERSIONS.md` / `agents.md` / `docs/RELEASE_CHECKLIST.md` /
   `docs/LINUX_RESUME_PROMPT.md`) carries the new value together; the recomputation snippet above
   prints it for this head. Historical `release-evidence/` records and the earlier prose that
   names `c565df66…` are left untouched on purpose — they record what ran under the identity of
   their time, and they are not current-identity statements.
2. **Native ABI re-verification (condition 3).** `#142` does not touch the generated pair or any
   compiled module: `opcodes_gen.py` and the new handlers module are both excluded from
   Cythonization by `setup.py`, and the generated `opcodes.py`/`opcodes.pxd` are byte-identical to
   the pre-split files, so this divergence adds no new build input and cannot change the compiled
   artifact. A native lane is still required for the leaves that move compiled code
   (#123/#161/#133/#150/#153/#155); it remains gated on a quiet, non-root, CPU-allocated runner.
3. **Independent review (condition 5).** Required for this divergence on its exact head.
