# Round 4: finder trust moves from in-memory claims to install records

Recorded 2026-10-02 by the lead integrator, against head `765ec61`.

## Why this round exists

Round-3 independent review returned REJECT on both heads
(`VERDICT-r3codex1.md`, `VERDICT-r3codex2.md`), with two blocker classes
between them. One of them -- the provenance false PASS -- had already been
found four separate times across three rounds, by four different routes.

## Blocker A: the in-memory provenance witness is attacker-writable

`_finder_was_imported_from` asked `sys.modules[name].__spec__` to name the
finder's file. Review showed the entire pair can be written by hand:

- build a module with `types.ModuleType`,
- attach a spec from `importlib.util.spec_from_file_location`, which sets
  `has_location=True`,
- point it at a file the attacker wrote into site-packages.

Every signal the check consumed is satisfied while the import system never
loaded anything from that file. Observed on the rebased tree before the fix:

```
finder_trusted: True
guard: PASS
cli_rc: 0
leaked_origin: foreign
```

This was the third in-memory witness to fail in a row (`__file__`, then the
code fingerprint, then `__spec__`). `LEAD_20261002_557_BLOCKING_NESTED_CODE_TWIN.md`
predicted exactly this shape of failure and asked whether folding more fields
into the comparison was still the right fix, or whether the rule should be
narrowed instead. This round narrows it.

## The narrowing

Provenance now comes from install records on disk:

- a distribution `RECORD` lists the file with a sha256 of its contents, so the
  bytes the finder runs from are the bytes an install wrote; or
- a `.pth` in the same site-packages directory imports the defining module by
  name, which is how `_virtualenv` and the `__editable__` shim are activated.

Neither can be rewritten into a different claim by a running process, which is
the property the previous three attempts lacked. Both genuine finders in this
environment still resolve as trusted and the clean CLI still reports PASS.

The trade-off is deliberate and should be stated plainly: a finder dropped
into site-packages by an installer that recorded no `RECORD` entry and no
`.pth` is now refused. That is the safe direction for a trust decision, and
the one shape it refuses is the one an attacker must construct by hand.

## Blocker B: `except Exception` does not catch hostile BaseException

Three sites named by review, plus one the review missed:

- `_finder_code_file` -- `find_spec` descriptor
- `_describe_finder` -- hostile `__module__` getter
- `_finder_source` -- module/spec reads
- `_code_fingerprint` -- code object reads
- `_foreign_path_locations` -- **not found by either reviewer**: `__path__` was
  read outside any guard, and `_resolve_origin` guards it only when
  `__file__` is absent, so a raising getter escaped even with a usable
  `__file__`

Each now re-raises `KeyboardInterrupt` and `SystemExit` and treats every other
`BaseException` as untrusted. Verified through the CLI with a hostile
`sitecustomize`: rc 1, JSON FAIL on stdout, no traceback on stderr.

## Verification

| Check | Result |
|---|---|
| `tests/test_import_origin_guard.py` | 108 passed (104 before, plus 4 rows) |
| production-gate + local-CI suites | 172 passed |
| `ruff format --check` | clean |
| `ruff check` | 2 findings, both `SIM117` already present on master |
| clean CLI | PASS, rc 0, no traceback |

### Mutation matrix

Four mutants built and killed. M4 additionally confirmed end-to-end
exploitable *without* its row (`finder_trusted: True`, `cli_rc: 0`, foreign
leaked), which is the check the #557 ledger asks for: a pinned mutant proves a
line is load-bearing, not that it is sufficient.

| Mutant | Reverted line | Killed by |
|---|---|---|
| M1 | `_finder_code_file` BaseException guard | `test_a_finder_descriptor_raising_base_exception_becomes_a_finding` |
| M2 | `_describe_finder` BaseException guard | `test_a_finder_module_getter_raising_base_exception_becomes_a_finding` |
| M3 | `__path__` read guard | `test_a_path_getter_that_raises_becomes_a_finding_not_a_traceback` |
| M4 | provenance rule -> forgeable `__spec__` | `test_a_forged_module_and_spec_do_not_certify_an_uncertified_finder` |

## One test row changed premise

`test_a_genuine_finder_with_a_non_utf8_source_file_keeps_its_trust` planted a
module into site-packages and imported it, then asserted it was trusted. Under
the new rule that shape is exactly what must be refused -- it is the planted
file, not an installer record. The row now also writes the `.pth` an installer
would have written, which keeps the row testing what it claims to pin (the
PEP 263 decoder) while making it a genuine shim rather than the attack shape.

## Note on an unverifiable citation

`LEAD_20261002_557_BLOCKING_NESTED_CODE_TWIN.md` cites
`LEAD_20261002_555_BLOCKING_FORGED_CODE_FILENAME.md`, option 2, as the
alternative that "has never been tried". **No such file exists in any local
ref**, including the branches named alongside it. The recommendation itself
is sound and is what this round implements; only the pointer is unresolvable.

## Status

Not merged. Release remains PARTIAL. Round-4 independent review required.
