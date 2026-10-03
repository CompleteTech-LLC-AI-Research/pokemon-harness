# #569 — the directory token drops nothing (the check nobody had done)

#569 replaces master's 51-entry `scripts/` enumeration with the `scripts`
directory token. The claim is that this preserves coverage. Verified by
resolving both file sets with the *same* Ruff (`0.16.5`, matching the
`pyproject.toml` pin in both trees) and diffing them.

    master, 51 enumerated paths  ->  51 files
    branch, `scripts` token     ->  113 files

    comm -23 master branch      ->  (empty)

Nothing master covered is dropped. The token widens coverage by 62 files, and
those 62 are genuinely clean rather than nominally covered:

    ruff check <the 62> --no-cache        -> All checks passed!
    ruff format --check <the 62>          -> 62 files already formatted

That is the substantive part: widening a gate is only an improvement if the
newly admitted files actually satisfy it. Here they do, which is what makes
#568's reformat work load-bearing rather than cosmetic.

The one file the token does *not* cover is
`scripts/produce_battle_state_fixtures.py`, held out by the recorded exclusion
plus `force-exclude = true`, with its SHA-1 pinned by
`tests/test_local_ci_policy.py`. That is the intended carve-out, verified
separately.

## Method note

Both trees were first placed on the same Ruff build. A stale `0.14.0` in one
venv and `0.16.10` in another would have made a coverage comparison meaningless
— `pyproject.toml` pins `ruff==0.16.5` for CI, so the comparison has to use that
version rather than whatever a venv happens to carry.
