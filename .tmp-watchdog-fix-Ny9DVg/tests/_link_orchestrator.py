"""Deterministic lockstep stepper for two-session link-cable tests.

The `_SessionRunner` in :mod:`tests.test_link_integration_remote` runs
each session on its own daemon thread, auto-stepping without any
coordination. That's fine for transport-level milestones (handshake,
nybble sync, menu vote), but it makes anything that needs
frame-accurate button alignment across both sides (walking two players
onto the trade-table hidden-event tile and pressing A on the same
frame) impossible to drive.

:class:`LockstepOrchestrator` fixes that:

1. Owns two worker threads, one per session.
2. Each worker waits on a ``go`` event, applies a queued button (if
   any), steps exactly N frames, then signals ``done``.
3. The main thread fires both ``go`` events simultaneously and waits
   for both ``done`` events, giving a per-chunk barrier across the
   two sessions.
4. Button presses are queued via ``press_both`` / ``press_a`` /
   ``press_b`` and apply at the top of the next step — so the main
   thread controls exactly which frame a button lands on.

The SerialLink transport (TCP or InProcess) is unchanged. Hooks
firing during a step still block on ``link.exchange``; because both
workers are running their step concurrently (not truly sequentially),
a hook on side A can block waiting for side B's hook to issue its
matching exchange — standard producer/consumer via the per-kind
queue inside the SerialLink.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from pokered_harness.link.remote import RemoteLinkEndpoint
from pokered_harness.session import Session


@dataclass
class _PendingPress:
    button: str
    duration: int


class _Worker:
    """One session + endpoint, driven via go/done events from the
    orchestrator's main thread."""

    def __init__(
        self,
        name: str,
        session: Session,
        endpoint: RemoteLinkEndpoint,
    ) -> None:
        self.name = name
        self.session = session
        self.endpoint = endpoint
        self._go = threading.Event()
        self._done = threading.Event()
        self._stop = threading.Event()
        self._pending_press: _PendingPress | None = None
        self._chunk_frames: int = 1
        self.exc: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, name=f"lockstep-{name}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._go.set()  # unblock one last time so the loop can exit
        self._thread.join(timeout=timeout)

    def queue_press(self, button: str, duration: int) -> None:
        """Queue a press to apply at the start of the next chunk. Only
        one pending press per worker — later calls overwrite earlier
        ones (matches the test-use assumption of one press per frame)."""
        self._pending_press = _PendingPress(button=button, duration=duration)

    def fire(self, chunk_frames: int) -> None:
        self._chunk_frames = chunk_frames
        self._done.clear()
        self._go.set()

    def wait_done(self, timeout: float) -> bool:
        return self._done.wait(timeout=timeout)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._go.wait()
                self._go.clear()
                if self._stop.is_set():
                    break
                # Apply queued press at the top of this chunk.
                if self._pending_press is not None:
                    p = self._pending_press
                    self._pending_press = None
                    self.session.press(p.button, duration=p.duration)
                # Step N frames, calling serial_tick after each so the
                # Serial ISR fires at the same cadence as LinkPair's
                # per-frame hardware tick.
                for _ in range(self._chunk_frames):
                    self.session.step(1)
                    self.endpoint.serial_tick()
                self._done.set()
        except BaseException as exc:  # noqa: BLE001
            self.exc = exc
            self._done.set()


class LockstepOrchestrator:
    """Deterministic per-frame sync across two sessions on a TCP
    SerialLink.

    Usage::

        ork = LockstepOrchestrator(sess_a, ep_a, sess_b, ep_b)
        ork.start()
        try:
            ork.step(60)                        # both advance 60 frames
            ork.press_both("a", duration=4)     # press A on both
            ork.step(20)                        # let the press register
            ork.press_a("left", duration=6)     # independently steer
            ork.press_b("right", duration=6)
            ork.step(24)                        # one tile's worth
        finally:
            ork.stop()

    Exceptions raised inside worker threads surface on the next
    ``step`` call.
    """

    def __init__(
        self,
        session_a: Session,
        endpoint_a: RemoteLinkEndpoint,
        session_b: Session,
        endpoint_b: RemoteLinkEndpoint,
    ) -> None:
        self._worker_a = _Worker("A", session_a, endpoint_a)
        self._worker_b = _Worker("B", session_b, endpoint_b)

    def start(self) -> None:
        self._worker_a.start()
        self._worker_b.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._worker_a.stop(timeout=timeout)
        self._worker_b.stop(timeout=timeout)

    @property
    def session_a(self) -> Session:
        return self._worker_a.session

    @property
    def session_b(self) -> Session:
        return self._worker_b.session

    @property
    def endpoint_a(self) -> RemoteLinkEndpoint:
        return self._worker_a.endpoint

    @property
    def endpoint_b(self) -> RemoteLinkEndpoint:
        return self._worker_b.endpoint

    def press_a(self, button: str, duration: int = 6) -> None:
        self._worker_a.queue_press(button, duration)

    def press_b(self, button: str, duration: int = 6) -> None:
        self._worker_b.queue_press(button, duration)

    def press_both(self, button: str, duration: int = 6) -> None:
        """Queue the same press on both sides for the next step. The
        two presses land on the same game-frame boundary — the whole
        point of the lockstep orchestrator."""
        self._worker_a.queue_press(button, duration)
        self._worker_b.queue_press(button, duration)

    def step(self, frames: int = 1, *, timeout_per_frame: float = 10.0) -> None:
        """Advance both sessions by ``frames`` frames, synchronized
        via a per-chunk barrier. Applies any queued presses at the
        top of the chunk.

        ``timeout_per_frame`` guards against a serial exchange
        deadlocking a hook; it scales the barrier wait so a ``step(60)``
        can wait up to ~60 × timeout_per_frame for each side.
        """
        if frames <= 0:
            raise ValueError(f"frames must be positive, got {frames}")
        self._worker_a.fire(frames)
        self._worker_b.fire(frames)
        barrier = timeout_per_frame * frames
        if not self._worker_a.wait_done(timeout=barrier):
            raise TimeoutError(
                f"side A didn't finish {frames} frames within {barrier:.1f}s"
            )
        if not self._worker_b.wait_done(timeout=barrier):
            raise TimeoutError(
                f"side B didn't finish {frames} frames within {barrier:.1f}s"
            )
        if self._worker_a.exc is not None:
            raise self._worker_a.exc
        if self._worker_b.exc is not None:
            raise self._worker_b.exc


# --- walk-to helper -------------------------------------------------------
#
# Gen 1 overworld movement: pressing a direction button holds for a few
# frames, the game notices on its next JoypadLowSensitivity read, and
# if the tile is walkable the player sprite animates over 16 frames
# (wWalkCounter counts down). We drive that by pressing the direction
# for a short duration at the top of a chunk and stepping enough frames
# for the tile move to complete, then checking (x, y).


_DIRECTION_DELTAS = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}


def _pos(session: Session) -> tuple[int, int]:
    gs = session.read_game_state()
    return (gs.overworld.x, gs.overworld.y)


def walk_a_toward(
    ork: LockstepOrchestrator,
    target_xy: tuple[int, int],
    *,
    max_tiles: int = 20,
    tile_frames: int = 20,
) -> bool:
    """Walk side A toward ``target_xy`` using the orchestrator. Returns
    True on success, False if a step didn't make progress (blocked
    tile / NPC). Side B just idles — useful when only one player needs
    to move."""
    return _walk_toward(
        ork=ork, target_xy=target_xy, side="a",
        max_tiles=max_tiles, tile_frames=tile_frames,
    )


def walk_b_toward(
    ork: LockstepOrchestrator,
    target_xy: tuple[int, int],
    *,
    max_tiles: int = 20,
    tile_frames: int = 20,
) -> bool:
    return _walk_toward(
        ork=ork, target_xy=target_xy, side="b",
        max_tiles=max_tiles, tile_frames=tile_frames,
    )


def _walk_toward(
    *,
    ork: LockstepOrchestrator,
    target_xy: tuple[int, int],
    side: str,
    max_tiles: int,
    tile_frames: int,
) -> bool:
    target_x, target_y = target_xy
    session = ork.session_a if side == "a" else ork.session_b
    press = ork.press_a if side == "a" else ork.press_b
    for _ in range(max_tiles):
        cx, cy = _pos(session)
        if (cx, cy) == (target_x, target_y):
            return True
        # Greedy Manhattan — move along whichever axis is further from
        # the target. Crude but adequate for the trade-table walks
        # (straight-line paths in open rows of the pokecenter / trade
        # center).
        dx = target_x - cx
        dy = target_y - cy
        if abs(dx) >= abs(dy) and dx != 0:
            direction = "right" if dx > 0 else "left"
        elif dy != 0:
            direction = "down" if dy > 0 else "up"
        else:
            return True
        press(direction, duration=8)
        ork.step(tile_frames)
        if _pos(session) == (cx, cy):
            # Didn't move — blocked. Try a perpendicular step then
            # retry. If still stuck, give up (caller should re-plan).
            return False
    return _pos(session) == (target_x, target_y)


__all__ = [
    "LockstepOrchestrator",
    "walk_a_toward",
    "walk_b_toward",
]
