"""Shared plumbing for the two-process remote link integration tests.

Split from ``tests/test_link_integration_remote.py`` for #137 with no
behavior change. Holds the ROM/fixture probing, session opening, TCP
pair wiring, remote-runner supervision and link-menu driving helpers
shared by the split test modules.
"""
from __future__ import annotations

import queue
import socket
import threading
import time
from pathlib import Path

import pytest

from pokered_harness.config import load_versions
from pokered_harness.link.remote import RemoteLinkEndpoint
from pokered_harness.link.serial_link import TcpSerialLink
from pokered_harness.session import Session
from tests._rom_assets import fixture_path, rom_path, sym_path

ROM_PATHS = {
    "blue": (
        rom_path("blue", color=True),
        sym_path("blue"),
    ),
    "yellow": (
        rom_path("yellow"),
        sym_path("yellow"),
    ),
    "red": (
        # Match the Red fixture produced by
        # scripts/produce_cable_club_fixture.py (color ROM, SHA e1deed6308…).
        rom_path("red", color=True),
        sym_path("red"),
    ),
}


def _roms_present(version: str) -> bool:
    rom, sym = ROM_PATHS[version]
    return rom.exists() and sym.exists()


def _open_session(version: str) -> Session:
    rom, sym = ROM_PATHS[version]
    pins = load_versions("VERSIONS.md")
    expected_sha = pins.sha1_for_path(rom)
    assert expected_sha is not None
    return Session.from_files(
        rom,
        sym,
        expected_rom_sha1=expected_sha,
        expected_pyboy_version=pins.pyboy_version,
    )


def _cable_club_state(version: str) -> Path:
    return fixture_path(version)


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
    ``endpoint.step(chunk)`` in a loop. The endpoint expands the chunk
    into frame-sized session steps and services serial hardware after
    every frame. Main-thread callers drive gameplay by queueing button presses via
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
        self._press_queue: queue.Queue[tuple[str, int, threading.Event]] = queue.Queue()
        self._progress = threading.Condition()
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

    def press(self, button: str, duration: int = 6) -> threading.Event:
        """Queue a button press. The runner thread applies it on its
        next loop iteration. The returned event is set after the press is
        applied, which lets a caller avoid building an unbounded input
        backlog while PyBoy is ticking."""
        applied = threading.Event()
        self._press_queue.put((button, duration, applied))
        return applied

    def wait_until_tick(self, target: int, timeout: float) -> None:
        """Wait for deterministic emulator-frame progress.

        The condition is notified after every completed step, so callers do
        not need wall-clock polling sleeps. A runner exception or a missed
        deadline is surfaced immediately and remains bounded.
        """
        if target < 0:
            raise ValueError(f"target must be non-negative, got {target}")
        deadline = time.monotonic() + timeout
        with self._progress:
            while self.session.current_tick() < target:
                if self.exc is not None:
                    raise self.exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"{self._thread.name} reached tick "
                        f"{self.session.current_tick()} before target {target}"
                    )
                self._progress.wait(timeout=remaining)
            if self.exc is not None:
                raise self.exc

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                # Drain pending presses before stepping.
                while True:
                    try:
                        button, duration, applied = self._press_queue.get_nowait()
                    except queue.Empty:
                        break
                    self.session.press(button, duration=duration)
                    applied.set()
                self.endpoint.step(self.chunk)
                with self._progress:
                    self._progress.notify_all()
        except Exception as exc:  # noqa: BLE001
            self.exc = exc
            with self._progress:
                self._progress.notify_all()


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
    listen_error: list[BaseException] = []
    listener_ready = threading.Event()

    def _listen() -> None:
        try:
            link_a_holder["link"] = TcpSerialLink.listen(
                port,
                version_a,
                ready_event=listener_ready,
                accept_timeout_s=10.0,
            )
        except BaseException as exc:  # noqa: BLE001
            listen_error.append(exc)
            listener_ready.set()

    listen_t = threading.Thread(target=_listen, daemon=True)
    listen_t.start()
    assert listener_ready.wait(timeout=10.0), "TCP listener did not become ready"
    if listen_error:
        raise listen_error[0]
    link_b = TcpSerialLink.connect("127.0.0.1", port, version_b)
    listen_t.join(timeout=3.0)
    assert not listen_t.is_alive(), "TCP listener thread did not finish"
    if listen_error:
        link_b.close()
        raise listen_error[0]
    assert "link" in link_a_holder, "TCP listener returned without a link"
    link_a = link_a_holder["link"]
    endpoint_a = RemoteLinkEndpoint.as_listener(session_a, link_a)
    endpoint_b = RemoteLinkEndpoint.as_connector(session_b, link_b)
    endpoint_a.install()
    endpoint_b.install()
    return link_a, link_b, endpoint_a, endpoint_b


_REMOTE_RUNNER_WINDOW_TIMEOUT = 120.0
_REMOTE_LINK_MENU_ATTEMPTS = 100


def _start_remote_runners(
    session_a: Session,
    endpoint_a: RemoteLinkEndpoint,
    session_b: Session,
    endpoint_b: RemoteLinkEndpoint,
) -> tuple[_SessionRunner, _SessionRunner]:
    """Start one independent emulator driver for each TCP endpoint."""
    runner_a = _SessionRunner(session_a, endpoint_a)
    runner_b = _SessionRunner(session_b, endpoint_b)
    try:
        runner_a.start()
        runner_b.start()
    except BaseException:
        runner_a.stop()
        runner_b.stop()
        raise
    return runner_a, runner_b


def _stop_remote_runners(
    runner_a: _SessionRunner, runner_b: _SessionRunner
) -> None:
    """Stop both emulator drivers and leave their exceptions inspectable."""
    runner_a.stop()
    runner_b.stop()


def _advance_remote_runners(
    runner_a: _SessionRunner,
    runner_b: _SessionRunner,
    frames: int,
    *,
    timeout_s: float = _REMOTE_RUNNER_WINDOW_TIMEOUT,
) -> None:
    """Wait until both independent runners have advanced ``frames``."""
    if frames < 0:
        raise ValueError(f"frames must be non-negative, got {frames}")
    targets = (
        runner_a.session.current_tick() + frames,
        runner_b.session.current_tick() + frames,
    )
    deadline = time.monotonic() + timeout_s
    for runner, target in (
        (runner_a, targets[0]),
        (runner_b, targets[1]),
    ):
        runner.wait_until_tick(
            target,
            max(0.0, deadline - time.monotonic()),
        )


def _press_remote_both(
    runner_a: _SessionRunner,
    runner_b: _SessionRunner,
    button: str,
    *,
    duration: int,
    advance_frames: int = 20,
) -> None:
    """Apply one press to each runner before advancing more frames."""
    applied_a = runner_a.press(button, duration=duration)
    applied_b = runner_b.press(button, duration=duration)
    deadline = time.monotonic() + _REMOTE_RUNNER_WINDOW_TIMEOUT
    if not applied_a.wait(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("listener runner did not apply the queued press")
    if not applied_b.wait(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("connector runner did not apply the queued press")
    _advance_remote_runners(
        runner_a,
        runner_b,
        advance_frames,
        timeout_s=max(0.0, deadline - time.monotonic()),
    )


def _drive_remote_to_link_menu(
    runner_a: _SessionRunner,
    runner_b: _SessionRunner,
    ready,
    *,
    attempts: int = _REMOTE_LINK_MENU_ATTEMPTS,
) -> None:
    """Advance a TCP pair to a caller-defined LinkMenu milestone.

    Progress is measured in emulator frames, not host wall-clock sleeps.
    Each command is applied only after the previous command has reached both
    independent runners, and each bounded barrier raises if either serial
    hook stops making progress. This keeps the diagnostic useful on slow
    hosts without accepting an arbitrary A-press flood as evidence of
    gameplay.
    """
    _advance_remote_runners(runner_a, runner_b, 180)
    for _ in range(3):
        _press_remote_both(runner_a, runner_b, "up", duration=6)
    for _ in range(attempts):
        if ready():
            return
        _press_remote_both(runner_a, runner_b, "a", duration=4)


# --- end-to-end trade protocol over TCP ----------------------------------


# Fixture walkability notes — tests/fixtures/link/*/cable_club.state:
#
# - Blue fixture: player stands in front of the Cable Club attendant,
#   walkable, can press UP + A to engage dialog.
# - Yellow fixture: produced locally by
#   scripts/produce_yellow_cable_club_fixture.py from the sibling
#   walkthrough_to_cerulean harness's cerulean_pc.state. Player ends
#   at map 0x40, tile (11, 3), one tile south of the Cable Club link
#   receptionist; pressing UP + A fires CableClubNPC. If the fixture
#   is the old EnterMap-hookwarp version, the nybble test's
#   _ensure_fixture_is_walkable precondition below skips the run
#   rather than hanging until timeout.
# - Red fixture: reproduced by scripts/produce_cable_club_fixture.py from the
#   retained external cerulean_pc.state input and pinned to the color Red ROM.
_FIXTURES_WITH_WALKABLE_PLAYER = {"blue", "red", "yellow"}


# Expected load-time player position per version. Used by the
# nybble-test precondition so we don't run on a stale / wrong-shape
# fixture. Keyed by ROM version, value is (map_id, x, y).
_FIXTURE_EXPECTED_POS = {
    # All versions share the same Cerulean Pokecenter interior map
    # (0x40). The receptionist-adjacent tile is (11, 3).
    "blue": (0x40, 11, 3),
    "red": (0x40, 11, 3),
    "yellow": (0x40, 11, 3),
}


def _ensure_fixture_is_walkable(version: str, session: Session) -> None:
    """Raise pytest.skip if the loaded state doesn't match the expected
    load-time position for this version — pytest then reports the skip
    with a pointer at the regeneration script rather than hanging the
    test until timeout."""
    expected = _FIXTURE_EXPECTED_POS.get(version)
    if expected is None:
        return
    gs = session.read_game_state()
    actual = (gs.overworld.map_id, gs.overworld.x, gs.overworld.y)
    if actual != expected:
        em, ex, ey = expected
        am, ax, ay = actual
        pytest.skip(
            f"{version} fixture shape mismatch: expected player at "
            f"map=0x{em:02x}, ({ex}, {ey}); got map=0x{am:02x}, "
            f"({ax}, {ay}). Regenerate via "
            f"scripts/produce_yellow_cable_club_fixture.py (or the "
            f"version-appropriate equivalent)."
        )


def _install_autoselect_trade_hook(session: Session) -> None:
    """Port of LinkPair._install_linkmenu_autoselect_trade — forces
    the LinkMenu vote to 'TRADE' by pre-planting 0xD4 in
    wLinkMenuSelectionReceiveBuffer at the instruction that reads it."""
    label = "LinkMenu.exchangeMenuSelectionLoop"
    if label not in session.symbols:
        return
    recv_addr = session.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
    bank, addr = session.symbols.bank_addr(label)
    mem = session._pyboy.memory

    def _force(_ctx, _mem=mem, _addr=recv_addr):
        _mem[_addr] = 0xD4
        _mem[_addr + 1] = 0xD4

    session._pyboy.hook_register(bank, addr + 3, _force, None)

