"""Produce a declared battle-scenario fixture with a bounded, fail-closed contract.

The catalog in ``release-evidence/battle-scenarios.json`` declares each scenario,
its pinned ROM/SYM, its immutable input fixture, and finite capture bounds.  This
command validates a request against that contract and refuses unsafe or
inconsistent requests *before* touching the emulator:

* an unknown or duplicate scenario is refused (an unknown required scenario is
  reported as ``BLOCKED``, never as a passing skip);
* a ROM or symbol file that is not pinned in ``VERSIONS.md`` is refused;
* an ``--input-fixture`` whose SHA-1 disagrees with the declared input is refused;
* an existing ``--output`` is never overwritten, and ``--report`` may not alias
  the output, the ``--catalog`` itself, or any input file;
* non-finite or non-positive bounds are refused, including the effective bounds
  a caller supplies directly instead of taking the declared ones.

The capture path itself is bounded and fail-closed:

* it drives the pinned ROM with an explicit, replayable input sequence and no
  runtime RAM, party, PP, RNG, or serial mutation;
* it enforces the declared ``max_frames``, ``max_wall_seconds``, and
  ``max_inputs`` bounds and aborts as soon as any of them would be exceeded,
  including when a slow step crosses the wall-clock deadline before publication;
* it asserts the declared ``capture_boundary`` (map, the link-receptionist tile,
  link state, and any declared party shape) *before* anything is written;
* it measures the executing runtime and refuses a requested runtime or named
  interpreter the process does not actually provide — comparing interpreter
  *environments* rather than resolved binaries, so a second virtual environment
  whose ``bin/python`` resolves to the same file is still refused;
* it writes the state file with ``O_EXCL`` so an existing fixture can never be
  overwritten, retains the published inode identity independently of the staging
  entry, and therefore removes exactly its own publication if any later step —
  including the finalization that removes the staging entry and the requested
  report's own publication — fails, while a competing writer's file (even a
  symlink aimed at this capture's inode) is never deleted;
* it carries one absolute deadline through record preparation, staging,
  publication, and report finalization, so a capture that drifts past
  ``max_wall_seconds`` in those phases refuses and withdraws its own artifacts
  instead of recording a shorter duration and publishing anyway;
* it records a private report with the replayable input history, the measured
  runtime identity, the producer and declared-producer identities, the
  asset/output hashes, and the reproduction comparison against the pinned fixture
  hashes;
* it refuses incomplete or unverifiable declared metadata — a missing capture
  boundary or bounds block, a non-integer map, a link-state label with no verified
  encoding, a non-positive or non-integer verified fixture size, a verified entry
  whose producer is absent or whose declared producer digest disagrees, or a
  verified entry with no pinned fixture hashes — before the emulator is opened;
* it refuses a declared opponent or prior-action precondition this bounded
  drive cannot observe, instead of recording an unchecked capture, while the
  two preconditions it *can* observe — the bag and the per-slot party records —
  are asserted against the measured boundary rather than refused;
* it validates the capture record it is about to return and admits only records
  that carry the producer, asset, boundary, and output identities within the
  effective bounds;
* a cleanup failure fails the capture and prevents publication, and an
  interruption publishes nothing, leaves no staged file, and still closes the
  emulator session.

Callers still capture real fixtures from legal ROM/SYM inputs; the recorded
hashes are what consumers pin.
"""

from __future__ import annotations

import argparse
import functools
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

    def __init__(
        self,
        bounds: dict[str, Any],
        *,
        clock: Callable[[], float],
        deadline: float | None = None,
        started: float | None = None,
    ) -> None:
        if not isinstance(bounds, dict):
            raise ScenarioRefusal("capture bounds must be a declared object")
        # The effective bounds are validated here, not merely the scenario's:
        # a caller that overrides the declared bounds must not be able to disable
        # the deadline with a non-finite value.
        self.bounds = validate_bounds(
            max_frames=bounds.get("max_frames"),
            max_wall_seconds=bounds.get("max_wall_seconds"),
            max_inputs=bounds.get("max_inputs"),
        )
        self._max_frames = self.bounds["max_frames"]
        self._max_inputs = self.bounds["max_inputs"]
        self._clock = clock
        if deadline is None:
            # Without a caller-supplied absolute deadline the bound is measured
            # from the operation's own start, so the drive and the elapsed
            # duration it records describe the same operation.
            base = clock() if started is None else started
            self._deadline = base + self.bounds["max_wall_seconds"]
        else:
            # A caller may carry one absolute deadline across the whole promised
            # operation; an unusable value is refused rather than accepted as an
            # unreachable bound.
            if not math.isfinite(float(deadline)):
                raise ScenarioRefusal("capture deadline must be finite")
            self._deadline = float(deadline)
        self.frames = 0
        self.inputs = 0
        self.history: list[_InputStep] = []

    @property
    def deadline(self) -> float:
        """The absolute wall-clock deadline this budget enforces."""
        return self._deadline

    def check_wall(self, phase: str) -> None:
        """Refuse when the wall-clock deadline has already passed."""
        self._check_wall(phase)

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
        # A press schedules the release; the advance is what must remain inside
        # the deadline, so re-check after a potentially slow ``press`` call.
        self._check_wall("input advance")
        session.step(frames)
        self.inputs += 1
        self.history.append(step)
        self._check_wall("input")


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


def _cleanup(action: Callable[[], str | None]) -> str | None:
    """Run one cleanup action in isolation, converting even an interrupt to a report.

    Cleanup runs while a primary failure is already propagating.  A cleanup
    failure -- including an interrupt handled under this cancellation contract --
    must be reported as a note instead of replacing the error the caller has to
    act on, and it must not prevent the remaining owned resources from being
    considered.
    """
    try:
        return action()
    except BaseException as exc:  # noqa: BLE001 - cleanup isolation must not mask the primary failure
        return f"cleanup was interrupted before it completed: {exc!r}"


def _withdraw_all(exc: BaseException, *publications: _OwnedPublication | None) -> None:
    """Undo every owned publication, attaching cleanup failures to ``exc``.

    The primary error always stays primary: a cleanup that cannot complete is
    reported as a note instead of replacing the failure a caller must act on,
    and one interrupted withdrawal never stops the remaining ones.
    """
    for publication in publications:
        if publication is None:
            continue
        error = _cleanup(publication.withdraw)
        if error is not None:
            _note(exc, error)


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
        # ``size_bytes`` is compared against the produced length, so a boolean or
        # numeric-string value (``True == 1``) could admit a one-byte file as a
        # verified reproduction.  Require a real positive integer, exactly as the
        # public catalog validator does: an integral float such as ``24.0`` reads
        # as the same length here but is refused there, so admitting it here would
        # make the two screens disagree about the same declaration.
        declared_size = fixture.get("size_bytes")
        if isinstance(declared_size, bool) or not isinstance(declared_size, int):
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} fixture.size_bytes must be a positive integer, "
                f"got {declared_size!r}"
            )
        _positive_int(declared_size, f"scenario {scenario_id!r} fixture.size_bytes")

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


def _repository_producer_locator(label: str) -> bool:
    """Return whether a producer locator names a readable in-tree script path."""
    if " " in label or not label.endswith(".py"):
        return False
    path = Path(label)
    return not path.is_absolute() and ".." not in path.parts


def verify_declared_producer(
    scenario: dict[str, Any],
    scenario_id: str,
    repo_root: str | Path,
) -> dict[str, Any]:
    """Resolve the declared producer identity, refusing what cannot be verified.

    A fixture's identity has three parts: the declared locator, its kind, and
    the digest actually observed here.  A declaration that pins an expected
    ``producer_sha1`` is compared against the observed bytes, so a changed or
    substituted producer is refused instead of being fingerprinted and accepted.
    A verified entry with no pinned digest is recorded with its identity
    explicitly unqualified rather than described as verified lineage, and its
    repository producer must still be readable so the capture can be replayed.
    ``partial``/``derived``/``unknown`` entries may name an external or operator
    producer that need not exist in this tree.
    """
    provenance = scenario["provenance"]
    status = provenance.get("status")
    named = provenance.get("producer")
    declared_sha1 = provenance.get("producer_sha1")
    if declared_sha1 is not None and not _is_digest(declared_sha1, 40):
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares a malformed producer_sha1 "
            f"{declared_sha1!r}; expected a 40-character lowercase hex digest"
        )
    identity: dict[str, Any] = {
        "locator": None,
        "kind": None,
        "declared_sha1": declared_sha1,
        "observed_sha1": None,
        "identity_verified": False,
    }
    if named is None:
        if status == "verified":
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} claims verified provenance but names no producer"
            )
        return identity
    if not isinstance(named, str) or not named.strip():
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares a non-textual provenance producer: {named!r}"
        )
    label = named.strip()
    declared_kind = provenance.get("producer_kind")
    if declared_kind is None:
        declared_kind = "repository" if _repository_producer_locator(label) else "derived"
    if declared_kind not in _PRODUCER_KINDS:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares producer_kind {declared_kind!r}, which is not "
            f"one of {list(_PRODUCER_KINDS)}"
        )
    identity["locator"] = label
    identity["kind"] = declared_kind
    if declared_kind == "repository":
        target = Path(repo_root) / label
        if target.is_file():
            identity["observed_sha1"] = sha1_file(target)
        elif status == "verified":
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} names producer {label!r}, which is not present at "
                f"{target}; the declared lineage cannot be reproduced from this tree"
            )
    if status == "verified":
        if declared_kind == "derived":
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} claims verified provenance from a derived "
                f"transformation {label!r}; derived bytes must be declared as derived provenance"
            )
        if declared_kind == "external" and declared_sha1 is None:
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} claims verified provenance from an external producer "
                f"{label!r} without pinning producer_sha1; the identity cannot be verified here"
            )
    observed = identity["observed_sha1"]
    if declared_sha1 is not None and observed is not None and observed != declared_sha1:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares producer_sha1 {declared_sha1} but {label!r} "
            f"hashes to {observed}; a changed producer cannot back a verified fixture"
        )
    identity["identity_verified"] = (
        status == "verified" and declared_sha1 is not None and observed == declared_sha1
    )
    return identity


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
    # The bag and per-slot party records are asserted preconditions, so a record
    # that omits the measurement behind that assertion cannot be admitted as
    # validated metadata.  ``valid`` stays tri-state: an unavailable or
    # contradictory observation is recorded, never silently defaulted.
    for field in ("bag", "party_records"):
        projection = observed.get(field)
        if not isinstance(projection, dict) or not isinstance(projection.get("observed"), bool):
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} does not record the observed {field}"
            )
        if "valid" not in projection or not (
            projection["valid"] is None or isinstance(projection["valid"], bool)
        ):
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} does not record the {field} validity"
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
    declared_bounds = record.get("declared_bounds")
    if not isinstance(declared_bounds, dict):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record the effective bounds"
        )
    try:
        effective = validate_bounds(
            max_frames=declared_bounds.get("max_frames"),
            max_wall_seconds=declared_bounds.get("max_wall_seconds"),
            max_inputs=declared_bounds.get("max_inputs"),
        )
    except ScenarioRefusal as exc:
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} records invalid effective bounds: {exc}"
        ) from exc
    for field, limit in (("frames_used", "max_frames"), ("inputs_used", "max_inputs")):
        if record[field] > effective[limit]:
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} records {field}={record[field]}, which "
                f"exceeds the effective {limit}={effective[limit]}"
            )
    if float(wall_seconds) > effective["max_wall_seconds"]:
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} records wall_seconds={wall_seconds}, which "
            f"exceeds the effective max_wall_seconds={effective['max_wall_seconds']}"
        )
    runtime = record.get("runtime")
    measurement = record.get("runtime_measurement")
    runtime_identity = record.get("runtime_identity")
    if measurement not in _RUNTIME_MEASUREMENTS:
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record whether its runtime "
            "identity was measured"
        )
    if measurement == "not_measured":
        # An injected session never inspected the capturing process, so the
        # record may carry no mode, interpreter, or identity for one: a record
        # that keeps the caller's request as if it were an observation is
        # exactly the unqualified claim this refusal exists to stop.
        if runtime != _UNMEASURED_RUNTIME:
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} records runtime {runtime!r} without "
                "measuring it"
            )
        if record.get("python") is not None:
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} records an interpreter it did not run"
            )
        if runtime_identity is not None:
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} records a runtime identity it did not measure"
            )
    else:
        # A measured capture must label itself with the mode it measured and
        # carry that same identity: a label that disagrees with the identity
        # describes a capture environment that was never observed.
        if runtime not in _RUNTIME_MODES:
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} claims a measured runtime {runtime!r}, "
                f"which is outside {list(_RUNTIME_MODES)}"
            )
        if (
            not isinstance(runtime_identity, dict)
            or runtime_identity.get("mode") != runtime
            or runtime_identity.get("measured") is not True
            or not isinstance(runtime_identity.get("executable"), str)
            or not runtime_identity["executable"].strip()
        ):
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} records an unmeasurable runtime identity"
            )


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


@dataclass(frozen=True)
class _OwnedPublication:
    """A directory entry this capture published.

    Ownership is established by the *publish operation itself*: an entry is this
    capture's only when the operation that created it returned successfully.
    Inode identity is retained as a second, independent check, because a
    competing writer can install an entry that deliberately shares this capture's
    inode (a hard link to the staging entry), so an inode match alone cannot
    establish which writer created the directory entry.  A definite failure of
    this capture's own publish -- ``EEXIST`` from ``os.link`` -- therefore never
    enters the rollback set at all, even when the destination entry happens to
    name the same inode.

    ``displaced_backup`` names the earlier entry this publication moved aside
    with one atomic rename, so a rollback can put that entry back with its
    identity and topology intact -- and, when the restore itself fails, the
    earlier bytes still exist on disk instead of surviving only in memory.

    Concurrency contract, stated honestly: this capture publishes and withdraws
    through pathnames, and POSIX pathnames provide no compare-and-swap, so no
    sequence of probes can close the window between a probe and a mutation.  What
    is guaranteed is narrower and checkable -- an entry that a probe *identifies
    as foreign* is never removed, and an entry this capture's own successful
    publish created is removed even when it is no longer reachable by name.
    An entry a competing writer installs in that window and which happens to name
    this capture's published inode is indistinguishable from this capture's own
    and may be removed; callers needing mutual exclusion must provide it outside
    this process, which is the namespace contract these artifacts are produced
    under.
    """

    path: Path
    inode: int
    device: int
    displaced_backup: Path | None = None

    def is_owned(self) -> bool | None:
        """Whether ``path`` still names exactly the entry this capture published.

        ``None`` means ownership could not be determined, which is deliberately
        not the same answer as ``False``: a failed ``lstat`` is unresolved
        cleanup that the caller must report, never a silent "somebody else's
        entry" that abandons an owned artifact without a note.

        ``lstat`` is deliberate: a competing writer's symlink pointing at this
        capture's own inode is a *different* directory entry owned by that
        writer, so following the link would delete somebody else's file.
        """
        try:
            entry = os.lstat(self.path)
        except FileNotFoundError:
            return False
        except OSError:
            return None
        return (entry.st_ino, entry.st_dev) == (self.inode, self.device)

    def withdraw(self) -> str | None:
        """Undo this publication; return a description of any cleanup failure.

        A directory entry can only be removed through its pathname, so ownership
        is probed and then *re-probed* at the point of removal: a competing
        writer that replaced ``path`` after the first probe is observed by the
        second and left exactly as it is.  A probe that cannot determine
        ownership is reported as unresolved cleanup instead of being read as
        "somebody else's entry".  When an earlier entry was moved aside,
        that earlier entry is put back through a single atomic rename, which
        preserves its inode and topology.

        Putting the displaced entry back does not depend on this capture's own
        publish having completed: an interrupt delivered *before* the publish
        rename leaves the pathname free, so the earlier entry is restored
        instead of being stranded at its backup name.  A pathname occupied by an
        entry that is not this capture's own is never overwritten to do that,
        and the retained backup is named in the returned report.

        The re-probe narrows the check/mutation window; it cannot close it, as
        the class contract above states.
        """
        seen = self.is_owned()
        if seen is None:
            seen = self.is_owned()
            if seen is None:
                return (
                    f"ownership of {self.path} could not be determined, so the entry was left "
                    f"in place rather than removed and the entry it displaced is retained at "
                    f"{self.displaced_backup}"
                )
        # Re-probe immediately before the removal: whatever a competing writer
        # installed since the first probe belongs to that writer, not to this
        # capture, and must survive this rollback untouched.
        if seen and self.is_owned() is True:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                return f"owned output {self.path} could not be removed: {exc!r}"
        # ``path`` does not name this capture's entry now: either the publish
        # never created it (an interrupt before the rename) or it has already
        # been removed.  An entry this publication moved aside still has to go
        # back, which the helper only does while the pathname is free.
        return _restore_moved_aside(self.path, self.displaced_backup)

    def commit(self) -> str | None:
        """Drop the recovery backup once this publication has been admitted.

        The backup exists so that a rollback can put the displaced entry back,
        so it must outlive every check that can still roll this publication back
        -- including the caller's final admission checks.  Once the operation has
        committed there is nothing left to restore, and the backup name is
        removed.  A backup that cannot be removed is reported instead of being
        discarded silently, and the earlier entry is still on disk at that name.
        """
        if self.displaced_backup is None:
            return None
        return _remove_staged(self.displaced_backup)


def _entry_identity(path: Path) -> tuple[int, int]:
    """Return the ``(inode, device)`` of a directory entry, without following it."""
    entry = os.lstat(path)
    return entry.st_ino, entry.st_dev


def _entry_state(path: Path) -> tuple[int, int] | None:
    """Return the ``(inode, device)`` of the entry at ``path``, or ``None`` when absent.

    ``lstat`` is deliberate: a competing writer's symlink pointing at this
    capture's own inode is a *different* directory entry owned by that writer, so
    following the link would name somebody else's file.  A pathname that cannot
    be probed at all raises ``OSError``: an unprobeable pathname is not the same
    answer as an absent one, and only the caller may decide what to do with it.
    """
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return None
    return entry.st_ino, entry.st_dev


def _remove_staged(path: Path) -> str | None:
    """Remove a staging entry; return a description of any failure."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"staged file {path} could not be removed: {exc!r}"
    return None


def _private_staging(path: Path) -> Path:
    """A private sibling name for one not-yet-final entry of this operation."""
    return path.with_name(f".{path.name}.partial-{os.getpid()}-{time.monotonic_ns()}")


def _write_staged(path: Path, payload: bytes) -> None:
    """Create ``path`` with ``payload``, mode 0600 and fsynced, never overwriting."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _encode_report(payload: Any) -> bytes:
    """Serialize a report payload with this module's stable formatting."""
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _restore_displaced(target: Path, backup: Path) -> tuple[bool, str | None]:
    """Put the entry this capture moved aside back at ``target``.

    The entry is published through a private staging name and one atomic rename,
    and that staging name is a hard link to the moved-aside entry, so a restored
    symlink stays a symlink and a restored hard link keeps naming the same inode.
    Because the staging name is a hard link rather than a copy, that rename leaves
    no removable recovery copy of its own behind.

    Returns ``(restored, error)``: whether the entry is in place again, and a
    description of anything that went wrong.  The two are reported separately
    because dropping the now-duplicate backup name is bookkeeping -- when that
    step alone fails the entry *is* restored, and claiming otherwise would
    misdescribe the artifact the next reader has to reconcile.
    """
    staged = _private_staging(target)
    try:
        os.link(backup, staged, follow_symlinks=False)
    except OSError as exc:
        return False, f"the earlier entry could not be staged for restoration ({exc!r})"
    try:
        os.replace(staged, target)
    except OSError as exc:
        staged_error = _cleanup(lambda: _remove_staged(staged))
        message = f"the earlier entry could not be renamed back ({exc!r})"
        return False, (message if staged_error is None else f"{message}; {staged_error}")
    cleanup_error = _remove_staged(backup)
    if cleanup_error is None:
        return True, None
    detail = (
        f"{cleanup_error}; the earlier entry is in place at {target} and {backup} is a "
        "second name for the same entry"
    )
    return True, detail


def _restore_moved_aside(target: Path, backup: Path | None) -> str | None:
    """Put a moved-aside entry back when nothing was published over it."""
    if backup is None:
        return None
    try:
        if _entry_state(backup) is None:
            # The move-aside itself failed, so the earlier entry never left
            # ``target`` and there is nothing to put back.
            return None
        if _entry_state(target) is not None:
            return (
                f"the entry this capture moved aside for {target} was not restored because "
                f"another entry now occupies that pathname; it is retained at {backup}"
            )
    except OSError as exc:
        return (
            f"the entry moved aside for {target} could not be restored because a pathname "
            f"could not be probed ({exc!r}); it is retained at {backup}"
        )
    restored, error = _restore_displaced(target, backup)
    if error is None:
        return None
    if restored:
        return f"the entry moved aside for {target} was restored, but {error}"
    return (
        f"replaced entry {target} could not be restored from {backup}: {error}; "
        f"the earlier entry is retained at {backup}"
    )


def write_report(path: str | Path, payload: Any) -> _OwnedPublication:
    """Write a private capture/replay report atomically.

    The report is staged in a private sibling and moved into place, so a failed
    or partial write never replaces an existing report with something partial and
    never leaves a partial file behind.

    An entry this call replaces is moved aside by one atomic rename to a durable
    private backup before anything is published over it, so its identity and
    topology survive: a displaced symlink stays a symlink and a displaced hard
    link keeps naming the same inode.  An existing report whose contents cannot
    be read is refused before anything is moved, because displacing it would lose
    a report this operation cannot account for.  The returned publication can put
    the displaced entry back, and a restore that fails leaves that entry on disk
    at its backup name instead of discarding the only recoverable copy.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = _private_staging(target)
    backup: Path | None = None
    publication: _OwnedPublication | None = None
    try:
        if _entry_state(target) is not None:
            # A report target is deliberately replaceable, but the entry it
            # replaces must be *secured* first.  An unreadable prior report is a
            # refusal: displacing it would lose it outright if a later step
            # failed, and there is no need to destroy it to make progress.
            try:
                target.read_bytes()
            except OSError as exc:
                raise ScenarioRefusal(
                    f"refusing to replace the existing report {target}: its current contents "
                    f"could not be read ({exc!r}) and so could not be preserved"
                ) from exc
            backup = target.with_name(
                f".{target.name}.displaced-{os.getpid()}-{time.monotonic_ns()}"
            )
            # One atomic rename, not a byte copy: the displaced entry keeps its
            # inode, its symlink target, and its hard-link relationships.
            os.replace(target, backup)
        _write_staged(staged, _encode_report(payload))
        # Retain the identity before the rename: an interrupt delivered inside
        # ``os.replace`` (after the entry exists, before it returns) must still
        # be recognisable as this capture's own publication.
        publication = _OwnedPublication(target, *_entry_identity(staged), backup)
        os.replace(staged, target)
        return publication
    except BaseException as exc:
        if publication is None:
            # Nothing was published over the entry this call moved aside, so it
            # is put back (a no-op when the move-aside itself never happened).
            # A restore that cannot complete leaves it at the backup name.
            restore_error = _cleanup(lambda: _restore_moved_aside(target, backup))
            if restore_error is not None:
                _note(exc, restore_error)
        else:
            publication_error = _cleanup(publication.withdraw)
            if publication_error is not None:
                _note(exc, publication_error)
        staging_error = _cleanup(lambda: _remove_staged(staged))
        if staging_error is not None:
            _note(exc, staging_error)
        raise


def republish_report(
    target: str | Path, payload: Any, current: _OwnedPublication
) -> _OwnedPublication:
    """Atomically replace the report this capture just published.

    Used for the final admission stamp: the value that has to be persisted is
    only observable once the report is already in place.  The entry displaced
    here is this capture's own report from a moment ago, so no recovery backup is
    taken for it; the successor instead inherits ``current``'s displaced-entry
    backup, so a rollback after this rename still puts the earlier report back.
    Either publication may own ``target`` depending on where an interruption
    lands, so a failure withdraws both: withdrawing the one that does not own
    ``target`` is a harmless no-op.
    """
    path = Path(target)
    staged = _private_staging(path)
    _write_staged(staged, _encode_report(payload))
    successor: _OwnedPublication | None = None
    try:
        successor = _OwnedPublication(path, *_entry_identity(staged), current.displaced_backup)
        os.replace(staged, path)
        return successor
    except BaseException as exc:
        _withdraw_all(exc, current, successor)
        staging_error = _cleanup(lambda: _remove_staged(staged))
        if staging_error is not None:
            _note(exc, staging_error)
        raise


def _restamp_capture_wall_seconds(plan: dict[str, Any], elapsed: float) -> bool:
    """Stamp the final elapsed seconds into ``plan``; report whether it changed."""
    record = plan.get("capture")
    if not isinstance(record, dict) or record.get("wall_seconds") == elapsed:
        return False
    record["wall_seconds"] = elapsed
    return True


def refuse_report_alias(report: str | Path, *protected: str | Path | None) -> None:
    """Refuse a report path that resolves to an input or to the output fixture."""
    report_path = Path(report)
    report_resolved = report_path.resolve(strict=False)
    for other in protected:
        if other is None:
            continue
        other_path = Path(other)
        alias = report_resolved == other_path.resolve(strict=False)
        if not alias and report_path.exists() and other_path.exists():
            try:
                alias = os.path.samefile(report_path, other_path)
            except OSError:  # pragma: no cover - a racing removal is not an alias
                alias = False
        if alias:
            raise ScenarioRefusal(
                f"refusing to write the report to {report_path}, which is the same file as "
                f"{other_path}; the report must not overwrite an input or the output fixture"
            )


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


def _bag_digest(stacks: list[dict[str, int]]) -> str:
    """Digest the observed stack list, never the raw save-state bytes."""
    canonical = json.dumps(
        [[stack["item_id"], stack["quantity"]] for stack in stacks],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _observe_bag(bag: Any) -> dict[str, Any]:
    """Sanitized projection of the parsed bag at the declared boundary.

    ``valid`` keeps the parser's tri-state: ``None`` means the observation was
    unavailable and must not be read as an observed bag, and a ``False`` bag is
    contradictory RAM.  Only item ids and quantities are projected; no raw
    save-state bytes are recorded.  The digest is published only for a bag that
    was actually observed as valid.
    """
    if bag is None:
        return {
            "observed": False,
            "valid": None,
            "count": None,
            "stacks": [],
            "digest": None,
        }
    stacks = [
        {"item_id": int(stack.item_id), "quantity": int(stack.quantity)} for stack in bag.stacks
    ]
    valid = None if bag.valid is None else bool(bag.valid)
    return {
        "observed": True,
        "valid": valid,
        "count": int(bag.count),
        "stacks": stacks,
        "digest": _bag_digest(stacks) if valid is True else None,
    }


def _observe_party_records(records: Any) -> dict[str, Any]:
    """Sanitized per-slot party records at the declared boundary.

    Each slot is projected through ``PartyRecord.to_resource_dict()``, which
    deliberately omits the raw 44-byte record, so a published capture record
    can never carry ROM-derived party bytes.  Slots are projected only when the
    read actually produced a valid party.
    """
    if records is None:
        return {"observed": False, "valid": None, "count": None, "slots": []}
    valid = None if records.valid is None else bool(records.valid)
    slots: list[dict[str, Any]] = []
    if valid is True:
        for record in records.records:
            projection = record.to_resource_dict()
            slots.append(
                {
                    "slot": int(projection["slot"]),
                    "species": int(projection["species"]),
                    "level": int(projection["level"]),
                    "record_size": int(projection["record_size"]),
                    "digest": str(projection["digest"]),
                }
            )
    return {
        "observed": True,
        "valid": valid,
        "count": None if records.count is None else int(records.count),
        "slots": slots,
    }


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
        "bag": _observe_bag(state.bag),
        "party_records": _observe_party_records(session.read_party_records()),
    }


def _assert_boundary(scenario: dict[str, Any], observed: dict[str, Any], scenario_id: str) -> None:
    """Refuse unless the observed state matches the declared capture boundary."""
    boundary = scenario["capture_boundary"]
    if observed["map_id"] != boundary["map_id"]:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares map_id {boundary['map_id']}, "
            f"observed {observed['map_id']}"
        )
    if (boundary["stage"], boundary["when"]) == _SUPPORTED_BOUNDARY and (
        observed["x"],
        observed["y"],
    ) != _LINK_RECEPTION_TILE:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares the link-reception pre-command boundary, but the "
            f"observed position ({observed['x']}, {observed['y']}) is not the link receptionist "
            f"tile {_LINK_RECEPTION_TILE}"
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
    _assert_declared_inventory(scenario.get("inventory"), observed["bag"], scenario_id)
    _assert_declared_party_mons(party.get("mons"), observed["party_records"], scenario_id)


def _render_stacks(stacks: list[tuple[int, int]]) -> str:
    """Render observed/declared stacks compactly for a refusal message."""
    if not stacks:
        return "an empty bag"
    return ", ".join(f"item {item_id:02x} x{quantity}" for item_id, quantity in stacks)


def _assert_declared_inventory(declared: Any, observed: dict[str, Any], scenario_id: str) -> None:
    """Assert a declared bag against the stacks measured at the boundary.

    A declared inventory is a complete assertion: the declared and observed
    ``(item, quantity)`` multisets must agree exactly, so a declaration that
    names only part of the observed bag is refused rather than read as an
    unchecked remainder.  ``inventory: null`` asserts nothing, and
    ``inventory: []`` asserts that the bag is empty.

    This is the failure mode the previous refusal existed to prevent: a
    declaration that could not be checked must still never publish.  It is
    checked here instead, because the bag *is* observable at this boundary
    (``read_game_state().bag``), and an unobservable bag is refused rather than
    assumed.
    """
    if declared is None:
        return
    if observed.get("valid") is not True:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares an inventory precondition, but the bag at the "
            "declared boundary was not observed as valid; refusing to publish an unchecked capture"
        )
    declared_stacks = sorted((int(item["item_id"]), int(item["quantity"])) for item in declared)
    observed_stacks = sorted(
        (int(stack["item_id"]), int(stack["quantity"])) for stack in observed["stacks"]
    )
    if declared_stacks != observed_stacks:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares inventory {_render_stacks(declared_stacks)}, "
            f"observed {_render_stacks(observed_stacks)} "
            f"(digest {observed['digest']})"
        )


def _assert_declared_party_mons(declared: Any, observed: dict[str, Any], scenario_id: str) -> None:
    """Assert declared per-slot party contents against the measured records.

    A declared member list is a complete assertion of the occupied party: it
    must name exactly the occupied slots once each, so a partial or duplicated
    declaration cannot leave a member unasserted.  Species and level are always
    compared; the record digest is compared when it is declared, which is what
    makes two same-species members distinguishable without publishing the raw
    44-byte records.
    """
    if not declared:
        return
    if observed.get("valid") is not True:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares per-mon party contents, but the party records at "
            "the declared boundary were not observed as valid; refusing to publish an unchecked "
            "capture"
        )
    slots = observed["slots"]
    occupied = list(range(len(slots)))
    declared_slots = sorted(int(mon["slot"]) for mon in declared)
    if declared_slots != occupied:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares party slots {declared_slots}, but the observed "
            f"party occupies {occupied}"
        )
    for mon in declared:
        slot = int(mon["slot"])
        measured = slots[slot]
        if int(mon["species"]) != measured["species"] or int(mon["level"]) != measured["level"]:
            raise CapturePreconditionFailed(
                f"scenario {scenario_id!r} declares party slot {slot} as species "
                f"{mon['species']} level {mon['level']}, observed species {measured['species']} "
                f"level {measured['level']}"
            )
        declared_digest = mon.get("digest")
        if declared_digest is not None and str(declared_digest) != measured["digest"]:
            raise CapturePreconditionFailed(
                f"scenario {scenario_id!r} declares party slot {slot} record digest "
                f"{declared_digest}, observed {measured['digest']}"
            )


def _assert_supported_conditions(scenario: dict[str, Any], scenario_id: str) -> None:
    """Refuse declared preconditions this bounded drive cannot observe.

    Silently driving past a declared opponent or prior action would publish a
    fixture whose declared preconditions were never checked, so for those two
    fields only declarations that assert nothing are accepted: an *absent*
    field (or an explicit ``null``) for the opponent, and an empty
    ``required_prior_actions`` list.  A declared opponent is refused even when
    it is empty, because the field's presence is itself the assertion that this
    drive cannot check.

    A declared ``inventory`` and a declared ``party.mons`` list are *asserted*,
    not refused: both are read from the loaded state at the declared boundary
    and compared before anything is published (see
    ``_assert_declared_inventory`` and ``_assert_declared_party_mons``).  This
    screen therefore only requires that a declaration is shaped so it *can* be
    asserted — a list of objects carrying the integer fields the comparison
    reads — so that a malformed declaration is refused before the emulator is
    opened rather than surfacing as an opaque ``KeyError`` at the boundary.
    """
    inventory = scenario.get("inventory")
    if inventory is not None:
        _require_declared_list(inventory, "inventory", scenario_id)
        for index, item in enumerate(inventory):
            for field in ("item_id", "quantity"):
                _require_declared_int(item.get(field), f"inventory[{index}].{field}", scenario_id)
    if scenario.get("opponent") is not None:
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} Scenario {scenario_id!r} declares an opponent precondition "
            "that this bounded drive does not observe; capture it with a producer that asserts "
            "the declared opponent, or declare the opponent as null."
        )
    prior_actions = scenario.get("required_prior_actions")
    if not isinstance(prior_actions, list):
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} must declare required_prior_actions as a list"
        )
    if prior_actions:
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} Scenario {scenario_id!r} declares required prior actions that "
            "this bounded drive does not replay; capture it with a producer that performs and "
            "records them, or declare an empty list."
        )
    party = scenario.get("party") or {}
    mons = party.get("mons")
    if mons:
        _require_declared_list(mons, "party.mons", scenario_id)
        for index, mon in enumerate(mons):
            for field in ("slot", "species", "level"):
                _require_declared_int(mon.get(field), f"party.mons[{index}].{field}", scenario_id)
            digest = mon.get("digest")
            if digest is not None and not _is_digest(digest, 64):
                raise ScenarioRefusal(
                    f"scenario {scenario_id!r} declares party.mons[{index}].digest as a value "
                    "that is not a SHA-256 digest"
                )


def _require_declared_list(value: Any, field: str, scenario_id: str) -> None:
    """Require a declared precondition list to be assertable objects."""
    if not isinstance(value, list) or any(not isinstance(entry, dict) for entry in value):
        raise ScenarioRefusal(f"scenario {scenario_id!r} must declare {field} as a list of objects")


def _require_declared_int(value: Any, field: str, scenario_id: str) -> None:
    """Require a declared precondition field to be a real integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares {field} as {value!r}, which is not an integer"
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
    position = session.read_game_state().overworld
    if (position.x, position.y) != _LINK_RECEPTION_TILE:
        raise CapturePreconditionFailed(
            f"the bounded drive ended at ({position.x}, {position.y}), not the link "
            f"receptionist tile {_LINK_RECEPTION_TILE}"
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
    """Open the pinned PyBoy session, or refuse when none can be established.

    ``runtime`` and ``python`` are not consumed here: the caller measures the
    executing runtime and refuses a mismatch before this factory runs, so the
    requested labels can never be recorded as if they had been observed.
    """
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


def _pyboy_cython_flag() -> bool:
    """Return the executing PyBoy's compiled-extension flag."""
    import pyboy.utils

    return bool(getattr(pyboy.utils, "cython_compiled", False))


def _pyboy_module_version() -> str | None:
    """Return the version of the *imported* PyBoy module, or ``None``."""
    import pyboy

    for attribute in ("__version__", "VERSION"):
        value = getattr(pyboy, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _pyboy_module_revision() -> str | None:
    """Return the build revision of the *imported* PyBoy module, or ``None``."""
    import pyboy
    import pyboy.utils

    for module in (pyboy, pyboy.utils):
        for attribute in ("__pokered_harness_revision__", "__revision__", "revision"):
            value = getattr(module, attribute, None)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _interpreter_environment(path: Path) -> str:
    """Return the environment root a ``<environment>/bin/python`` path belongs to.

    ``.venv-source/bin/python`` and ``.venv-native/bin/python`` can both be
    symlinks to the same ``/usr/bin/python3.11``, so the resolved binary cannot
    tell two environments apart.  The directory above ``bin`` is what identifies
    the environment, exactly as ``sys.prefix`` does for the running process.
    """
    return str(path.parent.parent)


def measure_runtime_identity(
    *,
    runtime: str,
    python: str | Path | None,
    repo_root: str | Path,
) -> dict[str, Any]:
    """Measure the executing runtime and enforce the requested labels.

    A caller's ``runtime``/``python`` arguments are requests, not evidence.  The
    mode is read from the imported PyBoy build, the interpreter *environment*
    from the running process, and the recorded module versions from the imported
    modules; a request that disagrees is refused instead of being copied into a
    record as if it had been observed.
    """
    if runtime not in _RUNTIME_MODES:
        raise ScenarioRefusal(f"runtime must be one of {list(_RUNTIME_MODES)}, got {runtime!r}")
    try:
        compiled = _pyboy_cython_flag()
        observed_version = _pyboy_module_version()
        observed_revision = _pyboy_module_revision()
    except ImportError as exc:
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} The pinned PyBoy runtime is not importable: {exc}"
        ) from exc
    mode = "cython" if compiled else "source"
    if runtime != mode:
        raise ScenarioRefusal(
            f"requested runtime {runtime!r} but the executing PyBoy reports {mode!r}; refusing "
            "to record a runtime this process does not provide"
        )
    executable = str(sys.executable)
    environment = str(sys.prefix)
    if python is not None:
        requested = Path(python)
        # Existence is checked first: a path that cannot be resolved at all is
        # refused before any environment comparison could accidentally accept it.
        if not requested.is_file():
            raise ScenarioRefusal(
                f"requested interpreter {python} does not exist; refusing to record an "
                "interpreter this process cannot resolve"
            )
        requested_environment = _interpreter_environment(requested)
        if requested_environment != environment:
            raise ScenarioRefusal(
                f"requested interpreter {python} belongs to the environment "
                f"{requested_environment!r}, which is not the executing interpreter "
                f"environment {environment!r} ({executable}); refusing to record an interpreter "
                "this process does not run in, even when both binaries resolve to the same file"
            )
        # The environment directory is not interpreter identity on its own: an
        # unrelated executable that merely lives in the same ``bin`` directory
        # would otherwise be recorded as the interpreter this capture ran, while
        # the record's own executable fields name the real process.
        executing = Path(sys.executable)
        try:
            requested_resolved = requested.resolve(strict=True)
        except OSError as exc:
            raise ScenarioRefusal(
                f"requested interpreter {python} could not be resolved: {exc!r}"
            ) from exc
        try:
            executing_resolved = executing.resolve(strict=True)
        except OSError:  # pragma: no cover - the running interpreter always resolves
            executing_resolved = executing
        if requested_resolved != executing_resolved:
            raise ScenarioRefusal(
                f"requested interpreter {python} resolves to {requested_resolved}, which is not "
                f"the executing interpreter {executable} ({executing_resolved}); refusing to "
                "record an interpreter this process does not run"
            )
    from pokered_harness.config import load_versions

    pins = load_versions(Path(repo_root) / "VERSIONS.md")
    return {
        "mode": mode,
        "executable": str(Path(sys.executable).resolve()),
        "executable_path": executable,
        "environment": environment,
        "base_environment": str(sys.base_prefix),
        "isolated_environment": environment != str(sys.base_prefix),
        "requested_python": str(python) if python is not None else None,
        "python_version": sys.version.split()[0],
        "pyboy_version": pins.pyboy_version,
        "pyboy_revision": pins.pyboy_revision,
        "pyboy_version_observed": observed_version,
        "pyboy_revision_observed": observed_revision,
        "measured": True,
    }


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


def _write_fixture_exclusive(
    output: Path, payload: bytes, *, guard: Callable[[str], None] | None = None
) -> _OwnedPublication:
    """Publish fixture bytes without ever overwriting, or leaving a partial file.

    The payload is staged in a private sibling and published with ``os.link``,
    which fails if the destination already exists.  Every step — including the
    removal of the staging entry that finalizes the operation — runs inside the
    rollback-protected region.

    Ownership combines the publish outcome with inode identity.  The inode is
    retained before the link, so a failure delivered once the link returned --
    including one inside ``os.link`` itself -- still removes this capture's own
    publication rather than abandoning it.  A ``FileExistsError`` is the one
    definite proof that this capture did *not* create the destination entry: a
    competing writer can hard-link the staging file itself, so a matching inode
    must not be read as ownership, and the refusal leaves that entry untouched.
    It is converted to an ordinary no-overwrite refusal.

    ``guard`` is called immediately before the link and again once the entry
    exists, so a caller's deadline is re-checked with the publication in place
    and a late expiry rolls it back instead of admitting it.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = _private_staging(output)
    publication: _OwnedPublication | None = None
    try:
        _write_staged(staged, payload)
        if guard is not None:
            guard("fixture staging")
        # Retained before the link: once the entry exists the staging path may
        # already be gone, so ownership must not be re-derived from that entry.
        publication = _OwnedPublication(output, *_entry_identity(staged))
        try:
            os.link(staged, output)
        except FileExistsError as exc:
            # A definite ``EEXIST`` proves this capture did not create the
            # destination entry, even when it names this capture's own staged
            # inode: a competing writer can hard-link that inode itself.  The
            # entry therefore never enters the rollback set.
            publication = None
            raise ScenarioRefusal(f"refusing to overwrite existing output: {output}") from exc
        if guard is not None:
            guard("fixture publication")
        # Finalization: with the staging entry gone the operation has committed.
        os.unlink(staged)
        return publication
    except BaseException as exc:
        _withdraw_all(exc, publication)
        staging_error = _cleanup(lambda: _remove_staged(staged))
        if staging_error is not None:
            _note(exc, staging_error)
        raise


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
    deadline: float | None = None,
    ownership: dict[str, Any] | None = None,
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
    _assert_supported_conditions(scenario, scenario_id)
    if input_fixture is None:
        raise ScenarioRefusal(
            "capture requires a source state; pass --input-fixture (the documented "
            "producer takes --source) so the drive starts from a known position"
        )

    factory = session_factory or _default_session_factory
    # An injected session is diagnostic: it never inspects the capturing process,
    # so the record must say its runtime was not measured instead of adopting the
    # caller's request as an observation.
    measured = session_factory is None
    if not measured and python is not None and not Path(python).is_file():
        # A diagnostic session is still handed a named interpreter to run; a
        # request that cannot be resolved at all is refused rather than recorded
        # as if it had been used.
        raise ScenarioRefusal(
            f"requested interpreter {python} does not exist; refusing to record an "
            "interpreter this process cannot resolve"
        )
    if measured and isinstance(plan, dict):
        # Measure and enforce the executing identity *before* the emulator opens,
        # so a requested runtime or interpreter is never recorded as observed.
        plan["runtime_identity"] = measure_runtime_identity(
            runtime=runtime, python=python, repo_root=repo_root
        )
    output_path = Path(output)
    # The operation starts before runtime measurement, so the recorded duration
    # covers measurement as well as the drive it produces.  ``run`` supplies its
    # own start so that one operation start governs both the elapsed duration and
    # the absolute deadline; a direct caller starts its own operation here.
    supplied_start = ownership.get("started") if ownership is not None else None
    started = (
        float(supplied_start)
        if isinstance(supplied_start, (int, float)) and not isinstance(supplied_start, bool)
        else clock()
    )
    budget = _CaptureBudget(bounds, clock=clock, deadline=deadline, started=started)
    session: Any | None = None
    failure: BaseException | None = None
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
        budget.check_wall("boundary observation")
        _assert_boundary(scenario, observed, scenario_id)
        budget.check_wall("state save")
        payload = session.save_state()
        budget.check_wall("state save")
    except (CaptureNotAvailable, ScenarioRefusal) as exc:
        failure = exc
        raise
    except Exception as exc:  # fail closed: never publish bytes on an error
        refusal = ScenarioRefusal(
            f"bounded capture aborted before writing ({type(exc).__name__}): {exc}"
        )
        failure = refusal
        raise refusal from exc
    except BaseException as exc:  # an interrupt still owns a session to close
        failure = exc
        raise
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as close_exc:
                if failure is None:
                    # An otherwise successful capture whose teardown failed has
                    # not established bounded cleanup, so it must not publish.
                    raise ScenarioRefusal(
                        f"capture teardown failed for {scenario_id!r} and the owned session was "
                        f"not closed; no bytes were written: {close_exc}"
                    ) from close_exc
                _note(failure, f"session.close() also failed: {close_exc!r}")

    effective_bounds = budget.bounds
    runtime_identity = plan.get("runtime_identity") if isinstance(plan, dict) else None
    declared = scenario.get("fixture") or {}
    # Every slow step below is bracketed by a deadline re-check, so a capture that
    # drifts past the declared bound while hashing, looking up the producer
    # revision, or staging the bytes refuses instead of publishing anyway.
    budget.check_wall("fixture digest")
    digest_sha1 = hashlib.sha1(payload).hexdigest()
    digest_sha256 = hashlib.sha256(payload).hexdigest()
    budget.check_wall("fixture digest")
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

    recorded_runtime = (
        runtime_identity["mode"] if measured and runtime_identity else _UNMEASURED_RUNTIME
    )
    record = {
        "producer": _CAPTURE_PRODUCER,
        "producer_sha1": sha1_file(Path(__file__)),
        "producer_revision": _producer_revision(repo_root),
        "runtime": recorded_runtime,
        "runtime_measurement": "measured" if measured else "not_measured",
        "runtime_identity": runtime_identity,
        "runtime_request": {
            "runtime": runtime,
            "python": str(python) if python is not None else None,
        },
        "python": str(python) if measured and python is not None else None,
        "role": role,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "wall_seconds": round(clock() - started, 6),
        "declared_bounds": effective_bounds,
        "declared_producer": plan.get("producer_identity") if isinstance(plan, dict) else None,
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
    budget.check_wall("record preparation")
    validate_capture_record(record, scenario_id)
    publication: _OwnedPublication | None = None
    try:
        publication = _write_fixture_exclusive(output_path, payload, guard=budget.check_wall)
        budget.check_wall("fixture publication")
        # The final admission sits inside the rollback region: interrupting this
        # sample still withdraws the entry this capture published rather than
        # abandoning it as an admitted fixture.
        sampled = clock()
        elapsed = round(sampled - started, 6)
        if sampled >= budget.deadline:
            raise CaptureBoundsExceeded(
                "capture exceeded max_wall_seconds before admitting the fixture; "
                "the declared bound may only be raised with a recorded justification"
            )
        if elapsed > effective_bounds["max_wall_seconds"]:
            raise CaptureBoundsExceeded(
                f"capture exceeded max_wall_seconds ({effective_bounds['max_wall_seconds']}) before "
                f"admitting the fixture; observed {elapsed}s"
            )
        record["wall_seconds"] = elapsed
        if ownership is not None:
            ownership["publication"] = publication
        return record
    except BaseException as exc:
        _withdraw_all(exc, publication)
        raise


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
    catalog_path: str | Path | None = None,
    capture: Callable[..., None] = capture_battle_scenario,
    clock: Callable[[], float] = time.monotonic,
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
    if report is not None:
        # The catalog is an input as well: a report written over it would replace
        # the very declarations this capture was validated against.
        refuse_report_alias(report, catalog_path, output, rom, sym, input_fixture)
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
    producer_identity = verify_declared_producer(scenario, scenario_id, repo_root)
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
        "producer_identity": producer_identity,
        "capture_bounds": bounds,
        "output": str(output),
    }
    # One operation start and one absolute deadline govern every later admission
    # path, and the capture is told about that start so the duration it records
    # and the bound it is held to describe the same operation.
    operation_start = clock()
    deadline = operation_start + bounds["max_wall_seconds"]
    ownership: dict[str, Any] = {"started": operation_start}
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
        deadline=deadline,
        ownership=ownership,
        clock=clock,
    )
    if isinstance(capture_record, dict):
        plan = {**plan, "capture": capture_record}
    fixture_publication: _OwnedPublication | None = ownership.get("publication")
    if report is not None:
        guard = _deadline_guard(deadline, clock)
        report_publication: _OwnedPublication | None = None
        try:
            guard("report preparation")
            report_publication = write_report(report, plan)
            guard("report finalization")
            # The report's own publication is promised work.  Sample the final
            # elapsed time once the entry is durable and stamp it into the
            # returned record; the persisted copy is replaced only when that
            # value differs from the one already written.
            final_sample = clock()
            if final_sample >= deadline:
                raise CaptureBoundsExceeded(
                    f"capture exceeded max_wall_seconds ({bounds['max_wall_seconds']}) while "
                    "finalizing the requested report; refusing to admit the pair"
                )
            if _restamp_capture_wall_seconds(plan, round(final_sample - operation_start, 6)):
                guard("report admission")
                report_publication = republish_report(report, plan, report_publication)
            # The report is now admitted, so the recovery backup of the entry it
            # displaced has outlived every check that could roll it back.
            uncommitted = report_publication.commit()
            if uncommitted is not None:
                plan["publication_cleanup"] = [uncommitted]
        except BaseException as exc:
            # A requested report that could not be written must not leave the
            # fixture behind as if the capture had been fully recorded, and a
            # report this capture published must not survive a rolled-back
            # fixture.  Only entries this capture owns are ever withdrawn.
            _withdraw_all(exc, report_publication, fixture_publication)
            raise
    else:
        try:
            # No report was requested, but the same absolute deadline still
            # governs final admission of the fixture.
            final_sample = clock()
            if final_sample >= deadline:
                raise CaptureBoundsExceeded(
                    f"capture exceeded max_wall_seconds ({bounds['max_wall_seconds']}) before "
                    "admitting the capture; refusing to admit the pair"
                )
            _restamp_capture_wall_seconds(plan, round(final_sample - operation_start, 6))
        except BaseException as exc:
            _withdraw_all(exc, fixture_publication)
            raise
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
            catalog_path=args.catalog,
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
