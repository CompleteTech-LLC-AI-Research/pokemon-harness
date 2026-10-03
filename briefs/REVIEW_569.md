# Independent review brief — PR #569 (and #568, which it stacks on)

You are an **independent reviewer**. Do not trust the author's claims.
Re-derive everything yourself with your own commands.

## Ground rules (violating any of these invalidates your review)

1. **Never reuse, copy, or symlink a virtualenv between worktrees.** This repo
   refuses pytest collection across checkouts. Each worktree needs its own venv.
2. **Always run Ruff with `--no-cache`.** A concurrent full pytest suite sharing
   a worktree's `.ruff_cache` will corrupt it and produce a *false* finding.
   This already caused one retracted finding in this PR's history.
3. **Do not run Ruff concurrently with a full pytest suite in the same worktree.**
4. If two runs of the same command on the same commit disagree, that is an
   **environment fault, not a code finding.** Say so explicitly.
5. You are reviewing only. Do not push, merge, or modify the branch.

## Repos and revisions

- Main checkout: `/home/agent/vpkg` (do not modify its working tree)
- `origin/master` = `b5302d091e5c809bd343f0f15996b8a47bdb4d3d`
- PR #568 head = `1cc86f88172494a99536b61ab3ab17c80faab7c9` (`fix/567-scripts-format-clean`)
- PR #569 head = `25607fce076e9c47b63bc1102f253ec2fb974659` (`fix/567-scripts-dir-lane`)
- `1cc86f8` is an **ancestor** of `25607fc`. #569 is stacked on #568, so
  **#568 must merge before #569**, or #569 will conflict.

If any head has moved when you start, re-derive it with
`gh pr view 569 --json headRefOid` and review the new head, and say which head
you actually reviewed.

Set up your own worktrees:

    cd /home/agent/vpkg
    git worktree add /home/agent/rv569_$$ origin/fix/567-scripts-dir-lane
    git worktree add /home/agent/rv568_$$ origin/fix/567-scripts-format-clean

## What #568 claims (39 reformats + 1 semantic file)

- 40 files changed.
- 39 are **AST-identical** to master — pure `ruff format` output.
- Only `scripts/check_pyboy_components.py` differs semantically, via four
  lint-driven edits:
  - late-binding default argument
  - two `next(...)` rewrites
  - combined `with`
  - executable mode change

Verify independently. Which files are NOT AST-identical to master:

    cd /home/agent/rv568_$$
    git diff --name-only b5302d0...HEAD > /tmp/f568.txt
    while read -r f; do
      case "$f" in
        *.py)
          old=$(git show "b5302d0:$f" 2>/dev/null) || continue
          a=$(printf '%s' "$old" | python3 -c 'import ast,sys;print(ast.dump(ast.parse(sys.stdin.read())))' 2>/dev/null)
          b=$(python3 -c "import ast,sys;print(ast.dump(ast.parse(open('$f').read())))" 2>/dev/null)
          [ "$a" != "$b" ] && echo "SEMANTIC CHANGE: $f" ;;
      esac
    done < /tmp/f568.txt

The claim is that the only output is `scripts/check_pyboy_components.py`. If any
other file appears, that is a finding — a reformat claimed to be
behavior-preserving is not.

Also confirm both master and the head pass the component tests, and that the
executable-mode change is real and intended:

    git diff b5302d0...HEAD -- scripts/check_pyboy_components.py
    ls -l scripts/check_pyboy_components.py

## What #569 claims (directory token replaces a 51-entry list)

The old lane enumerated 51 script paths by literal. #569 replaces that with the
`scripts` directory token, so the lane must resolve **113** files. The author's
coverage-preservation argument (independent measurement, Ruff 0.16.5):

- master's 51 enumerated paths resolve to **51** files
- the branch's `scripts` token resolves to **113** files
- `comm -23 master branch` is **empty** — nothing dropped
- the 62 newly covered files all pass `check` and `format`

Re-derive all four, with your own Ruff. Any dropped path is a **blocking**
finding — a silently unlinted file is exactly the regression this PR exists to
prevent.

### The real regression this PR fixed

Commit `11eb719` broke CI. The failing row was
`tests/test_stepping_loop_profile.py::test_probe_module_and_its_tier_are_part_of_the_ci_contract`
(`assert 0 >= 2`). The row counted **literal occurrences** of the path
`scripts/stepping_loop_profile.py`; the directory token legitimately removes that
literal while coverage stays real. Commit `25607fc` replaced literal counting
with real Ruff token resolution.

Confirm the row is now non-vacuous. These mutations must each turn it **red**:

- delete the `scripts` token from the lane
- exclude the probe so it stops being covered

    python3 -m pytest tests/test_stepping_loop_profile.py -q
    python3 -m pytest tests/test_stepping_loop_profile.py tests/test_local_ci_policy.py -q
    ruff check scripts tests --no-cache
    ruff format --check scripts tests   # author reports 448 files formatted

## Producer carve-out — must be exactly as described

`scripts/produce_battle_state_fixtures.py` is deliberately excluded
(`force-exclude = true`), left unformatted, because its output is pinned as
release evidence. It must be **byte-identical** to master and its SHA-1 must
match the value recorded in `release-evidence/battle-scenarios.json`:

    git diff b5302d0...HEAD -- scripts/produce_battle_state_fixtures.py  # expect EMPTY
    sha1sum scripts/produce_battle_state_fixtures.py                    # expect e2e9676594415148293909087bbb02863ed0db49
    grep -n 'produce_battle_state_fixtures' release-evidence/battle-scenarios.json

Any drift here is a **blocking** finding: it would break the provenance chain
between the fixture manifest and the checked-in evidence.

## CI

    gh pr checks 569

Author's numbers: run `37157467035` on head `25607fc` = **8314 passed, 0 failed**
in the native/unit tier, versus run `37148200858` on old head `11eb719`
= 8313 passed, 1 failed. Verify these are real runs on those exact SHAs, and
that the pass count genuinely went up rather than a test having been deleted.

## Deliverable

Report **APPROVE** or **REJECT** with:

- the exact head SHA(s) you reviewed
- your independent numbers for each check above
- every finding, with severity (blocking / non-blocking)
- confirmation that the producer file is untouched and CI is green on the head

If you approve **both** #568 and #569, state clearly that #568 must merge first.
