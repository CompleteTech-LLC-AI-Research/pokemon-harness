# PyBoy main-module split candidate (#133)

Status: **IMPLEMENTED CANDIDATE; NOT MERGE-QUALIFIED.** This document does not
close #133 or change the repository's `PARTIAL` release decision. Follow
`VENDORED_PYBOY_SPLIT_DECISION.md`, including honest identity re-pinning, fresh
source/native verification, and independent review of the final head.

## Why keep one translation unit?

`pyboy.pyboy.PyBoy`, `PyBoyMemoryView`, and `PyBoyRegisterFile` are augmented
by `pyboy.pxd`. Splitting them through ordinary Python mixins or dynamically
assigning their methods would not preserve that native declaration surface.
Instead, five hand-authored components preserve the original code verbatim:

| Component | Original lines | Lines | Responsibility |
|---|---|---:|---|
| `_pyboy_init.pxi` | 1-527 | 527 | Imports, constants, constructor and API initialization |
| `_pyboy_runtime.pxi` | 528-811 | 284 | Frame dispatch, events, pause, lifecycle and shutdown |
| `_pyboy_controls.pxi` | 812-1063 | 252 | Buttons, delayed input, state save/load |
| `_pyboy_api.pxi` | 1064-1555 | 492 | Game-area API, palettes, symbols, hooks and graphics access |
| `_pyboy_memory.pxi` | 1556-2118 | 563 | Register file and banked memory view |

These are source fragments, not independently importable Python modules or
separately compiled Cython includes. They retain indentation and join in the
fixed order in `_source.py`. No code is duplicated. A component may continue
the single `PyBoy` class opened by the initialization component.

The concatenation is byte-identical to `pyboy.py` at baseline
`3e865d5b0348d0c7c462ed8b40a107ae2b158277`:

- 83,796 bytes, 2,118 lines.
- Git blob: `7899e2df64b889372589c81ee74263b5ef7e9389`.
- SHA-256: `7996b54cda97238ef4a943c56ab76f998d2854b847b5ce8a9a9fba83c940cec3`.

## Python and native loading

The public `pyboy.py` facade executes this trusted installed source in its
own namespace. Class names, `__module__`, function signatures, docstrings,
private name mangling and module globals are preserved. `linecache` receives
the assembled text so standard `inspect` and tracebacks use the right lines.
The loader does not execute remotely supplied source or accept a runtime
component list. A missing packaged component raises an error; there is no
fallback to another PyBoy installation.

The optional native build uses the same assembler. It stages `pyboy.py` and
its unchanged augmenting `pyboy.pxd` under `build/pyboy-components/pyboy/` and
passes that source to the existing `pyboy.pyboy` extension. The real vendor
root remains in Cython's include path. All other extension names, compiler
flags and inputs are unchanged. `_source.py` is Python/build support and is
excluded from Cythonization. Component files and the `.pxd` are explicit
extension dependencies, so their changes invalidate the native build.

The assembled compiler input exceeds 1,000 lines but is transient build
output, never tracked or shipped as a second source implementation. Every
tracked component is below 1,000 lines. Use the supported bootstrap/setup
path for native builds, not direct Cythonization of the loader facade.

The harness wheel's explicit allowlist includes each component. The standalone
vendor packaging already includes `.pxi` resources. Direct tools that read
`pyboy.py` from disk without importing/assembling it will now see the facade;
this tooling tradeoff requires review. Python-mode import also compiles the
assembled text once per import rather than reusing the old monolithic `.pyc`.
There is no additional dispatch in the emulator's per-frame hot path.

## Verification and identity

Run the structural and controlled-behavior diagnostics with:

```bash
python scripts/check_pyboy_components.py
```

These diagnostics check the baseline blob, bounded files, unchanged `.pxd`,
packaging allowlist, native staging, public names/signatures/bytecode,
introspection, register masks, input queues, memory reads/writes, symbol
lookup, constructor failures and instance-level tick ownership. Their import
doubles are confined to the diagnostic script. They are not a source-runtime
or native-runtime acceptance suite, and do not run ROMs.

The fallback delivery's `apply.py` creates an isolated worktree at the exact
baseline, verifies and applies the source diff, recomputes the full vendored
content identity using the repository's existing recipe, and updates the
live identity carriers together. Use that helper rather than applying the
source-only diff and retaining the old pin. The new digest must be computed
from the complete checkout, not from a partial source reconstruction.

No existing pytest test, skip, xfail, MCP name, public entrypoint or CLI flag
is modified. This does not establish test-collection parity by itself: run
baseline and candidate collection and the existing full unit lane under both
supported runtimes. Also run source/native bootstrap identity checks, clean
wheel installation, and independent review on the final re-pinned head.
Any failed or unexecuted gate remains explicit; no release claim is advanced.
