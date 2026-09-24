# Harness-local divergence record — vendored PyBoy

Status: **DIVERGENCE DRAFTED, NOT RE-PINNED.** This tree is *no longer* byte-identical to the
upstream base revision `c565df66c3731fad2856169a90f6bbec99925915`. The revision pins still carry
that upstream string, so the identity is currently **stale by design of this draft** and the
honest re-pin (decision-doc condition 2) is the lead-owned integration step, not part of this
split. Do not treat the pin as truthful until that step lands.

Authority: `docs/VENDORED_PYBOY_SPLIT_DECISION.md` (option B, explicit in-fork divergence) and
the `#122` file-split program (`#138`).

## Upstream base

- Repository: `Baekalfen/PyBoy`, PyBoy `2.7.0` source snapshot.
- Base revision: `c565df66c3731fad2856169a90f6bbec99925915`.
- Pinned by `POKERED_HARNESS_PYBOY_REVISION`, `pyboy/__init__.py::__pokered_harness_revision__`,
  `scripts/bootstrap_pyboy.py::EXPECTED_REVISION` and
  `tests/_runtime_packaging_support.py::EXPECTED_PYBOY_REVISION`.

## Divergence 1 — `#138` split of the Pokemon Pinball plugin

`pyboy/plugins/game_wrapper_pokemon_pinball.py` was 1552 lines, above the repository's
1000-line file-split bound. It is now split into:

| File | Lines | Contents |
|---|---|---|
| `pyboy/plugins/game_wrapper_pokemon_pinball.py` | 791 | facade: `__pdoc__`, imports, `Stage`, `SpecialMode`, `BallType`, `BallSize`, the stage lists, and the `cdef class GameWrapperPokemonPinball` |
| `pyboy/plugins/game_wrapper_pokemon_pinball_data.py` | 771 | relocated `Pokemon` and `Maps` enums, plus the RAM-address / bank-offset constants and the four `*StageMapWildMons*` tables |

The facade re-exports every relocated name with
`from .game_wrapper_pokemon_pinball_data import *  # noqa: F403`, so the module's public surface
is unchanged.

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

1. **Honest re-pin (condition 2).** `POKERED_HARNESS_PYBOY_REVISION`, `pyboy/__init__.py`,
   `scripts/bootstrap_pyboy.py` and `tests/_runtime_packaging_support.py` still carry the upstream
   base string while the files differ. `README.md` / `VERSIONS.md` prose is untouched. This is a
   release-identity change and stays with the lead integrator.
2. **Native ABI re-verification (condition 3).** A fresh `bootstrap_pyboy.py --mode cython --check`
   plus the native unit lane must pass on this split tree. Both modules Cython-compile cleanly in
   isolation (see the review brief), but the full native rebuild has not been re-run for this
   head.
3. **Independent review (condition 5).** See `ledger/REVIEW_TASK_VENDORED_138.md`.
4. ROM-gated pinball behaviour is **not** exercised anywhere in this environment (no `.gb`/`.sym`/
   `.sav` assets); a synthetic substitute is not acceptable evidence.
