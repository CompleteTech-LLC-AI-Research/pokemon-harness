# BLOCKING finding — PR #556 head `c34be0b`: `_code_matches_source` omits `co_consts`

**Verdict: REQUEST CHANGES.** Do not merge head `c34be0b2facd79c733483ec87daf0b9bc7c6b862`.

This is the lead's own verification. It does **not** discharge the independent-review
requirement (#489); it is recorded because it reproduces a **full false PASS on the
exact tree the PR proposes to merge**.

## The defect

`_code_matches_source` (`scripts/check_import_origins.py:205-238`) corroborates a
finder's `co_filename` by recompiling the named file and requiring a code object
matching on **four** fields:

```python
if (
    candidate.co_name == target.co_name
    and candidate.co_code == target.co_code
    and candidate.co_names == target.co_names
    and candidate.co_varnames == target.co_varnames
):
    return True
```

`co_consts` is **not** compared. CPython addresses constants by *index*, not by
value, so the marshalled `co_code` is **byte-identical** when two functions differ
only in what their constants *are*. The four compared fields are therefore
insufficient: any hostile `find_spec` whose bytecode *shape* matches a genuine
`find_spec` already present in a real site-packages file is corroborated as genuine,
while serving a different path.

## Reproduction

Probe: `/workspace/poke-harness/.scratch/lead556_probe/const_bypass4.py`
Root-cause confirmation: `.../confirm_root_cause.py`

    cd /workspace/poke-harness
    .scratch/lead556venv/bin/python \
        .scratch/lead556_probe/const_bypass4.py \
        /workspace/poke-harness/.scratch/lead556

Merge tree under test: `git merge-tree --write-tree origin/master c34be0b` ->
tree `48bef2c`, materialised as commit `dbbf2af`, worktree
`/workspace/poke-harness/.scratch/lead556`, venv `/workspace/poke-harness/.scratch/lead556venv`.

Output:

    co_name match    : True
    co_code identical: True
    co_names match   : True
    co_varnames match: True
    co_consts DIFFER : True  <-- not compared
      genuine consts: ['None', "'pokered_harness.stolen'", '0', "'/x/x/x/x/x/x/x/x/x/x/x/x/"]
      hostile consts: ['None', "'pokered_harness.stolen'", '0', "'/home/agent/.cache/openco"]

    *** hostile finder TRUSTED: True
    guard status under attack: PASS
    FOREIGN SUBMODULE LOADED: foreign-submodule
      from: /home/agent/.cache/opencode-tmp/cb_zrmygpdh/pokered_harness/stolen.py
      outside checkout: True
    VERDICT: FALSE GREEN -- FULL BYPASS ON #556

The guard reports **PASS** while `pokered_harness.stolen` loads from a temp
directory outside the checkout. This is the exact failure mode the whole #534
guard stack exists to prevent, and it survives #556's repair intact.

Note the attack does **not** require writing into site-packages at the moment of
attack time beyond creating one ordinary `.py` file there; and it does not need to
know the genuine file's contents in advance — it only needs *some* real
site-packages file to define a `find_spec` of a given bytecode shape. The
attacker-created file's source supplies the shape; the constant supplies the path.

## Root cause isolated

`.../confirm_root_cause.py` patches **only** the comparison to include `co_consts`
(nested code objects normalised to their `co_code`), changing nothing else:

    AS-SHIPPED  _is_installation_finder : True
    WITH co_consts comparison        : False
    GENUINE editable finder as-shipped: True
    GENUINE editable finder + consts  : True

So `co_consts` is precisely the missing discriminator, and adding it is
**sufficient** and introduces **no false red** against the genuine editable
installer and the distutils helper in this venv.

## Why the existing rows miss it

Every forgery row in `tests/test_import_origin_guard.py` builds a hostile
`find_spec` whose bytecode has a *different shape* from anything in a real
site-packages file, so `co_code` alone separates them and the missing field never
matters. No row compares a hostile function against a genuine same-shape twin.

## Regression context (what #556 got right)

Measured on the same tree and venv, so these are controls, not claims:

- Guard + fixture-provenance suites: **117 tests, 0 failures, 0 errors, 0 skipped**
  (JUnit XML; the repo prints no summary line).
- Guard suite alone: **100 tests, 0 failures**.
- The original forged-`co_filename` attack, with the foreign submodule *not* on
  `sys.path`:
  - #555 head `89c7152`: forged finder trusted `True`, status **PASS**, foreign
    submodule loaded → **FALSE GREEN**.
  - #556 head `c34be0b`: forged finder trusted `False`, status **FAIL**,
    `<interpreter>` finding names the hostile finder → **ATTACK DEFEATED**.
    The repair does close the finding #556 was written for.
- Mutation matrix (guard suite, JUnit counts):
  - M1 corroboration neutered -> **KILLED** (1 failure)
  - M2 containment check removed -> **KILLED** (5 failures)
  - M3 empty-package fail-closed neutered -> **KILLED** (1 failure)
  - baseline restored -> 100/0/0
- False-red check with a **real** `pip install -e` of the merge tree: the
  `__editable___pokered_harness_0_1_0_finder` and `_distutils_hack.DistutilsMetaFinder`
  are both still `trusted=True`, untrusted list empty, `check_origins` **PASS**.

  (A stale `.pth` in the verification venv initially produced a *second*,
  unrelated FAIL. That was the lead's own contamination, removed before these
  numbers; recorded so nobody re-derives it.)

## What would close it

Compare constants as well, recursing into nested code objects so an inner
`find_spec`'s constants count:

```python
def _const_signature(code):
    return tuple(
        item.co_code if isinstance(item, types.CodeType) else (type(item).__name__, repr(item))
        for item in code.co_consts
    )
```

and require `candidate.co_consts == target.co_consts` **or** the recursive
signature to be equal. Then add a row that pins the refusal: a hostile `find_spec`
compiled under a genuine same-shape `find_spec`'s filename, with a foreign path
constant. Add the mirror row so the extra strictness cannot start refusing real
installers.

Note that constant comparison alone is still not a complete proof of provenance —
it closes this shape, not the class. The honest residual statement in the docstring
should say so.

## Constraints

Same as #555's finding: the repair must land on a new head on top of `c34be0b`,
and that head needs its own independent review. Nothing here waives #556's
existing verification — the 117/0 suite, the M1-M3 mutation matrix, the
false-red check, and the confirmed defeat of the original forged-`co_filename`
attack all stand as measured above.
