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

import queue
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
    ``session.step(chunk) + endpoint.serial_tick()`` in a loop. Main-
    thread callers drive gameplay by queueing button presses via
    :meth:`press`; presses are applied at the top of each chunk so
    step + press + serial-tick stay in the same Python thread (avoids
    a PyBoy/thread-safety rabbit hole)."""

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
        self._press_queue: queue.Queue[tuple[str, int]] = queue.Queue()
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

    def press(self, button: str, duration: int = 6) -> None:
        """Queue a button press. The runner thread applies it on its
        next loop iteration."""
        self._press_queue.put((button, duration))

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                # Drain pending presses before stepping.
                while True:
                    try:
                        button, duration = self._press_queue.get_nowait()
                    except queue.Empty:
                        break
                    self.session.press(button, duration=duration)
                self.session.step(self.chunk)
                self.endpoint.serial_tick()
        except Exception as exc:  # noqa: BLE001
            self.exc = exc


# --- hook-counter helper -------------------------------------------------


def _install_hook_counter(
    session: Session, symbol: str, bucket: list[int], slot: int
) -> None:
    """Install a PyBoy execution hook on ``symbol`` that bumps
    ``bucket[slot]`` every time the label is reached. Skips silently
    when the symbol isn't in this version's table so the same test
    can run cross-version."""
    if symbol not in session.symbols:
        return
    bank, addr = session.symbols.bank_addr(symbol)

    def _cb(_ctx: object) -> None:
        bucket[slot] += 1

    try:
        session._pyboy.hook_register(bank, addr, _cb, None)
    except ValueError:
        # Hook may already be registered by RemoteLinkEndpoint — that's
        # fine for our purposes.
        pass


def _wait_for(predicate, timeout: float, *, poll: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


# --- scaffold test --------------------------------------------------------


# Full listener × connector matrix. Red combinations auto-skip when
# the red cable_club.state fixture is absent (Mt. Moon → Cerulean is
# not yet scripted in the Red harness). Role matters: listener is
# internal-clock master, connector is external-clock slave — the
# reversed pair (e.g. yellow-listens-blue-connects) is a distinct
# wire configuration from its inverse.
@pytest.mark.parametrize(
    "version_a,version_b",
    [
        ("red", "red"),
        ("red", "blue"),
        ("blue", "red"),
        ("red", "yellow"),
        ("yellow", "red"),
        ("blue", "blue"),
        ("blue", "yellow"),
        ("yellow", "blue"),
        ("yellow", "yellow"),
    ],
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


# --- TCP-pair setup helper ----------------------------------------------


def _tcp_pair(
    session_a: Session,
    version_a: str,
    session_b: Session,
    version_b: str,
) -> tuple[TcpSerialLink, TcpSerialLink, RemoteLinkEndpoint, RemoteLinkEndpoint]:
    """Wire two sessions together over a localhost TCP SerialLink,
    install RemoteLinkEndpoints (listener=A, connector=B), and return
    everything so the caller can drive + tear down."""
    port = _free_port()
    link_a_holder: dict[str, TcpSerialLink] = {}

    def _listen() -> None:
        link_a_holder["link"] = TcpSerialLink.listen(port, version_a)

    listen_t = threading.Thread(target=_listen, daemon=True)
    listen_t.start()
    time.sleep(0.1)
    link_b = TcpSerialLink.connect("127.0.0.1", port, version_b)
    listen_t.join(timeout=3.0)
    link_a = link_a_holder["link"]
    endpoint_a = RemoteLinkEndpoint.as_listener(session_a, link_a)
    endpoint_b = RemoteLinkEndpoint.as_connector(session_b, link_b)
    endpoint_a.install()
    endpoint_b.install()
    return link_a, link_b, endpoint_a, endpoint_b


# --- end-to-end trade protocol over TCP ----------------------------------


# Fixture walkability notes — tests/fixtures/link/*/cable_club.state:
#
# - Blue fixture: player stands in front of the Cable Club attendant,
#   walkable, can press UP + A to engage dialog.
# - Yellow fixture: uses a weaker EnterMap hook-warp; player is planted
#   at the map edge and is NOT walkable. Confirmed in the single-process
#   integration test — see the "Yellow's fixture uses the weaker
#   EnterMap hook-warp" guard in test_link_integration.py.
# - Red fixture: not produced — Mt. Moon → Cerulean progression is not
#   yet scripted in the Red harness.
#
# The nybble-exchange test below requires BOTH sides to walk to the
# attendant, so only the blue↔blue pair runs today. Cross-version
# (blue↔yellow and yellow↔blue) and yellow↔yellow inherit yellow's
# walkability gap; they skip rather than silently pass-without-proving.
_FIXTURES_WITH_WALKABLE_PLAYER = {"blue"}


@pytest.mark.parametrize(
    "version_listen,version_connect",
    [
        ("red", "red"),
        ("red", "blue"),
        ("blue", "red"),
        ("red", "yellow"),
        ("yellow", "red"),
        ("blue", "blue"),
        ("blue", "yellow"),
        ("yellow", "blue"),
        ("yellow", "yellow"),
    ],
)
def test_remote_trade_reaches_link_menu_via_tcp(
    version_listen: str, version_connect: str
) -> None:
    """Full protocol drive over TCP up to the LinkMenu.

    Two sessions at the Cable Club attendant on opposite ends of a
    real localhost TCP SerialLink. Presses A/UP until both sides reach
    LinkMenu (the battle/trade/cancel picker). Reaching LinkMenu on
    *both* sides proves, in order:

    1. The handshake hook flipped the status byte on both sides.
    2. The attendant dialog advanced (SaveGameData fired).
    3. ``Serial_SyncAndExchangeNybble`` converged over TCP — this is
       the first real byte exchange in the trade/battle flow, and the
       game gates entry to the LinkMenu on the nybble sync completing.

    This is the two-process equivalent of the SaveGameData + LinkMenu
    milestones in
    :func:`test_link_integration.test_link_trade_roundtrip`. It stops
    at LinkMenu — past that, menu-selection is agent policy.

    Parametrized over the full listener × connector matrix; red and
    yellow rows skip until their fixtures become walkable.
    """
    if not (_roms_present(version_listen) and _roms_present(version_connect)):
        pytest.skip(
            f"ROMs not present for {version_listen}/{version_connect}"
        )
    state_listen = _cable_club_state(version_listen)
    state_connect = _cable_club_state(version_connect)
    if not (state_listen.exists() and state_connect.exists()):
        pytest.skip(
            f"Cable Club save states missing for "
            f"{version_listen}/{version_connect}"
        )
    if not (
        version_listen in _FIXTURES_WITH_WALKABLE_PLAYER
        and version_connect in _FIXTURES_WITH_WALKABLE_PLAYER
    ):
        pytest.skip(
            f"Fixture walkability gap: {version_listen}/{version_connect} "
            f"cannot walk to Cable Club attendant. See the "
            f"_FIXTURES_WITH_WALKABLE_PLAYER comment for details."
        )

    session_a = _open_session(version_listen)
    session_b = _open_session(version_connect)
    try:
        session_a.load_state(state_listen.read_bytes())
        session_b.load_state(state_connect.read_bytes())

        link_a, link_b, endpoint_a, endpoint_b = _tcp_pair(
            session_a, version_listen, session_b, version_connect
        )
        try:
            # Milestone hooks on both sides. Index 0 = listener/primary,
            # index 1 = connector/peer.
            save_game = [0, 0]
            link_menu = [0, 0]
            for idx, sess in enumerate((session_a, session_b)):
                _install_hook_counter(sess, "SaveGameData", save_game, idx)
                _install_hook_counter(sess, "LinkMenu", link_menu, idx)

            status_addr = session_a.symbols.addr_of("hSerialConnectionStatus")

            runner_a = _SessionRunner(session_a, endpoint_a)
            runner_b = _SessionRunner(session_b, endpoint_b)
            runner_a.start()
            runner_b.start()
            try:
                time.sleep(1.0)  # let map-script handshake settle
                for _ in range(3):
                    runner_a.press("up", duration=6)
                    runner_b.press("up", duration=6)
                    time.sleep(0.2)
                deadline = time.time() + 25.0
                while time.time() < deadline:
                    if link_menu[0] > 0 and link_menu[1] > 0:
                        break
                    runner_a.press("a", duration=4)
                    runner_b.press("a", duration=4)
                    time.sleep(0.15)
            finally:
                runner_a.stop()
                runner_b.stop()

            assert runner_a.exc is None, runner_a.exc
            assert runner_b.exc is None, runner_b.exc
            status_a = session_a._pyboy.memory[status_addr]
            status_b = session_b._pyboy.memory[status_addr]
            assert status_a == STATUS_INTERNAL, f"primary status=0x{status_a:02x}"
            assert status_b == STATUS_EXTERNAL, f"peer status=0x{status_b:02x}"
            assert save_game[0] > 0 and save_game[1] > 0, (
                f"SaveGameData never fired — attendant dialog stalled; "
                f"save_game={save_game}"
            )
            assert link_menu[0] > 0 and link_menu[1] > 0, (
                f"LinkMenu never reached — Serial_SyncAndExchangeNybble "
                f"did not converge over TCP "
                f"({version_listen}↔{version_connect}); link_menu={link_menu}"
            )
        finally:
            link_a.close()
            link_b.close()
    finally:
        session_a.close()
        session_b.close()


# --- observe Serial_ExchangeLinkMenuSelection + Serial_ExchangeBytes ---
#
# Driving the LinkMenu past its A-press with the remote endpoint turns
# out to expose a PyBoy 2.7 limitation: `hook_register` at an address
# where a hook already exists raises ValueError silently-eaten by our
# test helpers. Since RemoteLinkEndpoint.install() registers on the
# Serial_* labels first, a naive counter hook on e.g.
# Serial_ExchangeLinkMenuSelection never lands and the signal looks
# like "0 calls" even when the RPC is flowing.
#
# Rather than fight that (the endpoint's single-hook-per-address
# registration is the right design), the test below observes the RPC
# layer directly by wrapping `link.exchange`. That way we see exactly
# which symbolic ``kind`` values crossed the TCP boundary — which is
# the end-to-end transport proof we actually care about.


def test_remote_rpc_kinds_flow_over_tcp_reaching_link_menu_blue() -> None:
    """Observe every ``link.exchange`` RPC that flows during the
    blue↔blue Cable Club drive to LinkMenu.

    The nybble test proves the game reaches LinkMenu. This test is
    about the transport-layer observation: which symbolic ``kind``
    values the game actually asks the endpoint to RPC over TCP, in
    what order, and whether the exchange returns matching bytes.

    Expected kinds during the drive:
    - ``exchange_nybble/wSerialExchangeNybbleSendData`` (from
      Serial_SyncAndExchangeNybble, called many times during the
      attendant-dialog handshake and to gate LinkMenu entry)

    This is the last two-process gap I can close without agent policy
    for menu navigation. Post-LinkMenu exchanges
    (Serial_ExchangeLinkMenuSelection, Serial_ExchangeBytes) require
    coordinated A-press timing on both sides; the existing in-process
    LinkPair trade_roundtrip test covers that game-code flow, and the
    Phase 1 serial-link tests + Phase 2 symbol-translation tests cover
    the transport and kind-encoding separately.
    """
    if not _roms_present("blue"):
        pytest.skip("Blue ROM not present")
    state = _cable_club_state("blue")
    if not state.exists():
        pytest.skip(f"Blue Cable Club state missing: {state}")

    session_a = _open_session("blue")
    session_b = _open_session("blue")
    try:
        session_a.load_state(state.read_bytes())
        session_b.load_state(state.read_bytes())

        link_a, link_b, endpoint_a, endpoint_b = _tcp_pair(
            session_a, "blue", session_b, "blue"
        )
        try:
            # Wrap the link.exchange methods to record every RPC kind
            # that crosses the TCP boundary. Stays correct under the
            # runner's background-thread pressure (queue.Queue-backed
            # SerialLink doesn't mind the wrapper).
            kinds_a: list[str] = []
            kinds_b: list[str] = []
            orig_ex_a = link_a.exchange
            orig_ex_b = link_b.exchange

            def _wrap(sink: list[str], inner):
                def exchange(kind, my_bytes, *, timeout_ms=5000):
                    sink.append(kind)
                    return inner(kind, my_bytes, timeout_ms=timeout_ms)

                return exchange

            link_a.exchange = _wrap(kinds_a, orig_ex_a)  # type: ignore[method-assign]
            link_b.exchange = _wrap(kinds_b, orig_ex_b)  # type: ignore[method-assign]

            runner_a = _SessionRunner(session_a, endpoint_a)
            runner_b = _SessionRunner(session_b, endpoint_b)
            runner_a.start()
            runner_b.start()
            try:
                time.sleep(1.0)
                for _ in range(3):
                    runner_a.press("up", duration=6)
                    runner_b.press("up", duration=6)
                    time.sleep(0.2)
                # Drive until the nybble RPC has fired at least a
                # handful of times on each side — generous envelope.
                deadline = time.time() + 25.0
                while time.time() < deadline:
                    if len(kinds_a) >= 3 and len(kinds_b) >= 3:
                        break
                    runner_a.press("a", duration=4)
                    runner_b.press("a", duration=4)
                    time.sleep(0.15)
            finally:
                runner_a.stop()
                runner_b.stop()

            assert runner_a.exc is None, runner_a.exc
            assert runner_b.exc is None, runner_b.exc
            nybble_kind = (
                "exchange_nybble/wSerialExchangeNybbleSendData"
            )
            assert nybble_kind in kinds_a, (
                f"primary never issued nybble RPC; kinds_a={kinds_a}"
            )
            assert nybble_kind in kinds_b, (
                f"peer never issued nybble RPC; kinds_b={kinds_b}"
            )
            # And the counts must be roughly balanced — if they diverge
            # by a huge margin, one side is over/under-exchanging and
            # the peer would drift.
            count_a = kinds_a.count(nybble_kind)
            count_b = kinds_b.count(nybble_kind)
            assert abs(count_a - count_b) <= max(count_a, count_b), (
                f"nybble RPC count wildly unbalanced: "
                f"count_a={count_a}, count_b={count_b}"
            )
        finally:
            link_a.close()
            link_b.close()
    finally:
        session_a.close()
        session_b.close()
