# Ordinary Red six-party link admission v1

`release-evidence/scenarios/normal-red-cerulean-six-party-v1.json` admits a
separate, operator-managed captured state. It does not replace any legacy
fixture pin or establish the complete release matrix.

The state was captured by signed producer `35a9ccc9c00ebfe9f578f6eaf68a957de275d171`
in one 92.395-second continuation from the reviewed six-party state `53c756e...`.
Its ancestry continues through ordinary catches and a Mart visit to the
canonical fresh-boot Red color journey at `573e973...`. It is a state
continuation, not a new fresh boot. No controller RAM, PC, or PP writes were
used. The independent reviewer read and hashed 66 artifacts, 30 state pairs,
3,088 continuous action pairs, and four loaded Git source files; the reviewer
did not rerun the ROM. The earlier zero-input geometry failure remains retained.

The final location is Cerulean Center map 64, `(11,3)`, with six living,
fully healed party members and restored PP. The state remains outside Git:

```text
<fixture-root>/red/normal-red-cerulean-six-party-v1.state
```

Admission checks its size, SHA-1 and SHA-256 and both input pins before launching
an emulator. The manifest has captured provenance, not a reproduction claim.
The producer's operator pret checkout differed from the pinned whole checkout;
36 consulted source files were individually verified against pinned blobs.

## Execute one strict row

Use the documented clean source or separate native environment, with `.[dev]`
installed because this qualification command reuses the existing acceptance
drivers. In source mode, initialize the bundled PyBoy submodule and set
`PYTHONPATH=src:.:vendor/pyboy-src`, `PYBOY_NO_CYTHON=1`, and
`POKERED_TRADE_RUNTIME_MODE=source`. In native mode, use `PYTHONPATH=src:.`,
unset `PYBOY_NO_CYTHON`, and set `POKERED_TRADE_RUNTIME_MODE=cython`.

```bash
python -m scripts.qualify_normal_red_link \
  --rom <pinned-pokemon-red-color.gb> --symbols <pinned-pokemon-red.sym> \
  --fixture-root <operator-fixture-root> --output <new-external-run-directory> \
  --transport local --kind trade
```

Repeat with `--transport tcp` for two independent MCP processes. Trade offers
natural Ivysaur slot 0 and Pidgey slot 5 through the public party cursors. The
existing strict oracle requires both ROM copy markers, full 44-byte record
SHA-256 exchange, ordered survivor compaction, and unchanged records after
animation, evolution check, save and selection-loop return. The source records
are naturally different; no synthetic OT-ID mutation is used. Journals record
driver-level public pair operations and observations, not every internal RPC.

`--kind battle` requires both live terminal edges and a witnessed nonterminal
knockout followed by a living replacement, followed by complementary pinned
Red results (raw 0 is win, raw 1 is loss), actual exhaustion of the losing
six-member party, and post-match restoration. The public `terminal_result`
continues to report `None` for ambiguous raw zero; the scoped source oracle
classifies the result without changing that public observation contract.
Every battle decision comes from
public game-state resources, available natural damaging moves and menu geometry.
The operation journal, battle observations, failure receipt and bounded owned
process teardown remain external. Exhausted budgets fail; they never become
smoke-test acceptance. Each run has a 1,200-second wall bound plus cleanup and
phase/frame bounds in the drivers. After the result screen, pinned
`CableClub_DoBattleOrTrade` calls `HealParty` and `ReturnToCableClubRoom`:
HP and PP return to full, status clears, and all six full 44-byte record
digests must match the initially healed admitted party. Bag contents and
money must be unchanged. Colosseum has no map warps; room return remains
map 240. Acceptance requires a subsequent ordinary downward tile movement
from each returned spawn, with movement settled and no battle restarted.
Cached terminal flags and successful process exit cannot satisfy that check.

Historical native TCP attempt 7 passed terminal-edge/KO/replacement scope on
its original uncommitted source. Its receipt did not establish room control
or this new post-match oracle. It is retained without promotion or attribution
to a later signed source. Both winning roles, source-supported forfeit/tie,
and every required ordered family/runtime/transport row remain separate open
requirements of issues #103 and #72; this admission does not close them.

Only a terminal `PASS` with successful owned-process cleanup qualifies its
named row. The new source needs independent review and actual receipt review.
Other family pairings, source/native combinations not executed, dedicated quiet
CPU allocation, and the full release gate remain open. This admission does not
close the full trade/battle or release issues.

## Delivered tooling scope and retained committed observations

This delivers a versioned source-only controller foundation and the settled
movement retry repair; it does not promise a complete qualification matrix.
The retry sends DOWN only at the exact original spawn with integer
`walk_counter == 0`; old coordinates during ongoing movement cannot queue an
extra pulse. Temporal execution controls retain the old overshoot witness.

Actual rows attributed to signed source `421cebbc84f54fd59e9f1d7681984c1324a4217b`
completed as follows, under their original 1200-second bounds:

| Runtime | Transport | Trade | Battle |
|---|---|---|---|
| Native | LOCAL | PASS | PASS |
| Native | TCP | PASS | PASS |
| Source | LOCAL | PASS | FAILED: 1200-second timeout |
| Source | TCP | PASS | PASS |

These seven successful receipts establish only their exact historical scoped
rows. The source LOCAL battle timed out while battle remained live, before
`complete_battle` returned: its flushed `terminal_edges_only` marker is absent,
so the subsequent return helper was not reached. Last observed battle-driver
frame counter was 5264; the journal retains the pending step intent. This is
source call-order and retained observation evidence, not a reconstructed
invocation counter. No timeout was extended or promoted to PASS.

Controller imports and live OS readback provide runtime evidence, but completed
trade rows do not contain direct per-owner imported-module origins. The source
build flag alone does not establish actual loaded runtime. No later source
commit inherits these historical rows. Full #103/#72, winning-role symmetry,
forfeit/tie, other family pairings, admitted quiet CPU, and release readiness
remain open. The original full-qualification PR remains held separately.
