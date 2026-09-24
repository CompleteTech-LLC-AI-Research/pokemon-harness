"""Peer setup and ROM/fixture validation for the TCP trade peer.

The pre-split ``_run_peer.setup`` closure and the inline setup latch (#127)
become ``setup`` and ``_run_setup`` on this mixin.
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

from tests._battle_turn_evidence import BattleTurnObserver, install_continuation_hooks
from tests._tcp_trade_peer_link_menu import (
    _install_cable_club_confirmation_latch,
    _install_connection_starter_latch,
)
from tests._tcp_trade_peer_sync import _party_summary, _validate_battle_party_fixture


def _resolve_pre_link_menu(*, enabled, role, version, rom_bytes, symbol_bytes):
    """Resolve the pre-LinkMenu observation profile for one side of the link.

    Thin forwarder to :func:`tests._tcp_trade_peer._resolve_pre_link_menu`.
    The facade owns that function together with the pinned
    ``_PRE_LINK_MENU_PROFILES`` table, so the lookup is performed here at call
    time -- exactly the module-global lookup the pre-split ``setup`` closure
    performed, and the reason a monkeypatch of the facade's table still
    reaches the resolver's read site.
    """
    from tests import _tcp_trade_peer as facade

    return facade._resolve_pre_link_menu(
        enabled=enabled,
        role=role,
        version=version,
        rom_bytes=rom_bytes,
        symbol_bytes=symbol_bytes,
    )


class _PeerDriveSetupMixin:
    def setup(self) -> None:

        if not math.isfinite(self.args.deadline_seconds) or self.args.deadline_seconds <= 0:
            raise ValueError("deadline-seconds must be finite and positive")
        if not 0 <= self.args.serial_transcript_entries <= 4096:
            raise ValueError("serial-transcript-entries must be 0 or between 1 and 4096")
        self.remaining("setup")
        sys.path.insert(0, str(self.args.repo_root / "src"))

        from pokered_harness.config import load_versions
        from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
        from pokered_harness.session import Session

        self.remaining("dependency setup")
        rom_root = Path(os.environ.get("POKERED_ROM_ROOT", self.args.repo_root / "rom"))
        fixture_root = Path(
            os.environ.get(
                "POKERED_FIXTURE_ROOT", self.args.repo_root / "tests" / "fixtures" / "link"
            )
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
        rom, sym, fixture_version, trade_fixture_name, battle_fixture_name = rom_paths[
            self.args.version
        ]
        if self.pre_link_menu_history is not None:
            try:
                observer_rom_bytes = rom.read_bytes()
                observer_symbol_bytes = sym.read_bytes()
            except Exception as exc:  # noqa: BLE001 - Optional diagnostics must not fail gameplay.
                self.pre_link_menu_history.reason = f"asset_read_error:{type(exc).__name__}"[:128]
            else:
                self.pre_link_menu_history = _resolve_pre_link_menu(
                    enabled=True,
                    role=self.args.role,
                    version=self.args.version,
                    rom_bytes=observer_rom_bytes,
                    symbol_bytes=observer_symbol_bytes,
                )
        # The color Red/Blue Cable Club menu has three choices (0..2), while
        # Yellow adds a fourth (0..3). Keep the readiness predicate ROM-aware;
        # a hard-coded bound can otherwise leave a valid peer spinning until the
        # outer gameplay deadline without ever announcing the phase.
        self.link_menu_max = 3 if fixture_version == "yellow" else 2
        fixture_name = battle_fixture_name if self.args.goal == "battle" else trade_fixture_name
        state = fixture_root / fixture_version / fixture_name

        if self.args.record_dir is not None:
            self.remaining("record directory setup")
            self.args.record_dir.mkdir(parents=True, exist_ok=True)
            self.remaining("record directory setup")

        self.log(f"loading {self.args.version} ROM + state")
        self.remaining("version pin load")
        pins = load_versions(self.args.repo_root / "VERSIONS.md")
        expected_sha = pins.sha1_for_path(rom)
        if expected_sha is None:
            raise RuntimeError(f"no VERSIONS.md SHA-1 pin for {rom}")
        self.remaining("ROM setup")
        self.session = Session.from_files(
            rom,
            sym,
            expected_rom_sha1=expected_sha,
            expected_pyboy_version=pins.pyboy_version,
        )
        self.remaining("ROM setup")
        self.session.load_state(state.read_bytes())
        self.remaining("state setup")
        self.party_before = _party_summary(self.session)
        if self.args.goal == "battle":
            self.log(f"battle fixture validated: {_validate_battle_party_fixture(self.session)}")
        self.shot("00_loaded")
        self.log("state loaded, installing hooks")

        self.remaining("hook setup")
        self.link_menu_history.session = self.session
        if self.args.goal == "battle":
            self.battle_observer = BattleTurnObserver(
                self.session,
                role=self.args.role,
                version=self.args.version,
                before_party=self.party_before,
            )
        if self.pre_link_menu_history is None:
            self.link_menu_history.install(self.counters, battle_observer=self.battle_observer)
        else:
            observers_by_address = {}
            try:
                self.pre_link_menu_history.install(self.session)
                if self.pre_link_menu_history.reason == "external_pending":
                    for event, bank, address in self.pre_link_menu_history.config.sites:
                        if event == "Serial_SyncAndExchangeNybble":
                            observers_by_address[(bank, address)] = self.pre_link_menu_history
            except BaseException as exc:  # noqa: BLE001 - Optional diagnostics cannot fail setup.
                self.close_pre_link_menu_observer()
                self.pre_link_menu_history.reason = (
                    "observer_install_error:" + type(exc).__name__[:128]
                )
                self.pre_link_menu_history._error("install", "setup", exc)
            self.link_menu_history.install(
                self.counters,
                observers_by_address=observers_by_address,
                battle_observer=self.battle_observer,
            )
        if self.battle_observer is not None:
            install_continuation_hooks(
                self.session, self.battle_observer, version=self.args.version
            )
        self.cable_club_confirmation = _install_cable_club_confirmation_latch(self.session)

        self.log(f"establishing TCP {self.args.role}")
        if self.args.role == "listen":
            self.link = PyBoyLinkSession.listen(
                self.args.port,
                host=self.args.host,
                local_rom_version=fixture_version,
                accept_timeout_s=min(10.0, self.remaining("TCP listen")),
            )
        else:
            last_exc: Exception | None = None
            for attempt in range(60):
                self.remaining("TCP connect")
                try:
                    self.link = PyBoyLinkSession.connect(
                        self.args.host,
                        self.args.port,
                        local_rom_version=fixture_version,
                        timeout_s=min(10.0, self.remaining("TCP connect")),
                    )
                    break
                except OSError as exc:
                    last_exc = exc
                    if attempt == 59:
                        raise
                    time.sleep(min(0.25, self.remaining("TCP connect retry")))
            else:
                raise RuntimeError(f"could not connect to listener: {last_exc}")
        self.remaining("TCP setup")
        backend = getattr(self.link, "_network_backend", None)
        if backend is None:
            raise RuntimeError("network backend missing before attach")
        if self.args.serial_transcript_entries:
            # ``attach`` starts the receiver and performs the versioned HELLO
            # negotiation. Enable before it so the bounded transcript covers
            # every possible serial edge after a connected backend exists.
            backend.enable_serial_transcript(max_entries=self.args.serial_transcript_entries)
            self.log(
                "serial transcript enabled: "
                f"{self.args.serial_transcript_entries} bounded local records"
            )
        self.log("TCP established, attaching PyBoy")
        self.link.attach(self.session._pyboy)
        self.remaining("PyBoy attach")
        peer_version = backend.wait_for_hello(timeout=min(30.0, self.remaining("HELLO handshake")))
        self.peer_rom_version = peer_version
        self.remaining("HELLO handshake")
        selected_internal = self.link.negotiate_network_clock_role(peer_version)
        self.native_internal_clock = bool(selected_internal)
        self.remaining("network clock negotiation")
        self.connection_starter_latch = _install_connection_starter_latch(
            self.session,
            backend,
            enabled=self.native_internal_clock,
        )
        self.log(
            f"versioned handshake complete: local={fixture_version} "
            f"peer={peer_version} native_internal_clock={selected_internal}"
        )
        self.log("attached; starting drive loop")

    def _run_setup(self) -> int | None:
        setup_complete = False
        try:
            self.setup()
            setup_complete = True
        except BaseException as exc:  # noqa: BLE001
            timed_out = isinstance(exc, TimeoutError)
            if not timed_out:
                try:
                    timed_out = (
                        math.isfinite(self.deadline)
                        and self.args.deadline_seconds > 0
                        and time.monotonic() >= self.deadline
                    )
                except (TypeError, ValueError):
                    timed_out = False
            self.deadline_exceeded = timed_out
            self.drive_status = "deadline" if timed_out else "error"
            self.drive_error = self.setup_error_text(exc)
            self.log(f"EXCEPTION in setup: {self.drive_error}")
        finally:
            if not setup_complete:
                if self.pre_link_menu_history is not None:
                    self.close_pre_link_menu_observer()
                if self.trace is not None:
                    self.trace.cleanup(self.deadline)
                partial_backend = (
                    getattr(self.link, "_network_backend", None) if self.link is not None else None
                )
                try:
                    if self.link is not None:
                        self.link.detach_all()
                except BaseException as exc:  # noqa: BLE001
                    self.log(f"link cleanup raised {type(exc).__name__}: {exc}")
                try:
                    if self.session is not None:
                        self.session.close()
                except BaseException as exc:  # noqa: BLE001
                    self.log(f"session cleanup raised {type(exc).__name__}: {exc}")
                try:
                    if partial_backend is not None:
                        partial_backend.stop()
                except BaseException as exc:  # noqa: BLE001
                    self.log(f"backend cleanup raised {type(exc).__name__}: {exc}")
                self.emit_result(setup_failed=True)
        if not setup_complete:
            return 1
        if self.cable_club_confirmation is None:
            raise RuntimeError("Cable Club confirmation latch was not installed")

        self.drive_status = "ok"
