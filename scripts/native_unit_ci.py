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
import re
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
        line = json.dumps(
            {{"pid": os.getpid(), "ppid": os.getppid(), "executable": str(args[0]), "argv": argv}}
        )
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
        "schema_version": 1,
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


def _hex64(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _code(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fingerprint(identity: dict[str, Any]) -> str:
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_uv_states(states: dict[str, Any], *, target: str) -> list[str]:
    """Fail closed on missing, foreign or malformed pip/ensurepip state records."""
    problems: list[str] = []
    target = os.path.abspath(target)
    for label in UV_STATE_LABELS:
        state = states.get(label)
        if not isinstance(state, dict) or not state:
            problems.append(f"missing {label} pip/ensurepip state")
            continue
        if state.get("label") != label:
            problems.append(f"{label}: record carries a foreign phase label")
        if not isinstance(state.get("target_python"), str) or (
            os.path.abspath(state["target_python"]) != target
        ):
            problems.append(f"{label}: state describes a different target interpreter")
        if state.get("in_venv") is not True:
            problems.append(f"{label}: target is not an isolated environment")
        if state.get("pip_importable") is not False:
            problems.append(f"{label}: pip is present or unrecorded in the UV environment")
        codes = {}
        for key in ("pip_version", "ensurepip_version"):
            record = state.get(key)
            code = record.get("returncode") if isinstance(record, dict) else None
            if not _code(code):
                problems.append(f"{label}: {key} has no recorded integer return code")
            codes[key] = code
        if _code(codes["pip_version"]) and codes["pip_version"] == 0:
            problems.append(f"{label}: pip is present in the UV environment")
        if (
            label != "before"
            and _code(codes["ensurepip_version"])
            and (codes["ensurepip_version"] == 0)
        ):
            problems.append(
                f"{label}: ensurepip is still available, so the uv fallback is unreachable"
            )
    return problems


def validate_uv_audit(
    records: list[Any], *, uv: str, target: str, expected: dict[str, Any]
) -> list[str]:
    """Accept only the unchanged bootstrap's exact probe, install, check, probe sequence."""
    if not isinstance(records, list) or not records:
        return ["audit log is empty"]
    if not uv:
        return ["the pinned uv path is unknown"]
    problems: list[str] = []
    uv_real = os.path.realpath(uv)
    target = os.path.abspath(target)
    root = os.path.realpath(expected["root"])
    base = ["pip", "install", "--python", target, "--force-reinstall", "--no-deps"]
    rows: list[tuple[int, int, list[str]]] = []
    parents: dict[int, int] = {}
    for index, record in enumerate(records):
        argv = record.get("argv") if isinstance(record, dict) else None
        if not (
            isinstance(argv, list)
            and argv
            and all(isinstance(item, str) for item in argv)
            and _code(record.get("pid"))
            and isinstance(record.get("executable"), str)
            and _code(record.get("ppid"))
        ):
            problems.append(f"record {index}: malformed audit record")
        elif record["executable"] != argv[0]:
            problems.append(f"record {index}: executable does not match argv[0]")
        else:
            rows.append((index, record["pid"], argv))
            parents[index] = record["ppid"]

    def role(argv: list[str]) -> str | None:
        program, rest = argv[0], argv[1:]
        if rest[:2] == ["-m", "pip"] and rest != ["-m", "pip", "--version"]:
            return "pip-executed"
        if os.path.basename(program) == "uv":
            if os.path.realpath(program) != uv_real:
                return "foreign-uv"
            if len(rest) > 3 and os.path.abspath(rest[3]) == target:
                rest = [*rest[:3], target, *rest[4:]]
            if rest == ["pip", "check", "--python", target]:
                return "uv pip check"
            if rest == [*base, *expected["build_requirements"]]:
                return "install-build"
            if rest == [*base, "--no-build-isolation", "-e", rest[-1]] and (
                os.path.realpath(rest[-1]) == root
            ):
                return "install-editable"
            staged = Path(rest[-1]) if rest else Path()
            if (
                rest[:-1] == [*base, "--no-build-isolation", expected["cython_requirement"]]
                and staged.name == "pyboy-src"
                and staged.parent.name.startswith("pyboy-native-")
                and os.path.realpath(staged.parent.parent) == os.path.join(root, "build")
            ):
                return "install-native"
            return "unrecognized-uv"
        if os.path.abspath(program) != target:
            return None
        if rest == ["-m", "pip", "--version"]:
            return "pip-probe"
        if rest == ["-m", "ensurepip", "--upgrade"]:
            return "ensurepip"
        if (
            len(rest) == 6
            and rest[0] == expected["bootstrap_script"]
            and rest[1:3] == ["--mode", "cython"]
            and rest[3] == "--check-timeout"
            and rest[5] == "--_runtime-probe"
        ):
            return "runtime-probe"
        return None

    tagged = [(index, pid, role(argv)) for index, pid, argv in rows]
    argvs = {index: argv for index, _, argv in rows}
    messages = {
        "pip-executed": "pip was executed through python -m pip",
        "foreign-uv": "unexpected uv binary invoked",
        "unrecognized-uv": "unexpected uv command (not a bootstrap install or check role)",
    }
    for index, _, name in tagged:
        if name in messages:
            problems.append(f"record {index}: {messages[name]}")
    owner = next((pid for _, pid, name in tagged if name == "ensurepip"), None)
    if owner is None:
        return [*problems, "bootstrap did not probe pip then ensurepip on the target interpreter"]
    mine = [name for _, pid, name in tagged if pid == owner and name]
    wanted = ["pip-probe", "ensurepip", "install-build", "install-editable"]
    wanted += ["install-native", "uv pip check", "runtime-probe"]
    if mine != wanted:
        problems.append(f"probe order/role sequence differs: got {mine}, required {wanted}")

    def first(name: str, pid: int | None = None) -> int:
        found = (i for i, p, n in tagged if n == name and (pid is None or p == pid))
        return next(found, -1)

    build_probe = first("runtime-probe", owner)
    last = max(
        (i for i, pid, name in tagged if pid == owner and name == "runtime-probe"), default=-1
    )
    foreign = [(i, pid) for i, pid, name in tagged if pid != owner and name == "runtime-probe"]
    if len(foreign) != 1 or foreign[0][0] < last:
        problems.append("bootstrap --check did not follow with exactly one runtime probe")
    check_probe, checker = foreign[-1] if foreign else (-1, None)
    ppids: dict[int, set[int]] = {}
    for index, pid, _ in tagged:
        ppids.setdefault(pid, set()).add(parents[index])
    shell = ppids.get(owner, set())
    if len(shell) != 1 or ppids.get(checker) != shell or shell & set(ppids):
        problems.append("bootstrap build and --check processes lack one shared shell parent")
    if any(pid != owner and name not in (None, "runtime-probe") for _, pid, name in tagged):
        problems.append("a bootstrap role was executed by a different process")
    window = (first("install-build", owner), first("uv pip check", owner))
    probes = {owner: build_probe, checker: check_probe}
    children: dict[int, int] = {}
    for index, pid, name in tagged:
        if name is not None:
            continue
        parent = parents.get(index)
        discovery = _sdl_discovery_call(argvs[index])
        if pid == owner:
            if not (discovery and 0 <= build_probe < index < check_probe):
                problems.append(f"bootstrap process made unexpected subprocess records [{index}]")
        elif pid == checker:
            problems.append(
                f"bootstrap --check process made unexpected subprocess records [{index}]"
            )
        elif parent in probes and parent is not None:
            ends = check_probe if parent == owner else len(tagged)
            if not (discovery and 0 <= probes[parent] < index < ends):
                problems.append(f"record {index}: unexpected subprocess from a runtime probe")
            elif children.setdefault(parent, pid) != pid:
                problems.append(f"record {index}: runtime probe calls came from a second process")
        elif not (0 <= window[0] < index < window[1]):
            problems.append(f"record {index}: unexpected subprocess outside the uv install window")
    return problems


_SYSTEM_TOOL_DIRS = ("/sbin", "/usr/sbin", "/bin", "/usr/bin", "/usr/local/bin")
_SDL_LIBRARY_FILE = r"SDL2(?:_(?:image|ttf))?(?:-2\.0)?d?"
_SDL_LIBRARY = r"SDL2(?:_(?:image|ttf))?(?:-2\.0(?:\.0)?)?d?"


def _sdl_discovery_call(argv: list[str]) -> bool:
    """Classify a subprocess as ctypes.util.find_library probing for pysdl2's libraries.

    pysdl2 (imported through pyboy's window plugin) searches the system for each SDL2
    library name even when pysdl2-dll is bundled; on Linux that runs ldconfig, then the
    gcc and ld linker traces, then objdump for a found file. Only these exact argv
    shapes for SDL2/SDL2_image/SDL2_ttf names count; the caller additionally requires
    the importing process and phase.
    """
    program, rest = argv[0], argv[1:]
    link = f"-l{_SDL_LIBRARY}"
    name = Path(program).name
    if str(Path(program).parent) not in _SYSTEM_TOOL_DIRS and program != "ld":
        return False
    if name == "ldconfig":
        return rest == ["-p"]
    if name in ("gcc", "cc"):
        return (
            len(rest) == 4
            and rest[:2] == ["-Wl,-t", "-o"]
            and os.path.isabs(rest[2])
            and re.fullmatch(link, rest[3]) is not None
        )
    if program == "ld":
        flags = rest[1:-3]
        return (
            len(rest) >= 4
            and rest[0] == "-t"
            and len(flags) % 2 == 0
            and all(item == "-L" if i % 2 == 0 else bool(item) for i, item in enumerate(flags))
            and rest[-3:-1] == ["-o", os.devnull]
            and re.fullmatch(link, rest[-1]) is not None
        )
    if name == "objdump":
        library = Path(rest[3]).name if len(rest) == 4 else ""
        return (
            len(rest) == 4
            and rest[:3] == ["-p", "-j", ".dynamic"]
            and os.path.isabs(rest[3])
            and re.fullmatch(f"lib{_SDL_LIBRARY_FILE}\\.so(?:\\.\\d+)*", library) is not None
        )
    return False


def validate_native_build(
    document: Any, expected: dict[str, Any], live_identity: dict[str, Any] | None
) -> list[str]:
    """Strictly validate bootstrap_pyboy's own evidence record; missing or odd fails."""
    if not isinstance(document, dict) or not document:
        return ["native build evidence is missing, empty or not a JSON object"]
    problems: list[str] = []

    def need(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    need(document.get("evidence_version") == expected["evidence_version"], "build evidence version")
    need(document.get("procedure") == "bootstrap_pyboy --mode cython", "build procedure")
    need(document.get("mode") == "cython", "build mode is not cython")
    need(document.get("status") == "complete", "build status is not complete")
    inputs = document.get("build_inputs_sha256")
    need(_hex64(inputs) and inputs == expected["build_inputs_sha256"], "build inputs digest")
    interpreter = document.get("interpreter")
    need(
        isinstance(interpreter, dict)
        and interpreter.get("python_version") == expected["python_version"]
        and interpreter.get("prefix_name") == expected["prefix_name"],
        "build interpreter does not match the target environment",
    )
    producer = document.get("producer")
    need(
        isinstance(producer, dict)
        and producer.get("script") == "scripts/bootstrap_pyboy.py"
        and producer.get("script_sha256") == expected["script_sha256"],
        "build producer is not the checked-in bootstrap",
    )
    stamp = document.get("completed_at")
    need(
        isinstance(stamp, str)
        and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp) is not None,
        "build completion time is malformed",
    )
    identity = document.get("runtime_identity")
    if not isinstance(identity, dict) or not identity:
        problems.append("build runtime identity is missing")
        return problems
    need(document.get("installed_fingerprint") == _fingerprint(identity), "build fingerprint")
    need(identity.get("python") == expected["python_version"], "identity python version")
    need(identity.get("version") == expected["version"], "identity PyBoy version")
    need(identity.get("revision") == expected["revision"], "identity PyBoy revision pin")
    need(identity.get("cython_compiled") is True, "identity is not a compiled runtime")
    artifacts = identity.get("artifacts")
    modules = identity.get("modules")
    if not (
        isinstance(artifacts, dict) and artifacts and all(_hex64(v) for v in artifacts.values())
    ):
        problems.append("identity artifact digests are missing or malformed")
        artifacts = {}
    if not isinstance(modules, dict) or set(modules) != set(expected["runtime_modules"]):
        problems.append("identity module set differs from the runtime modules")
        modules = {}
    # Any suffix the bootstrap's own _module_kind accepts on a supported platform:
    # untagged, CPython-tagged (Linux/macOS), abi3, or Windows cpNNN-<platform>.
    extension = re.compile(r"\w+(?:\.(?:cpython-[\w-]+|cp\d+[\w-]*|abi3))?\.(?:so|pyd)")
    for name, item in modules.items():
        required = name in expected["cython_modules"]
        kind = item.get("kind") if isinstance(item, dict) else None
        artifact = item.get("artifact") if isinstance(item, dict) else None
        ok = isinstance(item, dict) and _hex64(item.get("sha256"))
        if required:
            ok = ok and kind == "cython"
        else:
            ok = ok and kind in ("source", "cython")
        if ok and isinstance(artifact, str):
            native = extension.fullmatch(artifact.replace("\\", "/").rsplit("/", 1)[-1]) is not None
            ok = native if kind == "cython" else artifact.endswith(".py")
        else:
            ok = False
        if ok and artifacts.get(artifact) != item["sha256"]:
            ok = False
        policy = "cython" if required else "source or cython"
        need(ok, f"identity module {name} is not a bound {policy} artifact of consistent kind")
    if live_identity is not None:
        need(identity == live_identity, "build identity differs from the installed runtime")
    return problems


def validate_pinball_proof(
    proof: Any,
    expected: dict[str, Any],
    origin_hashes: dict[str, str | None],
    identity: Any,
) -> list[str]:
    """Require the complete native Pinball proof bound to this head, target and build."""
    if not isinstance(proof, dict) or not proof:
        return ["pinball proof is missing, empty or not a JSON object"]
    problems: list[str] = []
    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    for key, value in (
        ("schema_version", 1),
        ("scope", "native-pinball-import-proof-not-gameplay"),
        ("status", "PASS"),
        ("problems", []),
        ("expected_revision", expected["revision"]),
        ("loaded_revision", expected["revision"]),
        ("harness_head", expected["head"]),
    ):
        if proof.get(key) != value or type(proof.get(key)) is not type(value):
            problems.append(f"pinball proof {key} is not {value!r}")
    origins = proof.get("module_origins")
    lines = proof.get("source_line_counts")
    if not isinstance(origins, dict) or set(origins) != set(PINBALL_MODULES):
        return [*problems, "pinball proof module origins are incomplete"]
    if not isinstance(lines, dict) or set(lines) != set(PINBALL_MODULES):
        problems.append("pinball proof line counts are incomplete")
    elif not all(_code(v) and 0 < v <= 1000 for v in lines.values()):
        problems.append("pinball proof line counts are not bounded integers")
    artifacts = identity.get("artifacts") if isinstance(identity, dict) else None
    prefix = os.path.realpath(expected["prefix"]) + os.sep
    for name in PINBALL_MODULES:
        origin = origins[name]
        base = os.path.basename(origin) if isinstance(origin, str) else ""
        package = name.split(".")[1:-1]
        relative = "/".join([*package, base])
        if not (
            base.startswith(name.rsplit(".", 1)[-1] + ".")
            and base.endswith(suffixes)
            and os.path.realpath(origin).endswith(os.sep.join(["pyboy", *package, base]))
        ):
            problems.append(f"{name} origin is not its native extension in the pyboy package")
            continue
        if not os.path.realpath(origin).startswith(prefix):
            problems.append(f"{name} origin is outside the target environment")
        digest = origin_hashes.get(origin)
        if (
            not _hex64(digest)
            or not isinstance(artifacts, dict)
            or artifacts.get(relative) != digest
        ):
            problems.append(f"{name} origin bytes are not the build identity artifact")
    return problems


def validate_uv_commands(records: Any, *, uv: str, target: str) -> list[str]:
    """Require retained raw version/list/check subprocess evidence with zero terminal codes."""
    target = os.path.abspath(target)
    argvs = {
        "version": [uv, "--version"],
        "list": [uv, "pip", "list", "--python", target, "--format", "json"],
        "check": [uv, "pip", "check", "--python", target],
    }
    by_name = (
        {r.get("name"): r for r in records if isinstance(r, dict)}
        if isinstance(records, list)
        else {}
    )
    problems: list[str] = []
    for name, argv in argvs.items():
        record = by_name.get(name)
        if record is None:
            problems.append(f"uv {name} subprocess evidence is missing")
            continue
        if record.get("argv") != argv:
            problems.append(f"uv {name}: command differs from the required argv")
        if not _code(record.get("returncode")) or record.get("timed_out") is not False:
            problems.append(f"uv {name}: no terminal return code (timed out or not run)")
        elif record["returncode"] != 0:
            problems.append(f"uv {name} failed with return code {record['returncode']}")
        for stream in ("stdout", "stderr"):
            text = record.get(stream)
            digest = record.get(f"{stream}_sha256")
            if not isinstance(text, str) or digest != hashlib.sha256(text.encode()).hexdigest():
                problems.append(f"uv {name}: raw {stream} is missing or unbound")
        if record.get("truncated") is not False:
            problems.append(f"uv {name}: raw output was truncated")
    version = by_name.get("version", {}).get("stdout", "")
    if isinstance(version, str) and version.split()[:2] != ["uv", UV_TOOL_VERSION]:
        problems.append(f"uv version is not {UV_TOOL_VERSION}: {version.strip()[:80]}")
    try:
        names = {
            re.sub(r"[-_.]+", "-", item["name"]).lower()
            for item in json.loads(by_name.get("list", {}).get("stdout", ""))
        }
    except (ValueError, KeyError, TypeError):
        names = None
        problems.append("uv pip list output is not a JSON package listing")
    if names is not None and "pip" in names:
        problems.append("pip is installed according to uv pip list")
    if names is not None and "pokered-harness" not in names:
        problems.append("uv pip list does not show the installed harness distribution")
    return problems


def native_source_digest(source_root: Path, revision: Path, suffixes: Any) -> str:
    """Recompute the bootstrap's staged-input digest from the vendored source."""
    digest = hashlib.sha256()
    for directory, children, filenames in os.walk(source_root):
        children[:] = sorted(
            n
            for n in children
            if not n.startswith(".")
            and n not in {"build", "dist", "__pycache__", "venv"}
            and not n.endswith(".egg-info")
        )
        for name in children:
            if (Path(directory) / name).is_symlink():
                raise ValueError("native build inputs must not contain symlinked directories")
        for name in sorted(filenames):
            source = Path(directory) / name
            if source.is_symlink():
                raise ValueError(f"symlinked native input {source}")
            if source.suffix not in suffixes and source != revision:
                continue
            digest.update(source.relative_to(source_root).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(source.read_bytes()).digest())
    return digest.hexdigest()


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _load_json(path: Path, problems: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"{path.name} is missing or not JSON: {type(exc).__name__}")
        return None


def _run_retained(name: str, argv: list[str], timeout: int) -> dict[str, Any]:
    """Run one verification command and keep bounded raw output and its terminal status."""
    limit = 4_000_000
    record: dict[str, Any] = {"name": name, "argv": argv, "timed_out": False}
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
        code, out, err = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        record["timed_out"] = True
        code, out, err = None, exc.stdout or b"", exc.stderr or b""
    record["returncode"] = code
    record["truncated"] = len(out) > limit or len(err) > limit
    for stream, data in (("stdout", out), ("stderr", err)):
        text = data[:limit].decode("utf-8", errors="replace")
        record[stream] = text
        record[f"{stream}_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return record


def uv_verify(root: Path, output: Path, uv: str | None) -> None:
    """Combine state, audit, build, pinball and command evidence; anything else is not PASS."""
    root = root.resolve()
    evidence = output / "evidence"
    uv = uv or shutil.which("uv") or ""
    target = os.path.abspath(sys.executable)
    problems: list[str] = []
    states = {
        label: _load_json(evidence / f"uv-state-{label}.json", problems)
        for label in UV_STATE_LABELS
    }
    problems += validate_uv_states(states, target=target)
    spec = importlib.util.spec_from_file_location(
        "_uv_qualification_bootstrap", root / "scripts" / "bootstrap_pyboy.py"
    )
    bootstrap = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(bootstrap)  # type: ignore[union-attr]
    preflight = _load_json(evidence / "preflight.json", problems)
    head = _git(root, "rev-parse", "HEAD")
    if not isinstance(preflight, dict) or preflight.get("harness_head") != head:
        problems.append("checkout HEAD differs from the preflight head")
    expected = {
        "root": str(root),
        "head": head,
        "prefix": sys.prefix,
        "prefix_name": Path(sys.prefix).name,
        "python_version": sys.version.split()[0],
        "evidence_version": bootstrap.BUILD_EVIDENCE_VERSION,
        "build_requirements": list(bootstrap.BUILD_REQUIREMENTS),
        "cython_requirement": bootstrap.CYTHON_REQUIREMENT,
        "bootstrap_script": str(root / "scripts" / "bootstrap_pyboy.py"),
        "script_sha256": _sha256(root / "scripts" / "bootstrap_pyboy.py"),
        "version": bootstrap.EXPECTED_PYBOY_VERSION,
        "revision": (root / "vendor" / "pyboy-src" / "POKERED_HARNESS_PYBOY_REVISION")
        .read_text(encoding="ascii")
        .strip(),
        "runtime_modules": list(bootstrap.RUNTIME_MODULES),
        "cython_modules": list(bootstrap.CYTHON_MODULES),
        "build_inputs_sha256": native_source_digest(
            root / "vendor" / "pyboy-src",
            root / "vendor" / "pyboy-src" / "POKERED_HARNESS_PYBOY_REVISION",
            bootstrap.NATIVE_INPUT_SUFFIXES,
        ),
    }
    if expected["revision"] != bootstrap.EXPECTED_REVISION:
        problems.append("checkout PyBoy pin differs from the bootstrap's expected revision")
    log = evidence / "uv-audit.jsonl"
    records: list[Any] = []
    try:
        for line in log.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    except (OSError, ValueError):
        problems.append("uv-audit.jsonl is missing or malformed")
    problems += validate_uv_audit(records, uv=uv, target=target, expected=expected)
    try:
        live = bootstrap._runtime_identity()
    except Exception as exc:  # noqa: BLE001 - an unreadable runtime is a failed proof
        live = None
        problems.append(f"installed runtime identity is unavailable: {type(exc).__name__}")
    build = _load_json(evidence / "native-build.json", problems)
    problems += validate_native_build(build, expected, live)
    if live is None:
        problems.append("native build cannot be bound to the installed runtime")
    proof = _load_json(evidence / "pinball-native.json", problems)
    origins = proof.get("module_origins") if isinstance(proof, dict) else None
    hashes = {}
    for origin in origins.values() if isinstance(origins, dict) else ():
        hashes[origin] = _sha256(Path(origin)) if isinstance(origin, str) else None
    identity = build.get("runtime_identity") if isinstance(build, dict) else None
    problems += validate_pinball_proof(proof, expected, hashes, identity)
    commands = [
        _run_retained("version", [uv, "--version"], 60),
        _run_retained("list", [uv, "pip", "list", "--python", target, "--format", "json"], 120),
        _run_retained("check", [uv, "pip", "check", "--python", target], 120),
    ]
    _write_json(evidence / "uv-verify-commands.json", {"schema_version": 1, "commands": commands})
    problems += validate_uv_commands(commands, uv=uv, target=target)
    if Path(sys.prefix).resolve() in Path(uv).resolve().parents:
        problems.append("uv was installed inside the target environment")
    bindings = {
        name: _sha256(evidence / name)
        for name in (
            "preflight.json",
            "uv-state-before.json",
            "uv-state-faulted.json",
            "uv-state-after.json",
            "uv-audit.jsonl",
            "native-build.json",
            "pinball-native.json",
            "uv-verify-commands.json",
        )
    }
    inputs = {
        name: _sha256(root / name)
        for name in ("pyproject.toml", "uv.lock", "scripts/bootstrap_pyboy.py")
    }
    if not all(bindings.values()) or not all(inputs.values()):
        problems.append("a required input/evidence hash is missing")
    _write_json(
        evidence / "uv-qualification.json",
        {
            "schema_version": 2,
            "scope": "pip-less-uv-native-bootstrap-qualification-not-full-unit-gate",
            "simulation": "ensurepip unavailability is a fault injected only in the owned UV env",
            "uv": uv,
            "uv_binary_sha256": _sha256(Path(os.path.realpath(uv))) if uv else None,
            "target_python": target,
            "target_python_sha256": _sha256(Path(os.path.realpath(target))),
            "head": head,
            "build_inputs_sha256": expected["build_inputs_sha256"],
            "installed_fingerprint": build.get("installed_fingerprint")
            if isinstance(build, dict)
            else None,
            "evidence_sha256": bindings,
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
