# #566 @ `c198489` — the new in-file-suppression row guards one file, not the lane

## What the PR's newest commit gets right

`c198489` closes a real escape, and it is genuinely reachable. Measured directly
on `c198489` with a real `F821` appended to the benchmark:

```
benchmark + F821 violation, no directive       -> ruff check exit=1  F821_count=1
benchmark + F821 + `# ruff: noqa`              -> ruff check exit=0  F821_count=0
benchmark + F821 + `# ruff: noqa: F821`        -> ruff check exit=0  F821_count=0
```

Every probe in the file lints a snippet on **stdin**, so a directive living in
the file's own bytes is invisible to all of them. The commit is right about
that, and right that matching directives as text is wrong: `tokenize`-based
comment detection plus a `--output-format=json` code match is the correct shape.

The row also does not over-reject. Each of these stays green on `c198489`:

```
# noqa: E501                                   green  (Ruff does not honour bare `# noqa: CODE` file-wide)
# RUFF: NOQA                                   green  (case-sensitive in Ruff's parser)
# ruff: noqa is a comment in prose here        green  (suffix must be `: CODE`)
'# ruff: noqa' quoted inside the docstring     green  (not a comment token)
```

And each of these turns it red, as claimed:

```
# ruff: noqa      F    # ruff: noqa: F821   F
# flake8: noqa    F    #ruff:noqa           F
```

124 rows pass on this head (`tests/test_local_ci_policy.py` +
`tests/test_matrix_concurrency_policy.py`).

## The gap: `_BENCHMARK` is a constant, so the guard is a point guard

`tests/test_local_ci_policy.py:472` pins `_BENCHMARK =
"scripts/benchmark_matrix_concurrency.py"`, and the new row scans that one path.
The escape is a property of **any** file Ruff lints, not of the benchmark, and
the lane list has ~50 more entries. Measured on `c198489`, planting
`# ruff: noqa` on the first line of each of these:

| lane file | ruff exit | `test_local_ci_policy.py` |
|---|---|---|
| `scripts/bootstrap_pyboy.py` | 0 | 19 passed |
| `scripts/production_gate.py` | 0 | 19 passed |
| `src/pokered_harness/_mcp_facade_entry.py` | 0 | 19 passed |

And it is live, not theoretical. On `scripts/production_gate.py`, `# ruff: noqa`
plus a real `def _probe(): return _undefined_zzz`:

```
ruff check --output-format=json scripts/production_gate.py -> exit 0, F821 reported: False
pytest tests/test_local_ci_policy.py                       -> 19 passed
```

That file is named 3x in `scripts/run_local_ci.sh` and 3x in
`.github/workflows/release-hygiene.yml`, so it is squarely inside the enforced
lane. The commit's own framing ("an in-file opt-out makes the whole file
unlintable while the suite stays green") is exactly right — it just does not hold
for 49 of the 50 files the claim is about.

This is not an over-rejection risk to trade against: the row already proves each
candidate directive really does silence Ruff, so generalising it adds no new
assumption. `src/` and `tests/` are covered by directory tokens, so the scan
should resolve the lane's real file set (the existing
`_ruff_lint_resolved_files()` already does this for `tests`) rather than pin one
path.

## Status

This is a review finding on my own prior work, recorded rather than buried.
`c198489` is **not** cleared to merge on this point. #566 stays draft-pending;
the fix belongs on the branch, and re-review must cover the resulting head, not
`c198489`.

Release remains **PARTIAL**. Nothing merged, no issue closed.
