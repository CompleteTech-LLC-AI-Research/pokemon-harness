# #570 re-confirmed after the #569 retraction

The #569 format-lane finding was retracted as a `.ruff_cache` artifact. That
retraction does **not** extend to #570, which was re-measured independently on
`43b977d` with a clean per-checkout venv and `--no-cache`.

Planted directive on the first line of each file, plus a real undefined name,
tree restored after each:

    scripts/bootstrap_pyboy.py                 ruff_exit=0  F821=0  policy_row=19 passed
    src/pokered_harness/_mcp_facade_entry.py   ruff_exit=0  F821=0  policy_row=19 passed
    scripts/production_gate.py                 ruff_exit=0  F821=0  policy_row=19 passed

All three confirm the escape is live on non-benchmark lane files while the whole
policy suite stays green. #570 stands as filed.

## Method note

The two work were different in kind. The #569 numbers came from a worktree
whose `.ruff_cache` was being written concurrently by a running full suite, and
two runs of the same command on the same commit disagreed — that is an
environment fault and the finding is retracted. The #570 numbers were taken on a
file-scoped `ruff check --output-format=json <file>` with `--no-cache`, which
does not depend on a tree-wide cache state, and each result was reproduced after
restoring the file.

A public correction was posted to #570 withdrawing the two-file inventory before
this re-measurement. That withdrawal was itself the error — I retracted a result
I had not re-run. #570's original report stands unamended.
