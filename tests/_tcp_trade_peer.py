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

# ruff: noqa: F401

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
import traceback
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from threading import get_ident
from typing import NamedTuple

from tests._battle_turn_evidence import (
    EVIDENCE_EVENTS,
    BattleTurnObserver,
    choose_supported_battle_move,
    install_continuation_hooks,
)
from tests._tcp_trade_peer_drive import (
    _PeerDrive,
)
from tests._tcp_trade_peer_link_menu import (
    _LINK_MENU_HISTORY_EVENTS,
    _TRADE_DIAG_SYMBOLS,
    _install_cable_club_confirmation_latch,
    _install_connection_starter_latch,
    _is_cable_club_save_choice_ready,
    _link_menu_has_real_selection_exchange,
    _link_menu_post_call_address,
    _link_menu_receive_candidate,
    _LinkMenuHistory,
    _read_link_menu_fields,
)
from tests._tcp_trade_peer_pre_link_menu import (
    _PRE_LINK_MENU_ERROR_LIMIT,
    _PRE_LINK_MENU_FIELDS,
    _PRE_LINK_MENU_HISTORY_LIMIT,
    _PRE_LINK_MENU_REPORT_FIELDS,
    _PRE_LINK_MENU_SCHEMA_VERSION,
    _PreLinkMenuConfig,
    _PreLinkMenuHistory,
)
from tests._tcp_trade_peer_sync import (
    PARTY_MON_SIZE,
    PARTY_NICK_SIZE,
    PARTY_OT_SIZE,
    _deadline_remaining,
    _finish_link_menu_phase,
    _hold_at_sync_boundary,
    _party_summary,
    _peer_frame_shutdown_sync,
    _peer_shutdown_sync,
    _validate_battle_party_fixture,
)
from tests._tcp_trade_peer_trace import (
    _PeerTraceWatchdog,
    _trace_warning,
)

# Local pinned ROM/SYM bytes, interpreted against pret's
# engine/link/cable_club_npc.asm and home/serial.asm. CALL return sites are
# instruction boundaries, not proof of the subsequent conditional outcome.
_PRE_LINK_MENU_PROFILES = {
    ("listen", "blue_color"): (
        "5f4b05725a860e04077045462176d3e2771c5022",
        "c779a0628cfc97cc9ac9db2520a1e23a2d8b7ed6",
        0x71C5,
        0x7263,
        0x227F,
        0x223F,
        0x72A8,
        1,
        0x5C0A,
        0x72D7,
    ),
    ("connect", "yellow"): (
        "cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1",
        "7c4205723943e7722230dcf014e5e8a2012474aa",
        0x7035,
        0x70D8,
        0x20DB,
        0x209B,
        0x711D,
        0x3D,
        0x580C,
        0x71AC,
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
    observed = (
        ("rom_sha1", hashlib.sha1(rom_bytes).hexdigest()),
        ("symbol_sha1", hashlib.sha1(symbol_bytes).hexdigest()),
    )
    history = _PreLinkMenuHistory(
        enabled=True,
        role=role,
        version=version,
        pins=pins,
        observed_pins=observed,
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
        (
            "Serial_SyncAndExchangeNybble.timeoutJump",
            0,
            sync + 0x1C,
            bytes((0xAF, 0xC3, timeout & 0xFF, timeout >> 8)),
        ),
        ("Serial_SyncAndExchangeNybble.return", 0, sync + 0x43, b"\xc9"),
        ("Serial_SyncAndExchangeNybble.publish60", 0, sync + 0x4A, bytes.fromhex("c660")),
        ("Serial_SyncAndExchangeNybble.receiveCompare", 0, sync + 0x5F, bytes.fromhex("fe60c0")),
        ("CableClubNPC.inactivityCloseCall", 1, call + 25, bytes((0xCD, close & 0xFF, close >> 8))),
        (
            "CableClubNPC.inactivityCloseReturn",
            1,
            call + 28,
            bytes((0x21, (close - 15) & 0xFF, (close - 15) >> 8)),
        ),
        ("CableClubNPC.choseNoCloseCall", 1, call + 44, bytes((0xCD, close & 0xFF, close >> 8))),
        (
            "CableClubNPC.choseNoCloseReturn",
            1,
            call + 47,
            bytes((0x21, (close - 10) & 0xFF, (close - 10) >> 8)),
        ),
        ("SetUnknownCounterToFFFF", 0, timeout, bytes.fromhex("3dea47ccea48ccc9")),
        (
            "LinkMenu",
            menu_bank,
            menu,
            bytes.fromhex("afea58" if version == "blue_color" else "afea57"),
        ),
    )
    for event, bank, address, signature in specs:
        valid = (bank == 0 and 0 <= address < 0x4000) or (bank > 0 and 0x4000 <= address < 0x8000)
        bank_end = 0x4000 if bank == 0 else 0x8000
        if not valid or address + len(signature) > bank_end:
            history.reason = "invalid_site:" + event
            return history
        offset = address if bank == 0 else bank * 0x4000 + address - 0x4000
        if rom_bytes[offset : offset + len(signature)] != signature:
            history.reason = "signature_mismatch:" + event
            return history
    if any(
        call + offset + 2 + displacement != connected
        for offset, displacement in ((8, 0x3B), (12, 0x37))
    ):
        history.reason = "branch_target_mismatch"
        return history
    return _PreLinkMenuHistory(
        enabled=True,
        role=role,
        version=version,
        pins=pins,
        observed_pins=observed,
        sites=tuple((event, bank, address) for event, bank, address, _ in specs),
    )


def _run_peer(trace=None) -> int:
    """Drive one side of the link and return the process verdict."""
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
    ap.add_argument(
        "--serial-transcript-entries",
        type=int,
        default=0,
        help=(
            "Opt in to a bounded local NetworkBackend serial transcript "
            "(1..4096 records; 0 disables it)."
        ),
    )
    ap.add_argument(
        "--reset-serial-transcript-before-link-menu",
        action="store_true",
        default=False,
        help=(
            "Diagnostic-only: reset the enabled bounded serial transcript "
            "after sync 19 and immediately before the ordinary LinkMenu A press "
            "(requires --serial-transcript-entries)."
        ),
    )
    ap.add_argument("--label", default="")
    ap.add_argument("--repo-root", type=Path, required=True)
    args = ap.parse_args()

    if args.reset_serial_transcript_before_link_menu and not args.serial_transcript_entries:
        ap.error("--reset-serial-transcript-before-link-menu requires --serial-transcript-entries")

    drive = _PeerDrive(args, trace)
    failure = drive._run_setup()
    if failure is not None:
        return failure

    drive._drive()
    return drive._tail()


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
