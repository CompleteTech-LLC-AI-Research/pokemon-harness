#!/usr/bin/env python3
"""Fail-closed prerequisites and pinball import proof for the native unit lane.

This is not the production gate or an allocation attestation. The unchanged
production_gate.py owns test selection, timeouts, and the final tier verdict.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.machinery
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import sysconfig
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PINBALL_MODULES = (
    "pyboy.plugins.game_wrapper_pokemon_pinball",
    "pyboy.plugins.game_wrapper_pokemon_pinball_data",
)
UNSAFE_ENVIRONMENT = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONUSERBASE",
    "PYTHONOPTIMIZE",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "PYBOY_NO_CYTHON",
    "POKERED_SKIP_SHA1",
    "PIP_PREFIX",
    "PIP_TARGET",
    "PIP_USER",
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True, timeout=15
    ).strip()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    # All destinations belong to the fresh output directory, never the checkout.
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def host_facts() -> tuple[dict[str, Any], list[str]]:
    """Record prerequisites; affinity and low load are NOT a CPU reservation."""
    problems: list[str] = []
    facts: dict[str, Any] = {
        "python": sys.version,
        "uid": os.geteuid() if hasattr(os, "geteuid") else None,
        "platform": sys.platform,
        "compiler": shutil.which("cc") or shutil.which("gcc"),
        "python_headers": str(Path(sysconfig.get_path("include")) / "Python.h"),
        "cpu_count": os.cpu_count(),
        "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "cpu_pressure": None,
        "allocation_attested": False,
    }
    if sys.platform != "linux":
        problems.append("this native CI recipe requires Linux")
    if facts["uid"] in (None, 0):
        problems.append("run as an ordinary non-root user, not namespace root")
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        problems.append("use the supported Python 3.11 or 3.12 interpreter")
    if not facts["compiler"]:
        problems.append("a C compiler is required")
    if not Path(facts["python_headers"]).is_file():
        problems.append("the selected Python interpreter has no Python.h")
    try:
        facts["cpu_pressure"] = Path("/proc/pressure/cpu").read_text(encoding="ascii")
    except OSError:
        # Unknown remains unknown; do not report an absent PSI interface as quiet.
        pass
    try:
        semaphore = multiprocessing.get_context("spawn").Semaphore(1)
        acquired = semaphore.acquire(timeout=1)
        if not acquired:
            raise OSError("could not acquire the shared-memory semaphore")
        semaphore.release()
        facts["shared_memory_semaphore"] = "usable"
    except (OSError, ValueError) as exc:
        facts["shared_memory_semaphore"] = "unusable"
        problems.append(f"writable shared memory is required: {type(exc).__name__}: {exc}")
    for name in UNSAFE_ENVIRONMENT:
        if os.environ.get(name):
            # Never include values: path overrides and pytest settings may contain secrets.
            problems.append(f"unset {name} before running the native unit lane")
    return facts, problems


def prepare(root: Path, output: Path) -> None:
    """Create a new external evidence directory, then record prerequisite failures."""
    root = root.resolve()
    output = output.resolve()
    if output == root or root in output.parents:
        raise ValueError("native evidence and environments must be outside the checkout")
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("run against the repository root")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("use a clean, committed checkout; preserve unrelated work")
    head = _git(root, "rev-parse", "HEAD")
    output.mkdir(parents=True, exist_ok=False)
    (output / "evidence").mkdir()
    (output / "work").mkdir()
    facts, problems = host_facts()
    _write_json(
        output / "evidence" / "preflight.json",
        {
            "schema_version": 1,
            "scope": "native-unit-prerequisites-not-release-qualification",
            "created_at": datetime.now(UTC).isoformat(),
            "harness_head": head,
            "host": facts,
            "status": "BLOCKED" if problems else "READY",
            "problems": problems,
        },
    )
    if problems:
        raise ValueError("; ".join(problems))


def inspect_pinball(root: Path) -> dict[str, Any]:
    """Prove that both split modules are native and retain their public identity."""
    vendor = root / "vendor" / "pyboy-src"
    expected = (vendor / "POKERED_HARNESS_PYBOY_REVISION").read_text(encoding="ascii").strip()
    pyboy = importlib.import_module("pyboy")
    utils = importlib.import_module("pyboy.utils")
    facade, data = [importlib.import_module(name) for name in PINBALL_MODULES]
    manager = importlib.import_module("pyboy.plugins.manager")
    problems: list[str] = []
    if len(expected) != 40 or any(char not in "0123456789abcdef" for char in expected):
        problems.append("checkout PyBoy revision pin is malformed")
    if getattr(pyboy, "__pokered_harness_revision__", None) != expected:
        problems.append("loaded PyBoy revision differs from this checkout's pin")
    if getattr(utils, "cython_compiled", None) is not True:
        problems.append("PyBoy utils do not report a compiled runtime")
    origins: dict[str, str] = {}
    lines: dict[str, int] = {}
    for name, module in zip(PINBALL_MODULES, (facade, data), strict=True):
        origin = str(getattr(module, "__file__", "") or "")
        origins[name] = origin
        if not origin.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
            problems.append(f"{name} is not an installed native extension")
        source = vendor / (name.replace(".", "/") + ".py")
        lines[name] = len(source.read_text(encoding="utf-8").splitlines())
        if lines[name] > 1000:
            problems.append(f"{name} exceeds 1000 lines")
    exports = getattr(data, "__all__", None)
    if not isinstance(exports, (list, tuple)) or not exports:
        problems.append("pinball data exports must be explicit and nonempty")
    elif any(not isinstance(name, str) for name in exports):
        problems.append("pinball data exports must be names")
    else:
        if len(set(exports)) != len(exports) or "Enum" in exports:
            problems.append(
                "pinball data exports duplicate names or expose Cython's Enum collision"
            )
        for name in exports:
            if not hasattr(data, name) or not hasattr(facade, name):
                problems.append(f"missing public pinball export: {name}")
            elif getattr(facade, name) is not getattr(data, name):
                problems.append(f"pinball export identity changed: {name}")
    wrapper = getattr(facade, "GameWrapperPokemonPinball", None)
    manager_type = getattr(manager, "PluginManager", None)
    manager_origin = str(getattr(manager, "__file__", "") or "")
    if not manager_origin.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
        problems.append("plugin manager is not an installed native extension")
    if not isinstance(wrapper, type) or not isinstance(manager_type, type):
        problems.append("public Pinball wrapper or plugin manager class is missing")
    else:
        # Cython may keep the imported wrapper class private to the manager
        # extension. Its typed instance slot is the actual integration path.
        try:
            manager_probe = manager_type.__new__(manager_type)
            wrapper_probe = wrapper.__new__(wrapper)
            manager_probe.game_wrapper_pokemon_pinball = wrapper_probe
            if manager_probe.game_wrapper_pokemon_pinball is not wrapper_probe:
                problems.append("plugin manager Pinball slot changed wrapper identity")
        except (AttributeError, TypeError, ValueError) as exc:
            problems.append(
                f"plugin manager Pinball slot rejected the wrapper: {type(exc).__name__}"
            )
    return {
        "schema_version": 1,
        "scope": "native-pinball-import-proof-not-gameplay",
        "status": "FAIL" if problems else "PASS",
        "expected_revision": expected,
        "loaded_revision": getattr(pyboy, "__pokered_harness_revision__", None),
        "module_origins": origins,
        "source_line_counts": lines,
        "problems": problems,
    }


def verify(root: Path, output: Path) -> None:
    preflight = json.loads((output / "evidence" / "preflight.json").read_text(encoding="utf-8"))
    if preflight.get("status") != "READY":
        raise ValueError("prerequisites were not READY")
    if _git(root, "rev-parse", "HEAD") != preflight["harness_head"]:
        raise ValueError("checkout HEAD changed after prerequisites")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("checkout changed during native build")
    proof = inspect_pinball(root)
    proof["harness_head"] = preflight["harness_head"]
    _write_json(output / "evidence" / "pinball-native.json", proof)
    if proof["status"] != "PASS":
        raise ValueError("; ".join(proof["problems"]))


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {description}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"{description} root is not an object")
    return value


def _require_gate_success(report: dict[str, Any]) -> dict[str, Any]:
    """Require the exact successful native unit plus five-repeat timing scope."""
    if report.get("overall") != "PASS":
        raise ValueError("production gate report overall status is not PASS")
    runtime = report.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("pyboy_mode") != "cython":
        raise ValueError("production gate report does not prove the Cython runtime")
    if report.get("gate_problems") != [] or report.get("cancellation") != "":
        raise ValueError("production gate report contains gate problems or cancellation")

    plan = report.get("execution_plan")
    expected_steps = [
        {"mode": "cython", "tier": "unit", "role": "selected-tier"},
        {"mode": "cython", "tier": "timing", "role": "selected-tier"},
    ]
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != 1
        or plan.get("scope") != "selected-tiers"
        or plan.get("runtime_modes") != ["cython"]
        or plan.get("selected_tiers") != ["unit", "timing"]
        or plan.get("early_smoke") is not False
        or plan.get("smoke_role") != "not-selected"
        or plan.get("steps") != expected_steps
    ):
        raise ValueError("production gate report does not contain the requested native scope")

    collections = report.get("collections")
    if (
        not isinstance(collections, list)
        or not collections
        or any(not isinstance(item, dict) or item.get("status") != "PASS" for item in collections)
    ):
        raise ValueError("production gate collection preflight is incomplete or not PASS")

    tiers = report.get("tiers")
    if (
        not isinstance(tiers, list)
        or len(tiers) != 2
        or any(not isinstance(item, dict) for item in tiers)
        or [item.get("name") for item in tiers] != ["unit", "timing"]
    ):
        raise ValueError("production gate report does not contain exactly unit and timing tiers")
    repeat_counts = {"unit": 1, "timing": 5}
    retained_counts: dict[str, dict[str, int]] = {}
    for tier in tiers:
        if (
            tier.get("required") is not True
            or tier.get("status") != "PASS"
            or tier.get("reason") != ""
            or tier.get("iteration_failures") != []
        ):
            raise ValueError(
                f"production gate tier {tier.get('name')!r} is not a clean required PASS"
            )
        name = tier["name"]
        returncodes = tier.get("returncodes")
        if (
            not isinstance(returncodes, list)
            or len(returncodes) != repeat_counts[name]
            or any(type(code) is not int or code != 0 for code in returncodes)
        ):
            raise ValueError(
                f"production gate tier {name!r} does not retain its expected successful runs"
            )
        counts = tier.get("counts")
        required_count_fields = (
            "total",
            "passed",
            "failed",
            "skipped",
            "xfailed",
            "xpassed",
            "errors",
        )
        if not isinstance(counts, dict):
            raise TypeError(f"production gate tier {name!r} has no counts object")
        normalized: dict[str, int] = {}
        for field in required_count_fields:
            value = counts.get(field)
            if type(value) is not int or value < 0:
                raise ValueError(f"production gate tier {name!r} has invalid {field} count")
            normalized[field] = value
        if (
            normalized["total"] <= 0
            or normalized["passed"] != normalized["total"]
            or any(normalized[field] for field in required_count_fields[2:])
        ):
            raise ValueError(f"production gate tier {name!r} contains incomplete test outcomes")
        retained_counts[name] = normalized
    return {
        "overall": "PASS",
        "runtime_mode": "cython",
        "selected_tiers": ["unit", "timing"],
        "tier_counts": retained_counts,
        "timing_repetitions": 5,
    }


def finalize(root: Path, output: Path) -> None:
    """Retain a terminal receipt only after the complete native gate is proven."""
    root = root.resolve()
    output = output.resolve()
    if output == root or root in output.parents:
        raise ValueError("native evidence and environments must be outside the checkout")
    evidence = output / "evidence"
    receipt_path = evidence / "native-terminal.json"
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "scope": "native-unit-terminal-receipt-not-release-qualification",
        "status": "FAIL",
        "harness_head": None,
        "gate": None,
        "problems": [],
    }
    try:
        preflight = _read_json_object(evidence / "preflight.json", "native preflight")
        head = _git(root, "rev-parse", "HEAD")
        if preflight.get("status") != "READY":
            raise ValueError("native prerequisites were not READY")
        if preflight.get("harness_head") != head:
            raise ValueError("checkout HEAD differs from the native preflight")
        receipt["harness_head"] = head
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("checkout is dirty at native finalization")

        build = _read_json_object(evidence / "native-build.json", "native build evidence")
        if build.get("mode") != "cython" or build.get("status") != "complete":
            raise ValueError("native build evidence is not a complete Cython build")
        proof = _read_json_object(evidence / "pinball-native.json", "native pinball proof")
        if proof.get("status") != "PASS" or proof.get("harness_head") != head:
            raise ValueError("native pinball proof is missing, failed, or tied to another HEAD")

        # Import only for finalization, after the environment and checkout are ready.
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from scripts import production_gate_evidence

        gate_dir = evidence / "gate"
        production_gate_evidence.verify_evidence_bundle(gate_dir)
        report = _read_json_object(gate_dir / "gate-report.json", "production gate report")
        if report.get("schema_version") != production_gate_evidence.EVIDENCE_SCHEMA_VERSION:
            raise ValueError("production gate report schema version is unsupported")
        receipt["gate"] = _require_gate_success(report)
        receipt["harness_head"] = head
        receipt["status"] = "PASS"
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        subprocess.SubprocessError,
    ) as exc:
        receipt["problems"] = [f"{type(exc).__name__}: {exc}"]
    evidence.mkdir(parents=True, exist_ok=True)
    _write_json(receipt_path, receipt)
    if receipt["status"] != "PASS":
        raise ValueError(receipt["problems"][0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "verify", "terminal-check"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            prepare(ROOT, args.output)
        elif args.operation == "verify":
            verify(ROOT, args.output.resolve())
        else:
            finalize(ROOT, args.output)
    except (OSError, ValueError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        print(f"native unit prerequisite/proof/finalization failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
