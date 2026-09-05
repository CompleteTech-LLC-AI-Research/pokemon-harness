"""Bounded cancellation diagnostic, never a graceful tick/gameplay qualification.

Uses immutable canonical fixtures and whole public Session.step calls at Q256.
Five-second operation timeouts were qualified on authored ROMs only; callers
must explicitly select all timing policy budgets for commercial-ROM diagnostics.
Run under an outer process timeout: Python cannot preempt a stuck native call.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import math
import multiprocessing
import os
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._rom_assets import find_rom_root, fixture_path

# Record failures without suppressing their diagnostic status or owner cleanup.
# ruff: noqa: BLE001

VERSIONS = ("red_color", "blue_color", "yellow")
QUANTUM_CYCLES = 256
MAX_REPORT_BYTES = 1_048_576
STDERR_LIMIT = 65_536
MENU_TERMINAL_RESERVE = 131_072
CALL_LOG_BYTE_LIMIT = 16 * 1024 * 1024
INLINE_CALL_WINDOW = 4
MILESTONE_EVENT_LIMIT = 32
READINESS_PHASES = (
    "party_qualified",
    "link_menu_trade_ready",
    "link_menu_a_applied",
    "trade_center_reached",
    "select_mon_ready",
    "outgoing_slot_ready",
    "pre_evolution_copy_validated",
    "post_save_cycle_returned",
)


class _PeerReadiness:
    """Bounded publish-once flags; no emulator or payload crosses owners."""

    def __init__(self, raw, index, phases=READINESS_PHASES):
        self.raw, self.index = raw, index
        self.phases = phases

    def publish_ready(self, phase):
        self.raw[self.index * len(self.phases) + self.phases.index(phase)] = 1

    def peer_ready(self, phase):
        return bool(self.raw[(1 - self.index) * len(self.phases) + self.phases.index(phase)])

    def snapshot(self):
        return {
            "own": {
                phase: bool(self.raw[self.index * len(self.phases) + i])
                for i, phase in enumerate(self.phases)
            },
            "peer": {phase: self.peer_ready(phase) for phase in self.phases},
        }


def _driver_factory(path):
    if not isinstance(path, str) or path.count(":") != 1:
        raise ValueError("owner driver must be an importable module:function string")
    module, name = path.split(":")
    factory = getattr(importlib.import_module(module), name)
    if not callable(factory):
        raise TypeError("owner driver factory must be callable")
    return factory


class CallEvidenceLog:
    """Owner-only log; complete means all attempted calls have terminal evidence.

    It does not mean their CPU calls succeeded or teardown was graceful.
    Incomplete writes/cap exhaustion are sticky failures.
    """

    def __init__(self, path, byte_limit=CALL_LOG_BYTE_LIMIT):
        if type(byte_limit) is not int or byte_limit <= 0:
            raise ValueError("call log byte limit must be positive")
        self.path = Path(path)
        self.byte_limit = byte_limit
        self._stream = self.path.open("xb", buffering=0)
        self._hash = hashlib.sha256()
        self.bytes = self.record_count = 0
        self.error = None
        self.complete = False

    def append(self, call):
        if self.error is not None or self._stream.closed:
            raise RuntimeError(self.error or "call log is closed")
        try:
            payload = (json.dumps(_jsonable(call), separators=(",", ":")) + "\n").encode()
            if self.bytes + len(payload) > self.byte_limit:
                raise RuntimeError("call log byte cap exceeded")
            remaining = memoryview(payload)
            while remaining:
                written = self._stream.write(remaining)
                if not written:
                    raise OSError("call log write made no progress")
                self._hash.update(remaining[:written])
                self.bytes += written
                remaining = remaining[written:]
            self._stream.flush()
            self.record_count += 1
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            raise

    def snapshot(self):
        return {
            "path": str(self.path),
            "bytes": self.bytes,
            "record_count": self.record_count,
            "sha256": self._hash.hexdigest(),
            "complete": self.complete,
            "error": self.error,
        }

    def close(self, complete=True):
        try:
            if not self._stream.closed:
                try:
                    self._stream.flush()
                finally:
                    self._stream.close()
            self.complete = bool(complete and self.error is None)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.complete = False
            raise


def validate_call_artifact(owner):
    """Qualify complete stream evidence with a bounded read; inline is unchanged."""
    manifest = owner.get("call_log")
    if manifest is None:
        if "call_counts" in owner:
            raise ValueError("streamed call artifact manifest is missing")
        return
    if not manifest.get("complete") or manifest.get("error"):
        raise ValueError("call artifact is incomplete")
    expected_bytes = manifest.get("bytes")
    expected_records = manifest.get("record_count")
    if (
        type(expected_bytes) is not int
        or not 0 <= expected_bytes <= CALL_LOG_BYTE_LIMIT
        or type(expected_records) is not int
        or expected_records < 0
        or expected_records != owner.get("call_counts", {}).get("total")
    ):
        raise ValueError("call artifact manifest/count mismatch")
    fd = os.open(manifest["path"], os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    digest = hashlib.sha256()
    size = lines = 0
    last = b""
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > CALL_LOG_BYTE_LIMIT:
            raise ValueError("call artifact must be a bounded regular file")
        while block := stream.read(min(65_536, CALL_LOG_BYTE_LIMIT + 1 - size)):
            size += len(block)
            digest.update(block)
            lines += block.count(b"\n")
            last = block[-1:]
            if size > CALL_LOG_BYTE_LIMIT:
                raise ValueError("call artifact exceeds byte cap")
    if (
        size != expected_bytes
        or digest.hexdigest() != manifest.get("sha256")
        or lines != expected_records
        or (size and last != b"\n")
    ):
        raise ValueError("call artifact bytes/hash/record count mismatch")


def validate_input_profile(args):
    profile = getattr(args, "input_profile", "none")
    if profile not in ("none", "menu"):
        raise ValueError("unknown input profile")
    if profile == "menu" and (args.listener_chunk != 1 or args.connector_chunk != 1):
        raise ValueError("menu input requires listener_chunk=connector_chunk=1")
    retention = getattr(args, "call_retention", "inline")
    if retention not in ("inline", "stream"):
        raise ValueError("unknown call retention")
    if profile != "menu" and (getattr(args, "rom_milestones", False) or retention == "stream"):
        raise ValueError("ROM milestones and streamed calls require menu input profile")
    return profile


def positive_float(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return value


def positive_int(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listener", choices=VERSIONS, default="blue_color")
    parser.add_argument("--connector", choices=VERSIONS, default="yellow")
    parser.add_argument("--both-orientations", action="store_true")
    parser.add_argument("--owner-mode", choices=("thread", "process"), default="thread")
    parser.add_argument("--input-profile", choices=("none", "menu"), default="none")
    parser.add_argument("--rom-milestones", action="store_true")
    parser.add_argument("--call-retention", choices=("inline", "stream"), default="inline")
    parser.add_argument("--listener-chunk", type=positive_int, default=1)
    parser.add_argument("--connector-chunk", type=positive_int, default=2)
    parser.add_argument("--frame-limit", type=positive_int, default=6)
    parser.add_argument("--overall-timeout", type=positive_float, default=30)
    parser.add_argument("--pair-timeout", type=positive_float, default=12)
    parser.add_argument("--cleanup-timeout", type=positive_float, default=3)
    parser.add_argument("--operation-timeout", type=positive_float, required=True)
    parser.add_argument("--rearm-budget", type=positive_int, required=True)
    parser.add_argument("--rearm-instruction-cap", type=positive_int, required=True)
    parser.add_argument("--max-edge-lateness", type=positive_int, required=True)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        validate_input_profile(args)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def resolve_assets(version, repo_root):
    """Validate selected canonical ordinary fixture and both input hashes."""
    from pokered_harness.config import load_versions
    from scripts.validate_fixture_manifest import _load_manifest, _validate_schema

    if version not in VERSIONS:
        raise ValueError("only canonical color Red/Blue and Yellow are supported")
    family = version.split("_")[0]
    manifest = _load_manifest(repo_root / "release-evidence" / "fixture-manifest.json")
    rows = _validate_schema(manifest)
    selected = [
        row
        for row in rows
        if row["version"] == family
        and row["kind"] == "ordinary"
        and row["provenance"]["status"] == "verified"
        and (family == "yellow" or row["variant"] == "color")
    ]
    if len(selected) != 1:
        raise ValueError("canonical fixture registry selection is not unique")
    row = selected[0]
    rom_root = find_rom_root(repo_root)
    rom = rom_root / Path(row["expected_rom"]["path"]).relative_to("rom")
    sym = rom_root / Path(row["expected_symbols"]["path"]).relative_to("rom")
    state_path = fixture_path(family, Path(row["path"]).name, repo_root)
    state = state_path.read_bytes()
    if (
        len(state) != row["size_bytes"]
        or hashlib.sha1(state).hexdigest() != row["sha1"]
        or hashlib.sha256(state).hexdigest() != row["sha256"]
    ):
        raise ValueError("fixture bytes do not match canonical registry")
    pins = load_versions(repo_root / "VERSIONS.md")
    for path, entry, expected in (
        (rom, row["expected_rom"], pins.sha1_for_path(rom)),
        (sym, row["expected_symbols"], pins.symbol_sha1_for_path(sym)),
    ):
        if expected != entry["sha1"] or hashlib.sha1(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"registry/VERSIONS/input hash mismatch: {path}")
    return {
        "rom": rom,
        "sym": sym,
        "state": state,
        "family": family,
        "pins": {
            "expected_rom_sha1": row["expected_rom"]["sha1"],
            "expected_symbol_sha1": row["expected_symbols"]["sha1"],
            "expected_pyboy_version": pins.pyboy_version,
            "expected_pyboy_revision": pins.pyboy_revision,
        },
        "provenance": {
            "fixture": str(state_path),
            "registry": row,
            "rom": str(rom),
            "symbols": str(sym),
        },
    }


def runtime_identity():
    modules = {}
    for name in ("pyboy.pyboy", "pyboy.core.cpu", "pyboy.core.mb", "pyboy.core.serial"):
        module = importlib.import_module(name)
        path = Path(module.__file__)
        modules[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"python": sys.version, "executable": sys.executable, "modules": modules}


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def observe(session, endpoint=None):
    """Owner-only, read-only native observations; no RAM writes or hooks."""
    game = session._pyboy
    board = game.mb
    serial = board.serial
    result = {
        "frame_count": int(game.frame_count),
        "session_tick": session.current_tick(),
        "cpu_cycles": int(board.cpu.cycles),
        "double_speed": getattr(board, "double_speed", None),
        "double_speed_type": type(getattr(board, "double_speed", None)).__name__,
        "retired_instructions": getattr(board.cpu, "retired_instructions", None),
        "serial": {
            name: getattr(serial, name, None)
            for name in (
                "SB",
                "SC",
                "clock",
                "clock_target",
                "_bits_remaining",
                "internal_clock",
                "transfer_enabled",
                "last_cycles",
            )
        },
        "lcd": {name: getattr(board.lcd, name, None) for name in ("clock", "clock_target", "LY")},
    }
    try:
        result["lcd"]["LY"] = int(game.memory[0xFF44])
    except (AttributeError, TypeError, IndexError) as exc:
        result["lcd"]["read_error"] = str(exc)
    if endpoint is not None:
        try:
            result["timed"] = _jsonable(endpoint.snapshot())
        except Exception as exc:
            result["snapshot_error"] = f"{type(exc).__name__}: {exc}"
    return _jsonable(result)


def _run_owner(
    index,
    args,
    records,
    sockets,
    cancelled,
    done,
    barrier,
    endpoints,
    guard,
    deadline,
    overall,
    session_factory,
    endpoint_factory,
    asset_resolver,
    checkpoint=None,
    evidence_path=None,
    owner_driver=None,
    peer_state=None,
    goal_flag=None,
    goal_stop=None,
    driver_options=None,
):
    record = records[index]
    session = endpoint = None
    bound = False
    version = (args.listener, args.connector)[index]
    chunk = (args.listener_chunk, args.connector_chunk)[index]
    record.update(version=version, owner_thread=threading.get_ident(), pid=os.getpid())
    profile = validate_input_profile(args)
    scheduled_offsets = set()
    log = observer = milestone_context = None
    driver = None

    def goal_cancel(exc=None):
        if owner_driver is None or goal_flag is None or goal_stop is None:
            return False
        if not (goal_flag.is_set() and goal_stop.is_set() and cancelled.is_set()):
            return False
        if exc is None:
            return True
        from pokered_harness.link.timed_wire import Cancelled

        return type(exc) is Cancelled

    seen_events = 0
    call_index = 0
    first_calls, last_calls, milestone_calls = [], [], []

    def finish_call(call):
        nonlocal seen_events
        if log is not None:
            counts = record["call_counts"]
            counts["total"] += 1
            status = call.get("status", "interrupted")
            counts[status] = counts.get(status, 0) + 1
            counts["noncompleted"] += status != "completed"
            counts["requested_frames"] += call["requested_frames"]
            if "actual_completed_frames" in call:
                counts["actual_completed_frames"] += call["actual_completed_frames"]
            else:
                counts["unknown_actual_calls"] += 1
        if observer is not None:
            state = observer.snapshot()
            record["milestones"] = state
            call["milestones"] = state["events"][seen_events:]
            seen_events = len(state["events"])
        if log is None:
            return
        try:
            log.append(call)
        except Exception as exc:
            record["unspooled_call"] = call
            record["errors"].append(f"call evidence: {type(exc).__name__}: {exc}")
            raise
        finally:
            record["call_log"] = log.snapshot()
        if len(first_calls) < INLINE_CALL_WINDOW:
            first_calls.append(call)
        last_calls.append(call)
        del last_calls[:-INLINE_CALL_WINDOW]
        if call.get("milestones") and len(milestone_calls) < MILESTONE_EVENT_LIMIT:
            milestone_calls.append(call)
        retained = {item["call_index"]: item for item in first_calls + last_calls + milestone_calls}
        record["calls"] = [retained[key] for key in sorted(retained)]
        record.pop("in_flight", None)

    def observed():
        record.pop("last_native_observation", None)
        result = observe(session, endpoint)
        if profile == "menu":
            from scripts._timed_menu_probe import read_input_snapshot, read_menu_snapshot

            try:
                result["menu"] = read_menu_snapshot(
                    symbols=session.symbols,
                    memory=session._pyboy.memory,
                    owner_thread_id=record["owner_thread"],
                )
                result["input_state"] = read_input_snapshot(
                    symbols=session.symbols,
                    memory=session._pyboy.memory,
                    register_file=session._pyboy.register_file,
                    owner_thread_id=record["owner_thread"],
                )
            except Exception:
                record["last_native_observation"] = result
                raise
        return result

    def publish(phase):
        record["phase"] = phase
        if checkpoint is not None:
            try:
                checkpoint(record)
            except Exception as exc:
                if "checkpoint_error" not in record:
                    record["checkpoint_error"] = f"{type(exc).__name__}: {exc}"
                    record["errors"].append(f"checkpoint: {record['checkpoint_error']}")

    try:
        if getattr(args, "call_retention", "inline") == "stream":
            if evidence_path is None:
                evidence_path = Path(tempfile.mkdtemp(prefix="poke-timed-calls-")) / "calls.jsonl"
            log = CallEvidenceLog(evidence_path)
            record["call_log"] = log.snapshot()
            record["call_counts"] = {
                "total": 0,
                "completed": 0,
                "interrupted": 0,
                "completed_no_progress": 0,
                "completed_partial": 0,
                "noncompleted": 0,
                "requested_frames": 0,
                "actual_completed_frames": 0,
                "unknown_actual_calls": 0,
            }
        publish("asset_validation")
        record["tcp_nodelay"] = sockets[index].getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        assets = asset_resolver(version, args.repo_root)
        record["provenance"] = assets["provenance"]
        if cancelled.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("diagnostic expired during asset validation")
        session = session_factory(assets["rom"], assets["sym"], **assets["pins"])
        session.load_state(assets["state"])
        record["loaded"] = observed()
        publish("loaded")
        if owner_driver is not None:
            if chunk != 1 or peer_state is None or goal_flag is None or goal_stop is None:
                raise ValueError("owner driver requires whole one-frame calls and shared flags")
            driver = _driver_factory(owner_driver)(
                session=session,
                side=("listen", "connect")[index],
                options=driver_options,
                peer_state=peer_state,
            )
            record["driver_snapshot"] = _jsonable(driver.snapshot())
            record["milestone_observer"] = "owner_driver"
        if getattr(args, "rom_milestones", False) and driver is None:
            from scripts._timed_menu_probe import observe_rom_milestones

            milestone_context = observe_rom_milestones(
                symbols=session.symbols,
                memory=session._pyboy.memory,
                register_file=session._pyboy.register_file,
                frame_count=lambda: session._pyboy.frame_count,
                hook_register=session._pyboy.hook_register,
                hook_deregister=session._pyboy.hook_deregister,
                owner_thread_id=record["owner_thread"],
                enabled=True,
                max_events=MILESTONE_EVENT_LIMIT,
            )
            observer = milestone_context.__enter__()
            record["milestones"] = observer.snapshot()
        endpoint = endpoint_factory(
            sockets[index],
            side=record["side"],
            rom_version=assets["family"],
            deadline=deadline,
            cancel_event=cancelled,
            rearm_budget=args.rearm_budget,
            rearm_instruction_cap=args.rearm_instruction_cap,
            max_edge_lateness=args.max_edge_lateness,
            quantum_cycles=QUANTUM_CYCLES,
            operation_timeout=args.operation_timeout,
        )
        with guard:
            endpoints[index] = endpoint
            if cancelled.is_set():
                endpoint.cancel()
        with session.locked(timeout_s=max(0.001, deadline - time.monotonic())):
            endpoint.attach(session._pyboy, deadline=deadline)
            session.bind_timed_execution(
                endpoint, timeout_s=max(0.001, deadline - time.monotonic())
            )
            bound = True
        record["attached"] = observed()
        record["metadata"] = _jsonable(endpoint.metadata)
        publish("attached_waiting_peer")
        barrier.wait(timeout=max(0.001, deadline - time.monotonic()))
        initial = session._pyboy.frame_count
        while not cancelled.is_set() and time.monotonic() < deadline:
            offset = session._pyboy.frame_count - initial
            remaining = args.frame_limit - offset
            if remaining <= 0:
                record["termination"] = "frame_bound"
                break
            if profile == "menu" and len(json.dumps(_jsonable(record)).encode()) > (
                MAX_REPORT_BYTES - MENU_TERMINAL_RESERVE
            ):
                raise RuntimeError("menu report capacity reached; no further input or tick")
            call = {
                "call_index": call_index,
                "frame_offset": offset,
                "started_monotonic": time.monotonic(),
                "requested_frames": min(chunk, remaining),
                "before": observed(),
            }
            call_index += 1
            record["calls"].append(call)
            if log is not None:
                record["in_flight"] = call
            publish("public_tick")
            try:
                if observer is not None:
                    observer.check()
                if driver is not None or (profile == "menu" and offset not in scheduled_offsets):
                    scheduled_offsets.add(offset)
                    if driver is not None:
                        suggested = driver.before_step(frame_offset=offset)
                    else:
                        from scripts._timed_menu_probe import menu_input_at

                        suggested = menu_input_at(offset)
                    if suggested is not None:
                        button, duration = suggested
                        call["input"] = {
                            "button": button,
                            "duration": duration,
                            "actual_completed_frame_offset": offset,
                            "status": "requested",
                        }
                        session.press(button, duration=duration)
                        call["input"]["status"] = "queued"
                        publish("input_queued_before_public_tick")
                session.step(call["requested_frames"], render=False)
                call["status"] = "completed"
                if observer is not None:
                    observer.check()
            except BaseException as exc:
                call["status"] = "interrupted"
                call["error"] = f"{type(exc).__name__}: {exc}"
                if goal_cancel(exc):
                    call["expected_goal_cancellation"] = True
                raise
            finally:
                call["elapsed_s"] = time.monotonic() - call["started_monotonic"]
                try:
                    call["after"] = observed()
                    call["actual_completed_frames"] = (
                        call["after"]["frame_count"] - call["before"]["frame_count"]
                    )
                except BaseException as exc:
                    if "last_native_observation" in record:
                        call["after"] = record["last_native_observation"]
                        call["actual_completed_frames"] = (
                            call["after"]["frame_count"] - call["before"]["frame_count"]
                        )
                    call["observation_error"] = f"{type(exc).__name__}: {exc}"
                    if "error" not in call:
                        call["status"] = "interrupted"
                        call["error"] = call["observation_error"]
                        raise
                finally:
                    if call.get("status") == "completed":
                        actual = call.get("actual_completed_frames")
                        if actual == 0:
                            call["status"] = "completed_no_progress"
                        elif actual != call["requested_frames"]:
                            call["status"] = "completed_partial"
                    driver_failure = finalization_failure = None
                    if driver is not None:
                        try:
                            driver.after_step(call=call)
                            record["driver_snapshot"] = _jsonable(driver.snapshot())
                            if driver.objective_complete():
                                goal_flag.set()
                            record["local_goal"] = goal_flag.is_set()
                        except BaseException as exc:
                            driver_failure = exc
                            call["driver_error"] = f"{type(exc).__name__}: {exc}"
                            record["errors"].append(f"driver observation: {call['driver_error']}")
                    try:
                        finish_call(call)
                    except BaseException as exc:
                        finalization_failure = exc
                        if log is not None:
                            record["unspooled_call"] = call
                        record["errors"].append(f"call finalization: {type(exc).__name__}: {exc}")
                    finally:
                        publish("public_tick_returned")
                    if "error" not in call:
                        failures = [
                            failure
                            for failure in (driver_failure, finalization_failure)
                            if failure is not None
                        ]
                        if len(failures) == 2:
                            raise BaseExceptionGroup(
                                "driver observation and call finalization failed", failures
                            )
                        if failures:
                            raise failures[0]
            if call["actual_completed_frames"] == 0:
                call["status"] = "completed_no_progress"
                record["termination"] = "no_progress"
                break
            if call["actual_completed_frames"] != call["requested_frames"]:
                call["status"] = "completed_partial"
                record["termination"] = "incomplete_public_call"
                break
        record.setdefault(
            "termination", "goal_cancelled" if goal_cancel() else "cancelled_or_deadline"
        )
    except BaseException as exc:
        if goal_cancel(exc):
            record["expected_goal_cancellation"] = f"{type(exc).__name__}: {exc}"
            record["termination"] = "goal_cancelled"
        else:
            record["errors"].append(f"{type(exc).__name__}: {exc}")
            record["termination"] = "owner_failure"
    finally:
        if owner_driver is None or record.get("termination") != "goal_cancelled":
            done.set()
        # Supervisor signals BOTH peers before either owner tears down.
        cancelled.wait(max(0, overall - time.monotonic()))
        if session is not None:
            if driver is not None:
                try:
                    record["driver_snapshot"] = _jsonable(driver.snapshot())
                    record["local_goal"] = goal_flag.is_set()
                except BaseException as exc:
                    record["errors"].append(
                        f"final driver observation: {type(exc).__name__}: {exc}"
                    )
            try:
                record["final"] = observed()
                publish("owner_unwound")
            except BaseException as exc:
                if "last_native_observation" in record:
                    record["final"] = record["last_native_observation"]
                record["errors"].append(f"final observation: {exc}")
        detached = endpoint is None
        try:
            if endpoint is not None:
                if bound:
                    session.unbind_timed_execution(endpoint, timeout_s=args.cleanup_timeout)
                else:
                    endpoint.close()
                detached = True
                record["cleanup"].append("endpoint_detached")
        except BaseException as exc:
            record["errors"].append(f"cleanup: {type(exc).__name__}: {exc}")
        finally:
            if driver is not None:
                try:
                    close_driver = getattr(driver, "close", None)
                    if close_driver is not None:
                        close_driver()
                        record["cleanup"].append("owner_driver_closed")
                except BaseException as exc:
                    record["errors"].append(f"driver cleanup: {type(exc).__name__}: {exc}")
            if observer is not None:
                try:
                    record["milestones"] = observer.snapshot()
                except Exception as exc:
                    record["errors"].append(f"milestone snapshot: {type(exc).__name__}: {exc}")
                try:
                    milestone_context.__exit__(None, None, None)
                    record["cleanup"].append("milestone_hooks_removed")
                except BaseException as exc:
                    record["errors"].append(f"milestone cleanup: {type(exc).__name__}: {exc}")
                finally:
                    try:
                        record["milestones"] = observer.snapshot()
                    except Exception as exc:
                        record["errors"].append(f"milestone snapshot: {type(exc).__name__}: {exc}")
            if session is not None and detached:
                try:
                    session.close(save=False, timeout_s=max(0.001, overall - time.monotonic()))
                    record["cleanup"].append("session_closed_without_save")
                except BaseException as exc:
                    record["errors"].append(f"cleanup: {type(exc).__name__}: {exc}")
            if log is not None:
                try:
                    log.close(
                        complete=("unspooled_call" not in record and "in_flight" not in record)
                    )
                except Exception as exc:
                    record["errors"].append(f"call log close: {type(exc).__name__}: {exc}")
                record["call_log"] = log.snapshot()
            sockets[index].close()
            publish("cleanup_finished")


def run_pair(args, *, session_factory=None, endpoint_factory=None, asset_resolver=None):
    """Dependency seams allow asset-free owner/lifecycle tests, not gameplay claims."""
    validate_input_profile(args)
    from pokered_harness.link.timed_remote import TimedRemoteEndpoint
    from pokered_harness.session import Session

    session_factory = session_factory or Session.from_files
    endpoint_factory = endpoint_factory or TimedRemoteEndpoint.from_connected_socket
    asset_resolver = asset_resolver or resolve_assets
    started = time.monotonic()
    overall = getattr(args, "absolute_deadline", started + args.overall_timeout)
    deadline = min(overall - args.cleanup_timeout, started + args.pair_timeout)
    if deadline <= started:
        return {"stop_reason": "insufficient_remaining_budget", "owners": [], "threads_alive": []}
    cancelled = threading.Event()
    done = threading.Event()
    barrier = threading.Barrier(2)
    endpoints = [None, None]
    records = [
        {"side": side, "calls": [], "cleanup": [], "errors": []}
        for side in ("listener", "connector")
    ]
    guard = threading.Lock()
    # Bind once and advertise the actual port while retaining the listener.
    connector = accepted = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(max(0.001, deadline - time.monotonic()))
            connector = socket.create_connection(
                listener.getsockname(), timeout=max(0.001, deadline - time.monotonic())
            )
            accepted, _ = listener.accept()
            address = listener.getsockname()
    except BaseException:
        for sock in (connector, accepted):
            if sock is not None:
                sock.close()
        raise
    sockets = (accepted, connector)

    def owner(index):
        _run_owner(
            index,
            args,
            records,
            sockets,
            cancelled,
            done,
            barrier,
            endpoints,
            guard,
            deadline,
            overall,
            session_factory,
            endpoint_factory,
            asset_resolver,
        )

    threads = [
        threading.Thread(target=owner, args=(i,), daemon=True, name=f"timed-rom-{i}")
        for i in range(2)
    ]
    for thread in threads:
        thread.start()
    signalled = done.wait(max(0, deadline - time.monotonic()))
    cancelled.set()
    supervisor_cancel_errors = []
    with guard:
        for index, endpoint in enumerate(endpoints):
            if endpoint is not None:
                try:
                    endpoint.cancel()
                except Exception as exc:
                    supervisor_cancel_errors.append(f"owner {index}: {type(exc).__name__}: {exc}")
    barrier.abort()
    for thread in threads:
        thread.join(max(0, min(overall, deadline + args.cleanup_timeout) - time.monotonic()))
    owners = json.loads(json.dumps(_jsonable(records)))
    for index, thread in enumerate(threads):
        owners[index]["owner_complete"] = not thread.is_alive()
        if thread.is_alive():
            owners[index]["cleanup"] = []
            owners[index]["cleanup_status"] = "incomplete_owner_still_alive"
    return {
        "label": "cancellation_diagnostic_not_graceful_tick_or_gameplay_success",
        "stop_reason": "owner_completion_or_failure" if signalled else "deadline",
        "quantum_cycles": QUANTUM_CYCLES,
        "address": address,
        "elapsed_s": time.monotonic() - started,
        "owners": owners,
        "supervisor_cancel_errors": supervisor_cancel_errors,
        "threads_alive": [t.name for t in threads if t.is_alive()],
    }


class _SharedFlag:
    """Monotonic one-byte signal, with no process-shared lock to strand.

    Only transition is zero to one. Cancellation has one parent writer; done
    writers all store the same value. No read-modify-write operation is used.
    """

    def __init__(self, storage):
        self._storage = storage

    def set(self):
        self._storage.value = 1

    def is_set(self):
        return self._storage.value != 0

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            time.sleep(0.01 if remaining is None else min(0.01, remaining))
        return True


def cancellation_bridge(shared, local, endpoints, guard, stop, errors):
    """Bridge process cancellation to the concrete Event required by the wire API."""
    while not stop.is_set():
        if not shared.wait(0.01):
            continue
        local.set()
        with guard:
            for endpoint in endpoints:
                if endpoint is not None:
                    try:
                        endpoint.cancel()
                    except Exception as exc:
                        errors.append(f"cancel: {type(exc).__name__}: {exc}")
        return


def _capture_stderr(path, fd=2):
    """Drain native fd writes continuously, retaining at most STDERR_LIMIT bytes."""
    read_fd, write_fd = os.pipe()
    saved = os.dup(fd)
    os.dup2(write_fd, fd)
    os.close(write_fd)
    stats = {"path": str(path), "bytes": 0, "total_bytes": 0, "truncated": False}

    def drain():
        try:
            with os.fdopen(read_fd, "rb", buffering=0) as source, open(path, "xb") as output:
                while block := source.read(8192):
                    remaining = max(0, STDERR_LIMIT - stats["bytes"])
                    retained = block[:remaining]
                    output.write(retained)
                    stats["bytes"] += len(retained)
                    stats["total_bytes"] += len(block)
                    stats["truncated"] = stats["total_bytes"] > STDERR_LIMIT
        except Exception as exc:
            stats["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=drain, daemon=True, name=f"fd-{fd}-drain")
    worker.start()
    return saved, worker, stats


def process_owner(
    args_dict,
    index,
    sock,
    cancel_event,
    done_event,
    barrier,
    deadline,
    overall,
    report_sender,
    stderr_path,
    owner_driver=None,
    peer_state=None,
    goal_flag=None,
    goal_stop=None,
    driver_options=None,
):
    """Spawn-safe target: no emulator, callbacks, or test closures cross processes."""
    saved, stderr_worker, stderr = _capture_stderr(stderr_path)
    try:
        stdout_saved, stdout_worker, stdout = _capture_stderr(
            Path(stderr_path).with_suffix(".stdout"), fd=1
        )
    except BaseException:
        os.dup2(saved, 2)
        os.close(saved)
        stderr_worker.join(timeout=0.2)
        raise
    records = [
        {"side": side, "calls": [], "cleanup": [], "errors": []}
        for side in ("listener", "connector")
    ]
    record = records[index]

    def checkpoint(value):
        payload = json.dumps(_jsonable(value)).encode()
        if len(payload) > MAX_REPORT_BYTES:
            raise ValueError("owner checkpoint exceeds byte limit")
        path = Path(stderr_path).with_suffix(".checkpoint.json")
        temporary = path.with_suffix(".pending")
        with temporary.open("wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)

    local, stop = threading.Event(), threading.Event()
    guard = threading.Lock()
    endpoints = [None, None]
    bridge_errors = []
    watcher = threading.Thread(
        target=cancellation_bridge,
        args=(cancel_event, local, endpoints, guard, stop, bridge_errors),
        daemon=True,
    )
    watcher.start()
    try:
        from pokered_harness.link.timed_remote import TimedRemoteEndpoint
        from pokered_harness.session import Session

        args = argparse.Namespace(**args_dict)
        args.repo_root = Path(args.repo_root)
        record["runtime"] = runtime_identity()
        record["child_started_monotonic"] = time.monotonic()
        _run_owner(
            index,
            args,
            records,
            [sock, sock],
            local,
            done_event,
            barrier,
            endpoints,
            guard,
            deadline,
            overall,
            Session.from_files,
            TimedRemoteEndpoint.from_connected_socket,
            resolve_assets,
            checkpoint,
            **(
                {
                    "owner_driver": owner_driver,
                    "peer_state": peer_state,
                    "goal_flag": goal_flag,
                    "goal_stop": goal_stop,
                    "driver_options": driver_options,
                }
                if owner_driver is not None
                else {}
            ),
        )
    except BaseException as exc:
        record["errors"].append(f"child: {type(exc).__name__}: {exc}")
        record["termination"] = "owner_failure"
        done_event.set()
    finally:
        stop.set()
        watcher.join(timeout=0.1)
        record["watcher_alive"] = watcher.is_alive()
        if watcher.is_alive():
            record["errors"].append("cancellation watcher did not stop")
        record["errors"].extend(bridge_errors)
        sock.close()
        for name, fd, original, worker, stats in (
            ("stdout", 1, stdout_saved, stdout_worker, stdout),
            ("stderr", 2, saved, stderr_worker, stderr),
        ):
            try:
                getattr(sys, name).flush()
            except Exception as exc:
                stats["error"] = f"flush: {type(exc).__name__}: {exc}"
            try:
                os.dup2(original, fd)
            except Exception as exc:
                stats["error"] = f"restore: {type(exc).__name__}: {exc}"
            finally:
                os.close(original)
            worker.join(timeout=0.2)
            record[name] = stats
            stats["drainer_alive"] = worker.is_alive()
            if worker.is_alive() or stats.get("error"):
                record["errors"].append(f"{name} capture did not complete cleanly")
        payload = json.dumps(_jsonable(record)).encode()
        if len(payload) > MAX_REPORT_BYTES:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix="poke-timed-owner-overflow-", suffix=".json", delete=False
            ) as artifact:
                artifact.write(payload)
            record = {
                "side": record["side"],
                "calls": [],
                "cleanup": [],
                "errors": ["owner report exceeds byte limit"],
                "termination": "report_overflow",
                "actual_missing": True,
                "stderr": stderr,
                "stdout": stdout,
                "full_evidence": {
                    "path": artifact.name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            }
            payload = json.dumps(record).encode()
        try:
            report_sender.send_bytes(payload)
        finally:
            report_sender.close()


def run_process_pair(
    args, *, context=None, child_target=None, owner_driver=None, owner_driver_phases=None
):
    """Two spawn owners with bounded report drains and terminate/kill fallback."""
    phases = READINESS_PHASES if owner_driver_phases is None else owner_driver_phases
    if (
        not isinstance(phases, tuple)
        or not 1 <= len(phases) <= 32
        or any(
            not isinstance(phase, str) or not phase.strip() or len(phase) > 64 for phase in phases
        )
        or len(set(phases)) != len(phases)
    ):
        raise ValueError(
            "owner driver phases must be 1..32 unique nonempty strings of at most 64 characters"
        )
    if owner_driver_phases is not None and owner_driver is None:
        raise ValueError("custom owner driver phases require an owner driver")
    validate_input_profile(args)
    if owner_driver is not None:
        if not isinstance(owner_driver, str) or owner_driver.count(":") != 1:
            raise ValueError("owner driver must be an importable module:function string")
        if args.listener_chunk != 1 or args.connector_chunk != 1:
            raise ValueError("owner driver requires one-frame chunks")
        if len(getattr(args, "owner_driver_options", [])) != 2:
            raise ValueError("owner driver requires two serialized option mappings")
    context = context or multiprocessing.get_context("spawn")
    child_target = child_target or process_owner
    started = time.monotonic()
    overall = getattr(args, "absolute_deadline", started + args.overall_timeout)
    deadline = min(overall - args.cleanup_timeout, started + args.pair_timeout)
    result = {
        "label": "cancellation_diagnostic_not_graceful_tick_or_gameplay_success",
        "owner_mode": "process",
        "owner_driver_phases": list(phases),
        "quantum_cycles": QUANTUM_CYCLES,
        "owners": [],
        "threads_alive": [],
        "processes_alive": [],
        "supervisor_cancel_errors": [],
    }
    if owner_driver is not None:
        result["owner_driver"] = owner_driver
    if deadline <= started:
        return dict(result, stop_reason="insufficient_remaining_budget")
    artifact_dir = Path(tempfile.mkdtemp(prefix="poke-timed-process-"))
    result["artifact_dir"] = str(artifact_dir)
    cancel = _SharedFlag(context.RawValue("B", 0))
    done = _SharedFlag(context.RawValue("B", 0))
    barrier = context.Barrier(2)
    if owner_driver is not None:
        readiness = context.RawArray("B", 2 * len(phases))
        goals = [_SharedFlag(context.RawValue("B", 0)) for _ in range(2)]
        goal_stop = _SharedFlag(context.RawValue("B", 0))
    processes, receivers, senders, readers, sockets = [], [], [], [], []
    records = [
        {"side": side, "calls": [], "cleanup": [], "errors": [], "termination": "missing_report"}
        for side in ("listener", "connector")
    ]

    def receive(index, connection):
        try:
            packet = json.loads(connection.recv_bytes(MAX_REPORT_BYTES))
            if not isinstance(packet, dict) or any(
                not isinstance(packet.get(key), list) for key in ("calls", "cleanup", "errors")
            ):
                raise ValueError("invalid owner report schema")
            records[index] = packet
        except Exception as exc:
            records[index]["errors"].append(f"report: {type(exc).__name__}: {exc}")

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(max(0.001, deadline - time.monotonic()))
            result["address"] = listener.getsockname()
            connector = socket.create_connection(
                result["address"], timeout=max(0.001, deadline - time.monotonic())
            )
            sockets.append(connector)
            accepted, _ = listener.accept()
            sockets.insert(0, accepted)
        result["tcp_nodelay"] = [
            sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) for sock in sockets
        ]
        for index, sock in enumerate(sockets):
            if time.monotonic() >= deadline:
                raise TimeoutError("process startup deadline expired")
            receiver, sender = context.Pipe(duplex=False)
            receivers.append(receiver)
            senders.append(sender)
            process = context.Process(
                target=child_target,
                args=(
                    _jsonable(vars(args)),
                    index,
                    sock,
                    cancel,
                    done,
                    barrier,
                    deadline,
                    overall,
                    sender,
                    str(artifact_dir / f"owner-{index}.stderr"),
                )
                + (
                    (
                        owner_driver,
                        _PeerReadiness(readiness, index, phases),
                        goals[index],
                        goal_stop,
                        _jsonable(args.owner_driver_options[index]),
                    )
                    if owner_driver is not None
                    else ()
                ),
                name=f"timed-rom-process-{index}",
                daemon=True,
            )
            process.start()
            processes.append(process)
            sender.close()
            reader = threading.Thread(target=receive, args=(index, receiver), daemon=True)
            reader.start()
            readers.append(reader)
        for sock in sockets:
            sock.close()
        while time.monotonic() < deadline and not done.wait(0.01):
            if owner_driver is not None and all(flag.is_set() for flag in goals):
                goal_stop.set()
                break
            if any(not process.is_alive() for process in processes):
                break
        result["stop_reason"] = (
            "both_owner_goals"
            if owner_driver is not None and goal_stop.is_set()
            else "owner_completion_or_failure"
            if done.is_set()
            else "deadline"
        )
    except Exception as exc:
        result["stop_reason"] = "startup_failure"
        result["supervisor_cancel_errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        result["cancelled_monotonic"] = time.monotonic()
        cancel.set()
        if owner_driver is not None:
            result["owner_goals"] = [flag.is_set() for flag in goals]
            result["goal_stop"] = goal_stop.is_set()
        # A lock-free shared byte reaches children before endpoint publication.
        # Do not acquire the barrier lock: a killed child may own it.
        cleanup_deadline = min(overall, time.monotonic() + args.cleanup_timeout)
        graceful_deadline = max(time.monotonic(), cleanup_deadline - 0.4)
        for process in processes:
            process.join(max(0, graceful_deadline - time.monotonic()))
        forced = set()
        for index, process in enumerate(processes):
            if process.is_alive():
                forced.add(index)
                process.terminate()
        for process in processes:
            process.join(max(0, cleanup_deadline - 0.2 - time.monotonic()))
        for process in processes:
            if process.is_alive():
                process.kill()
        for process in processes:
            process.join(max(0, cleanup_deadline - time.monotonic()))
        for reader in readers:
            reader.join(max(0, cleanup_deadline - time.monotonic()))
        result["owners"] = json.loads(json.dumps(records))
        for index, record in enumerate(result["owners"]):
            checkpoint_path = artifact_dir / f"owner-{index}.checkpoint.json"
            if "final" not in record and checkpoint_path.exists():
                try:
                    with checkpoint_path.open("rb") as stream:
                        payload = stream.read(MAX_REPORT_BYTES + 1)
                    if len(payload) > MAX_REPORT_BYTES:
                        raise ValueError("checkpoint exceeds byte limit")
                    record["last_checkpoint"] = json.loads(payload)
                    record["checkpoint_is_final"] = False
                except Exception as exc:
                    record["errors"].append(f"checkpoint: {type(exc).__name__}: {exc}")
            process = processes[index] if index < len(processes) else None
            alive = process is not None and process.is_alive()
            record.update(
                pid=process.pid if process else None,
                exitcode=process.exitcode if process else None,
                alive=alive,
                owner_complete=process is not None and not alive,
                forced_termination=index in forced,
                actual_missing="final" not in record,
            )
            if index in forced or alive:
                record["cleanup"] = []
                record["cleanup_status"] = "nongraceful_forced_or_live"
            if alive:
                result["processes_alive"].append(process.pid)
            if process is None or process.exitcode != 0 or index in forced:
                record["errors"].append("owner did not exit normally")
            for name in ("stderr", "stdout"):
                path = artifact_dir / f"owner-{index}.{name}"
                record.setdefault(
                    name,
                    {
                        "path": str(path),
                        "bytes": path.stat().st_size if path.exists() else 0,
                        "truncated": None,
                    },
                )
        result["report_readers_alive"] = [
            i for i, reader in enumerate(readers) if reader.is_alive()
        ]
        for resource in (*sockets, *senders, *receivers):
            resource.close()
        result["elapsed_s"] = time.monotonic() - started
    return result


def run_probe(args):
    started = time.monotonic()
    args.absolute_deadline = started + args.overall_timeout
    report = {
        "label": "cancellation_diagnostic",
        "runtime": runtime_identity(),
        "options": _jsonable(vars(args)),
        "pairs": [],
    }
    runner = run_process_pair if getattr(args, "owner_mode", "thread") == "process" else run_pair

    def collect(options):
        try:
            return runner(options)
        except Exception as exc:
            # Preserve prior orientation evidence if a subsequent startup fails.
            return {
                "stop_reason": "startup_failure",
                "owners": [],
                "threads_alive": [],
                "supervisor_cancel_errors": [f"{type(exc).__name__}: {exc}"],
            }

    report["pairs"].append(collect(args))
    if (
        args.both_orientations
        and not report["pairs"][-1]["threads_alive"]
        and not report["pairs"][-1].get("processes_alive")
        and not report["pairs"][-1].get("report_readers_alive")
    ):
        reverse = argparse.Namespace(**vars(args))
        reverse.listener, reverse.connector = args.connector, args.listener
        report["pairs"].append(collect(reverse))
    report["elapsed_s"] = time.monotonic() - started
    return report


def main(argv=None):
    args = parse_args(argv)
    try:
        report = run_probe(args)
    except Exception as exc:
        report = {"label": "cancellation_diagnostic", "error": f"{type(exc).__name__}: {exc}"}
    for pair in report.get("pairs", []):
        for owner in pair.get("owners", []):
            try:
                if (
                    getattr(args, "call_retention", "inline") == "stream"
                    and "call_log" not in owner
                ):
                    raise ValueError("streamed call artifact manifest is missing")
                validate_call_artifact(owner)
            except Exception as exc:
                owner.setdefault("errors", []).append(f"call artifact: {type(exc).__name__}: {exc}")
    # Exclusive creation prevents overwriting any existing evidence/assets.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, default=str)
        stream.write("\n")
    print(f"Cancellation diagnostic evidence: {args.output}")
    return (
        2
        if "error" in report
        or any(
            pair["threads_alive"]
            or pair.get("processes_alive")
            or pair.get("report_readers_alive")
            or pair.get("supervisor_cancel_errors")
            or pair["stop_reason"] != "owner_completion_or_failure"
            or any(
                owner["errors"]
                or owner.get("termination") != "frame_bound"
                or owner.get("alive")
                or owner.get("forced_termination")
                or owner.get("actual_missing")
                or owner.get("exitcode", 0) != 0
                or owner.get("watcher_alive")
                or owner.get("stderr", {}).get("drainer_alive")
                or owner.get("stderr", {}).get("error")
                or owner.get("stdout", {}).get("drainer_alive")
                or owner.get("stdout", {}).get("error")
                or owner.get("call_counts", {}).get("noncompleted", 0)
                or owner.get("call_log", {}).get("error")
                or ("call_log" in owner and not owner["call_log"]["complete"])
                or owner.get("milestones", {}).get("error")
                or (
                    "call_counts" not in owner
                    and any(call.get("status") != "completed" for call in owner.get("calls", []))
                )
                for owner in pair["owners"]
            )
            for pair in report.get("pairs", [])
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
