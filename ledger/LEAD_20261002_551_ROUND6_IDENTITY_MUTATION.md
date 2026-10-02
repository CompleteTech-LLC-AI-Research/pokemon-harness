# #551 round 6: identity is not enough — three round-5 findings, three fixes

Date: 2026-10-02
Branch: `lead/551-combine-553`
Round-5 reviewed head: `1723ec6`
Verified against `origin/master` (fetch; the branch is rebased onto it)
Status: **release remains PARTIAL. #551 is NOT merged.**

Both round-5 reviews returned REJECT. One found three new defects; the other
challenged the mutation evidence for the round-4 identity fix. All were
checked against behaviour before being acted on.

## Finding 1 — a rebound `PathFinder` still earns a false PASS (blocking)

Round 4's fix made stdlib trust an identity test (`finder is trusted`). Review
observed that identity is not the property being relied on. A `sys.meta_path`
entry is a mutable class in a live process, so an attacker with *no* write
access to site-packages can rebind `PathFinder.find_spec`, keep the original
object, and inherit its trust.

Reproduced on `1723ec6` before the fix:

    guard status: PASS
    foreign submodule loaded: foreign
    *** FALSE PASS via mutated trusted PathFinder ***

This route is distinct from the known planted-`.pth` one and needs no
filesystem write at all: no `.pth`, no `RECORD`, no new finder, identity intact.

**Fixed, then hardened.** These finders live in `_frozen_importlib` /
`_frozen_importlib_external`, which the interpreter loads from the frozen
stdlib, so a genuine one reports a `<frozen ...>` code file while a rebound one
names wherever the attacker compiled it. The first fix required exactly that
string.

That fix was itself a bypass, found while writing it and recorded here because
the sequence is the point. `<frozen ...>` is a *filename claim*, and
`compile()` accepts any filename the caller likes without reading that file —
the same property that made `co_filename` untrustworthy earlier in this module,
and the reason `_code_is_defined_in` exists. So an attacker compiles their own
`find_spec` *named* `<frozen importlib._bootstrap_external>` and inherits the
trust the new check was granting:

    forged <frozen ...> filename trusted: True
    guard status: PASS
    foreign submodule: foreign
    *** FORGED FROZEN FILENAME = FULL BYPASS ***

The check now verifies rather than believes. `_has_frozen_find_spec` locates
the frozen module's real on-disk stdlib source, compiles it, and requires the
finder's code object to actually appear among the code objects that file
produces — the discipline `_finder_code_file` already uses. A genuine frozen
finder is defined in that file and matches; one compiled from a string is in no
file and cannot. Both reproducers now answer `FAIL` with no filesystem write
and no new finder, while `BuiltinImporter`, `FrozenImporter` and `PathFinder`
remain trusted and the clean CLI is unaffected.

Note for whoever reads this next: the frozen module's spec reports the origin
`frozen`, so `find_spec(...).origin` is useless here and the module's
`__file__` is the only handle on the source that produced the running code.

## Finding 2 — an oversized `RECORD` row escaped as `_csv.Error`

`csv.reader` raises for a field longer than 131072 characters, and `_csv.Error`
is not an `Exception` subclass, so a single long row escaped `check_origins`
entirely — an `INTERNALERROR` under conftest, where a raise is not a finding.
Measured boundary: 131072 characters parses, 131073 raises. The pre-existing
hostile-`RECORD` row used 100000 characters, so the suite never reached it.

**Fixed.** `_record_rows` materialises the parse inside a `BaseException`
guard and returns no rows for input the parser will not read. A row the parser
declines to read is a row the guard does not attest.

**Correction, from round-7 review.** This entry originally claimed the
oversized-row row killed a mutant that narrowed `except BaseException` to
`except Exception`. It does not, and the claim was wrong:
`issubclass(csv.Error, Exception)` is `True` on this interpreter, so the
narrower handler catches it just the same. What actually fixes the escape is
*materialising* the parse — the original bug was that the generator was
iterated lazily, so the parse happened inside the caller's loop, outside the
guard entirely. The test does kill removal of the guard, but not the narrowing.

## Finding 3 — the `RECORD` algorithm label was discarded

`_record_rows` returned the declared algorithm and `_record_digests` threw it
away, so a row reading `sha512=<a sha256 digest>` attested the file. A record
claiming an algorithm the guard never computed attests nothing about it.

Measured before the fix: `sha512=` accepted `True`, `md5=` accepted `True`.

**Fixed.** Only `sha256` is attestable, so `sha512=`, `md5=`, `sha1=` and
`SHA256=` attest nothing and an honest `sha256=` row still does.

## Round-5 reviewer's challenge to the mutation evidence

The second review held that
`test_a_finder_equal_to_a_stdlib_finder_is_not_trusted` does not pin the
end-to-end outcome, because after asserting `FAIL` it imports the foreign
submodule and asserts it *loaded*.

That was measured against the rest of the file rather than taken at face value:
the same `assert leaked.ORIGIN == "foreign"` appears in ten rows, several of
them older than round 4, each preceded by a comment saying the escape really
works so the row is not vacuous. The assertion is a deliberate convention:
the guard's job is to *report* an untrusted interpreter, and once it has said
`FAIL` the interpreter is still the interpreter — the rows demonstrate that the
reported escape is real rather than hypothetical, and none of them is used as
evidence that the guard let it through. So the row was left as written, with
the convention recorded rather than changed on one reviewer's reading.

What the review did establish, and what the first review established
independently, is that the identity line is load-bearing: reverting it to
membership yields `PASS` plus a loaded foreign submodule. Round 5 measures that
mutation directly, and round 6 measures it again against the frozen-code check
added on top.

## Verification on the round-6 tree

| Check | Result |
|---|---|
| `pytest tests/test_import_origin_guard.py` | 123 passed |
| `pytest tests/test_production_gate_*.py tests/test_local_ci_policy.py tests/test_fixture_provenance.py` | 189 passed |
| Clean CLI, `--package pokered_harness` | PASS, rc 0, stderr empty |
| `ruff format --check` | clean |
| `ruff check` | one pre-existing `SIM117` |
| Mutations: drop the frozen-code check; believe the `<frozen ...>` name; skip the frozen-name gate; drop the algorithm check | killed |
| Round-4 reproducers (forged spec, equality, both iterators, three getters) | no traceback, all `FAIL` |
| `evidence/repro_forged_frozen_filename.py` | forged `<frozen ...>` refused, guard `FAIL` |
| `evidence/repro_mutated_pathfinder.py` | rebound `find_spec` refused, guard `FAIL` |
| Planted `.pth` | still reproduces — known open finding, unchanged |

Real finders were re-checked after every change: `_virtualenv._Finder` and the
editable `_EditableFinder` stay trusted, and the editable shim stays
RECORD-attested.

## For the next round

Two traps in this round cost real time and are worth not re-learning.

*A code filename is a claim.* `compile(source, filename, "exec")` names any
file without reading it. Any check that tests `co_filename` for a *string* is
checking an attacker-writable value. `_code_is_defined_in` exists for this and
the frozen check now uses it.

*An over-refusal can masquerade as a fix.* Two intermediate versions of the
frozen check refused the genuine finders and answered `FAIL` on the real
checkout — the clean CLI exited 1 — which reads exactly like a working fix
until you check the release lane. Always assert that `BuiltinImporter`,
`FrozenImporter` and `PathFinder` are still trusted and that the clean CLI still
exits 0, alongside the attack being blocked.

## Position

The planted-`.pth` gap is still open and still blocking, exactly as
`LEAD_20261002_551_ROUND5_PLANTED_PTH.md` records. Rounds 5 and 6 both failed
to close it, and the reason is structural rather than a matter of trying
harder: every candidate discriminator available inside the declared threat
model is either forgeable or refuses a genuine shim.

`#551` is not merged, and it must not be, on this head. Release remains
PARTIAL.
