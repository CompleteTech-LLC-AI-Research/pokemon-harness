"""Bounded ordinary Red six-party continuation to Cerulean Club; no link claim."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from pokered_harness.symbols.loader import read_wram_u8
from scripts.produce_normal_journey import NormalJourney, ObservedDriver, foundation, tiles

ORACLE_SHA256 = {
    "constants/event_constants.asm": "691a373649740af5ea340d09e816b0966e9164d5f78ded57b1759e76ada57455",
    "constants/item_constants.asm": "b5a1db015fbf0de637381eb39350f21f35a436a8088e2f6657f65c79f118f97f",
    "constants/map_constants.asm": "4129ef4e6908267e554c0308254f2269f1fb8f75b5c529faad6fd14c92119271",
    "constants/trainer_constants.asm": "27d774417a45f20d127d96fcb07b4625f2fc92fe4ffbc79efebee86c90b79a58",
    "data/maps/headers/Route3.asm": "32d67a918bf3777d7cd142f988d26cce0da03256bf9e7d8f229e44d6d11d160b",
    "data/maps/headers/Route4.asm": "1f7791041324cfe86ceeea513bafe1ae068516fd63a2b5a5ed1369a3d9e701d6",
    "data/maps/objects/CeruleanCity.asm": "c28fa2a17d5649d7a6c0a03a3bfa6a25203e63ddd5cd7423c5b6b1ab5dae8cb3",
    "data/maps/objects/CeruleanPokecenter.asm": "6cfdfc89cf2b9f87b524f07128442b2de6c9a28bdc28a17f2b4f80790ce87e10",
    "data/maps/objects/MtMoon1F.asm": "1539b0d5b3b21a247476391126b196baa04cbbf2c918e2f2abe1027b4a53e038",
    "data/maps/objects/MtMoonB1F.asm": "29c88a22c278bf68ad2ec0a7b1329c1e221258546c3014d1a24fd26ba28d6477",
    "data/maps/objects/MtMoonB2F.asm": "0c676edaf92b3bdcf1ef5e9ca5b2bc3b6330849602764aa6cf15fa6a69392c61",
    "data/maps/objects/PewterCity.asm": "0c98a4b6dbfbbf25d50717d48ca521240e58a6dff89574589a08c5ad25247c81",
    "data/maps/objects/PewterGym.asm": "5c7fb37602bdab9b8f51af52a38e09dfa1e05555b8aabb01a2a3fec2b02d36be",
    "data/maps/objects/PewterPokecenter.asm": "12f78c90e6bf0015fd7b83614fe8aeb16a064c966b2009eeabb9743a441a817a",
    "data/maps/objects/Route3.asm": "ab1c7d4d6b3dfd7437a65e7b58c6ed83932cb6313b674ddb43f325c872d803f1",
    "data/maps/objects/Route4.asm": "ae90dabec1af40a964b5376b70c00834abbe6a8b8ec590af5edb5e9a7112d721",
    "data/maps/objects/ViridianCity.asm": "b1aa24caeb3ba6a6e59c8c7ad544303ada71bc851e4d35371a51a5dd580d93c0",
    "data/maps/objects/ViridianForest.asm": "9e1e23ac1f52f797464b8aabd4efa14e90caddf67910ad441fe8a815db7aa627",
    "data/maps/objects/ViridianForestNorthGate.asm": "e334f404dd063045187792baae3665a90ff2f8139fa7831a88e2bbf5282d300d",
    "data/pokemon/evos_moves.asm": "1a4c475c47134440a4a4e54265314638d8c6953da558d7d6a7b7f7bc206c2f30",
    "data/tilesets/pair_collision_tile_ids.asm": "d7ed744bfa0fcaf8e7f69a23f9e3d44cdcf9f3c3f9931d93926f5084f1c4466b",
    "engine/battle/core.asm": "928a66d1ac444fc913c7c658d9f7739eb6351e797b3276da64a5de9682a57a5f",
    "engine/battle/move_effects/leech_seed.asm": "ee58f9d3462ada1aaf49e3452afd3c92b4c70c9e4b0041c8a5cbed1bde05d234",
    "engine/events/pokecenter.asm": "55903bede37954620e23375f2b387bb13b601bb8ae062f282a20eb7afd36c82c",
    "engine/overworld/trainer_sight.asm": "46b748a6c991ec7a7bb6fb65ca217d4fb6b8ebdb274c6105d2991c7b92c099fd",
    "gfx/blocksets/cavern.bst": "a097f4128798fa5a35c58a42b699a776b7c27ae284e2a0d0a68dc446145a0f7a",
    "gfx/blocksets/forest.bst": "c89e781078a80b59bdbda6aa74f57e96fe4b0837192a1be65c5d12b00a42ef89",
    "gfx/blocksets/gym.bst": "3119e3db40900f277fd2680078c109f5a5556b928f2691265dd7917b08c06edc",
    "gfx/blocksets/overworld.bst": "c4f6d88cc7dea8196aa51d3aa059c6d1da0ea45718dd12870fcddee9f9304d50",
    "gfx/blocksets/pokecenter.bst": "0dd8c8f693882a208b39487ba2b97505910f9e22b8f7108e11f0efe7405f61c9",
    "gfx/blocksets/reds_house.bst": "c1da7929e1487c26be83f4e2d8e5bf378dc4bc3651adfce3a48003b4d9ee9235",
    "home/overworld.asm": "ce1ebcec0d71af2fb61369295ad180bb51f18a5ba13711035524011bd0c7a849",
    "scripts/MtMoon1F.asm": "ccb7b0b62ba3e49b9281512293eda7cdd746d669af7f0458753b9d110ce89341",
    "scripts/MtMoonB2F.asm": "0d4720a3d824948ab1f017a3556726bdc81780219a5acbc3a4146b3b8c703c10",
    "scripts/PewterCity.asm": "24d6d17f5d0668a6ef2c66e27796ae6c8b5dca049ff0e04381f42749670f3db4",
    "scripts/Route3.asm": "2a599b54c30ce4e44eea408fd92903b936acdaa27de41fa66f8f117255b636d4",
}


class GuardedRouteDriver(ObservedDriver):
    def _append(self, row):
        super()._append(row)
        if row["status"] == "completed":
            state = self.session.read_game_state()
            active = state.party.active_mon
            if state.battle.active and active is not None and active.hp_valid and active.hp <= 0:
                raise foundation.CaptureRefused(
                    "observed active faint: retain boundary, stop route"
                )

    def press(self, button, **kwargs):
        state = self.session.read_game_state()
        if (
            button == "a"
            and kwargs.get("note") == "trainer: advance outcome/encounter text"
            and state.battle.active
            and state.battle.kind.name == "TRAINER"
            and state.battle.menu_open is False
        ):
            flag = read_wram_u8(
                self.session._pyboy.memory, self.session.symbols.addr_of("wPartyMenuAnimMonEnabled")
            )
            if flag == 64 and state.party.active_mon and state.party.active_mon.hp > 0:
                return super().press(
                    "b",
                    note="trainer: decline ROM-proven active optional party selector",
                    step_ticks=30,
                )
        return super().press(button, **kwargs)


class ClubJourney(NormalJourney):
    def ordinary_trainer_battle(self):
        before_overworld = self.overworld_returns
        for _ in range(600):
            state = self.session.read_game_state()
            if not state.battle.active:
                self.finish_trainer_return(before_overworld)
                return
            active = state.party.active_mon
            if active is not None and active.hp == 0:
                raise foundation.CaptureRefused(
                    "ordinary trainer battle fainted; no success claimed"
                )
            if state.battle.menu_open is None:
                raise foundation.CaptureRefused("trainer command observation unavailable")
            if (
                state.battle.menu_open is True
                and state.menu.watched_keys == 199
                and state.menu.max_item == 5
                and not self.driver.input_locked()
                and read_wram_u8(
                    self.session._pyboy.memory, self.session.symbols.addr_of("wMoveMenuType")
                )
                == 0
            ):
                if state.menu.current_item == 0:
                    self.driver.press(
                        "a", note="trainer: dismiss observed zero-index no-PP dialog", step_ticks=30
                    )
                    continue
                target = self.choose_move_slot(state) + 1
                for _ in range(8):
                    observed = self.session.read_game_state()
                    if (
                        observed.battle.menu_open is not True
                        or observed.menu.watched_keys != 199
                        or observed.menu.max_item != 5
                    ):
                        raise foundation.CaptureRefused("regular move cursor boundary changed")
                    if observed.menu.current_item == target:
                        break
                    self.driver.press(
                        "down", note="trainer: PP-qualified regular move cursor", step_ticks=30
                    )
                else:
                    raise foundation.CaptureRefused(
                        "regular move cursor did not reach PP-qualified slot"
                    )
                self.driver.press(
                    "a", note="trainer: commit PP-qualified regular move", step_ticks=90
                )
                continue
            if (
                state.battle.menu_open is True
                and state.menu.max_item == 1
                and (state.menu.watched_keys in (17, 33))
                and (not self.driver.input_locked())
            ):
                if active is None or not active.valid:
                    raise foundation.CaptureRefused("active combatant is not readable")
                if (
                    active is not None
                    and active.hp_valid
                    and (active.hp <= 8)
                    and (state.bag.has_item(20) is True)
                ):
                    hp_before = active.hp
                    for key in ("up", "left", "down"):
                        self.driver.press(key, note="trainer: ITEM column/Potion", step_ticks=30)
                    observed = self.session.read_game_state().menu
                    if observed.watched_keys != 17 or observed.current_item != 1:
                        raise foundation.CaptureRefused(
                            "ordinary ITEM cursor not observed in left lower command cell"
                        )
                    self.driver.press("a", note="trainer: open ordinary bag", step_ticks=60)
                    for _ in range(24):
                        current = self.session.read_game_state()
                        if (
                            current.party.active_mon is not None
                            and current.party.active_mon.hp > hp_before
                        ):
                            if current.bag.has_item(20) is not False:
                                raise foundation.CaptureRefused(
                                    "Potion healing lacked observed consumption"
                                )
                            self.checkpoint("ordinary_forest_trainer_potion_applied")
                            break
                        self.driver.press(
                            "a", note="trainer: apply single held Potion to lead", step_ticks=30
                        )
                    else:
                        raise foundation.CaptureRefused(
                            "held Potion did not heal through ordinary input"
                        )
                    continue
                for key in ("up", "up", "left"):
                    self.driver.press(key, note="trainer: choose FIGHT", step_ticks=30)
                self.driver.press("a", note="trainer: open move menu", step_ticks=60)
                state = self.session.read_game_state()
                target = self.choose_move_slot(state) + 1
                for _ in range(8):
                    current = self.session.read_game_state().menu.current_item
                    if current == target:
                        break
                    self.driver.press("down", note="trainer: observed move cursor", step_ticks=30)
                else:
                    raise foundation.CaptureRefused("move cursor did not reach PP-qualified slot")
                self.driver.press("a", note="trainer: commit observed ordinary move", step_ticks=90)
            else:
                self.driver.press(
                    "a", note="trainer: advance outcome/encounter text", step_ticks=30
                )
        raise foundation.CaptureRefused("ordinary trainer battle input bound exhausted")

    def finish_trainer_return(self, before):
        for _ in range(160):
            state = self.session.read_game_state()
            if self.overworld_returns > before:
                if state.battle.active:
                    raise foundation.CaptureRefused(
                        "another encounter before post-trainer overworld witness"
                    )
                self.checkpoint("ordinary_trainer_returned_overworld")
                return
            if state.battle.active:
                raise foundation.CaptureRefused("battle reactivated during trainer return")
            self.driver.press(
                "a",
                note="trainer: finish observed award/evolution dialog until ROM overworld witness",
                step_ticks=60,
            )
        raise foundation.CaptureRefused("post-trainer overworld execution witness unavailable")

    def choose_move_slot(self, state):
        active, enemy = state.party.active_mon, state.battle.enemy_mon
        if not active or not active.valid or not enemy or not enemy.valid:
            raise foundation.CaptureRefused("route move and target observations unavailable")
        preferred = (
            33 if enemy.type1 in (2, 3, 7, 20, 22) or enemy.type2 in (2, 3, 7, 20, 22) else 22
        )
        disabled = (
            read_wram_u8(
                self.session._pyboy.memory, self.session.symbols.addr_of("wPlayerDisabledMove")
            )
            & 15
        )
        for move in (preferred, 33, 22):
            for i, (candidate, pp) in enumerate(zip(active.moves, active.pp)):
                if candidate == move and pp & 63 and i + 1 != disabled:
                    return i
        raise foundation.CaptureRefused("ordinary route damaging PP exhausted")

    def _tile_navigate(self, goal, expected_map, transition=None, exit_direction="up"):
        unchanged = 0
        collision_pending = 0
        for _ in range(240):
            state = self.session.read_game_state()
            if transition is not None and state.overworld.map_id == transition:
                return
            if state.battle.active:
                self.settle_encounter()
                continue
            if state.overworld.map_id != expected_map:
                raise foundation.CaptureRefused(
                    "navigation left its admitted map or entered battle"
                )
            if self.driver.input_locked():
                self.driver.press("a", note="navigation: finish map text")
                continue
            before = (state.overworld.x, state.overworld.y)
            if before == goal and transition is None:
                return
            if (
                expected_map == 51
                and before == (1, 18)
                and (state.battle.engaged_trainer_class == 202)
            ):
                self.driver.press("right", note="forest: face observed Bug Catcher")
                self.driver.press(
                    "a", note="forest: advance observed Bug Catcher challenge", step_ticks=60
                )
                continue
            if (
                expected_map == 54
                and before == (4, 6)
                and (state.battle.engaged_trainer_class == 205)
            ):
                self.driver.press("left", note="gym: face observed Jr Trainer")
                self.driver.press(
                    "a", note="gym: advance observed Jr Trainer challenge", step_ticks=60
                )
                continue
            mem = self.session._pyboy.memory
            read = lambda name, memory=mem: self.session.symbols.read_u8(memory, name)
            width, height, tileset = (
                read("wCurMapWidth"),
                read("wCurMapHeight"),
                read("wCurMapTileset"),
            )
            blocks = tiles.read_overworld_map(self.session, width, height)
            patterns = tiles.load_blockset(tileset, self.game_source)
            grid = tiles.expand_to_tile_grid(blocks, patterns)
            try:
                passable = tiles.read_passable_tiles(self.session)
                collision_pending = 0
            except RuntimeError:
                collision_pending += 1
                if collision_pending > 8:
                    raise
                self.driver.press(
                    "a", note="navigation: settle pending ROM map/dialog", step_ticks=60
                )
                continue
            blockers = tiles.read_sprite_blockers(self.session)
            walkable = [
                [
                    grid[y * 2 + 1][x * 2] in passable and (x, y) not in blockers
                    for x in range(width * 2)
                ]
                for y in range(height * 2)
            ]
            walkable[before[1]][before[0]] = True
            walkable[goal[1]][goal[0]] = True
            path = tiles.astar(
                walkable,
                before,
                goal,
                tile_grid=grid,
                pair_collisions=tiles._PAIR_COLLISIONS.get(tileset),
            )
            if not path:
                if before == goal and transition is not None:
                    self.driver.press(exit_direction, note="navigation: cross door boundary")
                    continue
                if (
                    expected_map == 51
                    and before == (1, 18)
                    and (state.battle.engaged_trainer_class == 202)
                ):
                    self.driver.press(
                        "a", note="forest: advance observed Bug Catcher challenge", step_ticks=60
                    )
                    continue
                raise foundation.CaptureRefused("no readable ordinary tile path")
            self.driver.press(
                {"u": "up", "d": "down", "l": "left", "r": "right"}[path[0]],
                note="tile path: " + path[0],
            )
            after = self.session.read_game_state().overworld
            unchanged = unchanged + 1 if (after.x, after.y) == before else 0
            if unchanged >= 8:
                raise foundation.CaptureRefused("tile path did not move within bound")
        raise foundation.CaptureRefused("navigation action bound exhausted")

    def navigate(self, goal, expected_map, transition=None, exit_direction="up"):
        for attempt in range(10):
            try:
                return self._tile_navigate(goal, expected_map, transition, exit_direction)
            except foundation.CaptureRefused as exc:
                if str(exc) not in (
                    "no readable ordinary tile path",
                    "tile path did not move within bound",
                ) or expected_map not in (14, 59, 61):
                    raise
                state = self.session.read_game_state()
                if state.battle.active or state.overworld.map_id != expected_map:
                    raise
                mem, symbols = self.session._pyboy.memory, self.session.symbols
                offset = read_wram_u8(mem, symbols.addr_of("wTrainerSpriteOffset"))
                layout = (
                    {
                        2: (202, 4, 10, 6, 2),
                        3: (201, 1, 14, 4, 3),
                        4: (203, 1, 16, 9, 2),
                        5: (202, 5, 19, 5, 1),
                        6: (203, 2, 23, 4, 4),
                        7: (201, 2, 22, 9, 3),
                        8: (202, 6, 24, 6, 3),
                        9: (203, 3, 33, 10, 2),
                    }
                    if expected_map == 14
                    else {
                        1: (209, 1, 5, 6, 2),
                        2: (201, 3, 12, 16, 3),
                        3: (203, 5, 30, 4, 3),
                        4: (208, 1, 24, 31, 3),
                        5: (203, 6, 16, 23, 3),
                        6: (202, 7, 7, 22, 3),
                        7: (202, 8, 30, 27, 3),
                    }
                    if expected_map == 59
                    else {
                        1: (208, 2, 12, 8, 1),
                        2: (230, 1, 11, 16, 4),
                        3: (230, 2, 15, 22, 4),
                        4: (230, 3, 29, 11, 4),
                        5: (230, 4, 29, 17, 4),
                    }
                )
                eligible = {k: v[:2] for k, v in layout.items()}
                static = {v[:2]: v[2:4] for v in layout.values()}
                trainer = (state.battle.engaged_trainer_class, state.battle.engaged_trainer_set)
                positions = []
                source_slot = next(
                    (slot for slot, pair in eligible.items() if pair == trainer), None
                )
                if source_slot is not None:
                    live_address = symbols.addr_of("wSpriteStateData2") + source_slot * 16
                    positions.append(
                        (
                            read_wram_u8(mem, live_address + 5) - 4,
                            read_wram_u8(mem, live_address + 4) - 4,
                        )
                    )
                if not offset % 16 and eligible.get(offset // 16) == trainer:
                    address = symbols.addr_of("wSpriteStateData2") + offset
                    positions.append(
                        (read_wram_u8(mem, address + 5) - 4, read_wram_u8(mem, address + 4) - 4)
                    )
                if trainer in static:
                    positions.append(static[trainer])
                facing = None
                for x, y in positions:
                    delta = (x - state.overworld.x, y - state.overworld.y)
                    facing = {(0, -1): "up", (0, 1): "down", (-1, 0): "left", (1, 0): "right"}.get(
                        delta
                    )
                    if facing:
                        break
                if facing is None:
                    sight_ranges = {v[:2]: v[4] for v in layout.values()}
                    nearby = any(
                        (
                            (x == state.overworld.x or y == state.overworld.y)
                            and 0
                            < abs(x - state.overworld.x) + abs(y - state.overworld.y)
                            <= sight_ranges.get(trainer, 0)
                        )
                        for x, y in positions
                    )
                    if nearby:
                        self.receipt.setdefault(
                            "sourceidentified_trainer_approach_waits", []
                        ).append(
                            {
                                "trainer": trainer,
                                "positions": positions,
                                "player": [state.overworld.x, state.overworld.y],
                                "idle_frames": 90,
                            }
                        )
                        self.driver.idle(90, render=True)
                        continue
                    raise
                self.receipt["last_route3_pending_readback"] = {
                    "sprite_offset": offset,
                    "trainer": trainer,
                    "positions": positions,
                    "player": [state.overworld.x, state.overworld.y],
                    "raw_route_script": read_wram_u8(
                        mem,
                        symbols.addr_of(
                            {
                                14: "wRoute3CurScript",
                                59: "wMtMoon1FCurScript",
                                61: "wMtMoonB2FCurScript",
                            }[expected_map]
                        ),
                    ),
                }
                self.checkpoint("observed_adjacent_route3_trainer")
                self.driver.press(
                    facing,
                    note="Route3: face sourceidentified adjacent pending trainer",
                    step_ticks=30,
                )
                for _ in range(120):
                    if self.session.read_game_state().battle.active:
                        self.ordinary_trainer_battle()
                        break
                    self.driver.press(
                        "a", note="Route3: engage actual adjacent trainer dialog", step_ticks=60
                    )
                else:
                    raise foundation.CaptureRefused(
                        "sourceidentified adjacent trainer did not engage within120inputs"
                    )
        raise foundation.CaptureRefused("Route3 pending trainer retry bound exhausted")


def verify_parent(source_state, parent_receipt, *, state_sha1, state_sha256, receipt_sha256):
    """Check supplied pins and exact inclusion in the pinned parent receipt."""
    raw = source_state.read_bytes()
    parent_raw = parent_receipt.read_bytes()
    if (
        hashlib.sha1(raw).hexdigest() != state_sha1
        or hashlib.sha256(raw).hexdigest() != state_sha256
        or hashlib.sha256(parent_raw).hexdigest() != receipt_sha256
    ):
        raise foundation.CaptureRefused("operator parent state/receipt pins do not match")
    parent = json.loads(parent_raw)
    entries = [
        x
        for x in parent.get("checkpoints", [])
        if x.get("sha1") == state_sha1
        and x.get("sha256") == state_sha256
        and x.get("size") == len(raw)
        and x.get("name") == source_state.stem
    ]
    if (
        len(entries) != 1
        or source_state.stem.startswith("failed_")
        or parent.get("session_closed_without_save") is not True
    ):
        raise foundation.CaptureRefused(
            "source checkpoint not uniquely retained in closed parent receipt"
        )
    return raw, parent


def verify_oracles(game_source):
    for name, expected in ORACLE_SHA256.items():
        path = game_source / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise foundation.CaptureRefused("pinned consulted pret source does not match: " + name)


def require_six_party(state, *, healed=False):
    party = state.party
    if (
        not party.valid
        or len(party.mons) != 6
        or not party.sentinel_valid
        or any(not m.valid or not m.hp_valid or m.hp <= 0 for m in party.mons)
    ):
        raise foundation.CaptureRefused("six coherent living party records not observed")
    for mon in party.mons:
        if healed and (mon.hp != mon.max_hp or mon.status.raw != 0):
            raise foundation.CaptureRefused("all six normal Center full HP/status not observed")
        if not any(move and (pp & 63) for move, pp in zip(mon.moves, mon.pp)):
            raise foundation.CaptureRefused("living party member lacks an available move")


def run_club_route(journey):
    journey.driver.phase = "ordinary_six_party_route3"
    state = journey.session.read_game_state()
    require_six_party(state)
    if state.battle.active or (state.overworld.map_id, state.overworld.x, state.overworld.y) != (
        13,
        7,
        2,
    ):
        raise foundation.CaptureRefused("expected ordinary six-party Route2 checkpoint13,7,2")
    journey.checkpoint("ordinary_six_party_parent_reloaded")
    journey.navigate((8, 0), 13, transition=2)
    journey.driver.idle(60, render=True)
    journey.normal_center_heal(2, 58, (13, 25))
    require_six_party(journey.session.read_game_state(), healed=True)
    journey.checkpoint("ordinary_six_party_full_hp_pewter")
    journey.navigate((39, 18), 2, transition=14, exit_direction="right")
    journey.driver.idle(60, render=True)
    journey.navigate((59, 0), 14, transition=15)
    journey.driver.idle(60, render=True)
    journey.normal_center_heal(15, 68, (11, 5))
    journey.checkpoint("ordinary_mt_moon_center_healed")
    journey.driver.phase = "ordinary_mt_moon_to_cerulean"
    journey.navigate((18, 5), 15, transition=59)
    journey.driver.idle(60, render=True)
    journey.navigate((5, 5), 59, transition=60)
    journey.driver.idle(60, render=True)
    journey.navigate((21, 17), 60, transition=61)
    journey.driver.idle(60, render=True)
    journey.navigate((13, 8), 61)
    mem, symbols = journey.session._pyboy.memory, journey.session.symbols
    event = lambda number: bool(
        read_wram_u8(mem, symbols.addr_of("wEventFlags") + number // 8) & (1 << (number % 8))
    )
    for _ in range(120):
        if event(0x579):
            break
        current = journey.session.read_game_state()
        if current.battle.active:
            journey.ordinary_trainer_battle()
        else:
            journey.driver.press(
                "a", note="MtMoon: source-identified guardian challenge", step_ticks=60
            )
    else:
        raise foundation.CaptureRefused("MtMoon guardian victory event not observed")
    journey.checkpoint("ordinary_mt_moon_guardian_beaten")
    journey.navigate((13, 7), 61)
    journey.driver.press("up", note="MtMoon: face source Helix Fossil13,6", step_ticks=30)
    for _ in range(80):
        current = journey.session.read_game_state()
        if current.battle.active or current.overworld.map_id != 61:
            raise foundation.CaptureRefused("fossil interaction boundary changed")
        script = read_wram_u8(mem, symbols.addr_of("wMtMoonB2FCurScript"))
        if event(0x57F) and current.bag.has_item(42) is True and script == 0:
            break
        if (
            current.menu.watched_keys == 3
            and current.menu.max_item == 1
            and current.menu.current_item == 1
        ):
            journey.driver.press("up", note="MtMoon: observed Yes cursor", step_ticks=30)
        else:
            journey.driver.press(
                "a", note="MtMoon: source-identified Helix dialogue", step_ticks=60
            )
    else:
        raise foundation.CaptureRefused("Helix item/event/script completion not observed")
    journey.checkpoint("ordinary_helix_fossil_received")
    journey.navigate((5, 7), 61, transition=60)
    journey.driver.idle(60, render=True)
    journey.navigate((27, 3), 60, transition=15)
    journey.driver.idle(60, render=True)
    journey.navigate((89, 10), 15, transition=3, exit_direction="right")
    journey.driver.idle(60, render=True)
    journey.normal_center_heal(3, 64, (19, 17))
    journey.navigate((19, 17), 3, transition=64)
    journey.driver.idle(60, render=True)
    journey.navigate((11, 3), 64)
    final = journey.session.read_game_state()
    require_six_party(final, healed=True)
    if final.battle.active or (final.overworld.map_id, final.overworld.x, final.overworld.y) != (
        64,
        11,
        3,
    ):
        raise foundation.CaptureRefused("Cerulean Club starting point64,11,3 not observed")
    journey.checkpoint("ordinary_cerulean_six_living_club_ready")
    journey.receipt["cerulean_six_party_club_position"] = True


def committed_source_identity():
    """Require this producer and its imported helpers to equal committed bytes."""
    root = Path(__file__).resolve().parents[1]
    paths = [
        Path(__file__),
        Path(sys.modules[NormalJourney.__module__].__file__),
        Path(foundation.__file__),
        Path(tiles.__file__),
    ]
    try:
        commit = (
            subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=10)
            .decode()
            .strip()
        )
        for path in paths:
            relative = path.resolve().relative_to(root).as_posix()
            committed = subprocess.check_output(
                ["git", "-C", str(root), "show", commit + ":" + relative], timeout=10
            )
            if committed != path.read_bytes():
                raise foundation.CaptureRefused(
                    "producer/helper differs from committed source: " + relative
                )
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise foundation.CaptureRefused("committed source identity unavailable") from exc
    return commit


def capture(
    rom,
    symbols,
    output,
    game_source,
    source_state,
    parent_receipt,
    *,
    state_sha1,
    state_sha256,
    receipt_sha256,
    seconds=600,
):
    state_bytes, parent = verify_parent(
        source_state,
        parent_receipt,
        state_sha1=state_sha1,
        state_sha256=state_sha256,
        receipt_sha256=receipt_sha256,
    )
    verify_oracles(game_source)
    commit = committed_source_identity()
    journey = ClubJourney(rom, symbols, output, game_source, seconds=seconds)
    receipt = journey.receipt
    receipt.update(
        {
            "scope": "Ordinary Red six-party continuation to Cerulean Club; not strict link acceptance",
            "source_state": str(source_state),
            "source_state_sha1": state_sha1,
            "source_state_sha256": state_sha256,
            "parent_receipt_sha256": receipt_sha256,
            "parent_phase_status": parent.get("status"),
            "fresh_boot": False,
            "producer_commit": commit,
            "explicit_parent_state_loaded": True,
            "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "journey_helper_sha256": hashlib.sha256(
                Path(sys.modules[NormalJourney.__module__].__file__).read_bytes()
            ).hexdigest(),
            "consulted_source_sha256": dict(ORACLE_SHA256),
            "no_link_or_release_claim": True,
        }
    )
    try:
        pins = journey.pins
        journey.session = foundation.Session.from_files(
            rom,
            symbols,
            expected_rom_sha1=pins.sha1_for_path(foundation.ROM_LABEL),
            expected_symbol_sha1=pins.symbol_sha1_for_path(foundation.SYM_LABEL),
            expected_pyboy_version=pins.pyboy_version,
            expected_pyboy_revision=pins.pyboy_revision,
        )
        journey.session.load_state(state_bytes)
        foundation.register_default_hooks(journey.session)
        receipt["public_observers_enabled"] = {
            "menu": journey.session.enable_battle_menu_observation(),
            "resolution": journey.session.enable_battle_resolution_observation(),
            "end": journey.session.enable_battle_end_observation(),
        }
        if any(v is not True for v in receipt["public_observers_enabled"].values()):
            raise foundation.CaptureRefused("public battle observation initialization unavailable")
        journey.overworld_returns = 0

        def returned(_context):
            journey.overworld_returns += 1

        bank, address = journey.session.symbols.bank_addr("OverworldLoop")
        journey.session._pyboy.hook_register(bank, address, returned, None)
        receipt["runtime_origin"] = sys.modules["pyboy.pyboy"].__file__
        journey.driver = GuardedRouteDriver(
            journey.session, output, deadline=journey.started + journey.seconds
        )
        run_club_route(journey)
        receipt["status"] = "captured"
    except BaseException as exc:
        receipt["status"] = "failed"
        receipt["error"] = type(exc).__name__ + ": " + str(exc)
        if journey.session:
            try:
                journey.checkpoint("failed_observed_boundary")
            except BaseException as secondary:  # noqa: BLE001 - preserve primary controller failure
                receipt["failure_checkpoint_error"] = repr(secondary)
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - journey.started
        receipt["actions_attempted"] = journey.driver.actions if journey.driver else 0
        try:
            if journey.session:
                journey.session.close(save=False)
                receipt["session_closed_without_save"] = True
        except BaseException as exc:
            receipt["session_closed_without_save"] = False
            receipt["status"] = "failed"
            receipt["teardown_error"] = repr(exc)
            raise
        finally:
            journey.write_receipt()
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("rom", "symbols", "output", "game-source", "source-state", "parent-receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("state-sha1", "state-sha256", "receipt-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--seconds", type=float, default=600)
    args = parser.parse_args()
    capture(
        args.rom,
        args.symbols,
        args.output,
        args.game_source,
        args.source_state,
        args.parent_receipt,
        state_sha1=args.state_sha1,
        state_sha256=args.state_sha256,
        receipt_sha256=args.receipt_sha256,
        seconds=args.seconds,
    )


if __name__ == "__main__":
    main()
