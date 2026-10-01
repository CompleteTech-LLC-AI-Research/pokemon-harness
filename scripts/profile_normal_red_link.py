"""Opt-in fixed-history profiles; allocation admission precedes any ROM owner."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from pokered_harness.qualification_profile import (
    CallRecorder,
    external_directory,
    runtime_identity,
    source_identity,
)

ROOT = Path(__file__).resolve().parents[1]
WALL_BOUND = 1200
MAX_ACTIONS = 50_000
MAX_STEP_FRAMES = 18_000
BUTTONS = frozenset({"a", "b", "up", "down", "left", "right", "start", "select"})
BASELINE_FILES = (
    "scripts/normal_red_link_admission.py",
    "scripts/normal_red_link_journal.py",
    "scripts/normal_red_battle_drive.py",
    "scripts/normal_red_battle_return.py",
    "scripts/qualify_normal_red_link.py",
)


def validate_history(document):
    if (
        not isinstance(document, dict)
        or type(document.get("history_version")) is not int
        or document["history_version"] != 1
    ):
        raise ValueError("unknown fixed-history schema")
    operations = document.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_ACTIONS:
        raise ValueError("fixed-history action bound")
    frames = 0
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise TypeError("history operation must be an object")
        name = operation.get("operation")
        allowed = {"operation"}
        if name == "link_up":
            if index != 0:
                raise ValueError("history must link exactly once at its start")
            allowed.add("arm_barrier")
            if "arm_barrier" in operation and type(operation["arm_barrier"]) is not bool:
                raise ValueError("unknown frame-barrier choice")
        elif name == "step":
            allowed.add("count")
            count = operation.get("count")
            if type(count) is not int or not 1 <= count <= 120:
                raise ValueError("fixed-history step bound")
            frames += count
        elif name in ("press", "release", "state", "records", "rom_events"):
            allowed.add("owner")
            if type(operation.get("owner")) is not int or operation["owner"] not in (0, 1):
                raise ValueError("unknown history owner")
            if name in ("press", "release"):
                allowed.add("button")
                if type(operation.get("button")) is not str or operation["button"] not in BUTTONS:
                    raise ValueError("unknown public button")
            if name == "press":
                allowed.add("duration")
                duration = operation.get("duration")
                if type(duration) is not int or not 1 <= duration <= 60:
                    raise ValueError("fixed-history press bound")
        else:
            raise ValueError("unsupported history operation")
        if set(operation) - allowed:
            raise ValueError("history contains undeclared arguments")
    if operations[0].get("operation") != "link_up" or frames > MAX_STEP_FRAMES:
        raise ValueError("fixed-history initial link/frame bound")
    return operations


def baseline_sources(expected_head):
    import subprocess

    measured = source_identity(ROOT, expected_head)
    files = {}
    for relative in BASELINE_FILES:
        actual = (ROOT / relative).read_bytes()
        committed = subprocess.check_output(
            ["git", "--work-tree=" + str(ROOT), "show", expected_head + ":" + relative], cwd=ROOT
        )
        if actual != committed:
            raise ValueError("baseline controller source differs from commit")
        files[relative] = hashlib.sha256(actual).hexdigest()
    measured["checked_baseline_controller_files"] = files
    return measured


def allocation_admission(declaration_path, policy_path, output):
    """Reuse the actual pinned live reservation and resource/capacity checks."""
    from scripts.gate_capacity_admission import CapacitySession
    from scripts.gate_capacity_policy import load_capacity_policy
    from scripts.qualification_runner_declaration import validate_declaration
    from scripts.qualification_runner_facts import collect_facts
    from scripts.qualification_runner_report import load_declaration
    from scripts.qualification_runner_reservation import evaluate_resources

    declaration, error = load_declaration(declaration_path)
    if error:
        raise ValueError("runner declaration unavailable")
    checks = validate_declaration(declaration)
    if any(check.status != "ok" for check in checks):
        raise ValueError("runner declaration not admitted")
    facts = collect_facts(ROOT, output)
    checks.extend(evaluate_resources(declaration, facts, ROOT))
    (output / "reservation-checks.json").write_text(
        json.dumps([asdict(check) for check in checks], indent=2) + "\n"
    )
    if any(check.status != "ok" for check in checks):
        raise ValueError("live held reservation/resources not admitted")
    policy, error = load_capacity_policy(policy_path)
    if error or policy is None or policy.runner_id != declaration["runner_id"]:
        raise ValueError("capacity policy is not bound to the runner")
    if policy.max_concurrent_pairs != 1:
        raise ValueError("profiles require one admitted pair")
    capacity = CapacitySession(policy, repo_root=ROOT, temp_root=output)
    if capacity.ensure_started() != "ok":
        raise ValueError("profile capacity unavailable")
    decision = capacity.admission.admit("fixed-history-profile")
    if not decision.owner:
        raise ValueError("profile pair not admitted")
    capacity.telemetry.mark("fixed-history-profile", "running")
    return capacity


@contextmanager
def owner_launches(output, mode, transport, expected_head, cprofile):
    """Replace only this explicit profiler process's pair-launch references."""
    from tests import _mcp_trade_records_rom_support as support
    from tests import _mcp_trade_records_rom_tests_support as tcp_support
    from tests.test_mcp_timed_rom import PIPE_CAP, RomClient

    originals = support._launch_server, tcp_support._launch_server
    streams = []
    counter = 0

    async def launch(environment):
        nonlocal counter
        owner = "local-pair" if transport == "local" else f"tcp-owner-{counter}"
        owners = "0,1" if transport == "local" else str(counter)
        if counter >= (1 if transport == "local" else 2):
            raise ValueError("profile owner launch bound")
        counter += 1
        stream = (output / f"rpc-{owner}.jsonl").open("x")
        streams.append(stream)
        command = [
            sys.executable,
            "-m",
            "scripts.profile_mcp_owner",
            "--output",
            str(output / "owners" / owner),
            "--runtime",
            mode,
            "--owners",
            owners,
            "--expected-head",
            expected_head,
        ]
        if cprofile:
            command.append("--cprofile")
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *command,
                cwd=ROOT,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=PIPE_CAP,
                start_new_session=True,
            ),
            30,
        )
        client = RomClient(process)
        recorder = CallRecorder(stream, owner, process.pid)
        original_request = client.request

        async def measured_request(method, params):
            return await recorder.request(original_request, method, params)

        client.request = measured_request
        return client

    support._launch_server = tcp_support._launch_server = launch
    try:
        yield
    finally:
        support._launch_server, tcp_support._launch_server = originals
        for stream in streams:
            stream.close()


async def replay(pair, operations, stream):
    validate_history({"history_version": 1, "operations": operations})
    for sequence, operation in enumerate(operations, 1):
        name = operation["operation"]
        before = time.perf_counter_ns()
        failure = None
        result = None
        try:
            if name == "link_up":
                result = await pair.link_up(arm_barrier=operation.get("arm_barrier"))
            elif name == "step":
                result = await pair.step(operation["count"])
            elif name == "press":
                result = await pair.press(
                    operation["owner"], operation["button"], duration=operation["duration"]
                )
            elif name == "release":
                clients = [pair.client] if pair.transport == "local_pair" else pair.clients
                owner = operation["owner"]
                client = clients[0] if len(clients) == 1 else clients[owner]
                tool = "link_peer_release" if len(clients) == 1 and owner == 1 else "release"
                listed = await client.request("tools/list", {})
                if tool not in {row["name"] for row in listed["tools"]}:
                    raise ValueError("owner does not advertise public release")
                result = await client.tool(tool, {"button": operation["button"]})
            else:
                result = await getattr(pair, name)(operation["owner"])
        except BaseException as exc:
            failure = exc
            raise
        finally:
            row = {
                "sequence": sequence,
                "operation": operation,
                "wall_start_ns": before,
                "wall_end_ns": time.perf_counter_ns(),
                "completion": "returned" if failure is None else "raised",
                "error_type": type(failure).__name__ if failure else None,
                "result_sha256": hashlib.sha256(
                    json.dumps(result, sort_keys=True).encode()
                ).hexdigest(),
            }
            try:
                stream.write(json.dumps(row) + "\n")
                stream.flush()
            except (OSError, ValueError) as evidence_error:
                if failure is None:
                    raise
                failure.add_note(f"profile history write failed: {type(evidence_error).__name__}")


async def run_history(output, asset, transport, operations):
    from tests._mcp_trade_records_rom_oracle_support import _fixture_pair
    from tests._mcp_trade_records_rom_support import LocalPair, _child_runtime_mode, _stdio_server
    from tests._mcp_trade_records_rom_tests_support import _tcp_pair_servers

    mode = _child_runtime_mode()
    context = (
        _stdio_server(output, asset, asset)
        if transport == "local"
        else _tcp_pair_servers(output, asset, asset)
    )
    async with context as endpoint:
        pair = LocalPair(endpoint, mode) if transport == "local" else endpoint
        await _fixture_pair(pair, asset, asset, fixture="normal-red-cerulean-six-party-v1")
        with (output / "history-execution.jsonl").open("x") as stream:
            await replay(pair, operations, stream)
        await pair.eof()


def asset_identity(asset):
    """Retain validated input pins and state digest, never asset/record bytes."""
    return {
        "family": asset["family"],
        "pins": asset["pins"],
        "fixture_size": len(asset["state"]),
        "fixture_sha256": hashlib.sha256(asset["state"]).hexdigest(),
    }


def measurement_report(output, transport, expected_head, controller_runtime):
    """Missing owner evidence never becomes an inferred runtime/profile PASS."""
    labels = ["local-pair"] if transport == "local" else ["tcp-owner-0", "tcp-owner-1"]
    unknown = []
    owners = []
    for index, label in enumerate(labels):
        expected_ids = [0, 1] if transport == "local" else [index]
        try:
            start = json.loads((output / "owners" / label / "owner-start.json").read_text())
            end = json.loads((output / "owners" / label / "owner-end.json").read_text())
            after = end["runtime_identity_after"]
            valid = (
                start["owner_ids"] == expected_ids
                and start["source_identity"]["commit"] == expected_head
                and start["build_fingerprint"]
                == after["build_fingerprint"]
                == controller_runtime["build_fingerprint"]
                and start["requested_mode"]
                == after["requested_mode"]
                == controller_runtime["requested_mode"]
                and start["pyboy_revision"]
                == after["pyboy_revision"]
                == controller_runtime["pyboy_revision"]
                and start["os_executable_status"]
                == after["os_executable_status"]
                == controller_runtime["os_executable_status"]
                == "measured"
                and start["os_executable"]
                == after["os_executable"]
                == controller_runtime["os_executable"]
                and start["pid"] == after["pid"]
                and start["process_cpu_identity"]["status"] == "measured"
                and end["status"] == "RETURNED"
                and not end["profile_errors"]
            )
            if not valid:
                unknown.append(label + ": owner runtime or completion mismatch")
            rows = [
                json.loads(line)
                for line in (output / f"rpc-{label}.jsonl").read_text().splitlines()
            ]
            if not rows or any(
                row.get("cpu_sample_status") != "measured"
                or row.get("pid") != start["pid"]
                or row.get("owner_cpu_start", {}).get("process_start_ticks")
                != start["process_cpu_identity"].get("process_start_ticks")
                for row in rows
            ):
                unknown.append(label + ": process CPU sample unavailable/mismatched")
            owners.append(
                {
                    "role": label,
                    "owner_ids": expected_ids,
                    "pid": start["pid"],
                    "cpu_scope": "pair-process" if transport == "local" else "owner-process",
                    "build_fingerprint": start["build_fingerprint"],
                    "rpc_count": len(rows),
                }
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            unknown.append(label + ": " + type(exc).__name__)
    if len({row["pid"] for row in owners}) != len(owners):
        unknown.append("independent TCP owners share a PID")
    return {
        "status": "complete" if not unknown else "unknown",
        "unknown": unknown,
        "owners": owners,
        "qualified_profile": False,
        "cpu_accounting": "one server PID for local pair; do not sum owner views",
        "cprofile_scope": "main-thread Python calls; worker/native attribution unavailable",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "rom",
        "symbols",
        "fixture-root",
        "output",
        "history",
        "runner-declaration",
        "capacity-policy",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--runtime", choices=("source", "cython"), required=True)
    parser.add_argument("--transport", choices=("local", "tcp"), required=True)
    parser.add_argument("--cprofile", action="store_true")
    arguments = parser.parse_args()
    from scripts.normal_red_link_admission import resolve_normal_red_assets

    source = baseline_sources(arguments.expected_head)
    runtime = runtime_identity(ROOT, arguments.runtime)
    history = arguments.history.read_bytes()
    if len(history) > 2 * 1024 * 1024:
        raise ValueError("fixed-history file bound")
    operations = validate_history(json.loads(history))
    output = external_directory(arguments.output, ROOT)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    receipt = {
        "status": "BLOCKED",
        "source": source,
        "controller_runtime": runtime,
        "history_sha256": hashlib.sha256(history).hexdigest(),
        "wall_bound_seconds": WALL_BOUND,
        "scope": "fixed-history telemetry only; no trade/battle/release acceptance",
    }
    capacity = None
    failure = None
    started = time.perf_counter_ns()
    previous_mode = os.environ.get("POKERED_TRADE_RUNTIME_MODE")
    try:
        capacity = allocation_admission(
            arguments.runner_declaration, arguments.capacity_policy, output
        )
        asset = resolve_normal_red_assets(arguments.rom, arguments.symbols, arguments.fixture_root)
        receipt["asset_identity"] = asset_identity(asset)
        os.environ["POKERED_TRADE_RUNTIME_MODE"] = arguments.runtime
        with (
            capacity.monitoring(),
            owner_launches(
                output,
                arguments.runtime,
                arguments.transport,
                arguments.expected_head,
                arguments.cprofile,
            ),
        ):
            asyncio.run(
                asyncio.wait_for(
                    run_history(output, asset, arguments.transport, operations), WALL_BOUND
                )
            )
        receipt["status"] = "COMPLETED_HISTORY"
        receipt["owned_process_cleanup_completed"] = True
        receipt["measurement"] = measurement_report(
            output, arguments.transport, arguments.expected_head, runtime
        )
    except BaseException as exc:
        failure = exc
        receipt["status"] = "FAILED" if capacity is not None else "BLOCKED"
        receipt["error_type"] = type(exc).__name__
        raise
    finally:
        if previous_mode is None:
            os.environ.pop("POKERED_TRADE_RUNTIME_MODE", None)
        else:
            os.environ["POKERED_TRADE_RUNTIME_MODE"] = previous_mode
        receipt["wall_end_ns"] = time.perf_counter_ns()
        receipt["wall_start_ns"] = started
        evidence_failure = None
        if capacity:
            try:
                capacity.admission.release("fixed-history-profile")
                capacity.telemetry.mark(
                    "fixed-history-profile", "completed" if failure is None else "interrupted"
                )
                receipt["capacity"] = capacity.report(failed=int(failure is not None))
            except Exception as evidence_error:  # noqa: BLE001 -- retain original failure or re-raise after receipt
                receipt["capacity_evidence_status"] = "unknown"
                if failure is None:
                    evidence_failure = evidence_error
                    receipt["status"] = "FAILED"
                    receipt["error_type"] = type(evidence_error).__name__
                else:
                    failure.add_note(
                        f"profile capacity evidence failed: {type(evidence_error).__name__}"
                    )
        try:
            (output / "profile-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        except (OSError, ValueError) as evidence_error:
            if failure is None:
                raise
            failure.add_note(f"profile receipt write failed: {type(evidence_error).__name__}")

        if evidence_failure is not None:
            raise evidence_failure


if __name__ == "__main__":
    main()
