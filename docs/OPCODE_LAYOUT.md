# Generated opcode component layout — candidate for #123

Status: **candidate implementation, not acceptance or release qualification**.
The in-fork divergence decision still requires an honest content re-pin, the
actual source/native unit lanes, and independent review of the exact head.
Neither the current issue nor any related leaf is closed by this document.

## Source components, not independent runtime modules

`pyboy/core/opcodes_gen.py` retains the original instruction templates and
emits the original Python and Cython declaration streams in memory. Its final
emission stage, `opcodes_layout.py`, writes content-addressed `.pxi` source
components grouped as shared definitions, instructions, dispatch, tables, and
Cython signatures. The default component limit is 900 physical lines; every
emitted file is checked to remain strictly below 1,000 lines.

These are **source fragments**, not independently importable Python modules.
The existing dispatcher spans fragments. This is a deliberate tradeoff: a
lossless layout preserves every opcode body, comparison, annotation, exception
specification, and declaration byte. It does not replace the dispatcher with a
lookup table or introduce per-instruction wrapper calls. Reviewers must approve
this source-component interpretation of #123; it is not presented as an
independent-module refactor.

The original public module remains `pyboy.core.opcodes`. Its small facade uses
`_opcodes_runtime.py` to validate the literal manifest, reconstruct the original
source, and execute it once in the original module globals. Handler globals,
monkeypatch lookups, `__module__`, logical filenames, and source line numbers
therefore remain in the original namespace. `linecache` retains the logical
source for inspection and tracebacks. Import performs additional file reads,
hashing, and compilation; its performance on the real PyBoy tree is unmeasured.

The source loader does not persist a reassembled `.py` file. A packaged install
must retain the manifest and `.pxi` files, using the existing recursive `.pxi`
package-data rule. Missing files, changed digests, non-literal manifests, path
traversal, symlinked components, and declaration-facade drift fail closed.
Checksums detect corruption; they do not authenticate an untrusted installation.

## Native compilation and declaration compatibility

`opcodes.pxd` includes the generated declaration fragments without separating
a decorator from its declaration. Other Cython consumers keep their existing
module paths and cdef names.

During native compilation, `setup.py` loads the runtime helper without importing
PyBoy, then reconstructs the exact original `.py` and `.pxd` streams under
`build/opcode-layout/pyboy/core/`. The Extension name remains
`pyboy.core.opcodes`; only its compiler input path changes. The helper, manifest,
and generator are excluded from Cythonization but remain available as source.
This avoids compiling a Python facade as if it implemented the cdef declarations.

Reassembled sources, C files, object files, compiled extensions, caches, and
other build output must never be committed. Generated **source** fragments and
their manifest are reviewed source inputs, analogous to the already tracked
original generated pair; they are not compiled build output.

## Regeneration

From `vendor/pyboy-src/pyboy/core/`:

```sh
# Initial split of the pinned generated pair, entirely offline.
python opcodes_gen.py --from-existing

# Later regeneration from the upstream opcode table, as before.
python opcodes_gen.py
```

The offline command also works on an existing valid layout and is idempotent.
The normal command retains the original upstream fetch and instruction logic.
Neither mode permits silently overwriting a damaged generated layout. Fix the
underlying source of a damaged snapshot before regenerating it.

The standalone output-stage CLI is available for diagnostics using explicitly
supplied original `.py` and `.pxd` paths. It is not a separate opcode generator.
Changing the generator's destination globals to arbitrary filenames is rejected:
the runtime/build contract requires sibling `opcodes.py` and `opcodes.pxd` files.
No harness CLI flag, MCP tool/resource name, or documented runtime import changes.

## Required acceptance after preparation

Use the exact same unit selection on the baseline and candidate, in source and
native environments. Preserve every existing test identity and skip/xfail state.
The existing declaration test must inspect the reassembled declaration stream;
its 504-declaration count and exception-specification assertions remain intact.
The new focused tests are additional coverage, not replacements for old tests.

```sh
python -m pytest -q tests/test_opcode_layout.py tests/test_opcode_layout_integration.py
python scripts/bootstrap_pyboy.py --mode source --check
# In the separately provisioned, pinned Cython environment:
python scripts/bootstrap_pyboy.py --mode cython
python scripts/bootstrap_pyboy.py --mode cython --check
```

Run the repository's unit-only production gate in both environments using its
normal interpreter arguments. Retain collection parity, full output, exact head,
computed vendor identity, actual extension origins, and no-new-skip/xfail checks.
The authored opcode-shaped native probe is only a diagnostic for the compilation
strategy; it is not a full PyBoy build, gameplay acceptance, or independent review.
Keep the release `PARTIAL` until the separately required release gates pass.
