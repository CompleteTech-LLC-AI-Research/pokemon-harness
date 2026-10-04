# #567 residue classified: one real bug, two legitimate patterns, one scope decision

Worktree `/home/agent/wt567d`, branch `fix/567-lane-residue-v2`, full 3-commit
#567 stack rebased onto `a413eeb5` (`fd91a1b` step 1, `ffc680d`, `4680f80`).
Rebase conflicts were in the three lane files; each resolution kept step 2's
directory token and master's #566-era format lane.

**Correcting my own earlier rebase.** A previous attempt rebased step 2 onto
master *without* step 1 applied, which dropped step 1's 40 script reformats and
produced a misleading "40 files would be reformatted". The real shape is:
`1cc86f8` is **only** the 40 script reformats, and `11eb719` is **only** the four
lane files. Rebasing the whole stack gives the true residue below.

## Format lane: now green

    ruff format --check scripts tests --no-cache
    -> 447 files already formatted

Two files were one stray blank line short:

- `scripts/full_to_brock.py` (line 408)
- `tests/_tcp_trade_peer_drive_setup.py` (line 43)

Both are **AST-identical to master** after formatting, so the fix is provably
presentation-only. This closes the residual noted in
`LEAD_20261004T1700Z_569_REBASE_RESIDUE.md`.

## Lint lane: 163 errors, three distinct causes

`ruff check scripts tests --no-cache` exits 1. pyproject sets no `select`, so
Ruff's default set (E4/E7/E9/F) applies and all of these are genuinely in the
lane. 136x E402, 22x E731, 5x F811 across 50 files.

### 1. A real bug: docstring after `from __future__` (17 E402s, 6 files)

    tests/_pyboy_link_session_roms_support.py
    tests/_pyboy_link_session_roms_trade_support.py
    tests/_pyboy_link_session_roms_serial.py
    tests/_pyboy_link_session_roms_battle_support.py
    tests/test_pyboy_link_session_roms.py
    tests/test_pyboy_link_session_roms_diagnostics.py

Each has the module docstring **after** `from __future__ import annotations`.
Python only treats a string as a docstring when it is the first statement, so
`__doc__` is `None` and the documentation is silently a no-op expression.
Verified:

    >>> ast.get_docstring(ast.parse(open(p).read()))
    None

This is not a lint nit. The fix is to move the docstring above the `__future__`
import, which also removes the E402. It changes no runtime behavior, and it
restores six modules' `__doc__`.

### 2. Legitimate `sys.path` bootstrap (part of the 89 "other")

Scripts that must run both as `python scripts/foo.py` and under pytest insert
`src` on `sys.path` before importing, e.g. `scripts/link_trade_demo.py:30`,
`scripts/diagnose_pair_trade.py:68`. The import that follows is unavoidably not
"at top of file". Moving it is not an option — it would break direct launch.

### 3. Documented post-split re-exports (30 E402s, 12+ files)

`scripts/production_gate.py`, `scripts/gate_capacity.py` and siblings re-export
their split modules' names at the **end** of the file, with an explicit comment
saying this preserves the module surface and every `gate.<name>` monkeypatch
target the tests rely on. Those imports cannot move to the top without changing
the module's public surface and breaking monkeypatch-based tests.

## The scope decision this forces

Neither pattern 2 nor pattern 3 is a defect. Making the directory-token lane
green therefore requires a **scoped, measured allowance**, not a mass edit:

- `per-file-ignores` for E402 on the specific bootstrap/re-export files, pinned by
  a policy row so the allowlist cannot silently grow, and ideally verified by
  re-linting each allowed file with the directive removed (the approach #572
  already uses for selective suppressions).

E731 (22, lambda-to-def) and F811 (5, redefinition in tests) are ordinary lint
findings in newly-covered files and can be fixed directly.

## Status

This is **new work on top of #568/#569**, not a rebase artifact, and it needs its
own review. Nothing pushed, no PR modified. Original branches untouched at
`1cc86f8` and `25607fc`.
