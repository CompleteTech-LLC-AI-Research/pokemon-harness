"""Drive-loop core for the TCP trade peer.

``_PeerDrive`` owns the promoted outer-local frame of the pre-split ``_run_peer``,
its setup latch, the drive-loop error path, the teardown ``finally`` and the
result verdict (#127).  The phase bodies live on the mixins.
"""

from __future__ import annotations

import time
import traceback

from tests._battle_turn_evidence import BattleTurnObserver
from tests._tcp_trade_peer_drive_battle import _PeerDriveBattleMixin
from tests._tcp_trade_peer_drive_rendezvous import _PeerDriveRendezvousMixin
from tests._tcp_trade_peer_drive_setup import _PeerDriveSetupMixin
from tests._tcp_trade_peer_drive_support import _PeerDriveSupportMixin
from tests._tcp_trade_peer_drive_trade import _PeerDriveTradeMixin
from tests._tcp_trade_peer_link_menu import _TRADE_DIAG_SYMBOLS, _LinkMenuHistory
from tests._tcp_trade_peer_pre_link_menu import _PreLinkMenuHistory


class _PeerDrive(
    _PeerDriveSupportMixin,
    _PeerDriveSetupMixin,
    _PeerDriveRendezvousMixin,
    _PeerDriveTradeMixin,
    _PeerDriveBattleMixin,
):
    def __init__(self, args, trace) -> None:
        # The pre-split ``_run_peer`` frame, promoted to attributes (#127).
        self.args = args
        self.trace = trace
        # Establish the one process-wide cutoff before any ROM, TCP, or handshake
        # work. The existing gameplay code below continues to use this value.
        self.deadline = time.monotonic() + self.args.deadline_seconds
        self.drive_status = "error"
        self.drive_error: str | None = None
        self.deadline_exceeded = False
        self.session = None
        self.link = None
        self.native_internal_clock: bool | None = None
        self.peer_rom_version: str | None = None
        self.cable_club_confirmation: dict[str, int] | None = None
        self.connection_starter_latch: dict[str, object] | None = None
        self.party_before: dict[str, object] = {}
        self.party_after_trade: dict[str, object] | None = None
        self.final_state: dict[str, int] = {}
        self.final_cpu: dict[str, object] = {}
        self.link_menu_state: dict[str, object] = {}
        self.link_menu_history = _LinkMenuHistory(
            None, role=self.args.role, version=self.args.version
        )
        self.battle_observer: BattleTurnObserver | None = None
        self.pre_link_menu_history = (
            _PreLinkMenuHistory(enabled=True, role=self.args.role, version=self.args.version)
            if self.args.observe_pre_link_menu
            else None
        )
        self.counters = {s: [0] for s in _TRADE_DIAG_SYMBOLS}
        self.shots: list[str] = []
        self.select_mon_announced = False
        self.link_menu_announced = False
        self.peer_link_menu_ready = False
        self.link_menu_exchange_announced = False
        self.peer_link_menu_exchange_ready = False
        self.battle_turn_announced = False
        self.peer_battle_turn_ready = False
        self.link_menu_max = 0

    def _drive(self) -> None:
        try:
            self._rendezvous()
            if self.args.goal == "trade":
                self._trade()
            elif self.args.goal == "battle":
                self._battle()
        except Exception as exc:  # noqa: BLE001
            self.drive_status = "error"
            self.drive_error = f"{type(exc).__name__}: {exc}"
            self.log(f"EXCEPTION in drive loop: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        finally:
            if self.pre_link_menu_history is not None:
                self.close_pre_link_menu_observer()
            if self.trace is not None:
                self.trace.cleanup(self.deadline)
            # Detach the link while the emulator is still alive.  Stopping the
            # Session first leaves the native serial callback installed against a
            # closed NetworkBackend; a peer that is finishing its last transfer
            # can then spin on backend-closed errors during teardown.  The public
            # PyBoyLinkSession lifecycle restores the serial backend, disables
            # owner dispatch, and stops its transport in the required order.
            try:
                if self.link is not None:
                    self.link.detach_all()
            except Exception as exc:  # noqa: BLE001
                if self.drive_status == "ok":
                    self.drive_status = "error"
                    self.drive_error = f"link cleanup {type(exc).__name__}: {exc}"
                self.log(f"link cleanup raised {type(exc).__name__}: {exc}")
            try:
                self.final_state = self.state_snapshot()
                self.final_cpu = self.cpu_snapshot()
                self.link_menu_state = self.link_menu_snapshot()
            except Exception as exc:  # noqa: BLE001
                self.log(f"final state snapshot raised {type(exc).__name__}: {exc}")
            try:
                self.session.close()
            except Exception as exc:  # noqa: BLE001
                if self.drive_status == "ok":
                    self.drive_status = "error"
                    self.drive_error = f"session cleanup {type(exc).__name__}: {exc}"
                self.log(f"session cleanup raised {type(exc).__name__}: {exc}")
            try:
                if self.link._network_backend is not None:
                    self.link._network_backend.stop()
            except Exception as exc:  # noqa: BLE001
                if self.drive_status == "ok":
                    self.drive_status = "error"
                    self.drive_error = f"backend cleanup {type(exc).__name__}: {exc}"
                self.log(f"backend cleanup raised {type(exc).__name__}: {exc}")

    def _tail(self) -> int:
        if self.drive_status == "ok":
            if self.args.goal == "link_menu":
                goal_complete = (
                    self.link_menu_announced
                    and self.peer_link_menu_ready
                    and self.link_menu_exchange_announced
                    and self.peer_link_menu_exchange_ready
                    and self.counters["LinkMenu.doneChoosingMenuSelection"][0] > 0
                )
            elif self.args.goal == "trade":
                goal_complete = self.counters["_AddEnemyMonToPlayerParty"][0] > 0
            else:
                required_battle_hooks = (
                    "DisplayLinkBattleVersusTextBox",
                    "MoveSelectionMenu",
                    "LinkBattleExchangeData",
                )
                goal_complete = (
                    self.battle_turn_announced
                    and self.peer_battle_turn_ready
                    and all(self.counters[name][0] > 0 for name in required_battle_hooks)
                    and self.battle_observer is not None
                    and self.battle_observer.snapshot()["settled"] is True
                )
            if not goal_complete:
                self.drive_status = "deadline"
                self.deadline_exceeded = True
                self.drive_error = f"{self.args.goal} did not complete before deadline"
                self.log(self.drive_error)

        self.emit_result()
        return 0 if self.drive_status == "ok" else 1
