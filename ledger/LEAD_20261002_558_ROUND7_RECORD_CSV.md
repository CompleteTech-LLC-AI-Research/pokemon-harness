# #558 round 7: RECORD is CSV, and one claim is not provenance

Date: 2026-10-02
Branch: `lead/556-const-collision` → PR #558
Repair head: `ccb8982` (predecessors `594c8fa`, `613428b`, `0d7d848`)
Verified against `origin/master` `e8d776b`
Status: **BLOCKING — repaired, awaiting independent review. Not merged.**

## Origin of this round

`origin/lead/551-combine-553` advanced to `315e641` ("parse RECORD from the
right, and require every install to agree") while this branch was at `594c8fa`.
Rather than assume equivalence, both of its findings were measured against our
own head. Both are real defects here.

## 1. Quoted names were never decoded

`RECORD` is CSV, and pip quotes a name containing a comma. Round 5 parsed the
line from the right (size, digest, then the remainder is the name) so such a
name survives the split — but the surviving field still arrives **wrapped in
quotes**. Joining the raw text onto the site-packages root built a path no
record ever listed, so:

```
RECORD bytes: b'"csv,comma_row_module.py",sha256=XsNImj...,18\n'
name_field:   '"csv,comma_row_module.py"'
_safe_resolve(.../csv"comma_row_module.py)   # wrong path
hash-pinned, comma path recorded: False      # false RED
```

A genuinely installed finder whose location contains a comma loses its
provenance and is refused. A false red, bought for no security gain.

Fixed: quoted fields are decoded, and *only* actually-quoted ones, so a name
that merely begins with a quote is left alone.

Note: #551's own `315e641` has this same gap — its `unquote` is a URL decode at
`_resolve_origin`, unrelated to CSV quoting.

## 2. Keeping one claim lets a planted record decide

`_record_digests` used `setdefault`, keeping whichever claim sorted first. An
attacker writes a record agreeing with their own bytes and relies on ordering to
win against the install that actually laid the file down.

Fixed: the map holds **every** claim, and the caller requires all of them to
match. This matches #551's rule.

## Honest scope of the new rows

`test_a_recorded_path_that_another_record_contradicts_is_not_attested` kills a
keep-the-last mutant and pins that both claims are collected before the
decision. It **cannot** kill a keep-the-first mutant — on a genuine conflict
that mutant also refuses, because the forged digest sorts first. Verified
directly:

| Mutant | Row result |
|---|---|
| `all(actual == claim ...)` (correct) | passes |
| `any(...)` | FAILS |
| `sorted(expected)[-1]` (keep last) | FAILS |
| `sorted(expected)[0]` (keep first) | **survives** |

This is a property of the rule, not a gap in the row: distinguishing the two
would need a case where the first claim agrees with the file and a later one
does not — the more dangerous ordering, and deliberately not what the forgery
in this row is. The docstring now says so rather than implying wider coverage.

An earlier version of this row asserted only "not recorded", which let the
keep-the-first mutant through trivially — it refuses too. Corrected to assert
the collected claims directly.

## Evidence

| Check | Result |
|---|---|
| Guard + provenance suites on exact merge tree vs `origin/master` `e8d776b` | 188 passed |
| Attack probes: spec forgery, copied-source `exec`, raising `__file__`, `RECORD` CSV | all defeated / correct |
| Real `sys.meta_path` finder (`DistutilsMetaFinder`) still trusted | yes |
| `ruff check` / `ruff format --check` | clean |
| CLI `scripts/check_import_origins.py` | rc=0 |

Merge-tree verification: fresh `git worktree add --detach origin/master`, then
`git merge --no-ff lead/556-const-collision` — clean automatic merge; shared venv
`.pth` repointed for the run and restored after.

## Cumulative mutation matrix for #558

| Mutation | Row killed |
|---|---|
| Remove `_finder_was_imported_from` from trust decision | copied-source row |
| Restore unguarded `getattr(module, "__file__", None)` | raising-getter row |
| Strip `try/except` from `_finder_module` | hostile-metaclass row |
| Provenance returns `True` unconditionally | copied-source + spec-forgery rows |
| `RECORD` digest comparison → membership test | RECORD-bytes row |
| Drop CSV unquote of quoted names | quoted-CSV row |
| `all(...)` → `any(...)` over claims | contradicting-record row |
| `all(...)` → keep-last | contradicting-record row |
| `all(...)` → keep-first | *not killed — see honest scope above* |

## Disposition

Author cannot approve own work. **No independent review of `ccb8982`.** #558
stays open and unmerged.

Delegation was retried three times this round (issue #489): two `followup_task`
resends to an existing agent and one fresh `spawn_agent` with `fork_turns='none'`
and the entire brief inlined in the message. All three returned "no task
specified". That is the tenth recorded failed delivery, now also with
`list_agents` and `followup_task` intermittently returning `unsupported call`.

## Reconciliation with #551

`origin/lead/551-combine-553` (`315e641`) and this branch (`ccb8982`) now
implement the same design from the same starting point, via different review
paths. Differences found so far:

| | #551 `315e641` | #558 `ccb8982` |
|---|---|---|
| RECORD name parse | from the right | from the right |
| CSV unquote of quoted names | **missing** | present |
| all claims must match | present | present |
| `__file__` getter crash | fixed (`could not be read` ×2) | fixed |
| `_finder_module` boundary | present | present |

Both rewrite the same functions. Whichever lands first, the other must **rebase
rather than merge both**.
