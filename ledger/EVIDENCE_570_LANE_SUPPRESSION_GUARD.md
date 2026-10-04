# #570 lane-wide file-level suppression guard — measured evidence

Date: 2026-10-04
Base: `a413eeb5ba4bc8e5407fc31cd84c31efe855f314` (master, PR #566 merge)
Worktree: `/workspace/poke-harness/.scratch/wt570-guard`
Branch: `fix/570-lane-suppression-guard`
Changed file: `tests/test_local_ci_policy.py` only

**This is the author's own measurement, not an independent review.** The
independent-review bar for this change is not met by this document.

## The hole being closed

Every probe in `tests/test_local_ci_policy.py` lints a snippet on stdin with
`--stdin-filename`. That is what makes the probes immune to `extend-exclude`,
`per-file-ignores` and a narrowed rule set, because Ruff resolves all three
from the filename and never from the bytes being linted. The mirror-image cost
is that a suppression living in a file's own content is invisible to all of
them: the snippet carries no directive to find.

PR #566 closed that for one file (`_BENCHMARK`). This change covers the lane.

## Measured lane inventory (base `a413eeb5`)

Resolved by asking Ruff itself (`ruff check <lane paths> --show-files`), not by
re-implementing `extend-exclude`:

| quantity | value |
|---|---|
| lane path tokens | 53 |
| resolved files (whole lane) | 386 |
| resolved under `tests/` only | 334 |
| resolved **outside** `tests/` | 52 |
| file-level selective directives | 34 |
| file-level blanket directives | 0 |

The 52 files outside `tests/` are the part the first cut of this change missed:
`_ruff_lint_resolved_files()` defaulted `tree="tests"`, so the row was only
measuring 334 of 386 files while claiming to cover "the lane". The helper now
takes `tree: str | None`, and the new row passes `tree=None`.

### The 34 legitimate selective allowances

| named codes | count | who |
|---|---|---|
| `F821` | 31 | generated `tests/_sentinel_support_part*.py` fragments |
| `F401` | 3 | `_sentinel_support_base.py`, `tests/_tcp_trade_peer.py`, `tests/test_mcp_trade_records_rom.py` |

These are load-bearing. Stripping the directive from
`tests/_sentinel_support_part1.py` surfaces **105 F821s** that the assembled
module supplies, which is why a blanket "no file-level directive" rule would be
wrong rather than merely strict. Measured examples after removing the directive:

| file | codes revealed |
|---|---|
| `tests/_sentinel_support_base.py` | 5 x F401 |
| `tests/_sentinel_support_part1.py` | 105 x F821 |
| `tests/_tcp_trade_peer.py` | 43 x F401 |
| `tests/test_mcp_trade_records_rom.py` | 100 x F401 |

The rule the row enforces is therefore **measured**: a selective directive is
allowed only where removing it reveals every code it names. A directive that
has stopped covering its code fails, rather than sitting in the lane unexamined.

## Directive grammar, measured not assumed

Ruff honours a much narrower grammar than "looks like a directive". Every case
below was measured against `ruff check` on stdin (lint) or `ruff format --diff`
(format), not inferred from the source.

Honoured by `ruff check` (must be detected): bare `noqa`, doubled leading hash,
missing space after the hash, spaces around the colon, leading indentation,
leading `-`, a trailing comment, the `flake8` alias, and hashes interspersed
with whitespace or dashes (`# # ruff: noqa`, `#- # ruff: noqa`).

Inert under `ruff check` (must NOT be flagged): any trailing colon
(`# ruff: noqa:` — Ruff splits on colons, so an empty code list disables
nothing), and an upper-cased value (`# RUFF: NOQA` — the keyword comparison is
case-sensitive).

Honoured by `ruff format`: a leading `fmt: off` or `yapf: disable` stops the
reformat; `#   fmt:   off` does too; `fmt: skip` and a trailing `fmt: on` do
not.

Final pattern characteristics, over 34 spellings:

* **false negatives: 0** — every silencing form Ruff honours is detected.
* **false positives: 1** — `#-#- ruff: noqa`, which Ruff does not honour.
  This errs towards refusing a file Ruff would otherwise lint, which is the
  survivable direction; the opposite error would let a real opt-out through.
* A formatter opt-out is refused **whether or not it is later re-enabled**.
  Measured: given a file already ending in `fmt: off`, code appended after that
  line is left unformatted. A trailing `off` with no matching `on` is not
  bookkeeping, it is an opt-out extending over whatever comes next.

## Correctness fix found by mutation testing

The first version of the leading-prefix pattern was
`[#-]+(?:[ \t]*[#-]+)*`, which is ambiguous — `#` and `# ` can both start the
inner group — and it **backtracked catastrophically** on the ASCII-ruler section
separators this repository uses:

`tests/_battle_item_evidence.py:113` — `# ` followed by 75 dashes

That nested form did not return within 3 s on that comment. The scan appeared
to hang rather than fail: full-suite runs exceeded a 3000 s timeout at 57% with
the process at 88% CPU and no child process, which is the signature of
in-process regex backtracking rather than slowness. The host was also contended
(load 5–14 on 4 cores, with `rustc` and `vitest` from unrelated projects), which
masked the cause.

The shipped form is a single flat group `(?:[#-][ \t]*)+`, which is unambiguous:

```
nested  [#-]+(?:[ \t]*[#-]+)*   ->  did not return in 3s on a 77-char comment
flat    (?:[#-][ \t]*)+         ->  0.004 ms; linear to a 5000-char comment
```

Effect: the full 386-file scan went from *not terminating* to **14.25 s**, and
`tests/test_local_ci_policy.py` went from a >50 min hang to **70 s** for all 20
tests.

## Commands and terminal results

Measured on this branch head, in `.venv-570` (Python 3.11.2, pytest 9.1.1):

| command | result |
|---|---|
| `pytest tests/test_local_ci_policy.py -q` | exit 0, 20 tests, 70 s |
| `pytest tests/test_matrix_concurrency_policy.py -q` | exit 0, 105 tests, 24 s |
| `pytest tests/test_local_ci_policy.py tests/test_matrix_concurrency_policy.py -q` | exit 0, 125 tests |
| main `ruff check` lane, exactly as `scripts/run_local_ci.sh` defines it (53 tokens) | exit 0, `All checks passed!` |
| main `ruff format --check` lane | exit 0, `387 files already formatted` |
| `ruff check tests/test_local_ci_policy.py` | exit 0 |
| `ruff format --check tests/test_local_ci_policy.py` | exit 0 |
| `bash -n scripts/run_local_ci.sh` | exit 0 |
| `git diff --check` | exit 0 |

## Mutation testing

Each case mutates a lane file, runs the new row, and restores. "exp=FAIL" means
the row must refuse the tree; "exp=PASS" means the row must stay green because
Ruff genuinely still lints or formats that file.

| case | mutation | expected | got |
|---|---|---|---|
| base | none | PASS | PASS |
| d1 | `#--ruff: noqa` on `scripts/production_gate.py` | FAIL | FAIL |
| d2 | `#   # ruff: noqa` | FAIL | FAIL |
| d3 | `#- # ruff: noqa` | FAIL | FAIL |
| d4 | `# fmt: off` on a lane `scripts/` file | FAIL | FAIL |
| d5 | `#   fmt:   off` | FAIL | FAIL |
| d6 | `# yapf: disable` | FAIL | FAIL |
| d7 | `    # ruff: noqa` (indented) | FAIL | FAIL |
| i1 | `# ruff: noqa:` (inert: trailing colon) | PASS | PASS |
| i2 | `# RUFF: NOQA` (inert: case) | PASS | PASS |
| i3 | `# noqa` (line-level, not file-level) | PASS | PASS |
| i4 | `# fmt: off` + `# fmt: on` | PASS | FAIL |
| i5 | `# fmt: skip` (inert) | PASS | PASS |
| s1 | `tests/_sentinel_support_part1.py` directive made stale | FAIL | FAIL |

All 14 behaved as expected except **i4**, where the expectation was wrong rather
than the row: the measured behaviour above shows a trailing `off` genuinely
exempts later code, so refusing the pair is correct and i4's "exp=PASS" was an
error in writing the case. The row's behaviour is unchanged; only my
expectation was corrected, and the reasoning is recorded in the row's docstring.

Mutation tests supplement review, they do not replace it. They show the row
reacts correctly to planted changes; they cannot show the row is measuring the
right thing, which is why the inventory above is reported separately.

## Not established here

* No independent review of this head. `ledger/REVIEW_564_MATRIX_CONCURRENCY_09C84F1.md`
  records that sub-agent task delivery is unreliable in this environment (see
  #489). A fresh independent review of the exact pushed head is still required
  before merge.
* No hosted CI result. The repository's private workflow is skipped and an
  empty check rollup is not a pass; the local lanes above are the evidence.
* This does not touch #106. The CPU-budget qualification still cannot run
  without an operator-declared allocation, so #106 stays open.
