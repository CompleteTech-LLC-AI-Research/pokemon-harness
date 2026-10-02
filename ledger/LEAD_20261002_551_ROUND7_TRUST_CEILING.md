# #551 round 7: the trust checks cannot be closed against an in-process attacker

Date: 2026-10-02
Branch: `lead/551-combine-553`
Round-6 reviewed head: `dcd44e0`
Status: **release remains PARTIAL. #551 is NOT merged.**

Both round-6 reviews returned REJECT. One found a **false refusal** the round-6
fix had introduced — a real bug, now fixed. The other two findings were a
false PASS that cannot be closed the way the last five rounds have been trying
to close it, and this entry records why, because the reason is the finding.

## Fixed this round: the algorithm check was a false refusal

Round 6 made `_record_digests` skip every row whose algorithm was not
`sha256`, to stop `sha512=<a sha256 digest>` laundering a digest that was
never computed under the label it carried. That closed the laundering route
and broke real installs at the same time:

| RECORD algorithm, digest genuinely computed under it | round 6 | now |
|---|---|---|
| `sha256` | `True` | `True` |
| `sha512` | `False` | `True` |
| `blake2b` | `False` | `True` |
| `md5` | `False` | `True` |
| `sha512=<a sha256 digest>` | `False` | `False` |
| `notanalgorithm=...` | `False` | `False` |
| empty hash field | `False` | `False` |

`RECORD` permits any algorithm `hashlib` guarantees and the wheel
specification allows SHA-256 *or stronger*, so refusing every non-SHA-256 row
refuses an honest install — a false red in the release lane, caused by the fix
rather than by the environment.

The label is now honoured instead of either trusted or discarded: each claim
is stored as an `(algorithm, digest)` pair and the digest is recomputed with
the algorithm the record actually names. A genuine SHA-512 row attests; a
SHA-256 digest wearing a `sha512` label does not; an algorithm this
interpreter cannot compute attests nothing.

## Corrected: the CSV mutation claim was wrong

Round 6 recorded that its oversized-`RECORD` row killed a mutant narrowing
`except BaseException` to `except Exception`. It does not:
`issubclass(csv.Error, Exception)` is `True`, so the narrower handler catches
the same error. The row kills *removal* of the guard, and what actually fixes
the escape is materialising the parse — the original bug was that
`csv.reader` is lazy, so the parse happened inside the caller's loop, outside
the guard entirely. The ledger in round 6 said otherwise and is corrected
there.

## Not fixed: two false PASSes with no filesystem write

Both were reproduced independently on `dcd44e0` and confirmed here.

**A. Rebinding a helper the genuine `find_spec` calls.** `_has_frozen_find_spec`
verifies the code of `find_spec`. `PathFinder.find_spec` delegates its work to
`_get_spec`, and replacing only that helper leaves the checked code genuine:

    trusted (find_spec still genuine): True
    guard status: PASS
    foreign submodule loaded: foreign
    *** FULL BYPASS via helper mutation ***

**B. Steering the source path the code is verified against.**
`_frozen_module_file` resolves the frozen module through `sys.modules` and
trusts its `__file__` — ordinary mutable state — to choose the file that
`_code_is_defined_in` recompiles. Pointing that `__file__` at a file the
attacker wrote, and compiling a `find_spec` with a copied fingerprint, makes
the verification compare the attacker's code against the attacker's file.

## Why neither is closed here, and why the next round should not try

Both attacks work by mutating in-process state that the guard reads. That is
not a bug in either fix; it is the shape of the problem. The chain so far is

    __file__  ->  __spec__  ->  code fingerprint  ->  disk RECORD / .pth

and every link was replaced because the previous link was a claim a running
process can rewrite. Round 6 moved the claim from `find_spec`'s code to the
source file that code is verified against, and that file was reached through
`sys.modules`, which is the same kind of claim one level up.

The measurement that decides it:

    *** ANY in-process code can force a false PASS ***

    class Sneaky:
        @classmethod
        def find_spec(cls, name, path=None, target=None): ...

    sys.meta_path.insert(0, Sneaky)
    check_import_origins._is_installation_finder = lambda f: True
    check_import_origins._is_trusted_stdlib_finder = lambda f: True
    # -> guard status: PASS, foreign submodule loaded: foreign

Two module-level predicates are rebound and the guard reports a clean `PASS`
while serving a foreign submodule. No `__spec__` forgery, no `.pth`, no
`RECORD`, no code fingerprint, no stdlib or site-packages write — none of the
provenance machinery is involved, because the code that *asks the question* was
replaced. Round 7 reproduced this on the committed head
(`evidence/repro_predicate_patch.py`).

So a per-attribute check on `find_spec`, on its helpers, or on the path its
code is verified against cannot be made sound against an attacker holding
arbitrary in-process execution. It can only be made harder, and each round has
paid for "harder" with a new false PASS found by the next reviewer. Adding a
fourth witness — checking `_get_spec` too, or pinning `sys.modules` — would
move the goalpost one attribute along and be reported as a blocker next round.

This is not an argument that the guard is useless. Measured on the same head,
an attacker who arranges `sys.meta_path` and **leaves the guard alone** is
caught every time:

    guard status (guard untouched): FAIL
    detail: untrusted sys.meta_path finder(s) can import a checked package's
            submodules from outside this checkout

That is the threat the guard exists for, and it holds. What cannot hold is any
claim that the *provenance* checks are sound against code already executing in
the interpreter, because that code can rewrite the question.

The honest move is to state the boundary in the module rather than keep
spending rounds on an undefendable claim. Concretely, for whoever picks this
up:

- do not add a check that `_get_spec` is genuine, or that pins
  `sys.modules['importlib._bootstrap_external']`, or that hashes the stdlib
  source. Each is forgeable by the same in-process code and will be reported
  as a blocker, correctly, in round 8.
- the guard's contract should read: *an interpreter whose meta-path and
  checked packages are untouched is refused; an interpreter already running
  attacker code is out of scope.* That is defensible, testable, and matches
  what the code actually does.
- the planted-`.pth` gap is a **different** finding and remains open on its
  own terms: it needs a write into site-packages and does not assume in-process
  execution. Rounds 5 and 6 failed to close it for reasons recorded in
  `LEAD_20261002_551_ROUND5_PLANTED_PTH.md`, and that failure is not evidence
  for the ceiling described here.

## Verification on the round-7 tree

| Check | Result |
|---|---|
| `pytest tests/test_import_origin_guard.py` | 123 passed |
| `pytest tests/test_production_gate_*.py tests/test_local_ci_policy.py tests/test_fixture_provenance.py` | 189 passed |
| Clean CLI, `--package pokered_harness` | PASS, rc 0, empty stderr |
| Genuine `BuiltinImporter` / `FrozenImporter` / `PathFinder` / `_virtualenv._Finder` / editable shim | trusted |
| `RECORD` genuine sha256 / sha512 / sha384 / blake2b / md5 rows | attested |
| `RECORD` laundered, unknown-algorithm and empty-hash rows | refused |

## Position

Two round-7 changes are in: the algorithm false refusal is fixed, and the CSV
mutation claim is corrected. Two false PASSes are **not** fixed, because the
fix cannot be made at this layer, and the measurement above is the reason.

`#551` is not merged, and it must not be, on this head. Release remains
PARTIAL.
