"""Shared constants and helpers for the Route 2 heal-loop grinder.

Extracted from ``scripts/grind.py`` (see issue #154). Holds the map/move
constants, :class:`GrindResult`, and the navigation, battle, and heal-loop
helpers used by :func:`grind_to` in the ``grind`` entry point.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pokered_harness.session import Session

# --- Map constants (shared across Red/Blue/Yellow) -------------------------
M_PALLET = 0x00
M_VIRIDIAN = 0x01
M_PEWTER = 0x02
M_ROUTE_1 = 0x0C
M_ROUTE_2 = 0x0D
M_REDS_1F = 0x25
M_REDS_2F = 0x26
M_VIRIDIAN_POKECENTER = 0x29

# Grass patch in the southern half of Route 2 (tile id 0x52, y in [45, 52]
# around x=4..15). Copy-pasted from level_up.py so we don't have to rework
# that module.
GRASS_Y_MIN = 46
GRASS_Y_MAX = 52
GRASS_ANCHOR = (6, 51)  # a centred grass tile for the walk-in-grass loop

# Route 2 south entry (warps down into Viridian City).
ROUTE2_SOUTH_EXIT = (8, 71)

# Viridian City: Pokecenter door warp tile and north-exit column (warps up
# to Route 2 (8, 71)). Empirically the north-border warp threshold is any
# tile on the top row around x=17..22 — (17, 0) is the column the existing
# viridian→r2 A* target uses. (19, 0) also works, use 17 to match
# yellow_to_brock's `vi2r2` A* target.
VIRIDIAN_PC_DOOR = (23, 25)
VIRIDIAN_NORTH_EXIT = (17, 0)


# --- Damaging-move allowlist ----------------------------------------------
# Superset of every first-gym-relevant damaging move Bulbasaur / Charmander
# / Squirtle / Pikachu can have before L15. Status-only moves (Growl,
# Tail Whip, Thunder Wave without Thunderbolt) are intentionally excluded
# so the battle AI prefers damaging picks.
DAMAGING_MOVE_IDS = {
    33,   # Tackle
    10,   # Scratch
    98,   # Quick Attack
    40,   # Poison Sting
    73,   # Leech Seed (drain ~ damage)
    22,   # Vine Whip
    52,   # Ember
    55,   # Water Gun
    84,   # ThunderShock
    85,   # Thunderbolt
    86,   # Thunder Wave (kept — can still chip)
    24,   # Double Kick
    29,   # Headbutt (Pidgey fallback)
}

# Common "gym-relevant learn-move" IDs we grind toward.
MOVE_VINE_WHIP = 22
MOVE_DOUBLE_KICK = 24


@dataclass
class GrindResult:
    """Summary stats for the caller's log."""
    start_level: int
    final_level: int
    battles: int
    blackouts: int
    heals: int
    wall_seconds: float
    hit_target_level: bool
    learned_target_move: bool
    stopped_reason: str  # "target_level", "target_move", "timeout", "failed"


def _lead(drv: rtb.Driver):
    gs = drv.gs()
    return gs.party.mons[0] if gs.party.mons else None


def _hp_fraction(mon) -> float:
    if mon is None or mon.max_hp <= 0:
        return 0.0
    return mon.hp / mon.max_hp


def _clear_repel(drv) -> None:
    """Zero out wRepelRemainingSteps so wild encounters can fire on the
    next overworld step. We activate Repel during the heal round-trip so
    we don't grind Bulbasaur further on the way back — but the grass
    bouncing loop needs encounters, so clear it before resuming."""
    try:
        base = drv.sym.addr_of("wRepelRemainingSteps")
        drv.mem[base] = 0
    except (AttributeError, LookupError, TypeError):
        return


def _pathfind(drv, session: Session, outdir: Path, goal_xy: str,
              label: str, rom: str, sym: str, sha1: str) -> str:
    """Save a seed state, invoke the A* pathfinder subprocess, return the
    direction string.  Wraps ``full_to_brock.run_pathfinder`` with the
    scratch-state boilerplate."""
    seed = outdir / f"_{label}.state"
    seed.write_bytes(session.save_state())
    out = outdir / f"_{label}.txt"
    return ftb.run_pathfinder(seed, goal_xy, out, rom, sym, sha1)


def _safe_walk(drv, path: str, *, label: str, stop_map_ids=()) -> str:
    """Walk an A*-returned direction string, resolving battles as they
    fire. Thin wrapper over ``ftb.walk_path`` for symmetry with the other
    helpers in this module."""
    return ftb.walk_path(drv, path, label=label, stop_map_ids=stop_map_ids)


# --- Patched version of rtb.Driver.resolve_battle --------------------------
# The stock resolver mashes A for 50 ticks on a faint to drive through the
# blackout dialog. We'd rather *detect* the faint and let the grinder's
# own recovery path handle it (so we can re-enter Route 2 deterministically
# via navigate_to_viridian_with_retry). The drop-in below still drives the
# battle to completion with the same PP-aware move picker but records the
# post-battle state so the caller can act on a faint.


def _battle_turn(drv: rtb.Driver, *, flee_below_hp_frac: float = 0.55,
                  force_fight: bool = False,
                  allow_learn: bool = False) -> bool:
    """Drive one battle menu turn.

    If pre-turn HP is below ``flee_below_hp_frac`` * max and this isn't
    a forced-fight retry, RUN instead of FIGHT. Running from wild
    battles is Speed-gated in Gen 1 — the caller should cap flee
    retries at ~2 per battle.

    Uses this module's expanded :data:`DAMAGING_MOVE_IDS` allowlist
    rather than the one in :mod:`run_to_brock` so Pikachu's
    ThunderShock / Double Kick are preferred on Yellow.

    Returns True if we attempted to RUN, False if we attempted to FIGHT.
    """
    # Wait for the 4-item FIGHT/PKMN/ITEM/RUN menu to be responsive.
    for _ in range(60):
        gs = drv.gs()
        if not gs.battle.active:
            return False
        mx = drv.sym.read_u8(drv.mem, "wMaxMenuItem")
        if not drv.joy_locked() and mx == 3:
            break
        drv.press("a")

    # Read wBattleMon HP if populated, else fall back to party_mon.
    # wBattleMon* can hold stale values from a prior battle (e.g.
    # hp=0 from last faint) before the new battle's transfer kicks in,
    # so trust only strictly-positive readings and fall back otherwise.
    hp_frac = 1.0
    party_hp_frac = 1.0
    try:
        m = drv.gs().party.mons[0]
        if m.max_hp > 0:
            party_hp_frac = m.hp / m.max_hp
    except IndexError:
        pass
    try:
        base_mhp = drv.sym.addr_of("wBattleMonMaxHP")
        bm_mhp = (drv.mem[base_mhp] << 8) | drv.mem[base_mhp + 1]
        base_hp = drv.sym.addr_of("wBattleMonHP")
        bm_hp = (drv.mem[base_hp] << 8) | drv.mem[base_hp + 1]
        if bm_mhp > 0 and bm_hp > 0:
            hp_frac = bm_hp / bm_mhp
        else:
            hp_frac = party_hp_frac
    except (AttributeError, LookupError, TypeError):
        hp_frac = party_hp_frac

    gs = drv.gs()
    flee = (not force_fight
            and hp_frac < flee_below_hp_frac
            and gs.battle.kind == 1)

    if flee:
        print(f"    [battle] FLEE hp_frac={hp_frac:.2f}", flush=True)
        # RUN is the bottom-right item in the 2x2 FIGHT/PKMN/ITEM/RUN menu.
        drv.press("up")
        drv.press("up")
        drv.press("left")
        drv.press("down")
        drv.press("right")
        drv.press("a", step=90)
        # Drive run-attempt dialog to completion. If the run fails the
        # menu reopens and we need to try again next turn (outer loop).
        for _ in range(30):
            gs = drv.gs()
            if not gs.battle.active:
                return True
            mx = drv.sym.read_u8(drv.mem, "wMaxMenuItem")
            if not drv.joy_locked() and mx == 3:
                return True
            drv.press("a", step=20)
        return True

    # FIGHT cursor.
    drv.press("up")
    drv.press("up")
    drv.press("left")
    drv.press("a", step=60)

    pp = drv.read_pp()
    gs = drv.gs()
    moves = list(gs.party.mons[0].moves) if gs.party.mons else [0, 0, 0, 0]
    target = None
    for i in range(4):
        if pp[i] > 0 and moves[i] in DAMAGING_MOVE_IDS:
            target = i
            break
    if target is None:
        for i in range(4):
            if pp[i] > 0:
                target = i
                break
    if target is None:
        drv.press("a", step=90)
        return False

    for _ in range(4):
        drv.press("up")
    for _ in range(target):
        drv.press("down")
    drv.press("a", step=90)

    # Advance turn outcome dialog.
    for _ in range(80):
        gs = drv.gs()
        if not gs.battle.active:
            return False
        mx = drv.sym.read_u8(drv.mem, "wMaxMenuItem")
        if not drv.joy_locked() and mx == 3:
            return False
        # Yes/No on "$mon is trying to learn $move. Delete a move to
        # make room?" — mx==1 is the 2-item YES/NO menu. Press A to
        # accept YES (needed for Pikachu's Double Kick / Bulbasaur's
        # Vine Whip).  A subsequent mx==3 "which move to forget?"
        # menu shows the 4 moves — cursor defaults to slot 0 but we
        # send DOWN to land on slot 1 (usually Growl / Tail Whip)
        # before confirming, so we don't overwrite the strong damaging
        # move the mon was given at creation.
        #
        # Exception: in trainer battles, after we KO an enemy mon and
        # the trainer has more, Gen 1 asks "Would you like to switch
        # POKÉMON?" as a YES/NO prompt. Pressing YES opens the party
        # menu (mx==0 for a one-mon party — just "CANCEL"), pressing A
        # on CANCEL returns to the YES/NO prompt → infinite loop at
        # step 123 of forest leg 0. Detect by looking at enemy HP: if
        # enemy is at full HP the prompt is a switch-offer (they just
        # sent in a fresh mon), not a learn-offer (learn fires on
        # level-up which happens during the end-of-battle XP flow
        # where enemy HP has been zeroed for a while). Press B to
        # refuse. Learn prompts still land here with enemy HP=0 (or
        # post-battle stale values), so we keep the A-press default.
        if not drv.joy_locked() and mx == 1:
            if not allow_learn:
                # Outside of the grinder (forest walk, gym, etc.) a
                # YES/NO prompt means either "switch POKÉMON?" (trainer
                # just KO'd our mon's opponent) or "learn new move?"
                # (level-up). Both are safe to refuse with B; switching
                # is a no-op we don't need, and we definitely don't
                # want to accidentally accept a learn-move prompt that
                # then requires navigating the move-to-forget menu —
                # the cursor lands on slot 0 and if we confirm with A
                # we'll overwrite whichever damaging move lives there.
                # Concrete past bug: Pikachu at L15/16 learned Double
                # Team, the grinder's A-press confirmed the forget
                # menu on slot 0 = ThunderShock, leaving Pikachu with
                # only status moves and soft-locking trainer fights.
                drv.press("b", step=20)
                continue
            # allow_learn=True (grinder phase): accept by default. Before
            # pressing A, check if this is actually a switch prompt (enemy
            # at full HP = fresh switched-in mon) and refuse if so.
            try:
                base_ehp = drv.sym.addr_of("wEnemyMonHP")
                base_emhp = drv.sym.addr_of("wEnemyMonMaxHP")
                ehp = (drv.mem[base_ehp] << 8) | drv.mem[base_ehp + 1]
                emhp = (drv.mem[base_emhp] << 8) | drv.mem[base_emhp + 1]
                enemy_at_full_hp = emhp > 0 and ehp >= emhp
            except (AttributeError, LookupError, TypeError):
                enemy_at_full_hp = False
            if enemy_at_full_hp:
                drv.press("b", step=20)
                continue
            drv.press("a", step=20)
            continue
        drv.press("a", step=20)
    return False


def _resolve_battle_no_blackout_mash(drv: rtb.Driver,
                                      *, max_turns: int = 80,
                                      allow_learn: bool = True) -> bool:
    """Drive battle to completion. Returns True if the lead mon fainted
    (caller should handle blackout), False otherwise.

    Caps flee attempts at 2 per battle — after a third failed RUN, we
    should just attack (Gen 1 run-chance is Speed-based; if it's failing
    consistently more tries won't help).

    After the battle ends, drives any level-up / move-learn dialogs by
    mashing A and selecting slot 1 (usually Growl / Tail Whip) on the
    "which move should be forgotten?" prompt so the strong damaging
    move in slot 0 stays untouched.
    """
    flee_attempts = 0
    for _ in range(max_turns):
        gs = drv.gs()
        if not gs.battle.active:
            break
        if gs.party.mons and gs.party.mons[0].hp == 0:
            _drive_post_battle_dialogs(drv, allow_learn=allow_learn)
            return True
        force_fight = flee_attempts >= 2
        fled = _battle_turn(drv, force_fight=force_fight,
                            allow_learn=allow_learn)
        if fled:
            flee_attempts += 1
    _drive_post_battle_dialogs(drv, allow_learn=allow_learn)
    return False


def _drive_post_battle_dialogs(drv: rtb.Driver,
                                allow_learn: bool = True) -> None:
    """After the battle ends, advance any level-up / EXP / move-learn
    dialogs. Exits once joyIgnore clears AND no text box is rendering.

    Gen 1's move-learn flow (when ``allow_learn=True``):
      1. "$mon wants to learn $move" — A.
      2. "Delete a move?" (mx==1 YES/NO) — A for YES.
      3. "Which move should be forgotten?" (mx==3 4-item list) —
         DOWN×2 then A overwrites slot 2 (usually the status move
         Growl/Tail Whip), keeping slot 0's damaging move intact.

    With ``allow_learn=False`` we press B on the YES/NO instead, so
    no forget menu opens. Used by Yellow's grinder where the lead
    (Pikachu) starts with ThunderShock in slot 0 and any forget-menu
    misnavigation that lands on slot 0 leaves the lead with only
    status moves — soft-locking subsequent battles.
    """
    for _ in range(80):
        gs = drv.gs()
        if gs.battle.active:
            return
        joy_locked = drv.joy_locked()
        in_text = gs.text.dest_in_vram_tilemap
        if not joy_locked and not in_text:
            return
        mx = drv.sym.read_u8(drv.mem, "wMaxMenuItem") if "wMaxMenuItem" in drv.sym else 0
        if not joy_locked and in_text and mx == 1:
            if allow_learn:
                drv.press("a", step=30)  # YES to learn
            else:
                drv.press("b", step=30)  # NO — refuse learn/switch
            continue
        if not joy_locked and in_text and mx == 3 and allow_learn:
            drv.press("down")
            drv.press("down")
            drv.press("a", step=30)
            continue
        drv.press("a", step=20)


# --- Heal-loop primitive ---------------------------------------------------


def _force_blackout_heal(drv: rtb.Driver, session: Session,
                         max_iterations: int = 20) -> bool:
    """Deliberately trigger a wild battle, force-fight, and accept the
    faint. In Gen 1 a blackout teleports the party to the last-visited
    PokéCenter (Viridian here, after the grinder's first successful
    heal) and fully restores them — equivalent to a walk-back-and-heal
    but robust against Route 2 A*-path desyncs.

    Returns True if we blackout-teleported off Route 2 (recovery
    branch in the outer loop will reset us onto Route 2), False if
    no battle fired or HP got replenished without fainting."""
    _clear_repel(drv)
    start_map = drv.gs().overworld.map_id
    for i in range(max_iterations):
        gs = drv.gs()
        if gs.overworld.map_id != start_map:
            # Blackout warped us to a different map.
            return True
        m = _lead(drv)
        if m is None:
            return False
        if not gs.battle.active:
            # Step through grass to provoke an encounter. Keep it
            # simple — down/up bounce within the grass patch.
            _ensure_in_grass(drv, session, None, None, None, None)
            for _ in range(30):
                if drv.gs().battle.active:
                    break
                if drv.gs().overworld.map_id != start_map:
                    return True
                drv.press("down" if i % 2 == 0 else "up")
        if not drv.gs().battle.active:
            continue
        # Force-fight the battle. Grind's _battle_turn with force_fight
        # keeps us attacking even at low HP; RUN gets Speed-gated
        # anyway vs some Route 2 mons.
        for _ in range(80):
            gs = drv.gs()
            if not gs.battle.active:
                break
            if gs.party.mons and gs.party.mons[0].hp == 0:
                # Fainted — mash A through blackout animation.
                for _ in range(120):
                    g = drv.gs()
                    if not g.battle.active and g.overworld.map_id != start_map:
                        return True
                    drv.press("a")
                break
            _battle_turn(drv, force_fight=True)
    # Never fainted and never warped — either HP replenished or something
    # else weird. Caller should retry normal heal or bail.
    return drv.gs().overworld.map_id != start_map


def walk_to_viridian_and_heal(
    drv: rtb.Driver,
    session: Session,
    outdir: Path,
    rom: str,
    sym: str,
    sha1: str,
) -> bool:
    """From somewhere on Route 2, walk south to Viridian, heal at the
    Pokecenter, walk back north to the Route 2 south entry. Returns True
    on success.

    Safe to call repeatedly — a no-op if HP is already full (we still
    path, heal, and walk back, but the game's heal animation is fast).
    """
    gs = drv.gs()
    start_map = gs.overworld.map_id
    print(f"  [heal] starting from map=0x{start_map:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y})", flush=True)

    # 1) Get to Viridian. From some Route 2 tiles (notably (5, 48)) the
    #    player sits below a one-way ledge in a small trapped pocket —
    #    A*'s grid model doesn't see a path out because it doesn't track
    #    ledge direction, but physically the player can step DOWN through
    #    the ledge into open terrain. When A* returns NO PATH FOUND, drop
    #    DOWN a few tiles (ledges always allow southbound) and retry. A
    #    successful retry is the normal case — the old "bail + Option-B
    #    fallback" code path is no longer reachable in practice.
    if start_map == M_ROUTE_2:
        ftb._activate_repel(drv)
        # Settle the emulator before saving state for A* — a race between
        # the grinder press loop and subprocess state read occasionally
        # serialises a partial wTilesetCollisionPtr on Yellow.
        session.step(60, render=True)
        path = None
        for attempt in range(4):
            try:
                path = _pathfind(drv, session, outdir, "8,71",
                                  f"grind_r2_south_{attempt}",
                                  rom, sym, sha1)
                print(f"  [heal] route2→south A* attempt {attempt+1}: "
                      f"{len(path)} steps", flush=True)
                break
            except RuntimeError as e:
                gs2 = drv.gs()
                print(f"  [heal] route2 pathfind attempt {attempt+1} "
                      f"at ({gs2.overworld.x},{gs2.overworld.y}) "
                      f"failed: {e}", flush=True)
                # Ledge-pocket escape: step DOWN 3× (one-way safe). If we
                # didn't move at all, we really are walled — give up.
                moved_any = False
                for _ in range(3):
                    before = (drv.gs().overworld.x, drv.gs().overworld.y)
                    drv.press("down")
                    if drv.gs().battle.active:
                        drv.resolve_battle()
                    after = (drv.gs().overworld.x, drv.gs().overworld.y)
                    if after != before:
                        moved_any = True
                if not moved_any:
                    # Tile is fully boxed in (e.g. (7, 51) on Route 2 —
                    # one-way ledge entry plus tree neighbors). Bail
                    # cleanly — caller's Option-B fallback reloads the
                    # clean viridian_to_route2 milestone and tops up
                    # the lead, so end-to-end pipeline still wins. We
                    # tried _force_blackout_heal here too but it can't
                    # take a grass-step from a fully-walled tile to
                    # trigger an encounter, so it just spun for ~100k
                    # presses before giving up.
                    print("  [heal] ledge-escape made no progress; "
                          "bailing — caller will Option-B-recover",
                          flush=True)
                    return False
                session.step(60, render=True)
        if path is None:
            print("  [heal] no route2 path after 4 attempts; bailing",
                  flush=True)
            return False
        _safe_walk(drv, path, label="grind_r2_south",
                    stop_map_ids=(M_VIRIDIAN,))
        # Cross the south border warp.
        for _ in range(4):
            if drv.gs().overworld.map_id == M_VIRIDIAN:
                break
            drv.press("down")
    elif start_map in (M_REDS_1F, M_REDS_2F, M_PALLET):
        # Blackout landed us at home. Use the existing recovery plumbing.
        print(f"  [heal] blacked out on 0x{start_map:02x}; navigate back "
              "up via navigate_to_viridian_with_retry", flush=True)
        ok = ftb.navigate_to_viridian_with_retry(
            drv, outdir, rom, sym, sha1, session, max_attempts=5,
        )
        if not ok:
            return False

    session.step(60, render=True)
    map_now = drv.gs().overworld.map_id
    if map_now != M_VIRIDIAN:
        print(f"  [heal] WARN not in Viridian after south-walk "
              f"(map=0x{map_now:02x})", flush=True)
        return False

    # 2) A* from current Viridian xy to the PC door, then cross the warp.
    ftb._activate_repel(drv)
    try:
        path = _pathfind(drv, session, outdir, "23,25", "grind_to_pc",
                          rom, sym, sha1)
        print(f"  [heal] viridian→PC A* {len(path)} steps", flush=True)
        _safe_walk(drv, path, label="grind_to_pc",
                    stop_map_ids=(M_VIRIDIAN_POKECENTER,))
    except RuntimeError as e:
        print(f"  [heal] pc pathfind failed: {e}", flush=True)
    # Step UP across the door warp if we didn't transition yet.
    for _ in range(4):
        if drv.gs().overworld.map_id == M_VIRIDIAN_POKECENTER:
            break
        drv.press("up")
    session.step(60, render=True)
    if drv.gs().overworld.map_id != M_VIRIDIAN_POKECENTER:
        print(f"  [heal] WARN failed to enter PC "
              f"(map=0x{drv.gs().overworld.map_id:02x})", flush=True)
        return False

    # 3) Talk to nurse — mirror heal_at_viridian_pokecenter's internal
    #    logic so we don't double the door-walk it performs.
    for _ in range(6):
        drv.press("up")
    drv.press("a")
    for _ in range(30):
        mx = drv.sym.read_u8(drv.mem, "wMaxMenuItem")
        if mx == 1 and not drv.gs().text.dest_in_vram_tilemap:
            break
        drv.press("a")
    drv.press("a", step=60)
    for _ in range(60):
        try:
            m = drv.gs().party.mons[0]
            if m.hp == m.max_hp:
                break
        except IndexError:
            pass
        drv.press("a")
    for _ in range(15):
        drv.press("b", step=60)

    # 4) Exit south through the warp back into Viridian.
    for _ in range(8):
        if drv.gs().overworld.map_id == M_VIRIDIAN:
            break
        drv.press("down")
    session.step(60, render=True)
    gs = drv.gs()
    m = gs.party.mons[0] if gs.party.mons else None
    print(f"  [heal] healed: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y}) "
          f"HP={m.hp}/{m.max_hp}" if m else "no party",
          flush=True)

    # 5) Walk back north to Route 2. A* to (17, 0) then UP to warp.
    ftb._activate_repel(drv)
    try:
        path = _pathfind(drv, session, outdir,
                          f"{VIRIDIAN_NORTH_EXIT[0]},{VIRIDIAN_NORTH_EXIT[1]}",
                          "grind_vi_to_r2", rom, sym, sha1)
        print(f"  [heal] viridian→r2 A* {len(path)} steps", flush=True)
        _safe_walk(drv, path, label="grind_vi_to_r2",
                    stop_map_ids=(M_ROUTE_2,))
    except RuntimeError as e:
        print(f"  [heal] vi→r2 pathfind failed: {e}", flush=True)
    for _ in range(6):
        if drv.gs().overworld.map_id == M_ROUTE_2:
            break
        drv.press("up")
    session.step(60, render=True)
    gs = drv.gs()
    print(f"  [heal] back on route2: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y})", flush=True)
    return gs.overworld.map_id == M_ROUTE_2


# --- Grass-walking / encounter roll ---------------------------------------


def _ensure_in_grass(drv: rtb.Driver,
                      session: Session | None = None,
                      outdir: Path | None = None,
                      rom: str | None = None,
                      sym: str | None = None,
                      sha1: str | None = None) -> bool:
    """Get into the Route 2 grass patch (y<=52). Tries the canonical
    hardcoded corridor first (matches ``level_up.ensure_in_grass``),
    and falls back to A* if that bumps into trees.

    Returns True if we ended up on a grass tile (GRASS_Y_MIN..MAX)."""
    # First try the canonical zig-zag. Runs in <30 presses when player
    # spawn is exactly (8, 71). Anywhere else it may stall — handled by
    # the A* fallback below.
    path = (
        ["up"] * 9       # (8, 71) -> (8, 62)
        + ["left"]       # (8, 62) -> (7, 62)
        + ["up"] * 5     # (7, 62) -> (7, 57)
        + ["left"] * 2   # (7, 57) -> (5, 57)
        + ["up"]         # (5, 57) -> (5, 56)
        + ["left"]       # (5, 56) -> (4, 56)
        + ["up"] * 5     # (4, 56) -> (4, 51)  -- into grass
        + ["right"] * 2  # (4, 51) -> (6, 51)
    )
    stall = 0
    for d in path:
        gs = drv.gs()
        if gs.battle.active:
            return True
        if gs.overworld.map_id != M_ROUTE_2:
            return False
        if drv.joy_locked():
            drv.press("a")
            continue
        cur = (gs.overworld.x, gs.overworld.y)
        drv.press(d)
        after = (drv.gs().overworld.x, drv.gs().overworld.y)
        if cur == after:
            stall += 1
            if stall >= 5:
                break
        else:
            stall = 0

    gs = drv.gs()
    if (gs.overworld.map_id == M_ROUTE_2
            and GRASS_Y_MIN <= gs.overworld.y <= GRASS_Y_MAX):
        return True

    # Fallback: A* to a known grass tile. Only runs if we got stuck.
    if session is None:
        return False
    try:
        _clear_repel(drv)  # don't want Repel on while pathing INTO grass
        path_str = _pathfind(drv, session, outdir, "6,51",
                              "grind_to_grass", rom, sym, sha1)
        print(f"  [grind] A* into-grass {len(path_str)} steps", flush=True)
        _safe_walk(drv, path_str, label="grind_to_grass")
    except RuntimeError as e:
        print(f"  [grind] into-grass pathfind failed: {e}", flush=True)
    gs = drv.gs()
    return (gs.overworld.map_id == M_ROUTE_2
            and GRASS_Y_MIN <= gs.overworld.y <= GRASS_Y_MAX)


def _walk_until_battle(drv: rtb.Driver, max_steps: int = 40) -> bool:
    """Bounce the player within the Route 2 grass patch until a wild
    encounter fires. Returns True if battle active after the walk.

    Cycles deterministically through UP / RIGHT / DOWN / LEFT so we
    always explore away from grass-edge corners even when two axes are
    blocked by trees. Encounters in Gen 1 can only fire on grass tiles
    (tile id 0x52), so we intentionally take every movement direction
    in sequence to maximise grass-stepping probability."""
    rotation = ["up", "right", "down", "left"]
    last_xy = None
    stuck_count = 0
    for i in range(max_steps):
        gs = drv.gs()
        if gs.battle.active:
            return True
        if gs.overworld.map_id != M_ROUTE_2:
            return False
        if drv.joy_locked():
            drv.press("a")
            continue
        y = gs.overworld.y
        x = gs.overworld.x
        # Prefer staying inside grass. When already there, step
        # through all four directions in rotation — each step is a
        # grass-or-adjacent candidate.
        if y < GRASS_Y_MIN:
            d = "down"
        elif y > GRASS_Y_MAX:
            d = "up"
        elif x > 6:
            # Drift east leads to (7, 51) and beyond — a one-way ledge
            # entry where the heal-path A* fails because the surrounding
            # tiles are trees. Bias LEFT to stay near the safe grass
            # corridor x∈[4..6].
            d = "left"
        elif x < 4:
            d = "right"
        else:
            d = rotation[i % 4]
        before = (gs.overworld.x, gs.overworld.y)
        drv.press(d)
        if drv.gs().battle.active:
            return True
        after = (drv.gs().overworld.x, drv.gs().overworld.y)
        # Track persistent stalls: if we've stayed at the same tile
        # for 6 presses in a row, try every direction once to escape.
        if after == before:
            if last_xy == before:
                stuck_count += 1
            else:
                stuck_count = 1
            if stuck_count >= 4:
                for altd in rotation:
                    drv.press(altd)
                    if drv.gs().battle.active:
                        return True
                    if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
                        break
                stuck_count = 0
        else:
            stuck_count = 0
        last_xy = (drv.gs().overworld.x, drv.gs().overworld.y)
    return drv.gs().battle.active

# ``full_to_brock`` imports this module back (``import grind``) at import time,
# so these are bound after the definitions above to avoid handing a partially
# initialized module to the circular importer.
import full_to_brock as ftb
import run_to_brock as rtb
