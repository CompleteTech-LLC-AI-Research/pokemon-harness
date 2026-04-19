# Plan: Route 2 grind with PokéCenter heal loop

**Goal:** Replace the Option-B RAM boost in `scripts/blue_forest_to_brock.py`
and `scripts/yellow_to_brock.py` with an honest Route 2 grind so Bulbasaur
(Blue) and Pikachu (Yellow) earn the levels and moves they need to beat
Brock legitimately.

**Status (2026-04-19):** This plan has not been started. The Red pipeline
(`scripts/full_to_brock.py`) uses `scripts/level_up.py` for grinding,
which works in practice on colorized Red but is fragile: the Blue run
saw `L5 → L0 in 1 battle` (post-blackout state ambiguity) and the Yellow
run never attempted the grind.

## Problem statement

At the end of `pallet_to_viridian` / `viridian_to_route2`, the starter
is at L5-6 with ~20 HP and a mix of damaging + status moves. Route 2's
wild pool (Pidgey L3-5, Rattata L2-4, NidoranM/F L3-5, plus Yellow's
Jigglypuff/Pikachu) can out-damage a L5 starter over 2-3 encounters,
KOing it before it levels enough to learn its first gym-relevant move
(Vine Whip at L13 for Bulbasaur; Double Kick at L15 for Pikachu). No
Potions are available yet, so the only heal source is Viridian
Pokémon Center to the south.

The current "mash A through wild battles" approach accumulates damage
without a recovery valve. Two observed failure modes:

1. Bulbasaur blacks out mid-Route 2. Blackout heals, but resets XP
   progress to whatever was saved and dumps us at the house — we
   then have to re-navigate back to Route 2 and the RNG may still
   be adverse.
2. The starter reaches Brock undertuned (L6-7) and loses; the Blue
   Option-B run confirmed this by observation before we short-circuited
   with a L50 boost.

## Design space

Three options, ordered from cheapest-to-build to most-authentic:

### A. Minimal — heal-between-battles loop, no items

After every battle check current HP. If HP ≤ 40 % of max HP, walk the
player south from Route 2 back to Viridian Pokécenter (map 0x29), talk
to the nurse to refill, walk back north onto Route 2 grass, resume.
Stop when target level reached.

- **Pros:** No Mart UI navigation, no Parcel side-quest, fully
  encapsulated in a single new helper. Blackout becomes a genuine
  failure signal instead of a routine event.
- **Cons:** Round-trip Viridian PC ↔ Route 2 grass is ~30-50 steps,
  real-time ~2-5 s per heal cycle. For an 8-level grind that's maybe
  10-20 round-trips (~1-2 min of harness time). Tolerable.
- **Ships v1 in:** ~1 day.

### B. Authentic — Parcel quest + Mart + Potions

Add the canonical pokered flow: enter Viridian Mart, receive Oak's
Parcel from the clerk, return to Oak in Pallet, receive Pokédex.
Viridian Mart then accepts purchases. Buy a stack of Potions. Grind
on Route 2 using Potions when HP low; fall back to PC heal when
Potion stockpile runs out.

- **Pros:** Faster per-battle cadence (Potion heal is free in time,
  no walk). Matches how a human actually plays Yellow/Blue. Uses the
  Pokédex side-effect to unlock Viridian old man.
- **Cons:** Mart UI is a multi-level menu (BUY/SELL/QUIT → item list →
  quantity-select). Oak's Parcel delivery flow is its own scripted
  sequence inside the lab. Meaningful complexity. Pokédex unlock is
  currently RAM-poked for Red/Blue — honest acquisition is a small
  extra gain but not load-bearing for Brock.
- **Ships v1 in:** ~3-4 days.

### C. Version-specific — Mankey/Nidoran catch

Catch a Mankey on Route 22 (west of Viridian, free access once on
foot) in Yellow, or a cheap Nidoran to gain type variety. This
duplicates the problem: catching needs Poké Balls which need Mart
access, so you're back to plan B plus a catch flow.

- **Pros:** Most faithful to canon Yellow strategy (Mankey's
  Karate Chop crushes Brock).
- **Cons:** Poké Ball UI, catch mechanics (wait-and-throw timing),
  second-party-member management. Substantial extra work beyond B.
- **Ships v1 in:** ~1 week.

## Recommendation

**Do A first.** It closes the actual gap (both harnesses currently
resort to RAM boost for Brock) with minimal new code, and the Viridian
PC heal helper already exists in `run_to_brock.Driver.heal_at_
viridian_pokecenter`. We can always bolt on B or C later if we want
faster runs or a catch-flow regression test, but A is the blocker.

B is worth queuing after A lands if we end up wanting faster harness
runs for other reasons (e.g. a full-game walkthrough would benefit
from the Mart UI being understood).

C is out of scope unless we decide Yellow's Brock fight must be won
by canon strategy.

## v1 implementation plan (Option A)

### Files touched

- `scripts/grind_with_heal.py` — new module. Pure logic, callable as
  a phase from any end-to-end script.
- `scripts/blue_forest_to_brock.py` — remove `boost_bulbasaur`, replace
  the `option_b_boost` phase with a call to the new grinder.
- `scripts/yellow_to_brock.py` — same, replace `option_b_boost` with
  the grinder. Adjust target level (L15 for Pikachu's Double Kick vs
  L13 for Bulbasaur's Vine Whip).
- `scripts/level_up.py` — decide whether to keep, update, or retire.
  Currently load-bearing for Red; safest is to leave it alone and
  have `grind_with_heal.py` be the new, shared grinder used by
  Blue/Yellow, revisiting Red once the new version is proven.
- `plans/route2-grind-heal-loop.md` — this file, updated as work
  progresses.

### Milestones

1. **Heal-loop primitive.** Given a `Session` on Route 2, walk the
   player south across the map boundary into Viridian, path to the
   Pokécenter door at (23, 25), invoke `heal_at_viridian_pokecenter`,
   walk back north to Route 2 grass. Verify idempotency (calling it
   at full HP no-ops after visiting the nurse). A* through both maps
   plus the existing `navigate_to_viridian_with_retry` fallback.
2. **Grind step.** Walk one tile in grass → if battle fires, resolve
   via `rtb.Driver.resolve_battle` → return `(battles += 1, xp_gained,
   hp_after)`. Version-aware move preference: register Pikachu's
   Thunder Shock (move 84) and Double Kick (24) as damaging so
   `rtb.DAMAGING_MOVE_IDS` gives the right answer on Yellow.
3. **Grind loop.** Repeat the grind step; after each, if HP < 40% of
   max, call the heal-loop primitive. Stop when `party.mons[0].level
   >= target`. Cap total battles at ~40 to fail-closed rather than
   loop forever.
4. **Blackout recovery.** If a battle ends with party[0].hp == 0, we'll
   end up on the home map post-blackout. Walk back to Route 2 (reuse
   `navigate_to_viridian_with_retry` + viridian_to_route2 sequence)
   and continue grinding. Blackout's auto-heal is a free restore; we
   only lose accumulated XP that was above the last level-up threshold.
5. **Wire into Blue/Yellow harnesses.** Replace the RAM-boost phase
   with the grinder. Run end-to-end on both versions until Boulder
   Badge lands without Option B.
6. **Promote to Red.** Once v1 is stable on the two harder versions,
   swap `level_up.py` for the new grinder in `full_to_brock.py`.
   Red should be strictly easier since we already know it can grind.

### Risks and open questions

- **HP threshold.** 40 % of max is a starting guess. Might need to be
  higher (say 50 %) for Yellow's Pikachu which has less HP than
  Bulbasaur. Measure and tune during milestone 3.
- **Encounter distribution variance.** Both Blue and Yellow have
  Pidgey/Rattata in the same level bands as Red, but Yellow adds
  Pikachu (L5-7) and Jigglypuff (L3-6). An L7 wild Pikachu is a
  real threat to an L5 Pikachu. The grinder should check pre-battle
  enemy level and **attempt to flee** (mash B? run command?) if the
  fight looks unwinnable. This is worth doing in milestone 2 rather
  than papered over with more heal cycles.
- **Route 2 trainer lines.** No wild-grindable trainers south of the
  forest, but Yellow sometimes adds Pikachu Pals or Coming Up Pals
  movement. Verify the grass block we pick is NPC-free.
- **Move slots are finite.** Bulbasaur learns Leech Seed (L7) and
  Vine Whip (L13); Pikachu learns Thunder Wave (L18 in original,
  earlier in some revisions) and risks overwriting ThunderShock.
  Move-learn menu appears during grind and blocks with a Yes/No —
  the grinder must press A or B deterministically. Prefer B to keep
  the default-taught moves stable until we have a reason to overwrite.
- **Map-id drift.** `heal_at_viridian_pokecenter` assumes we enter
  Viridian from the south gate (`xy` near (21, 35)). Our grinder
  might leave us on a different column after blackout recovery —
  ensure the helper uses A* from current position, not a hardcoded
  path.
- **Real-time cost.** 40 battles × (10-20 s/battle + occasional
  heal round-trip) is ~10-15 min of harness time per end-to-end run.
  Acceptable for CI-like validation, painful for iteration. Consider
  a `--skip-grind` flag that keeps the RAM-boost shortcut available
  for downstream-phase debugging.

### Success criteria

Running `python -u scripts/blue_forest_to_brock.py` (and `yellow_to_
brock.py`) without `option_b_boost` produces:

- Starter at target level by end of `grind` phase (L13+ / L15+).
- Boulder Badge obtained at end of `brock_badge`.
- No RAM pokes to level, HP, stats, or moves anywhere in the pipeline.
- Blackout count ≤ 2 per run (target: 0; allow a couple for RNG).
- Total wall-clock run time ≤ 20 min on the dev machine.

## v2 extensions (deferred)

If v1 ships and we want more, queue these:

- **Option B:** Parcel quest + Viridian Mart + Potions. Adds a
  `parcel_quest` phase and a `mart_shop` phase, both reusable across
  all three versions.
- **Yellow Mankey catch** (Option C): Route 22 detour, Poké Ball
  buy, catch mechanics, second-slot party management. Most value if
  we decide to replace Option-B's over-boosted Pikachu with a
  canon-strength Pikachu + Mankey team.
- **Red promotion:** After v1 stabilises on Blue/Yellow, swap out
  `level_up.py` in `scripts/full_to_brock.py` too. Retire
  `level_up.py` as dead code once the swap sticks.
