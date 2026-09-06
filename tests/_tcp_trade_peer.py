"""Standalone TCP-trade peer — spawned as a subprocess by
``test_pyboy_link_session_subprocess.py``.

Runs one side of a two-PyBoy Pokémon trade. Takes the role
(``listen`` or ``connect``), a TCP port, and a deadline on the
command line. Connects to or listens for a peer, loads the Yellow
Cable Club state, drives through the trade flow, then prints a
JSON blob to stdout reporting the final hook counters and exit
cleanly.

The parent process uses this script to get **subprocess-level
scheduling isolation** — each PyBoy instance runs in its own
Python interpreter with its own GIL, so OS scheduling gives both
sides real parallelism (unlike two threads in one Python process).
"""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from threading import get_ident
from typing import NamedTuple

# Opt-in diagnostic contract only; hook installation is a separate increment.
_PRE_LINK_MENU_SCHEMA_VERSION = 1
_PRE_LINK_MENU_HISTORY_LIMIT = 32
_PRE_LINK_MENU_ERROR_LIMIT = 8
_PRE_LINK_MENU_FIELDS = (
    ("hSerialConnectionStatus", 1), ("hSerialSendData", 1),
    ("hSerialReceiveData", 1), ("hSerialReceivedNewData", 1),
    ("wSerialExchangeNybbleSendData", 1), ("wSerialExchangeNybbleTempReceiveData", 1),
    ("wSerialExchangeNybbleReceiveData", 1), ("wSerialSyncAndExchangeNybbleReceiveData", 1),
    ("wUnknownSerialCounter", 2), ("wUnknownSerialCounter2", 2),
    ("wLinkTimeoutCounter", 1),
)
_PRE_LINK_MENU_REPORT_FIELDS = (
    "schema_version", "enabled", "available", "reason", "role", "version",
    "pins", "observed_pins", "sites", "limit", "total", "counts", "first",
    "recent", "recent_dropped", "recent_truncated", "hooks", "cleanup_pending",
    "error_limit", "error_count", "errors", "errors_dropped", "errors_truncated",
)

# Local pinned ROM/SYM bytes, interpreted against pret's
# engine/link/cable_club_npc.asm and home/serial.asm. CALL return sites are
# instruction boundaries, not proof of the subsequent conditional outcome.
_PRE_LINK_MENU_PROFILES = {
    ("listen", "blue_color"): (
        "5f4b05725a860e04077045462176d3e2771c5022",
        "c779a0628cfc97cc9ac9db2520a1e23a2d8b7ed6",
        0x71C5, 0x7263, 0x227F, 0x223F, 0x72A8, 1, 0x5C0A, 0x72D7,
    ),
    ("connect", "yellow"): (
        "cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1",
        "7c4205723943e7722230dcf014e5e8a2012474aa",
        0x7035, 0x70D8, 0x20DB, 0x209B, 0x711D, 0x3D, 0x580C, 0x71AC,
    ),
}


def _resolve_pre_link_menu(*, enabled, role, version, rom_bytes, symbol_bytes):
    """Resolve immutable input bytes only; never access an emulator or install hooks.

    Even fully verified sites remain unavailable until a later installation
    stage succeeds. Unsupported/disabled configurations do not inspect assets.
    """
    history = _PreLinkMenuHistory(enabled=enabled, role=role, version=version)
    if not enabled:
        return history
    profile = _PRE_LINK_MENU_PROFILES.get((role, version))
    if profile is None:
        history.reason = "unsupported_role_version"
        return history
    rom_pin, sym_pin, npc, call, sync, timeout, connected, menu_bank, menu, close = profile
    pins = (("rom_sha1", rom_pin), ("symbol_sha1", sym_pin))
    if type(rom_bytes) is not bytes or type(symbol_bytes) is not bytes:
        history = _PreLinkMenuHistory(enabled=True, role=role, version=version, pins=pins)
        history.reason = "invalid_asset_bytes"
        return history
    observed = (("rom_sha1", hashlib.sha1(rom_bytes).hexdigest()),
                ("symbol_sha1", hashlib.sha1(symbol_bytes).hexdigest()))
    history = _PreLinkMenuHistory(
        enabled=True, role=role, version=version, pins=pins, observed_pins=observed,
    )
    if history.reason.startswith("pin_mismatch:"):
        return history
    symbols = {}
    try:
        for line in symbol_bytes.decode("ascii").splitlines():
            # Match symbols.loader's address-line grammar; RGBDS also emits
            # valid non-address constants, which are deliberately ignored.
            match = re.match(
                r"^\s*([0-9A-Fa-f]{1,4}):([0-9A-Fa-f]{4})\s+(\S+)\s*(?:;.*)?$",
                line,
            )
            if match is None:
                continue
            bank_text, address_text, name = match.groups()
            if name in symbols:
                history.reason = "duplicate_symbol:" + name
                return history
            symbols[name] = (int(bank_text, 16), int(address_text, 16))
    except (UnicodeError, ValueError, IndexError):
        history.reason = "invalid_symbol_format"
        return history
    anchors = (
        ("CableClubNPC", (1, npc)),
        ("Serial_SyncAndExchangeNybble", (0, sync)),
        ("SetUnknownCounterToFFFF", (0, timeout)),
        ("CableClubNPC.connected", (1, connected)),
        ("LinkMenu", (menu_bank, menu)),
        ("CloseLinkConnection", (1, close)),
        ("CableClubNPC.choseNo", (1, call + 44)),
    )
    for name, address in anchors:
        if name not in symbols:
            history.reason = "missing_symbol:" + name
            return history
        if symbols[name] != address:
            history.reason = "symbol_address_mismatch:" + name
            return history
    # The caller's full CALL + counter read + both JR NZ instructions. Their
    # relative targets must independently land at the pinned connected label.
    caller_signature = bytes((0xCD, sync & 0xFF, sync >> 8)) + bytes.fromhex(
        "2147cc2a3c203b7e3c2037060a"
    )
    specs = (
        ("CableClubNPC.beforeSync", 1, call, caller_signature),
        ("CableClubNPC.afterSync", 1, call + 3, bytes.fromhex("2147cc")),
        ("CableClubNPC.counterBranch1", 1, call + 8, bytes.fromhex("203b")),
        ("CableClubNPC.counterBranch2", 1, call + 12, bytes.fromhex("2037")),
        ("CableClubNPC.counterExpired", 1, call + 14, bytes.fromhex("060a")),
        ("CableClubNPC.connected", 1, connected, bytes.fromhex("af3277")),
        ("Serial_SyncAndExchangeNybble", 0, sync, bytes.fromhex("3effea3ecc")),
        ("Serial_SyncAndExchangeNybble.timeoutJump", 0, sync + 0x1C,
         bytes((0xAF, 0xC3, timeout & 0xFF, timeout >> 8))),
        ("Serial_SyncAndExchangeNybble.return", 0, sync + 0x43, b"\xc9"),
        ("Serial_SyncAndExchangeNybble.publish60", 0, sync + 0x4A, bytes.fromhex("c660")),
        ("Serial_SyncAndExchangeNybble.receiveCompare", 0, sync + 0x5F,
         bytes.fromhex("fe60c0")),
        ("CableClubNPC.inactivityCloseCall", 1, call + 25,
         bytes((0xCD, close & 0xFF, close >> 8))),
        ("CableClubNPC.inactivityCloseReturn", 1, call + 28,
         bytes((0x21, (close - 15) & 0xFF, (close - 15) >> 8))),
        ("CableClubNPC.choseNoCloseCall", 1, call + 44,
         bytes((0xCD, close & 0xFF, close >> 8))),
        ("CableClubNPC.choseNoCloseReturn", 1, call + 47,
         bytes((0x21, (close - 10) & 0xFF, (close - 10) >> 8))),
        ("SetUnknownCounterToFFFF", 0, timeout, bytes.fromhex("3dea47ccea48ccc9")),
        ("LinkMenu", menu_bank, menu,
         bytes.fromhex("afea58" if version == "blue_color" else "afea57")),
    )
    for event, bank, address, signature in specs:
        valid = (bank == 0 and 0 <= address < 0x4000) or (
            bank > 0 and 0x4000 <= address < 0x8000
        )
        bank_end = 0x4000 if bank == 0 else 0x8000
        if not valid or address + len(signature) > bank_end:
            history.reason = "invalid_site:" + event
            return history
        offset = address if bank == 0 else bank * 0x4000 + address - 0x4000
        if rom_bytes[offset:offset + len(signature)] != signature:
            history.reason = "signature_mismatch:" + event
            return history
    if any(call + offset + 2 + displacement != connected
           for offset, displacement in ((8, 0x3B), (12, 0x37))):
        history.reason = "branch_target_mismatch"
        return history
    return _PreLinkMenuHistory(
        enabled=True, role=role, version=version, pins=pins, observed_pins=observed,
        sites=tuple((event, bank, address) for event, bank, address, _ in specs),
    )


class _PreLinkMenuConfig(NamedTuple):
    enabled: bool
    role: str
    version: str
    pins: tuple[tuple[str, str], ...]
    observed_pins: tuple[tuple[str, str], ...]
    sites: tuple[tuple[str, int, int], ...]
    limit: int


class _PreLinkMenuHistory:
    """Owner-thread observations with bounded retention and owned-hook cleanup."""

    def __init__(self, *, enabled, role, version, pins=(), observed_pins=(),
                 sites=(), limit=_PRE_LINK_MENU_HISTORY_LIMIT):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a bool")
        if type(limit) is not int or limit != _PRE_LINK_MENU_HISTORY_LIMIT:
            raise ValueError("limit must be exactly 32")
        if type(role) is not str or type(version) is not str:
            raise ValueError("role and version must be strings")

        def freeze_pins(values):
            rows = tuple(values)
            if any(type(row) not in (tuple, list) for row in rows):
                raise ValueError("pin rows must be tuples or lists")
            rows = tuple(tuple(row) for row in rows)
            if any(len(row) != 2 or any(type(v) is not str for v in row) for row in rows):
                raise ValueError("pins must contain name/digest string pairs")
            if len({row[0] for row in rows}) != len(rows):
                raise ValueError("duplicate pin name")
            return tuple(sorted(rows))

        site_rows = tuple(sites)
        if any(type(row) not in (tuple, list) for row in site_rows):
            raise ValueError("site rows must be tuples or lists")
        frozen_sites = tuple(tuple(row) for row in site_rows)
        if any(
            len(row) != 3 or type(row[0]) is not str
            or type(row[1]) is not int or type(row[2]) is not int
            or row[1] < 0 or not 0 <= row[2] <= 0xFFFF
            for row in frozen_sites
        ):
            raise ValueError("sites must contain event/bank/address triples")
        if len({row[0] for row in frozen_sites}) != len(frozen_sites):
            raise ValueError("duplicate site event")
        self.config = _PreLinkMenuConfig(
            enabled, role, version, freeze_pins(pins), freeze_pins(observed_pins),
            frozen_sites, limit,
        )
        self.available = False
        expected = dict(self.config.pins)
        observed = dict(self.config.observed_pins)
        mismatch = next((name for name in sorted(expected.keys() | observed.keys())
                         if expected.get(name) != observed.get(name)), None)
        self.reason = (
            "disabled" if not enabled else
            f"pin_mismatch:{mismatch}" if mismatch is not None else
            "pins_unverified" if not expected else "hooks_not_installed"
        )
        self.total = 0
        self.counts = dict.fromkeys((row[0] for row in frozen_sites), 0)
        self.first = {}
        self.recent = deque(maxlen=limit)
        self.hooks = {}
        self.cleanup_pending = []
        self.error_count = 0
        self.errors = deque(maxlen=_PRE_LINK_MENU_ERROR_LIMIT)
        self._session = None
        self._owner = None
        self._owned = []
        self._field_addresses = {}
        self._attempted = False

    def _error(self, event, stage, exc):
        self.error_count += 1
        self.errors.append({"event": event, "stage": stage,
                            "error": type(exc).__name__[:128]})

    def _read(self, event, name, getter, maximum):
        try:
            value = getter()
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError("invalid observed integer")
            return value
        except BaseException as exc:  # noqa: BLE001
            self._error(event, name, exc)
            return None

    def _observe(self, event):
        if get_ident() != self._owner:
            self._error(event, "owner_thread", RuntimeError())
            return
        self.total += 1
        self.counts[event] += 1
        sample = {"event": event, "sequence": self.total, "frame": None,
                  "cpu_cycles": None, "registers": {}, "fields": {}}
        sample["frame"] = self._read(event, "frame", self._session.current_tick, (1 << 63) - 1)
        sample["cpu_cycles"] = self._read(
            event, "cpu_cycles", lambda: self._session._pyboy.mb.cpu.cycles, (1 << 63) - 1,
        )
        for name in ("PC", "SP", "A", "F"):
            sample["registers"][name] = self._read(
                event, name, lambda name=name: getattr(self._session._pyboy.mb.cpu, name),
                0xFFFF if name in ("PC", "SP") else 0xFF,
            )
        for name, length in _PRE_LINK_MENU_FIELDS:
            address = self._field_addresses[name]
            values = [self._read(
                event, f"{name}[{index}]",
                lambda address=address, index=index: self._session._pyboy.memory[address + index],
                0xFF,
            ) for index in range(length)]
            sample["fields"][name] = values[0] if length == 1 else values
        self.first.setdefault(event, sample)
        self.recent.append(sample)

    def install(self, session):
        if self._attempted or not self.config.enabled or self.reason != "hooks_not_installed":
            return
        self._owner = get_ident()
        self._session = session
        self._attempted = True
        sites = tuple(row for row in self.config.sites
                      if row[0] not in ("LinkMenu", "Serial_SyncAndExchangeNybble"))
        self.hooks = {event: {"bank": bank, "address": address, "status": "pending"}
                      for event, bank, address in sites}
        self.hooks["LinkMenu"] = {"status": "external_owner"}
        self.hooks["Serial_SyncAndExchangeNybble"] = {"status": "external_pending"}
        try:
            if len(sites) != 15 or len({(b, a) for _, b, a in sites}) != 15:
                raise ValueError("expected fifteen distinct owned sites")
            for name, length in _PRE_LINK_MENU_FIELDS:
                bank, address = session.symbols.bank_addr(name)
                if type(bank) is not int or bank != 0 or type(address) is not int:
                    raise ValueError("invalid field address")
                if not (0xC000 <= address and address + length <= 0xE000
                        or 0xFF80 <= address and address + length <= 0xFFFF):
                    raise ValueError("field is outside WRAM/HRAM")
                self._field_addresses[name] = address
        except BaseException as exc:  # noqa: BLE001
            self.reason = "site_or_field_resolution_error:" + type(exc).__name__[:128]
            self._error("install", "resolve", exc)
            return
        for event, bank, address in sites:
            def callback(_context, event=event):
                try:
                    self._observe(event)
                except BaseException as exc:  # noqa: BLE001
                    self._error(event, "callback", exc)
            try:
                session._pyboy.hook_register(bank, address, callback, None)
            except BaseException as exc:  # noqa: BLE001
                self.hooks[event]["status"] = "registration_failed"
                self._error(event, "register", exc)
                self.reason = "hook_registration_error:" + event
                self._cleanup()
                return
            self._owned.append((event, bank, address))
            self.hooks[event]["status"] = "registered"
        self.reason = "external_pending"

    def external_registered(self):
        if get_ident() != self._owner or self.reason != "external_pending":
            return
        self.hooks["Serial_SyncAndExchangeNybble"]["status"] = "external_registered"
        self.available = len(self._owned) == 15
        self.reason = None if self.available else "owned_hooks_incomplete"

    def external_failed(self, exc):
        if get_ident() != self._owner or self.reason != "external_pending":
            return
        self.hooks["Serial_SyncAndExchangeNybble"]["status"] = "external_failed"
        self._error("Serial_SyncAndExchangeNybble", "external_register", exc)
        self.reason = "external_registration_failed"
        self._cleanup()

    def _cleanup(self):
        self.available = False
        if self._session is None or not self._owned:
            self.cleanup_pending = [event for event, _, _ in self._owned]
            return
        pending = []
        for event, bank, address in reversed(self._owned):
            try:
                self._session._pyboy.hook_deregister(bank, address)
            except BaseException as exc:  # noqa: BLE001
                self.hooks[event]["status"] = "cleanup_pending"
                self._error(event, "deregister", exc)
                pending.append((event, bank, address))
            else:
                self.hooks[event]["status"] = "removed"
        self._owned = list(reversed(pending))
        self.cleanup_pending = [event for event, _, _ in self._owned]

    def close(self):
        self.available = False
        if not self._attempted:
            # Keep disabled/asset-validation reasons in the emitted result.
            return
        if self._owner is not None and get_ident() != self._owner:
            self._error("close", "owner_thread", RuntimeError())
            self.cleanup_pending = [event for event, _, _ in self._owned]
            self.reason = "cleanup_wrong_thread"
            return
        self._cleanup()
        self.reason = "cleanup_pending" if self.cleanup_pending else "closed"

    def snapshot(self):
        return deepcopy({
            "schema_version": _PRE_LINK_MENU_SCHEMA_VERSION,
            "enabled": self.config.enabled,
            "available": self.available,
            "reason": self.reason,
            "role": self.config.role,
            "version": self.config.version,
            "pins": dict(self.config.pins),
            "observed_pins": dict(self.config.observed_pins),
            "sites": {event: {"bank": bank, "address": address}
                      for event, bank, address in self.config.sites},
            "limit": self.config.limit,
            "total": self.total,
            "counts": self.counts,
            "first": self.first,
            "recent": list(self.recent),
            "recent_dropped": max(0, self.total - len(self.recent)),
            "recent_truncated": self.total > len(self.recent),
            "hooks": self.hooks,
            "cleanup_pending": self.cleanup_pending,
            "error_limit": _PRE_LINK_MENU_ERROR_LIMIT,
            "error_count": self.error_count,
            "errors": list(self.errors),
            "errors_dropped": max(0, self.error_count - len(self.errors)),
            "errors_truncated": self.error_count > len(self.errors),
        })

_TRADE_DIAG_SYMBOLS = (
    "CableClubNPC",
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
)


PARTY_MON_SIZE = 44
PARTY_OT_SIZE = 11
PARTY_NICK_SIZE = 11


def _deadline_remaining(deadline: float, *, phase: str) -> float:
    """Return setup time remaining, failing closed at the absolute cutoff."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{phase} exceeded the process deadline")
    return remaining


def _hold_at_sync_boundary(
    backend: object,
    *,
    ready_sync_id: int,
    release_sync_id: int,
    timeout: float,
    service_pending_edges: Callable[[], int],
    progress_callback: Callable[[], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Rendezvous at a ROM boundary until both peers have observed release.

    Stable UI milestones can drain already-admitted owner-dispatch edges
    without ticking. Timed ROM phases must provide ``progress_callback`` so
    the owner continues authentic emulation while the peer catches up.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("timeout must be a positive number")
    if timeout <= 0:
        raise ValueError("timeout must be a positive number")

    deadline = monotonic() + timeout

    def wait_for_peer(marker: int, *, phase: str) -> None:
        while True:
            if backend.poll_peer_sync(sync_id=marker):
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RuntimeError(f"sync boundary {phase} did not converge: marker={marker}")
            if progress_callback is None:
                service_pending_edges()
            else:
                progress_callback()
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(0.001, remaining))

    backend.announce_sync(sync_id=ready_sync_id)
    wait_for_peer(ready_sync_id, phase="ready")
    backend.announce_sync(sync_id=release_sync_id)
    wait_for_peer(release_sync_id, phase="release")


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
    """Return whether the ROM has exchanged a non-idle LinkMenu vote.

    ``LinkMenu`` entry alone is not permission to drive the next game phase:
    Cable Club peers may enter its polling loop at different host times and
    may negotiate which Game Boy supplies clocks.  The post-call hook is the
    only observation here that proves the ROM actually returned from its
    native selection exchange.  Require both the locally sent vote and the
    received peer vote; all data remains an observation, never a menu write.
    """
    decisive = history.first_decisive
    return "sent" in decisive and "received" in decisive


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

    def install(self, buckets, observers_by_address=None):
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
            observer = (observers_by_address.get((bank, addr))
                        if "Serial_SyncAndExchangeNybble" in events else None)

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


def _peer_shutdown_sync(
    backend,
    *,
    cooperative_sync,
    step,
    backend_snapshot,
    ready_sync_id: int,
    release_sync_id: int,
    timeout: float = 120.0,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> None:
    """Close a successful pair only after both peers have gone quiet.

    Reaching the same game milestone is not sufficient for teardown:
    either ROM may still have an armed native serial transfer.  The first
    marker lets both owners finish their local post-milestone drain; the
    second marker is an acknowledgement that the resulting wire-idle
    window was observed by both peers.  The ready barrier continues
    stepping the owner emulator; the final acknowledgements service only
    already-admitted edges so a faster peer cannot reopen a transfer while
    the other side is finishing its drain.
    """

    def wait_for_peer_marker(sync_id: int) -> None:
        deadline_at = monotonic() + timeout
        while monotonic() < deadline_at:
            if backend.poll_peer_sync(sync_id=sync_id):
                return
            # After a local wire-idle observation no new master edge can
            # be created without ticking the emulator.  Service only
            # already-admitted slave work while waiting for the peer's
            # marker; this keeps the final handshake symmetric without
            # reopening a native transfer on the faster side.
            backend.service_pending_edges(max_edges=1)
            sleep(0.001)
        raise RuntimeError(
            f"peer shutdown sync {sync_id} did not converge: backend={backend_snapshot()}"
        )

    cooperative_sync(sync_id=ready_sync_id, timeout=timeout, step_frames=1)
    backend.wait_for_wire_idle(
        timeout=timeout,
        progress_callback=lambda: step(1),
        stable_checks=4,
    )
    backend.announce_sync(sync_id=release_sync_id)
    wait_for_peer_marker(release_sync_id)
    backend.wait_for_wire_idle(
        timeout=timeout,
        progress_callback=lambda: backend.service_pending_edges(max_edges=1),
        stable_checks=4,
    )
    # The release marker can be consumed while the peer is still
    # finishing its own idle wait.  A final passive acknowledgement makes
    # both sides observe that second drain before either detaches.
    final_sync_id = release_sync_id + 1
    backend.announce_sync(sync_id=final_sync_id)
    wait_for_peer_marker(final_sync_id)
    backend.wait_for_wire_idle(
        timeout=timeout,
        progress_callback=lambda: backend.service_pending_edges(max_edges=1),
        stable_checks=4,
    )
    # Do not detach as soon as the peer sees the final marker: the peer
    # may still be returning from its own final idle drain.  Advertise a
    # completion marker only after that drain and wait passively for the
    # matching completion marker.  No emulator tick occurs in this last
    # exchange, so it cannot create a new master transfer between the
    # marker and teardown.
    done_sync_id = final_sync_id + 1
    backend.announce_sync(sync_id=done_sync_id)
    wait_for_peer_marker(done_sync_id)


def _finish_link_menu_phase(goal, *, cooperative_sync, peer_shutdown_sync):
    """Use passive shutdown only when no further gameplay is requested."""
    if goal == "link_menu":
        peer_shutdown_sync(ready_sync_id=123, release_sync_id=124, timeout=10.0)
    else:
        cooperative_sync(sync_id=123, timeout=10.0, step_frames=1)


def _party_summary(session) -> dict[str, object]:
    """Return the game-owned party arrays for post-trade verification."""
    memory = session._pyboy.memory
    addr_of = session.symbols.addr_of
    count = int(memory[addr_of("wPartyCount")])
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")
    return {
        "count": count,
        "species": [int(memory[species_addr + i]) for i in range(count + 1)],
        "mon_species": [int(memory[mons_addr + i * PARTY_MON_SIZE]) for i in range(count)],
        "mon_records": [
            bytes(
                memory[mons_addr + i * PARTY_MON_SIZE + offset] for offset in range(PARTY_MON_SIZE)
            ).hex()
            for i in range(count)
        ],
    }


def _validate_battle_party_fixture(session) -> str:
    """Validate that the saved battle fixture is already Colosseum-legal."""
    pb = session._pyboy
    addr_of = session.symbols.addr_of
    count_addr = addr_of("wPartyCount")
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")

    count = pb.memory[count_addr]
    if count < 3:
        raise RuntimeError(f"battle fixture party count is {count}, expected >=3")
    if pb.memory[species_addr + count] != 0xFF:
        raise RuntimeError("battle fixture party species list is not FF-terminated")
    species = []
    for slot in range(count):
        slot_species = pb.memory[species_addr + slot]
        mon_addr = mons_addr + slot * PARTY_MON_SIZE
        mon_species = pb.memory[mon_addr]
        hp = (pb.memory[mon_addr + 1] << 8) | pb.memory[mon_addr + 2]
        if slot_species in (0, 0xFF) or mon_species != slot_species:
            raise RuntimeError(
                "battle fixture has invalid party slot "
                f"{slot}: species={slot_species} mon_species={mon_species}"
            )
        if hp <= 0:
            raise RuntimeError(f"battle fixture party slot {slot} has no HP")
        for move_idx in range(4):
            move_id = pb.memory[mon_addr + 8 + move_idx]
            pp = pb.memory[mon_addr + 29 + move_idx] & 0x3F
            if move_id != 0 and pp <= 0:
                raise RuntimeError(f"battle fixture party slot {slot} move {move_idx} has zero PP")
        species.append(int(slot_species))
    return f"party_count={int(count)} species={species}"


def _trace_warning(message: str) -> None:
    """Diagnostics must never replace a peer result or its original exception."""
    try:
        print(f"[peer trace] {message}", file=sys.stderr, flush=True)
    except BaseException:  # noqa: BLE001, S110 - Failed logging must not recurse or mask peer errors.
        pass


class _PeerTraceWatchdog:
    """Opt-in Python stack capture, with at most two one-shot schedules.

    This owns the process-wide faulthandler timer. It does not terminate the
    peer, inspect emulator state, or promise native C stack frames. Stderr is
    best effort: parent filtering/tail limits may discard frames. An explicit
    existing POKERED_PEER_TRACE_DIR retains a private, unique PID artifact.
    Two dump attempts bound output frequency, not an exact byte quota.
    """

    def __init__(self, delay, destination, *, owned=False):
        self.delay = delay
        self.destination = destination
        self.owned = owned
        self.attempts = 0
        self.initial_due = None
        self.closed = False

    @classmethod
    def from_env(cls):
        raw = os.environ.get("POKERED_PEER_TRACE_AFTER_SECONDS")
        if raw is None:
            return None
        try:
            delay = float(raw)
            if not math.isfinite(delay) or delay <= 0:
                raise ValueError("trace delay must be finite and positive")
        except BaseException as exc:  # noqa: BLE001
            _trace_warning(f"invalid trace configuration ({type(exc).__name__}); disabled")
            return None
        trace = cls(delay, sys.stderr)
        directory = os.environ.get("POKERED_PEER_TRACE_DIR")
        if directory is not None:
            try:
                if not directory or not Path(directory).is_dir():
                    raise ValueError("trace directory must already exist")
                # mkstemp uses exclusive creation; never mkdir or
                # overwrite an existing artifact, even for repeated same-PID runs.
                trace.destination, artifact = tempfile.mkstemp(
                    prefix=f"peer-trace-{os.getpid()}-", suffix=".log", dir=directory,
                )
                trace.owned = True
                _trace_warning(f"stack artifact: {artifact}")
            except BaseException as exc:  # noqa: BLE001
                _trace_warning(f"trace artifact open failed ({type(exc).__name__}); using stderr")
        return trace

    def _arm(self, delay):
        if self.closed or self.attempts >= 2:
            return
        # Failed scheduling calls count too; never retry indefinitely.
        self.attempts += 1
        faulthandler.dump_traceback_later(
            delay, repeat=False, file=self.destination, exit=False,
        )

    def start(self):
        if self.closed or self.attempts:
            return
        try:
            self.initial_due = time.monotonic() + self.delay
            self._arm(self.delay)
        except BaseException as exc:  # noqa: BLE001
            _trace_warning(f"trace arming failed ({type(exc).__name__})")

    def cleanup(self, deadline):
        try:
            if self.closed or self.attempts != 1 or self.initial_due is None:
                return
            now = time.monotonic()
            if now < self.initial_due:
                return  # Preserve a pending initial dump; do not postpone it.
            # Elapsed is not observed firing: faulthandler has no fired callback.
            # A delayed initial watchdog may race this replacement. At most two
            # schedules remain possible, including failed scheduling attempts.
            delay = min(self.delay, max(0.000001, deadline - now))
            self._arm(delay)
        except BaseException as exc:  # noqa: BLE001
            _trace_warning(f"cleanup trace arming failed ({type(exc).__name__})")

    def close(self):
        if self.closed:
            return
        try:
            if self.attempts:
                faulthandler.cancel_dump_traceback_later()
        except BaseException as exc:  # noqa: BLE001
            # Raw descriptors have no GC closer: keep it valid until process
            # exit if cancellation was not confirmed, rather than risk reuse.
            _trace_warning(f"trace cancellation failed ({type(exc).__name__}); retaining destination")
            return
        self.closed = True
        if self.owned:
            try:
                os.close(self.destination)
            except BaseException as exc:  # noqa: BLE001
                _trace_warning(f"trace destination close failed ({type(exc).__name__})")


def _run_peer(trace=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--role", choices=("listen", "connect"), required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--deadline-seconds", type=float, default=240.0)
    ap.add_argument("--goal", choices=("link_menu", "trade", "battle"), default="link_menu")
    ap.add_argument(
        "--version",
        choices=("red_gb", "red_color", "blue_gb", "blue_color", "yellow"),
        default="yellow",
    )
    ap.add_argument("--record-dir", type=Path)
    ap.add_argument(
        "--observe-pre-link-menu",
        action="store_true",
        default=False,
        help="Include bounded pre-LinkMenu diagnostics and their availability status.",
    )
    ap.add_argument("--label", default="")
    ap.add_argument("--repo-root", type=Path, required=True)
    args = ap.parse_args()

    # Establish the one process-wide cutoff before any ROM, TCP, or handshake
    # work. The existing gameplay code below continues to use this value.
    deadline = time.monotonic() + args.deadline_seconds
    drive_status = "error"
    drive_error: str | None = None
    deadline_exceeded = False
    session = None
    link = None
    party_before: dict[str, object] = {}
    party_after_trade: dict[str, object] | None = None
    final_state: dict[str, int] = {}
    final_cpu: dict[str, object] = {}
    link_menu_state: dict[str, object] = {}
    link_menu_history = _LinkMenuHistory(None, role=args.role, version=args.version)
    pre_link_menu_history = (
        _PreLinkMenuHistory(enabled=True, role=args.role, version=args.version)
        if args.observe_pre_link_menu else None
    )
    counters = {s: [0] for s in _TRADE_DIAG_SYMBOLS}
    shots: list[str] = []
    select_mon_announced = False
    link_menu_announced = False
    peer_link_menu_ready = False
    link_menu_exchange_announced = False
    peer_link_menu_exchange_ready = False
    battle_turn_announced = False
    peer_battle_turn_ready = False
    link_menu_max = 0

    def log(msg):
        print(f"[peer {args.role}] {msg}", file=sys.stderr, flush=True)

    def close_pre_link_menu_observer():
        if pre_link_menu_history is None:
            return
        try:
            pre_link_menu_history.close()
        except BaseException as exc:  # noqa: BLE001 - Diagnostic cleanup cannot replace gameplay errors.
            pre_link_menu_history.available = False
            pre_link_menu_history.reason = "cleanup_error:" + type(exc).__name__[:128]
            pre_link_menu_history._error("close", "cleanup", exc)

    def remaining(phase: str) -> float:
        return _deadline_remaining(deadline, phase=phase)

    def setup_error_text(exc: BaseException) -> str:
        try:
            message = f"{type(exc).__name__}: {exc}"
        except BaseException:  # noqa: BLE001
            message = type(exc).__name__
        return message[:2048]

    def backend_snapshot() -> dict[str, object]:
        backend = getattr(link, "_network_backend", None) if link is not None else None
        if backend is None:
            return {}
        try:
            return backend.debug_snapshot()
        except BaseException:  # noqa: BLE001
            return {}

    def link_menu_snapshot() -> dict[str, object]:
        """Capture ROM-owned LinkMenu selection fields for failed runs.

        These are observations only.  In particular, this helper never
        writes the send/receive buffers or any connection/warp state.  The
        final values may have been reused after selection and cannot alone
        distinguish a malformed exchange from a valid vote followed by a
        failed warp. The bounded history preserves earlier observations.
        """
        if session is None:
            return {}
        return _read_link_menu_fields(session)

    def emit_result(*, setup_failed: bool = False) -> None:
        if setup_failed or session is None:
            party_after: dict[str, object] = {}
        elif party_after_trade is not None:
            party_after = party_after_trade
        else:
            try:
                party_after = _party_summary(session)
            except BaseException:  # noqa: BLE001
                party_after = {}
        result = {s: counters[s][0] for s in _TRADE_DIAG_SYMBOLS}
        result["_role"] = args.role
        result["_version"] = args.version
        result["party_before"] = {} if setup_failed else party_before
        result["party_after"] = party_after
        result["_final_state"] = final_state
        result["_final_cpu"] = final_cpu
        result["_link_menu_state"] = {} if setup_failed else link_menu_state
        result["_link_menu_history"] = link_menu_history.snapshot()
        if pre_link_menu_history is not None:
            result["_pre_link_menu_history"] = pre_link_menu_history.snapshot()
        result["_shots"] = shots
        result["_backend_stats"] = backend_snapshot()
        result["_drive_status"] = drive_status
        result["_drive_error"] = drive_error
        result["_deadline_exceeded"] = deadline_exceeded
        try:
            encoded = json.dumps(result)
        except (TypeError, ValueError):
            result["party_before"] = {}
            result["party_after"] = {}
            result["_final_state"] = {}
            result["_final_cpu"] = {}
            result["_shots"] = []
            result["_backend_stats"] = {}
            encoded = json.dumps(result)
        log(f"final counters: {result}")
        # Sentinel-delimited JSON line so the parent can grep it out of
        # the ROM-loading warning spam on stdout.
        print(f"__TCP_TRADE_RESULT__ {encoded}")

    def shot(phase: str) -> None:
        if args.record_dir is None:
            return
        # Render one frame on demand so headless/null-window screenshots
        # reflect the restored/current LCD state.
        try:
            session.step(1, render=True)
            img = session._pyboy.screen.image
        except Exception as exc:  # noqa: BLE001
            log(f"shot {phase} failed before save: {type(exc).__name__}: {exc}")
            return
        pair = f"{args.label}." if args.label else ""
        path = args.record_dir / f"{pair}{phase}.{args.role}.{args.version}.png"
        try:
            img.save(path)
        except Exception as exc:  # noqa: BLE001
            log(f"shot {phase} save failed: {type(exc).__name__}: {exc}")
            return
        shots.append(str(path))
        log(f"shot {phase}: {path}")

    def setup() -> None:
        nonlocal link, link_menu_max, party_before, session, pre_link_menu_history

        if not math.isfinite(args.deadline_seconds) or args.deadline_seconds <= 0:
            raise ValueError("deadline-seconds must be finite and positive")
        remaining("setup")
        sys.path.insert(0, str(args.repo_root / "src"))

        from pokered_harness.config import load_versions
        from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
        from pokered_harness.session import Session

        remaining("dependency setup")
        rom_root = Path(os.environ.get("POKERED_ROM_ROOT", args.repo_root / "rom"))
        fixture_root = Path(
            os.environ.get("POKERED_FIXTURE_ROOT", args.repo_root / "tests" / "fixtures" / "link")
        )
        if not rom_root.is_dir():
            raise RuntimeError(f"ROM root not found: {rom_root}")

        rom_paths = {
            "red_gb": (
                rom_root / "red" / "pokemon-red.gb",
                rom_root / "red" / "pokemon-red.sym",
                "red",
                "cable_club-vanilla.state",
                "cable_club-battle-vanilla.state",
            ),
            "red_color": (
                rom_root / "red" / "pokemon-red-color.gb",
                rom_root / "red" / "pokemon-red.sym",
                "red",
                "cable_club.state",
                "cable_club-battle.state",
            ),
            "blue_gb": (
                rom_root / "blue" / "pokemon-blue.gb",
                rom_root / "blue" / "pokemon-blue.sym",
                "blue",
                "cable_club-vanilla.state",
                "cable_club-battle-vanilla.state",
            ),
            "blue_color": (
                rom_root / "blue" / "pokemon-blue-color.gb",
                rom_root / "blue" / "pokemon-blue.sym",
                "blue",
                "cable_club.state",
                "cable_club-battle.state",
            ),
            "yellow": (
                rom_root / "yellow" / "pokemon-yellow.gbc",
                rom_root / "yellow" / "pokemon-yellow.sym",
                "yellow",
                "cable_club.state",
                "cable_club-battle.state",
            ),
        }
        rom, sym, fixture_version, trade_fixture_name, battle_fixture_name = rom_paths[args.version]
        if pre_link_menu_history is not None:
            try:
                observer_rom_bytes = rom.read_bytes()
                observer_symbol_bytes = sym.read_bytes()
            except Exception as exc:  # noqa: BLE001 - Optional diagnostics must not fail gameplay.
                pre_link_menu_history.reason = f"asset_read_error:{type(exc).__name__}"[:128]
            else:
                pre_link_menu_history = _resolve_pre_link_menu(
                    enabled=True,
                    role=args.role,
                    version=args.version,
                    rom_bytes=observer_rom_bytes,
                    symbol_bytes=observer_symbol_bytes,
                )
        # The color Red/Blue Cable Club menu has three choices (0..2), while
        # Yellow adds a fourth (0..3). Keep the readiness predicate ROM-aware;
        # a hard-coded bound can otherwise leave a valid peer spinning until the
        # outer gameplay deadline without ever announcing the phase.
        link_menu_max = 3 if fixture_version == "yellow" else 2
        fixture_name = battle_fixture_name if args.goal == "battle" else trade_fixture_name
        state = fixture_root / fixture_version / fixture_name

        if args.record_dir is not None:
            remaining("record directory setup")
            args.record_dir.mkdir(parents=True, exist_ok=True)
            remaining("record directory setup")

        log(f"loading {args.version} ROM + state")
        remaining("version pin load")
        pins = load_versions(args.repo_root / "VERSIONS.md")
        expected_sha = pins.sha1_for_path(rom)
        if expected_sha is None:
            raise RuntimeError(f"no VERSIONS.md SHA-1 pin for {rom}")
        remaining("ROM setup")
        session = Session.from_files(
            rom,
            sym,
            expected_rom_sha1=expected_sha,
            expected_pyboy_version=pins.pyboy_version,
        )
        remaining("ROM setup")
        session.load_state(state.read_bytes())
        remaining("state setup")
        party_before = _party_summary(session)
        if args.goal == "battle":
            log(f"battle fixture validated: {_validate_battle_party_fixture(session)}")
        shot("00_loaded")
        log("state loaded, installing hooks")

        remaining("hook setup")
        link_menu_history.session = session
        if pre_link_menu_history is None:
            link_menu_history.install(counters)
        else:
            observers_by_address = {}
            try:
                pre_link_menu_history.install(session)
                if pre_link_menu_history.reason == "external_pending":
                    for event, bank, address in pre_link_menu_history.config.sites:
                        if event == "Serial_SyncAndExchangeNybble":
                            observers_by_address[(bank, address)] = pre_link_menu_history
            except BaseException as exc:  # noqa: BLE001 - Optional diagnostics cannot fail setup.
                close_pre_link_menu_observer()
                pre_link_menu_history.reason = "observer_install_error:" + type(exc).__name__[:128]
                pre_link_menu_history._error("install", "setup", exc)
            link_menu_history.install(counters, observers_by_address=observers_by_address)

        log(f"establishing TCP {args.role}")
        if args.role == "listen":
            link = PyBoyLinkSession.listen(
                args.port,
                host=args.host,
                local_rom_version=fixture_version,
                accept_timeout_s=min(10.0, remaining("TCP listen")),
            )
        else:
            last_exc: Exception | None = None
            for attempt in range(60):
                remaining("TCP connect")
                try:
                    link = PyBoyLinkSession.connect(
                        args.host,
                        args.port,
                        local_rom_version=fixture_version,
                        timeout_s=min(10.0, remaining("TCP connect")),
                    )
                    break
                except OSError as exc:
                    last_exc = exc
                    if attempt == 59:
                        raise
                    time.sleep(min(0.25, remaining("TCP connect retry")))
            else:
                raise RuntimeError(f"could not connect to listener: {last_exc}")
        remaining("TCP setup")
        log("TCP established, attaching PyBoy")
        link.attach(session._pyboy)
        remaining("PyBoy attach")
        backend = getattr(link, "_network_backend", None)
        if backend is None:
            raise RuntimeError("network backend missing after attach")
        peer_version = backend.wait_for_hello(timeout=min(30.0, remaining("HELLO handshake")))
        remaining("HELLO handshake")
        selected_internal = link.negotiate_network_clock_role(peer_version)
        remaining("network clock negotiation")
        log(
            f"versioned handshake complete: local={fixture_version} "
            f"peer={peer_version} native_internal_clock={selected_internal}"
        )
        log("attached; starting drive loop")

    setup_complete = False
    try:
        setup()
        setup_complete = True
    except BaseException as exc:  # noqa: BLE001
        timed_out = isinstance(exc, TimeoutError)
        if not timed_out:
            try:
                timed_out = (
                    math.isfinite(deadline)
                    and args.deadline_seconds > 0
                    and time.monotonic() >= deadline
                )
            except (TypeError, ValueError):
                timed_out = False
        deadline_exceeded = timed_out
        drive_status = "deadline" if timed_out else "error"
        drive_error = setup_error_text(exc)
        log(f"EXCEPTION in setup: {drive_error}")
    finally:
        if not setup_complete:
            if pre_link_menu_history is not None:
                close_pre_link_menu_observer()
            if trace is not None:
                trace.cleanup(deadline)
            partial_backend = getattr(link, "_network_backend", None) if link is not None else None
            try:
                if link is not None:
                    link.detach_all()
            except BaseException as exc:  # noqa: BLE001
                log(f"link cleanup raised {type(exc).__name__}: {exc}")
            try:
                if session is not None:
                    session.close()
            except BaseException as exc:  # noqa: BLE001
                log(f"session cleanup raised {type(exc).__name__}: {exc}")
            try:
                if partial_backend is not None:
                    partial_backend.stop()
            except BaseException as exc:  # noqa: BLE001
                log(f"backend cleanup raised {type(exc).__name__}: {exc}")
            emit_result(setup_failed=True)
    if not setup_complete:
        return 1

    drive_status = "ok"

    def state_snapshot() -> dict[str, int]:
        snapshot = {
            "map_id": session.read_game_state().overworld.map_id,
            "hSerialConnectionStatus": session._pyboy.memory[
                session.symbols.addr_of("hSerialConnectionStatus")
            ],
            "wLinkState": session._pyboy.memory[session.symbols.addr_of("wLinkState")],
        }
        for name in (
            "wIsInBattle",
            "wBattleType",
            "wActionResultOrTookBattleTurn",
            "wMoveMenuType",
            "wPlayerSelectedMove",
        ):
            try:
                snapshot[name] = int(session._pyboy.memory[session.symbols.addr_of(name)])
            except (AttributeError, KeyError, TypeError):
                pass
        return snapshot

    def cpu_snapshot() -> dict[str, object]:
        """Capture bounded CPU/serial state for a stalled native run."""
        cpu = getattr(getattr(session._pyboy, "mb", None), "cpu", None)
        serial = getattr(getattr(session._pyboy, "mb", None), "serial", None)
        result: dict[str, object] = {}
        for name in (
            "PC",
            "SP",
            "A",
            "F",
            "B",
            "C",
            "D",
            "E",
            "H",
            "L",
            "cycles",
            "halted",
            "stopped",
            "interrupt_master_enable",
            "interrupts_enabled_register",
            "interrupts_flag_register",
        ):
            if hasattr(cpu, name):
                result[name] = getattr(cpu, name)
        for name in (
            "SB",
            "SC",
            "transfer_enabled",
            "internal_clock",
            "_bits_remaining",
            "clock",
            "clock_target",
        ):
            if hasattr(serial, name):
                result[f"serial.{name}"] = getattr(serial, name)
        return result

    def cooperative_sync(sync_id: int, *, timeout: float = 60.0, step_frames: int = 4) -> None:
        """Rendezvous without freezing the local emulator thread.

        A blocking barrier is safe only when neither ROM can be servicing
        serial IRQs. At a nominal UI boundary the peer may still have one
        last edge in flight, so continue ticking while waiting for the
        marker. This keeps the slave able to re-arm and makes the boundary
        an orchestration point rather than a scheduler stop.
        """
        if isinstance(step_frames, bool) or not isinstance(step_frames, int):
            raise TypeError("step_frames must be a positive integer")
        if step_frames <= 0:
            raise ValueError("step_frames must be a positive integer")
        link._network_backend.announce_sync(sync_id=sync_id)
        deadline_at = time.monotonic() + timeout
        while time.monotonic() < deadline_at:
            if link._network_backend.poll_peer_sync(sync_id=sync_id):
                return
            session.step(step_frames)
        raise RuntimeError(
            f"cooperative sync {sync_id} did not converge: "
            f"local={state_snapshot()} backend={backend_snapshot()}"
        )

    def passive_sync(*, ready_sync_id: int, release_sync_id: int, timeout: float = 60.0) -> None:
        """Rendezvous without advancing the restored game state.

        This is used only before the first gameplay input. Both peers have
        already attached and negotiated their native roles, so progressing a
        ROM while the other process is still in setup can create a
        direction-dependent first exchange. A two-marker control handshake
        keeps this pre-drive phase transport-only and clamps its deadline to
        the process-wide cutoff.
        """
        backend = link._network_backend
        deadline_at = min(deadline, time.monotonic() + timeout)

        def wait_for_peer(marker: int, *, phase: str) -> None:
            while not backend.poll_peer_sync(sync_id=marker):
                remaining_at = deadline_at - time.monotonic()
                if remaining_at <= 0:
                    raise RuntimeError(
                        f"passive sync {phase} did not converge: "
                        f"marker={marker} backend={backend_snapshot()}"
                    )
                time.sleep(min(0.001, remaining_at))

        backend.announce_sync(sync_id=ready_sync_id)
        wait_for_peer(ready_sync_id, phase="ready")
        backend.announce_sync(sync_id=release_sync_id)
        wait_for_peer(release_sync_id, phase="release")

    def peer_shutdown_sync(
        *, ready_sync_id: int, release_sync_id: int, timeout: float = 120.0
    ) -> None:
        _peer_shutdown_sync(
            link._network_backend,
            cooperative_sync=cooperative_sync,
            step=session.step,
            backend_snapshot=backend_snapshot,
            ready_sync_id=ready_sync_id,
            release_sync_id=release_sync_id,
            timeout=timeout,
        )

    def current_menu_item() -> int | None:
        try:
            return session._pyboy.memory[session.symbols.addr_of("wCurrentMenuItem")]
        except (AttributeError, KeyError, TypeError):
            return None

    def menu_snapshot() -> dict[str, int]:
        memory = session._pyboy.memory
        snapshot: dict[str, int] = {}
        for name in (
            "wCurrentMenuItem",
            "wMaxMenuItem",
            "wMenuWatchedKeys",
            "wMenuJoypadPollCount",
            "wMenuWrappingEnabled",
            "wMenuWatchMovingOutOfBounds",
        ):
            try:
                snapshot[name] = int(memory[session.symbols.addr_of(name)])
            except (AttributeError, KeyError, TypeError):
                pass
        return snapshot

    def wait_for_menu_ready(
        *,
        label: str,
        min_item: int,
        max_item: int,
        expected_max: int | None = None,
        required_keys: int = 0x01,
        timeout: float = 60.0,
    ) -> dict[str, int]:
        """Wait until ROM menu fields describe an input-ready menu."""
        deadline_at = time.monotonic() + timeout
        while time.monotonic() < deadline_at:
            snapshot = menu_snapshot()
            current = snapshot.get("wCurrentMenuItem")
            configured_max = snapshot.get("wMaxMenuItem")
            watched = snapshot.get("wMenuWatchedKeys", 0)
            if (
                current is not None
                and configured_max is not None
                and min_item <= current <= max_item
                and (expected_max is None or configured_max == expected_max)
                and watched & required_keys == required_keys
            ):
                return snapshot
            session.step(2)
        raise RuntimeError(
            f"{label} did not become input-ready: "
            f"menu={menu_snapshot()} state={state_snapshot()} "
            f"cpu={cpu_snapshot()} backend={backend_snapshot()}"
        )

    def menu_fields_ready(
        *,
        min_item: int,
        max_item: int,
        expected_max: int | None = None,
        required_keys: int = 0x01,
    ) -> bool:
        snapshot = menu_snapshot()
        current = snapshot.get("wCurrentMenuItem")
        configured_max = snapshot.get("wMaxMenuItem")
        watched = snapshot.get("wMenuWatchedKeys", 0)
        return bool(
            current is not None
            and configured_max is not None
            and min_item <= current <= max_item
            and (expected_max is None or configured_max == expected_max)
            and watched & required_keys == required_keys
        )

    def wait_for_link_menu_selection_exchange(*, label: str, timeout: float = 120.0) -> None:
        """Require each ROM to observe a real, non-idle LinkMenu exchange.

        The control marker only reports local ROM evidence.  It never
        selects a menu item or infers Game Boy clock ownership from the TCP
        role.  Keep stepping while the peer catches up because either ROM may
        be the active serial clock at this point.
        """
        nonlocal link_menu_exchange_announced, peer_link_menu_exchange_ready

        deadline_at = min(deadline, time.monotonic() + timeout)
        while time.monotonic() < deadline_at:
            if (
                not link_menu_exchange_announced
                and _link_menu_has_real_selection_exchange(link_menu_history)
            ):
                link._network_backend.announce_sync(sync_id=125)
                link_menu_exchange_announced = True
                log(f"{label}: local LinkMenu selection exchange observed")
            if link_menu_exchange_announced and not peer_link_menu_exchange_ready:
                peer_link_menu_exchange_ready = link._network_backend.poll_peer_sync(sync_id=125)
            if link_menu_exchange_announced and peer_link_menu_exchange_ready:
                log(f"{label}: peer LinkMenu selection exchange observed")
                return
            session.step(1)
        raise RuntimeError(
            f"{label} LinkMenu selection exchange did not converge: "
            f"local_evidence={link_menu_exchange_announced} "
            f"peer_evidence={peer_link_menu_exchange_ready} "
            f"history={link_menu_history.snapshot()} "
            f"menu={menu_snapshot()} state={state_snapshot()} "
            f"backend={backend_snapshot()}"
        )

    def move_menu_to_item(
        *,
        target: int,
        min_item: int,
        max_item: int,
        label: str,
        timeout: float = 30.0,
        input_duration: int = 2,
        settle_frames: int = 4,
    ) -> None:
        """Move a ROM-owned cursor with bounded one-shot directions."""
        if input_duration <= 0 or settle_frames <= 0:
            raise ValueError("menu input and settle durations must be positive")
        deadline_at = time.monotonic() + timeout
        while time.monotonic() < deadline_at:
            snapshot = wait_for_menu_ready(
                label=label,
                min_item=min_item,
                max_item=max_item,
                timeout=min(5.0, max(0.1, deadline_at - time.monotonic())),
            )
            current = snapshot["wCurrentMenuItem"]
            if current == target:
                return
            if current < target:
                button = "down"
            else:
                button = "up"
            session.press(button, duration=input_duration)
            session.step(settle_frames)
        raise RuntimeError(
            f"{label} cursor did not reach {target}: "
            f"menu={menu_snapshot()} state={state_snapshot()} "
            f"cpu={cpu_snapshot()} backend={backend_snapshot()}"
        )

    def read_active_battle_moves() -> tuple[tuple[int, int], ...]:
        """Read the ROM-populated active move/PP slots without mutation."""
        memory = session._pyboy.memory
        addr_of = session.symbols.addr_of
        moves_addr = addr_of("wBattleMonMoves")
        pp_addr = addr_of("wBattleMonPP")
        return tuple(
            (
                int(memory[moves_addr + move_idx]),
                int(memory[pp_addr + move_idx]) & 0x3F,
            )
            for move_idx in range(4)
        )

    def choose_first_usable_battle_move() -> int:
        """Navigate the real move menu to the first move with PP."""
        ready = wait_for_menu_ready(
            label="battle move menu",
            min_item=1,
            max_item=4,
            required_keys=0x01,
            timeout=60.0,
        )
        active_moves = read_active_battle_moves()
        move_count = ready.get("wMaxMenuItem", 0) - 1
        if not 1 <= move_count <= len(active_moves):
            raise RuntimeError(f"ROM move-menu count is invalid: menu={ready} moves={active_moves}")
        usable_slots = [
            index
            for index, (move_id, pp) in enumerate(active_moves[:move_count])
            if move_id != 0 and pp > 0
        ]
        if not usable_slots:
            raise RuntimeError(f"active battle mon has no usable move: moves={active_moves}")
        # Move-menu cursors are one-based. Let the ROM install the cursor
        # before reading it; no RAM write or test-only selection hook is used.
        target = usable_slots[0]
        move_menu_to_item(
            target=target + 1,
            min_item=1,
            max_item=move_count,
            label="battle move menu",
        )
        # A legal move is committed through the normal input path. After this
        # point the battle loop runs without synthetic input.  As with the
        # command menu, the peer may still be leaving the phase rendezvous;
        # retry only while the ROM continues to expose the move menu and stop
        # as soon as the post-menu control-flow label fires.
        prior_move_selection_phase = counters["MainInBattleLoop.selectEnemyMove"][0]
        next_move_input_tick = -1
        move_input_attempts = 0
        selected_move_id = active_moves[target][0]
        selection_deadline = min(time.monotonic() + 30.0, deadline)
        while time.monotonic() < selection_deadline:
            if counters["MainInBattleLoop.selectEnemyMove"][0] > prior_move_selection_phase:
                return selected_move_id
            if (
                menu_fields_ready(
                    min_item=1,
                    max_item=move_count,
                    required_keys=0x01,
                )
                and session.current_tick() >= next_move_input_tick
            ):
                session.press("a")
                move_input_attempts += 1
                next_move_input_tick = session.current_tick() + 8
            session.step(2)
        raise RuntimeError(
            "ROM move-menu A input was not consumed: "
            f"expected={selected_move_id} "
            f"select_enemy_move={counters['MainInBattleLoop.selectEnemyMove'][0]} "
            f"move_input_attempts={move_input_attempts} "
            f"menu={menu_snapshot()} counters={counters} "
            f"state={state_snapshot()} cpu={cpu_snapshot()} "
            f"backend={backend_snapshot()}"
        )

    try:
        # Both independent processes must finish attach/HELLO/clock-role
        # selection before either owner begins driving the restored game
        # state. Keep this rendezvous passive: advancing one ROM while the
        # other is still constructing its PyBoy can produce a
        # direction-dependent first serial exchange.
        passive_sync(ready_sync_id=99, release_sync_id=98, timeout=60.0)
        # Phase 1: walk UP ×3 + A-mash to reach LinkMenu.
        for _ in range(3):
            session.press("up", duration=6)
            session.step(20)
        # Start the receptionist interaction from a synchronized input
        # boundary so both processes enter the Cable Club dialog at
        # nearly the same game phase.
        # The initial movement boundary can still overlap the ROM's final
        # connection-role negotiation. Keep ticking while waiting for the
        # peer marker so a slave IRQ can re-arm instead of freezing one
        # process inside a blocking transport barrier.
        cooperative_sync(sync_id=100, timeout=60.0)
        log("phase 1 start")
        last_progress = time.monotonic()
        serial_phase_ticks = 0
        peer_link_menu_ready = False
        while time.monotonic() < deadline:
            # TCP listener/connector is not a Game Boy clock-role contract.
            # Cable Club may invert clock ownership while either ROM remains
            # in its polling loop, so a peer which has already entered
            # LinkMenu must continue authentic emulation until the other ROM
            # independently observes that same entry.  Do not wait for wire
            # quietness here: the next legitimate transfer can be the peer's
            # final pre-menu edge.
            if link_menu_announced:
                if not peer_link_menu_ready:
                    peer_link_menu_ready = link._network_backend.poll_peer_sync(sync_id=121)
                if peer_link_menu_ready:
                    _finish_link_menu_phase(
                        args.goal,
                        cooperative_sync=cooperative_sync,
                        peer_shutdown_sync=peer_shutdown_sync,
                    )
                    log("phase 1 done: LinkMenu fired on both peers")
                    break
                session.step(1)
                continue
            in_serial_phase = (
                counters["SaveGameData"][0] > 0 or counters["Serial_SyncAndExchangeNybble"][0] > 0
            )
            if in_serial_phase:
                # Do not block on a phase barrier while either emulator is
                # inside a serial transfer. The slave's main thread must be
                # allowed to run its serial IRQ handler and re-arm SC while
                # the peer's NetworkBackend is waiting for this edge. The
                # network edge/response protocol already provides the
                # per-byte synchronization; explicit barriers are reserved
                # for safe UI/phase boundaries below.
                session.step(4)
                serial_phase_ticks += 1
                if (
                    counters["LinkMenu.waitForInputLoop"][0] > 0
                    and menu_fields_ready(
                        min_item=0,
                        max_item=link_menu_max,
                        expected_max=link_menu_max,
                        required_keys=0x01,
                    )
                    and not link_menu_announced
                ):
                    link_menu_announced = True
                    log("phase 1 local LinkMenu fired")
                    shot("01_link_menu")
                    link._network_backend.announce_sync(sync_id=121)
                    log("phase 1 LinkMenu readiness sent")
            else:
                if (
                    counters["LinkMenu.waitForInputLoop"][0] > 0
                    and menu_fields_ready(
                        min_item=0,
                        max_item=link_menu_max,
                        expected_max=link_menu_max,
                        required_keys=0x01,
                    )
                    and not link_menu_announced
                ):
                    link_menu_announced = True
                    log("phase 1 local LinkMenu fired")
                    shot("01_link_menu")
                    link._network_backend.announce_sync(sync_id=121)
                    log("phase 1 LinkMenu readiness sent")
                session.press("a", duration=4)
                session.step(40)
            if time.monotonic() - last_progress > 10.0:
                log(
                    f"phase 1 progress: "
                    f"CableClubNPC={counters['CableClubNPC'][0]} "
                    f"SaveGameData={counters['SaveGameData'][0]} "
                    f"Serial_SyncAndExchangeNybble={counters['Serial_SyncAndExchangeNybble'][0]} "
                    f"LinkMenu={counters['LinkMenu'][0]} "
                    f"waitForInputLoop={counters['LinkMenu.waitForInputLoop'][0]} "
                    f"menu={menu_snapshot()}"
                )
                last_progress = time.monotonic()

        if not link_menu_announced or not peer_link_menu_ready:
            raise RuntimeError(
                "LinkMenu rendezvous did not converge before the gameplay phase: "
                f"local_announced={link_menu_announced} "
                f"peer_ready={peer_link_menu_ready} "
                f"menu={menu_snapshot()} state={state_snapshot()} "
                f"backend={backend_snapshot()}"
            )

        if args.goal == "trade":
            # Phase barrier: both sides at LinkMenu before voting Trade
            # Center. Without this the vote-exchange nibble loop has
            # no way to guarantee overlap in Pokemon's polling windows.
            log("sync: link_menu barrier")
            cooperative_sync(sync_id=1, timeout=60.0)
            log("sync: past link_menu barrier")
            # The LinkMenu hook fires before the ROM has finished installing
            # its final menu fields. Settle those fields and rendezvous at
            # the actual Trade Center cursor before sending A; otherwise a
            # peer can consume the selection while the other is still in the
            # menu's setup loop.
            wait_for_menu_ready(
                label="trade LinkMenu",
                min_item=0,
                max_item=link_menu_max,
                expected_max=link_menu_max,
                required_keys=0x01,
                timeout=60.0,
            )
            session.step(60)
            move_menu_to_item(
                target=0,
                min_item=0,
                max_item=link_menu_max,
                label="trade LinkMenu",
                input_duration=12,
                settle_frames=40,
            )
            # Both peers have now reached the ROM-owned Trade cursor. First
            # rendezvous before the real A event so the two independent
            # processes enter the ROM's selection exchange from the same
            # input boundary. A one-frame cooperative barrier keeps each
            # owner thread live for any final serial edge without allowing
            # one side to consume the choice several host frames ahead.
            cooperative_sync(sync_id=19, timeout=120.0, step_frames=1)
            session.press("a", duration=4)
            # A queued input is not evidence that Cable Club exchanged a
            # selection.  Advance only after both ROMs independently report
            # the post-call sent-and-received vote evidence.
            wait_for_link_menu_selection_exchange(label="trade")
            log("trade menu selection exchange verified on both peers")
            # Trade Center warp — A-mash until map becomes 0xEF. Once one
            # peer reaches the map it must keep ticking while the other peer
            # completes its ROM-owned selection exchange; stopping the first
            # owner here can strand the second peer before it can announce
            # the same milestone. Keep the established 20-frame input
            # cadence here: one-frame calls can leave the ROM's joypad pulse
            # and serial handoff split across too many host-thread scheduling
            # boundaries on a cross-family pair.
            TRADE_CENTER = 0xEF
            warp_announced = False
            peer_warp_ready = False
            while time.monotonic() < deadline:
                if (
                    session.read_game_state().overworld.map_id == TRADE_CENTER
                    and not warp_announced
                ):
                    link._network_backend.announce_sync(sync_id=2)
                    warp_announced = True
                    log("local trade-center warp announced")
                    shot("02_trade_center")
                if warp_announced and link._network_backend.poll_peer_sync(sync_id=2):
                    peer_warp_ready = True
                    break
                if not warp_announced:
                    session.press("a", duration=4)
                session.step(20)
            if not (warp_announced and peer_warp_ready):
                raise RuntimeError(
                    "trade-center warp rendezvous did not converge: "
                    f"local={state_snapshot()} backend={backend_snapshot()}"
                )
            log("trade center warp complete on both peers")

            # Both ROMs have now reached the map, but the peer can still have
            # one final serial edge in flight. Drain it while both owners
            # continue ticking, then use a symmetric release rendezvous. A
            # no-tick hold is unsafe here because an already-armed ROM may
            # still need its next native serial callback to finish the phase.
            link._network_backend.wait_for_wire_idle(
                timeout=120.0,
                progress_callback=lambda: session.step(1),
                stable_checks=4,
            )
            cooperative_sync(sync_id=18, timeout=120.0, step_frames=1)
            log("sync: trade-center cooperative barrier complete")
            shot("03_post_warp_sync")

            # Walk onto hidden-event trigger tile.
            conn_status = session._pyboy.memory[session.symbols.addr_of("hSerialConnectionStatus")]
            walk_dir = "right" if conn_status == 0x02 else "left"
            for _ in range(6):
                if counters["CableClubLeftGameboy"][0] + counters["CableClubRightGameboy"][0] > 0:
                    break
                session.press(walk_dir, duration=8)
                session.step(30)

            # A-mash to dismiss "JUST A MOMENT!" and start
            # CableClub_DoBattleOrTrade. NO sync barrier here — the
            # big trainer/party block exchange that runs inside
            # CableClub_DoBattleOrTrade needs both sides' CPUs
            # actively ticking to exchange bytes. Blocking on a
            # barrier mid-exchange would stall both ends. Instead
            # we rely on the warp barrier (sync_id=2) aligning us
            # closely enough that the natural parallel tick rates
            # keep the exchange progressing on both sides.
            while time.monotonic() < deadline:
                if counters["CableClub_DoBattleOrTrade"][0] > 0:
                    break
                session.press("a", duration=4)
                session.step(20)
            log(
                "CableClub_DoBattleOrTrade fired; big exchange running "
                f"{state_snapshot()} "
                f"backend={backend_snapshot()}"
            )

            # Wait for the big exchange to complete — detect via
            # TradeCenter_SelectMon firing (runs after the exchange
            # + warp-to-trade-flow).
            last_exchange_log = time.monotonic()
            peer_ready_for_select_mon = False
            while time.monotonic() < deadline:
                if counters["TradeCenter_SelectMon"][0] > 0 and not select_mon_announced:
                    link._network_backend.announce_sync(sync_id=3)
                    select_mon_announced = True
                    log(
                        "announced select_mon ready "
                        f"{state_snapshot()} "
                        f"CallCurrentTradeCenterFunction={counters['CallCurrentTradeCenterFunction'][0]} "
                        f"TradeCenter_SelectMon={counters['TradeCenter_SelectMon'][0]} "
                        f"backend={backend_snapshot()}"
                    )
                    shot("04_select_mon_ready")
                if select_mon_announced and link._network_backend.poll_peer_sync(sync_id=3):
                    peer_ready_for_select_mon = True
                    log(
                        f"peer announced select_mon ready {state_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                    break
                session.step(20)
                if time.monotonic() - last_exchange_log > 15.0:
                    log(
                        "waiting for select_mon convergence "
                        f"{state_snapshot()} "
                        f"CableClub_DoBattleOrTrade={counters['CableClub_DoBattleOrTrade'][0]} "
                        f"CallCurrentTradeCenterFunction={counters['CallCurrentTradeCenterFunction'][0]} "
                        f"TradeCenter_SelectMon={counters['TradeCenter_SelectMon'][0]} "
                        f"backend={backend_snapshot()}"
                    )
                    last_exchange_log = time.monotonic()
            if select_mon_announced:
                if not peer_ready_for_select_mon:
                    log(
                        "peer never announced select_mon before deadline "
                        f"{state_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                else:
                    # One side can reach TradeCenter_SelectMon before the peer,
                    # but blocking immediately on a barrier can starve the
                    # slower side of the CPU progress it still needs to finish
                    # the same exchange. Keep stepping until the peer's own
                    # announcement arrives, then use a real barrier so both
                    # sides start menu navigation from a matched boundary.
                    settle_deadline = min(deadline, time.monotonic() + 10.0)
                    while time.monotonic() < settle_deadline:
                        session.step(20)
                    log(
                        "TradeCenter_SelectMon converged on both peers; "
                        f"big exchange done {state_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                    shot("05_select_mon_converged")
            else:
                log(
                    "TradeCenter_SelectMon not reached before deadline "
                    f"{state_snapshot()} "
                    f"backend={backend_snapshot()}"
                )
            if select_mon_announced and peer_ready_for_select_mon:
                # Barrier here: both sides are post-exchange, about to
                # drive the menu. Safe to sync (game is in UI-setup phase,
                # no active serial traffic).
                log("sync: select_mon barrier")
                cooperative_sync(sync_id=3, timeout=60.0)
                log("sync: past select_mon barrier")
                shot("06_select_mon_sync")
            else:
                log("skipping select_mon barrier; peers never converged")

            # State-aware menu navigation.
            log(f"entering menu nav; counters={ {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS} }")
            last_log = time.monotonic()
            stats_key = "TradeCenter_SelectMon.selectStatsMenuItem"
            trade_key = "TradeCenter_SelectMon.selectTradeMenuItem"
            menu_key = "TradeCenter_SelectMon.playerMonMenu_HandleInput"
            tct_key = "TradeCenter_Trade"
            prev = {k: counters[k][0] for k in (stats_key, trade_key, menu_key, tct_key)}
            right_pending = 0
            while time.monotonic() < deadline and counters["_AddEnemyMonToPlayerParty"][0] == 0:
                session.step(40)
                if time.monotonic() - last_log > 15.0:
                    snap = {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS}
                    log(f"menu nav progress: {snap}")
                    last_log = time.monotonic()
                now = {k: counters[k][0] for k in (stats_key, trade_key, menu_key, tct_key)}
                if now[trade_key] > prev[trade_key]:
                    session.press("a", duration=4)
                    right_pending = 0
                elif now[stats_key] > prev[stats_key]:
                    right_pending = 5
                    session.press("right", duration=12)
                elif right_pending > 0:
                    session.press("right", duration=12)
                    right_pending -= 1
                elif now[menu_key] > prev[menu_key] or now[tct_key] > prev[tct_key]:
                    session.press("a", duration=4)
                else:
                    session.press("a", duration=4)
                prev = now

            # Post-trade sync. Whichever side's
            # _AddEnemyMonToPlayerParty fired first has finished the
            # trade locally but the peer may still be mid-exchange
            # waiting for a few final bytes. Exiting the drive loop
            # immediately would tear down our SerialCore and leave
            # the peer's on_edge calls timing out. The shutdown
            # handshake announces this milestone while both owners
            # continue servicing authentic serial work, then closes
            # only after both peers acknowledge a quiet transport.
            if counters["_AddEnemyMonToPlayerParty"][0] > 0:
                # The hook is at function entry. Advance through the
                # copy routine before the rendezvous so the result records
                # the exchanged party record, not the later room-cleanup
                # state after the trade animation.
                session.step(120)
                party_after_trade = _party_summary(session)
                log("sync: post-trade barrier")
                try:
                    cooperative_sync(sync_id=4, timeout=120.0)
                    log("sync: past post-trade barrier")
                    shot("07_post_trade")
                except Exception as exc:  # noqa: BLE001
                    drive_status = "error"
                    drive_error = f"{type(exc).__name__}: {exc}"
                    log(f"post-trade sync raised {type(exc).__name__}: {exc}")
                log("sync: post-trade shutdown drain")
                peer_shutdown_sync(ready_sync_id=5, release_sync_id=6)
                log("sync: post-trade shutdown barrier complete")
        elif args.goal == "battle":
            log("sync: link_menu battle barrier")
            cooperative_sync(sync_id=11, timeout=120.0)
            log("sync: past link_menu battle barrier")

            # LinkMenu opens with TRADE selected (item 0); BATTLE is the
            # next item (item 1). Select it with ordinary directional input
            # in case a fixture or a prior menu leaves the cursor elsewhere,
            # then commit the choice with A. No RAM writes or execution hooks
            # may select the battle mode: this is the same user-input path
            # an MCP client would use.
            # LinkMenu's entry hook precedes its text rendering and menu
            # initialization. Wait for the ROM-owned menu fields, navigate
            # with bounded one-shot input, and verify the cursor before
            # committing the choice.
            link_menu_before = wait_for_menu_ready(
                label="battle LinkMenu",
                min_item=0,
                max_item=link_menu_max,
                expected_max=link_menu_max,
                required_keys=0x01,
                timeout=60.0,
            )
            initial_item = link_menu_before["wCurrentMenuItem"]
            # LinkMenu's hook and menu-field initialization occur before its
            # first stable joypad polling window. Give the ROM the same
            # post-entry settling interval as the in-process acceptance
            # driver before issuing the directional event.
            session.step(60)
            move_menu_to_item(
                target=1,
                min_item=0,
                max_item=link_menu_max,
                label="battle LinkMenu",
                input_duration=12,
                settle_frames=40,
            )
            selected_item = current_menu_item()
            log(
                "battle LinkMenu input selection; "
                f"initial_item={initial_item} selected_item={selected_item}"
            )
            if selected_item != 1:
                raise RuntimeError(
                    "ordinary LinkMenu input did not select COLOSSEUM: "
                    f"initial_item={initial_item} selected_item={selected_item} "
                    f"state={state_snapshot()}"
                )
            # Both peers have now observed the ROM-owned BATTLE cursor. Keep
            # each owner live while rendezvousing at this boundary: a visible
            # menu does not prove that the final LinkMenu serial edge has
            # drained, and a no-tick hold could strand an EDGE_RESP. Once both
            # processes announce readiness, they commit A from the same menu
            # phase without starving the owner pump.
            cooperative_sync(sync_id=117, timeout=120.0, step_frames=1)
            session.press("a", duration=4)
            # Do not advance based on the input event alone.  Both ROMs must
            # return from their own native LinkMenu selection exchange.
            wait_for_link_menu_selection_exchange(label="battle")
            shot("02_battle_menu")

            COLOSSEUM = 0xF0
            # Do not treat the selection A press as proof of a warp.  A
            # cross-version peer can leave LinkMenu first and continue to
            # tick through Cable Club while this ROM is still waiting for its
            # own ROM-owned selection exchange.  Keep the local emulator
            # active, retry only through the public A-input path, and fail
            # closed if the observable map never changes.
            battle_warp_deadline = min(deadline, time.monotonic() + 120.0)
            while time.monotonic() < battle_warp_deadline:
                if session.read_game_state().overworld.map_id == COLOSSEUM:
                    break
                session.press("a", duration=4)
                session.step(20)
            if session.read_game_state().overworld.map_id != COLOSSEUM:
                raise RuntimeError(
                    "battle LinkMenu selection did not reach Colosseum: "
                    f"state={state_snapshot()} menu={menu_snapshot()} "
                    f"backend={backend_snapshot()}"
                )

            # Hold the first peer at the verified map boundary while the
            # other peer completes its own ordinary menu selection.  The
            # cooperative wait continues stepping the ROM, so a native
            # serial IRQ cannot be starved by the phase rendezvous.
            battle_warp_announced = False
            peer_battle_warp_ready = False
            battle_warp_sync_deadline = min(deadline, time.monotonic() + 120.0)
            while time.monotonic() < battle_warp_sync_deadline:
                if not battle_warp_announced:
                    link._network_backend.announce_sync(sync_id=112)
                    battle_warp_announced = True
                    log(f"battle Colosseum warp verified; readiness sent {state_snapshot()}")
                if link._network_backend.poll_peer_sync(sync_id=112):
                    peer_battle_warp_ready = True
                    break
                session.step(20)
            if not peer_battle_warp_ready:
                raise RuntimeError(
                    "battle Colosseum warp rendezvous did not converge: "
                    f"local={state_snapshot()} backend={backend_snapshot()}"
                )
            log("battle Colosseum warp complete on both peers")
            shot("03_colosseum")
            session.step(120)

            conn_status = session._pyboy.memory[session.symbols.addr_of("hSerialConnectionStatus")]
            walk_dir = "right" if conn_status == 0x02 else "left"
            for _ in range(6):
                if counters["CableClub_DoBattleOrTrade"][0] > 0:
                    break
                session.press(walk_dir, duration=8)
                session.step(30)
            while time.monotonic() < deadline:
                if counters["CableClub_DoBattleOrTrade"][0] > 0:
                    break
                session.press("a", duration=4)
                session.step(20)
            log(
                "battle CableClub_DoBattleOrTrade fired "
                f"count={counters['CableClub_DoBattleOrTrade'][0]} "
                f"{state_snapshot()} backend={backend_snapshot()}"
            )
            shot("04_battle_launch")

            # The party-patch counters can fire before the final ROM-owned
            # serial work has returned to the battle path. Wait for the later
            # VS-text hook, which is the first observed boundary shared by
            # both variants after that exchange. Do not send input after the
            # hook fires; the following transition is timed ROM work.
            intro_deadline = min(deadline, time.monotonic() + 300.0)
            last_prebattle_log = time.monotonic()
            while (
                time.monotonic() < intro_deadline
                and counters["DisplayLinkBattleVersusTextBox"][0] == 0
            ):
                session.press("a", duration=4)
                # Keep the real-ROM serial exchange moving at the same
                # throughput as the established battle driver. The owner
                # tick wrapper still services queued edges at every frame;
                # one-frame calls add enough Python scheduling overhead to
                # exhaust the bounded prebattle window before the VS hook.
                session.step(20)
                if time.monotonic() - last_prebattle_log > 15.0:
                    log(
                        "battle prebattle progress: "
                        f"counters={ {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS} } "
                        f"state={state_snapshot()} cpu={cpu_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                    last_prebattle_log = time.monotonic()
            log(
                "battle VS-text milestone reached; waiting for wire quiet "
                f"counters={ {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS} } "
                f"state={state_snapshot()} backend={backend_snapshot()}"
            )
            if counters["DisplayLinkBattleVersusTextBox"][0] == 0:
                raise RuntimeError(
                    "battle VS-text milestone did not complete: "
                    f"counters={counters} state={state_snapshot()} "
                    f"backend={backend_snapshot()}"
                )
            # A later edge can still be admitted immediately after the hook.
            # Let the owner continue for a bounded stable quiet window before
            # entering the no-tick ready/release barrier.
            link._network_backend.wait_for_wire_idle(
                timeout=120.0,
                progress_callback=lambda: session.step(1),
                stable_checks=4,
            )
            log("battle VS-text wire quiet; entering hold barrier")
            _hold_at_sync_boundary(
                link._network_backend,
                ready_sync_id=113,
                release_sync_id=114,
                timeout=120.0,
                service_pending_edges=lambda: link._network_backend.service_pending_edges(
                    max_edges=1
                ),
                progress_callback=lambda: session.step(1),
            )
            log("battle VS-text hold barrier complete; entering transition")

            # The VS splash and transition are timed ROM work. Do not mash A
            # through them: an input consumed in the transition can leave the
            # two independent ROMs in different battle menu states. Wait for
            # the ROM's own battle-menu hooks before selecting FIGHT.
            transition_deadline = min(deadline, time.monotonic() + 180.0)
            while time.monotonic() < transition_deadline:
                if counters["BattleTransition"][0] > 0:
                    break
                session.step(1)
            if counters["BattleTransition"][0] == 0:
                raise RuntimeError(
                    "battle transition did not start after VS-text barrier: "
                    f"counters={counters} state={state_snapshot()} "
                    f"cpu={cpu_snapshot()} backend={backend_snapshot()}"
                )
            _hold_at_sync_boundary(
                link._network_backend,
                ready_sync_id=115,
                release_sync_id=116,
                timeout=120.0,
                service_pending_edges=lambda: link._network_backend.service_pending_edges(
                    max_edges=1
                ),
                progress_callback=lambda: session.step(1),
            )
            log("battle transition hold barrier complete; entering menu")
            menu_deadline = min(deadline, time.monotonic() + 180.0)
            while time.monotonic() < menu_deadline:
                if counters["MainInBattleLoop"][0] > 0 and counters["DisplayBattleMenu"][0] > 0:
                    break
                session.step(1)
            if not (counters["MainInBattleLoop"][0] > 0 and counters["DisplayBattleMenu"][0] > 0):
                raise RuntimeError(
                    "battle menu did not open after intro: "
                    f"counters={counters} state={state_snapshot()} "
                    f"cpu={cpu_snapshot()} backend={backend_snapshot()}"
                )
            log(
                "battle menu hook reached; settling ROM menu fields "
                f"menu={menu_snapshot()} state={state_snapshot()} "
                f"backend={backend_snapshot()}"
            )
            # The DisplayBattleMenu hook fires before it installs
            # wMaxMenuItem/wMenuWatchedKeys. Wait for those ROM-owned fields
            # on this side before announcing the cross-process boundary.
            battle_menu_ready_deadline = min(deadline, time.monotonic() + 60.0)
            while time.monotonic() < battle_menu_ready_deadline:
                if (
                    counters["DisplayBattleMenu.leftColumn_WaitForInput"][0] > 0
                    or counters["DisplayBattleMenu.rightColumn_WaitForInput"][0] > 0
                ) and menu_fields_ready(
                    min_item=0,
                    max_item=1,
                    expected_max=1,
                    required_keys=0x01,
                ):
                    break
                session.step(2)
            if not (
                (
                    counters["DisplayBattleMenu.leftColumn_WaitForInput"][0] > 0
                    or counters["DisplayBattleMenu.rightColumn_WaitForInput"][0] > 0
                )
                and menu_fields_ready(
                    min_item=0,
                    max_item=1,
                    expected_max=1,
                    required_keys=0x01,
                )
            ):
                raise RuntimeError(
                    "battle menu fields did not become input-ready: "
                    f"counters={counters} menu={menu_snapshot()} "
                    f"state={state_snapshot()} cpu={cpu_snapshot()} "
                    f"backend={backend_snapshot()}"
                )
            log(
                "battle menu fields ready; entering sync "
                f"menu={menu_snapshot()} state={state_snapshot()}"
            )
            # Both ROMs now own a live battle menu. Rendezvous before either
            # side commits FIGHT so a subprocess cannot consume A while its
            # peer is still finishing DisplayTextBoxID/menu setup.
            _hold_at_sync_boundary(
                link._network_backend,
                ready_sync_id=12,
                release_sync_id=16,
                timeout=120.0,
                service_pending_edges=lambda: link._network_backend.service_pending_edges(
                    max_edges=1
                ),
            )
            # Match the in-process acceptance driver: after the rendezvous,
            # give both ROMs a short input-free window to finish entering
            # HandleMenuInput before sending the single ordinary A event.
            session.step(4)
            move_menu_to_item(
                target=0,
                min_item=0,
                max_item=1,
                label="battle command menu",
            )
            log(
                "battle menu ready; selecting FIGHT through ordinary input "
                f"state={state_snapshot()} cpu={cpu_snapshot()}"
            )
            move_menu_deadline = min(deadline, time.monotonic() + 120.0)
            next_fight_input_tick = -1
            fight_input_attempts = 0
            while time.monotonic() < move_menu_deadline and (
                counters["MoveSelectionMenu"][0] == 0
                or counters["MoveSelectionMenu.menuset"][0] == 0
                or not menu_fields_ready(min_item=1, max_item=4)
            ):
                # A peer can still be returning from the rendezvous while
                # this ROM is waiting in HandleMenuInput.  If the first
                # one-frame event was sampled before that wait became active,
                # retry it at a bounded frame interval, but only while the
                # ROM still describes the command menu.  This remains the
                # public directional/A path and stops as soon as the ROM's
                # move-menu hook proves that FIGHT was consumed.
                if (
                    menu_fields_ready(
                        min_item=0,
                        max_item=1,
                        expected_max=1,
                        required_keys=0x01,
                    )
                    and session.current_tick() >= next_fight_input_tick
                ):
                    session.press("a")
                    fight_input_attempts += 1
                    next_fight_input_tick = session.current_tick() + 8
                session.step(2)
            if (
                counters["MoveSelectionMenu"][0] == 0
                or counters["MoveSelectionMenu.menuset"][0] == 0
                or not menu_fields_ready(min_item=1, max_item=4)
            ):
                raise RuntimeError(
                    "move menu did not open after FIGHT: "
                    f"counters={counters} menu={menu_snapshot()} "
                    f"state={state_snapshot()} "
                    f"cpu={cpu_snapshot()} backend={backend_snapshot()} "
                    f"fight_input_attempts={fight_input_attempts}"
                )
            log(
                "battle move menu fields ready; entering sync "
                f"menu={menu_snapshot()} state={state_snapshot()}"
            )
            # The move menu is another ROM-owned input boundary. Match the
            # peers before reading its cursor or sending the legal move.
            _hold_at_sync_boundary(
                link._network_backend,
                ready_sync_id=13,
                release_sync_id=17,
                timeout=120.0,
                service_pending_edges=lambda: link._network_backend.service_pending_edges(
                    max_edges=1
                ),
            )
            session.step(4)
            selected_move_id = choose_first_usable_battle_move()
            # Give the ROM a bounded opportunity to consume the ordinary A
            # input and leave MoveSelectionMenu. Without this handoff one
            # subprocess can log the selection before its input is actually
            # processed, while the other begins LinkBattleExchangeData.
            log(
                "battle move selected through ROM menu; native exchange begins "
                f"move_id={selected_move_id} state={state_snapshot()} "
                f"cpu={cpu_snapshot()}"
            )

            last_battle_log = time.monotonic()
            while time.monotonic() < deadline:
                if counters["EndOfBattle"][0] > 0:
                    break
                battle_turn_complete = counters["LinkBattleExchangeData"][0] > 0 and (
                    counters["ExecutePlayerMove"][0] + counters["ExecuteEnemyMove"][0] > 0
                )
                if battle_turn_complete and not battle_turn_announced:
                    link._network_backend.announce_sync(sync_id=14)
                    battle_turn_announced = True
                    shot("05_battle_turn")
                    log(
                        "announced battle turn completion "
                        f"lbe={counters['LinkBattleExchangeData'][0]} "
                        f"execute_player={counters['ExecutePlayerMove'][0]} "
                        f"execute_enemy={counters['ExecuteEnemyMove'][0]} "
                        f"backend={backend_snapshot()}"
                    )
                if battle_turn_announced and link._network_backend.poll_peer_sync(sync_id=14):
                    peer_battle_turn_ready = True
                    break
                session.step(20)
                if time.monotonic() - last_battle_log > 15.0:
                    log(
                        "battle progress: "
                        f"counters={ {k: counters[k][0] for k in _TRADE_DIAG_SYMBOLS} } "
                        f"state={state_snapshot()} cpu={cpu_snapshot()} "
                        f"backend={backend_snapshot()}"
                    )
                    last_battle_log = time.monotonic()
            if battle_turn_announced:
                try:
                    cooperative_sync(sync_id=15, timeout=120.0)
                    log("sync: past battle turn barrier")
                    shot("06_battle_synced")
                except Exception as exc:  # noqa: BLE001
                    drive_status = "error"
                    drive_error = f"{type(exc).__name__}: {exc}"
                    log(f"battle turn sync raised {type(exc).__name__}: {exc}")
                post_deadline = min(deadline, time.monotonic() + 10.0)
                while time.monotonic() < post_deadline:
                    session.press("a", duration=4)
                    session.step(20)
                log("sync: post-battle shutdown drain")
                peer_shutdown_sync(ready_sync_id=21, release_sync_id=22)
                log("sync: post-battle shutdown barrier complete")
            if not peer_battle_turn_ready:
                log(
                    "peer battle turn completion not observed before deadline "
                    f"lbe={counters['LinkBattleExchangeData'][0]} "
                    f"execute_player={counters['ExecutePlayerMove'][0]} "
                    f"execute_enemy={counters['ExecuteEnemyMove'][0]} "
                    f"backend={backend_snapshot()}"
                )
    except Exception as exc:  # noqa: BLE001
        drive_status = "error"
        drive_error = f"{type(exc).__name__}: {exc}"
        log(f"EXCEPTION in drive loop: {type(exc).__name__}: {exc}")

    finally:
        if pre_link_menu_history is not None:
            close_pre_link_menu_observer()
        if trace is not None:
            trace.cleanup(deadline)
        # Detach the link while the emulator is still alive.  Stopping the
        # Session first leaves the native serial callback installed against a
        # closed NetworkBackend; a peer that is finishing its last transfer
        # can then spin on backend-closed errors during teardown.  The public
        # PyBoyLinkSession lifecycle restores the serial backend, disables
        # owner dispatch, and stops its transport in the required order.
        try:
            if link is not None:
                link.detach_all()
        except Exception as exc:  # noqa: BLE001
            if drive_status == "ok":
                drive_status = "error"
                drive_error = f"link cleanup {type(exc).__name__}: {exc}"
            log(f"link cleanup raised {type(exc).__name__}: {exc}")
        try:
            final_state = state_snapshot()
            final_cpu = cpu_snapshot()
            link_menu_state = link_menu_snapshot()
        except Exception as exc:  # noqa: BLE001
            log(f"final state snapshot raised {type(exc).__name__}: {exc}")
        try:
            session.close()
        except Exception as exc:  # noqa: BLE001
            if drive_status == "ok":
                drive_status = "error"
                drive_error = f"session cleanup {type(exc).__name__}: {exc}"
            log(f"session cleanup raised {type(exc).__name__}: {exc}")
        try:
            if link._network_backend is not None:
                link._network_backend.stop()
        except Exception as exc:  # noqa: BLE001
            if drive_status == "ok":
                drive_status = "error"
                drive_error = f"backend cleanup {type(exc).__name__}: {exc}"
            log(f"backend cleanup raised {type(exc).__name__}: {exc}")

    if drive_status == "ok":
        if args.goal == "link_menu":
            goal_complete = (
                link_menu_announced
                and peer_link_menu_ready
            )
        elif args.goal == "trade":
            goal_complete = counters["_AddEnemyMonToPlayerParty"][0] > 0
        else:
            required_battle_hooks = (
                "DisplayLinkBattleVersusTextBox",
                "MoveSelectionMenu",
                "LinkBattleExchangeData",
            )
            goal_complete = (
                battle_turn_announced
                and peer_battle_turn_ready
                and all(counters[name][0] > 0 for name in required_battle_hooks)
                and (counters["ExecutePlayerMove"][0] + counters["ExecuteEnemyMove"][0] > 0)
            )
        if not goal_complete:
            drive_status = "deadline"
            deadline_exceeded = True
            drive_error = f"{args.goal} did not complete before deadline"
            log(drive_error)

    emit_result()
    return 0 if drive_status == "ok" else 1


def main() -> int:
    trace = _PeerTraceWatchdog.from_env()
    try:
        if trace is not None:
            trace.start()
        return _run_peer(trace=trace)
    finally:
        if trace is not None:
            trace.close()


if __name__ == "__main__":
    sys.exit(main())
