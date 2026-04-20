"""End-to-end integration tests for the TWO-PROCESS remote link cable.

The single-process equivalent is :mod:`test_link_integration` — it pairs
two :class:`Session` objects with an in-process :class:`LinkPair`. That
exercise proved the bridge logic + game-logic sequencing end-to-end on
real ROMs through the full trade handshake.

This module's job is narrower: prove that when the same two sessions
are wired through a :class:`TcpSerialLink` + :class:`RemoteLinkEndpoint`
pair (as they would be in production with two separate MCP servers),
the handshake and the hardware-serial-tick still work. We do NOT
re-exercise the trade UI flow; that would just be testing the game
logic a second time.

All tests are skipped unless the real ROMs + ``cable_club.state``
fixtures are present.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from pokered_harness.link.remote import (
    STATUS_EXTERNAL,
    STATUS_INTERNAL,
    RemoteLinkEndpoint,
)
from pokered_harness.link.serial_link import TcpSerialLink
from pokered_harness.session import Session


REPO_ROOT = Path(__file__).resolve().parents[1]
for _parent in [REPO_ROOT, *REPO_ROOT.parents]:
    if (_parent / "rom").is_dir():
        ROM_ROOT = _parent / "rom"
        break
else:  # pragma: no cover
    ROM_ROOT = REPO_ROOT / "rom"

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "link"


ROM_PATHS = {
    "blue": (
        ROM_ROOT / "blue" / "pokemon-blue-color.gb",
        ROM_ROOT / "blue" / "pokemon-blue.sym",
    ),
    "yellow": (
        ROM_ROOT / "yellow" / "pokemon-yellow.gbc",
        ROM_ROOT / "yellow" / "pokemon-yellow.sym",
    ),
    "red": (
        ROM_ROOT / "red" / "pokemon-red.gb",
        ROM_ROOT / "red" / "pokemon-red.sym",
    ),
}


def _roms_present(version: str) -> bool:
    rom, sym = ROM_PATHS[version]
    return rom.exists() and sym.exists()


def _open_session(version: str) -> Session:
    rom, sym = ROM_PATHS[version]
    return Session.from_files(rom, sym)


def _cable_club_state(version: str) -> Path:
    return FIXTURE_ROOT / version / "cable_club.state"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# --- threaded session driver ---------------------------------------------


class _SessionRunner:
    """Background-thread driver for a remote-endpoint-owning session.

    Each MCP process in production owns one session + one endpoint and
    steps its emulator on its own cadence. The remote tests simulate
    that by running each session in a daemon thread: the thread pumps
    ``session.step(chunk) + endpoint.serial_tick()`` in a loop, and the
    main thread coordinates button presses + assertions."""

    def __init__(
        self,
        session: Session,
        endpoint: RemoteLinkEndpoint,
        *,
        chunk: int = 4,
    ) -> None:
        self.session = session
        self.endpoint = endpoint
        self.chunk = chunk
        self._stop = threading.Event()
        self.exc: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"runner-{endpoint.clock_status_byte:#x}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.session.step(self.chunk)
                self.endpoint.serial_tick()
        except Exception as exc:  # noqa: BLE001
            self.exc = exc


# --- scaffold test --------------------------------------------------------


@pytest.mark.parametrize(
    "version_a,version_b",
    [("blue", "blue"), ("yellow", "yellow"), ("blue", "yellow")],
)
def test_remote_handshake_writes_status_on_both_sides(
    version_a: str, version_b: str
) -> None:
    """Two-process equivalent of the single-process handshake check.

    On the Cerulean Pokemon Center map the game's overworld script calls
    ``Serial_TryEstablishingExternallyClockedConnection`` every frame
    (pret/pokered scripts/CeruleanPokecenter.asm). The bridge's
    handshake hook writes a role-specific byte to
    ``hSerialConnectionStatus`` — 0x02 on the listener (internal clock)
    and 0x01 on the connector (external clock).

    In the *remote* model each endpoint independently sets its own
    local status byte — no peer exchange is required for the handshake
    itself. Successfully running both sessions in parallel over a TCP
    link and observing both status bytes flip away from the default
    0xFF proves: sessions load + step + endpoint install works end-to-
    end against real ROM code.
    """
    if not (_roms_present(version_a) and _roms_present(version_b)):
        pytest.skip(f"ROMs not present for {version_a}/{version_b}")
    state_a = _cable_club_state(version_a)
    state_b = _cable_club_state(version_b)
    if not (state_a.exists() and state_b.exists()):
        pytest.skip(
            f"Cable Club save states missing; see README for how to "
            f"produce {state_a} and {state_b}"
        )

    session_a = _open_session(version_a)
    session_b = _open_session(version_b)
    try:
        session_a.load_state(state_a.read_bytes())
        session_b.load_state(state_b.read_bytes())

        port = _free_port()

        # Listener is our "primary" for this test; connector attaches.
        link_a_holder: dict[str, TcpSerialLink] = {}

        def _listen() -> None:
            link_a_holder["link"] = TcpSerialLink.listen(port, version_a)

        listen_t = threading.Thread(target=_listen, daemon=True)
        listen_t.start()

        # Wait for listener to bind before connecting.
        time.sleep(0.1)
        link_b = TcpSerialLink.connect("127.0.0.1", port, version_b)
        listen_t.join(timeout=3.0)
        link_a = link_a_holder["link"]

        try:
            # Each endpoint picks up its role from listen/connect
            # (listener=internal, connector=external).
            endpoint_a = RemoteLinkEndpoint.as_listener(session_a, link_a)
            endpoint_b = RemoteLinkEndpoint.as_connector(session_b, link_b)
            endpoint_a.install()
            endpoint_b.install()

            status_addr = session_a.symbols.addr_of("hSerialConnectionStatus")

            runner_a = _SessionRunner(session_a, endpoint_a)
            runner_b = _SessionRunner(session_b, endpoint_b)
            runner_a.start()
            runner_b.start()
            try:
                # Poll for both handshake bytes to flip away from
                # "not established" (0xFF). 6 seconds is generous —
                # the map script fires the hook every frame.
                deadline = time.time() + 6.0
                while time.time() < deadline:
                    status_a = session_a._pyboy.memory[status_addr]
                    status_b = session_b._pyboy.memory[status_addr]
                    if status_a != 0xFF and status_b != 0xFF:
                        break
                    time.sleep(0.05)
            finally:
                runner_a.stop()
                runner_b.stop()

            assert runner_a.exc is None, runner_a.exc
            assert runner_b.exc is None, runner_b.exc

            status_a = session_a._pyboy.memory[status_addr]
            status_b = session_b._pyboy.memory[status_addr]
            assert status_a == STATUS_INTERNAL, (
                f"listener side (primary) expected clock-role byte "
                f"0x{STATUS_INTERNAL:02x}, got 0x{status_a:02x}"
            )
            assert status_b == STATUS_EXTERNAL, (
                f"connector side (peer) expected clock-role byte "
                f"0x{STATUS_EXTERNAL:02x}, got 0x{status_b:02x}"
            )
        finally:
            link_a.close()
            link_b.close()
    finally:
        session_a.close()
        session_b.close()
