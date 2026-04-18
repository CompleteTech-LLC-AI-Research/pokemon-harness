# Walkthrough Driver — Status Report

This note records the outcome of the first walkthrough attempt (boot →
Viridian City) and pins down what's reproducible vs. what needs more
work.

## What got built

[`scripts/walkthrough.py`](walkthrough.py) is a scripted playthrough driver
that logs **every** button press with three artifacts:

```
walkthrough_output/
  press_NNNN.png    — framebuffer screenshot after the press
  press_NNNN.state  — PyBoy save state bytes (roundtrips via load_state)
  press_NNNN.json   — parsed GameState snapshot (overworld, battle, party, ...)
  playthrough.md    — human-readable per-press log
  playthrough.json  — machine-readable per-press log
  milestones/       — key checkpoint states (listed below)
```

Run with:

```bash
POKERED_ROM_PATH=... POKERED_SYM_PATH=... python scripts/walkthrough.py --clean
```

## Where the latest run got to

| Press | Map | Coords | Milestone |
|-------|-----|--------|-----------|
| 1     | (boot) | — | Title screen → NEW GAME |
| 29    | REDS_HOUSE_2F | (3, 6) | Bedroom preloaded during Oak intro |
| ~175  | REDS_HOUSE_2F | (3, 6) | Player gains walk control (post-shrink cutscene) |
| 180   | REDS_HOUSE_1F | (7, 1) | Stairs warp to 1F |
| 193   | PALLET_TOWN | (3, 7) | Exited front door — **actually outside** |
| 366   | PALLET_TOWN | (3, 6) | Stuck against Pallet hedge/fence, spamming UP |

The run stops because the blind "walk UP until map changes" loop hits a
fence Red can't step over. The remaining work is strictly Pallet Town
navigation: route around the hedge, reach Oak's-lab trigger, handle
starter selection, rival battle, walk Route 1, enter Viridian.

## Real empirical findings (committed in the script)

These were learned by probing the live ROM and are now hard-coded in
[`walkthrough.py`](walkthrough.py):

* **REDS_HOUSE_2F stairs are at `(7, 1)`**, not the lower-left as I
  initially assumed. The bed at `(3-4, 5)` blocks UP from the spawn at
  `(3, 6)`, forcing the path: `DOWN → LEFT → UP×5 → RIGHT×5 → UP`.
* **REDS_HOUSE_1F exit door is at `(2, 7)` or `(3, 7)`** on y=7, not at
  x=7. Walking straight DOWN from the stairs arrival lands on the SE
  wall and stalls.
* **Warp fade-in takes ~60-100 ticks** after the map change. Reading
  `wCurMap` immediately after a warp-triggering press returns the old
  map; the driver must idle through the fade before acting.
* **Button presses need ≥6-tick duration** to register reliably. The
  default `pyboy.button(name, duration=1)` is often missed at menu
  boundaries. `WalkthroughDriver.press` defaults to `duration=6`.
* **`wTextDest` is NOT a reliable "dialog active" indicator.** It points
  into WRAM scratch buffers most of the time. Use the `PrintText`
  execution hook (`text_shown` event) for recent-text detection.
* **`wMaxMenuItem` is sticky.** It holds the last menu's bound even
  after the menu closes, so it can't be used alone to detect "a menu
  is up."

## Committed milestone save states

In `walkthrough_output/milestones/` (gitignored — they derive from the
ROM). Each has a matching `.png` and `.json` alongside.

| File | What it represents |
|------|--------------------|
| `01_bedroom_spawn.state` | End of shrink cutscene; map loaded, no control yet |
| `02_bedroom_with_control.state` | Player can walk; used by `_player_has_walk_control` probe |
| `03_stairs_to_1f.state` | Just after stairs warp; (7, 1) in REDS_HOUSE_1F |
| `04_exit_to_pallet.state` | First frame in PALLET_TOWN after exiting |
| `05_pallet_final_blocker.state` | Stuck at Pallet fence — resume from here to continue |

Load any of them with:

```python
session.load_state(open("milestones/04_exit_to_pallet.state", "rb").read())
```

## What would finish the job

Roughly in order:

1. **Pallet Town routing**: after exiting the house, walk LEFT+UP around
   the hedge line to reach the Pallet-to-Route-1 corridor. Oak's
   intercept-trigger tile is somewhere on that corridor.
2. **Oak's lab navigation**: walk up to the Pokéball table (center-top
   of the lab), press A on one, confirm YES.
3. **Rival battle**: press A to use the first move every turn. Bulbasaur
   vs. Charmander or similar — straightforward.
4. **Route 1**: walk UP through Route 1. Wild encounters will appear;
   the script already has a "select RUN" fallback in `_handle_overworld_tick`,
   but may need more aggressive handling if RUN fails.
5. **Viridian City**: arrival condition is `wCurMap == 0x01`.

Each of these is its own small puzzle — the overall shape is "probe
each map's walkable graph, hard-code the path, verify empirically."
The driver infrastructure already handles all the plumbing; only the
phase functions need more route data.

## Lessons carried back to the harness

Three findings from this walkthrough hardened the main harness:

1. `wTextDest` is not trustworthy for UI-active detection. The harness
   now exposes `PrintText`-hook events as `text_shown` for precisely
   this reason.
2. Warp transitions take a non-trivial number of ticks. Any
   state-polling loop that expects a warp to have "landed" needs an
   idle/poll with a timeout, not a single post-press check.
3. Menu-state fields like `wMaxMenuItem` hold their value beyond their
   menu's lifetime. Presence of a menu must be verified by probing
   (input makes the cursor move) or by recent-hook activity, not by
   the sticky RAM field alone.
