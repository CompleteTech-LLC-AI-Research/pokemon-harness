"""Produce a declared battle-scenario fixture with a bounded, fail-closed contract.

The catalog in ``release-evidence/battle-scenarios.json`` declares each scenario,
its pinned ROM/SYM, its immutable input fixture, and finite capture bounds.  This
command validates a request against that contract and refuses unsafe or
inconsistent requests *before* touching the emulator:

* an unknown or duplicate scenario is refused (an unknown required scenario is
  reported as ``BLOCKED``, never as a passing skip);
* a ROM or symbol file that is not pinned in ``VERSIONS.md`` is refused;
* an ``--input-fixture`` whose SHA-1 disagrees with the declared input is refused;
* an existing ``--output`` is never overwritten;
* non-finite or non-positive bounds are refused.

The capture path itself is bounded and fail-closed:

* it drives the pinned ROM with an explicit, replayable input sequence and no
  runtime RAM, party, PP, RNG, or serial mutation;
* it enforces the declared ``max_frames``, ``max_wall_seconds``, and
  ``max_inputs`` bounds and aborts as soon as any of them would be exceeded;
* it asserts the declared ``capture_boundary`` (map, link state, and any declared
  party shape) *before* anything is written;
* it writes the state file with ``O_EXCL`` so an existing fixture can never be
  overwritten, and it leaves no partial file behind when capture fails;
 * it records a private report with the replayable input history, the producer and
   runtime identities, the asset/output hashes, and the reproduction comparison
   against the pinned fixture hashes;
* it refuses incomplete or unverifiable declared metadata — a missing capture
  boundary or bounds block, a non-integer map, a link-state label with no verified
  encoding, a producer-named verified entry whose producer no longer exists, or a
  verified entry with no pinned fixture hashes — before the emulator is opened;
* it validates the capture record it is about to record and admits only records
  that carry the producer, asset, boundary, and output identities;
* an interruption publishes nothing, leaves no staged file, and still closes the
  emulator session.

Callers still capture real fixtures from legal ROM/SYM inputs; the recorded
hashes are what consumers pin.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
# Fixture fields a "verified" entry must pin before its bytes may be compared.
_PINNED_FIXTURE_FIELDS = ("sha1", "sha256", "size_bytes")
# Fields every admitted capture record must carry; a record missing one of them
# cannot be admitted as validated metadata.
_CAPTURE_RECORD_FIELDS = (
    "producer",
    "producer_sha1",
    "runtime",
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


@dataclass(frozen=True)
class _InputStep:
    """One replayable capture input: a button press, or an idle frame run."""

    button: str | None
    duration: int
    frames: int

    @property
    def emulated_frames(self) -> int:
        """Frames this step advances the emulator.

        ``PyBoy.button`` only schedules a release; the emulator advances when
        the session ticks, so a press advances exactly ``frames`` frames and
        ``duration`` merely bounds the hold inside them.
        """
        return self.frames

    def render(self) -> str:
        if self.button is None:
            return f"step:{self.frames}"
        return f"press:{self.button}:{self.duration}:{self.frames}"


class _CaptureBudget:
    """Fail-closed frame, input, and wall-clock budget for one bounded drive.

    Every emulator advance is charged against the declared bounds *before* it
    happens, so a drive can never overshoot ``max_frames`` or ``max_inputs``,
    and the wall-clock deadline is re-checked around every advance.
    """

    def __init__(self, bounds: dict[str, Any], *, clock: Callable[[], float]) -> None:
        self._max_frames = bounds["max_frames"]
        self._max_inputs = bounds["max_inputs"]
        self._clock = clock
        self._deadline = clock() + float(bounds["max_wall_seconds"])
        self.frames = 0
        self.inputs = 0
        self.history: list[_InputStep] = []

    def _check_wall(self, phase: str) -> None:
        if self._clock() >= self._deadline:
            raise CaptureBoundsExceeded(
                f"capture exceeded max_wall_seconds during {phase}; "
                "the declared bound may only be raised with a recorded justification"
            )

    def _reserve_frames(self, frames: int) -> None:
        if self.frames + frames > self._max_frames:
            raise CaptureBoundsExceeded(
                f"capture would exceed max_frames ({self._max_frames}); "
                "aborted before advancing the emulator"
            )
        self.frames += frames

    def step(self, session: Any, frames: int) -> None:
        """Advance ``frames`` idle frames, charged against the bounds."""
        _positive_int(frames, "step frames")
        self._check_wall("idle step")
        step = _InputStep(None, 0, frames)
        self._reserve_frames(step.emulated_frames)
        session.step(frames)
        self.history.append(step)
        self._check_wall("idle step")

    def press(self, session: Any, button: str, *, duration: int, frames: int) -> None:
        """Press ``button`` and advance ``frames``, charged against the bounds."""
        if button not in _BUTTON_NAMES:
            raise ScenarioRefusal(f"unknown capture button {button!r}")
        _positive_int(duration, "press duration")
        _positive_int(frames, "press frames")
        if duration > frames:
            raise ScenarioRefusal(f"press duration {duration} exceeds the ticked frames {frames}")
        if self.inputs + 1 > self._max_inputs:
            raise CaptureBoundsExceeded(
                f"capture would exceed max_inputs ({self._max_inputs}); "
                "aborted before sending another input"
            )
        self._check_wall("input")
        step = _InputStep(button, duration, frames)
        self._reserve_frames(step.emulated_frames)
        session.press(button, duration=duration)
        session.step(frames)
        self.inputs += 1
        self.history.append(step)
        self._check_wall("input")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioRefusal(message)


def load_catalog(path: str | Path) -> dict[str, Any]:
    """Load a scenario catalog and require a non-empty ``scenarios`` list."""
    target = Path(path)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScenarioRefusal(f"catalog not found: {target}") from exc
    except json.JSONDecodeError as exc:
        raise ScenarioRefusal(f"catalog is not valid JSON: {target}: {exc}") from exc

    _require(isinstance(document, dict), "catalog root must be an object")
    scenarios = document.get("scenarios")
    _require(
        isinstance(scenarios, list) and scenarios, "catalog.scenarios must be a non-empty list"
    )
    for index, scenario in enumerate(scenarios):
        _require(isinstance(scenario, dict), f"catalog.scenarios[{index}] must be an object")
        scenario_id = scenario.get("scenario_id")
        _require(
            isinstance(scenario_id, str) and scenario_id.strip(),
            f"catalog.scenarios[{index}].scenario_id is required",
        )
    return document


def scenario_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return scenarios keyed by ID, refusing duplicate IDs."""
    index: dict[str, dict[str, Any]] = {}
    for scenario in catalog["scenarios"]:
        scenario_id = scenario["scenario_id"]
        if scenario_id in index:
            raise ScenarioRefusal(f"duplicate scenario id: {scenario_id}")
        index[scenario_id] = scenario
    return index


def find_scenario(catalog: dict[str, Any], scenario_id: str) -> dict[str, Any]:
    """Return the declared scenario or raise an explicit BLOCKED refusal."""
    index = scenario_index(catalog)
    if scenario_id not in index:
        raise ScenarioBlocked(f"required scenario {scenario_id!r} is not declared in the catalog")
    return index[scenario_id]


def scenario_status(catalog: dict[str, Any], scenario_id: str) -> str:
    """Return ``READY`` or ``BLOCKED``; ``BLOCKED`` is never a passing result."""
    try:
        find_scenario(catalog, scenario_id)
    except ScenarioBlocked:
        return "BLOCKED"
    return "READY"


def _positive_int(value: Any, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        or float(value) != int(value)
    ):
        raise ScenarioRefusal(f"{name} must be a finite positive integer")
    return int(value)


def _is_digest(value: Any, length: int) -> bool:
    """Return whether ``value`` is a lowercase hex digest of ``length`` characters."""
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_bounds(
    *,
    max_frames: Any,
    max_wall_seconds: Any,
    max_inputs: Any,
) -> dict[str, Any]:
    """Validate finite positive capture bounds and return normalized values."""
    frames = _positive_int(max_frames, "max_frames")
    inputs = _positive_int(max_inputs, "max_inputs")
    if (
        isinstance(max_wall_seconds, bool)
        or not isinstance(max_wall_seconds, (int, float))
        or not math.isfinite(float(max_wall_seconds))
        or float(max_wall_seconds) <= 0
    ):
        raise ScenarioRefusal("max_wall_seconds must be finite and greater than zero")
    return {
        "max_frames": frames,
        "max_wall_seconds": float(max_wall_seconds),
        "max_inputs": inputs,
    }


def validate_scenario_metadata(scenario: dict[str, Any], scenario_id: str) -> None:
    """Refuse incomplete or unverifiable declared metadata before any emulator work.

    A capture is only as trustworthy as the metadata it asserts and records, so
    every field the capture later reads is screened here.  Reading these fields
    with ``[]`` deep inside the drive would surface a missing key as an opaque
    ``KeyError``, and reading them with ``.get`` would silently accept a
    ``None`` boundary as an unasserted precondition.
    """
    if not isinstance(scenario, dict):
        raise ScenarioRefusal(f"scenario {scenario_id!r} is not a declared object")
    game = scenario.get("game")
    if not isinstance(game, dict) or not _is_digest(game.get("rom_sha1"), 40):
        raise ScenarioRefusal(f"scenario {scenario_id!r} must declare a pinned ROM SHA-1")
    if not _is_digest(game.get("sym_sha1"), 40):
        raise ScenarioRefusal(f"scenario {scenario_id!r} must declare a pinned symbol SHA-1")

    bounds = scenario.get("capture_bounds")
    if not isinstance(bounds, dict):
        raise ScenarioRefusal(f"scenario {scenario_id!r} must declare capture_bounds")
    validate_bounds(
        max_frames=bounds.get("max_frames"),
        max_wall_seconds=bounds.get("max_wall_seconds"),
        max_inputs=bounds.get("max_inputs"),
    )

    provenance = scenario.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("status") not in _PROVENANCE_STATUSES:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} must declare a provenance status in "
            f"{list(_PROVENANCE_STATUSES)}"
        )
    fixture = scenario.get("fixture")
    if not isinstance(fixture, dict):
        raise ScenarioRefusal(f"scenario {scenario_id!r} must declare its fixture block")
    if provenance["status"] == "verified":
        absent = [field for field in _PINNED_FIXTURE_FIELDS if not fixture.get(field)]
        if absent:
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} claims verified provenance but does not declare "
                f"{', '.join(absent)}; an unverified byte comparison must not be admitted "
                "as verified provenance"
            )
        if not _is_digest(fixture.get("sha1"), 40) or not _is_digest(fixture.get("sha256"), 64):
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} declares malformed fixture sha1/sha256 digests"
            )

    boundary = scenario.get("capture_boundary")
    if not isinstance(boundary, dict):
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} must declare capture_boundary; a capture cannot "
            "assert an undeclared boundary"
        )
    for field in ("stage", "when"):
        declared = boundary.get(field)
        if not isinstance(declared, str) or not declared.strip():
            raise ScenarioRefusal(f"scenario {scenario_id!r} must declare capture_boundary.{field}")
    map_id = boundary.get("map_id")
    if isinstance(map_id, bool) or not isinstance(map_id, int) or map_id < 0:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} must declare a non-negative integer capture_boundary.map_id"
        )
    label = boundary.get("link_state")
    if not isinstance(label, str) or label not in _LINK_STATE_CODES:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares link_state {label!r}, which has no "
            "verified wLinkState encoding; refusing to assert an unverified value"
        )


def resolve_pins(pins: Any, rom: str | Path, sym: str | Path) -> tuple[str, str]:
    """Resolve ``rom``/``sym`` against VERSIONS.md, refusing unpinned inputs."""
    rom_pin = pins.sha1_for_path(rom)
    if rom_pin is None:
        raise ScenarioRefusal(f"ROM is not pinned in VERSIONS.md: {rom}")
    sym_pin = pins.symbol_sha1_for_path(sym)
    if sym_pin is None:
        raise ScenarioRefusal(f"symbol file is not pinned in VERSIONS.md: {sym}")
    return rom_pin, sym_pin


def validate_scenario_pins(scenario: dict[str, Any], rom_pin: str, sym_pin: str) -> None:
    """Refuse when the resolved pins disagree with the declared scenario."""
    game = scenario["game"]
    if game["rom_sha1"] != rom_pin:
        raise ScenarioRefusal(
            f"resolved ROM pin {rom_pin} disagrees with scenario {game['rom_sha1']}"
        )
    if game["sym_sha1"] != sym_pin:
        raise ScenarioRefusal(
            f"resolved symbol pin {sym_pin} disagrees with scenario {game['sym_sha1']}"
        )


def verify_pinned_assets(
    rom: str | Path,
    sym: str | Path,
    rom_pin: str,
    sym_pin: str,
) -> None:
    """Refuse when the on-disk ROM/symbol assets disagree with the resolved pins.

    Pin resolution matches a documented path suffix; it does not read the
    asset. Verify the actual bytes so a missing or substituted file cannot be
    recorded in the capture plan as if it were the pinned asset.
    """
    for path, expected, label in (
        (rom, rom_pin, "ROM"),
        (sym, sym_pin, "symbol file"),
    ):
        target = Path(path)
        if not target.is_file():
            raise ScenarioRefusal(f"{label} not found: {target}")
        actual = sha1_file(target)
        if actual != expected:
            raise ScenarioRefusal(f"{label} SHA-1 mismatch: expected {expected}, got {actual}")


def sha1_file(path: str | Path) -> str:
    """Return the SHA-1 of a file using chunked reads."""
    digest = hashlib.sha1()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_declared_producer(
    scenario: dict[str, Any],
    scenario_id: str,
    repo_root: str | Path,
) -> str | None:
    """Identify the producer named by the declared provenance, or refuse.

    A verified entry must name the producer that generated its pinned bytes, and
    that producer must still be readable in the tree.  A changed, removed, or
    unspecified producer would leave a "verified" fixture whose lineage can no
    longer be reproduced, so it is refused instead of being recorded as if the
    lineage were intact.  Non-path prose (used by derived transformations) is
    recorded as-is with no digest.
    """
    provenance = scenario["provenance"]
    named = provenance.get("producer")
    if named is None:
        if provenance.get("status") == "verified":
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} claims verified provenance but names no producer"
            )
        return None
    if not isinstance(named, str) or not named.strip():
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares a non-textual provenance producer: {named!r}"
        )
    label = named.strip()
    if not label.endswith(".py") or " " in label:
        return None
    target = Path(repo_root) / label
    if not target.is_file():
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} names producer {label!r}, which is not present at "
            f"{target}; the declared lineage cannot be reproduced from this tree"
        )
    return sha1_file(target)


def validate_capture_record(record: Any, scenario_id: str) -> None:
    """Refuse a capture record that cannot be admitted as validated metadata.

    The record is the only machine-readable evidence a consumer receives, so a
    record that omits the producer, asset, boundary, or output identity is
    refused *before* the produced bytes are published: a fixture whose metadata
    cannot be admitted must not exist on disk as if it had been admitted.
    """
    if not isinstance(record, dict):
        raise ScenarioRefusal(f"capture record for {scenario_id!r} is not an object")
    absent = [field for field in _CAPTURE_RECORD_FIELDS if record.get(field) is None]
    if absent:
        raise ScenarioRefusal(f"capture record for {scenario_id!r} is missing {', '.join(absent)}")
    producer = record.get("producer")
    if not isinstance(producer, str) or not producer.strip():
        raise ScenarioRefusal(f"capture record for {scenario_id!r} does not name its producer")
    if not _is_digest(record.get("producer_sha1"), 40):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record a producer digest"
        )
    for field, label in (("rom_sha1", "ROM"), ("sym_sha1", "symbol")):
        if not _is_digest(record.get(field), 40):
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} does not record the pinned {label} digest"
            )

    output = record.get("output")
    if not isinstance(output, dict):
        raise ScenarioRefusal(f"capture record for {scenario_id!r} has no output block")
    if not isinstance(output.get("path"), str) or not output["path"].strip():
        raise ScenarioRefusal(f"capture record for {scenario_id!r} does not record the output path")
    size = output.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record a positive output size"
        )
    if not _is_digest(output.get("sha1"), 40) or not _is_digest(output.get("sha256"), 64):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record output sha1/sha256 digests"
        )

    sequence = record.get("input_sequence")
    if not isinstance(sequence, list) or any(not isinstance(step, str) for step in sequence):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record a replayable input history"
        )
    observed = record.get("observed_boundary")
    if (
        not isinstance(observed, dict)
        or not isinstance(observed.get("map_id"), int)
        or not isinstance(observed.get("link_state_raw"), int)
    ):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record the observed boundary"
        )
    reproduction = record.get("reproduction")
    if not isinstance(reproduction, dict) or not isinstance(reproduction.get("matches"), bool):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record the reproduction comparison"
        )
    for field in ("inputs_used", "frames_used"):
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ScenarioRefusal(f"capture record for {scenario_id!r} does not record {field}")
    wall_seconds = record.get("wall_seconds")
    if (
        isinstance(wall_seconds, bool)
        or not isinstance(wall_seconds, (int, float))
        or not math.isfinite(float(wall_seconds))
        or float(wall_seconds) < 0
    ):
        raise ScenarioRefusal(f"capture record for {scenario_id!r} does not record wall_seconds")


def verify_input_fixture(path: str | Path, expected_sha1: str | None) -> str:
    """Return the input SHA-1, refusing a mismatch against the declared value."""
    target = Path(path)
    if not target.is_file():
        raise ScenarioRefusal(f"input fixture not found: {target}")
    actual = sha1_file(target)
    if expected_sha1 is not None and actual != expected_sha1:
        raise ScenarioRefusal(
            f"input fixture SHA-1 mismatch: expected {expected_sha1}, got {actual}"
        )
    return actual


def ensure_output_available(output: str | Path) -> None:
    """Refuse to overwrite an existing output path."""
    if Path(output).exists():
        raise ScenarioRefusal(f"refusing to overwrite existing output: {output}")


def resolve_role(scenario: dict[str, Any], scenario_id: str, role: str | None) -> str:
    """Resolve and validate the production role for a scenario request."""
    allowed = scenario["battle"]["roles"]
    required_role: str | None = None
    if scenario_id.endswith("__listen"):
        required_role = "listen"
    elif scenario_id.endswith("__connect"):
        required_role = "connect"
    if role is None:
        role = required_role or allowed[0]
    if required_role is not None and role != required_role:
        raise ScenarioRefusal(f"scenario {scenario_id!r} requires role {required_role!r}")
    if role not in allowed:
        raise ScenarioRefusal(f"role {role!r} is not allowed for scenario {scenario_id!r}")
    return role


def write_report(path: str | Path, payload: Any) -> None:
    """Write a private capture/replay report; never called for the stub path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _symbol_byte(session: Any, name: str) -> int:
    """Read one symbol-backed byte through the session's symbol table.

    ``Session`` exposes ``symbols`` publicly but keeps the emulator memory
    private; this reads the same ``symbols``/``memory`` pair the harness's own
    observers use.  An absent symbol is a refusal, never a guessed zero.
    """
    symbols = getattr(session, "symbols", None)
    memory = getattr(getattr(session, "_pyboy", None), "memory", None)
    if symbols is None or memory is None:
        raise CapturePreconditionFailed(
            f"cannot read {name}: the capture session exposes no symbol table/memory"
        )
    if name not in symbols:
        raise CapturePreconditionFailed(
            f"cannot assert the declared boundary: {name} is absent from the loaded .sym table"
        )
    return symbols.read_u8(memory, name)


def _observe_boundary(session: Any) -> dict[str, Any]:
    """Return the observed pre-write boundary used for the precondition check."""
    state = session.read_game_state()
    return {
        "map_id": state.overworld.map_id,
        "x": state.overworld.x,
        "y": state.overworld.y,
        "link_state_raw": _symbol_byte(session, "wLinkState"),
        "party_count": state.party.count,
        "active_slot": state.party.active_slot,
    }


def _assert_boundary(scenario: dict[str, Any], observed: dict[str, Any], scenario_id: str) -> None:
    """Refuse unless the observed state matches the declared capture boundary."""
    boundary = scenario["capture_boundary"]
    if observed["map_id"] != boundary["map_id"]:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares map_id {boundary['map_id']}, "
            f"observed {observed['map_id']}"
        )
    label = boundary["link_state"]
    if label not in _LINK_STATE_CODES:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares link_state {label!r}, which has no "
            "verified wLinkState encoding; refusing to assert an unverified value"
        )
    expected_link = _LINK_STATE_CODES[label]
    if observed["link_state_raw"] != expected_link:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares link_state {label!r} "
            f"(wLinkState={expected_link}), observed wLinkState="
            f"{observed['link_state_raw']}"
        )
    party = scenario.get("party") or {}
    if party.get("count") is not None and observed["party_count"] != party["count"]:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares party.count {party['count']}, "
            f"observed {observed['party_count']}"
        )
    if party.get("active_slot") is not None and observed["active_slot"] != party["active_slot"]:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares party.active_slot {party['active_slot']}, "
            f"observed {observed['active_slot']}"
        )
    if party.get("mons"):
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares per-mon party contents, which this "
            "bounded drive does not assert; refusing to record an unchecked capture"
        )


def _drive_to_link_reception(session: Any, budget: _CaptureBudget) -> None:
    """Drive the supplied source state to the Cable Club link receptionist.

    The input sequence is the documented, replayable route from a Cerulean
    Pokecenter source state: settle, walk up to the counter row, sidestep the
    nurse, cross to the receptionist's adjacent tile, and face it.  No runtime
    RAM, party, PP, RNG, or serial state is mutated.
    """
    budget.step(session, 10)  # let the overworld settle after loading
    for _ in range(4):
        budget.press(session, "up", duration=_DEFAULT_PRESS_DURATION, frames=_DEFAULT_STEP_FRAMES)
    for _ in range(2):
        budget.press(session, "left", duration=_DEFAULT_PRESS_DURATION, frames=_DEFAULT_STEP_FRAMES)
    budget.press(session, "down", duration=_DEFAULT_PRESS_DURATION, frames=_DEFAULT_STEP_FRAMES)
    while session.read_game_state().overworld.x < _LINK_RECEPTION_TILE[0]:
        budget.press(
            session, "right", duration=_DEFAULT_PRESS_DURATION, frames=_DEFAULT_STEP_FRAMES
        )
    budget.press(session, "up", duration=_DEFAULT_PRESS_DURATION, frames=30)
    if session.read_game_state().overworld.x != _LINK_RECEPTION_TILE[0]:
        raise CapturePreconditionFailed(
            "the bounded drive did not reach the link receptionist column"
        )


def _default_session_factory(
    *,
    rom: str | Path,
    sym: str | Path,
    repo_root: str | Path,
    runtime: str,
    python: str | Path | None,
    plan: dict[str, Any],
) -> Any:
    """Open the pinned PyBoy session, or refuse when none can be established."""
    del runtime, python, plan
    try:
        from pokered_harness.config import load_versions
        from pokered_harness.session import Session
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} The pinned PyBoy runtime is not importable: {exc}"
        ) from exc
    pins = load_versions(Path(repo_root) / "VERSIONS.md")
    return Session.from_files(
        rom,
        sym,
        expected_rom_sha1=pins.sha1_for_path(rom),
        expected_symbol_sha1=pins.symbol_sha1_for_path(sym),
        expected_pyboy_version=pins.pyboy_version,
        expected_pyboy_revision=pins.pyboy_revision,
    )


def _producer_revision(repo_root: str | Path) -> str | None:
    """Best-effort enclosing commit for the produced provenance record."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    revision = result.stdout.strip()
    return revision or None


def _write_fixture_exclusive(output: Path, payload: bytes) -> None:
    """Publish fixture bytes without ever overwriting, or leaving a partial file.

    The payload is staged in a private sibling and published with ``os.link``,
    which fails if the destination already exists.  The staged file is removed
    on every exit path, so a failed capture leaves nothing behind.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(f".{output.name}.partial-{os.getpid()}-{time.monotonic_ns()}")
    try:
        descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staged, output)
        except FileExistsError as exc:
            raise ScenarioRefusal(f"refusing to overwrite existing output: {output}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(staged)


def capture_battle_scenario(
    *,
    scenario: dict[str, Any],
    rom: str | Path,
    sym: str | Path,
    input_fixture: str | Path | None,
    output: str | Path,
    role: str,
    bounds: dict[str, Any],
    report: str | Path | None,
    repo_root: str | Path,
    runtime: str,
    python: str | Path | None,
    plan: dict[str, Any],
    session_factory: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Drive a pinned ROM to the declared boundary and publish the fixture.

    Returns the private provenance record for the capture.  ``run`` merges it
    into the report it writes, so a caller never has to re-derive the input
    history, the runtime identity, or the output hashes.
    """
    del report
    scenario_id = scenario["scenario_id"]
    validate_scenario_metadata(scenario, scenario_id)
    boundary = scenario["capture_boundary"]
    if (boundary["stage"], boundary["when"]) not in (_SUPPORTED_BOUNDARY,):
        producer = scenario.get("provenance", {}).get("producer", "the documented producer")
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} The {boundary['stage']!r}/{boundary['when']!r} boundary "
            f"requires the controlled linked run produced by {producer}; a single-console "
            "bounded drive cannot establish it."
        )
    if input_fixture is None:
        raise ScenarioRefusal(
            "capture requires a source state; pass --input-fixture (the documented "
            "producer takes --source) so the drive starts from a known position"
        )

    factory = session_factory or _default_session_factory
    output_path = Path(output)
    started = clock()
    budget = _CaptureBudget(bounds, clock=clock)
    session: Any | None = None
    try:
        try:
            session = factory(
                rom=rom,
                sym=sym,
                repo_root=repo_root,
                runtime=runtime,
                python=python,
                plan=plan,
            )
        except CaptureNotAvailable:
            raise
        except (ImportError, RuntimeError, OSError) as exc:
            raise CaptureNotAvailable(
                f"{_CAPTURE_MESSAGE} The pinned runtime could not be opened: {exc}"
            ) from exc
        session.load_state(Path(input_fixture).read_bytes())
        _drive_to_link_reception(session, budget)
        observed = _observe_boundary(session)
        _assert_boundary(scenario, observed, scenario_id)
        payload = session.save_state()
    except (CaptureNotAvailable, ScenarioRefusal):
        raise
    except Exception as exc:  # fail closed: never publish bytes on an error
        raise ScenarioRefusal(
            f"bounded capture aborted before writing ({type(exc).__name__}): {exc}"
        ) from exc
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()

    declared = scenario.get("fixture") or {}
    digest_sha1 = hashlib.sha1(payload).hexdigest()
    digest_sha256 = hashlib.sha256(payload).hexdigest()
    matches = (
        declared.get("sha1") == digest_sha1
        and declared.get("sha256") == digest_sha256
        and declared.get("size_bytes") == len(payload)
    )
    verified = scenario.get("provenance", {}).get("status") == "verified"
    if verified and not matches:
        raise ScenarioRefusal(
            f"bounded capture did not reproduce the declared fixture for {scenario_id!r}: "
            f"declared sha1 {declared['sha1']} ({declared.get('size_bytes')} bytes), "
            f"produced sha1 {digest_sha1} ({len(payload)} bytes); no bytes were written"
        )

    record = {
        "producer": _CAPTURE_PRODUCER,
        "producer_sha1": sha1_file(Path(__file__)),
        "producer_revision": _producer_revision(repo_root),
        "runtime": runtime,
        "python": str(python) if python is not None else None,
        "role": role,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "wall_seconds": round(clock() - started, 6),
        "declared_bounds": bounds,
        "inputs_used": budget.inputs,
        "frames_used": budget.frames,
        "input_sequence": [step.render() for step in budget.history],
        "observed_boundary": observed,
        "rom_sha1": plan.get("rom_sha1"),
        "sym_sha1": plan.get("sym_sha1"),
        "input_fixture_sha1": plan.get("input_fixture_sha1"),
        "output": {
            "path": str(output_path),
            "size_bytes": len(payload),
            "sha1": digest_sha1,
            "sha256": digest_sha256,
        },
        "reproduction": {
            "declared_fixture_sha1": declared.get("sha1"),
            "declared_fixture_sha256": declared.get("sha256"),
            "declared_size_bytes": declared.get("size_bytes"),
            "matches": matches,
        },
    }
    validate_capture_record(record, scenario_id)
    _write_fixture_exclusive(output_path, payload)
    return record


def run(
    *,
    scenario_id: str,
    catalog: dict[str, Any],
    rom: str | Path,
    sym: str | Path,
    output: str | Path,
    repo_root: str | Path = _REPO,
    input_fixture: str | Path | None = None,
    role: str | None = None,
    report: str | Path | None = None,
    max_frames: Any = None,
    max_wall_seconds: Any = None,
    max_inputs: Any = None,
    runtime: str = "source",
    python: str | Path | None = None,
    pins: Any = None,
    capture: Callable[..., None] = capture_battle_scenario,
) -> dict[str, Any]:
    """Validate a scenario request, then invoke the bounded capture path."""
    scenario = find_scenario(catalog, scenario_id)
    validate_scenario_metadata(scenario, scenario_id)
    resolved_role = resolve_role(scenario, scenario_id, role)
    declared = scenario["capture_bounds"]
    bounds = validate_bounds(
        max_frames=declared["max_frames"] if max_frames is None else max_frames,
        max_wall_seconds=(
            declared["max_wall_seconds"] if max_wall_seconds is None else max_wall_seconds
        ),
        max_inputs=declared["max_inputs"] if max_inputs is None else max_inputs,
    )
    ensure_output_available(output)
    if pins is None:
        from pokered_harness.config import load_versions

        pins = load_versions(Path(repo_root) / "VERSIONS.md")
    rom_pin, sym_pin = resolve_pins(pins, rom, sym)
    validate_scenario_pins(scenario, rom_pin, sym_pin)
    verify_pinned_assets(rom, sym, rom_pin, sym_pin)
    provenance = scenario["provenance"]
    declared_input_sha1 = provenance.get("input_fixture_sha1")
    if provenance.get("source_fixture_id") is not None and input_fixture is None:
        raise ScenarioRefusal("scenario declares a source fixture; an input fixture is required")
    declared_producer_sha1 = verify_declared_producer(scenario, scenario_id, repo_root)
    input_sha1: str | None = None
    if input_fixture is not None:
        input_sha1 = verify_input_fixture(input_fixture, declared_input_sha1)
    plan = {
        "scenario_id": scenario_id,
        "role": resolved_role,
        "runtime": runtime,
        "python": str(python) if python is not None else None,
        "rom_sha1": rom_pin,
        "sym_sha1": sym_pin,
        "input_fixture_sha1": input_sha1,
        "declared_producer": provenance.get("producer"),
        "declared_producer_sha1": declared_producer_sha1,
        "capture_bounds": bounds,
        "output": str(output),
    }
    capture_record = capture(
        scenario=scenario,
        rom=rom,
        sym=sym,
        input_fixture=input_fixture,
        output=output,
        role=resolved_role,
        bounds=bounds,
        report=report,
        repo_root=repo_root,
        runtime=runtime,
        python=python,
        plan=plan,
    )
    if isinstance(capture_record, dict):
        plan = {**plan, "capture": capture_record}
    if report is not None:
        write_report(report, plan)
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, help="declared scenario id")
    parser.add_argument("--catalog", type=Path, default=_CATALOG_PATH)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--sym", type=Path, required=True)
    parser.add_argument("--input-fixture", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--role", choices=("listen", "connect"), default=None)
    parser.add_argument("--runtime", choices=("source", "cython"), default="source")
    parser.add_argument("--python", type=Path, default=None)
    parser.add_argument("--max-frames", type=float, default=None)
    parser.add_argument("--max-wall-seconds", type=float, default=None)
    parser.add_argument("--max-inputs", type=float, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=_REPO)
    args = parser.parse_args(argv)

    try:
        catalog = load_catalog(args.catalog)
        run(
            scenario_id=args.scenario,
            catalog=catalog,
            rom=args.rom,
            sym=args.sym,
            output=args.output,
            repo_root=args.repo_root,
            input_fixture=args.input_fixture,
            role=args.role,
            report=args.report,
            max_frames=args.max_frames,
            max_wall_seconds=args.max_wall_seconds,
            max_inputs=args.max_inputs,
            runtime=args.runtime,
            python=args.python,
        )
    except ScenarioBlocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    except CaptureNotAvailable as exc:
        print(f"capture unavailable: {exc}", file=sys.stderr)
        return 4
    except (OSError, ScenarioRefusal) as exc:
        print(f"battle scenario refused: {exc}", file=sys.stderr)
        return 2

    print(f"scenario capture complete: {args.scenario}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
