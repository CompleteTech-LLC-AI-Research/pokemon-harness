# #566 head 16d102e: `maybe_directive` prefix gate has a real blind spot

## Finding (blocking)

Commit `16d102e` added a prefix short-circuit to
`test_benchmark_cannot_opt_out_of_linting_with_in_file_suppression` in
`tests/test_local_ci_policy.py`. The row now only probes comments that pass
`maybe_directive()`:

    directive_prefixes = (
        "# ruff", "#ruff", "# flake8", "#flake8", "# fmt", "#fmt",
        "# yapf", "#yapf", "# noqa", "#noqa",
    )
    def maybe_directive(comment: str) -> bool:
        return comment.strip().lower().startswith(directive_prefixes)

Ruff's own directive grammar accepts **whitespace between the `#` and the
directive keyword**. The gate does not. A file-level directive written as
`#  ruff: noqa` (two spaces) or `#\truff: noqa` (tab) therefore silences the
entire file, while `maybe_directive()` returns `False` and the comment is never
probed. The row stays green on a genuinely unlinted file.

This is exactly the drift class the row exists to catch: a hand-written prefix
list reimplementing a grammar, going out of sync with the real grammar.

## Measured evidence

Ruff `0.14.0`, real files on disk (not stdin — per-file directives are honored
only for real files):

    a.py  x = _undefined_name_probe                     -> reported
    b.py  # ruff: noqa\nx = _undefined_name_probe       -> SILENCED
    c.py  #  ruff: noqa\nx = _undefined_name_probe      -> SILENCED   <-- gate=SKIPPED
    d.py  #   ruff: noqa\nx = _undefined_name_probe     -> SILENCED   <-- gate=SKIPPED

Cross-checked against `maybe_directive()`:

| comment | silences F821 | maybe_directive |
|---|---|---|
| `# ruff: noqa` | yes | probed |
| `#  ruff: noqa` | yes | **SKIPPED** |
| `#   ruff: noqa` | yes | **SKIPPED** |
| `#\truff: noqa` | yes | **SKIPPED** |
| `# ruff: noqa: F821` | yes | probed |
| `#  ruff: noqa: F821` | yes | **SKIPPED** |
| `# flake8: noqa` | yes | probed |
| `#  flake8: noqa` | yes | **SKIPPED** |

Exhaustive sweep of 25 candidate spellings found **six** forms that silence
Ruff file-wide. Only the ones with a single space after `#` pass the gate.

## Coverage is also narrower than it looks

The benchmark carries 131 comments. `maybe_directive()` probes **1** of them
(`# noqa: BLE001 - an unreadable source is not evidence`, line 158); the other
130 are skipped. So on today's file the row rests on a single probed comment,
and the gate is what decides whether anything is probed at all. The cost
saving is real (~130 subprocesses) but the short-circuit is the wrong
trade: it converts an exhaustive check into a prefix match that is already
demonstrably unsound.

## Non-defects checked while here

- `# type: ignore`, `# nosec`, `# pylint: disable=all`, `#  noqa` (no keyword),
  `# noqa:`, `## noqa`, `# NOQA` do **not** silence Ruff. So `# type: ignore`
  is *not* a bypass of this row; it is only a mypy-level construct. The earlier
  suspicion in the review brief about `# type: ignore` is refuted by measurement.
- `_reports_code` failing closed on non-list JSON and on unparseable output is
  correct, and the `invalid-syntax` precondition is now a real code match
  rather than a substring match.

## Remedy

Replace the prefix gate with a real normalization: strip the leading `#` and any
run of whitespace, then match the directive keyword set. Do not enumerate
whitespace variants. Re-verify by planting `#  ruff: noqa` and confirming the
row goes red.

## Status

#566 remains **not independently reviewed** and therefore **not mergeable**.
This defect is on the current head `16d102e` and must be fixed on the branch.
Separately, issue #570 (the guard pins `_BENCHMARK`, so it protects one file and
not the lane) is unchanged and still must land.
