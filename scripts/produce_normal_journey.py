"""Capture fresh normal Red gameplay through Brock using bounded ordinary input.

Assets, checkpoints and the complete input journal remain operator managed.
This producer is not a trade, link-battle or release qualification gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

from pokered_harness.symbols.loader import read_wram_u8
from scripts import path_from_tiles as tiles
from scripts import produce_normal_foundation as foundation

ROOT = Path(__file__).resolve().parents[1]
GAME_SOURCE_COMMIT = "fbcf7d0e19a3a2db505440d3ccd3d40ca996c15c"
KNOWN_BASE_PP = {33: 35, 45: 40, 73: 10, 22: 10}
BLOCKSET_SHA256 = {
    "overworld.bst": "c4f6d88cc7dea8196aa51d3aa059c6d1da0ea45718dd12870fcddee9f9304d50",
    "reds_house.bst": "c1da7929e1487c26be83f4e2d8e5bf378dc4bc3651adfce3a48003b4d9ee9235",
    "pokecenter.bst": "0dd8c8f693882a208b39487ba2b97505910f9e22b8f7108e11f0efe7405f61c9",
    "forest.bst": "c89e781078a80b59bdbda6aa74f57e96fe4b0837192a1be65c5d12b00a42ef89",
    "gym.bst": "3119e3db40900f277fd2680078c109f5a5556b928f2691265dd7917b08c06edc",
}


class ObservedDriver(foundation.JournalDriver):
    def _append(self, row):
        if row["status"] == "completed" and self.phase == "ordinary_forest_to_pewter":
            state = self.session.read_game_state()
            row["battle_observation"] = foundation.to_jsonable(state.battle)
            row["menu_observation"] = foundation.to_jsonable(state.menu)
        super()._append(row)


class NormalJourney:
    def __init__(self, rom, symbols, output, game_source, *, seconds=600):
        if isinstance(seconds, bool) or not math.isfinite(seconds) or (not 0 < seconds <= 3600):
            raise foundation.CaptureRefused(
                "wall budget must be finite, positive and at most 3600 seconds"
            )
        foundation.require_fresh_inputs(rom, symbols, output)
        self.versions = ROOT / "VERSIONS.md"
        pins = foundation.load_versions(self.versions)
        expected = (
            pins.sha1_for_path(foundation.ROM_LABEL),
            pins.symbol_sha1_for_path(foundation.SYM_LABEL),
        )
        actual = (
            hashlib.sha1(rom.read_bytes()).hexdigest(),
            hashlib.sha1(symbols.read_bytes()).hexdigest(),
        )
        if not all(expected) or actual != expected:
            raise foundation.CaptureRefused("canonical Red-color ROM/SYM pins do not match")
        for name, expected_hash in BLOCKSET_SHA256.items():
            path = game_source / "gfx" / "blocksets" / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise foundation.CaptureRefused("pinned external blockset does not match: " + name)
        output.mkdir(parents=True, exist_ok=False)
        self.rom, self.symbols, self.output = (rom, symbols, output)
        self.game_source = game_source
        self.pins = pins
        self.started, self.seconds = (time.monotonic(), seconds)
        self.session, self.driver, self.training_wild = (None, None, False)
        self.has_run = False
        self.receipt = {
            "status": "starting",
            "scope": "Fresh ordinary-input Red-color through Boulder Badge; not a full gameplay gate",
            "source_state": None,
            "inherited_battery_ram": False,
            "controller_ram_writes": False,
            "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "foundation_sha256": hashlib.sha256(Path(foundation.__file__).read_bytes()).hexdigest(),
            "tiles_helper_sha256": hashlib.sha256(Path(tiles.__file__).read_bytes()).hexdigest(),
            "rom_sha1": actual[0],
            "sym_sha1": actual[1],
            "rom_sha256": hashlib.sha256(rom.read_bytes()).hexdigest(),
            "sym_sha256": hashlib.sha256(symbols.read_bytes()).hexdigest(),
            "versions_sha256": hashlib.sha256(self.versions.read_bytes()).hexdigest(),
            "python": sys.version,
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "blockset_source_commit": GAME_SOURCE_COMMIT,
            "blocksets_sha256": dict(BLOCKSET_SHA256),
            "wall_budget_seconds": seconds,
            "checkpoints": [],
        }

    def write_receipt(self):
        temporary = self.output / "receipt.json.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.receipt, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.output / "receipt.json")

    def checkpoint(self, name):
        original = name
        number = 2
        while (self.output / (name + ".state")).exists():
            name = original + "_" + str(number)
            number += 1
        raw = self.session.save_state()
        (self.output / (name + ".state")).write_bytes(raw)
        (self.output / (name + ".json")).write_text(
            json.dumps(foundation.to_jsonable(self.session.read_game_state()), indent=2)
        )
        self.receipt["checkpoints"].append(
            {
                "name": name,
                "size": len(raw),
                "sha1": hashlib.sha1(raw).hexdigest(),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        self.write_receipt()

    @staticmethod
    def restored_lead(lead):
        return (
            lead is not None
            and lead.valid
            and lead.hp_valid
            and (lead.hp == lead.max_hp)
            and any(lead.moves)
            and all(
                (
                    move in KNOWN_BASE_PP and pp & 63 == KNOWN_BASE_PP[move]
                    for move, pp in zip(lead.moves, lead.pp)
                    if move
                )
            )
        )

    def choose_move_slot(self, state):
        active, enemy = (state.party.active_mon, state.battle.enemy_mon)
        if active is None or not active.valid or enemy is None or (not enemy.valid):
            raise foundation.CaptureRefused("active move/target observations are not readable")
        candidates = [
            i
            for i, (move, pp) in enumerate(zip(active.moves, active.pp))
            if move in (33, 22, 73) and pp & 63 > 0
        ]
        if not candidates:
            raise foundation.CaptureRefused("no known ordinary move with readable PP")
        seeded = (
            read_wram_u8(
                self.session._pyboy.memory, self.session.symbols.addr_of("wEnemyBattleStatus2")
            )
            & 128
        )
        seed_slots = [i for i in candidates if active.moves[i] == 73]
        if seed_slots and (not seeded) and (enemy.type1 != 22) and (enemy.type2 != 22):
            return seed_slots[0]
        direct = [i for i in candidates if active.moves[i] in (33, 22)]
        if not direct:
            raise foundation.CaptureRefused("no direct move with readable PP after seed")
        return next((i for i in direct if active.moves[i] == 22), direct[0])

    def ordinary_trainer_battle(self):
        for _ in range(240):
            state = self.session.read_game_state()
            if not state.battle.active:
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

    def settle_encounter(self):
        for _ in range(160):
            state = self.session.read_game_state()
            if not state.battle.active:
                self.driver.idle(60, render=True)
                return
            if state.battle.kind.name == "TRAINER" or self.training_wild:
                self.ordinary_trainer_battle()
                continue
            if state.battle.kind.name != "WILD":
                raise foundation.CaptureRefused("unexpected battle kind on ordinary route")
            if state.battle.menu_open is None:
                raise foundation.CaptureRefused("wild command observation unavailable")
            if (
                state.battle.menu_open is True
                and state.menu.max_item == 1
                and (state.menu.watched_keys in (17, 33))
                and (not self.driver.input_locked())
            ):
                for key in ("b", "up", "left", "right", "down"):
                    self.driver.press(key, note="return route: RUN selection", step_ticks=30)
                self.driver.press("a", note="return route: attempt RUN", step_ticks=60)
            elif state.battle.menu_open is True and state.menu.watched_keys == 199:
                self.driver.press(
                    "b", note="return route: cancel observed move menu", step_ticks=30
                )
            else:
                self.driver.press("a", note="return route: encounter/escape text", step_ticks=30)
        raise foundation.CaptureRefused("return encounter did not settle")

    def navigate(self, goal, expected_map, transition=None, exit_direction="up"):
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
            path = tiles.astar(walkable, before, goal, tile_grid=grid)
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

    def normal_center_heal(self, city, center, door):
        self.navigate(door, city, transition=center)
        self.driver.idle(60, render=True)
        self.navigate((3, 3), center)
        self.driver.press("up", note="training: face ordinary Center nurse")
        for _ in range(120):
            state = self.session.read_game_state()
            lead = state.party.mons[0] if state.party.valid and state.party.mons else None
            if self.restored_lead(lead):
                break
            self.driver.press("a", note="training: ordinary nurse healing", step_ticks=60)
        else:
            raise foundation.CaptureRefused("normal training heal not observed")
        for _ in range(24):
            before = self.session.read_game_state().overworld
            if before.map_id == city:
                self.checkpoint("ordinary_training_center_heal")
                return
            if before.map_id != center:
                raise foundation.CaptureRefused("training heal unexpected map")
            self.driver.press(
                "down", note="training: source-mapped straight Center exit", step_ticks=60
            )
            after = self.session.read_game_state().overworld
            if after.map_id == center and (after.x, after.y) == (before.x, before.y):
                self.driver.press(
                    "a", note="training: finish pending nurse farewell", step_ticks=60
                )
        raise foundation.CaptureRefused("training Center exit not observed")

    def normal_training_to_thirteen(self):
        self.driver.phase = "ordinary_route1_training"
        for battle_index in range(64):
            lead = self.session.read_game_state().party.mons[0]
            if lead.level >= 13:
                self.checkpoint("ordinary_level_thirteen_observed")
                self.navigate((11, 0), 12, transition=1)
                self.normal_center_heal(1, 41, (23, 25))
                self.navigate((21, 35), 1)
                return
            state = self.session.read_game_state()
            if state.overworld.map_id != 12:
                raise foundation.CaptureRefused("training requires Route1")
            mem = self.session._pyboy.memory
            read = lambda name, memory=mem: self.session.symbols.read_u8(memory, name)
            width, height, ts = (
                read("wCurMapWidth"),
                read("wCurMapHeight"),
                read("wCurMapTileset"),
            )
            grid = tiles.expand_to_tile_grid(
                tiles.read_overworld_map(self.session, width, height),
                tiles.load_blockset(ts, self.game_source),
            )
            passable = tiles.read_passable_tiles(self.session)
            blockers = tiles.read_sprite_blockers(self.session)
            walkable = [
                [
                    grid[y * 2 + 1][x * 2] in passable and (x, y) not in blockers
                    for x in range(width * 2)
                ]
                for y in range(height * 2)
            ]
            start = (state.overworld.x, state.overworld.y)
            grasses = [
                (x, y)
                for y in range(height * 2)
                for x in range(width * 2)
                if walkable[y][x] and grid[y * 2 + 1][x * 2] == read("wGrassTile")
            ]
            grasses.sort(key=lambda p: abs(p[0] - start[0]) + abs(p[1] - start[1]))
            target = next(
                (p for p in grasses if tiles.astar(walkable, start, p, tile_grid=grid) is not None),
                None,
            )
            if target is None:
                raise foundation.CaptureRefused("training grass unreadable")
            self.navigate(target, 12)
            self.training_wild = True
            try:
                for _ in range(256):
                    current = self.session.read_game_state()
                    if current.battle.active:
                        self.checkpoint("ordinary_training_wild_encounter")
                        self.ordinary_trainer_battle()
                        self.driver.idle(90, render=True)
                        self.checkpoint("ordinary_training_wild_completed")
                        break
                    x, y = (current.overworld.x, current.overworld.y)
                    options = [
                        k
                        for k, dx, dy in [
                            ("up", 0, -1),
                            ("down", 0, 1),
                            ("left", -1, 0),
                            ("right", 1, 0),
                        ]
                        if (x + dx, y + dy) in grasses
                    ]
                    if not options:
                        raise foundation.CaptureRefused("training grass pacing unavailable")
                    self.driver.press(options[0], note="training: observed ordinary grass step")
                else:
                    raise foundation.CaptureRefused("training encounter bound exhausted")
            finally:
                self.training_wild = False
            self.navigate((11, 0), 12, transition=1)
            self.normal_center_heal(1, 41, (23, 25))
            self.navigate((21, 35), 1, transition=12, exit_direction="down")
            self.driver.idle(60, render=True)
        raise foundation.CaptureRefused("normal training sixty-four-battle bound exhausted")

    def run(self):
        if self.has_run:
            raise foundation.CaptureRefused("journey instance already ran; preserve its identity")
        self.has_run = True
        try:
            pins = self.pins
            self.session = foundation.Session.from_files(
                self.rom,
                self.symbols,
                expected_rom_sha1=pins.sha1_for_path(foundation.ROM_LABEL),
                expected_symbol_sha1=pins.symbol_sha1_for_path(foundation.SYM_LABEL),
                expected_pyboy_version=pins.pyboy_version,
                expected_pyboy_revision=pins.pyboy_revision,
            )
            foundation.register_default_hooks(self.session)
            self.receipt["public_observers_enabled"] = {
                "menu": self.session.enable_battle_menu_observation(),
                "resolution": self.session.enable_battle_resolution_observation(),
                "end": self.session.enable_battle_end_observation(),
            }
            if any(v is not True for v in self.receipt["public_observers_enabled"].values()):
                raise foundation.CaptureRefused(
                    "public execution observation initialization unavailable"
                )
            self.receipt["runtime_origin"] = sys.modules["pyboy.pyboy"].__file__
            self.receipt["pyboy_revision"] = pins.pyboy_revision
            self.driver = ObservedDriver(
                self.session, self.output, deadline=self.started + self.seconds
            )
            for name, phase in foundation.walkthrough.PHASES:
                self.driver.phase = name
                (foundation.run_route1_to_viridian if name == "route1_to_viridian" else phase)(
                    self.driver
                )
                foundation.validate_phase(name, self.session.read_game_state())
                self.checkpoint(name)
            self.driver.phase = "viridian_mart_parcel"
            self.navigate((29, 19), 1, transition=42)
            self.driver.idle(60, render=True)
            for _ in range(120):
                if self.session.read_game_state().bag.has_item(70) is True:
                    self.checkpoint("oak_parcel_received")
                    break
                self.driver.press("a", note="mart: receive ordinary Oak Parcel", step_ticks=30)
            else:
                raise foundation.CaptureRefused("Oak Parcel was not observed within dialog bound")
            for _ in range(60):
                if not self.driver.input_locked():
                    break
                self.driver.press("b", note="mart: finish parcel dialog")
            self.driver.phase = "return_oak_parcel"
            self.navigate((3, 7), 42, transition=1, exit_direction="down")
            self.driver.idle(60, render=True)
            self.navigate((21, 35), 1)
            for _ in range(4):
                if self.session.read_game_state().overworld.map_id == 12:
                    break
                self.driver.press("down", note="Viridian: Route1 south exit")
            self.driver.idle(60, render=True)
            self.navigate((10, 35), 12)
            for _ in range(6):
                self.settle_encounter()
                if self.session.read_game_state().overworld.map_id == 0:
                    break
                self.driver.press("down", note="Route1: Pallet south exit")
            self.driver.idle(60, render=True)
            self.navigate((12, 11), 0, transition=40)
            self.driver.idle(60, render=True)
            self.checkpoint("return_to_oaks_lab_with_parcel")
            self.navigate((5, 3), 40)
            self.driver.press("up", note="Oak: face ordinary lab NPC")
            for _ in range(180):
                mem = self.session._pyboy.memory
                got_dex = bool(
                    read_wram_u8(mem, self.session.symbols.addr_of("wEventFlags") + 4) & 32
                )
                if got_dex and self.session.read_game_state().bag.has_item(70) is False:
                    self.receipt["observed_got_pokedex"] = True
                    self.checkpoint("ordinary_pokedex_received")
                    break
                self.driver.press(
                    "a", note="Oak: ordinary parcel delivery/Pokedex text", step_ticks=30
                )
            else:
                raise foundation.CaptureRefused(
                    "ordinary Pokedex flag/parcel consumption not observed"
                )
            for _ in range(60):
                self.driver.press("b", note="Oak: finish Pokedex dialog", step_ticks=30)
            self.driver.phase = "ordinary_pokedex_to_forest"
            self.navigate((4, 11), 40, transition=0, exit_direction="down")
            self.driver.idle(60, render=True)
            self.navigate((10, 0), 0, transition=12)
            self.driver.idle(60, render=True)
            foundation.validate_phase("pallet_to_route1", self.session.read_game_state())
            self.normal_training_to_thirteen()
            foundation.validate_phase("route1_to_viridian", self.session.read_game_state())
            self.navigate((18, 0), 1, transition=13)
            self.driver.idle(60, render=True)
            self.checkpoint("ordinary_route2_after_pokedex")
            self.navigate((3, 43), 13, transition=50)
            self.driver.idle(60, render=True)
            self.navigate((5, 0), 50, transition=51)
            self.driver.idle(60, render=True)
            if (
                self.session.read_game_state().overworld.map_id != 51
                or self.session.read_game_state().battle.active
            ):
                raise foundation.CaptureRefused("ordinary Viridian Forest entry not observed")
            self.checkpoint("ordinary_viridian_forest_entry")
            self.driver.phase = "ordinary_forest_to_pewter"
            self.navigate((11, 29), 51)
            self.driver.press("right", note="forest: face source-mapped Potion ball")
            for _ in range(60):
                if self.session.read_game_state().bag.has_item(20) is True:
                    self.checkpoint("ordinary_forest_potion_pickup")
                    break
                self.driver.press("a", note="forest: obtain ordinary Potion pickup")
            else:
                raise foundation.CaptureRefused("ordinary Potion pickup not observed")
            for _ in range(6):
                self.driver.press("b", note="forest: finish pickup dialog")
            self.navigate((1, 0), 51, transition=47)
            self.driver.idle(60, render=True)
            self.checkpoint("ordinary_forest_north_gate")
            self.navigate((5, 0), 47, transition=13)
            self.driver.idle(60, render=True)
            self.navigate((8, 0), 13, transition=2)
            self.driver.idle(60, render=True)
            if (
                self.session.read_game_state().overworld.map_id != 2
                or self.session.read_game_state().battle.active
            ):
                raise foundation.CaptureRefused("ordinary Pewter arrival not observed")
            self.checkpoint("ordinary_pewter_arrival")
            self.driver.phase = "ordinary_pewter_heal_and_brock"
            self.navigate((13, 25), 2, transition=58)
            self.driver.idle(60, render=True)
            self.navigate((3, 3), 58)
            self.driver.press("up", note="Pewter: face source-mapped nurse")
            for _ in range(120):
                current = self.session.read_game_state()
                lead = current.party.mons[0] if current.party.valid and current.party.mons else None
                if self.restored_lead(lead):
                    self.checkpoint("ordinary_pewter_center_healed")
                    break
                self.driver.press("a", note="Pewter: ordinary nurse healing dialog", step_ticks=60)
            else:
                raise foundation.CaptureRefused("ordinary Pokemon Center healing not observed")
            for _ in range(6):
                self.driver.press("b", note="Pewter: finish nurse text")
            for _ in range(16):
                if self.session.read_game_state().overworld.map_id == 2:
                    break
                if self.session.read_game_state().overworld.map_id != 58:
                    raise foundation.CaptureRefused("Center exit left admitted maps")
                before_exit = self.session.read_game_state().overworld
                self.driver.press(
                    "down",
                    note="Pewter: ordinary source-mapped straight Center exit",
                    step_ticks=60,
                )
                after_exit = self.session.read_game_state().overworld
                if after_exit.map_id == 58 and (after_exit.x, after_exit.y) == (
                    before_exit.x,
                    before_exit.y,
                ):
                    self.driver.press(
                        "a", note="Pewter: advance pending nurse animation/farewell", step_ticks=60
                    )
            else:
                raise foundation.CaptureRefused("ordinary Center exit not observed")
            self.driver.idle(60, render=True)
            self.navigate((16, 17), 2, transition=54)
            self.driver.idle(60, render=True)
            self.checkpoint("ordinary_pewter_gym_entry")
            self.navigate((4, 2), 54)
            self.driver.press("up", note="Pewter: face source-mapped Brock")
            for _ in range(120):
                if self.session.read_game_state().battle.active:
                    break
                self.driver.press(
                    "a", note="Pewter: engage Brock through ordinary dialog", step_ticks=60
                )
            else:
                raise foundation.CaptureRefused("ordinary Brock engagement not observed")
            self.ordinary_trainer_battle()
            for _ in range(120):
                badge = read_wram_u8(
                    self.session._pyboy.memory, self.session.symbols.addr_of("wObtainedBadges")
                )
                if badge & 1:
                    self.checkpoint("ordinary_boulder_badge_received")
                    self.receipt["observed_boulder_badge"] = True
                    break
                self.driver.press(
                    "a", note="Pewter: finish observed Brock award dialog", step_ticks=60
                )
            else:
                raise foundation.CaptureRefused(
                    "Boulder Badge not observed; no Brock success claimed"
                )
            self.receipt["status"] = "captured"
        except BaseException as exc:
            self.receipt["status"] = "failed"
            self.receipt["error"] = type(exc).__name__ + ": " + str(exc)
            if self.session is not None:
                try:
                    self.checkpoint("failed_observed_boundary")
                except BaseException as checkpoint_error:  # noqa: BLE001 - retain the primary failure
                    self.receipt["failure_checkpoint_error"] = (
                        type(checkpoint_error).__name__ + ": " + str(checkpoint_error)
                    )
            raise
        finally:
            self.receipt["elapsed_seconds"] = time.monotonic() - self.started
            self.receipt["actions_attempted"] = self.driver.actions if self.driver else 0
            try:
                if self.session:
                    try:
                        self.session.close(save=False)
                        self.receipt["session_closed_without_save"] = True
                    except BaseException as close_error:
                        self.receipt["session_closed_without_save"] = False
                        self.receipt["teardown_error"] = (
                            type(close_error).__name__ + ": " + str(close_error)
                        )
                        if self.receipt["status"] != "failed":
                            self.receipt["status"] = "failed"
                            raise
            finally:
                self.write_receipt()
        return self.receipt


def capture(rom: Path, symbols: Path, output: Path, game_source: Path, *, seconds=600):
    return NormalJourney(rom, symbols, output, game_source, seconds=seconds).run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--sym", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--game-source-root", type=Path, required=True)
    parser.add_argument("--wall-seconds", type=float, default=600)
    args = parser.parse_args()
    capture(args.rom, args.sym, args.out, args.game_source_root, seconds=args.wall_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
