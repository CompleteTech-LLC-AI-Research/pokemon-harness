"""Inspection-only checkpoints for a locally paired native PyBoy runtime.

This helper deliberately has a narrow contract.  It never advances either
endpoint, sends input, writes emulated RAM, detaches a pair, or attempts to
make a save-state replayable.  The two native save streams and their metadata
are collected while the canonical endpoint owners and the link-provider lock
are held.  Filesystem I/O happens only after those locks have been released.

The module lives under ``tests`` because it is a diagnostic aid for operator
managed real-ROM runs.  It must not become part of the runtime API or cause
ROM-derived artifacts to enter the repository.
"""

from __future__ import annotations

import io
import json
import os
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any

from pokered_harness.ownership import EmulatorOwnershipError, owner_for, owner_group


class PairCheckpointError(RuntimeError):
    """The pair was not at a safe, settled inspection boundary."""


@dataclass(frozen=True)
class _PairSnapshot:
    """In-memory capture assembled before any filesystem operation."""

    frame_ordinal: int
    state_blobs: tuple[bytes, bytes]
    endpoint_metadata: tuple[dict[str, Any], dict[str, Any]]
    provider_metadata: dict[str, Any]
    caller_metadata: dict[str, Any]


_SERIAL_FIELDS = (
    "SB",
    "SC",
    "transfer_enabled",
    "internal_clock",
    "double_speed",
    "last_cycles",
    "_cycles_to_interrupt",
    "clock",
    "clock_target",
    "_shift_register",
    "_bits_remaining",
    "transfer_generation",
    "backend_failed",
)

_BACKEND_COUNTERS = (
    "edge_count",
    "peer_unarmed_edges",
    "peer_master_edges",
    "peer_rearm_attempts",
    "peer_rearm_successes",
)

_MISSING = object()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _json_safe(value: object) -> object:
    """Convert diagnostic values without retaining native/runtime objects."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    # A repr is safer than leaking an object graph (or a ROM-derived buffer)
    # into the JSON manifest.  This is only a diagnostic description.
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _optional_int(value: object) -> int | None:
    return int(value) if _is_int(value) else None


def _optional_bool_attribute(instance: object, name: str) -> bool | None:
    value = getattr(instance, name, _MISSING)
    return None if value is _MISSING else bool(value)


def _optional_presence(instance: object, name: str) -> bool | None:
    value = getattr(instance, name, _MISSING)
    return None if value is _MISSING else value is not None


def _same_identity(left: Iterable[object], right: Iterable[object]) -> bool:
    left_items = tuple(left)
    right_items = tuple(right)
    return len(left_items) == len(right_items) and all(
        before is after for before, after in zip(left_items, right_items)
    )


def _endpoint_metadata(pyboy: object, physical: tuple[int, int]) -> dict[str, Any]:
    """Read scalar runtime diagnostics without mutating the endpoint."""

    mb = getattr(pyboy, "mb", None)
    serial = getattr(mb, "serial", None)
    cpu = getattr(mb, "cpu", None)
    register_file = getattr(pyboy, "register_file", None)
    backend = getattr(serial, "backend", None)

    serial_state: dict[str, object] = {}
    for field in _SERIAL_FIELDS:
        value = getattr(serial, field, None)
        if isinstance(value, bool):
            serial_state[field] = value
        elif _is_int(value):
            serial_state[field] = int(value)
        elif value is not None:
            serial_state[field] = _json_safe(value)

    pending = getattr(serial, "_owner_boundary_pending", None)
    if isinstance(pending, Mapping):
        pending_sequences = sorted(int(key) for key in pending if _is_int(key))
        pending_count = len(pending)
    else:
        pending_sequences = []
        pending_count = None

    backend_counters: dict[str, int] = {}
    for field in _BACKEND_COUNTERS:
        value = getattr(backend, field, None)
        if _is_int(value):
            backend_counters[field] = int(value)

    events = _json_safe(list(getattr(pyboy, "events", ())))
    queued_input = _json_safe(list(getattr(pyboy, "queued_input", ())))
    pc = getattr(register_file, "PC", None)
    if not _is_int(pc):
        pc = getattr(cpu, "PC", None)

    return {
        "endpoint_class": f"{type(pyboy).__module__}.{type(pyboy).__qualname__}",
        "frame_count": _optional_int(getattr(pyboy, "frame_count", None)),
        "cpu": {
            "pc": _optional_int(pc),
            "cycles": _optional_int(getattr(cpu, "cycles", None)),
        },
        "physical_clock": {
            "epoch": int(physical[0]),
            "units": int(physical[1]),
        },
        "serial": serial_state,
        "serial_owner": {
            # Native extension builds may not expose these private owner
            # fields.  ``None`` means unavailable; ``False`` means the field
            # existed and was observed clear.
            "owner_pump_active": _optional_bool_attribute(serial, "owner_pump_active"),
            "owner_poll_enabled": _optional_bool_attribute(serial, "owner_poll_enabled"),
            "pump_claimed": _optional_presence(serial, "_owner_pump_claim"),
            "pre_callback_installed": _optional_presence(serial, "_owner_pre_metadata"),
            "post_callback_installed": _optional_presence(serial, "_owner_post"),
            "pending_boundary_count": pending_count,
            "pending_boundary_sequences": pending_sequences,
        },
        "backend": {
            "class": (
                f"{type(backend).__module__}.{type(backend).__qualname__}"
                if backend is not None
                else None
            ),
            "counters": backend_counters,
        },
        # Interaction button levels are in the native state stream, but these
        # Python queues are not.  Retaining their scalar contents makes the
        # omission explicit without pretending this is a replay capsule.
        "pending_input": {
            "events": events,
            "queued_input": queued_input,
        },
    }


def _validate_local_boundary(
    link: object, members: tuple[object, object]
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Validate a settled local scheduler boundary without changing it."""

    network_backend = getattr(link, "_network_backend", None)
    if network_backend is not None:
        raise PairCheckpointError("pair checkpoints require local mode, not network mode")
    if not bool(getattr(link, "paired", False)):
        raise PairCheckpointError("pair checkpoints require two locally paired endpoints")
    if bool(getattr(link, "_step_active", False)):
        raise PairCheckpointError("cannot capture while the local scheduler is active")
    if bool(getattr(link, "_peer_progress_active", False)):
        raise PairCheckpointError("cannot capture during peer progress")
    scheduler_fault = getattr(link, "_scheduler_fault", None)
    if scheduler_fault is not None:
        raise PairCheckpointError(f"local scheduler fault: {scheduler_fault}")

    epoch_mbs = getattr(link, "_epoch_mbs", None)
    epoch_serials = getattr(link, "_epoch_serials", None)
    physical_generations = getattr(link, "_physical_generations", None)
    physical_now = getattr(link, "_physical_now", None)
    physical_expected = getattr(link, "_physical_expected", None)
    epoch_expected = getattr(link, "_epoch_expected", None)
    if not all(
        value is not None
        for value in (
            epoch_mbs,
            epoch_serials,
            physical_generations,
            physical_now,
            physical_expected,
            epoch_expected,
        )
    ):
        raise PairCheckpointError("local scheduler epoch is not initialized")

    for index, pyboy in enumerate(members):
        mb = getattr(pyboy, "mb", None)
        serial = getattr(mb, "serial", None)
        if mb is not epoch_mbs[index] or serial is not epoch_serials[index]:
            raise PairCheckpointError("attached motherboard or serial identity changed")

    reader = getattr(link, "_read_physical_clock", None)
    if not callable(reader):
        raise PairCheckpointError("local scheduler lacks physical-clock inspection")
    try:
        physical = tuple(reader(pyboy) for pyboy in members)
    except Exception as error:  # pragma: no cover - runtime-specific failure
        raise PairCheckpointError(f"unable to read physical clock: {error}") from error

    generations = tuple(int(value[0]) for value in physical)
    now = tuple(int(value[1]) for value in physical)
    if generations != tuple(physical_generations):
        raise PairCheckpointError("physical clock load epoch changed")
    if any(after < before for after, before in zip(now, tuple(physical_now))):
        raise PairCheckpointError("physical clock moved backwards")
    if now != tuple(physical_expected):
        raise PairCheckpointError("physical clock changed outside the local scheduler")

    clocks = tuple(int(pyboy.mb.serial.clock) for pyboy in members)
    if clocks != tuple(epoch_expected):
        raise PairCheckpointError("serial clock changed outside the local scheduler")
    return physical, clocks


def _capture_in_memory(
    link: object,
    frame_ordinal: int,
    caller_metadata: Mapping[str, object],
) -> _PairSnapshot:
    """Capture under canonical locks; performs no filesystem I/O."""

    members = tuple(getattr(link, "_pyboys", ()))
    if len(members) != 2:
        raise PairCheckpointError("exactly two attached endpoints are required")
    endpoints = members

    with owner_group(owner_for(endpoint) for endpoint in endpoints):
        operation_lock = getattr(link, "_operation_lock", None)
        if operation_lock is None:
            raise PairCheckpointError("link provider does not expose its operation lock")
        with operation_lock:
            current = tuple(getattr(link, "_pyboys", ()))
            if not _same_identity(members, current):
                raise EmulatorOwnershipError("provider membership changed; retry the operation")
            physical, _clocks = _validate_local_boundary(link, members)

            state_blobs: list[bytes] = []
            endpoint_metadata: list[dict[str, Any]] = []
            for pyboy, clock in zip(members, physical):
                stream = io.BytesIO()
                save_state = getattr(pyboy, "save_state", None)
                if not callable(save_state):
                    raise PairCheckpointError("attached endpoint lacks save_state")
                save_state(stream)
                blob = stream.getvalue()
                if not blob:
                    raise PairCheckpointError("endpoint returned an empty save-state")
                state_blobs.append(bytes(blob))
                endpoint_metadata.append(_endpoint_metadata(pyboy, clock))

            coordinator = getattr(link, "coordinator", None)
            provider_metadata = {
                "class": f"{type(link).__module__}.{type(link).__qualname__}",
                "coordinator_class": (
                    f"{type(coordinator).__module__}.{type(coordinator).__qualname__}"
                    if coordinator is not None
                    else None
                ),
                "network_mode": False,
                "paired": True,
                "endpoint_count": 2,
            }
            return _PairSnapshot(
                frame_ordinal=frame_ordinal,
                state_blobs=(state_blobs[0], state_blobs[1]),
                endpoint_metadata=(endpoint_metadata[0], endpoint_metadata[1]),
                provider_metadata=provider_metadata,
                caller_metadata=dict(caller_metadata),
            )


class PairCheckpointWriter:
    """Bounded writer for inspection-only local pair checkpoints."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        max_count: int,
        selected_frame_ordinals: Iterable[int] | None = None,
    ) -> None:
        if output_root is None or not str(output_root).strip():
            raise ValueError("output_root is required")
        if isinstance(max_count, bool) or not isinstance(max_count, int) or max_count <= 0:
            raise ValueError("max_count must be a positive integer")
        selected = None
        if selected_frame_ordinals is not None:
            selected = frozenset(selected_frame_ordinals)
            if any(not _is_int(item) or item < 0 for item in selected):
                raise ValueError("selected frame ordinals must be non-negative integers")
            if len(selected) > max_count:
                raise ValueError("selected frame ordinals exceed max_count")

        self.output_root = Path(output_root)
        self.max_count = max_count
        self.selected_frame_ordinals = selected
        self._bookkeeping_lock = threading.Lock()
        self._captured: set[int] = set()
        self._reserved: set[int] = set()

    @property
    def captured_count(self) -> int:
        with self._bookkeeping_lock:
            return len(self._captured)

    def capture(
        self,
        link: object,
        frame_ordinal: int,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> Path | None:
        """Capture the current settled pair if ``frame_ordinal`` is selected.

        The helper itself never advances the pair and never supplies input.
        ``None`` means the ordinal was filtered out by
        ``selected_frame_ordinals``.
        """

        if not _is_int(frame_ordinal) or frame_ordinal < 0:
            raise ValueError("frame_ordinal must be a non-negative integer")
        if (
            self.selected_frame_ordinals is not None
            and frame_ordinal not in self.selected_frame_ordinals
        ):
            return None
        caller_metadata = {} if metadata is None else dict(metadata)
        # Validate JSON shape before entering emulator ownership.  This avoids
        # discovering an unserializable caller value after native capture.
        try:
            json.dumps(_json_safe(caller_metadata), sort_keys=True)
        except (TypeError, ValueError) as error:  # pragma: no cover - _json_safe is broad
            raise ValueError(f"metadata is not JSON-compatible: {error}") from error

        with self._bookkeeping_lock:
            if frame_ordinal in self._captured or frame_ordinal in self._reserved:
                raise PairCheckpointError(f"frame ordinal already selected: {frame_ordinal}")
            if len(self._captured) + len(self._reserved) >= self.max_count:
                raise PairCheckpointError("checkpoint count limit reached")
            self._reserved.add(frame_ordinal)

        try:
            snapshot = _capture_in_memory(link, frame_ordinal, caller_metadata)
            artifact_dir = self._write_snapshot(snapshot)
        except BaseException:
            with self._bookkeeping_lock:
                self._reserved.discard(frame_ordinal)
            raise
        with self._bookkeeping_lock:
            self._reserved.discard(frame_ordinal)
            self._captured.add(frame_ordinal)
        return artifact_dir

    def _write_snapshot(self, snapshot: _PairSnapshot) -> Path:
        """Write state files followed by the manifest, outside emulator locks."""

        artifact_dir = self.output_root / (
            f"pair-checkpoint-{snapshot.frame_ordinal:08d}-{uuid.uuid4().hex}"
        )
        artifact_dir.mkdir(parents=True, exist_ok=False)

        artifacts = []
        for index, blob in enumerate(snapshot.state_blobs):
            state_name = f"side-{index}.state"
            target = artifact_dir / state_name
            temporary = artifact_dir / f".{state_name}.{uuid.uuid4().hex}.tmp"
            with temporary.open("wb") as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            artifacts.append(
                {
                    "side": index,
                    "path": state_name,
                    "bytes": len(blob),
                    "sha256": sha256(blob).hexdigest(),
                }
            )

        manifest = {
            "schema": 1,
            "inspection_only": True,
            "replay_supported": False,
            "frame_ordinal": snapshot.frame_ordinal,
            "artifacts": artifacts,
            "provider": snapshot.provider_metadata,
            "endpoints": list(snapshot.endpoint_metadata),
            "caller_metadata": _json_safe(snapshot.caller_metadata),
        }
        manifest_name = artifact_dir / "manifest.json"
        temporary_manifest = artifact_dir / f".manifest.{uuid.uuid4().hex}.tmp"
        with temporary_manifest.open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Manifest is deliberately last: its presence means both state blobs
        # and their hashes have been durably written to the unique directory.
        os.replace(temporary_manifest, manifest_name)
        return artifact_dir


__all__ = ["PairCheckpointError", "PairCheckpointWriter"]
