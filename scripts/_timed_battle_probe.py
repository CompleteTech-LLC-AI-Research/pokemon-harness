"""Bounded, observational owner driver for one authentic Colosseum turn.

Never advances a CPU, loads assets, or writes memory/registers. The supervisor
alone applies returned inputs and must keep stepping both peers after evidence
freezes. Supports effects 0, 6 and 44 on a nonfaint turn, except Counter (its
special return path is not qualified here). All faint transitions and other
effects fail closed. Historical fixture acquisition is never certified.
"""

from __future__ import annotations

import threading
from collections import deque
from copy import deepcopy

from scripts._timed_battle_probe_reads import (
    _byte,
    _integer,
    _location,
    _party,
    _read,
    _rom,
    _validate_party,
)
from scripts._timed_battle_probe_schemas import (
    HIDDEN_EVENT_INPUT_LIMIT,
    MENU_FIELDS,
    MOVE_EFFECTS,
    OBSERVATION_SYMBOLS,
    PHASES,
    PROVENANCE,
    READINESS_PHASES,  # noqa: F401 - retained public facade attribute
    SUPPORTED_EFFECTS,
    UNSUPPORTED_MOVES,
    VERIFICATION_SYMBOLS,  # noqa: F401 - retained public facade attribute
)


class BattleOwnerDriver:
    def __init__(self, *, session, side, options, peer_state):
        self.thread = threading.get_ident()
        self.session, self.side, self.peer = session, side, peer_state
        self.version = options["version"]
        self.slot = options.get("move_slot")
        self.checkpoint = options.get("checkpoint", "room-return")
        if side not in ("listen", "connect"):
            raise ValueError("unsupported owner role")
        if self.version not in ("red", "blue", "red_color", "blue_color", "yellow"):
            raise ValueError("unsupported version")
        if self.checkpoint not in ("settled-turn", "room-return"):
            raise ValueError("unsupported checkpoint")
        if self.slot is not None:
            _integer(self.slot, 0, 3, "move slot")
        self.hooks, self.flags = [], [0] * len(PHASES)
        self.sequence, self.next_input, self.approach_steps = 0, 0, 0
        self.phase, self.context, self.poll = "approach", None, None
        self.error, self.closed = None, False
        self.events = deque(maxlen=32)
        self.counts = dict.fromkeys(OBSERVATION_SYMBOLS, 0)
        self.sent, self.attempts = set(), {}
        self.interaction_observation = None
        self.last_action = None
        self.baseline = self.turn = self.room = None
        self.exchange_seq = self.end_seq = self.return_seq = 0
        self.exchange = None
        self.actions = {
            key: {
                "executed": False,
                "done": False,
                "skip_reason": None,
                "damage_done": 0,
                "move_missed": None,
                "damage_samples": [],
            }
            for key in ("local", "enemy")
        }
        self.pending_damage = {"local": None, "enemy": None}
        self.pp_entries = 0
        self.before = _party(session)
        self.lead = _validate_party(self.before)
        record = bytes.fromhex(self.before["records"][self.lead])
        if record[4] not in (0, 64):
            raise ValueError("unsupported first-living status (only healthy/paralysis)")
        self.chosen = self._choose(record[8:12], record[29:33])
        # Resolve and validate all observational reads before the first hook.
        for name in MENU_FIELDS:
            _read(session, name)
        _read(session, "wDamage", 2)
        self._combatants()
        addresses = [(name, _location(session, name)) for name in OBSERVATION_SYMBOLS]
        addresses.extend(self._verified_continuations())
        if len({location for _, location in addresses}) != len(addresses):
            raise ValueError("aliased observation hook locations")
        self.counts.update({name: 0 for name, _ in addresses})
        try:
            for name, (bank, address) in addresses:
                session._pyboy.hook_register(bank, address, self._callback, name)
                self.hooks.append((bank, address))
            self._publish("party_qualified")
        except BaseException as exc:
            try:
                self.close()
            except BaseException as cleanup:  # noqa: BLE001 - preserve original and cleanup failures
                raise BaseExceptionGroup("battle registration/rollback failed", [exc, cleanup])
            raise

    def _choose(self, moves, pp):
        choices = range(4) if self.slot is None else (self.slot,)
        for slot in choices:
            move = _byte(moves[slot])
            if not move or not (_byte(pp[slot]) & 63):
                continue
            if move > 165:
                raise ValueError("invalid move ID")
            data = self._move_data(move)
            if move in UNSUPPORTED_MOVES:
                if self.slot is not None:
                    raise ValueError(UNSUPPORTED_MOVES[move])
                continue
            if move in MOVE_EFFECTS and data[1] in SUPPORTED_EFFECTS and data[2] > 0:
                return {"slot": slot, "move_id": move, "effect": data[1]}
        raise ValueError("no legal supported existing move with PP (effects 0, 6, 44)")

    def _move_data(self, move):
        _integer(move, 1, 165, "move ID")
        bank, base = _location(self.session, "Moves")
        data = _rom(self.session, bank, base + (move - 1) * 6, 6)
        if data[0] != move:
            raise ValueError("move table ID mismatch")
        if move in MOVE_EFFECTS and data[1] != MOVE_EFFECTS[move]:
            raise ValueError("move table effect contradicts supported catalog")
        return data

    def _verified_continuations(self):
        s = self.session
        bank, start = _location(s, "SelectEnemyMove")
        expected = 0x56E3 if self.version == "yellow" else 0x5571
        if (bank, start + 13) != (15, expected):
            raise ValueError("ordinary post-exchange address mismatch")
        target_bank, target = _location(s, "LinkBattleExchangeData")
        load_bank, load = _location(s, "LoadScreenTilesFromBuffer1")
        expected_load = 0x371B if self.version == "yellow" else 0x3725
        receive = s.symbols.addr_of("wSerialExchangeNybbleReceiveData")
        expected_bytes = (
            b"\xcd"
            + target.to_bytes(2, "little")
            + b"\xcd"
            + load.to_bytes(2, "little")
            + b"\xfa"
            + receive.to_bytes(2, "little")
        )
        if target_bank != bank or (load_bank, load) != (0, expected_load):
            raise ValueError("post-exchange call target mismatch")
        if _rom(s, bank, start + 10, 9) != expected_bytes:
            raise ValueError("post-exchange callsite bytes mismatch")
        hooks = [("post_exchange", (bank, expected))]
        # The shared hurt/confusion label is NOT itself proof of paralysis.
        # Verify and observe the dedicated LD HL,FullyParalyzedText fallthrough.
        _, text = _location(s, "FullyParalyzedText")
        _, printer = _location(s, "PrintText")
        for side, label in (
            ("local", "CheckPlayerStatusConditions.MonHurtItselfOrFullyParalysed"),
            ("enemy", "CheckEnemyStatusConditions.monHurtItselfOrFullyParalysed"),
        ):
            b, a = _location(s, label)
            signature = (
                b"\x21" + text.to_bytes(2, "little") + b"\xcd" + printer.to_bytes(2, "little")
            )
            if _rom(s, b, a - 6, 6) != signature:
                raise ValueError(f"{side} paralysis branch bytes mismatch")
            hooks.append((f"{side}_fully_paralyzed", (b, a - 6)))
        return hooks

    def _owner(self):
        if threading.get_ident() != self.thread:
            raise RuntimeError("battle driver called outside owner thread")

    def _check(self):
        self._owner()
        if self.error:
            raise RuntimeError(self.error)
        if self.closed:
            raise RuntimeError("battle driver closed")

    def _publish(self, phase):
        index = PHASES.index(phase)
        if not self.flags[index]:
            self.flags[index] = 1
            self.peer.publish_ready(phase)

    def _combatants(self):
        result = {}
        for key, prefix, slot in (
            ("local", "wBattleMon", "wPlayerMonNumber"),
            ("enemy", "wEnemyMon", "wEnemyMonPartyPos"),
        ):
            hp = _read(self.session, prefix + "HP", 2)
            result[key] = {
                "slot": _read(self.session, slot),
                "species": _read(self.session, prefix + "Species"),
                "hp": hp[0] * 256 + hp[1],
                "status": _read(self.session, prefix + "Status"),
                "moves": _read(self.session, prefix + "Moves", 4),
                "pp": _read(self.session, prefix + "PP", 4),
            }
        return result

    def _callback(self, name):
        if self.closed:
            return
        try:
            self._observe(name)
        except BaseException as exc:  # noqa: BLE001 - native callbacks may swallow exceptions
            if self.error is None:
                self.error = f"observation {name}: {type(exc).__name__}: {exc}"[:512]

    def _observe(self, name):
        self._check()
        self.sequence += 1
        if self.sequence >= 2**63 - 1:
            raise RuntimeError("observation counter exhausted")
        self.counts[name] += 1
        contexts = {
            "LinkMenu.waitForInputLoop": "link",
            "LinkMenu.doneChoosingMenuSelection": "warp",
            "PrepareForSpecialWarp": "warp",
            "CableClubLeftGameboy": "dialogue",
            "CableClubRightGameboy": "dialogue",
            "CableClub_DoBattleOrTrade": "exchange",
            "DisplayBattleMenu": "battle",
            "MoveSelectionMenu": "move",
            "HandlePartyMenuInput": "party",
            "LinkBattleExchangeData": "exchange",
            "EndOfBattle": "ending",
            "ReturnToCableClubRoom": "returning",
        }
        if name in contexts:
            self.context, self.poll = contexts[name], None
            self.phase = self.context
            self.events.append({"seq": self.sequence, "phase": self.context})
        if name == "HandleMenuInput":
            self.poll = self.context
        if name == "HandlePartyMenuInput":
            if "inspect_a" not in self.sent:
                raise ValueError("unexpected party selection boundary")
            self.poll = "party"
            self._publish("party_inspected")
        if name == "DisplayBattleMenu" and self.flags[4] and "party_b" in self.sent:
            self._publish("party_cancelled")
        if name == "MainInBattleLoop":
            if self.baseline is None:
                combatants = self._combatants()
                if combatants["local"]["slot"] != self.lead:
                    raise ValueError("automatic first-living lead mismatch")
                for mon in combatants.values():
                    if not mon["hp"] or mon["status"] not in (0, 64):
                        raise ValueError("unsupported initial HP/status boundary")
                local = combatants["local"]
                slot = self.chosen["slot"]
                if local["moves"][slot] != self.chosen["move_id"] or not local["pp"][slot] & 63:
                    raise ValueError("lead move differs from preflight")
                _validate_baseline(self.before, combatants)
                self.baseline = {"seq": self.sequence, **combatants}
                self._publish("battle_menu_ready")
            elif self.exchange_seq and self.turn is None:
                if not all(action["done"] for action in self.actions.values()):
                    raise ValueError("turn boundary without both completed actions")
                combatants = self._combatants()
                if any(not mon["hp"] for mon in combatants.values()):
                    raise ValueError("unsupported faint boundary")
                turn = {
                    "exchange_seq": self.exchange_seq,
                    "settled_seq": self.sequence,
                    **self.exchange,
                    **combatants,
                    "actions": deepcopy(self.actions),
                    "pp": {
                        "before": list(self.baseline["local"]["pp"]),
                        "after": list(combatants["local"]["pp"]),
                        "decrement_entries": self.pp_entries,
                    },
                }
                _validate_local_turn(self.baseline, turn)
                if (
                    _read(self.session, "wPlayerSelectedMove") != turn["local_move_id"]
                    or _read(self.session, "wEnemySelectedMove") != turn["enemy_move_id"]
                ):
                    raise ValueError("decoded/executed move IDs disagree with exchange")
                self.turn = turn
                self._publish("settled_turn")
        if name in ("HandlePlayerMonFainted", "HandleEnemyMonFainted") and self.turn is None:
            raise ValueError("unsupported faint boundary; no settled nonfaint turn")
        if name == "post_exchange" and self.turn is None:
            if self.exchange_seq or not self.flags[5] or "move_a" not in self.sent:
                raise ValueError("unexpected ordinary exchange continuation")
            send = _read(self.session, "wSerialExchangeNybbleSendData")
            receive = _read(self.session, "wSerialExchangeNybbleReceiveData")
            if send != self.chosen["slot"] or not 0 <= receive < 4:
                raise ValueError("unsupported exchanged action")
            self.exchange_seq = self.sequence
            self.exchange = {
                "send": send,
                "receive": receive,
                "local_move_id": _read(self.session, "wPlayerSelectedMove"),
                # This continuation precedes the ROM's enemy ID decoding.
                "enemy_move_id": _read(self.session, "wEnemyMonMoves", 4)[receive],
            }
            for side in ("local", "enemy"):
                data = self._move_data(self.exchange[f"{side}_move_id"])
                if self.exchange[f"{side}_move_id"] in UNSUPPORTED_MOVES:
                    raise ValueError(UNSUPPORTED_MOVES[self.exchange[f"{side}_move_id"]])
                if (
                    self.exchange[f"{side}_move_id"] not in MOVE_EFFECTS
                    or data[1] not in SUPPORTED_EFFECTS
                    or not data[2]
                ):
                    raise ValueError("unsupported exchanged move effect")
                self.exchange[f"{side}_move_effect"] = data[1]
        if self.exchange_seq and self.turn is None:
            mapping = {
                "PlayerCanExecuteMove": ("local", "executed"),
                "EnemyCanExecuteMove": ("enemy", "executed"),
                "ExecutePlayerMoveDone": ("local", "done"),
                "ExecuteEnemyMoveDone": ("enemy", "done"),
            }
            if name in mapping:
                side, field = mapping[name]
                self.actions[side][field] = True
                if field == "done":
                    self.actions[side]["move_missed"] = _read(self.session, "wMoveMissed")
            if name in ("local_fully_paralyzed", "enemy_fully_paralyzed"):
                side = name.split("_")[0]
                if _read(self.session, "hWhoseTurn") != (0 if side == "local" else 1):
                    raise ValueError("paralysis branch turn mismatch")
                self.actions[side]["skip_reason"] = "fully_paralyzed"
            if name in ("ApplyDamageToEnemyPokemon", "ApplyDamageToPlayerPokemon"):
                side = "local" if name == "ApplyDamageToEnemyPokemon" else "enemy"
                prefix = "wEnemyMon" if side == "local" else "wBattleMon"
                hp = _read(self.session, prefix + "HP", 2)
                damage = _read(self.session, "wDamage", 2)
                if self.pending_damage[side] is not None:
                    raise ValueError("overlapping damage application")
                self.pending_damage[side] = {
                    "before_hp": hp[0] * 256 + hp[1],
                    "damage": damage[0] * 256 + damage[1],
                }
            if name in ("ApplyAttackToEnemyPokemonDone", "ApplyAttackToPlayerPokemonDone"):
                side = "local" if name == "ApplyAttackToEnemyPokemonDone" else "enemy"
                sample = self.pending_damage[side]
                if sample is None or not self.actions[side]["executed"]:
                    raise ValueError("damage Done without actual application")
                prefix = "wEnemyMon" if side == "local" else "wBattleMon"
                hp = _read(self.session, prefix + "HP", 2)
                sample.update(
                    after_hp=hp[0] * 256 + hp[1], move_missed=_read(self.session, "wMoveMissed")
                )
                if len(self.actions[side]["damage_samples"]) >= 2:
                    raise ValueError("unsupported damage application count")
                self.actions[side]["damage_samples"].append(sample)
                self.pending_damage[side] = None
                self.actions[side]["damage_done"] += 1
            if name == "DecrementPP" and _read(self.session, "hWhoseTurn") == 0:
                if not self.actions["local"]["executed"] or self.actions["local"]["done"]:
                    raise ValueError("DecrementPP outside local execution path")
                self.pp_entries += 1
        if name == "EndOfBattle":
            if self.turn is None or "run_a" not in self.sent:
                raise ValueError("unsupported battle end before controlled both-RUN")
            self.end_seq = self.sequence
            self.end_result = {
                "send": _read(self.session, "wSerialExchangeNybbleSendData"),
                "receive": _read(self.session, "wSerialExchangeNybbleReceiveData"),
                "result": _read(self.session, "wBattleResult"),
            }
            if self.end_result != {"send": 15, "receive": 15, "result": 2}:
                raise ValueError("natural both-RUN reciprocal draw not observed")
        if name == "ReturnToCableClubRoom":
            if not self.end_seq:
                raise ValueError("room return without preceding battle end")
            self.return_seq = self.sequence

    def _pulse(self, button, frame, once=None, *, hidden_event=False):
        if once and once in self.sent:
            return None
        key = "hidden_event" if hidden_event else self.context or "approach"
        limit = (
            HIDDEN_EVENT_INPUT_LIMIT
            if hidden_event
            else (40 if key in ("approach", "dialogue") else 16)
        )
        count = self.attempts.get(key, 0)
        if count >= limit:
            self.error = f"{key} input quota exhausted"
            raise RuntimeError(self.error)
        self.attempts[key] = count + 1
        if once:
            self.sent.add(once)
        duration, cadence = (
            (4, 20) if hidden_event and button == "a" else ((8, 30) if hidden_event else (2, 20))
        )
        self.next_input = frame + cadence
        self.last_action = {
            "frame_offset": frame,
            "button": button,
            "duration": duration,
            "cadence": cadence,
            "phase": self.context,
        }
        return button, duration

    def before_step(self, frame_offset):
        self._check()
        try:
            return self._before_step(frame_offset)
        except Exception as exc:
            if self.error is None:
                self.error = f"input failed: {type(exc).__name__}: {exc}"[:512]
            raise

    def _before_step(self, frame_offset):
        _integer(frame_offset, 0, 2**63 - 1, "frame offset")
        if self.objective_complete() or frame_offset < self.next_input:
            return None
        if not self.peer.peer_ready("party_qualified"):
            return None
        menu = {name: _read(self.session, name) for name in MENU_FIELDS}
        item, keys = menu["wCurrentMenuItem"], menu["wMenuWatchedKeys"]

        def pulse(button, once=None):
            return self._pulse(button, frame_offset, once)

        if menu["wCurMap"] == 240 and menu["wLinkState"] == 1:
            self._publish("colosseum_reached")
        if self.context == "link":
            maximum = 3 if self.version == "yellow" else 2
            if menu["wMaxMenuItem"] != maximum or not keys & 1:
                return None
            if item != 1:
                return pulse("down" if item < 1 else "up")
            self._publish("link_menu_colosseum_ready")
            if self.peer.peer_ready("link_menu_colosseum_ready"):
                return pulse("a", "link_a")
        elif self.context == "party" and self.poll == "party":
            if (
                menu["wPartyMenuTypeOrMessageID"] == 0
                and keys == 3
                and menu["wMaxMenuItem"] == self.before["count"] - 1
            ):
                return pulse("b", "party_b")
        elif self.context == "battle" and self.poll == "battle":
            if menu["wMaxMenuItem"] != 1 or menu["wTopMenuItemY"] != 14:
                return None
            if self.turn:
                if not self.peer.peer_ready("settled_turn"):
                    return None
                target, once = 3, "run_a"
            elif not self.flags[4]:
                target, once = 2, "inspect_a"
            elif self.flags[5]:
                if not self.peer.peer_ready("party_cancelled"):
                    return None
                target, once = 0, "fight_a"
            else:
                return None
            column = 0 if menu["wTopMenuItemX"] == 9 else 1
            if menu["wTopMenuItemX"] not in (9, 15) or item not in (0, 1):
                return None
            if column != target // 2:
                return pulse("right" if target // 2 else "left")
            if item != target % 2:
                return pulse("down" if target % 2 else "up")
            if keys & 1:
                return pulse("a", once)
        elif self.context == "move" and self.poll == "move":
            if not keys & 1 or not 1 <= item <= 4:
                return None
            slot = self.chosen["slot"]
            disabled = menu["wPlayerDisabledMove"] >> 4
            if disabled == slot + 1:
                raise ValueError("chosen move disabled")
            if item != slot + 1:
                return pulse("down" if item < slot + 1 else "up")
            self._publish("move_ready")
            if self.peer.peer_ready("move_ready"):
                return pulse("a", "move_a")
        elif self.context == "dialogue":
            return pulse("a")
        elif self.context in (None, "warp") and (self.flags[2] or menu["wCurMap"] == 0xF0):
            role = menu["hSerialConnectionStatus"]
            observation = {
                "frame_offset": frame_offset,
                "map": menu["wCurMap"],
                "link_state": menu["wLinkState"],
                "serial_role": role,
                "x": menu["wXCoord"],
                "y": menu["wYCoord"],
                "facing": menu["wSpritePlayerStateData1FacingDirection"],
                "joy_ignore": menu["wJoyIgnore"],
                "walk_counter": menu["wWalkCounter"],
                "status_flags5": menu["wStatusFlags5"],
                "admission_reason": "room_not_ready",
            }
            self.interaction_observation = observation
            if menu["wCurMap"] != 0xF0 or menu["wLinkState"] != 1:
                return None
            if not self.peer.peer_ready("colosseum_reached"):
                observation["admission_reason"] = "peer_room_not_ready"
                return None
            if role not in (1, 2):
                observation["admission_reason"] = "invalid_serial_role"
                raise ValueError("invalid serial role")
            x, facing, direction = (3, 0x0C, "right") if role == 2 else (6, 0x08, "left")
            if menu["wJoyIgnore"] or menu["wWalkCounter"]:
                observation["admission_reason"] = "input_masked_or_walking"
                return None
            if menu["wStatusFlags5"] & 0x84:
                observation["admission_reason"] = "a_blocked_or_scripted_movement"
                return None
            if (menu["wXCoord"], menu["wYCoord"]) != (x, 4):
                observation["admission_reason"] = "unexpected_adjacent_coordinates"
                return None
            # These are STANDING coordinates. Hidden-event lookup checks the
            # tile in front: internal (4,4), external (5,4). OverworldLoop
            # requires A pressed and home/hidden_events.asm requires A held.
            if menu["wSpritePlayerStateData1FacingDirection"] != facing:
                observation["admission_reason"] = "orient"
                return self._pulse(direction, frame_offset, hidden_event=True)
            observation["admission_reason"] = "interact"
            return self._pulse("a", frame_offset, hidden_event=True)
        elif self.context is None:
            if self.counts["SaveGameData"] or self.counts["Serial_SyncAndExchangeNybble"]:
                return None
            if self.approach_steps < 3:
                self.approach_steps += 1
                return pulse("up")
            return pulse("a")
        return None

    def after_step(self, call):
        self._check()
        try:
            self._after_step(call)
        except Exception as exc:
            if self.error is None:
                self.error = f"after-step failed: {type(exc).__name__}: {exc}"[:512]
            raise

    def _after_step(self, call):
        if self.return_seq and self.room is None:
            state = {
                "map": _read(self.session, "wCurMap"),
                "link_state": _read(self.session, "wLinkState"),
                "is_in_battle": _read(self.session, "wIsInBattle"),
            }
            if state == {"map": 240, "link_state": 1, "is_in_battle": 0}:
                healed = _party(self.session)
                _validate_healed(self.before, healed)
                self.sequence += 1
                self.room = {
                    "end_seq": self.end_seq,
                    "return_seq": self.return_seq,
                    "settled_seq": self.sequence,
                    **self.end_result,
                    **state,
                    "healed_party": healed,
                }
                self._publish("room_returned")

    def objective_complete(self):
        self._owner()
        evidence = self.turn if self.checkpoint == "settled-turn" else self.room
        return not self.error and bool(evidence)

    def snapshot(self):
        self._owner()
        return deepcopy(
            {
                "schema_version": 1,
                "role": self.side,
                "side": self.side,
                "version": self.version,
                "move_slot": self.slot,
                "chosen": self.chosen,
                "checkpoint": self.checkpoint,
                "phase": self.phase,
                "readiness": self.flags,
                "checkpoint_reached": self.objective_complete(),
                "gameplay_completed": self.objective_complete(),
                "full_authentic_acceptance": False,
                "provenance": PROVENANCE,
                "before_party": self.before,
                "baseline": self.baseline,
                "turn": self.turn,
                "room_return": self.room,
                "unsupported_reason": self.error,
                "hook_counts": self.counts,
                "recent_transitions": list(self.events),
                "interaction_observation": self.interaction_observation,
                "last_action": self.last_action,
                "input_attempts": self.attempts,
                "hidden_event_quota_used": self.attempts.get("hidden_event", 0),
            }
        )

    def close(self):
        self._owner()
        # A hook whose native deregistration fails must already be inert.
        self.closed = True
        errors = []
        for bank, address in reversed(tuple(self.hooks)):
            try:
                self.session._pyboy.hook_deregister(bank, address)
            except BaseException as exc:  # noqa: BLE001 - attempt every owned hook removal
                errors.append(exc)
            else:
                self.hooks.remove((bank, address))
        if errors:
            if self.error is None:
                self.error = "battle observer cleanup failed"
            raise BaseExceptionGroup("battle observer cleanup failed", errors)
        self.closed = True


BattleProbe = BattleOwnerDriver


def create_owner_driver(*, session, side, options, peer_state):
    return BattleOwnerDriver(session=session, side=side, options=options, peer_state=peer_state)


def _mon(mon):
    _integer(mon["slot"], 0, 5, "active slot")
    _integer(mon["species"], 1, 190, "active species")
    _integer(mon["hp"], 1, 65535, "nonfaint HP")
    if mon["status"] not in (0, 64) or type(mon["status"]) is not int:
        raise ValueError("unsupported status")
    for key in ("moves", "pp"):
        if len(mon[key]) != 4:
            raise ValueError(f"invalid {key} length")
        for value in mon[key]:
            _byte(value)


def _validate_baseline(party, baseline):
    lead = _validate_party(party)
    record = bytes.fromhex(party["records"][lead])
    expected = {
        "slot": lead,
        "species": record[0],
        "hp": int.from_bytes(record[1:3], "big"),
        "status": record[4],
        "moves": list(record[8:12]),
        "pp": list(record[29:33]),
    }
    if baseline["local"] != expected:
        raise ValueError("ordinary party to automatic lead baseline mismatch")


def _validate_healed(before, healed):
    _validate_party(healed)
    if before["count"] != healed["count"] or before["species"] != healed["species"]:
        raise ValueError("healed party identity mismatch")
    for original, current in zip(before["records"], healed["records"], strict=True):
        old, new = bytes.fromhex(original), bytes.fromhex(current)
        if new[1:3] != new[34:36] or not int.from_bytes(new[1:3], "big") or new[4]:
            raise ValueError("room party HP/status not healed")
        # HealParty also restores PP. Record it, but do not claim PP restoration
        # here; HP/status and every non-HP/status/PP party byte are checked.
        if any(old[i] != new[i] for i in range(44) if i not in (1, 2, 4, 29, 30, 31, 32)):
            raise ValueError("room party changed beyond healing fields")


def _validate_local_turn(baseline, turn):
    """Local settlement check shared by the live observer and pure parent."""
    send = _integer(turn["send"], 0, 3, "send slot")
    receive = _integer(turn["receive"], 0, 3, "receive slot")
    for side, opposite, slot, move_key in (
        ("local", "enemy", send, "local_move_id"),
        ("enemy", "local", receive, "enemy_move_id"),
    ):
        before, after = baseline[side], turn[side]
        _mon(before)
        _mon(after)
        if any(before[k] != after[k] for k in ("slot", "species", "moves")):
            raise ValueError("unsupported active combatant change")
        move = _integer(turn[move_key], 1, 165, "selected move ID")
        effect = _integer(turn[f"{side}_move_effect"], 0, 255, "selected move effect")
        if move not in MOVE_EFFECTS or effect != MOVE_EFFECTS[move]:
            raise ValueError("unsupported/forged selected move catalog effect")
        if move in UNSUPPORTED_MOVES:
            raise ValueError(UNSUPPORTED_MOVES[move])
        if before["moves"][slot] != move:
            raise ValueError("move ID does not match exchanged slot")
        action = turn["actions"][side]
        if action["done"] is not True:
            raise ValueError("action did not reach Done before settled boundary")
        missed = _integer(action["move_missed"], 0, 1, "completed action miss flag")
        _integer(action["damage_done"], 0, 2 if effect == 44 else 1, "damage completions")
        if action["skip_reason"] == "fully_paralyzed":
            if action["executed"] is not False or action["damage_done"] or after["status"] != 64:
                raise ValueError("unsupported paralysis execution evidence")
        elif action["skip_reason"] is not None or action["executed"] is not True:
            raise ValueError("unsupported skipped action")
        # Per-application evidence covers calculated damage, completed zero
        # damage and multi-hit aggregates. An absent
        # application is accepted only for a verified skip or actual miss.
        samples = action["damage_samples"]
        if not isinstance(samples, list) or len(samples) != action["damage_done"]:
            raise ValueError("damage completion/sample count mismatch")
        hp = baseline[opposite]["hp"]
        for sample in samples:
            previous = _integer(sample["before_hp"], 1, 65535, "pre-application HP")
            current = _integer(sample["after_hp"], 1, 65535, "post-application HP")
            amount = _integer(sample["damage"], 0, 65535, "calculated damage")
            _integer(sample["move_missed"], 0, 0, "applied damage miss flag")
            if previous != hp or previous - current != amount:
                raise ValueError("calculated damage disagrees with observed HP application")
            hp = current
        if hp != turn[opposite]["hp"]:
            raise ValueError("HP delta lacks completed damage application evidence")
        if action["executed"] and not samples and not missed:
            raise ValueError("zero outcome lacks actual miss or completed zero-damage path")
        if samples and missed:
            raise ValueError("miss flag contradicts completed application")
        if effect == 44 and action["damage_done"] not in (0, 2):
            raise ValueError("nonfaint Double Kick/Bonemerang must complete both hits")
        old_status, new_status = baseline[opposite]["status"], turn[opposite]["status"]
        if old_status != new_status and not (
            old_status == 0
            and new_status == 64
            and effect == 6
            and action["executed"] is True
            and bool(samples)
            and not missed
        ):
            raise ValueError("status change lacks supported paralysis effect")
    pp = turn["pp"]
    if pp["before"] != baseline["local"]["pp"] or pp["after"] != turn["local"]["pp"]:
        raise ValueError("local PP evidence differs from snapshots")
    entries = _integer(pp["decrement_entries"], 0, 100000, "DecrementPP entries")
    expected = list(pp["before"])
    if turn["actions"]["local"]["executed"]:
        if not entries or not expected[send] & 63:
            raise ValueError("execution lacks usable PP/decrement path")
        expected[send] -= 1
    elif entries:
        raise ValueError("skipped action entered DecrementPP")
    if pp["after"] != expected:
        raise ValueError("local PP delta inconsistent with execution")


def adjudicate_pair(owners, checkpoint="settled-turn"):
    """Pure, fail-closed parent validation; never compares enemy PP copies."""
    result = {
        "complete": False,
        "checkpoint_only": False,
        "gameplay_completed": False,
        "full_authentic_acceptance": False,
        "provenance": PROVENANCE,
        "status": "partial",
        "errors": [],
    }
    if checkpoint not in ("settled-turn", "room-return"):
        result.update(status="unsupported", errors=["unknown checkpoint"])
        return result
    try:
        if not isinstance(owners, (list, tuple)) or len(owners) != 2:
            raise ValueError("exactly two owner snapshots required")
        if any(not isinstance(o, dict) for o in owners):
            raise ValueError("owner snapshots must be objects")
        if {o.get("role") for o in owners} != {"listen", "connect"}:
            raise ValueError("distinct listen/connect owners required")
        for index, owner in enumerate(owners):
            if owner.get("unsupported_reason"):
                result["status"] = "unsupported"
                raise ValueError(owner["unsupported_reason"])
            _integer(owner["schema_version"], 1, 1, "schema version")
            if owner["checkpoint"] != checkpoint:
                raise ValueError("schema/checkpoint mismatch")
            if owner["version"] not in ("red", "blue", "red_color", "blue_color", "yellow"):
                raise ValueError("unsupported version")
            if "side" in owner and owner["side"] != owner["role"]:
                raise ValueError("owner side/role mismatch")
            if owner.get("move_slot") is not None:
                _integer(owner["move_slot"], 0, 3, "requested move slot")
            if owner["provenance"] != PROVENANCE or owner["full_authentic_acceptance"] is not False:
                raise ValueError("unknown provenance must remain disclosed")
            if owner["checkpoint_reached"] is not True or owner["gameplay_completed"] is not True:
                raise ValueError("local gameplay checkpoint missing")
            flags = owner["readiness"]
            if len(flags) != len(PHASES) or any(
                type(f) is not int or f not in (0, 1) for f in flags
            ):
                raise ValueError("invalid readiness")
            if not all(flags[: 8 if checkpoint == "settled-turn" else 9]):
                raise ValueError("required input phases missing")
            lead = _validate_party(owner["before_party"])
            baseline, turn = owner["baseline"], owner["turn"]
            _validate_baseline(owner["before_party"], baseline)
            _validate_local_turn(baseline, turn)
            for seq in (baseline["seq"], turn["exchange_seq"], turn["settled_seq"]):
                _integer(seq, 1, 2**63 - 1, "evidence sequence")
            if not 0 < baseline["seq"] < turn["exchange_seq"] < turn["settled_seq"]:
                raise ValueError("exchange/completed boundary ordering invalid")
            send = _integer(turn["send"], 0, 3, "send move slot")
            chosen = owner["chosen"]
            _integer(chosen["slot"], 0, 3, "chosen slot")
            _integer(chosen["move_id"], 1, 165, "chosen move ID")
            _integer(chosen["effect"], 0, 255, "chosen effect")
            if chosen != {
                "slot": send,
                "move_id": turn["local_move_id"],
                "effect": turn["local_move_effect"],
            }:
                raise ValueError("chosen move differs from supported exchanged move")
            if owner.get("move_slot") is not None and owner["move_slot"] != send:
                raise ValueError("explicit move slot not honored")
            if baseline["local"]["slot"] != lead:
                raise ValueError("first living lead mismatch")
            other = owners[1 - index]
            for phase in ("baseline", "turn"):
                for side, opposite in (("local", "enemy"), ("enemy", "local")):
                    mon = owner[phase][side]
                    _mon(mon)
                    peer = other[phase][opposite]
                    for field in ("slot", "species", "hp", "status", "moves"):
                        if mon[field] != peer[field]:
                            raise ValueError(f"reciprocal {phase} {side} {field} mismatch")
            if turn["send"] != other["turn"]["receive"] or turn["receive"] != other["turn"]["send"]:
                raise ValueError("reciprocal action bytes mismatch")
            for side in ("local", "enemy"):
                action = turn["actions"][side]
                peer_action = other["turn"]["actions"]["enemy" if side == "local" else "local"]
                if action != peer_action:
                    raise ValueError("reciprocal execution outcome mismatch")
            if checkpoint == "room-return":
                room = owner["room_return"]
                for key in ("end_seq", "return_seq", "settled_seq"):
                    _integer(room[key], 1, 2**63 - 1, "room sequence")
                _validate_healed(owner["before_party"], room["healed_party"])
                if not (
                    turn["settled_seq"] < room["end_seq"] < room["return_seq"] < room["settled_seq"]
                ):
                    raise ValueError("battle end/room return ordering invalid")
                for key, value in (
                    ("send", 15),
                    ("receive", 15),
                    ("result", 2),
                    ("map", 240),
                    ("link_state", 1),
                    ("is_in_battle", 0),
                ):
                    if type(room[key]) is not int or room[key] != value:
                        raise ValueError("natural both-RUN room cleanup missing")
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        result["errors"].append(f"invalid battle evidence: {exc}")
        if result["status"] != "unsupported":
            result["status"] = "mismatch"
        return result
    result.update(complete=True, gameplay_completed=True, status="complete")
    return result
