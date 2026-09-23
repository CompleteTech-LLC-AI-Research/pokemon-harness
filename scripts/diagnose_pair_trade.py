#!/usr/bin/env python3
"""Run an inspection-only Red/Yellow local-link trade diagnostic.

This is deliberately a small operator diagnostic around the natural real-ROM
helpers in :mod:`tests.test_pyboy_link_session_roms`.  It does not alter game
RAM, invent input, or claim release/acceptance evidence.  Pair save states are
written by ``tests._pair_checkpoints.PairCheckpointWriter`` and are therefore
inspection artifacts, not replay capsules.

The command requires an explicit output directory because ROM-derived state
must stay outside the source checkout.  Example (with operator-managed ROMs
and fixtures already installed)::

    python scripts/diagnose_pair_trade.py \
        --output-dir /tmp/pokered-pair-diagnostic \
        --capture-frame 1200 --capture-frame 2400

No ROM is opened while this module is imported.  The real-ROM helper module is
loaded only when :func:`run_pair_trade` is called.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import signal
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Resolve the checkout before importing the diagnostic-only test helper.  A
# direct ``python scripts/...`` launch normally puts ``scripts/`` (not the
# checkout root) on ``sys.path``.
REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _import_root in (_SCRIPTS_DIR, REPO_ROOT / "src", REPO_ROOT):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

try:
    from tests._pair_checkpoints import PairCheckpointWriter
except ModuleNotFoundError:  # pragma: no cover - source checkout provides this
    # Keep a useful import error for an accidentally incomplete source tree.
    PairCheckpointWriter = None  # type: ignore[assignment,misc]


TERMINAL_FILENAME = "terminal.json"
SCHEMA_VERSION = 1
DEFAULT_MAX_CAPTURES = 4
DEFAULT_DEADLINE_SECONDS = 600.0
DEFAULT_TRADE_BUDGET_FRAMES = 4000
DEFAULT_STEP_FRAMES = 20


# The bounded-diagnostic building blocks (exception hierarchy, value coercion,
# wall deadline and signal cancellation) live in a sibling module so this
# driver stays under the 1000-line bound (#158).  The scripts/ directory is on
# ``sys.path`` above, which also covers the direct ``python scripts/...`` launch
# and the test that loads this file by location.
from _diagnose_pair_trade_support import (
    CaptureConfigurationError,
    DeadlineExceeded,
    DiagnosticCancelled,
    DiagnosticError,
    _Deadline,
    _finite_positive_float,
    _json_safe,
    _nonnegative_int,
    _normalise_capture_frames,
    _positive_int,
    _SignalCancellation,
)


def _hash_file(path: Path) -> dict[str, object]:
    """Fingerprint a file without retaining any ROM-derived bytes."""

    result: dict[str, object] = {
        "path": str(path),
        "exists": path.is_file(),
        "bytes": None,
        "sha1": None,
        "sha256": None,
    }
    if not path.is_file():
        return result
    digest = hashlib.sha256()
    sha1_digest = hashlib.sha1()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            sha1_digest.update(block)
            size += len(block)
    result["bytes"] = size
    result["sha1"] = sha1_digest.hexdigest()
    result["sha256"] = digest.hexdigest()
    return result


def _combined_source_hash(files: Iterable[Path]) -> str:
    """Hash source fingerprints in a stable path-sorted order."""

    digest = hashlib.sha256()
    for path in sorted({Path(item) for item in files}, key=lambda item: str(item)):
        fingerprint = _hash_file(path)
        digest.update(str(path).encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(str(fingerprint.get("sha256") or "missing").encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _safe_import_real_helpers():
    """Import the existing natural driver lazily and without opening a ROM."""

    # A script launched as ``python scripts/...`` does not necessarily put
    # the checkout root on sys.path.  The import itself only defines helpers;
    # the fixture-gated tests open no emulator during module import.
    root_text = str(REPO_ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return importlib.import_module("tests.test_pyboy_link_session_roms")


def _open_session(version: str, *, state_path: str | Path | None = None):
    """Lazy compatibility wrapper for the natural test helper."""

    helper = _safe_import_real_helpers()
    return helper._open_session(version, state_path=state_path)


def _install_trade_diag_counters(a, b) -> dict[str, list[int]]:
    helper = _safe_import_real_helpers()
    return helper._install_trade_diag_counters(a, b)


def _drive_past_link_menu_to_trade_center(a, b, link, **kwargs) -> dict[str, object]:
    helper = _safe_import_real_helpers()
    return helper._drive_past_link_menu_to_trade_center(a, b, link, **kwargs)


def _drive_complete_trade(a, b, link, *, counters, **kwargs) -> dict[str, object]:
    helper = _safe_import_real_helpers()
    return helper._drive_complete_trade(a, b, link, counters=counters, **kwargs)


def _version_and_asset_metadata() -> dict[str, object]:
    """Collect pins, asset fingerprints, runtime identity, and source hashes."""

    helper: Any | None = None
    try:
        helper = _safe_import_real_helpers()
    except Exception as error:  # noqa: BLE001  # diagnostic can still report
        helper_error: object = {
            "type": type(error).__name__,
            "message": str(error),
        }
    else:
        helper_error = None

    versions_path = REPO_ROOT / "VERSIONS.md"
    pins: dict[str, object] = {
        "path": str(versions_path),
        "file": _hash_file(versions_path),
        "pyboy_version": None,
        "rom_sha1": {},
        "symbol_sha1": {},
    }
    versions = None
    try:
        config = importlib.import_module("pokered_harness.config")
        versions = config.load_versions(versions_path)
    except Exception as error:  # noqa: BLE001  # incomplete trees remain reportable
        pins["error"] = {"type": type(error).__name__, "message": str(error)}
    else:
        pins["pyboy_version"] = versions.pyboy_version

    assets: dict[str, object] = {}
    source_files = [
        Path(__file__).resolve(),
        REPO_ROOT / "tests" / "test_pyboy_link_session_roms.py",
        REPO_ROOT / "tests" / "_pair_checkpoints.py",
        REPO_ROOT / "src" / "pokered_harness" / "config.py",
        REPO_ROOT / "src" / "pokered_harness" / "session.py",
        REPO_ROOT / "src" / "pokered_harness" / "ownership.py",
        REPO_ROOT / "src" / "pokered_harness" / "link" / "pyboy_link_session.py",
        REPO_ROOT / "src" / "pokered_harness" / "link" / "_pyboy_link_session_network_mixin.py",
        REPO_ROOT / "src" / "pokered_harness" / "link" / "_pyboy_link_session_lifecycle_mixin.py",
        REPO_ROOT / "src" / "pokered_harness" / "link" / "_pyboy_link_session_stepping_mixin.py",
        REPO_ROOT / "src" / "pokered_harness" / "link" / "_pyboy_link_session_support.py",
        REPO_ROOT / "src" / "pokered_harness" / "link" / "serial_coordinator.py",
    ]

    for version in ("red", "yellow"):
        if helper is not None:
            try:
                rom, sym = helper._ROM_PATHS[version]
                fixture = helper._state_path(version)
            except Exception as error:  # noqa: BLE001  # helper drift remains reportable
                assets[version] = {
                    "error": {"type": type(error).__name__, "message": str(error)}
                }
                continue
        else:
            rom = sym = fixture = Path("")
        rom = Path(rom)
        sym = Path(sym)
        fixture = Path(fixture)
        rom_pin = versions.sha1_for_path(rom) if versions is not None else None
        sym_pin = versions.symbol_sha1_for_path(sym) if versions is not None else None
        pins["rom_sha1"][version] = rom_pin
        pins["symbol_sha1"][version] = sym_pin
        assets[version] = {
            "rom": {
                **_hash_file(rom),
                "pinned_sha1": rom_pin,
            },
            "symbols": {
                **_hash_file(sym),
                "pinned_sha1": sym_pin,
            },
            "fixture": _hash_file(fixture),
        }
        source_files.extend((rom, sym, fixture))

    runtime: dict[str, object] = {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "executable": sys.executable,
        "pokered_harness": None,
        "pyboy": None,
    }
    for package in ("pokered-harness", "pyboy"):
        key = package.replace("-", "_")
        try:
            runtime[key] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            runtime[key] = None
    runtime["modules"] = {}
    for module_name in ("pyboy.pyboy", "pyboy.core.serial", "pyboy.core.mb"):
        try:
            module = importlib.import_module(module_name)
        except Exception as error:  # noqa: BLE001 - metadata must remain reportable
            runtime["modules"][module_name] = {
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                }
            }
        else:
            module_path = getattr(module, "__file__", None)
            runtime["modules"][module_name] = (
                _hash_file(Path(module_path)) if module_path is not None else None
            )
    source_fingerprints = {
        (
            str(path.relative_to(REPO_ROOT))
            if path.is_relative_to(REPO_ROOT)
            else str(path)
        ): _hash_file(path)
        for path in source_files
    }
    return {
        "pins": pins,
        "assets": assets,
        "runtime": runtime,
        "source": {
            "files": source_fingerprints,
            "combined_sha256": _combined_source_hash(source_files),
        },
        "helper_import_error": helper_error,
    }


class _SessionInputProxy:
    """Transparent Session wrapper that records exactly attempted inputs."""

    def __init__(
        self,
        target: object,
        side: int,
        transcript: list[dict[str, object]],
        frame_ordinal: Callable[[], int],
        sequence: Callable[[], int],
        deadline: _Deadline,
    ) -> None:
        self._target = target
        self._side = side
        self._transcript = transcript
        self._frame_ordinal = frame_ordinal
        self._sequence = sequence
        self._deadline = deadline

    def _record(
        self,
        operation: str,
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
    ) -> None:
        self._deadline.check(f"input {operation}")
        self._transcript.append(
            {
                "sequence": self._sequence(),
                "side": self._side,
                "frame_ordinal": self._frame_ordinal(),
                "operation": operation,
                "args": _json_safe(args),
                "kwargs": _json_safe(dict(kwargs)),
            }
        )

    def press(self, *args: object, **kwargs: object) -> object:
        self._record("press", args, kwargs)
        return self._target.press(*args, **kwargs)  # type: ignore[attr-defined]

    def hold(self, *args: object, **kwargs: object) -> object:
        self._record("hold", args, kwargs)
        return self._target.hold(*args, **kwargs)  # type: ignore[attr-defined]

    def release(self, *args: object, **kwargs: object) -> object:
        self._record("release", args, kwargs)
        return self._target.release(*args, **kwargs)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._target, name)


class _CaptureLinkProxy:
    """Transparent local link proxy that captures only selected frame ends."""

    def __init__(
        self,
        target: object,
        *,
        capture: Callable[[int], object],
        selected_frame_ordinals: Iterable[int],
        deadline: _Deadline,
        max_operation_frames: int | None = None,
    ) -> None:
        self._target = target
        self._capture = capture
        self._selected = frozenset(
            _nonnegative_int(item, "capture frame ordinal")
            for item in selected_frame_ordinals
        )
        self._deadline = deadline
        if max_operation_frames is not None:
            max_operation_frames = _positive_int(
                max_operation_frames, "max_operation_frames"
            )
        self._max_operation_frames = max_operation_frames
        self.frame_ordinal = 0
        self._captured: set[int] = set()
        self._capture(0)
        self._captured.add(0)

    @property
    def target(self) -> object:
        return self._target

    @property
    def captured_frame_ordinals(self) -> tuple[int, ...]:
        return tuple(sorted(self._captured))

    def _advance(self, method_name: str, frames: int, *args: object, **kwargs: object) -> object:
        frames = _positive_int(frames, "frames")
        requested_end = self.frame_ordinal + frames
        result: object = None
        while self.frame_ordinal < requested_end:
            self._deadline.check(f"{method_name} frame {self.frame_ordinal}")
            next_boundary = min(
                (
                    ordinal
                    for ordinal in self._selected
                    if self.frame_ordinal < ordinal <= requested_end
                ),
                default=requested_end,
            )
            segment = next_boundary - self.frame_ordinal
            if self._max_operation_frames is not None:
                segment = min(segment, self._max_operation_frames)
            segment_end = self.frame_ordinal + segment
            # Segment calls are the exact owner operation requested by the
            # natural helper.  No frame is ticked merely to obtain a capture.
            result = getattr(self._target, method_name)(segment, *args, **kwargs)
            self.frame_ordinal = segment_end
            if self.frame_ordinal in self._selected:
                self._deadline.check(f"capture frame {self.frame_ordinal}")
                self._capture(self.frame_ordinal)
                self._captured.add(self.frame_ordinal)
        return result

    def step_interleaved(self, frames: int = 1, *args: object, **kwargs: object) -> object:
        return self._advance("step_interleaved", frames, *args, **kwargs)

    def step(self, frames: int = 1, *args: object, **kwargs: object) -> object:
        return self._advance("step", frames, *args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._target, name)


@dataclass
class _RunState:
    stage: str = "startup"
    result: str = "running"
    frame_ordinal: int = 0
    trade: dict[str, object] | None = None
    warp: dict[str, object] | None = None
    error: dict[str, object] | None = None


def _safe_map_snapshot(session: object) -> dict[str, object]:
    try:
        state = session.read_game_state()  # type: ignore[attr-defined]
        overworld = getattr(state, "overworld", None)
        if overworld is None:
            return {"available": False}
        result: dict[str, object] = {"available": True}
        for name in ("map_id", "x", "y", "walk_counter", "direction"):
            value = getattr(overworld, name, None)
            result[name] = getattr(value, "value", value)
        return result
    except Exception as error:  # noqa: BLE001  # runtime-specific diagnostic path
        return {"available": False, "error": f"{type(error).__name__}: {error}"}


def _copy_hook_counters(counters: Mapping[str, object] | None) -> dict[str, object]:
    if counters is None:
        return {}
    copied: dict[str, object] = {}
    for key, value in counters.items():
        if isinstance(value, (list, tuple)):
            copied[str(key)] = [
                int(item) if isinstance(item, int) else _json_safe(item)
                for item in value
            ]
        else:
            copied[str(key)] = _json_safe(value)
    return copied


def _close_session(session: object) -> None:
    close = getattr(session, "close", None)
    if not callable(close):
        return
    # Session.close has a stable ``close(save=False, ...)`` contract.  Do not
    # retry an internal TypeError: doing so can call a partially completed
    # native teardown twice.
    close(save=False)


def _validate_output_root(output_dir: str | Path) -> Path:
    """Validate an explicit, external, and unused diagnostic directory."""

    if output_dir is None or not str(output_dir).strip():
        raise ValueError("output_dir is required and must not be empty")
    output_root = Path(output_dir).expanduser().resolve()
    repo_root = REPO_ROOT.resolve()
    if output_root == repo_root or output_root.is_relative_to(repo_root):
        raise ValueError(
            f"output_dir must be outside the source checkout: {output_root}"
        )
    terminal_path = output_root / TERMINAL_FILENAME
    if terminal_path.exists():
        raise FileExistsError(
            f"refusing to overwrite existing diagnostic terminal: {terminal_path}"
        )
    return output_root


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(_json_safe(value), stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run_pair_trade(
    output_dir: str | Path,
    *,
    capture_frame_ordinals: Iterable[int] | None = None,
    max_captures: int = DEFAULT_MAX_CAPTURES,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    trade_budget_frames: int = DEFAULT_TRADE_BUDGET_FRAMES,
    step_frames: int = DEFAULT_STEP_FRAMES,
    open_session: Callable[..., object] = _open_session,
    link_factory: Callable[[], object] | None = None,
    install_trade_diag_counters: Callable[
        [object, object], dict[str, list[int]]
    ] = _install_trade_diag_counters,
    drive_to_trade_center: Callable[..., dict[str, object]] = (
        _drive_past_link_menu_to_trade_center
    ),
    drive_trade: Callable[..., dict[str, object]] = _drive_complete_trade,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Run the bounded diagnostic and always emit a terminal JSON record.

    The returned mapping is the same JSON-safe record written to
    ``<output_dir>/terminal.json``.  Errors are represented in that record and
    do not prevent cleanup; callers such as :func:`main` use ``result`` to
    choose their process status.
    """

    output_root = _validate_output_root(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    requested = _normalise_capture_frames(capture_frame_ordinals)
    _positive_int(max_captures, "max_captures")
    _finite_positive_float(deadline_seconds, "deadline_seconds")
    _positive_int(trade_budget_frames, "trade_budget_frames")
    _positive_int(step_frames, "step_frames")
    all_capture_frames = (0, *requested)
    if len(all_capture_frames) > max_captures:
        raise CaptureConfigurationError(
            f"{len(all_capture_frames)} requested captures exceed max_captures={max_captures}"
        )

    deadline = _Deadline(deadline_seconds, clock=clock)
    static_metadata: dict[str, object] = {}
    state = _RunState()
    transcript: list[dict[str, object]] = []
    input_sequence = 0
    sessions: list[object] = []
    link: object | None = None
    cleanup_errors: list[dict[str, str]] = []
    writer: Any | None = None
    captured: list[int] = []
    session_proxies: list[_SessionInputProxy] = []
    counters: dict[str, list[int]] | None = None
    link_proxy: _CaptureLinkProxy | None = None
    cancellation = _SignalCancellation()

    def next_input_sequence() -> int:
        nonlocal input_sequence
        input_sequence += 1
        return input_sequence

    def capture_metadata(frame_ordinal: int) -> dict[str, object]:
        return {
            "inspection_only": True,
            "acceptance_evidence": False,
            "stage": state.stage,
            "frame_ordinal": frame_ordinal,
            "maps": {
                "side-0": _safe_map_snapshot(session_proxies[0]) if session_proxies else {},
                "side-1": (
                    _safe_map_snapshot(session_proxies[1])
                    if len(session_proxies) > 1
                    else {}
                ),
            },
            "hooks": _copy_hook_counters(counters),
            "input_transcript": list(transcript),
            "run_metadata": static_metadata,
        }

    def capture_at(frame_ordinal: int) -> None:
        deadline.check(f"capture frame {frame_ordinal}")
        if writer is None or link is None:
            raise DiagnosticError("checkpoint writer/link unavailable")
        writer.capture(
            link,
            frame_ordinal,
            metadata=capture_metadata(frame_ordinal),
        )
        captured.append(frame_ordinal)
        state.frame_ordinal = frame_ordinal

    def terminal_record(phase: str) -> dict[str, object]:
        """Build a JSON-safe report for either side of cleanup."""

        return {
            "schema": SCHEMA_VERSION,
            "diagnostic": "red-yellow-pair-trade",
            "inspection_only": True,
            "acceptance_evidence": False,
            "terminal_phase": phase,
            "cleanup_pending": phase == "cleanup_pending",
            "cancellation_requested": cancellation.requested,
            "cancellation_signal": (
                signal.Signals(cancellation.signum).name
                if cancellation.signum is not None
                else None
            ),
            "stage": state.stage,
            "result": state.result,
            "frame_ordinal": (
                link_proxy.frame_ordinal
                if link_proxy is not None
                else state.frame_ordinal
            ),
            "requested_capture_frames": list(requested),
            "captured_frames": sorted(captured),
            "unreached_capture_frames": [
                frame for frame in all_capture_frames if frame not in captured
            ],
            "elapsed_seconds": deadline.elapsed,
            "metadata": static_metadata,
            "warp": state.warp,
            "trade": state.trade,
            "error": state.error,
            "cleanup_errors": cleanup_errors,
            "input_count": len(transcript),
            "input_transcript": transcript,
        }

    def reconcile_cancellation() -> None:
        if cancellation.requested and state.result == "completed_inspection":
            state.result = "cancelled"
            state.error = {
                "type": "DiagnosticCancelled",
                "message": "diagnostic cancellation requested during cleanup",
            }

    def cleanup_resources() -> None:
        # Detach the pair before stopping either emulator.  If a custom link
        # exposes a more explicit disconnect API, prefer it; real
        # PyBoyLinkSession exposes ``close`` (an alias of ``detach_all``).
        if link is not None:
            attempted = False
            for method_name in ("disconnect", "close", "detach_all"):
                method = getattr(link, method_name, None)
                if not callable(method):
                    continue
                attempted = True
                try:
                    method()
                except BaseException as error:  # noqa: BLE001
                    cleanup_errors.append(
                        {
                            "operation": f"link.{method_name}",
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                    # A failed explicit disconnect may leave attached native
                    # endpoints behind; try the next bounded cleanup hook.
                    continue
                break
            if not attempted:
                cleanup_errors.append(
                    {
                        "operation": "link.cleanup",
                        "error": "no close/disconnect method",
                    }
                )
        for side, session in enumerate(reversed(sessions)):
            try:
                _close_session(session)
            except BaseException as error:  # noqa: BLE001
                cleanup_errors.append(
                    {
                        "operation": f"session.close[{len(sessions) - side - 1}]",
                        "error": f"{type(error).__name__}: {error}",
                    }
                )

    return_value: dict[str, object] | None = None
    try:
        cancellation.install()
        try:
            static_metadata = _version_and_asset_metadata()
            cancellation.raise_if_requested()
            deadline.check("run metadata")
            deadline.check("session startup")
            if link_factory is None:
                from pokered_harness.link.pyboy_link_session import PyBoyLinkSession

                link_factory = PyBoyLinkSession.local
            link = link_factory()
            writer = PairCheckpointWriter(
                output_root,
                max_count=max_captures,
                selected_frame_ordinals=all_capture_frames,
            )
            state.stage = "open_sessions"
            # Append each successful construction separately.  If Yellow fails to
            # open, the already-created Red session remains reachable by finally
            # and is closed exactly once.
            sessions.append(open_session("red"))
            sessions.append(open_session("yellow"))
            proxy_link_target = link
            attach = proxy_link_target.attach  # type: ignore[attr-defined]
            attach(sessions[0]._pyboy)  # type: ignore[attr-defined]
            attach(sessions[1]._pyboy)  # type: ignore[attr-defined]
            session_proxies.extend(
                _SessionInputProxy(
                    session,
                    side,
                    transcript,
                    lambda: link_proxy.frame_ordinal if link_proxy is not None else 0,
                    next_input_sequence,
                    deadline,
                )
                for side, session in enumerate(sessions)
            )
            counters = install_trade_diag_counters(session_proxies[0], session_proxies[1])

            state.stage = "initial_capture"
            # _CaptureLinkProxy captures exactly once at ordinal zero before any
            # natural helper has a chance to press a button.
            link_proxy = _CaptureLinkProxy(
                proxy_link_target,
                capture=capture_at,
                selected_frame_ordinals=requested,
                deadline=deadline,
                # The native link owner has its own per-frame guard, but a
                # multi-frame call can otherwise hold Python past this driver's
                # wall deadline. One-frame owner calls preserve total frames and
                # the natural input schedule while restoring regular checks.
                max_operation_frames=1,
            )

            state.stage = "trade_center"
            state.warp = drive_to_trade_center(
                session_proxies[0],
                session_proxies[1],
                link_proxy,
            )
            deadline.check("trade-center helper")
            expected_map = 0xEF
            final_map_a = state.warp.get("final_map_a") if state.warp else None
            final_map_b = state.warp.get("final_map_b") if state.warp else None
            if final_map_a != expected_map or final_map_b != expected_map:
                raise DiagnosticError(
                    "trade-center precondition failed: expected both final maps "
                    f"to be 0x{expected_map:02x}, got "
                    f"A={final_map_a!r}, B={final_map_b!r}"
                )

            state.stage = "trade"
            state.trade = drive_trade(
                session_proxies[0],
                session_proxies[1],
                link_proxy,
                counters=counters,
                trade_budget_frames=trade_budget_frames,
                step_frames=step_frames,
            )
            deadline.check("trade helper")
            state.stage = "complete"
            state.result = "completed_inspection"
        except DiagnosticCancelled as error:
            state.result = "cancelled"
            state.error = {"type": type(error).__name__, "message": str(error)}
        except DeadlineExceeded as error:
            state.result = "deadline_exceeded"
            state.error = {"type": type(error).__name__, "message": str(error)}
        except BaseException as error:  # noqa: BLE001 - terminal evidence must survive all failures
            state.result = "error"
            state.error = {
                "type": type(error).__name__,
                "message": str(error),
            }
        finally:
            cancellation.cleanup_started = True
            reconcile_cancellation()

            # Publish a durable state before teardown.  If native cleanup hangs or
            # an external supervisor later sends SIGTERM/SIGKILL, this provisional
            # record still identifies the last completed frame and stage.
            provisional_write_error: dict[str, str] | None = None
            try:
                provisional = terminal_record("cleanup_pending")
                try:
                    _write_json_atomic(output_root / TERMINAL_FILENAME, provisional)
                except BaseException as error:  # noqa: BLE001
                    provisional_write_error = {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
            finally:
                # Report assembly must never prevent link/session teardown.
                cleanup_resources()

            reconcile_cancellation()
            # A cancellation can arrive while the final atomic write is in
            # progress. Reconcile once and perform at most one bounded rewrite
            # so a completed report cannot survive a delivered cancellation.
            for _report_attempt in range(2):
                reconcile_cancellation()
                terminal = terminal_record("final")
                if provisional_write_error is not None:
                    terminal["provisional_terminal_write_error"] = provisional_write_error
                try:
                    _write_json_atomic(output_root / TERMINAL_FILENAME, terminal)
                except BaseException as error:  # noqa: BLE001
                    # There is no safe place to report a second filesystem
                    # failure in the terminal record; preserve it in the
                    # returned mapping/stdout.
                    terminal["terminal_write_error"] = {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                if not (
                    cancellation.requested
                    and terminal.get("result") == "completed_inspection"
                ):
                    break
            return_value = terminal

    finally:
        cancellation.restore()
    return return_value


def _parse_capture_arguments(values: Iterable[str]) -> tuple[int, ...]:
    parsed: list[int] = []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if not token:
                raise argparse.ArgumentTypeError("capture frame cannot be empty")
            try:
                parsed.append(int(token, 10))
            except ValueError as error:
                raise argparse.ArgumentTypeError(f"invalid capture frame: {token!r}") from error
    try:
        return _normalise_capture_frames(parsed)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="external directory for inspection-only pair states and terminal.json",
    )
    parser.add_argument(
        "--capture-frame",
        action="append",
        default=[],
        metavar="ORDINAL",
        help="post-frame ordinal to capture; repeat or comma-separate (frame 0 is automatic)",
    )
    parser.add_argument("--max-captures", type=int, default=DEFAULT_MAX_CAPTURES)
    parser.add_argument("--deadline-seconds", type=float, default=DEFAULT_DEADLINE_SECONDS)
    parser.add_argument("--trade-budget-frames", type=int, default=DEFAULT_TRADE_BUDGET_FRAMES)
    parser.add_argument("--step-frames", type=int, default=DEFAULT_STEP_FRAMES)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        capture_frames = _parse_capture_arguments(args.capture_frame)
        result = run_pair_trade(
            args.output_dir,
            capture_frame_ordinals=capture_frames,
            max_captures=args.max_captures,
            deadline_seconds=args.deadline_seconds,
            trade_budget_frames=args.trade_budget_frames,
            step_frames=args.step_frames,
        )
    except BaseException as error:  # noqa: BLE001 - CLI must be deterministic
        print(
            json.dumps(
                {
                    "schema": SCHEMA_VERSION,
                    "diagnostic": "red-yellow-pair-trade",
                    "inspection_only": True,
                    "acceptance_evidence": False,
                    "result": "error",
                    "error": {"type": type(error).__name__, "message": str(error)},
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    clean_success = (
        result.get("result") == "completed_inspection"
        and not result.get("cleanup_errors")
        and not result.get("terminal_write_error")
    )
    return 0 if clean_success else 1


if __name__ == "__main__":  # pragma: no cover - exercised by operators
    raise SystemExit(main())


__all__ = [
    "TERMINAL_FILENAME",
    "CaptureConfigurationError",
    "DeadlineExceeded",
    "DiagnosticError",
    "_CaptureLinkProxy",
    "_SessionInputProxy",
    "build_parser",
    "main",
    "run_pair_trade",
]
