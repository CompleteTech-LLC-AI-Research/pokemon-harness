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
from collections.abc import Callable
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
        self._before_step: Callable[[], None] | None = None
        self._last_progress_at = time.monotonic()
        self.exc: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"runner-{endpoint.clock_status_byte:#x}",
            daemon=True,
        )

    def start(self) -> None:
        with self._progress:
            self._last_progress_at = time.monotonic()
        self._thread.start()

    def set_before_step(self, callback: Callable[[], None] | None) -> None:
        """Set owner-thread work to run immediately before the next step."""
        with self._progress:
            self._before_step = callback

    def progress_status(self) -> tuple[float, Exception | None, bool]:
        """Return runner bookkeeping without reading the emulator state."""
        with self._progress:
            return self._last_progress_at, self.exc, self._thread.is_alive()

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
                with self._progress:
                    before_step = self._before_step
                if before_step is not None:
                    before_step()
                self.endpoint.step(self.chunk)
                with self._progress:
                    self._last_progress_at = time.monotonic()
                    self._progress.notify_all()
        except Exception as exc:  # noqa: BLE001
            with self._progress:
                self.exc = exc
                self._progress.notify_all()


# --- hook-counter helper -------------------------------------------------


def _install_hook_counter(session: Session, symbol: str, bucket: list[int], slot: int) -> None:
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


def _stop_remote_runners(runner_a: _SessionRunner, runner_b: _SessionRunner) -> None:
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


class _RemoteInputSchedule:
    """Apply one side's bounded menu inputs on its owner thread."""

    def __init__(
        self,
        session: Session,
        ready: Callable[[], bool],
        stop_inputs: threading.Event,
        input_lock: threading.Lock,
        *,
        attempts: int,
    ) -> None:
        self._session = session
        self._ready = ready
        self._stop_inputs = stop_inputs
        self._input_lock = input_lock
        self._attempts = max(0, attempts)
        self._start_tick: int | None = None
        self._next_tick: int | None = None
        self._finish_tick: int | None = None
        self._up_count = 0
        self._a_count = 0
        self.done = threading.Event()

    def _press_if_active(self, button: str, duration: int) -> bool:
        if self._ready():
            with self._input_lock:
                self._stop_inputs.set()
            self.done.set()
            return False
        with self._input_lock:
            if self._stop_inputs.is_set():
                self.done.set()
                return False
            self._session.press(button, duration=duration)
        return True

    def before_step(self) -> None:
        if self.done.is_set() or self._stop_inputs.is_set():
            self.done.set()
            return
        tick = self._session.current_tick()
        if self._start_tick is None:
            self._start_tick = tick
            self._next_tick = tick + 180
            return
        if self._next_tick is None or tick < self._next_tick:
            return
        if self._finish_tick is not None:
            self.done.set()
            return
        if self._up_count < 3:
            if not self._press_if_active("up", 6):
                return
            self._up_count += 1
            self._next_tick = tick + 20
            return
        if self._a_count >= self._attempts:
            self.done.set()
            return
        if not self._press_if_active("a", 4):
            return
        self._a_count += 1
        self._next_tick = tick + 20
        if self._a_count == self._attempts:
            self._finish_tick = self._next_tick


def _drive_remote_to_link_menu(
    runner_a: _SessionRunner,
    runner_b: _SessionRunner,
    ready,
    *,
    attempts: int = _REMOTE_LINK_MENU_ATTEMPTS,
) -> None:
    """Advance a TCP pair to a caller-defined LinkMenu milestone.

    Each runner schedules its own bounded inputs on its emulator-owning
    thread, so a serial RPC blocking one side does not prevent the other side
    from receiving its next command. The caller only observes readiness,
    runner exceptions, and per-runner step-progress timestamps; it never
    reads emulator state from the coordinating thread. ``ready`` is evaluated
    concurrently by the coordinator and owner threads, so it must be a
    thread-safe, non-blocking predicate over shared progress counters only.
    """
    stop_inputs = threading.Event()
    input_lock = threading.Lock()
    schedules = (
        _RemoteInputSchedule(runner_a.session, ready, stop_inputs, input_lock, attempts=attempts),
        _RemoteInputSchedule(runner_b.session, ready, stop_inputs, input_lock, attempts=attempts),
    )
    runners = (runner_a, runner_b)
    for runner, schedule in zip(runners, schedules, strict=True):
        runner.set_before_step(schedule.before_step)
    try:
        while True:
            now = time.monotonic()
            for runner in runners:
                last_progress, exc, alive = runner.progress_status()
                if exc is not None:
                    raise exc
                if not alive:
                    raise RuntimeError(f"{runner._thread.name} stopped unexpectedly")
                if now - last_progress > _REMOTE_RUNNER_WINDOW_TIMEOUT:
                    raise TimeoutError(
                        f"{runner._thread.name} made no step progress within "
                        f"{_REMOTE_RUNNER_WINDOW_TIMEOUT:.1f}s"
                    )
            if ready():
                with input_lock:
                    stop_inputs.set()
                return
            if all(schedule.done.is_set() for schedule in schedules):
                return
            time.sleep(0.01)
    finally:
        with input_lock:
            stop_inputs.set()
        for runner in runners:
            runner.set_before_step(None)


class _RemoteTradeInputSchedule:
    """Drive one Cable Club session independently on its owner thread."""

    def __init__(
        self,
        session: Session,
        role: str,
        stop_inputs: threading.Event,
        input_lock: threading.Lock,
        rng_done: dict[str, threading.Event],
    ) -> None:
        self._session = session
        self._role = role
        self._stop_inputs = stop_inputs
        self._input_lock = input_lock
        self._rng_done = rng_done
        self.start_tick: int | None = None
        self.trade_tick: int | None = None
        self.last_tick: int | None = None
        self.stage = "initial_settle"
        self.next_tick: int | None = None
        self.move_due: int | None = None
        self.menu_presses = 0
        self.movement_cycles = 0
        self.rng_presses = 0
        self.failure: str | None = None

    def _press(self, button: str, duration: int) -> bool:
        with self._input_lock:
            if self._stop_inputs.is_set() or all(
                event.is_set() for event in self._rng_done.values()
            ):
                return False
            self._session.press(button, duration=duration)
        return True

    def _fail(self, reason: str) -> None:
        with self._input_lock:
            self.failure = reason
            self._stop_inputs.set()

    def before_step(self) -> None:
        """Run only on the owning runner thread before its next chunk."""
        if self._stop_inputs.is_set():
            return
        tick = self._session.current_tick()
        self.last_tick = tick
        if self.start_tick is None:
            self.start_tick = tick
            self.stage = "menu"
            self.next_tick = tick + 60
            return

        state = self._session.read_game_state().overworld
        if self.stage == "menu":
            if state.map_id == 0xEF:
                self.trade_tick = tick
                self.stage = "trade_settle"
                return
            if self.next_tick is None or tick < self.next_tick:
                return
            if self.menu_presses >= 120:
                self._fail("Trade Center not reached within 120 A presses")
                return
            if not self._press("a", duration=4):
                return
            self.menu_presses += 1
            self.next_tick = tick + 8
            return

        if self.stage == "trade_settle":
            if self.trade_tick is None or tick < self.trade_tick + 240:
                return
            self.stage = "movement"
            self.move_due = tick

        if self.stage == "movement":
            if self.movement_cycles < 4:
                if self.move_due is None or tick < self.move_due:
                    return
                button = "right" if state.x < 4 else "left" if state.x > 5 else None
                if button is not None and not self._press(button, duration=8):
                    return
                self.movement_cycles += 1
                self.move_due = tick + 24
                return
            if self.move_due is None or tick < self.move_due:
                return
            self.stage = "rng"
            self.next_tick = tick

        if self.stage == "rng":
            if self._rng_done[self._role].is_set():
                self.stage = "done"
                return
            if self.next_tick is None or tick < self.next_tick:
                return
            if self.rng_presses >= 200:
                self._fail("matching RNG return not reached within 200 A presses")
                return
            if not self._press("a", duration=4):
                return
            self.rng_presses += 1
            self.next_tick = tick + 12


def _drive_remote_trade_to_rng(
    runners: tuple[_SessionRunner, _SessionRunner],
    schedules: tuple[_RemoteTradeInputSchedule, _RemoteTradeInputSchedule],
    rng_done: dict[str, threading.Event],
    stop_inputs: threading.Event,
    input_lock: threading.Lock,
    *,
    no_progress_timeout_s: float = 120.0,
    outer_timeout_s: float = 600.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Start owner schedules and wait for both map and matched-RNG proofs.

    The coordinator reads only runner bookkeeping and shared events/state;
    it never calls ``current_tick`` or reads either emulator. Teardown remains
    with the caller so it can close both links before joining the runners.
    """
    for runner, schedule in zip(runners, schedules, strict=True):
        runner.set_before_step(schedule.before_step)
    started_at = monotonic()
    try:
        for runner in runners:
            runner.start()
        while True:
            now = monotonic()
            if now - started_at >= outer_timeout_s:
                raise TimeoutError(
                    f"remote trade drive exceeded {outer_timeout_s:.1f}s outer budget"
                )
            with input_lock:
                failures = [schedule.failure for schedule in schedules if schedule.failure]
            if failures:
                raise RuntimeError("remote trade input schedule failed: " + "; ".join(failures))
            if all(event.is_set() for event in rng_done.values()):
                with input_lock:
                    if all(event.is_set() for event in rng_done.values()):
                        failures = [schedule.failure for schedule in schedules if schedule.failure]
                        if failures:
                            raise RuntimeError(
                                "remote trade input schedule failed: " + "; ".join(failures)
                            )
                        missing_map = [
                            schedule._role for schedule in schedules if schedule.trade_tick is None
                        ]
                        if missing_map:
                            raise AssertionError(
                                f"Trade Center map was not observed for: {missing_map}"
                            )
                        stop_inputs.set()
                return
            for runner in runners:
                last_progress, exc, alive = runner.progress_status()
                if exc is not None:
                    raise exc
                if not alive:
                    raise RuntimeError(f"{runner._thread.name} stopped before RNG completion")
                if now - last_progress >= no_progress_timeout_s:
                    raise TimeoutError(
                        f"{runner._thread.name} made no progress for {no_progress_timeout_s:.1f}s"
                    )
            sleep(0.01)
    finally:
        with input_lock:
            stop_inputs.set()
        for runner in runners:
            runner.set_before_step(None)


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
