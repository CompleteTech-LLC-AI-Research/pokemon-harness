# Decision: handling the eight vendored PyBoy split leaves (#123, #133, #138, #142, #150, #153, #155, #161)

Status: **DECIDED — explicit in-fork divergence is the target handling; the leaves remain OPEN and
BLOCKED in this environment.** No leaf is completed by this document, and no leaf may be closed
merely because its original file disappears (#122 requirement).

Scope: the vendored sub-issue group listed under "Vendored pyboy (decision pending)" in #122.
Owner of this decision: the lead integrator (shared integration / release-identity file owner).

## Context

`vendor/pyboy-src/` is a pinned third-party PyBoy `2.7.0` source snapshot, not harness source and
not an upstream branch. Its identity is recorded twice and asserted by the repository:

- `vendor/pyboy-src/POKERED_HARNESS_PYBOY_REVISION` = `c565df66c3731fad2856169a90f6bbec99925915`
- `vendor/pyboy-src/pyboy/__init__.py` `__pokered_harness_revision__` = the same value

That identity is load-bearing:

- `scripts/bootstrap_pyboy.py` (`--mode source --check`) fails closed unless
  `pyboy.__pokered_harness_revision__ == EXPECTED_REVISION`.
- `tests/test_runtime_packaging_build_contract.py` asserts
  `pyboy.__pokered_harness_revision__ == EXPECTED_PYBOY_REVISION`.
- `tests/test_runtime_packaging_dependency_pins.py` reads the pin file.
- `VERSIONS.md` records the same pin as the documented release runtime.

The eight files and their kinds:

| Leaf | File | Kind |
|---|---|---|
| #123 | `pyboy/core/opcodes.py` (7168) | **auto-generated** by `opcodes_gen.py` |
| #161 | `pyboy/core/opcodes.pxd` (1023) | **auto-generated** by `opcodes_gen.py` |
| #142 | `pyboy/core/opcodes_gen.py` (1457) | generator (fetches upstream opcode tables via `urlopen`) |
| #133 | `pyboy/pyboy.py` (2118) | hand-authored core |
| #138 | `pyboy/plugins/game_wrapper_pokemon_pinball.py` (1553) | hand-authored plugin |
| #150 | `pyboy/core/mb.py` (1220) | hand-authored core (Cython `mb.pxd`) |
| #153 | `pyboy/core/serial.py` (1117) | hand-authored core (Cython `serial.pxd`) |
| #155 | `pyboy/core/lcd.py` (1087) | hand-authored core (Cython `lcd.pxd`) |

`vendor/pyboy-src/setup.py` excludes `opcodes_gen.py` from the built package and expects the
generated `opcodes.py`/`opcodes.pxd` to be produced by it. The `.py` core modules are compiled to
Cython extensions in the optional native mode.

## Options considered

**A. Upstream / revendor.** Adopt an upstream PyBoy branch and split there, or revendor a newer
upstream snapshot. *Unavailable.* `docs/upstream_pr/README.md` and `BRANCH_READY.md` are
proposal-only and state explicitly that no upstream branch, PR, or merge status exists ("NOT a
current upstream branch", "NOT READY FOR UPSTREAM CLAIMS"). There is nothing to revendor to, and a
"newer" snapshot would move the pinned revision and invalidate the recorded release identity. This
option cannot be exercised here.

**B. Explicit in-fork divergence.** Split the files inside `vendor/pyboy-src/`, add a divergence
record, and re-pin the harness-local revision. This is the only path that actually satisfies the
1000-line target in this repository, so it is chosen as the target handling. It carries hard,
verifiable conditions (below).

**C. Exclude the vendored tree from the #122 split program.** Rejected as the outcome of this
decision because it would leave the 1000-line objective unmet for these files without an explicit
divergence; option B is the intended resolution. Option C remains available only as a fallback if
option B's evidence bar can never be met.

## Decision

The eight vendored leaves are handled by **explicit in-fork divergence (option B)**. The divergence
must satisfy all of the following before any of the eight leaves is closed:

1. **Generated files are regenerated, never hand-split.** `opcodes.py` (#123) and `opcodes.pxd`
   (#161) must be produced by running `opcodes_gen.py`, not edited or hand-split. A split is only
   acceptable if the split generator, when executed, emits byte-identical generated output to the
   pre-split files (proved by hash before and after). Hand-editing generated files is prohibited.

2. **Identity is re-pinned honestly.** Before divergence, the revision pin is not truthful for a
   modified tree. The divergence commit must (a) add a harness-local divergence revision and record
   the upstream base revision, (b) update `POKERED_HARNESS_PYBOY_REVISION`, `README`/`VERSIONS.md`,
   and `__pokered_harness_revision__` consistently, (c) keep
   `scripts/bootstrap_pyboy.py --check`, `tests/test_runtime_packaging_build_contract.py`, and
   `tests/test_runtime_packaging_dependency_pins.py` passing against the new pin. Leaving the old
   revision string while changing the files is a falsified identity and is not permitted.

3. **Cython ABI verified.** For every `.py` file with a `.pxd` sibling (#150, #153, #155) and for the
   generated pair (#123/#161), the native mode must be rebuilt and
   `scripts/bootstrap_pyboy.py --mode cython --check` plus the native unit lane must pass on the
   split tree with the split re-imported through the public API. A split that only passes in source
   is not acceptable.

4. **Public API and import identity preserved.** No public symbol, module path, or Cython `cdef`
   surface used by the harness may move or disappear; split modules must re-export the original
   names so `import pyboy` and the documented `mb.serial` backend keep resolving.

5. **Independent review on the exact head**, since a divergence is a change of a dependency, not a
   mechanical split.

## Why the leaves stay OPEN here

None of the option-B conditions can be exercised in this environment:

- **No compiled runtime.** There is no `.venv-cython` and no built `pyboy` extension modules, so
  condition 3 (Cython ABI) cannot be verified. Source-only success is explicitly insufficient.
- **Generator cannot be proven.** `opcodes_gen.py` fetches the upstream opcode tables over the
  network at generation time; regeneration determinism against the pinned tables cannot be
  established reliably here, and the generated outputs' hashes are not reproducible under this
  constraint.
- **Identity re-pin is a release-identity change** with a required native lane, so it cannot be
  declared terminal without the runtime modes above.

Therefore the eight leaves are **not completed, not closed, and not merged**, and the decision here
fixes the approach and the evidence bar rather than marking them done. `#122`'s vendored group
remains "decision recorded, execution blocked".

## Consequences

- The vendored group is no longer "decision pending": the handling is explicit in-fork divergence
  with regeneration, honest re-pinning, Cython ABI verification, API preservation, and independent
  review.
- Non-vendored leaves of #122 continue to be split independently; this decision does not gate them.
- The release status stays **PARTIAL**. This document advances no release gate and records no
  real-ROM, native, or allocation result.

## Unblocking

The smallest action that unblocks the group is an environment with (i) the Cython build toolchain
and a buildable PyBoy checkout, and (ii) reproducible `opcodes_gen.py` output against the pinned
upstream tables. With both, a single pilot leaf — expected **#138
`plugins/game_wrapper_pokemon_pinball.py`**, whose Python-only plugin surface has no Cython `.pxd`
sibling — can be split and reviewed first, followed by the `.pxd`-backed cores and finally the
generated opcode pair.
