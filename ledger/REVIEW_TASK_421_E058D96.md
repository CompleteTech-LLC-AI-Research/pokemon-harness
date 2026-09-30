# Review task — PR #421, head `e058d96b`, the #420 fix

Independent review. Produce findings; do not approve on prior reasoning.

## Paths
- PR: https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/421
- Issue: #420
- Read-only worktree already prepared: `/home/agent/poke-harness/.scratch/v421new` at `e058d96b`
- Parent (defective) tree for differential: `/home/agent/poke-harness/.scratch/v421old` at `165b5b3d`
- Interpreter: `/home/agent/poke-harness/pokemon/.venv/bin/python`

Do NOT edit, branch, commit, push, or merge. Do NOT touch `/home/agent/poke-harness/pokemon`
(protected, has staged files). Probes in `/tmp` only.

## Context already established (verify, don't trust)
A prior blocking finding was `TypeError: argument of type 'bool' is not iterable`, from
`return name in _starred_names_in_loop_target(statement, name)` where the helper already
returns `bool`. Commit `e058d96b` changed it to return the bool directly. The lead has
re-verified the TypeError reproduces at `165b5b3d` and is gone at `e058d96b`, and that the
new verdict matches executed CPython.

## Your job
1. Confirm the fix is complete and correct: does any other call site of
   `_starred_names_in_loop_target` still misuse its return value? grep the whole module,
   including the other call at `e058d96b:2871`.
2. Confirm the fix is not over-broad. The CONTROL `for cs in (1, contextlib.nullcontext()):`
   then `with cs:` must still read `True` (the loop target is a real context manager and the
   assert really fires).
3. Confirm #423 is NOT fixed by this PR and stays open: a starred store in a loop body with
   the `with` as a LATER SIBLING statement must still read `True` where CPython raises.
4. Mutation-check: revert the one-line change and confirm a test catches it. Restore the file
   afterwards; do not leave the worktree dirty.
5. Run the sentinel + milestone suites and report counts from the JUnit XML:
   `cd /home/agent/poke-harness/.scratch/v421new && /home/agent/poke-harness/pokemon/.venv/bin/python -m pytest tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py -q --junitxml=/tmp/rev421.xml`
6. `ruff check tests/` and `ruff format --check tests/`.

## Ground-truth rule
`_is_enforced(...) == True` means the assert can fail on at least one executed argument path;
`== False` means it can never fail. Tool `False` while CPython fires = FALSE-DEAD (damaging).
Tool `True` while CPython raises `TypeError` = FALSE-LIVE (safe). **Derive every ground truth
by EXECUTING a fixture, never by reading the analyzer.** A static table produced a confident
wrong answer during this investigation.

## Output
Write `/home/agent/poke-harness/.scratch/fix429/ledger/REVIEW_421_E058D96_INDEP.md`, then
reply: verdict (APPROVE / REQUEST CHANGES), each finding with file:line, whether pre-existing
or introduced, and executed evidence.
