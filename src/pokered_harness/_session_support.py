"""Private support types and helpers for :mod:`pokered_harness.session` (issue #144).

Extracted from the 1443-line ``session`` module so the canonical module stays
below the 1000-line split target.  Nothing here is new public API: the
exception hierarchy, ``RunUntilResult``, the ``_HookState``/``_StopAttempt``
lifecycle records, and the module-level helpers are re-exported from
``pokered_harness.session`` so every existing import path keeps resolving.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pokered_harness.events.hooks import GameEvent
from pokered_harness.ownership import owner_group
from pokered_harness.pyboy_protocol import PyBoyLike
from pokered_harness.state import GameState

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from pokered_harness.session import Session


class SessionError(RuntimeError):
    """Base class for failures at the emulator-session boundary."""

    code = "session_error"


class SessionClosedError(SessionError):
    """Raised when an operation is attempted after session shutdown."""

    code = "session_closed"


class SessionCloseTimeout(SessionError, TimeoutError):
    """Raised when shutdown cannot complete before its deadline."""

    code = "session_close_timeout"


class SessionCloseError(SessionError):
    """Raised when the emulator rejects a shutdown attempt."""

    code = "session_close_failed"


# Compatibility spelling used by the earlier ownership API.  Keep the main
# typed timeout class as the canonical public exception while allowing raw
# provider integrations to share their existing import contract.
SessionCleanupTimeoutError = SessionCloseTimeout


class SessionLockTimeout(SessionError):
    """Raised when a bounded compound-operation lock cannot be acquired."""

    code = "session_lock_timeout"


class SessionConfigurationError(ValueError):
    """Raised when a ROM, symbol file, or version pin is unusable."""

    code = "invalid_session_configuration"


class RomNotFoundError(SessionConfigurationError):
    """Raised when the configured ROM file is not available."""

    code = "rom_not_found"


class SymbolNotFoundError(SessionConfigurationError):
    """Raised when the configured symbol file is not available."""

    code = "symbol_not_found"


class InvalidStateError(ValueError):
    """Raised when a save-state payload is not usable."""

    code = "invalid_state"


class VersionMismatch(SessionError):
    """Raised when a loaded ROM, symbol file, or PyBoy version misses its pin."""

    code = "version_mismatch"


class RomHashMismatch(VersionMismatch):
    """Raised when the ROM bytes do not match the configured SHA-1 pin."""

    code = "rom_hash_mismatch"


class SymbolHashMismatch(VersionMismatch):
    """Raised when the symbol bytes do not match the configured SHA-1 pin."""

    code = "symbol_hash_mismatch"


_DEFAULT_CLOSE_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class RunUntilResult:
    event: GameEvent | None
    ticks_spent: int

    @property
    def reached(self) -> bool:
        return self.event is not None


class _HookState:
    """Small mutable flag shared by a guarded raw PyBoy hook."""

    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True


class _StopAttempt:
    """Immutable identity for one asynchronous ``PyBoy.stop`` attempt.

    The lifecycle fields on :class:`Session` are reused by a retry.  Keeping
    the completion event and result on this per-attempt record means an older
    caller cannot accidentally wait on, or read the result from, a later
    retry that replaced the session's current attempt.
    """

    __slots__ = ("done", "error", "owner_id", "stopped")

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.stopped = False


@contextmanager
def locked_sessions(
    *sessions: Session,
    allow_closed: bool = False,
    timeout_s: float | None = None,
) -> Iterator[None]:
    """Acquire one or more Session owners in canonical identity order.

    Pair operations must enter this group before touching provider/network
    locks.  The ordered owner scope rejects reverse single-owner acquisition
    before it can become a two-thread deadlock; cleanup may opt into closed
    sessions while still waiting for active emulator work to finish.
    """
    with owner_group(
        (session._owner for session in sessions),
        allow_closed=allow_closed,
        timeout=(
            None
            if timeout_s is None
            else _validate_timeout(timeout_s, "timeout_s")
        ),
    ):
        if not allow_closed:
            for session in sessions:
                session._ensure_open()
        yield


def sha1_of_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with Path(path).open("rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _validate_timeout(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be finite and non-negative")
    try:
        candidate = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite and non-negative") from exc
    if not math.isfinite(candidate) or candidate < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return candidate


def _validate_close_timeout(value: float) -> float:
    """Validate the strict positive deadline used by :meth:`Session.close`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(  # noqa: TRY004 - preserve close() ValueError contract
            "timeout_s must be finite and positive, at most threading.TIMEOUT_MAX"
        )
    try:
        candidate = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "timeout_s must be finite and positive, at most threading.TIMEOUT_MAX"
        ) from exc
    if not math.isfinite(candidate) or candidate <= 0:
        raise ValueError(
            "timeout_s must be finite and positive, at most threading.TIMEOUT_MAX"
        )
    # ``threading.Lock.acquire`` rejects values above TIMEOUT_MAX even though
    # a caller may legitimately use a very large finite deadline.  Preserve
    # that public contract by clamping after validation; integer conversion
    # overflow remains a rejected input above.
    return min(candidate, threading.TIMEOUT_MAX)


def _normalise_sha1(value: str, *, label: str = "ROM SHA-1") -> str:
    if not isinstance(value, str):
        raise SessionConfigurationError(
            f"{label} must be exactly 40 hexadecimal characters, got {value!r}"
        )
    candidate = value.strip()
    if not _SHA1_RE.fullmatch(candidate):
        raise SessionConfigurationError(
            f"{label} must be exactly 40 hexadecimal characters, got {value!r}"
        )
    return candidate.lower()


def _validate_positive_int(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _default_pyboy_factory(
    rom_path: str, *, window: str = "null", cgb: bool = True
) -> PyBoyLike:
    from pyboy import PyBoy

    # ``window`` is configurable: "null" (headless, ADR default — MCP/tests
    # stay fast and windowless) vs "SDL2" (visible window for local viewing).
    # ``cgb=True`` enables Game Boy Color mode so Pokemon Red renders with
    # its stock CGB auto-palette instead of the DMG grayscale fallback.
    # Headless harness consumers do not expose audio, and PyBoy's default
    # sound emulation is a significant per-frame cost in the source runtime.
    # Keep audio for visible sessions while making the documented headless
    # path deterministic and suitable for bounded automation.
    sound_emulated = window not in {"null", "headless", "dummy"}
    return PyBoy(  # type: ignore[return-value]
        rom_path,
        window=window,
        cgb=cgb,
        sound_emulated=sound_emulated,
    )

# Monotonic identity for each Session.  A replacement session (a new Session
# object over a fresh emulator) must not be able to reuse the tick/load
# generation pair of the session it replaced, so the identity is included in
# every epoch snapshot.
#
# A bare per-process counter is not enough: it restarts at 1 in every new
# process, so two fresh processes would report the same ``session_id`` for
# their first session and a client comparing epochs across a restart could
# accept a stale snapshot.
#
# The id is therefore a string naming the process lifetime and the in-process
# session index:
#
#     <pid>-<process-start-ns>-<index>
#
# ``pid`` separates concurrent processes, and the system-wide monotonic clock
# separates sequential process lifetimes that reuse a pid.  A string (rather
# than a packed integer) keeps both components exact: a 53-bit float-safe
# integer cannot hold a nanosecond timestamp together with a pid and a
# counter without mangling one of them, and JSON transports numbers through
# IEEE-754 doubles.
_PROCESS_START_NS = time.monotonic_ns()
_PROCESS_LIFETIME_ID = f"{os.getpid()}-{_PROCESS_START_NS}"
_SESSION_ID_SEQUENCE = itertools.count(1)
_SESSION_ID_LOCK = threading.Lock()


def _next_session_id() -> str:
    with _SESSION_ID_LOCK:
        return f"{_PROCESS_LIFETIME_ID}-{next(_SESSION_ID_SEQUENCE)}"


# ROM labels whose execution brackets the battle command/move menu.  There is
# no RAM byte that reports "menu open": ``wMoveMenuType`` is a mode selector
# (0 regular, 1 mimic, 2 relearn/PP) written before every ``MoveSelectionMenu``
# call and left set after the menu closes.  The session instead observes the
# control flow.  Entry into ``SelectMenuItem`` (the move-menu input loop) or
# ``DisplayBattleMenu.handleBattleMenuInput`` (the FIGHT/ITEM/POKéMON/RUN
# input setup) proves the menu was drawn and is awaiting input; entry into the
# per-turn ``MainInBattleLoop`` or its post-selection continuation
# ``MainInBattleLoop.selectEnemyMove`` proves the menu closed.  Observation is
# only available when every one of these labels exists in the loaded ``.sym``;
# otherwise the session reports no menu evidence and ``COMMAND_SELECTION`` is
# never derived (fail closed).
_BATTLE_MENU_OPEN_SYMBOLS = (
    "SelectMenuItem",
    "DisplayBattleMenu.handleBattleMenuInput",
)
# The routine every battle end funnels through (``core.asm``:
# ``_InitBattleCommon`` calls ``callfar EndOfBattle``).  Its entry is the last
# instant the outcome bytes are still meaningful, so the session samples them
# there rather than trusting a client-timed read.
_BATTLE_END_SYMBOL = "EndOfBattle"
_BATTLE_MENU_CLOSE_SYMBOLS = (
    "MainInBattleLoop",
    "MainInBattleLoop.selectEnemyMove",
    # Both the regular move-selection return (core.asm: ``call MoveSelectionMenu
    # / call LoadScreenTilesFromBuffer1``) and the Mimic submenu return
    # (effects.asm: ``call MoveSelectionMenu / call LoadScreenTilesFromBuffer1``)
    # redraw the screen immediately after the menu closes.  ``MainInBattleLoop``
    # alone does not fire on Mimic's return into animation/result text, so this
    # closes the observation on the shared post-menu redraw path.
    "LoadScreenTilesFromBuffer1",
)
# Move execution brackets, used to observe ``ACTION_RESOLUTION`` for an
# ordinary FIGHT turn.  ``wActionResultOrTookBattleTurn`` cannot report it:
# ``ExecutePlayerMoveDone`` clears the byte as it returns, so a client polling
# at any interval only ever sees the flag set for the item/switch/run turns
# that never execute a move.  Both combatants' routines are bracketed because
# either side can be resolving when a client reads.
#
# ``Execute*MoveDone`` is not the only exit.  A status or residual move jumps
# straight to ``JumpMoveEffect`` (``engine/battle/effects.asm``), whose handler
# returns to the caller of ``Execute*Move``, and the damage paths return
# directly once the target's HP reaches zero (``core.asm``: the player routine
# returns with ``ret z`` beside the faint check).  Those returns bypass both
# ``*Done`` labels, so the bracket would stay open while the ROM moved on,
# turning the next command or replacement menu into a spurious contradiction.
# The exits therefore also include the post-move continuations that every one
# of those returns lands on: ``HandlePoisonBurnLeechSeed`` runs immediately
# after either move routine returns, ``HandlePlayerMonFainted`` /
# ``HandleEnemyMonFainted`` are the faint continuations (they run *before*
# ``ChooseNextMon`` opens the replacement menu), ``MainInBattleLoop`` is the
# per-turn entry every finished turn comes back to, and ``EndOfBattle`` is the
# escape/run tail that returns out of the loop without re-entering it.
_BATTLE_RESOLUTION_OPEN_SYMBOLS = (
    "ExecutePlayerMove",
    "ExecuteEnemyMove",
)
_BATTLE_RESOLUTION_CLOSE_SYMBOLS = (
    "ExecutePlayerMoveDone",
    "ExecuteEnemyMoveDone",
    "HandlePoisonBurnLeechSeed",
    "HandlePlayerMonFainted",
    "HandleEnemyMonFainted",
    "MainInBattleLoop",
    "EndOfBattle",
)


@dataclass(frozen=True, slots=True)
class SessionEpoch:
    """Identity and clock counters captured atomically with a snapshot.

    ``tick`` advances with :meth:`Session.step`; ``load_generation`` advances
    on every successful :meth:`Session.load_state` (the tick is restored by a
    load, so the pair alone can repeat); ``reset_generation`` advances on
    every :meth:`Session.reset_tick` (which can intentionally rewind the
    tick); ``session_id`` distinguishes a replacement :class:`Session` from
    the one it replaced, including across process restarts (it names the
    process lifetime and the in-process session index).
    """

    tick: int
    load_generation: int
    reset_generation: int
    session_id: str


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """A game-state observation paired with its :class:`SessionEpoch`.

    Both fields are read under one owner-locked emulator scope, so the epoch
    always describes the same instant as ``state``; a concurrent step or load
    cannot tear the two apart.
    """

    state: GameState
    epoch: SessionEpoch
