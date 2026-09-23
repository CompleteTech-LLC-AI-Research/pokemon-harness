"""Bounded cancellation diagnostic, never a graceful tick/gameplay qualification.

Uses immutable canonical fixtures and whole public Session.step calls at Q256.
Five-second operation timeouts were qualified on authored ROMs only; callers
must explicitly select all timing policy budgets for commercial-ROM diagnostics.
Run under an outer process timeout: Python cannot preempt a stuck native call.
"""

# This is the entry point (issue #146): it keeps the CLI (``parse_args``), the owner
# orchestration (``_run_owner``), the spawned child entry (``process_owner``), the
# thread/process dispatch (``run_probe``) and ``main``. The asset resolution, evidence
# log, and validation helpers live in :mod:`scripts._probe_timed_rom_pair_support` and
# are re-exported below so the public module surface is unchanged. ``parse_args`` stays
# here so its ``description=__doc__`` reads this module's docstring, as before.

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing  # noqa: F401  (retained facade attribute: probe.multiprocessing)
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Record failures without suppressing their diagnostic status or owner cleanup.
# ruff: noqa: BLE001


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


# ``run_pair``/``run_process_pair`` live in the support module and call back into
# ``_run_owner``/``process_owner`` defined above; the support module imports those two
# names from here at the bottom of its own body. Binding the re-exports after the
# definitions keeps the mutual import resolvable in either import order.
from scripts._probe_timed_rom_pair_support import (
    CALL_LOG_BYTE_LIMIT,  # noqa: F401  (retained facade attribute: probe.CALL_LOG_BYTE_LIMIT)
    INLINE_CALL_WINDOW,
    MAX_REPORT_BYTES,
    MENU_TERMINAL_RESERVE,
    MILESTONE_EVENT_LIMIT,
    QUANTUM_CYCLES,
    READINESS_PHASES,  # noqa: F401  (retained facade attribute: probe.READINESS_PHASES)
    STDERR_LIMIT,  # noqa: F401  (retained facade attribute: probe.STDERR_LIMIT)
    VERSIONS,
    CallEvidenceLog,
    _capture_stderr,
    _driver_factory,
    _fixture_kind,  # noqa: F401  (retained facade attribute: probe._fixture_kind)
    _jsonable,
    _PeerReadiness,  # noqa: F401  (retained facade attribute: probe._PeerReadiness)
    _SharedFlag,  # noqa: F401  (retained facade attribute: probe._SharedFlag)
    cancellation_bridge,
    dataclasses,  # noqa: F401  (retained facade attribute: probe.dataclasses)
    find_rom_root,  # noqa: F401  (retained facade attribute: probe.find_rom_root)
    fixture_path,  # noqa: F401  (retained facade attribute: probe.fixture_path)
    importlib,  # noqa: F401  (retained facade attribute: probe.importlib)
    math,  # noqa: F401  (retained facade attribute: probe.math)
    observe,
    positive_float,
    positive_int,
    resolve_assets,
    run_pair,
    run_process_pair,
    runtime_identity,
    stat,  # noqa: F401  (retained facade attribute: probe.stat)
    validate_call_artifact,
    validate_input_profile,
)

if __name__ == "__main__":
    raise SystemExit(main())
