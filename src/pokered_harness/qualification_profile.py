"""Opt-in, local qualification metadata; never imported by the MCP entry point."""

from __future__ import annotations

import hashlib
import importlib
import importlib.machinery
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def external_directory(path: Path, repository: Path) -> Path:
    """Reject evidence in the source checkout, including symlink aliases."""
    path = path.resolve()
    repository = repository.resolve()
    if path == repository or repository in path.parents:
        raise ValueError("profile evidence must be outside the source checkout")
    if any(
        (parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file()
        for parent in (path, *path.parents)
    ):
        raise ValueError("profile evidence must be outside Git checkouts")
    return path


def module_identity(module) -> dict:
    origin = getattr(module, "__file__", None)
    if not origin:
        raise ValueError("runtime module has no measurable file origin")
    path = Path(origin).resolve(strict=True)
    if str(path).endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
        kind = "extension"
    elif path.suffix == ".py":
        kind = "source"
    else:
        raise ValueError("runtime module kind is unknown")
    return {
        "path": str(path),
        "kind": kind,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def runtime_identity(repository: Path, mode: str, *, importer=importlib.import_module) -> dict:
    """Measure this process's actual imported classes/core before any owner launch."""
    from pokered_harness.config import load_versions

    if mode not in ("source", "cython"):
        raise ValueError("unknown requested runtime")
    pins = load_versions(repository / "VERSIONS.md")
    if not pins.pyboy_revision:
        raise ValueError("runtime revision pin is unknown")
    package = importer("pyboy")
    version = getattr(package, "__version__", None)
    revision = getattr(package, "__pokered_harness_revision__", None)
    if version != pins.pyboy_version or revision != pins.pyboy_revision:
        raise ValueError("actual imported PyBoy does not match VERSIONS pins")
    modules = {}
    classes = {}
    for name, class_name in (
        ("pyboy.pyboy", "PyBoy"),
        ("pyboy.core.cpu", "CPU"),
        ("pyboy.core.serial", "Serial"),
        ("pyboy.core.mb", None),
    ):
        module = importer(name)
        modules[name] = module_identity(module)
        if class_name:
            actual_class = getattr(module, class_name)
            if not isinstance(actual_class, type):
                raise ValueError("runtime export is not a measured class")
            if class_name == "PyBoy" and getattr(package, "PyBoy", None) is not actual_class:
                raise ValueError("package PyBoy constructor differs from measured runtime class")
            actual_module_name = getattr(actual_class, "__module__", None)
            if not actual_module_name:
                raise ValueError("runtime class has no measurable module")
            classes[class_name] = {
                "module": actual_module_name,
                "origin": module_identity(importer(actual_module_name)),
            }
    vendor = (repository / "vendor" / "pyboy-src").resolve()
    identities = [*modules.values(), *(row["origin"] for row in classes.values())]
    for identity in identities:
        path = Path(identity["path"])
        vendored = vendor == path or vendor in path.parents
        if mode == "source" and (identity["kind"] != "source" or not vendored):
            raise ValueError("source runtime requires actual vendored Python modules/classes")
        if mode == "cython" and (identity["kind"] != "extension" or vendored):
            raise ValueError("native runtime requires actual installed extensions/classes")
    build = {
        name: {key: value[key] for key in ("kind", "sha256")} for name, value in modules.items()
    }
    build["classes"] = {
        name: {
            "module": row["module"],
            "origin": {key: row["origin"][key] for key in ("kind", "sha256")},
        }
        for name, row in classes.items()
    }
    build["version"] = version
    build["revision"] = revision
    return {
        "requested_mode": mode,
        "python": sys.version,
        "executable": sys.executable,
        "os_executable": str(Path("/proc/self/exe").resolve(strict=True))
        if Path("/proc/self/exe").exists()
        else str(Path(sys.executable).resolve()),
        "os_executable_status": "measured" if Path("/proc/self/exe").exists() else "unknown",
        "pid": os.getpid(),
        "pyboy_version": version,
        "pyboy_revision": revision,
        "modules": modules,
        "classes": classes,
        "build_fingerprint": hashlib.sha256(json.dumps(build, sort_keys=True).encode()).hexdigest(),
    }


def owner_cpu(pid: int) -> dict:
    """Linux process CPU, including owner worker threads; unknown stays explicit."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        return {
            "status": "measured",
            "user_seconds": int(fields[11]) / ticks,
            "system_seconds": int(fields[12]) / ticks,
            "resolution_seconds": 1 / ticks,
            "process_start_ticks": int(fields[19]),
        }
    except (OSError, ValueError, IndexError, AttributeError):
        return {"status": "unknown", "reason": "process CPU sample unavailable"}


def safe_request(method: str, params: dict) -> dict:
    result = {"method": method}
    if method == "tools/call":
        result["tool"] = params.get("name")
        arguments = params.get("arguments") or {}
        for key in ("count", "duration", "button", "enabled"):
            value = arguments.get(key)
            if type(value) in (str, int, bool):
                result[key] = value
    if method == "resources/read" and params.get("uri") in {
        "pokered://game-state",
        "pokered://peer-game-state",
        "pokered://party-records",
        "pokered://peer-party-records",
        "pokered://events",
        "pokered://peer-events",
    }:
        result["uri"] = params["uri"]
    return result


def step_result(result) -> dict:
    """Keep actual returned tick/error evidence without ROM/state payloads."""
    if not isinstance(result, dict):
        return {"status": "unknown", "reason": "non-object RPC result"}
    output = {"is_error": result.get("isError") is True}
    allowed = {
        "ok",
        "count",
        "frames",
        "tick",
        "ticks",
        "primary_tick",
        "peer_tick",
        "cycles",
        "completed",
        "advanced_frames",
        "requested_frames",
        "actual_frames",
        "actual_cycles",
        "partial",
        "epoch",
        "error",
        "code",
        "operation",
        "remaining_frames",
        "frames_completed",
    }
    for content in result.get("content", []):
        if not isinstance(content, dict) or content.get("type") != "text":
            continue
        try:
            value = json.loads(content.get("text", ""))
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            output["returned_fields"] = {
                key: item
                for key, item in value.items()
                if key in allowed
                and (
                    item is None
                    or type(item) in (int, bool)
                    or (type(item) is float and math.isfinite(item))
                    or (key == "code" and type(item) is str and re.fullmatch(r"[a-z_]{1,64}", item))
                )
            }
    output["returned_fields_status"] = "measured" if output.get("returned_fields") else "unknown"
    output["result_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return output


class CallRecorder:
    """Observe real RPCs; propagate return values, failures and cancellation unchanged."""

    def __init__(self, stream, owner: str, pid: int, *, max_calls: int = 100_000):
        self.stream = stream
        self.owner = owner
        self.pid = pid
        self.max_calls = max_calls
        self.sequence = 0

    async def request(self, original, method, params):
        if self.sequence >= self.max_calls:
            raise ValueError("profile call bound exhausted")
        self.sequence += 1
        row = {
            "sequence": self.sequence,
            "owner": self.owner,
            "pid": self.pid,
            "cpu_scope": "pair-process" if self.owner == "local-pair" else "owner-process",
            "cpu_accounting_key": self.pid,
            "controller_cpu_scope": "controller process only; excludes server",
            "request": safe_request(method, params),
            "wall_start_ns": time.perf_counter_ns(),
            "controller_cpu_start_ns": time.process_time_ns(),
            "owner_cpu_start": owner_cpu(self.pid),
        }
        failure = None
        result = None
        try:
            result = await original(method, params)
            return result
        except BaseException as exc:
            failure = exc
            raise
        finally:
            row.update(
                {
                    "wall_end_ns": time.perf_counter_ns(),
                    "controller_cpu_end_ns": time.process_time_ns(),
                    "owner_cpu_end": owner_cpu(self.pid),
                    "completion": "returned" if failure is None else "raised",
                }
            )
            start_cpu, end_cpu = row["owner_cpu_start"], row["owner_cpu_end"]
            row["cpu_sample_status"] = (
                "measured"
                if (
                    start_cpu.get("status") == end_cpu.get("status") == "measured"
                    and type(start_cpu.get("process_start_ticks")) is int
                    and start_cpu["process_start_ticks"] > 0
                    and start_cpu["process_start_ticks"] == end_cpu.get("process_start_ticks")
                )
                else "unknown"
            )
            if failure is not None:
                row["error_type"] = type(failure).__name__
            if method == "tools/call" and params.get("name") in ("step", "link_step"):
                row["step_result"] = step_result(result)
            try:
                self.stream.write(json.dumps(row) + "\n")
                self.stream.flush()
            except (OSError, ValueError) as evidence_error:
                if failure is None:
                    raise
                failure.add_note(f"profile evidence write failed: {type(evidence_error).__name__}")


def source_identity(repository: Path, expected_head: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise ValueError("profile requires an exact source commit")
    git = ["git", "--work-tree=" + str(repository.resolve())]
    actual = subprocess.check_output(git + ["rev-parse", "HEAD"], cwd=repository, text=True).strip()
    if actual != expected_head:
        raise ValueError("profile source commit mismatch")
    if subprocess.check_output(git + ["status", "--porcelain"], cwd=repository, text=True).strip():
        raise ValueError("profile requires clean committed source")
    return {"commit": actual}
