#!/usr/bin/env python3
"""Fail-closed prerequisites and pinball import proof for the native unit lane.

This is not the production gate or an allocation attestation. The unchanged
production_gate.py owns test selection, timeouts, and the final tier verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.machinery
import importlib.util
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
UV_TOOL_VERSION = "0.12.17"
UV_AUDIT_ENV = "NATIVE_UV_AUDIT_ACTIVE"
UV_STATE_LABELS = ("before", "faulted", "after")
UV_AUDIT_HOOK = """\
import json
import os
import sys

_LOG = {log!r}


def _record(event, args):
    if event != "subprocess.Popen" or os.environ.get({env!r}) != "1":
        return
    try:
        argv = [str(item) for item in (args[1] or [])]
        line = json.dumps({{"pid": os.getpid(), "executable": str(args[0]), "argv": argv}})
        descriptor = os.open(_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, (line + "\\n").encode())
        finally:
            os.close(descriptor)
    except Exception:
        pass


sys.addaudithook(_record)
"""
UV_FAULT_PTH = "import sys; sys.modules['ensurepip'] = None\nimport native_uv_audit\n"
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


def uv_instrument(output: Path) -> None:
    """Install the audit hook and the simulated ensurepip fault in the owned UV env only.

    The fault only simulates a Python whose ensurepip is unavailable; it is not a
    native platform absence. Nothing here alters the bootstrap under test.
    """
    output = output.resolve()
    purelib = Path(sysconfig.get_path("purelib")).resolve()
    if sys.prefix == sys.base_prefix or (output / "work") not in purelib.parents:
        raise ValueError("refusing to instrument anything but the owned ephemeral UV environment")
    log = output / "evidence" / "uv-audit.jsonl"
    hook = UV_AUDIT_HOOK.format(log=str(log), env=UV_AUDIT_ENV)
    with (purelib / "native_uv_audit.py").open("x", encoding="utf-8") as handle:
        handle.write(hook)
    with (purelib / "zz_native_uv_faults.pth").open("x", encoding="utf-8") as handle:
        handle.write(UV_FAULT_PTH)


def uv_state(output: Path, label: str) -> dict[str, Any]:
    """Record whether pip and ensurepip are reachable from the target interpreter."""
    state: dict[str, Any] = {
        "label": label,
        "target_python": os.path.abspath(sys.executable),
        "in_venv": sys.prefix != sys.base_prefix,
        "pip_importable": importlib.util.find_spec("pip") is not None,
    }
    for name, module in (("pip_version", "pip"), ("ensurepip_version", "ensurepip")):
        result = subprocess.run(
            [sys.executable, "-m", module, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        state[name] = {"returncode": result.returncode, "stderr_tail": result.stderr[-300:]}
    _write_json(output / "evidence" / f"uv-state-{label}.json", state)
    return state


def validate_uv_states(states: dict[str, dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    for label in UV_STATE_LABELS:
        state = states.get(label)
        if not isinstance(state, dict):
            problems.append(f"missing {label} pip/ensurepip state")
            continue
        if not state.get("in_venv"):
            problems.append(f"{label}: target is not an isolated environment")
        if state.get("pip_importable") or state.get("pip_version", {}).get("returncode") == 0:
            problems.append(f"{label}: pip is present in the UV environment")
        if label != "before" and state.get("ensurepip_version", {}).get("returncode") == 0:
            problems.append(
                f"{label}: ensurepip is still available, so the uv fallback is unreachable"
            )
    if len({state.get("target_python") for state in states.values() if state}) > 1:
        problems.append("states describe different target interpreters")
    return problems


def validate_uv_audit(records: list[dict[str, Any]], *, uv: str, target: str) -> list[str]:
    """Reject audit evidence unless the real bootstrap selected uv for this target."""
    if not records:
        return ["audit log is empty"]
    problems: list[str] = []
    uv_real = os.path.realpath(uv)
    target = os.path.abspath(target)
    pip_probe = ensure_probe = first_install = None
    installs = checks = 0
    editable = unisolated = False
    for index, record in enumerate(records):
        argv = [str(item) for item in record.get("argv") or []]
        if not argv:
            continue
        program, rest = argv[0], argv[1:]
        if rest[:1] == ["-m"] and rest[1:2] == ["pip"] and rest[2:3] in (["install"], ["check"]):
            problems.append(f"record {index}: pip was executed by the interpreter")
        elif os.path.abspath(program) == target and rest == ["-m", "pip", "--version"]:
            pip_probe = index if pip_probe is None else pip_probe
        elif os.path.abspath(program) == target and rest[:2] == ["-m", "ensurepip"]:
            ensure_probe = index if ensure_probe is None else ensure_probe
        elif rest[:1] == ["pip"] and rest[1:2] in (["install"], ["check"]):
            if os.path.realpath(program) != uv_real:
                problems.append(f"record {index}: unexpected uv executable")
            option = rest.index("--python") if "--python" in rest else -1
            if option < 0 or option + 1 >= len(rest) or os.path.abspath(rest[option + 1]) != target:
                problems.append(f"record {index}: uv does not target the owned interpreter")
            if rest[1] == "install":
                installs += 1
                first_install = index if first_install is None else first_install
                editable = editable or "-e" in rest
                unisolated = unisolated or "--no-build-isolation" in rest
            else:
                checks += 1
    if pip_probe is None or ensure_probe is None:
        problems.append("bootstrap did not probe pip then ensurepip on the target interpreter")
    if first_install is None or installs < 3 or not editable or not unisolated:
        problems.append("bootstrap did not run the build, editable and native uv installs")
    if checks < 1:
        problems.append("bootstrap did not run uv pip check")
    if None not in (pip_probe, ensure_probe, first_install) and not (
        pip_probe < ensure_probe < first_install
    ):
        problems.append("probe order was not pip, ensurepip, then uv install")
    return problems


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def uv_verify(root: Path, output: Path, uv: str | None) -> None:
    """Combine state, audit and native proof into one verdict; BLOCKED/FAIL is never PASS."""
    evidence = output / "evidence"
    uv = uv or shutil.which("uv") or ""
    target = os.path.abspath(sys.executable)
    problems: list[str] = []
    states = {}
    for label in UV_STATE_LABELS:
        path = evidence / f"uv-state-{label}.json"
        states[label] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    problems += validate_uv_states(states)
    log = evidence / "uv-audit.jsonl"
    records = []
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    if not uv:
        problems.append("uv is not on PATH")
    else:
        problems += validate_uv_audit(records, uv=uv, target=target)
        version = subprocess.run(
            [uv, "--version"], capture_output=True, text=True, timeout=60, check=False
        )
        if version.stdout.split()[:2] != ["uv", UV_TOOL_VERSION]:
            problems.append(f"uv version is not {UV_TOOL_VERSION}: {version.stdout.strip()}")
        if Path(sys.prefix).resolve() in Path(uv).resolve().parents:
            problems.append("uv was installed inside the target environment")
        listing = subprocess.run(
            [uv, "pip", "list", "--python", target, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if listing.returncode:
            problems.append("uv pip list failed")
        elif "pip" in {item["name"].lower() for item in json.loads(listing.stdout)}:
            problems.append("pip is installed according to uv pip list")
        check = subprocess.run(
            [uv, "pip", "check", "--python", target],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if check.returncode:
            problems.append("final uv pip check failed")
    proof_path = evidence / "pinball-native.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8")) if proof_path.is_file() else {}
    if proof.get("status") != "PASS":
        problems.append("native pinball import proof did not PASS")
    inputs = {
        name: _sha256(root / name)
        for name in ("pyproject.toml", "uv.lock", "scripts/bootstrap_pyboy.py")
    }
    inputs["native-build.json"] = _sha256(evidence / "native-build.json")
    inputs["uv-audit.jsonl"] = _sha256(log)
    if not all(inputs.values()):
        problems.append("a required input/evidence hash is missing")
    _write_json(
        evidence / "uv-qualification.json",
        {
            "schema_version": 1,
            "scope": "pip-less-uv-native-bootstrap-qualification-not-full-unit-gate",
            "simulation": "ensurepip unavailability is a fault injected only in the owned UV env",
            "uv": uv,
            "target_python": target,
            "input_sha256": inputs,
            "audit_records": len(records),
            "status": "FAIL" if problems else "PASS",
            "problems": problems,
        },
    )
    if problems:
        raise ValueError("; ".join(problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=("prepare", "verify", "uv-instrument", "uv-state", "uv-verify")
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--label", choices=UV_STATE_LABELS)
    parser.add_argument("--uv")
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            prepare(ROOT, args.output)
        elif args.operation == "uv-instrument":
            uv_instrument(args.output)
        elif args.operation == "uv-state":
            uv_state(args.output.resolve(), args.label or "before")
        elif args.operation == "uv-verify":
            uv_verify(ROOT, args.output.resolve(), args.uv)
        else:
            verify(ROOT, args.output.resolve())
    except (OSError, ValueError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        print(f"native unit prerequisite/proof failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
