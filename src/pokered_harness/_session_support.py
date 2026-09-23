"""Private support types and helpers for :mod:`pokered_harness.session` (issue #144).

Extracted from the 1443-line ``session`` module so the canonical module stays
below the 1000-line split target.  Nothing here is new public API: the
exception hierarchy, ``RunUntilResult``, the ``_HookState``/``_StopAttempt``
lifecycle records, and the module-level helpers are re-exported from
``pokered_harness.session`` so every existing import path keeps resolving.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pokered_harness.events.hooks import GameEvent
from pokered_harness.ownership import owner_group
from pokered_harness.pyboy_protocol import PyBoyLike

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
