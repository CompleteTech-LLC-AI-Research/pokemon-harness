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
    if (
        not isinstance(wrapper, type)
        or getattr(manager, "GameWrapperPokemonPinball", None) is not wrapper
    ):
        problems.append("plugin manager no longer exposes the public pinball wrapper class")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "verify"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            prepare(ROOT, args.output)
        else:
            verify(ROOT, args.output.resolve())
    except (OSError, ValueError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        print(f"native unit prerequisite/proof failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
