"""LinkMenu observation and navigation helpers for the TCP trade peer.

Extracted from ``tests/_tcp_trade_peer.py`` (#127): the diagnostic symbol
inventory, the banked-CALL signature validators, the Cable Club confirmation and
connection-starter latch installers, the read-only field reader, and the bounded
:class:`_LinkMenuHistory` observer.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy

from tests._battle_turn_evidence import EVIDENCE_EVENTS

_TRADE_DIAG_SYMBOLS = tuple(
    dict.fromkeys(
        (
            "CableClubNPC",
            "YesNoChoice",
            "HandleMenuInput",
            "SaveGameData",
            "Serial_SyncAndExchangeNybble",
            "LinkMenu",
            "LinkMenu.waitForInputLoop",
            "LinkMenu.doneChoosingMenuSelection",
            "LinkMenu.choseCancel",
            "CloseLinkConnection",
            "PrepareForSpecialWarp",
            "SpecialEnterMap",
            "CableClubLeftGameboy",
            "CableClubRightGameboy",
            "CableClub_DoBattleOrTrade",
            "CableClub_DoBattleOrTradeAgain",
            "CableClub_DoBattleOrTradeAgain.finishedPatchingPlayerData",
            "CableClub_DoBattleOrTradeAgain.finishedPartyMonsPatchListPart",
            "CableClub_DoBattleOrTradeAgain.finishedEnemyMonsPatchListPart",
            "CableClub_DoBattleOrTradeAgain.trading",
            "ReturnToCableClubRoom",
            "CallCurrentTradeCenterFunction",
            "TradeCenter_SelectMon",
            "TradeCenter_SelectMon.playerMonMenu_HandleInput",
            "TradeCenter_SelectMon.chosePlayerMon",
            "TradeCenter_SelectMon.selectStatsMenuItem",
            "TradeCenter_SelectMon.selectTradeMenuItem",
            "TradeCenter_Trade",
            "_AddEnemyMonToPlayerParty",
            "DisplayLinkBattleVersusTextBox",
            "BattleTransition",
            "MainInBattleLoop",
            "MainInBattleLoop.selectEnemyMove",
            "DisplayBattleMenu",
            "DisplayBattleMenu.leftColumn_WaitForInput",
            "DisplayBattleMenu.rightColumn_WaitForInput",
            "MoveSelectionMenu",
            "MoveSelectionMenu.menuset",
            "LinkBattleExchangeData",
            "ExecutePlayerMove",
            "ExecuteEnemyMove",
            "PlayerCalcMoveDamage",
            "EndOfBattle",
            *(
                event
                for event in EVIDENCE_EVENTS
                if event not in {"post_exchange", "local_fully_paralyzed", "enemy_fully_paralyzed"}
            ),
        )
    )
)


_LINK_MENU_HISTORY_EVENTS = (
    "LinkMenu",
    "LinkMenu.waitForInputLoop",
    "LinkMenu.doneChoosingMenuSelection",
    "LinkMenu.choseCancel",
    "CloseLinkConnection",
    "PrepareForSpecialWarp",
    "SpecialEnterMap",
    "LinkMenu.afterExchange",
)


def _link_menu_post_call_address(session):
    """Validate a direct CALL in banked ROM before observing its return site."""
    bank, addr = session.symbols.bank_addr("LinkMenu.exchangeMenuSelectionLoop")
    target_bank, target = session.symbols.bank_addr("Serial_ExchangeLinkMenuSelection")
    if not ((bank == 0 and 0 <= addr <= 0x3FFC) or (bank > 0 and 0x4000 <= addr <= 0x7FFC)):
        raise ValueError("CALL site is outside its ROM bank")
    compatible = (target_bank == 0 and 0 <= target < 0x4000) or (
        target_bank == bank and bank > 0 and 0x4000 <= target < 0x8000
    )
    memory = session._pyboy.memory
    opcode, lo, hi = (int(memory[bank, addr + offset]) for offset in range(3))
    if opcode != 0xCD or (lo | hi << 8) != target or not compatible:
        raise ValueError("CALL opcode, target, or target bank mismatch")
    return bank, addr + 3


def _link_menu_receive_candidate(values):
    """Select the first valid tag, not the first decisive vote (ROM order)."""
    for index, value in enumerate(values[:2]):
        if (value & 0xF0) == 0xD0:
            return {"index": index, "value": value}
    return None


def _link_menu_has_real_selection_exchange(history):
    """Return whether this ROM has post-call, directional vote evidence.

    ``LinkMenu`` entry alone is not permission to drive the next game phase:
    Cable Club peers may enter its polling loop at different host times and
    may negotiate which Game Boy supplies clocks.  The post-call hook is the
    only observation here that proves the ROM actually returned from its
    native selection exchange.  A real exchange can be asymmetric at either
    endpoint: one ROM can retain its sent vote while the other retains the
    received vote.  Require one locally observed, non-idle direction here;
    the existing peer sync below requires that evidence from both ROMs before
    gameplay advances.  All data remains an observation, never a menu write.
    """
    return bool({"sent", "received"} & history.first_decisive.keys())


def _is_cable_club_save_choice_ready(confirmation, counters, menu):
    """Return whether Cable Club is awaiting its native save confirmation.

    The latch is armed only at the verified ``CableClubNPC`` CALL site for
    ``YesNoChoice`` and disarmed at that call's return address.  The generic
    menu fields consequently qualify input only while that specific cartridge
    call is active; persisted overworld menus cannot authorize a save input.
    """
    return bool(
        confirmation["call_entries"] > confirmation["return_entries"]
        and counters["SaveGameData"][0] == 0
        and counters["Serial_SyncAndExchangeNybble"][0] == 0
        and counters["LinkMenu"][0] == 0
        and counters["CloseLinkConnection"][0] == 0
        and menu.get("wCurrentMenuItem") == 0
        and menu.get("wMaxMenuItem") == 1
        and menu.get("wMenuWatchedKeys", 0) & 0x01 == 0x01
    )


def _install_cable_club_confirmation_latch(session):
    """Observe the one CableClubNPC CALL YesNoChoice site in a loaded ROM.

    The hook is an owner-thread observation only.  Locating it from the
    loaded symbols and requiring the unique direct target within the
    CableClubNPC body prevents a generic YesNoChoice call elsewhere in the
    ROM from being treated as the Cable Club confirmation boundary.
    """
    npc_bank, npc = session.symbols.bank_addr("CableClubNPC")
    yes_no_bank, yes_no = session.symbols.bank_addr("YesNoChoice")
    if yes_no_bank != 0 or npc_bank <= 0:
        raise ValueError("Cable Club YesNoChoice call has incompatible banks")
    memory = session._pyboy.memory
    target = (0xCD, yes_no & 0xFF, yes_no >> 8)
    candidates = []
    for address in range(npc, min(npc + 0x100, 0x8000 - 6)):
        call = tuple(int(memory[npc_bank, address + offset]) for offset in range(3))
        if call == target:
            candidates.append(address)
    if len(candidates) != 1:
        raise ValueError(
            f"expected exactly one Cable Club YesNoChoice call site, found {len(candidates)}"
        )
    state = {"call_entries": 0, "return_entries": 0}
    call_site = candidates[0]

    def entered(_ctx):
        state["call_entries"] += 1

    def returned(_ctx):
        state["return_entries"] += 1

    session._pyboy.hook_register(npc_bank, call_site, entered, None)
    session._pyboy.hook_register(npc_bank, call_site + 3, returned, None)
    return state


def _install_connection_starter_latch(session, backend, *, enabled):
    """Publish just before the starter ROM enables its first native clock.

    Cable Club first tries an external-clock byte, then enables an internal
    clock byte in the same native loop.  If both independently driven ROMs
    reach that loop on the same frame, they can both complete the external
    attempt and elect the external role.  The TCP listener/connector choice
    gives us a deterministic *input* starter: let that ROM reach its own
    internal-clock instruction, then release the peer's ordinary dialogue
    input.  The callback observes code execution and sends a control-plane
    marker only; it does not write FF01, FF02, HRAM, or game RAM.
    """
    state = {"enabled": bool(enabled), "announced": False, "address": None}
    if not enabled:
        return state
    npc_bank, npc = session.symbols.bank_addr("CableClubNPC")
    if npc_bank <= 0:
        raise ValueError("CableClubNPC must be in a switchable bank")
    memory = session._pyboy.memory
    # ld a,$01 ; ldh [rSB],a ; ld a,$81 ; ldh [rSC],a
    target = (0x3E, 0x01, 0xE0, 0x01, 0x3E, 0x81, 0xE0, 0x02)
    candidates = []
    for address in range(npc, min(npc + 0x100, 0x8000 - len(target))):
        actual = tuple(int(memory[npc_bank, address + offset]) for offset in range(len(target)))
        if actual == target:
            candidates.append(address + 6)
    if len(candidates) != 1:
        raise ValueError(
            f"expected exactly one Cable Club native internal-clock store, found {len(candidates)}"
        )
    state["address"] = candidates[0]

    def entered(_ctx):
        if not state["announced"]:
            backend.announce_sync(sync_id=119)
            state["announced"] = True

    session._pyboy.hook_register(npc_bank, candidates[0], entered, None)
    return state


def _read_link_menu_fields(session, *, include_map=False, on_error=None):
    """Read selection state without writes; optionally report bounded errors."""
    snapshot = {}
    fields = (
        ("wCurrentMenuItem", 1),
        ("wMaxMenuItem", 1),
        ("wCableClubDestinationMap", 1),
        ("wLinkState", 1),
        ("hSerialConnectionStatus", 1),
        ("wLinkMenuSelectionSendBuffer", 2),
        ("wLinkMenuSelectionReceiveBuffer", 2),
    )
    if include_map:
        fields += (("wCurMap", 1),)
    for symbol, size in fields:
        try:
            addr = session.symbols.addr_of(symbol)
            values = [int(session._pyboy.memory[addr + i]) for i in range(size)]
            snapshot[symbol] = values[0] if size == 1 else values
        except BaseException as exc:  # noqa: BLE001
            if on_error is not None:
                on_error(symbol, exc)
    return snapshot


class _LinkMenuHistory:
    """Bounded, read-only observations; first milestones survive buffer reuse.

    ``install`` replaces the caller's milestone installation loop so counters
    and observations share a callback. Repeated installation is a no-op.
    ``first`` retains one sample per event, independently of ``recent``.
    """

    def __init__(self, session, *, role, version, limit=8):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        self.session = session
        self.role = role
        self.version = version
        self.limit = limit
        self.total = 0
        self.counts = dict.fromkeys(_LINK_MENU_HISTORY_EVENTS, 0)
        self.first = {}
        self.first_decisive = {}
        self.recent = deque(maxlen=limit)
        self.hooks = {}
        self.error_count = 0
        self.errors = deque(maxlen=limit)
        self._installed = False

    def _error(self, event, stage, exc):
        self.error_count += 1
        try:
            message = f"{type(exc).__name__}: {exc}"[:256]
        except BaseException:  # noqa: BLE001
            message = type(exc).__name__[:256]
        self.errors.append({"event": event, "stage": stage, "error": message})

    def _observe(self, event):
        self.total += 1
        self.counts[event] += 1
        sample = {
            "event": event,
            "role": self.role,
            "version": self.version,
            "tick": None,
            "seq": self.total,
        }
        try:
            sample["tick"] = int(self.session.current_tick())
        except BaseException as exc:  # noqa: BLE001
            self._error(event, "tick", exc)
        sample.update(
            _read_link_menu_fields(
                self.session,
                include_map=True,
                on_error=lambda symbol, exc: self._error(event, symbol, exc),
            )
        )
        # This predicts the next selection read from observed buffers. Even
        # afterExchange precedes the ROM branch; it is not acceptance proof.
        sample["recv_candidate"] = _link_menu_receive_candidate(
            sample.get("wLinkMenuSelectionReceiveBuffer", [])
        )
        self.first.setdefault(event, sample)
        # Idle exchanges can precede the first A/B vote by many frames.
        # Retain that vote separately even after the recent ring rolls over.
        # Receive selection prefers the first D0-tagged byte, falling back
        # to byte one when byte zero is invalid. A valid idle first byte
        # must not be displaced by a decisive second byte.
        for symbol, direction in (
            ("wLinkMenuSelectionSendBuffer", "sent"),
            ("wLinkMenuSelectionReceiveBuffer", "received"),
        ):
            values = sample.get(symbol, [])
            candidate = (
                sample["recv_candidate"]
                if direction == "received"
                else _link_menu_receive_candidate(values[:1])
            )
            if (
                event == "LinkMenu.afterExchange"
                and candidate is not None
                and (candidate["value"] & 0x0C) != 0
            ):
                self.first_decisive.setdefault(direction, sample)
        self.recent.append(sample)

    def install(self, buckets, observers_by_address=None, battle_observer=None):
        if self._installed:
            return
        self._installed = True
        observers_by_address = observers_by_address or {}
        # Resolve every address before instrumentation changes any ROM opcode.
        addresses = {}
        for event in dict.fromkeys((*_TRADE_DIAG_SYMBOLS, *_LINK_MENU_HISTORY_EVENTS)):
            if event not in buckets and event not in self.counts:
                continue
            try:
                addresses[event] = (
                    _link_menu_post_call_address(self.session)
                    if event == "LinkMenu.afterExchange"
                    else self.session.symbols.bank_addr(event)
                )
                self.hooks[event] = {"available": False}
            except BaseException as exc:  # noqa: BLE001
                self._error(event, "resolve", exc)
                self.hooks[event] = {"available": False, "reason": self.errors[-1]["error"]}
        grouped = {}
        for event, address in addresses.items():
            grouped.setdefault(address, []).append(event)
        for (bank, addr), events in grouped.items():
            # Only the already-owned serial entry supports optional fanout.
            observer = (
                observers_by_address.get((bank, addr))
                if "Serial_SyncAndExchangeNybble" in events
                else None
            )

            def callback(_ctx, events=tuple(events), observer=observer):
                for event in events:
                    try:
                        if event in buckets:
                            buckets[event][0] += 1
                    except BaseException as exc:  # noqa: BLE001
                        self._error(event, "counter", exc)
                    try:
                        if event in self.counts:
                            self._observe(event)
                    except BaseException as exc:  # noqa: BLE001
                        self._error(event, "callback", exc)
                    if battle_observer is not None and event in EVIDENCE_EVENTS:
                        battle_observer.observe(event)
                if observer is not None and observer.available:
                    try:
                        observer._observe("Serial_SyncAndExchangeNybble")
                    except BaseException as exc:  # noqa: BLE001
                        observer._error("Serial_SyncAndExchangeNybble", "callback", exc)

            try:
                self.session._pyboy.hook_register(bank, addr, callback, None)
                for event in events:
                    self.hooks[event] = {"available": True, "bank": bank, "address": addr}
                if observer is not None:
                    observer.external_registered()
            except BaseException as exc:  # noqa: BLE001
                for event in events:
                    self._error(event, "register", exc)
                    self.hooks[event] = {"available": False, "reason": self.errors[-1]["error"]}
                if observer is not None:
                    observer.external_failed(exc)
        # A failed symbol resolution never reached grouped registration.
        registered_addresses = set(grouped)
        for address, observer in observers_by_address.items():
            if address not in registered_addresses:
                observer.external_failed(ValueError("serial entry did not resolve"))

    def snapshot(self):
        return deepcopy(
            {
                "role": self.role,
                "version": self.version,
                "limit": self.limit,
                "total": self.total,
                "counts": self.counts,
                "first": self.first,
                "first_decisive": self.first_decisive,
                "recent": list(self.recent),
                "recent_dropped": max(0, self.total - len(self.recent)),
                "recent_truncated": self.total > len(self.recent),
                "hooks": self.hooks,
                "error_count": self.error_count,
                "errors": list(self.errors),
                "errors_dropped": self.error_count - len(self.errors),
                "errors_truncated": self.error_count > len(self.errors),
            }
        )
