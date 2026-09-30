# Review task — PR #433, head `39f1b19`, the #429 repair

You did not author this. Produce **findings**; do not approve on the author's
reasoning. The lead merges.

## Paths

- Repo: `/home/agent/poke-harness/pokemon`
- Worktree, **read-only**: `/home/agent/poke-harness/.scratch/fix429`, HEAD `39f1b19`
- Interpreter: `/home/agent/poke-harness/pokemon/.venv/bin/python`
- PR: https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/pull/433

Do not edit, branch, commit, push, or merge. Do not touch
`/home/agent/poke-harness/pokemon` — it is protected and has staged files.
Throwaway probes go in `/tmp`, never in the repo.

## What the change claims

`elif True:` was treated as an always-running branch, so a store in it settled
a name unconditionally. The header was then reported DEAD where CPython enters
it — a **false-dead**, the damaging direction.

## Verify

1. Read the diff: `git -C /home/agent/poke-harness/.scratch/fix429 show 39f1b19`
2. The regression shape — expected **live** (`True`):
   ```python
   import contextlib
   def outer(x):
       cs = contextlib.nullcontext()
       if x: pass
       elif True: cs = list()
       with cs:
           assert x != 1
   ```
   With `x = 1` the `if x:` arm runs, `cs` is still the `nullcontext()`,
   `with cs:` enters, `assert x != 1` FIRES. Execute it to confirm, do not
   infer.
3. **Adversarially probe the new helper** `_elif_link_is_conditional`. Try to
   find an input where it is wrong in either direction. Ideas worth trying:
   - `else:` (not `elif`) storing — an `else` arm is also in `orelse`
   - a three-link chain `if / elif / elif`
   - `if True: ... elif True: ...` (first link true, so the later link is
     actually *unreachable*)
   - a `while`/`for` whose `orelse` contains an `If`
   - a store in an `elif` arm where the earlier test is a literal `False`
     (so the arm IS unconditional)
   - nested `def` inside an `elif` arm
   - a comprehension or lambda inside the arm
4. Confirm the **first-link `if True:` control is unchanged**. This is the
   regression risk in the other direction: over-broad fixes that make every
   nested `if True:` conditional would break the module-scope shape
   `b90f985` was written to fix.
5. Run the full suite on the exact head and report counts from the JUnit XML:
   ```
   cd /home/agent/poke-harness/.scratch/fix429
   /home/agent/poke-harness/pokemon/.venv/bin/python -m pytest \
     tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py \
     -q --junitxml=/tmp/rev429.xml
   ```
6. Mutation-check: remove the `if _elif_link_is_conditional(...)` guard from
   `_is_always_true_branch` and confirm the new rows fail. Restore the file
   afterwards (or work in a copy — do not leave the worktree dirty).
7. Confirm `ruff check tests/` and `ruff format --check tests/` are clean.
8. Confirm the new rows are **executed** against CPython by
   `_assert_entry_contract` rather than asserted from a static table.

## Scope check

This PR claims to fix #429 only. Verify it does **not** silently change the
answers for #423, #425 or #417. These should still report their filed
(failing) values on this tree:

- #423: starred store in a loop body, `with cs:` a later sibling → still `True` (still a false-live)
- #425: `for cs in (1, None): pass` then `with cs:` → still `True` (still a false-live)
- #425 control: `for cs in (1, contextlib.nullcontext()):` → still `True` (correct)

A PR that quietly fixed those is fine but must not *claim* it without rows
pinning them.

## Known remaining defects — OUT OF SCOPE, verify they are pre-existing

The lead measured these by execution on CPython 3.12.14. They are
**false-LIVE** (tool says live, CPython raises `TypeError`), i.e. the safe
direction, and they are **not** caused by this PR. Confirm each one also
misbehaves on the parent commit `63ab8dd`:

```python
import contextlib
def o(x):
    cs = contextlib.nullcontext()
    if False: pass
    elif True: cs = list()      # unconditional arm
    with cs:
        assert x != 1           # CPython: TypeError -> DEAD
```

Also the same family: `if False: / elif False: / elif True: STORE`, and
`if False: pass / else: STORE`. Also on `master` and on #421, but NOT on
#412's head: `if True: cs = list()` as a **first** link, `for/else` with a
non-empty iterable, and `while False: else:`.

Do **not** report these as blockers for this PR. Report them as separate
findings so the lead can decide whether to file them.
