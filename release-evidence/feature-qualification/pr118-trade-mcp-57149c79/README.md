# Real-ROM MCP trade matrix - evidence bundle

Status: **TERMINAL**

This directory is a sanitized record of the real-ROM MCP stdio trade matrix that is the
outstanding acceptance item for PR #118 and for issue #105. It contains no ROM bytes, no
symbol files, no save states, no traces and no credentials: only SHA-1 digests, sizes,
labels and per-row outcomes.

## Identity

| field | value |
|---|---|
| reviewed head | `57149c79931dde507515f3ae2d639806953e17ea` |
| worktree head | `57149c79931dde507515f3ae2d639806953e17ea` |
| worktree tree | `72c99491d9a36db3eac277c55381d7712412970b` |
| suite | `tests/test_mcp_trade_records_rom.py` |

`worktree_head` must equal `reviewed_head`; if it does not, this bundle does not describe the
reviewed commit and must not be cited as such.

## Dual identity: these rows also describe the merged commit

Rows execute against the reviewed head, but PR #118 landed as a later commit. That transfer is
proved here rather than asserted:

- reviewed head: `57149c79931dde507515f3ae2d639806953e17ea`
- merged commit: `80f2313c2f12dcffaef150dcd69ef2831b07bafd`
- differing files: 1
- rows unaffected: **True**

| path | bytes r/m | sha256 r = m | AST r = m | imported by tested modules |
|---|---|---|---|---|
| `scripts/produce_battle_scenario.py` | 14253 / 14201 | no | yes | no |

`rows_unaffected` is true only when every differing file is AST-identical between the two
commits AND is not imported by the modules the rows execute through. If a later commit
touches the tested surface this recomputes to false, and the rows must be re-run against the
merged commit instead of transferred.

Rows that reach `rc=0` at the reviewed head therefore also describe the merged commit, but they
are labelled by the state actually executed, as the acceptance policy requires.

## Runtime provenance (measured, not asserted)

The dual-runtime claim is only meaningful if each tier really is the runtime it declares, so each
tier was probed directly:

| tier | python | pyboy | fork revision | compiled ext | is declared runtime | revision = vendored pin |
|---|---|---|---|---|---|---|
| `source` | 3.11.2 | 2.7.0 | `c565df66c373` | 0 | yes | yes |
| `cython` | 3.11.2 | 2.7.0 | `c565df66c373` | 58 | yes | yes |

Vendored revision marker: `c565df66c3731fad2856169a90f6bbec99925915`.

- both tiers are their declared runtime: **True**
- both tiers share the pinned fork revision: **True**

The source tier must import the vendored `.py` package; the cython tier must import compiled
extensions from outside the vendored tree. Both tiers report the revision the harness exposes as
`pyboy.__pokered_harness_revision__`, which is what makes these rows dual-runtime evidence rather
than two runs of the same interpreter.

## Rows

`rows_expected = 48` (24 declared MCP stdio trade rows x {`source`, `cython`} runtimes).

- pass: 48
- not_pass: 0
- in_flight: 0
- outstanding: 0
- pass by runtime: `{'cython': 24, 'source': 24}`

A row counts as a pass only when all of these hold: its most recent `END` record in `rows.log`
is `rc=0`, and its JUnit XML exists with `tests>=1, failures=0, errors=0, skipped=0`. Every
other row is recorded as its honest result and is never counted as a pass. Killed rows
(`rc=143`) are not evidence and are re-run before being cited.

## How each row was decided (parallel attempt vs low-load re-run)

- rows decided by the parallel run: 38
- rows decided by the serial discriminator: 10
- earlier non-passing attempts recorded across those rows: 26

`rows.log` is append-only, so a row can carry more than one `END` record. Whichever comes last
decides the row, and the serial discriminator marks its record with `SERIAL-DISCRIMINATOR:` so a
re-run cannot silently replace a failure. Reporting only the final state would hide that a second
pass happened, which is the shape of retrying until green, so it is disclosed per row in
`rows_pass_detail[].decided_by` together with `prior_not_pass_attempts`.

Why a re-run is legitimate here rather than a weakened green: the parallel attempt loses a race,
not an assertion. Those rows terminate in `timeout: no FRAME_DONE from peer within 10s`,
`serial_backend_error`, or asyncio `CancelledError` - infrastructure and timing errors raised by a
loaded host (47 unrelated infinite burners on 12 cores at load1 ~90), never a mismatch about
party-record exchange. The serial pass runs the same commit, same node id, same interpreter and
the same 10-second peer barrier, one row at a time, in a window whose entrance is gated on
measured load; nothing about the assertion is relaxed. Load actually observed is recorded in
`re_run_provenance.serial_rounds`.

## Retained per-row artifacts

`junit/<row>.xml` is the runner's own JUnit XML and `logs/<row>.log` is its pytest log. Both
go through the same rewrite: every absolute local path is replaced by a placeholder, and
nothing else is touched. A passing row's XML is a clean `<testcase>` element holding only
repo-relative node ids, so the rewrite is a no-op there; a failing row's XML embeds the
traceback and therefore names interpreter and asyncio files, and its log does too. The
placeholders are bracket-delimited precisely because they are substituted inside XML text:
an angle-bracketed placeholder would make the document unparseable.

| placeholder | what it stood for |
|---|---|
| `[worktree]` | the tested worktree checked out at the reviewed head |
| `[source-venv]` | the source-tier interpreter's site-packages |
| `[native-venv]` | a compiled-tier interpreter's site-packages |
| `[operator-root]` | the operator-supplied private asset root |
| `[matrix-root]` | this matrix's own bookkeeping directory |
| `[workspace]` | the local workspace root |
| `[home]` | the local home directory |
| `[usr]`, `[tmp]`, `[var]`, … | the corresponding system directory |

The exact list, in the forms emitted (placeholders only, never the roots), is in
`trade-evidence.json` under `sanitization.placeholders`.

The substitution is applied longest-prefix-first and is asserted to round-trip byte for byte
before any file is written (`sanitization.round_trip_verified` in `trade-evidence.json`). The
build aborts rather than emit a record it cannot prove it left otherwise untouched. A
bundle-wide sweep then re-scans every emitted file for absolute-path substrings and fails the
build if any survive.

The log is what makes the row's acceptance auditable: each passing row carries
`MCP_TRADE_RECORDS_ROM {...}` with `primary_before`/`primary_after`/`peer_before`/`peer_after`
party digests, `offered_slots`, `receiving_slot`, `fixture_sha1`, `peer_fixture_sha1`, the game
milestones and the runtime mode. Those records are also lifted into
`trade-evidence.json` under `rows_pass_detail[].rom_record`, so the input hashes survive even if
the raw logs are dropped (`--omit-logs`).

- JUnit XML files included: 48
- logs included: 48
- passing rows carrying an input-hash record: 48 of 48

A TERMINAL bundle is refused when any passing row lacks either artifact or its input-hash
record, because a pass without its digests is not auditable evidence.

## Operator assets (digests only)

- ROMs: 5
- symbol tables: 3
- link-test fixtures: 31

Assets were supplied by the operator from a private root held outside version control. The
digest list is in `trade-evidence.json` under `assets[]`, each entry carrying `kind`, `label`,
`size` and `sha1`. No asset bytes are included in this bundle.

## Reproducing

These runs require operator-supplied ROMs and fixtures that cannot be distributed. With those
roots in place the matrix is driven by, from a clean checkout at the reviewed head:

```sh
# worktree path and branch were declared before creation, per repo workspace policy
bash run_pr118_realrom.sh source cython     # PR118_PARALLEL=<n>
python3 summarize_matrix.py                 # rewrites matrix-summary.json
python3 build_evidence.py <outdir>          # regenerates this bundle
python3 verify_evidence_bundle.py <outdir>  # independent re-derivation
```

Guardrails honoured by this lane: `POKERED_SKIP_SHA1` was never set, and no bound, tolerance,
skip or xfail was changed to obtain a green row.
