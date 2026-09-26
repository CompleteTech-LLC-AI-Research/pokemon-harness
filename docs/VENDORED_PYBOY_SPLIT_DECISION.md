# Current combined vendor candidate

The tracked vendor tree now includes the generated opcode layout (#123), the main PyBoy
source components (#133), the serial type split (#153), and the bounded motherboard and
LCD component splits (#150, #155). Its recomputed full-manifest content identity is
`e91b07c39474e40265ca88684f7ce3d9ea171096`. Earlier identities below are historical
snapshots and do not qualify this combined tree. The native build, source/native unit
gates, packaging checks, and exact-head review remain required. Issue #122 remains open
because other file splits are outstanding.

---

# Combined #153 and #133 vendor candidate

The current tracked vendor tree includes the serial type split (#153) and main PyBoy source
components (#133). Its recomputed content identity is `acd569fdb5836b58558856ac066102d1e9ccaf96`. The separate
identity notes below describe earlier single-change candidates and are retained as history.
Full native and repository gate qualification remains pending; #122 remains open.

# Additional unqualified #153 serial split

The candidate based on `3e865d5b0348d0c7c462ed8b40a107ae2b158277` carries vendor content identity
`0a0f7f315e58e1c06a8c2cb9a716b8116fdf92ef`. This is not an acceptance decision. All native,
runtime, public-surface, and independent-review gates below remain required.
The prior dated decisions and identity records below are retained as history.

# Decision: handling the eight vendored PyBoy split leaves (#123, #133, #138, #142, #150, #153, #155, #161)

Status: **DECIDED — explicit in-fork divergence; execution has started (see the "Update" sections
below, including the re-pin). The leaves remain OPEN.** No leaf is completed by this document, and
no leaf may be closed merely because its original file disappears (#122 requirement).

Scope: the vendored sub-issue group listed under "Vendored pyboy (decision pending)" in #122.
Owner of this decision: the lead integrator (shared integration / release-identity file owner).

## Context

`vendor/pyboy-src/` is a pinned third-party PyBoy `2.7.0` source snapshot, not harness source and
not an upstream branch. Its identity is recorded twice and asserted by the repository:

- `vendor/pyboy-src/POKERED_HARNESS_PYBOY_REVISION` = the current pin (see the Update sections: it
  is now the harness-local divergence revision, not the pre-divergence fork revision)
- `vendor/pyboy-src/pyboy/__init__.py` `__pokered_harness_revision__` = the same value

The **pre-divergence fork revision** this tree descended from remains
`c565df66c3731fad2856169a90f6bbec99925915`, recorded in
`vendor/pyboy-src/POKERED_HARNESS_PYBOY_DIVERGENCE.md`. It is a commit of the harness's own PyBoy
fork (`CompleteDotTech/pyboy-link-cable-fork`, 32 commits ahead of upstream tag `v2.7.0` =
`4627b90b878e91faff443b3acd6d4e4be09a4387`), **not** an upstream `Baekalfen/PyBoy` revision. It
stopped being the pin once the tree diverged.

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
   the inherited (pre-divergence, harness-fork) revision — not an upstream base, because none of the
   PyBoy revisions this tree descends from is an upstream commit — (b) update
   `POKERED_HARNESS_PYBOY_REVISION`, `README`/`VERSIONS.md`,
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

The option-B conditions are **not all** exercisable in this environment. Condition 1's
reproducibility requirement is now met (see the second bullet); conditions 2 and 3 remain blocked:

- **No compiled runtime (condition 3).** There is no built `pyboy` extension module set and the
  native build cannot be produced here. Re-verified 2026-09-23:
  - the compiler and Cython are present — `gcc (Debian 12.2.0-14+deb12u1) 12.2.0` and a virtualenv
    with `Cython 3.0.12` — so the blocker is not a missing compiler;
  - the **CPython development headers are absent**: `/usr/include/python3.11/` does not exist,
    `python3.11-dev` is not installed, `python3-config` is unavailable, and `uid=1000` with
    `CapEff=0000000000000000` and no `sudo` means no package can be added. Every `cdef`/C-extension
    compile therefore fails with `fatal error: Python.h: No such file or directory`;
  - **`/dev/shm` is mounted read-only** (`tmpfs ro,nosuid,nodev,noexec,size=64000k`), so the vendor
    `setup.py` `build_ext` path — which passes `nthreads=cpu_count()` into
    `Cython.Build.cythonize` — dies inside `multiprocessing.Pool` with
    `OSError: [Errno 30] Read-only file system`. Forcing `cpu_count()` to 0 runs the Cython stage
    serially and emits the 58 generated `.c` files, but the subsequent compile still fails on the
    missing `Python.h` above.
  Condition 3 (native/Cython ABI verified on the split tree) therefore cannot be exercised, and
  source-only success is explicitly insufficient.
- **Generator determinism — proven (condition 1's reproducibility requirement met).** The prior
  claim that regeneration "cannot be established reliably here" is **withdrawn as stale**. Re-run
  2026-09-23 from a clean scratch directory: `opcodes_gen.py` fetches
  `http://pastraiser.com/cpu/gameboy/gameboy_opcodes.html` (`HTTP 200`, `56659` bytes) and a fresh
  execution reproduces **byte-identical** output that matches the pinned files:
  `opcodes.py` sha256 `2538898d44bc74deb448b995cc4888b94296fce58ab324f59f8d18ef75b06dd6`,
  `opcodes.pxd` sha256 `b1c0c5ad1298789c13a3eec1aca9cd3d1bc701a1209f96803f03898bfea07cf5` (both equal
  to the tracked files). Regeneration determinism is no longer a blocker; what still blocks the
  generated pair — and every other leaf — is condition 2 plus the condition 3 native lane.
- **Identity re-pin is a release-identity change (condition 2)** with a required native lane, so it
  cannot be declared terminal without the runtime modes above.

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

Requirement (ii) — reproducible `opcodes_gen.py` output against the pinned upstream tables — is now
**satisfied** (byte-identical regeneration proved above). The group therefore reduces to one
remaining external prerequisite: **(i) an environment with the Cython build toolchain plus the
CPython development headers and a writable shared-memory/temp device**, so `build_ext` can build
and condition 3 (native ABI) can be verified. With that, a single pilot leaf — expected **#138
`plugins/game_wrapper_pokemon_pinball.py`**, whose Python-only plugin surface has no Cython `.pxd`
sibling — can be split and reviewed first, followed by the `.pxd`-backed cores and finally the
generated opcode pair.

## Update — 2026-09-24: the native build CAN be produced here (supersedes the condition-3 bullet)

The "No compiled runtime (condition 3)" bullet above concludes the native build "cannot be produced
here" and that every C-extension compile fails on `fatal error: Python.h: No such file or
directory`. Re-audited **2026-09-24** on `origin/master`: that conclusion is an artifact of the
*default* interpreter, not a hard limit. A native build **was** produced and its identity check
passed.

Root causes of the earlier failure, and their remedy:

- The system `/usr/bin/python3.11` has no `python3.11-dev` headers and there is no root/sudo — true.
  But other CPythons on the box ship development headers. A uv-managed **CPython 3.12.14** provides
  `Python.h` and `libpython3.12.so` (any CPython with dev headers works; e.g. `uv python install
  3.12`). Building against it is the remedy, not a blocked path.
- `/dev/shm` is read-only — true. Cython's `build_ext` passes `nthreads=cpu_count()` into
  `cythonize`, whose `multiprocessing.Pool` needs a writable shared-memory device. Remedy: run the
  build under a private mount namespace with a writable tmpfs on `/dev/shm` (the same pattern the
  test lanes already use).

Verified in an isolated worktree at `origin/master` `0f1b824` (`docs` content unchanged at
`cd3c3fe`):

```bash
unshare -rm --propagation private bash -c \
  'mount -t tmpfs -o size=8g tmpfs /dev/shm; export TMPDIR=/tmp; \
   "$VENV312/bin/python" scripts/bootstrap_pyboy.py --mode cython'
"$VENV312/bin/python" scripts/bootstrap_pyboy.py --mode cython --check   # exit 0
```

- Built wheel `pyboy-2.7.0-cp312-cp312-linux_x86_64.whl`, sha256
  `803b1cc08dbe2b1d5f24e9eed5f8cf4e599233baacf049146ab3aa2ab980d803`.
- `--mode cython --check` returns **0** (deterministic re-run); the required modules resolve to
  compiled extensions (`pyboy.core.mb/serial/lcd/opcodes` →
  `*.cpython-312-x86_64-linux-gnu.so`); `pyboy.utils.cython_compiled == True`; and
  `pyboy.__pokered_harness_revision__ == c565df66c3731fad2856169a90f6bbec99925915` (pin intact).

So condition 3's **build + identity-check** half is satisfiable here. What still cannot be shown is
its **native-lane-passes** half: `production_gate.py --runtime-mode cython --tier unit` on
unmodified master returns **FAIL**, entirely on the already-known host-resource families —
contention-sensitive `test_mcp_timed_*` / `test_probe_timed_rom_pair_*` deadline tests (host was at
~34/4 CPUs), a root-in-mount-namespace artifact in `test_qualification_runner_lockstate` (root maps
the asset root "writable by this job"), and
`test_qualification_runner_release::test_unwritable_host_lock_directory_refuses_to_launch` (the same
operator CPU-allocation blocker recorded for #85/#86); one wheel-build test also hit the gate's
900s per-tier timeout under load. None of these failures is native-specific.

**Revised bar.** With requirement (ii) already satisfied (see the generator bullet above) and the
toolchain now shown buildable here, the group reduces to a single external prerequisite: a
**quiet, non-root, CPU-allocated** runner able to take the native unit lane to green. With that,
pilot leaf `#138` can be split, rebuilt natively, its lane run, and independently reviewed. The
eight leaves stay **OPEN**, `#122` stays **OPEN**, release status stays **PARTIAL**; this update
fixes the record and advances no leaf.

## Update — 2026-09-24: a historical record is validated at its own head, and it does not qualify a later head

Condition 2 requires the divergence commit to re-pin the harness-local revision. The shipped
registration bundle `release-evidence/feature-qualification/issue90-medicine-yellow-2d87676/` is a
**historical** real-ROM record of a run at head `2d876763` under the then-current pin `c565df66…`.
Its own README states that nothing in it "is a claim about any later commit", and the repository's
standing policy agrees (`agents.md` §"Historical evidence that must remain labeled historical";
`VERSIONS.md`, "historical rows … do not qualify a later head").

The unit guard `tests/test_battle_healing_registration.py::test_runtime_registration_bundle_is_sanitized_and_consistent`
nevertheless compared the bundle's `vendored_revision_marker` against the **live** `VERSIONS.md`
pin. Those two commitments are incompatible the moment condition 2 is exercised: any re-pin moves
the live pin and invalidates every historical record tied to the previous one. That is exactly what
made `975f1a5` (the pilot merge) red — a deterministic, ROM-free unit failure introduced by the
re-pin, not by the split.

**Recording the resolution.** The guard now checks the marker against the pin that was in effect at
the bundle's own recorded `worktree_head`, read from the committed blob at that head, and
additionally requires that head to resolve here, to carry exactly the recorded `worktree_tree`, and
to be an ancestor of `HEAD`. A fabricated marker, head, or tree — or a head whose object is absent
in this checkout — fails closed rather than falling back to the live pin, so the provision cannot
launder a bad record. This binds a record to the commit it describes rather than to "whatever the
pin says today", a narrowing of the accepted value (one specific historical pin), not a relaxation
to arbitrary drift. Current-identity pin enforcement is unchanged and lives elsewhere
(`scripts/production_gate.py` requires the measured runtime revision to equal the manifest pin at
gate time, and the runtime-packaging tests and `scripts/bootstrap_pyboy.py --check` assert it).

**This is a coverage statement, not a green light.** Because the divergence re-pin changes the
vendored runtime identity, the pre-divergence bundle describes a different measured runtime than
the post-divergence candidate. Under the policy quoted above it therefore **does not qualify a
later head**: #90.3's Yellow registration and #170's runtime record must be **re-qualified under
the new pin** before that evidence may be read as describing the current candidate. Correcting the
guard so it validates the record against the state the record names keeps the record honestly
labeled as historical; it does **not** establish #90.3 or #170 for any later head, and it changes
no leaf's acceptance status.

Consequence: condition 2 can be executed without regenerating a real-ROM bundle (impossible here —
no `.gb`/`.sym`/`.sav` assets). The group's remaining external prerequisite is unchanged: the
native unit lane still needs a **quiet, non-root, CPU-allocated** runner to reach green (condition
3), and #90.3/#170 re-qualification is a new owed row. The eight leaves stay **OPEN**, `#122` stays
**OPEN**, release status stays **PARTIAL**; this update records the evidence-integrity semantics and
advances no leaf on its own.

## Update — 2026-09-24 (d): the #138 and #142 splits re-landed together after the historical-pin guard fix

The pilot merge (`975f1a5`) was reverted by `#230` **because the condition-2 re-pin broke the
registration guard** — not because of the split. **`#234` fixed that guard** (it now validates the
`#90.3` bundle against the pin in force at the bundle's own recorded `worktree_head`, not against
the live pin; see the section above). With that regression removed, the divergence can be
re-landed, and both pilot leaves were re-landed **together** on the post-`#234` `master`:

- **`#138` (Divergence 1).** `pyboy/plugins/game_wrapper_pokemon_pinball.py` (1552) is now a
  791-line facade plus `game_wrapper_pokemon_pinball_data.py` (875), both under the bound. The
  `cdef class` stays put (`game_wrapper_pokemon_pinball.pxd` declares it and `plugins/manager.pxd`
  cimports it as a `cdef public` extension type), so only module-level data moved; the data module
  re-exports its names under an explicit `__all__`, because a bare `import *` leaked `Enum`, which
  shadows the C type `Enum` in the facade's translation unit — a native-only failure that source
  mode hid.
- **`#142` (Divergence 2).** `pyboy/core/opcodes_gen.py` (1457) is now a 565-line facade plus
  `pyboy/core/opcodes_gen_handlers.py` (923) holding the 48 `OpcodeData` handler methods;
  `OpcodeData` is `class OpcodeData(OpcodeDataHandlers)`. Both files are under the bound. The
  generator and the new handlers module are excluded from Cythonization by `setup.py`, and
  regeneration stays byte-identical (`opcodes.py` `2538898d…`, `opcodes.pxd` `b1c0c5ad…`), so this
  leaf adds no compiled build input and the generated pair is untouched.
- **Honest re-pin (condition 2) executed once, covering both.** The vendored manifest now hashes to
  the harness-local divergence revision `eceaa3bb15dedd6847a3a37d3400421e3024cb5c`. Every
  current-identity carrier (`POKERED_HARNESS_PYBOY_REVISION`, `pyboy/__init__.py`'s
  `__pokered_harness_revision__`, `scripts/bootstrap_pyboy.py::EXPECTED_REVISION`,
  `tests/_runtime_packaging_support.py::EXPECTED_PYBOY_REVISION`,
  `tests/_qualification_runner_support.py`, the CI assertions in
  `.github/workflows/release-hygiene.yml` / `scripts/run_local_ci.sh`, and the current-identity
  prose in `README.md` / `VERSIONS.md` / `agents.md` / `docs/RELEASE_CHECKLIST.md` /
  `docs/LINUX_RESUME_PROMPT.md`) moved together. The pre-divergence fork revision `c565df66…` is
  recorded, not reused as the pin (it is a harness-fork revision, not an upstream `Baekalfen/PyBoy`
  commit; upstream `v2.7.0` is `4627b90b…`). Historical `release-evidence/` records and earlier prose
  that names `c565df66…` are left untouched — they describe runs under the identity of their time.

  One pin-covered comment is deliberately left mis-attributed in this pass: `pyboy/__init__.py`'s
  revision comment still ends *"it replaced the upstream base revision `c565df66…`"*. That comment is
  **inside the content identity**, so rewording it moves the pin and would reopen #235's
  re-qualification target for a prose-only change. The correction is folded into the **next
  pin-moving** vendored change instead; the residual is recorded as open item 7 in
  `vendor/pyboy-src/POKERED_HARNESS_PYBOY_DIVERGENCE.md`.
  A guard pins the residual rather than trusting it: `tests/test_vendored_provenance_wording.py`
  fails if that comment is reworded without a matching re-pin, or if any of the corrected carriers
  regress to "upstream base".

Condition status for the two re-landed leaves: 1 (generation) satisfied byte-identically; 2
(re-pin) executed; 4 (public API) preserved — no public symbol, module path, or `cdef` surface
moved; 3 (Cython ABI) — `#138`'s earlier native rebuild is **not** re-run on this head, and the
native unit lane still requires the quiet, non-root, CPU-allocated runner, so **condition 3 is not
terminal**; 5 (independent review) pending on the exact head. `#138` and `#142` therefore stay
**OPEN**, `#122` stays **OPEN**, and release status stays **PARTIAL**.

## Issue #133 candidate: bounded main-module source components

Candidate content identity: `2385be4897e993f2c92418ec6d297425684687d8` (inherited `eceaa3bb15dedd6847a3a37d3400421e3024cb5c`). The main Python
source is split into five verbatim components, assembled into one namespace
and one native translation unit with unchanged `pyboy.pxd`. See
`PYBOY_MAIN_MODULE_SPLIT.md` for design, tradeoffs and pending gates.
This records implementation and re-pinning, not acceptance: #133 remains
unqualified until full source/native checks and independent review pass.
