"""Owner-thread trade inputs and bounded observations; never advances a CPU.

Contracts follow engine/link/cable_club.asm in pokered and pokeyellow:
RemovePokemon compacts, AddEnemyMonToPlayerParty appends, TryEvolvingMon
precedes evolution, and the post-save DoBattleOrTradeAgain precedes patching.
Only non-trade-evolving outgoing species are supported. No byte masks.
"""

from __future__ import annotations

import threading
from collections import deque
from copy import deepcopy

READINESS_PHASES = (
    "party_qualified",
    "link_menu_trade_ready",
    "link_menu_a_applied",
    "trade_center_reached",
    "select_mon_ready",
    "outgoing_slot_ready",
    "pre_evolution_copy_validated",
    "post_save_cycle_returned",
)
PHASES = READINESS_PHASES
INPUT_LIMITS = {"approach": 3, "hidden_event": 6, "cursor": 12, "dialogue": 32}
TRADE_EVOLUTIONS = frozenset((0x26, 0x27, 0x29, 0x93))
MENU_FIELDS = (
    "wCurrentMenuItem",
    "wMaxMenuItem",
    "wMenuWatchedKeys",
    "wTopMenuItemX",
    "wTopMenuItemY",
    "wWhichTradeMonSelectionMenu",
    "wTradingWhichPlayerMon",
    "wTradingWhichEnemyMon",
    "wTwoOptionMenuID",
    "wCurMap",
    "wXCoord",
    "wYCoord",
    "wLinkState",
    "hSerialConnectionStatus",
)
OBSERVATION_SYMBOLS = (
    "SaveGameData",
    "Serial_SyncAndExchangeNybble",
    "LinkMenu.waitForInputLoop",
    "LinkMenu.doneChoosingMenuSelection",
    "PrepareForSpecialWarp",
    "CableClubLeftGameboy",
    "CableClubRightGameboy",
    "CableClub_DoBattleOrTrade",
    "TradeCenter_SelectMon",
    "TradeCenter_SelectMon.playerMonMenu_HandleInput",
    "TradeCenter_SelectMon.chosePlayerMon",
    "TradeCenter_SelectMon.selectStatsMenuItem",
    "TradeCenter_SelectMon.selectTradeMenuItem",
    "TradeCenter_SelectMon.choseTrade",
    "TradeCenter_Trade",
    "DisplayTwoOptionMenu",
    "HandleMenuInput",
    "TradeCenter_Trade.tradeConfirmed",
    "_AddEnemyMonToPlayerParty",
    "TryEvolvingMon",
    "SavePartyAndDexData",
    "CableClub_DoBattleOrTradeAgain",
)


def _read(session, name, size=1):
    address = session.symbols.addr_of(name)
    values = tuple(int(session._pyboy.memory[address + i]) for i in range(size))
    return values[0] if size == 1 else values


def read_party(session):
    """Internal owner-only reader; callers must already hold owner context.

    The driver invokes this only during construction or checked hook callbacks.
    This function neither transfers ownership nor makes concurrent reads safe.
    """
    count = _read(session, "wPartyCount")
    if not 1 <= count <= 6:
        raise ValueError(f"invalid party count: {count}")
    party = {"count": count, "species": list(_read(session, "wPartySpecies", count + 1))}
    for field, symbol, size in (
        ("records", "wPartyMons", 44),
        ("ot_names", "wPartyMonOT", 11),
        ("nicknames", "wPartyMonNicks", 11),
    ):
        base = session.symbols.addr_of(symbol)
        party[field] = [
            bytes(int(session._pyboy.memory[base + i * size + j]) for j in range(size)).hex()
            for i in range(count)
        ]
    _validate_party(party)
    return party


def _validate_party(party):
    count = party["count"]
    if type(count) is not int or not 1 <= count <= 6:
        raise ValueError("party count must be 1..6")
    if (
        len(party["species"]) != count + 1
        or any(type(value) is not int for value in party["species"])
        or party["species"][-1] != 255
    ):
        raise ValueError("invalid species terminator/length")
    for key, size in (("records", 44), ("ot_names", 11), ("nicknames", 11)):
        if len(party[key]) != count:
            raise ValueError(f"invalid {key} count")
        for value in party[key]:
            if len(bytes.fromhex(value)) != size:
                raise ValueError(f"invalid {key} length")
    for i, species in enumerate(party["species"][:-1]):
        if not 1 <= species <= 190 or bytes.fromhex(party["records"][i])[0] != species:
            raise ValueError("party species/record mismatch")


def _expected(before, outgoing, peer, incoming):
    _validate_party(before)
    _validate_party(peer)
    if type(outgoing) is not int or not 0 <= outgoing < before["count"]:
        raise ValueError("outgoing slot outside party")
    if type(incoming) is not int or not 0 <= incoming < peer["count"]:
        raise ValueError("peer outgoing slot outside party")
    result = {"count": before["count"]}
    for key in ("records", "ot_names", "nicknames"):
        result[key] = before[key][:outgoing] + before[key][outgoing + 1 :] + [peer[key][incoming]]
    species = before["species"][:-1]
    result["species"] = (
        species[:outgoing] + species[outgoing + 1 :] + [peer["species"][incoming], 255]
    )
    return result


class TradeOwnerDriver:
    """All callbacks execute on the constructing owner thread, without ticking."""

    def __init__(self, *, session, side, options, peer_state):
        self.session = session
        self.thread = threading.get_ident()
        self.side = side
        self.version = options["version"]
        self.slot = options["outgoing_slot"]
        self.checkpoint = options["checkpoint"]
        if side not in ("listen", "connect") or self.checkpoint not in (
            "select-mon",
            "reciprocal-exchange",
        ):
            raise ValueError("unsupported role/checkpoint")
        self.peer = peer_state
        self.flags = [0] * len(PHASES)
        self.phase = "approach"
        self.counts = dict.fromkeys(OBSERVATION_SYMBOLS, 0)
        self.sequence = 0
        self.last = {}
        self.events = deque(maxlen=32)
        self.next_input = 0
        self.approach_steps = 0
        self.sent = set()
        self.input_attempts = {}
        self.menu_context = None
        self.poll_context = None
        self.copy_entry = 0
        self.save_entry = 0
        self.copy = None
        self.final = None
        self.error = None
        self.before = read_party(session)
        if type(self.slot) is not int or not 0 <= self.slot < self.before["count"]:
            raise ValueError("outgoing slot outside initial party")
        if self.before["species"][self.slot] in TRADE_EVOLUTIONS:
            raise ValueError("unsupported outgoing trade evolution before input")
        # Resolve everything first; instrumentation is observational only.
        addresses = [(name, session.symbols.bank_addr(name)) for name in OBSERVATION_SYMBOLS]
        self.hooks = []
        try:
            for name, (bank, address) in addresses:
                session._pyboy.hook_register(bank, address, self._callback, name)
                self.hooks.append((bank, address))
            self._publish("party_qualified")
        except BaseException as install_error:
            try:
                self.close()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve both failures
                raise BaseExceptionGroup(
                    "trade observer construction and cleanup failed",
                    [install_error, cleanup_error],
                ) from None
            raise

    def _owner(self):
        if threading.get_ident() != self.thread:
            raise RuntimeError("trade driver called outside owner thread")

    def _publish(self, phase):
        index = PHASES.index(phase)
        if not self.flags[index]:
            self.flags[index] = 1
            self.peer.publish_ready(phase)

    def _menu(self):
        return {name: _read(self.session, name) for name in MENU_FIELDS}

    def _callback(self, name):
        try:
            self._observe(name)
        except BaseException as exc:  # noqa: BLE001 - native callbacks may swallow exceptions
            if self.error is None:
                self.error = f"observation {name} failed: {type(exc).__name__}: {exc}"

    def _observe(self, name):
        self._owner()
        self.sequence += 1
        self.counts[name] += 1
        self.last[name] = self.sequence
        contexts = {
            "LinkMenu.waitForInputLoop": "link",
            "LinkMenu.doneChoosingMenuSelection": "warp",
            "PrepareForSpecialWarp": "warp",
            "CableClubLeftGameboy": "dialogue",
            "CableClubRightGameboy": "dialogue",
            "CableClub_DoBattleOrTrade": "exchange",
            "TradeCenter_SelectMon": "select_setup",
            "TradeCenter_SelectMon.playerMonMenu_HandleInput": "party",
            "TradeCenter_SelectMon.chosePlayerMon": "submenu_setup",
            "TradeCenter_SelectMon.selectStatsMenuItem": "stats",
            "TradeCenter_SelectMon.selectTradeMenuItem": "trade_choice",
            "TradeCenter_SelectMon.choseTrade": "selection_exchange",
            "TradeCenter_Trade": "confirmation_setup",
            "TradeCenter_Trade.tradeConfirmed": "trade_running",
        }
        if name in contexts:
            context = contexts[name]
            if context != self.menu_context:
                self.events.append({"seq": self.sequence, "phase": context})
            self.menu_context = context
            self.poll_context = None
        if (
            name == "DisplayTwoOptionMenu"
            and self.menu_context == "confirmation_setup"
            and _read(self.session, "wTwoOptionMenuID") == 5
        ):
            self.menu_context = "confirmation"
        if name == "HandleMenuInput":
            self.poll_context = self.menu_context
        if name == "_AddEnemyMonToPlayerParty" and self.menu_context == "trade_running":
            self.copy_entry = self.sequence
        if name == "TryEvolvingMon" and self.copy_entry and self.copy is None:
            self.copy = read_party(self.session)
            count = self.before["count"]
            valid = self.copy["count"] == count
            for key in ("records", "ot_names", "nicknames"):
                valid = valid and self.copy[key][:-1] == (
                    self.before[key][: self.slot] + self.before[key][self.slot + 1 :]
                )
            if not valid:
                self.error = "post-copy survivor compaction mismatch"
            elif self.copy["species"][-2] in TRADE_EVOLUTIONS:
                self.error = "unsupported incoming trade evolution"
            else:
                self._publish("pre_evolution_copy_validated")
        if name == "SavePartyAndDexData" and self.copy is not None:
            self.save_entry = self.sequence
        if name == "CableClub_DoBattleOrTradeAgain" and self.save_entry and self.final is None:
            # This later control-flow entry proves the preceding save returned.
            # Capture before this routine patches FE bytes in the party arrays.
            self.final = read_party(self.session)
            if self.final != self.copy:
                self.error = "unsupported final party transformation"
            elif not self.error:
                self._publish("post_save_cycle_returned")

    def before_step(self, *, frame_offset):
        self._owner()
        if self.error:
            raise RuntimeError(self.error)
        if self.objective_complete() or frame_offset < self.next_input:
            return None
        menu = self._menu()
        item = menu["wCurrentMenuItem"]
        maximum = menu["wMaxMenuItem"]
        keys = menu["wMenuWatchedKeys"]

        def pulse(button, duration, cadence, once=None):
            if once is not None:
                if once in self.sent:
                    return None
                self.sent.add(once)
            if once is None:
                if button == "a":
                    kind = "dialogue"
                elif self.menu_context is None and not self.flags[3]:
                    kind = "approach"
                elif self.flags[3] and self.menu_context in (None, "warp"):
                    kind = "hidden_event"
                else:
                    kind = "cursor"
                budget_key = (kind, self.menu_context)
                attempts = self.input_attempts.get(budget_key, 0)
                if attempts >= INPUT_LIMITS[kind]:
                    self.error = f"{kind} input quota exhausted"
                    raise RuntimeError(self.error)
                self.input_attempts[budget_key] = attempts + 1
            self.next_input = frame_offset + cadence
            return button, duration

        if not self.peer.peer_ready("party_qualified"):
            return None
        if menu["wCurMap"] == 0xEF:
            self._publish("trade_center_reached")
        if self.flags[7] or self.copy is not None:
            return None
        context = self.menu_context
        self.phase = context or "approach"
        if context == "link":
            expected = 3 if self.version == "yellow" else 2
            if maximum != expected or not 0 <= item <= expected or not keys & 1:
                return None
            if item != 0:
                return pulse("up", 12, 40)
            self._publish("link_menu_trade_ready")
            if self.peer.peer_ready("link_menu_trade_ready"):
                action = pulse("a", 4, 20, "link_a")
                if action:
                    self._publish("link_menu_a_applied")
                return action
            return None
        if context in ("exchange", "select_setup", "selection_exchange", "trade_running"):
            return None
        if context == "party" and self.poll_context == "party":
            if (
                maximum != self.before["count"]
                or keys != 0x91
                or menu["wWhichTradeMonSelectionMenu"] != 0
                or menu["wTopMenuItemX"] != 1
                or menu["wTopMenuItemY"] != 1
            ):
                return None
            if not 0 <= item < maximum:
                return None
            self._publish("select_mon_ready")
            if self.checkpoint == "select-mon":
                return None
            if not self.peer.peer_ready("select_mon_ready"):
                return None
            if item != self.slot:
                # HandleMenuInput moves UP before testing watched return keys.
                return pulse("down" if item < self.slot else "up", 2, 4)
            self._publish("outgoing_slot_ready")
            if self.peer.peer_ready("outgoing_slot_ready"):
                return pulse("a", 4, 20, "party_a")
            return None
        if context in ("stats", "trade_choice") and self.poll_context == context:
            if item != 0 or maximum != 0 or menu["wTopMenuItemY"] != 16:
                return None
            if context == "stats" and keys == 0x13 and menu["wTopMenuItemX"] == 1:
                return pulse("right", 12, 20, "stats_right")
            if context == "trade_choice" and keys == 0x23 and menu["wTopMenuItemX"] == 11:
                return pulse("a", 4, 20, "trade_a")
            return None
        if context == "confirmation" and self.poll_context == context:
            if maximum == 1 and keys == 3 and menu["wTradingWhichPlayerMon"] == self.slot:
                if item == 1:
                    return pulse("up", 2, 4)
                if item == 0:
                    return pulse("a", 4, 20, "confirm_a")
            return None
        if context == "dialogue":
            return pulse("a", 4, 20)
        if self.flags[3] and self.peer.peer_ready("trade_center_reached"):
            if context not in (None, "warp"):
                return None
            role = menu["hSerialConnectionStatus"]
            if role not in (1, 2):
                raise RuntimeError("unsupported serial role")
            return pulse("right" if role == 2 else "left", 8, 30)
        if context is None:
            if self.counts["SaveGameData"] or self.counts["Serial_SyncAndExchangeNybble"]:
                return None
            if self.approach_steps < 3:
                self.approach_steps += 1
                return pulse("up", 6, 20)
            return pulse("a", 4, 40)
        return None

    def after_step(self, *, call):
        self._owner()
        if self.error:
            raise RuntimeError(self.error)

    def close(self):
        """Remove owned observers on the owner thread before session close."""
        self._owner()
        errors = []
        for bank, address in reversed(tuple(self.hooks)):
            try:
                self.session._pyboy.hook_deregister(bank, address)
            except BaseException as exc:  # noqa: BLE001 - attempt every owned removal
                errors.append(exc)
            else:
                self.hooks.remove((bank, address))
        if errors:
            raise BaseExceptionGroup("trade observer cleanup failed", errors)

    def objective_complete(self):
        self._owner()
        return not self.error and bool(
            self.flags[4] if self.checkpoint == "select-mon" else self.flags[7]
        )

    def snapshot(self):
        self._owner()
        return deepcopy(
            {
                "role": self.side,
                "version": self.version,
                "outgoing_slot": self.slot,
                "checkpoint": self.checkpoint,
                "phase": self.phase,
                "readiness": list(self.flags),
                "checkpoint_reached": self.objective_complete(),
                "copy_complete": bool(self.flags[6]),
                "trade_endpoint_observed": bool(self.flags[7]),
                "before_party": self.before,
                "copied_party": self.copy,
                "final_party": self.final,
                "unsupported_reason": self.error,
                "hook_counts": dict(self.counts),
                "last_events": dict(self.last),
                "recent_transitions": list(self.events),
            }
        )


def create_owner_driver(*, session, side, options, peer_state):
    return TradeOwnerDriver(session=session, side=side, options=options, peer_state=peer_state)


def adjudicate_pair(owners, checkpoint):
    """Compare two local reports; phase flags are never reciprocal proof."""
    result = {"complete": False, "checkpoint_only": False, "errors": [], "status": "partial"}
    if checkpoint not in ("select-mon", "reciprocal-exchange"):
        result.update(status="unsupported", errors=["unknown checkpoint"])
        return result
    if len(owners) != 2:
        result["errors"] = ["exactly two owner snapshots required"]
        return result
    if {o.get("role") for o in owners} != {"listen", "connect"}:
        result.update(status="mismatch", errors=["distinct listen/connect reports required"])
        return result
    if any(o.get("checkpoint") != checkpoint for o in owners):
        result.update(
            status="mismatch", errors=["owner checkpoint contradicts requested checkpoint"]
        )
        return result
    if any(o.get("unsupported_reason") for o in owners):
        result.update(
            status="unsupported",
            errors=[o["unsupported_reason"] for o in owners if o.get("unsupported_reason")],
        )
        return result
    if checkpoint == "select-mon":
        try:
            for owner in owners:
                party = owner["before_party"]
                _validate_party(party)
                slot = owner["outgoing_slot"]
                if type(slot) is not int or not 0 <= slot < party["count"]:
                    raise ValueError("outgoing slot outside initial party")
                flags = owner["readiness"]
                if len(flags) != len(PHASES) or flags[0] != 1 or flags[4] != 1:
                    raise ValueError("qualified SelectMon readiness evidence missing")
                if owner.get("checkpoint_reached") is not True:
                    raise ValueError("local SelectMon checkpoint not reached")
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            result["errors"].append(f"invalid checkpoint evidence: {exc}")
            return result
        result["checkpoint_only"] = True
        return result
    try:
        for index, owner in enumerate(owners):
            other = owners[1 - index]
            before, copied, final = (
                owner.get(k) for k in ("before_party", "copied_party", "final_party")
            )
            if before is None or copied is None or final is None:
                result["errors"].append(f"owner {index}: missing party evidence")
                continue
            expected = _expected(
                before, owner["outgoing_slot"], other["before_party"], other["outgoing_slot"]
            )
            _validate_party(copied)
            _validate_party(final)
            if copied != expected:
                result["errors"].append(f"owner {index}: reciprocal pre-evolution copy mismatch")
                result["status"] = "mismatch"
            if final != copied:
                result["errors"].append(f"owner {index}: unsupported final transformation")
                if result["status"] != "mismatch":
                    result["status"] = "unsupported"
            if not all(
                owner.get(key) is True
                for key in ("checkpoint_reached", "copy_complete", "trade_endpoint_observed")
            ):
                result["errors"].append(f"owner {index}: local goal/copy/final endpoint missing")
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        result.update(status="mismatch")
        result["errors"].append(f"invalid evidence: {exc}")
    if not result["errors"]:
        result.update(complete=True, status="complete")
    return result
