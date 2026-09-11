"""Shared, read-only evidence contract for one real link-battle turn.

The battle drivers are deliberately responsible for input and scheduling.  This
module only observes ROM-owned hooks and RAM and validates an immutable snapshot
after a later battle-loop boundary.  Hook counters alone are never accepted as
settlement evidence.

The pure validator is also used by the TCP parent, so malformed or fabricated
peer rows fail closed before a strict matrix row can pass.  The observer does
not write memory, press input, or advance an emulator.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# Keep the move catalog in one place with the existing bounded diagnostic.  It
# has no import-time emulator or asset side effects.
from scripts._timed_battle_probe import (
    MOVE_EFFECTS,
    SUPPORTED_EFFECTS,
    UNSUPPORTED_MOVES,
)

SCHEMA_VERSION = 1
MAX_SEQUENCE = 2**63 - 1
PARTY_MON_SIZE = 44
COLOSSEUM_MAP_ID = 0xF0

# These names are counters as well as observer events.  The continuation and
# status-branch events are registered by ``install_continuation_hooks`` below.
EVIDENCE_EVENTS = (
    "MainInBattleLoop",
    "LinkBattleExchangeData",
    "post_exchange",
    "PlayerCanExecuteMove",
    "EnemyCanExecuteMove",
    "ExecutePlayerMove",
    "ExecuteEnemyMove",
    "ExecutePlayerMoveDone",
    "ExecuteEnemyMoveDone",
    "ApplyDamageToEnemyPokemon",
    "ApplyDamageToPlayerPokemon",
    "ApplyAttackToEnemyPokemonDone",
    "ApplyAttackToPlayerPokemonDone",
    "DecrementPP",
    "local_fully_paralyzed",
    "enemy_fully_paralyzed",
    "HandlePlayerMonFainted",
    "HandleEnemyMonFainted",
    "EndOfBattle",
    "ReturnToCableClubRoom",
)

_BATTLE_MEMORY = (
    "wPlayerMonNumber",
    "wEnemyMonPartyPos",
    "wPlayerSelectedMove",
    "wEnemySelectedMove",
    "wSerialExchangeNybbleSendData",
    "wSerialExchangeNybbleReceiveData",
    "wBattleResult",
    "hWhoseTurn",
    "wMoveMissed",
    "wDamage",
    "wIsInBattle",
    "wLinkState",
    "wCurMap",
)


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _read(session: Any, name: str, size: int = 1) -> int | list[int]:
    address = session.symbols.addr_of(name)
    values = [int(session._pyboy.memory[address + index]) for index in range(size)]
    if any(type(value) is not int or not 0 <= value <= 255 for value in values):
        raise ValueError(f"invalid {name} memory byte")
    return values[0] if size == 1 else values


def _rom(session: Any, bank: int, address: int, size: int) -> bytes:
    return bytes(int(session._pyboy.memory[bank, address + index]) for index in range(size))


def _mon(mon: dict[str, Any], *, allow_faint: bool = False) -> None:
    _integer(mon["slot"], 0, 5, "active slot")
    _integer(mon["species"], 1, 190, "active species")
    hp = _integer(mon["hp"], 0 if allow_faint else 1, 65535, "active HP")
    max_hp = _integer(mon["max_hp"], 1, 65535, "active max HP")
    if hp > max_hp:
        raise ValueError("active HP exceeds max HP")
    _integer(mon["status"], 0, 255, "active status")
    for key in ("moves", "pp"):
        values = mon[key]
        if not isinstance(values, list) or len(values) != 4:
            raise ValueError(f"invalid {key} length")
        for value in values:
            _integer(value, 0, 255, f"{key} byte")


def _party(session: Any) -> dict[str, Any]:
    count = _integer(_read(session, "wPartyCount"), 1, 6, "party count")
    species = list(_read(session, "wPartySpecies", count + 1))
    base = session.symbols.addr_of("wPartyMons")
    records = [
        bytes(int(session._pyboy.memory[base + index * PARTY_MON_SIZE + offset])
              for offset in range(PARTY_MON_SIZE)).hex()
        for index in range(count)
    ]
    return {"count": count, "species": species, "mon_species": species[:-1], "mon_records": records}


def _combatants(session: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for side, prefix, slot_name in (
        ("local", "wBattleMon", "wPlayerMonNumber"),
        ("enemy", "wEnemyMon", "wEnemyMonPartyPos"),
    ):
        hp = _read(session, prefix + "HP", 2)
        max_hp = _read(session, prefix + "MaxHP", 2)
        result[side] = {
            "slot": _read(session, slot_name),
            "species": _read(session, prefix + "Species"),
            "hp": hp[0] * 256 + hp[1],
            "max_hp": max_hp[0] * 256 + max_hp[1],
            "status": _read(session, prefix + "Status"),
            "moves": list(_read(session, prefix + "Moves", 4)),
            "pp": list(_read(session, prefix + "PP", 4)),
        }
    return result


def _move_data(session: Any, move: int) -> bytes:
    _integer(move, 1, 165, "move ID")
    bank, address = session.symbols.bank_addr("Moves")
    data = _rom(session, bank, address + (move - 1) * 6, 6)
    if data[0] != move:
        raise ValueError("move table ID mismatch")
    return data


def _validate_party(party: dict[str, Any]) -> int:
    count = _integer(party["count"], 1, 6, "party count")
    species = party["species"]
    records = party.get("mon_records", party.get("records"))
    if not isinstance(species, list) or len(species) != count + 1 or species[-1] != 255:
        raise ValueError("invalid party species terminator")
    if not isinstance(records, list) or len(records) != count:
        raise ValueError("invalid party record count")
    living: list[int] = []
    for index, raw in enumerate(records):
        data = bytes.fromhex(raw)
        _integer(species[index], 1, 190, "party species")
        if len(data) != PARTY_MON_SIZE or data[0] != species[index]:
            raise ValueError("invalid party record/species")
        if int.from_bytes(data[1:3], "big"):
            living.append(index)
    if not living:
        raise ValueError("party has no living Pokémon")
    return living[0]


def _validate_baseline(party: dict[str, Any], baseline: dict[str, Any]) -> None:
    lead = _validate_party(party)
    records = party.get("mon_records", party.get("records"))
    data = bytes.fromhex(records[lead])
    expected = {
        "slot": lead,
        "species": data[0],
        "hp": int.from_bytes(data[1:3], "big"),
        "max_hp": int.from_bytes(data[34:36], "big"),
        "status": data[4],
        "moves": list(data[8:12]),
        "pp": list(data[29:33]),
    }
    if baseline["local"] != expected:
        raise ValueError("party to automatic lead baseline mismatch")


def _validate_action(action: dict[str, Any]) -> None:
    if not isinstance(action, dict):
        raise TypeError("action is not an object")
    if type(action.get("executed")) is not bool:
        raise ValueError("action executed flag is invalid")
    if type(action.get("done")) is not bool:
        raise ValueError("action done flag is invalid")
    reason = action.get("skip_reason")
    if reason not in (None, "fully_paralyzed", "opponent_fainted"):
        raise ValueError("unsupported action skip reason")
    _integer(action.get("damage_done"), 0, 2, "damage completions")
    missed = action.get("move_missed")
    if missed is not None:
        _integer(missed, 0, 1, "move miss flag")
    samples = action.get("damage_samples")
    if not isinstance(samples, list) or len(samples) != action["damage_done"]:
        raise ValueError("damage completion/sample count mismatch")
    for sample in samples:
        if not isinstance(sample, dict):
            raise TypeError("damage sample is not an object")
        before = _integer(sample.get("before_hp"), 1, 65535, "pre-application HP")
        after = _integer(sample.get("after_hp"), 0, 65535, "post-application HP")
        amount = _integer(sample.get("damage"), 0, 65535, "calculated damage")
        _integer(sample.get("move_missed"), 0, 0, "applied damage miss flag")
        if after > before or before - after != amount:
            raise ValueError("calculated damage disagrees with HP application")


def validate_turn(baseline: dict[str, Any], turn: dict[str, Any]) -> None:
    """Validate one owner's observed application path, without repairing it."""
    if not isinstance(baseline, dict) or not isinstance(turn, dict):
        raise TypeError("baseline/turn evidence is not an object")
    send = _integer(turn.get("send"), 0, 3, "send slot")
    receive = _integer(turn.get("receive"), 0, 3, "receive slot")
    for side, opposite, slot, move_key in (
        ("local", "enemy", send, "local_move_id"),
        ("enemy", "local", receive, "enemy_move_id"),
    ):
        before, after = baseline[side], turn[side]
        _mon(before)
        _mon(after, allow_faint=True)
        if any(before[key] != after[key] for key in ("slot", "species", "max_hp", "moves")):
            raise ValueError("active combatant identity changed during turn")
        move = _integer(turn.get(move_key), 1, 165, "selected move ID")
        effect = _integer(turn.get(f"{side}_move_effect"), 0, 255, "move effect")
        data = turn.get("move_data", {}).get(side) if isinstance(turn.get("move_data"), dict) else None
        if move in UNSUPPORTED_MOVES:
            raise ValueError(UNSUPPORTED_MOVES[move])
        if move not in MOVE_EFFECTS or effect not in SUPPORTED_EFFECTS:
            raise ValueError("unsupported exchanged move effect")
        if data is not None and (data[0] != move or data[1] != effect or not data[2]):
            raise ValueError("move table metadata disagrees with exchange")
        if before["moves"][slot] != move:
            raise ValueError("move ID does not match exchanged slot")

        action = turn["actions"][side]
        _validate_action(action)
        reason = action["skip_reason"]
        if reason == "fully_paralyzed":
            if action["executed"] or action["damage_done"] or action["done"]:
                raise ValueError("paralyzed action has execution evidence")
        elif reason == "opponent_fainted":
            if action["executed"] or action["damage_done"] or action["done"]:
                raise ValueError("faint-skipped action has execution evidence")
            if turn[opposite]["hp"] == 0:
                raise ValueError("faint skip target is the wrong combatant")
        elif not action["executed"] or not action["done"]:
            raise ValueError("action did not complete before settled boundary")

        hp = baseline[opposite]["hp"]
        for sample in action["damage_samples"]:
            if sample["before_hp"] != hp:
                raise ValueError("damage sample is not chained to prior HP")
            hp = sample["after_hp"]
        if hp != turn[opposite]["hp"]:
            raise ValueError("HP delta lacks completed application evidence")
        missed = action["move_missed"]
        if action["executed"] and not action["damage_samples"] and missed != 1:
            raise ValueError("zero outcome lacks an observed miss or application")
        if action["damage_samples"] and missed == 1:
            raise ValueError("miss flag contradicts completed application")
        if (
            effect == 44
            and action["damage_done"] not in (0, 2)
            # A first hit may legitimately KO and suppress the second hit.
            and not (turn[opposite]["hp"] == 0 and action["damage_done"] == 1)
        ):
            raise ValueError("multi-hit move did not complete its applications")
        old_status, new_status = before["status"], after["status"]
        if old_status != new_status and not (
            old_status == 0
            and new_status == 64
            and effect == 6
            and action["executed"]
            and action["damage_samples"]
            and missed != 1
        ):
            raise ValueError("status change lacks supported application evidence")

    pp = turn["pp"]
    if pp["before"] != baseline["local"]["pp"] or pp["after"] != turn["local"]["pp"]:
        raise ValueError("local PP evidence differs from snapshots")
    entries = _integer(pp.get("decrement_entries"), 0, 100000, "PP decrement entries")
    expected = list(pp["before"])
    local = turn["actions"]["local"]
    if local["executed"]:
        slot = send
        if entries != 1 or not expected[slot] & 63:
            raise ValueError("executed move lacks exactly one local PP decrement")
        expected[slot] -= 1
    elif entries:
        raise ValueError("skipped action entered PP decrement")
    if pp["after"] != expected:
        raise ValueError("local PP delta is inconsistent with execution")


def _normalise_row(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    evidence = row.get("battle_turn")
    if evidence is None and "turn" in row and "baseline" in row:
        evidence = row
    return evidence if isinstance(evidence, dict) else None


def verify_battle_turns(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...], *, require_cleanup: bool = False) -> list[str]:
    """Return fail-closed peer errors; never mutate either row."""
    errors: list[str] = []
    if not isinstance(rows, (list, tuple)) or len(rows) != 2:
        return ["exactly two battle peer rows are required"]
    evidence = [_normalise_row(row) for row in rows]
    if any(item is None for item in evidence):
        return ["settled snapshot is missing"]
    left, right = evidence  # type: ignore[misc]
    try:
        for item in (left, right):
            if item.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("invalid battle evidence schema")
            if item.get("unsupported_reason"):
                raise ValueError(str(item["unsupported_reason"]))
            if item.get("settled") is not True:
                raise ValueError("settled snapshot is missing")
            _integer(item.get("exchange_seq"), 1, MAX_SEQUENCE, "exchange sequence")
            _integer(item.get("settled_seq"), 1, MAX_SEQUENCE, "settled sequence")
            if item["exchange_seq"] >= item["settled_seq"]:
                raise ValueError("exchange/settled boundary ordering invalid")
            validate_turn(item["baseline"], item["turn"])
            terminal = item.get("terminal")
            if (
                any(item["turn"][side]["hp"] == 0 for side in ("local", "enemy"))
                and (not isinstance(terminal, dict) or terminal.get("boundary") != "EndOfBattle")
            ):
                raise ValueError("KO outcome lacks EndOfBattle evidence")
            if terminal is not None:
                _integer(terminal.get("seq"), item["settled_seq"] + 1, MAX_SEQUENCE, "terminal sequence")
                _integer(terminal.get("result"), 0, 255, "battle result")
        for phase in ("baseline", "turn"):
            for side, opposite in (("local", "enemy"), ("enemy", "local")):
                for field in ("slot", "species", "hp", "max_hp", "status", "moves"):
                    if left[phase][side][field] != right[phase][opposite][field]:
                        raise ValueError(f"battle peers disagree on settled {phase} combatant state")
        if left["turn"]["send"] != right["turn"]["receive"] or left["turn"]["receive"] != right["turn"]["send"]:
            raise ValueError("battle peers disagree on exchanged move slots")
        for side in ("local", "enemy"):
            other_side = "enemy" if side == "local" else "local"
            if left["turn"][f"{side}_move_id"] != right["turn"][f"{other_side}_move_id"]:
                raise ValueError("battle peers disagree on exchanged move IDs")
            if left["turn"]["actions"][side] != right["turn"]["actions"][other_side]:
                raise ValueError("battle peers disagree on execution/application outcome")
        for field in ("result",):
            lterm, rterm = left.get("terminal"), right.get("terminal")
            if (lterm is None) != (rterm is None):
                raise ValueError("battle terminal evidence is asymmetric")
            if (
                lterm is not None
                and lterm[field] != rterm[field]
                and {lterm[field], rterm[field]} != {0, 1}
            ):
                raise ValueError("battle terminal results are not complementary")
        if require_cleanup or left.get("cleanup") is not None or right.get("cleanup") is not None:
            for item in (left, right):
                cleanup = item.get("cleanup")
                if not isinstance(cleanup, dict) or cleanup.get("is_in_battle") != 0:
                    raise ValueError("battle cleanup is incomplete")
                if cleanup.get("map") != COLOSSEUM_MAP_ID or cleanup.get("link_state") != 1:
                    raise ValueError("battle cleanup returned to an unexpected state")
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        return [str(exc)]
    return errors


def adjudicate_pair(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...], *, require_cleanup: bool = False) -> dict[str, Any]:
    """Serializable wrapper used by strict parent tests and diagnostics."""
    errors = verify_battle_turns(rows, require_cleanup=require_cleanup)
    return {
        "complete": not errors,
        "gameplay_completed": not errors,
        "full_authentic_acceptance": False,
        "status": "complete" if not errors else "mismatch",
        "errors": errors,
    }


class BattleTurnObserver:
    """Observe one owner through a settled battle boundary.

    Call :meth:`observe` from the existing ROM hook callback.  The observer
    intentionally does not own the emulator hook registration, allowing local
    and TCP counter/history callbacks to remain the sole registration owner.
    """

    def __init__(self, session: Any, *, role: str, version: str, before_party: dict[str, Any] | None = None) -> None:
        self.session = session
        self.role, self.version = role, version
        self.sequence = 0
        self.counts = {name: 0 for name in EVIDENCE_EVENTS}
        self.baseline: dict[str, Any] | None = None
        self.exchange: dict[str, Any] | None = None
        self.turn: dict[str, Any] | None = None
        self.terminal: dict[str, Any] | None = None
        self.cleanup: dict[str, Any] | None = None
        self.error: str | None = None
        self.before_party = deepcopy(before_party) if before_party is not None else _party(session)
        self.actions = {
            side: {
                "executed": False,
                "done": False,
                "skip_reason": None,
                "damage_done": 0,
                "move_missed": None,
                "damage_samples": [],
            }
            for side in ("local", "enemy")
        }
        self.pending = {"local": None, "enemy": None}
        self.pp_entries = 0
        self.faint_seen = False
        self._selected_slot = None

        # Resolve all data symbols before a caller installs the first hook.
        for name in _BATTLE_MEMORY:
            session.symbols.addr_of(name)
        self._validate_initial_party()

    def _validate_initial_party(self) -> None:
        _validate_party(self.before_party)

    def _capture_exchange(self) -> None:
        if self.exchange is not None:
            return
        if self.baseline is None:
            raise ValueError("move exchange observed before battle baseline")
        send = _integer(_read(self.session, "wSerialExchangeNybbleSendData"), 0, 3, "send slot")
        receive = _integer(_read(self.session, "wSerialExchangeNybbleReceiveData"), 0, 3, "receive slot")
        active = _combatants(self.session)
        local_move = _read(self.session, "wPlayerSelectedMove")
        enemy_move = _read(self.session, "wEnemySelectedMove")
        if not local_move:
            local_move = active["local"]["moves"][send]
        if not enemy_move:
            enemy_move = active["enemy"]["moves"][receive]
        moves = {"local": _integer(local_move, 1, 165, "local move ID"), "enemy": _integer(enemy_move, 1, 165, "enemy move ID")}
        data: dict[str, list[int]] = {}
        effects: dict[str, int] = {}
        for side in ("local", "enemy"):
            move = moves[side]
            if move in UNSUPPORTED_MOVES:
                raise ValueError(UNSUPPORTED_MOVES[move])
            row = _move_data(self.session, move)
            if move not in MOVE_EFFECTS or row[1] not in SUPPORTED_EFFECTS or not row[2]:
                raise ValueError("unsupported exchanged move effect")
            effects[side] = row[1]
            data[side] = list(row)
        self.exchange = {
            "exchange_seq": self.sequence,
            "send": send,
            "receive": receive,
            "local_move_id": moves["local"],
            "enemy_move_id": moves["enemy"],
            "local_move_effect": effects["local"],
            "enemy_move_effect": effects["enemy"],
            "move_data": data,
        }

    def _capture_turn(self, *, boundary: str) -> None:
        if self.turn is not None or self.exchange is None or self.baseline is None:
            return
        if not all(action["done"] or action["skip_reason"] == "opponent_fainted" for action in self.actions.values()):
            return
        combatants = _combatants(self.session)
        turn = {
            **deepcopy(self.exchange),
            "settled_seq": self.sequence,
            "boundary": boundary,
            **combatants,
            "actions": deepcopy(self.actions),
            "pp": {
                "before": list(self.baseline["local"]["pp"]),
                "after": list(combatants["local"]["pp"]),
                "decrement_entries": self.pp_entries,
            },
            "outcome": "ko" if any(mon["hp"] == 0 for mon in combatants.values()) else "settled",
        }
        validate_turn(self.baseline, turn)
        self.turn = deepcopy(turn)

    def _observe(self, name: str) -> None:
        self.sequence += 1
        if self.sequence >= MAX_SEQUENCE:
            raise ValueError("observation sequence exhausted")
        self.counts.setdefault(name, 0)
        self.counts[name] += 1
        if name == "ReturnToCableClubRoom":
            if self.terminal is None:
                raise ValueError("room return without EndOfBattle")
            self.cleanup = {
                "seq": self.sequence,
                "map": _read(self.session, "wCurMap"),
                "link_state": _read(self.session, "wLinkState"),
                "is_in_battle": _read(self.session, "wIsInBattle"),
            }
            return
        if name == "MainInBattleLoop":
            if self.baseline is None:
                baseline = _combatants(self.session)
                for mon in baseline.values():
                    _mon(mon)
                _validate_baseline(self.before_party, baseline)
                self.baseline = {"seq": self.sequence, **baseline}
            elif self.exchange is not None and self.turn is None:
                self._capture_turn(boundary=name)
            return
        if name in ("LinkBattleExchangeData", "post_exchange"):
            self._capture_exchange()
            return
        if self.exchange is None or self.turn is not None:
            if name == "EndOfBattle":
                self._record_terminal()
            return
        mapping = {
            "PlayerCanExecuteMove": ("local", "executed"),
            "EnemyCanExecuteMove": ("enemy", "executed"),
            "ExecutePlayerMove": ("local", "entered"),
            "ExecuteEnemyMove": ("enemy", "entered"),
            "ExecutePlayerMoveDone": ("local", "done"),
            "ExecuteEnemyMoveDone": ("enemy", "done"),
        }
        if name in mapping:
            side, field = mapping[name]
            if field == "executed":
                self.actions[side][field] = True
            elif field == "done":
                self.actions[side][field] = True
                self.actions[side]["move_missed"] = _integer(_read(self.session, "wMoveMissed"), 0, 1, "move miss flag")
            return
        if name in ("local_fully_paralyzed", "enemy_fully_paralyzed"):
            side = name.split("_")[0]
            expected = 0 if side == "local" else 1
            if _read(self.session, "hWhoseTurn") != expected:
                raise ValueError("paralysis branch turn mismatch")
            self.actions[side]["skip_reason"] = "fully_paralyzed"
            return
        if name in ("ApplyDamageToEnemyPokemon", "ApplyDamageToPlayerPokemon"):
            side = "local" if name.endswith("EnemyPokemon") else "enemy"
            if not self.actions[side]["executed"]:
                raise ValueError("damage application before action execution")
            prefix = "wEnemyMon" if side == "local" else "wBattleMon"
            hp = _read(self.session, prefix + "HP", 2)
            damage = _read(self.session, "wDamage", 2)
            if self.pending[side] is not None:
                raise ValueError("overlapping damage application")
            self.pending[side] = {"before_hp": hp[0] * 256 + hp[1], "damage": damage[0] * 256 + damage[1]}
            return
        if name in ("ApplyAttackToEnemyPokemonDone", "ApplyAttackToPlayerPokemonDone"):
            side = "local" if name.endswith("EnemyPokemonDone") else "enemy"
            pending = self.pending[side]
            if pending is None or not self.actions[side]["executed"]:
                raise ValueError("damage Done without actual application")
            prefix = "wEnemyMon" if side == "local" else "wBattleMon"
            hp = _read(self.session, prefix + "HP", 2)
            pending = {**pending, "after_hp": hp[0] * 256 + hp[1], "move_missed": _read(self.session, "wMoveMissed")}
            if len(self.actions[side]["damage_samples"]) >= 2:
                raise ValueError("unsupported damage application count")
            self.actions[side]["damage_samples"].append(pending)
            self.actions[side]["damage_done"] += 1
            self.pending[side] = None
            return
        if name == "DecrementPP":
            if _read(self.session, "hWhoseTurn") == 0:
                if not self.actions["local"]["executed"] or self.actions["local"]["done"]:
                    raise ValueError("PP decrement outside local execution path")
                self.pp_entries += 1
            return
        if name in ("HandlePlayerMonFainted", "HandleEnemyMonFainted"):
            self.faint_seen = True
            side = "local" if name.startswith("HandlePlayer") else "enemy"
            if not self.actions[side]["executed"]:
                self.actions[side]["skip_reason"] = "opponent_fainted"
            return
        if name == "EndOfBattle":
            self._record_terminal()
            if self.turn is None:
                self._capture_turn(boundary=name)
            return
    def _record_terminal(self) -> None:
        result = _integer(_read(self.session, "wBattleResult"), 0, 255, "battle result")
        self.terminal = {
            "boundary": "EndOfBattle",
            "seq": self.sequence,
            "result": result,
            "send": _read(self.session, "wSerialExchangeNybbleSendData"),
            "receive": _read(self.session, "wSerialExchangeNybbleReceiveData"),
        }

    def observe(self, name: str) -> None:
        """Record an event and retain an error instead of mutating the ROM."""
        if self.error is not None:
            return
        try:
            self._observe(name)
        except (AttributeError, KeyError, TypeError, ValueError, IndexError) as exc:
            self.error = f"battle observation {name}: {type(exc).__name__}: {exc}"[:512]

    record = observe

    def snapshot(self) -> dict[str, Any]:
        return deepcopy({
            "schema_version": SCHEMA_VERSION,
            "role": self.role,
            "version": self.version,
            "settled": self.turn is not None and self.error is None,
            "baseline": self.baseline,
            "turn": self.turn,
            "battle_turn": self.turn,
            "exchange_seq": self.turn["exchange_seq"] if self.turn else None,
            "settled_seq": self.turn["settled_seq"] if self.turn else None,
            "terminal": self.terminal,
            "cleanup": self.cleanup,
            "unsupported_reason": self.error,
            "hook_counts": self.counts,
            "before_party": self.before_party,
        })


def continuation_locations(session: Any, version: str) -> tuple[tuple[str, int, int], ...]:
    """Return verified post-exchange/status branch hooks for a ROM session."""
    bank, start = session.symbols.bank_addr("SelectEnemyMove")
    expected = 0x56E3 if version == "yellow" else 0x5571
    if (bank, start + 13) != (15, expected):
        raise ValueError("ordinary post-exchange address mismatch")
    target_bank, target = session.symbols.bank_addr("LinkBattleExchangeData")
    load_bank, load = session.symbols.bank_addr("LoadScreenTilesFromBuffer1")
    expected_load = 0x371B if version == "yellow" else 0x3725
    receive = session.symbols.addr_of("wSerialExchangeNybbleReceiveData")
    expected_bytes = b"\xcd" + target.to_bytes(2, "little") + b"\xcd" + load.to_bytes(2, "little") + b"\xfa" + receive.to_bytes(2, "little")
    actual = bytes(int(session._pyboy.memory[bank, start + 10 + index]) for index in range(9))
    if target_bank != bank or (load_bank, load) != (0, expected_load) or actual != expected_bytes:
        raise ValueError("post-exchange callsite bytes mismatch")
    text = session.symbols.addr_of("FullyParalyzedText")
    printer = session.symbols.addr_of("PrintText")
    locations = [("post_exchange", bank, start + 13)]
    for side, label in (("local", "CheckPlayerStatusConditions.MonHurtItselfOrFullyParalysed"), ("enemy", "CheckEnemyStatusConditions.monHurtItselfOrFullyParalysed")):
        branch_bank, branch = session.symbols.bank_addr(label)
        signature = b"\x21" + text.to_bytes(2, "little") + b"\xcd" + printer.to_bytes(2, "little")
        actual = bytes(int(session._pyboy.memory[branch_bank, branch - 6 + index]) for index in range(6))
        if actual != signature:
            raise ValueError(f"{side} paralysis branch bytes mismatch")
        locations.append((f"{side}_fully_paralyzed", branch_bank, branch - 6))
    return tuple(locations)


def install_continuation_hooks(session: Any, observer: BattleTurnObserver, *, version: str) -> list[tuple[int, int]]:
    """Install only the verified continuation hooks, returning owned locations."""
    owned: list[tuple[int, int]] = []
    for name, bank, address in continuation_locations(session, version):
        session._pyboy.hook_register(bank, address, lambda _ctx, name=name: observer.observe(name), None)
        owned.append((bank, address))
    return owned


__all__ = [
    "EVIDENCE_EVENTS",
    "SCHEMA_VERSION",
    "BattleTurnObserver",
    "adjudicate_pair",
    "continuation_locations",
    "install_continuation_hooks",
    "validate_turn",
    "verify_battle_turns",
]
