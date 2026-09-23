"""Declared catalog constants, refusals, and shared numeric guards.

Split out of ``scripts/produce_battle_scenario.py`` for the #122 file-size
contract with no behavior change: the code below is copied verbatim except that
calls to facade-owned, monkeypatch-patched entry points resolve through
``_entry`` so attribute patches on the loaded producer module stay visible.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_REPO = Path(__file__).resolve().parents[1]
_CATALOG_PATH = _REPO / "release-evidence" / "battle-scenarios.json"
_CAPTURE_MESSAGE = (
    "capture requires a controlled ROM run; no fixture bytes were written. "
    "Run the documented external capture procedure with legal ROM/SYM inputs, "
    "record the resulting hashes, and pin them in the scenario catalog."
)
_DEFAULT_STEP_FRAMES = 20
_DEFAULT_PRESS_DURATION = 8
# The single-console bounded drive can only reproduce boundaries it reaches
# with legal inputs from a supplied source state.  A linked boundary needs a
# controlled two-console run and is refused (never guessed).
_SUPPORTED_BOUNDARY = ("link_reception", "pre_command")
_CAPTURE_PRODUCER = "scripts/produce_battle_scenario.py"
# The Cable Club link receptionist tile (11, 3) in the Cerulean Pokecenter
# interior (map 0x40, shared by R/B/Y) — the only tile whose A press fires
# ``CableClubNPC``.
_LINK_RECEPTION_TILE = (11, 3)
# ``wLinkState`` values the producer is willing to assert, taken from the
# pinned pret sources (``LINK_STATE_NONE EQU $00 ; not using link``).  Only
# states with a verified meaning are listed; an unlisted label is refused
# rather than skipped.
_LINK_STATE_CODES = {"disconnected": 0}
# Provenance statuses the catalog may declare.  Anything else is refused rather
# than copied into a record a consumer might read as an admission.
_PROVENANCE_STATUSES = ("verified", "partial", "derived", "unknown")
# How a declared fixture producer is located.  ``repository`` names a tracked
# script whose bytes can be re-read here; ``external`` names an operator- or
# out-of-tree producer whose identity can only be pinned by digest; ``derived``
# names a transformation whose identity is not a single readable file.
_PRODUCER_KINDS = ("repository", "external", "derived")
# The runtime modes the harness builds and can therefore claim to have measured.
_RUNTIME_MODES = ("source", "cython")
# A capture whose session was injected for diagnosis cannot claim an observed
# runtime at all: the process identity is never inspected for that path, so the
# record says so instead of copying the caller's request into the identity.
_UNMEASURED_RUNTIME = "unmeasured"
# Whether the record's runtime identity was measured in this process or the
# capture ran with an injected session and cannot qualify its own runtime.
_RUNTIME_MEASUREMENTS = ("measured", "not_measured")
# Fixture fields a "verified" entry must pin before its bytes may be compared.
_PINNED_FIXTURE_FIELDS = ("sha1", "sha256", "size_bytes")
# Fields every admitted capture record must carry; a record missing one of them
# cannot be admitted as validated metadata.
_CAPTURE_RECORD_FIELDS = (
    "producer",
    "producer_sha1",
    "runtime",
    "runtime_measurement",
    "role",
    "captured_at_utc",
    "wall_seconds",
    "declared_bounds",
    "inputs_used",
    "frames_used",
    "input_sequence",
    "observed_boundary",
    "rom_sha1",
    "sym_sha1",
    "output",
    "reproduction",
)
_BUTTON_NAMES = frozenset({"a", "b", "start", "select", "up", "down", "left", "right"})


class ScenarioRefusal(ValueError):
    """Raised when a declared scenario request is refused before capture."""


class ScenarioBlocked(ScenarioRefusal):
    """Raised when a required scenario is absent; callers must report BLOCKED."""


class CaptureNotAvailable(RuntimeError):
    """Raised when no controlled ROM run can be established for a capture."""


class CaptureBoundsExceeded(ScenarioRefusal):
    """Raised when a declared capture bound would be exceeded."""


class CapturePreconditionFailed(ScenarioRefusal):
    """Raised when the declared capture boundary is not observed before saving."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioRefusal(message)


def _note(exception: BaseException, message: str) -> None:
    """Attach supplementary evidence to an exception when the runtime supports it."""
    add_note = getattr(exception, "add_note", None)
    if add_note is not None:
        add_note(message)


def _check_deadline(deadline: float, clock: Callable[[], float], phase: str) -> None:
    """Refuse once an absolute declared deadline has passed."""
    if clock() >= deadline:
        raise CaptureBoundsExceeded(
            f"capture exceeded max_wall_seconds during {phase}; "
            "the declared bound may only be raised with a recorded justification"
        )


def _deadline_guard(deadline: float, clock: Callable[[], float]) -> Callable[[str], None]:
    """Bind an absolute deadline to the phase-reporting guard callables use."""
    return functools.partial(_check_deadline, deadline, clock)
