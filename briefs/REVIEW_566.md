# Independent review brief — PR #566

You are an **independent reviewer**. Do not trust the author's claims.
Re-derive everything yourself with your own commands.

## Ground rules (violating any of these invalidates your review)

1. **Never reuse, copy, or symlink a virtualenv between worktrees.** This repo
   refuses pytest collection across checkouts. Each worktree needs its own venv.
2. **Always run Ruff with `--no-cache`.** A concurrent full pytest suite sharing
   a worktree's `.ruff_cache` will corrupt it and produce a *false* finding.
3. **Do not run Ruff concurrently with a full pytest suite in the same worktree.**
4. If two runs of the same command on the same commit disagree, that is an
   **environment fault, not a code finding.** Say so explicitly.
5. You are reviewing only. Do not push, merge, or modify the branch.

## Revision under review

- `origin/master` = `b5302d091e5c809bd343f0f15996b8a47bdb4d3d`
- PR #566 head = `16d102e2308de3c57a09f392ff472ab771a480dd` (`fix/106-benchmark-ci-lanes`)

**This head moved during review.** The previously-reviewed head was `fceb11c3`.
Any verdict recorded against `fceb11c3` does **not** cover `16d102e`.

    cd /home/agent/vpkg
    gh pr view 566 --json headRefOid,isDraft,mergeStateStatus
    git worktree add /home/agent/rv566_$$ origin/fix/106-benchmark-ci-lanes

## What #566 is for

Issue #106: the #564 benchmark harness was not being linted by either CI ruff
lane. The PR adds lint policy rows in `tests/test_local_ci_policy.py` that fail
closed when the benchmark goes unmeasured.

## Claims to verify on head 16d102e

1. The 124 focused policy/benchmark tests pass.
2. Two previously-vacuous paths now fail closed:
   - an **unparseable** benchmark
   - an **unsupported `--output-format`**, which yields non-JSON
3. Real planted opt-outs turn the row **red** (the escape must be caught).
4. Inert forms remain **accepted** (no false positives).

Start with:

    cd /home/agent/rv566_$$
    python3 -m pytest tests/test_local_ci_policy.py -q
    ruff check . --no-cache

The row of interest is
`test_benchmark_cannot_opt_out_of_linting_with_in_file_suppression`. Its
correctness rests on `silences(...)` and the new `_reports_code(...)` helper.

## The change in 16d102e specifically — scrutinise this hardest

Relative to `fceb11c3`, commit `16d102e` does three things.

**(a) `_reports_code` now fails closed on unrecognised shapes.** It returns
`True` (report) when the parsed JSON is not a list, and when parsing fails.
That is the correct direction — verify no path makes it return `False` for a
run that measured nothing.

**(b) The `invalid-syntax` precondition now uses `--output-format=json` +
`_reports_code(...)`** instead of a substring check. Substring matching on
`invalid-syntax` is fragile; confirm this is now a real code match and that the
precondition cannot pass vacuously if the benchmark stops being lintable.

**(c) A prefix short-circuit was added.** Comment scanning is now gated on
`maybe_directive(comment)`:

    directive_prefixes = (
        "# ruff", "#ruff", "# flake8", "#flake8", "# fmt", "#fmt",
        "# yapf", "#yapf", "# noqa", "#noqa",
    )
    def maybe_directive(comment: str) -> bool:
        return comment.strip().lower().startswith(directive_prefixes)

The author's justification is that probing all ~131 comments through two lanes
costs ~130 subprocesses, so comments Ruff's directive grammar cannot match are
skipped as "ordinary prose".

**This is a vacuity risk and is the most important thing for you to check.**
The comment claims "Every spelling measured above starts with one of these
tokens." A hand-written prefix list is a *reimplementation* of Ruff's grammar,
and the row's whole purpose is to be the backstop against exactly that class of
reimplementation drifting out of sync.

Determine, empirically and independently:

- Does **any** comment in the benchmark that `silences()` would treat as a live
  escape get skipped by `maybe_directive()`? If yes, that is a **blocking**
  finding: the row has a blind spot that is invisible in the green run.
- What happens if a directive is written as `# type: ignore` or with leading
  whitespace, a nonstandard spelling, or a directive form absent from the
  tuple? Does the row still catch it?
- Is the tuple complete against the actual forms the tests enumerate in
  `lint_directives` / `_FORMAT_PROBES`? Cross-check the tuple against those
  literals directly rather than trusting the comment.

A concrete test: plant a real file-level suppression in the benchmark whose
comment text does **not** begin with any entry in `directive_prefixes`, yet
which genuinely silences linting. If the row stays green, the short-circuit has
a hole and must be removed or replaced with a real grammar check.

## Known limitation — already filed as issue #570 (do not re-litigate here)

The in-file suppression guard pins `_BENCHMARK`, so it protects **one file**,
not the whole lint lane. This was independently measured with a clean
per-worktree venv and `--no-cache`: planting a blanket directive plus a real
`F821` on any of

- `scripts/bootstrap_pyboy.py`
- `scripts/production_gate.py`
- `src/pokered_harness/_mcp_facade_entry.py`

makes Ruff exit 0 while the policy suite stays green.

That gap is tracked as **issue #570**. Your job is to confirm whether `16d102e`
makes the measured scope **worse**, and to review #566 on its actual stated
scope. Do not reject #566 solely for the #570 gap — but do state plainly in
your verdict that #566 does not close the lane, so #570 must land.

## Scope discipline

**Merging #566 does not close issue #106.** Controlled benchmark / real-ROM
qualification remains outstanding, and real-ROM evidence may not be waived.
Confirm the PR does not itself assert closure of #106.

## Deliverable

Report **APPROVE** or **REJECT** with:

- the exact head SHA you reviewed
- your independent test counts
- your independent verdict on the `maybe_directive` short-circuit (with the
  evidence for it)
- every finding, with severity (blocking / non-blocking)
- confirmation of whether the row is non-vacuous under mutation
