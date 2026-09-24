# Harness-local divergence record — vendored PyBoy

Status: **RE-PINNED — harness-local divergence revision.** This tree is *no longer* byte-identical
to the upstream base revision `c565df66c3731fad2856169a90f6bbec99925915`. The revision pins now
carry the harness-local divergence revision `d78fb7253f0d290c15ddb392d05b327aea0faa82`, and the
upstream base is recorded below rather than reused as the pin. The pin is a content identity, not
a git object; its exact definition and recomputation are given under "Harness-local divergence
revision".

Authority: `docs/VENDORED_PYBOY_SPLIT_DECISION.md` (option B, explicit in-fork divergence) and
the `#122` file-split program (`#138`).

## Upstream base

- Repository: `Baekalfen/PyBoy`, PyBoy `2.7.0` source snapshot.
- Base revision: `c565df66c3731fad2856169a90f6bbec99925915`.
- This is the revision the tree was forked from, **not** the current pin. The current pin is the
  harness-local divergence revision below.

## Harness-local divergence revision

- Value: `d78fb7253f0d290c15ddb392d05b327aea0faa82`, carried by `POKERED_HARNESS_PYBOY_REVISION`,
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

  It prints `d78fb7253f0d290c15ddb392d05b327aea0faa82` for this head. The digest deliberately
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

### Verification performed on this head

- **Native ABI (condition 3), demonstrated.** Both modules were Cythonized and compiled
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

## Open items (not done here)

1. **Honest re-pin (condition 2) — DONE.** `POKERED_HARNESS_PYBOY_REVISION`, `pyboy/__init__.py`,
   `scripts/bootstrap_pyboy.py`, `tests/_runtime_packaging_support.py`, the CI/local-runner
   assertions, and the current-identity prose in `README.md` / `VERSIONS.md` / `agents.md` /
   `docs/RELEASE_CHECKLIST.md` / `docs/LINUX_RESUME_PROMPT.md` all carry the harness-local
   divergence revision. Historical gate/evidence records that name the upstream base are left
   untouched on purpose — they record what ran under the pre-divergence identity.
2. **Native ABI re-verification (condition 3).** A fresh `bootstrap_pyboy.py --mode cython --check`
   plus the native unit lane must pass on this split tree. Both modules Cython-compile cleanly in
   isolation (see the review brief), but the full native rebuild has not been re-run for this
   head.
3. **Independent review (condition 5).** See `ledger/REVIEW_TASK_VENDORED_138.md`.
4. **Full native rebuild not re-run end to end.** The evidence above recompiles the two changed
   modules inside an existing native tree; `bootstrap_pyboy.py --mode cython --check` has not been
   re-executed on this head (a concurrent attempt hit the script's own 1800 s install timeout under
   host contention, unrelated to this change).
5. ROM-gated pinball behaviour is **not** exercised anywhere in this environment (no `.gb`/`.sym`/
   `.sav` assets); a synthetic substitute is not acceptable evidence.
