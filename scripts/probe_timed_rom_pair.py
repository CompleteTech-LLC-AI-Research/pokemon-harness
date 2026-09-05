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
import socket
import sys
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
    return parser.parse_args(argv)


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


def run_pair(args, *, session_factory=None, endpoint_factory=None, asset_resolver=None):
    """Dependency seams allow asset-free owner/lifecycle tests, not gameplay claims."""
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
        record = records[index]
        session = endpoint = None
        bound = False
        version = (args.listener, args.connector)[index]
        chunk = (args.listener_chunk, args.connector_chunk)[index]
        record.update(version=version, owner_thread=threading.get_ident())
        try:
            assets = asset_resolver(version, args.repo_root)
            record["provenance"] = assets["provenance"]
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("diagnostic expired during asset validation")
            session = session_factory(assets["rom"], assets["sym"], **assets["pins"])
            session.load_state(assets["state"])
            record["loaded"] = observe(session)
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
            record["attached"] = observe(session, endpoint)
            record["metadata"] = _jsonable(endpoint.metadata)
            barrier.wait(timeout=max(0.001, deadline - time.monotonic()))
            initial = session._pyboy.frame_count
            while not cancelled.is_set() and time.monotonic() < deadline:
                remaining = args.frame_limit - (session._pyboy.frame_count - initial)
                if remaining <= 0:
                    record["termination"] = "frame_bound"
                    break
                call = {
                    "requested_frames": min(chunk, remaining),
                    "before": observe(session, endpoint),
                }
                record["calls"].append(call)
                try:
                    session.step(call["requested_frames"], render=False)
                    call["status"] = "completed"
                except BaseException as exc:
                    call["status"] = "interrupted"
                    call["error"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    try:
                        call["after"] = observe(session, endpoint)
                        call["actual_completed_frames"] = (
                            call["after"]["frame_count"] - call["before"]["frame_count"]
                        )
                    except BaseException as exc:
                        call["observation_error"] = f"{type(exc).__name__}: {exc}"
                        if "error" not in call:
                            raise
                if call["actual_completed_frames"] == 0:
                    call["status"] = "completed_no_progress"
                    record["termination"] = "no_progress"
                    break
            record.setdefault("termination", "cancelled_or_deadline")
        except BaseException as exc:
            record["errors"].append(f"{type(exc).__name__}: {exc}")
            record["termination"] = "owner_failure"
        finally:
            done.set()
            # Supervisor signals BOTH peers before either owner tears down.
            cancelled.wait(max(0, overall - time.monotonic()))
            if session is not None:
                try:
                    record["final"] = observe(session, endpoint)
                except BaseException as exc:
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
                if session is not None and detached:
                    session.close(save=False, timeout_s=max(0.001, overall - time.monotonic()))
                    record["cleanup"].append("session_closed_without_save")
            except BaseException as exc:
                record["errors"].append(f"cleanup: {type(exc).__name__}: {exc}")
            finally:
                sockets[index].close()

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


def run_probe(args):
    started = time.monotonic()
    args.absolute_deadline = started + args.overall_timeout
    report = {
        "label": "cancellation_diagnostic",
        "runtime": runtime_identity(),
        "options": _jsonable(vars(args)),
        "pairs": [],
    }
    report["pairs"].append(run_pair(args))
    if args.both_orientations and not report["pairs"][-1]["threads_alive"]:
        reverse = argparse.Namespace(**vars(args))
        reverse.listener, reverse.connector = args.connector, args.listener
        report["pairs"].append(run_pair(reverse))
    report["elapsed_s"] = time.monotonic() - started
    return report


def main(argv=None):
    args = parse_args(argv)
    try:
        report = run_probe(args)
    except Exception as exc:
        report = {"label": "cancellation_diagnostic", "error": f"{type(exc).__name__}: {exc}"}
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
            or pair.get("supervisor_cancel_errors")
            or pair["stop_reason"] != "owner_completion_or_failure"
            or any(
                owner["errors"] or owner.get("termination") != "frame_bound"
                for owner in pair["owners"]
            )
            for pair in report.get("pairs", [])
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
