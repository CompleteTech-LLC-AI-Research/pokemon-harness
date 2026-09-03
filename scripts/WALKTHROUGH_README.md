# Walkthrough Driver — Diagnostic Status Report

This note records a historical Pokémon Red walkthrough experiment. It is a
diagnostic account of one pinned-ROM run, not current release evidence. The
walkthrough can exercise the harness, but its generated states, screenshots,
RAM shortcuts, and scripted menu control do not certify single-session,
paired-link, trade, battle, or production readiness. Use the
[production runbook](../docs/PRODUCTION_RUNBOOK.md) and
[release checklist](../docs/RELEASE_CHECKLIST.md) for current release claims.

## What got built

[`scripts/walkthrough.py`](walkthrough.py) is a linear scripted
playthrough that logs **every** button press with three artifacts:

```
walkthrough_output/
  press_NNNN.png    — framebuffer screenshot after the press
  press_NNNN.state  — PyBoy save state bytes (roundtrips via load_state)
  press_NNNN.json   — parsed GameState snapshot
  playthrough.md    — human-readable per-press log
  playthrough.json  — machine-readable per-press log
  milestones/       — selected checkpoint states
```

Run with:

```bash
POKERED_ROM_PATH=<rom-path> POKERED_SYM_PATH=<sym-path> \
  python scripts/walkthrough.py --clean
```

## Historical observations on the pinned ROM

The observations below came from the walkthrough experiment. They are useful
for reproducing diagnostic behavior, but they are not a current release gate
and do not generalize to other ROM variants or runtime modes.

| Phase | Status | Evidence |
|-------|--------|----------|
| Boot → bedroom with walk control | observed in diagnostic run | press ~175 |
| Bedroom → stairs warp → 1F | observed in diagnostic run | press ~188, warp at (7, 1) |
| 1F → front door → Pallet Town | observed in diagnostic run | press ~202, door at (3, 7) |
| Pallet → Oak intercept at (10, 1) → lab | observed in diagnostic run | press ~233 |
| Oak's lab speech → walk to Bulbasaur → A | observed in diagnostic run | press ~314, party=1 (Bulbasaur 0x99, level 5) |
| Rival picks his starter (script 6→9) | observed in diagnostic run | `wOaksLabCurScript` progresses |
| Rival battle trigger at (5, 6) | timing-sensitive | works in isolation in the historical run |
| Rival battle (Tackle mash) | isolated diagnostic only | milestone 12/13 |
| Lab exit → Pallet → Route 1 | isolated diagnostic only | path `LEFT×3 UP×10 RIGHT UP×3` |
| Route 1 → Viridian City | isolated diagnostic only | 55-move route recorded below |

## Concrete empirical findings baked into the script

1. **REDS_HOUSE_2F stairs are at (7, 1)** (bed at (3-4, 5) blocks the obvious path up from spawn). Path: DOWN LEFT UP×5 RIGHT×5 UP.
2. **REDS_HOUSE_1F door is at x∈{2, 3} on y=7**, not on the east side where the stairs drop you. First 3 DOWN presses after stairs-warp are no-ops (warp settle).
3. **Warp fades take 60-120 ticks**. Reading `wCurMap` immediately after a warp-trigger returns the old map.
4. **Pallet → Oak intercept triggers at y=1**, specifically (10, 1). Walking UP from the house-exit needs a sidestep first because (5, 5) is the house door and UP would re-enter.
5. **Pallet → Oak's Lab direct warp** is at (12, 11), reachable via RIGHT×4 DOWN×6 RIGHT×3 UP.
6. **Button presses need `duration ≥ 6`** — 1-tick presses are missed at menu boundaries.
7. **`wTextDest` is unreliable** for "dialog active" — it points into WRAM scratch buffers most of the time. Use the `PrintText` hook (`text_shown` event) instead.
8. **`wMaxMenuItem` is sticky** — it holds its last value after the menu closes. Can't be used alone to detect "a menu is showing."
9. **Lab script state** (`wOaksLabCurScript`) sequence for the starter/rival flow:
   - 4 (FollowedOak) → 5 (OakChooseMonSpeech) → 6 (PlayerDontGoAway) → 8 (ChoseStarter) → 9 (RivalChoosesStarter) → 10 (RivalChallengesPlayer) → 11 (RivalStartBattle).
   - Transition 6→8→9 is fast; 9→10 requires the rival's sprite walk to complete.
   - Battle fires when player is at wYCoord==6 AND script==10.
10. **Route 1 ledges** only allow southbound jumps. Northbound path requires zig-zagging around them.
11. **PyBoy installs a stdout logging handler** that emits `.sym`-skip warnings — **`contextlib.redirect_stdout(sys.stderr)` around Session construction** is required for MCP stdio to work.

## Locally generated milestone save states

The files in `walkthrough_output/milestones/` are gitignored artifacts derived
from the ROM. They are not committed, distributed, or interchangeable between
ROM variants. Treat them as operator-managed diagnostic inputs:

Each has a matching `.png` and `.json` alongside, load with:

```python
session.load_state(open("milestones/NAME.state", "rb").read())
```

| File | State | map | xy | party |
|------|-------|-----|----|----|
| `step_0030.state` | end of shrink cutscene | REDS_HOUSE_2F | (3, 6) | 0 |
| `step_0175.state` | walk control in bedroom | REDS_HOUSE_2F | (3, 7) | 0 |
| `step_0180.state` | just after stairs warp | REDS_HOUSE_1F | (7, 1) | 0 |
| `step_0193.state` | first frame in Pallet Town | PALLET_TOWN | (5, 6)-ish | 0 |
| `step_0314.state` | Bulbasaur acquired | OAKS_LAB | (8, 4) | 1 |

## Where it gets flaky

When chained end-to-end, the step that breaks most often is:

- **Rival battle trigger after Bulbasaur pickup.** We correctly wait
  for `wOaksLabCurScript >= 10` and walk to (5, 6), but occasionally
  the battle doesn't fire — likely because the script needs a
  specific sub-tick window between the rival's sprite walk completing
  and the player's movement input.

The path from the rival battle onward (exit lab → Pallet → Route 1 →
Viridian) was observed from a clean post-battle state in the historical
standalone BFS. That observation is not a current release rerun. The
integration point is the rival battle itself, not the downstream navigation.

## Remaining work to fully automate

1. **Rival-battle trigger reliability**: add a second probe that idles
   at (5, 6) for 10+ seconds and fires the battle via a scripted
   walk-DOWN-into-rival interaction if the automatic y==6 trigger
   doesn't fire.
2. **Battle-loss recovery**: Bulbasaur at level 5 loses to the rival's
   Charmander about half the time. When we faint, Oak's post-battle
   script still runs and we end up in Pallet, so it's not blocking
   per se, but HP=0 then Route 1 wild = blackout. Needs a "grind on
   Route 1" sub-phase to level up first.
3. **Route 1 wild-encounter handling**: current "mash A → Tackle"
   wins most encounters but RUN logic via DOWN-RIGHT-A in the battle
   menu was never validated because the fresh Bulbasaur outpaces most
   early-route mons.

These are additional state-machine refinements recorded by the diagnostic
experiment. They do not establish a current repeatable end-to-end path or
production acceptance; the required button sequences are known for the pinned
ROM used in that experiment.

## Historical end-state

### Diagnostic path recorded on a real ROM

The historical run recorded a path from boot into Viridian City (723 presses,
with a per-press artifact trail). Its final state was
`map=VIRIDIAN_CITY xy=(21, 35) party=1 Bulbasaur L6 @ 19/19 HP`. This is a
single-run diagnostic observation, not a repeatable current acceptance result;
the generated artifacts and exact runtime identity must be supplied before
reproducing it.

### 🚧 Post-Viridian: partial diagnostic progress, two real blockers hit

Pushing further toward the first badge uncovered two legitimate
automation challenges documented here as open problems:

The post-Viridian shortcut below is an Option-B/RAM-boost diagnostic. It is
not an untouched playthrough and must not be counted as release acceptance.

1. **Old Man gate (bypassed via RAM write).** Viridian's `Old Man`
   blocks the north exit until `EVENT_GOT_POKEDEX` is set. The
   canonical path requires: Viridian Mart → get Oak's Parcel → back
   to Pallet → deliver to Oak → return. I verified steps 1-3 work
   (`Oak's Parcel` item ID 0x46 acquired, reached the lab), but Oak's
   sprite position after the rival cutscene no longer matches the
   `object_event` table in `pokered/data/maps/objects/OaksLab.asm`.
   An exhaustive search of the lab with A-press facing every
   direction at every tile **did not find an interactable Oak**.
   Pragmatic shortcut used: RAM-set bit 37 of `wEventFlags`
   (`mem[wEventFlags + 4] |= 0x20`). This unblocks the Old Man and is
   exactly the kind of targeted memory write the harness was built
   to support.

2. **Move-selection in battle.** We reached Viridian Forest and
   ground Bulbasaur from L6 → L9 by fighting trainers and wild
   encounters with "mash A = use first move = Tackle". After enough
   battles, Tackle ran out of PP (35 uses) and the game shows "No PP
   left for this move!" — at which point A-mash loops forever on the
   same message because Tackle is still the selected slot. The real
   fix is a tiny state machine: in the move menu, if the selected
   move has PP=0, press DOWN to cycle to `Leech Seed` (L7 move, 10
   PP, Grass-type — actually *super-effective* vs Brock's Rock team).
   I did not wire this up in this session.

### More locally generated milestone saves

Besides the early-game milestones, new checkpoints in
`walkthrough_output/milestones/`:

| File | State |
|------|-------|
| `VIRIDIAN_ENTRY.state` | just arrived at Viridian (press 723) |
| `IN_MART.state` | inside Viridian Mart |
| `MART_INTERACT.state` | after talking to clerk, bag has Parcel (0x46) |
| `OUTSIDE_MART.state` | exited Mart back to Viridian |
| `ROUTE1_NORTH.state` | re-entered Route 1 going south |
| `PALLET_RETURN.state` | back in Pallet Town |
| `VIRIDIAN_UNBLOCKED.state` | after RAM-set of `EVENT_GOT_POKEDEX` |
| `ROUTE_2.state` | entered Route 2 — **past the Old Man** |
| `FOREST_ENTRY.state` | through south gate into Viridian Forest |
| `FOREST_PROGRESS.state` | Bulbasaur L9, stuck at (18, 32) with no PP |

### To finish the first badge (Boulder Badge)

Remaining work, in order:

1. **Fix PP management in battle** (~50 lines): if
   `wBattleMenuCurrentPP` is 0 after a FIGHT menu open, press DOWN
   or SELECT to pick a different move; fall back to Struggle when
   all 0.
2. **Viridian Forest north gate** (a BFS from `FOREST_PROGRESS`
   reaching map `0x2F`).
3. **Pewter City navigation** (BFS to gym warp).
4. **Gym interior** — defeat Jr. Trainer + Brock. Bulbasaur's **Leech
   Seed** (super-effective) + enough HP wins this reliably once PP
   management works.
5. **Check `wObtainedBadges` bit 0 is set** (Boulder Badge).

The harness provides the session, input, state, and hook plumbing used by this
experiment. This walkthrough does not prove that every behavioral path is
automated, that the native runtime matches source mode, or that any link/trade/
battle acceptance matrix passes. The open items above are behavioral scripting
work, not evidence of a production-ready release.
