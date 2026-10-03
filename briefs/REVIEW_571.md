# Independent review brief — PR #571 (the #566 fix)

You are an **independent reviewer**. Do not trust the author's claims — the
author found this bug themselves, so in particular verify the fix rather than
confirming it.

## Ground rules (violating any of these invalidates your review)

1. **Never reuse, copy, or symlink a virtualenv between worktrees.** This repo
   refuses pytest collection across checkouts. Create your own venv.
2. **Always run Ruff with `--no-cache`.**
3. **Do not run Ruff concurrently with a full pytest suite in the same worktree.**
4. If two runs of the same command on the same commit disagree, that is an
   **environment fault, not a code finding.** Say so explicitly.
5. Reviewing only. Do not push, merge, or modify the branch.

## Revisions

- `origin/master` = `b5302d091e5c809bd343f0f15996b8a47bdb4d3d`
- PR #571 head = `fix/106-directive-normalization`, expected `23333c8`
- Its base is PR #566's head `53b6a97a`. #566 itself is **rejected** and still
  open; #571 is a candidate replacement for #566's head.

Confirm the actual head with:

    cd /home/agent/vpkg
    gh pr view 571 --json headRefOid,baseRefName,isDraft,mergeable
    git worktree add /home/agent/rv571_$$ origin/fix/106-directive-normalization

## The defect being fixed

`tests/test_local_ci_policy.py` gained a `maybe_directive()` short-circuit on
head `16d102e` that matched a comment against a tuple of ten literal prefixes.
Ruff accepts any run of whitespace between the comment marker and the directive
keyword, so `#  ruff: noqa` (two spaces), `#   ruff: noqa` (three), and
`#\truff: noqa` (tab) silence an entire file while the gate returned `False` and
the comment was never probed. The guarding row then passed on a benchmark that
no lint rule could see.

Author's claim: this is a **regression**, because the pre-`16d102e` row rejects
the two-space spelling.

## Verify these claims independently

**1. The fix matches the normalized form.** Expected:

    directive_keyword = re.compile(r"\A#+\s*(?:ruff|flake8|fmt|yapf|noqa)\b", re.IGNORECASE)
    def maybe_directive(comment: str) -> bool:
        return directive_keyword.match(comment.strip()) is not None

**2. The mutation matrix.** Build each mutant by prepending a single line to the
real `scripts/benchmark_matrix_concurrency.py` — use Python or `printf '%b'` so
tabs are real bytes, **not** a literal backslash-t. A shell `printf '#\truff'`
under some shells emits a literal `\t` and will make you wrongly conclude the tab
form does not silence Ruff.

Author's matrix (row = `test_benchmark_cannot_opt_out_of_linting_with_in_file_suppression`):

| prepended line | Ruff silences file | row |
|---|---|---|
| `# ordinary prose comment` | no | passes |
| `# ruff: noqa` | yes | fails |
| `#  ruff: noqa` | yes | fails |
| `#   ruff: noqa` | yes | fails |
| `#<TAB>ruff: noqa` | yes | fails |
| `#  flake8: noqa` | yes | fails |
| `# type: ignore` | no | passes |

Re-derive all seven. Also confirm the real benchmark file is **restored clean**
afterward — `git diff -- scripts/benchmark_matrix_concurrency.py` must be empty.

**3. The claimed non-bypasses.** Verify `# type: ignore`, `# nosec`,
`# pylint: disable=all`, `# noqa` without a code list, and `## noqa` do **not**
silence Ruff, so they are correctly allowed through.

**4. No new over-broad false positive.** The regex now matches some comments
that do not silence anything (e.g. `#  noqa` with no code list). Confirm this
only costs a subprocess and cannot turn the row red on a legitimate file.

**5. Suites and lint.**

    cd /home/agent/rv571_$$
    python3 -m venv .venv && .venv/bin/pip install -e . pytest pytest-asyncio ruff
    .venv/bin/python -m pytest tests/test_local_ci_policy.py tests/test_stepping_loop_profile.py -p no:cacheprovider
    .venv/bin/python -m ruff check tests/test_local_ci_policy.py --no-cache
    .venv/bin/python -m ruff format --check tests/test_local_ci_policy.py

Author reports 45 passed, lint clean, format clean.

**6. Diff scope.** Expected: `tests/test_local_ci_policy.py` only — one import,
one matcher, its comment. Anything else is a finding.

## Methodological warnings

- **Per-file Ruff directives are honored only for real files, not
  `--stdin-filename`.** Demonstrating this class of bypass requires planting the
  mutation in the actual benchmark file. A stdin-only probe cannot prove it.
- Judge "does Ruff silence the file" by reading Ruff's **output**, not by
  inverting its exit code carelessly: `ruff check` exits 0 both when it finds
  nothing and when everything is suppressed. Compare against a control file with
  no directive.
- Do not treat the exit code of a `grep` in a pipeline as the result of the
  command being measured.

## Scope discipline

Merging #571 does **not** close issue #106. Controlled benchmark / real-ROM
qualification remains outstanding and real-ROM evidence may not be waived.
Issue #570 — the guard pins `_BENCHMARK`, so it protects one file rather than
the whole lane — is unchanged and must still land. Confirm the PR body does not
claim otherwise.

## Deliverable

Report **APPROVE** or **REJECT** with:
- the exact head SHA reviewed
- your independently measured mutation matrix
- every finding, marked blocking or non-blocking
- explicit confirmation that the benchmark file was left clean
