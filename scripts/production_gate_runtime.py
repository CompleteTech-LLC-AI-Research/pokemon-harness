"""Runtime probes and identity/policy problem detection.

Split from ``scripts/production_gate.py`` for issue #124 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on the loaded gate module stay visible.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_assets import (
    parse_expected_pyboy_revision,
    parse_expected_pyboy_version,
)
from scripts.production_gate_model import (
    GATE_CONTROLLED_ENVIRONMENT,
    PYBOY_RUNTIME_MODULES,
    RUNTIME_MODES,
    AssetRecord,
    Counts,
    GateReport,
    _asset_key,
    _normalize_nodeid,
)


def build_test_environment(
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
    expected_sha1: dict[Path, str],
    *,
    runtime_mode: str = "source",
) -> dict[str, str]:
    """Return the exact environment inherited by every pytest subprocess."""

    if runtime_mode not in RUNTIME_MODES:
        raise ValueError(f"unsupported runtime mode: {runtime_mode!r}")

    environment = {key: value for key, value in os.environ.items()}
    environment["POKERED_ROM_ROOT"] = str(rom_root)
    environment["POKERED_FIXTURE_ROOT"] = str(fixture_root)
    environment["PYTHONUNBUFFERED"] = "1"

    # A caller's pytest selection/plugin environment is not part of the
    # release contract.  In particular, inherited ``PYTEST_ADDOPTS`` can
    # silently deselect required tests, and a third-party plugin can alter
    # collection or xfail behavior.  The gate owns these settings.
    for key in GATE_CONTROLLED_ENVIRONMENT:
        environment.pop(key, None)
    # Remove the rest of pytest's parent-process state as well (for example
    # PYTEST_CURRENT_TEST and xdist variables), then opt into the controlled
    # plugin set above.  Leaving autoload enabled would let an installed
    # third-party plugin change collection, skips, or xfail semantics.
    for key in tuple(environment):
        if key.startswith("PYTEST_"):
            environment.pop(key, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for key in tuple(environment):
        if key.startswith("POKERED_PEER_") or key == "POKERED_ROM_VERSION":
            environment.pop(key, None)
    # Never pass the diagnostic hash bypass into a production-gate child.
    # ``main`` separately reports its presence as a policy failure so merely
    # stripping it cannot turn an unsafe invocation green.
    environment.pop("POKERED_SKIP_SHA1", None)

    versions_file = project_root / "VERSIONS.md"
    if versions_file.is_file():
        environment["POKERED_VERSIONS_PATH"] = str(versions_file)
    else:
        environment.pop("POKERED_VERSIONS_PATH", None)

    # Ensure the subprocess tests import this checkout, not an editable
    # install from a different worktree.  Source mode intentionally places
    # the vendored PyBoy first. Cython mode places only the harness source on
    # PYTHONPATH so the interpreter's installed extension modules are used.
    vendored_pyboy = (project_root / "vendor" / "pyboy-src").resolve(strict=False)
    source_entries = [str(project_root / "src"), str(project_root)]
    if runtime_mode == "source":
        source_entries.insert(0, str(vendored_pyboy))
    old_pythonpath = environment.get("PYTHONPATH")
    if old_pythonpath:
        for entry in old_pythonpath.split(os.pathsep):
            if not entry:
                continue
            entry_path = Path(entry).expanduser()
            if not entry_path.is_absolute():
                entry_path = project_root / entry_path
            if runtime_mode == "cython" and entry_path.resolve(strict=False) == vendored_pyboy:
                continue
            source_entries.append(entry)
    environment["PYTHONPATH"] = os.pathsep.join(source_entries)
    if runtime_mode == "source":
        environment["PYBOY_NO_CYTHON"] = "1"
    else:
        environment.pop("PYBOY_NO_CYTHON", None)

    # The stdio and golden-path tests are environment-driven.  Default them
    # to the pinned Red stock ROM when it is available, without overwriting a
    # caller's explicit selection.
    red_rom = rom_root / "red" / "pokemon-red.gb"
    red_sym = rom_root / "red" / "pokemon-red.sym"
    if "POKERED_ROM_PATH" not in environment and red_rom.is_file():
        environment["POKERED_ROM_PATH"] = str(red_rom)
    if "POKERED_SYM_PATH" not in environment and red_sym.is_file():
        environment["POKERED_SYM_PATH"] = str(red_sym)
    if "POKERED_ROM_SHA1" not in environment:
        selected_rom = environment.get("POKERED_ROM_PATH")
        if selected_rom:
            selected_path = Path(selected_rom).expanduser()
            if not selected_path.is_absolute():
                selected_path = project_root / selected_path
            try:
                selected_key = _asset_key(
                    selected_path.resolve(strict=False).relative_to(
                        rom_root.expanduser().resolve(strict=False)
                    )
                )
            except ValueError:
                selected_key = _asset_key(selected_path)
        else:
            selected_key = Path("red/pokemon-red.gb")
        selected_expected = expected_sha1.get(selected_key)
        if selected_expected:
            environment["POKERED_ROM_SHA1"] = selected_expected
    if "POKERED_SYM_SHA1" not in environment:
        selected_sym = environment.get("POKERED_SYM_PATH")
        if selected_sym:
            selected_path = Path(selected_sym).expanduser()
            if not selected_path.is_absolute():
                selected_path = project_root / selected_path
            try:
                selected_key = _asset_key(
                    selected_path.resolve(strict=False).relative_to(
                        rom_root.expanduser().resolve(strict=False)
                    )
                )
            except ValueError:
                selected_key = _asset_key(selected_path)
            selected_expected = expected_sha1.get(selected_key)
            if selected_expected:
                environment["POKERED_SYM_SHA1"] = selected_expected
    return environment


def probe_runtime(
    python_executable: Path,
    project_root: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    """Probe the interpreter that will run pytest, not the parent shell."""

    probe = r"""
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import platform
import sys
from pathlib import Path

PYBOY_RUNTIME_MODULES = (
    "pyboy",
    "pyboy.pyboy",
    "pyboy.utils",
    "pyboy.core.mb",
    "pyboy.core.serial",
)
CYTHON_RUNTIME_MODULES = tuple(
    name for name in PYBOY_RUNTIME_MODULES if name != "pyboy"
)

def spec_path(name):
    spec = importlib.util.find_spec(name)
    return str(spec.origin) if spec and spec.origin else None

def module_kind(path):
    if not path:
        return "missing"
    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    if any(path.endswith(suffix) for suffix in suffixes):
        return "cython/native-extension"
    if path.endswith(".py"):
        return "python-source"
    return "unknown"

result = {
    "python_executable": sys.executable,
    "python_prefix": sys.prefix,
    "python_version": platform.python_version(),
    "platform": platform.platform(),
    "pytest_version": None,
    "pyboy_version": None,
    "pyboy_revision": None,
    "pyboy_module": spec_path("pyboy"),
    "pyboy_kind": module_kind(spec_path("pyboy")),
    "serial_module": spec_path("pyboy.core.serial"),
    "pyboy_modules": {
        name: spec_path(name) for name in PYBOY_RUNTIME_MODULES
    },
    "pyboy_module_kinds": {},
    "pyboy_mode": "unknown",
    "harness_module": spec_path("pokered_harness"),
    "serial_core": None,
    "serial_contract": "unavailable",
}
result["pyboy_module_kinds"] = {
    name: module_kind(path) for name, path in result["pyboy_modules"].items()
}
module_kinds = result["pyboy_module_kinds"]
if all(module_kinds.get(name) == "cython/native-extension" for name in CYTHON_RUNTIME_MODULES):
    result["pyboy_mode"] = "cython"
elif all(module_kinds.get(name) == "python-source" for name in PYBOY_RUNTIME_MODULES):
    result["pyboy_mode"] = "source"
for package, key in (("pytest", "pytest_version"), ("pyboy", "pyboy_version")):
    try:
        result[key] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        pass
try:
    import pyboy

    result["pyboy_version"] = getattr(pyboy, "__version__", None)
    result["pyboy_revision"] = getattr(pyboy, "__pokered_harness_revision__", None)
except Exception as exc:
    result["pyboy_import_error"] = f"{type(exc).__name__}: {exc}"
try:
    import pokered_harness.link.serial_core as serial_core
    serial_cls = getattr(serial_core, "SerialCore", None)
    result["serial_core"] = repr(serial_cls)
    required = ("set_SB", "set_SC", "apply_external_edge", "tick")
    result["serial_contract"] = (
        "bit-accurate-backend"
        if serial_cls is not None and all(hasattr(serial_cls, name) for name in required)
        else "incompatible-stock-or-partial"
    )
except Exception as exc:
    result["serial_contract_error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(result, sort_keys=True))
"""
    try:
        completed = subprocess.run(
            [str(python_executable), "-c", probe],
            cwd=project_root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"probe_error": f"{type(exc).__name__}: {exc}"}
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or not lines:
        return {
            "probe_error": (
                f"runtime probe exit={completed.returncode}; "
                f"stdout={completed.stdout[-1000:]!r}; stderr={completed.stderr[-1000:]!r}"
            )
        }
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return {"probe_error": f"invalid runtime probe JSON: {exc}: {lines[-1]!r}"}


def runtime_problems(
    project_root: Path,
    runtime: dict[str, Any],
    *,
    expected_mode: str | None = None,
) -> list[str]:
    """Return runtime identity failures that must prevent a green gate."""

    problems: list[str] = []
    if expected_mode is not None and expected_mode not in RUNTIME_MODES:
        raise ValueError(f"unsupported runtime mode: {expected_mode!r}")
    if runtime.get("probe_error"):
        problems.append(f"runtime probe failed: {runtime['probe_error']}")
        return problems

    if expected_mode is not None and runtime.get("pyboy_mode") != expected_mode:
        problems.append(
            "selected interpreter runtime mode mismatch: "
            f"expected {expected_mode!r}, got {runtime.get('pyboy_mode')!r}"
        )

    versions_file = project_root / "VERSIONS.md"
    expected_version = parse_expected_pyboy_version(versions_file)
    expected_manifest_revision = parse_expected_pyboy_revision(versions_file)
    actual_version = runtime.get("pyboy_version")
    if not actual_version:
        problems.append("PyBoy version was not reported by the selected interpreter")
    elif expected_version and actual_version != expected_version:
        problems.append(
            f"PyBoy version mismatch: expected {expected_version!r}, got {actual_version!r}"
        )

    revision_file = project_root / "vendor" / "pyboy-src" / "POKERED_HARNESS_PYBOY_REVISION"
    if not revision_file.is_file():
        problems.append(f"pinned PyBoy revision marker is missing: {revision_file}")
    else:
        try:
            expected_revision = revision_file.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            problems.append(f"could not read PyBoy revision marker: {type(exc).__name__}: {exc}")
        else:
            if not expected_revision:
                problems.append("pinned PyBoy revision marker is empty")
            elif runtime.get("pyboy_revision") != expected_revision:
                problems.append(
                    "PyBoy revision mismatch: "
                    f"expected {expected_revision!r}, got {runtime.get('pyboy_revision')!r}"
                )
            if (
                expected_manifest_revision
                and runtime.get("pyboy_revision") != expected_manifest_revision
            ):
                problems.append(
                    "PyBoy revision does not match VERSIONS.md: "
                    f"expected {expected_manifest_revision!r}, "
                    f"got {runtime.get('pyboy_revision')!r}"
                )

    if runtime.get("serial_contract") != "bit-accurate-backend":
        problems.append(
            "selected interpreter does not expose the bit-accurate serial contract "
            f"({runtime.get('serial_contract')!r})"
        )
    if not runtime.get("pyboy_module"):
        problems.append("selected interpreter cannot resolve the PyBoy module")
    if not runtime.get("harness_module"):
        problems.append("selected interpreter cannot resolve the harness package")

    mode = expected_mode or runtime.get("pyboy_mode")
    if mode in RUNTIME_MODES:
        problems.extend(_runtime_identity_problems(project_root, runtime, mode))
    return problems


def _runtime_identity_problems(
    project_root: Path,
    runtime: dict[str, Any],
    mode: str,
) -> list[str]:
    """Reject imports that do not belong to this checkout and selected runtime."""

    problems: list[str] = []

    def resolved_path(value: Any, label: str) -> Path | None:
        if not isinstance(value, str) or not value:
            problems.append(f"{label} path was not reported by the selected interpreter")
            return None
        try:
            return Path(value).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            problems.append(f"{label} path could not be resolved: {type(exc).__name__}: {exc}")
            return None

    def within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    project_source = (project_root / "src").resolve(strict=False)
    vendored_pyboy = project_root / "vendor" / "pyboy-src"
    vendored_pyboy_resolved = vendored_pyboy.resolve(strict=False)
    if mode == "source" and vendored_pyboy.is_symlink():
        problems.append(f"selected vendored PyBoy root is a symlink: {vendored_pyboy}")
    if mode == "source" and not vendored_pyboy.is_dir():
        problems.append(f"selected vendored PyBoy root is missing: {vendored_pyboy}")
    if not project_source.is_dir():
        problems.append(f"selected harness source root is missing: {project_source}")

    harness_module = resolved_path(runtime.get("harness_module"), "harness module")
    if harness_module is not None and not within(harness_module, project_source):
        problems.append(
            "harness module resolves outside the selected project source: "
            f"{runtime.get('harness_module')}"
        )

    raw_modules = runtime.get("pyboy_modules")
    if not isinstance(raw_modules, dict):
        problems.append("selected interpreter did not report all PyBoy module paths")
        return problems

    allowed_pyboy_roots = [vendored_pyboy_resolved]
    if mode == "cython":
        python_prefix = resolved_path(runtime.get("python_prefix"), "Python prefix")
        if python_prefix is not None:
            allowed_pyboy_roots.append(python_prefix)

    resolved_modules: dict[str, Path] = {}
    for module_name in PYBOY_RUNTIME_MODULES:
        module_path = resolved_path(raw_modules.get(module_name), f"{module_name} module")
        if module_path is None:
            continue
        resolved_modules[module_name] = module_path
        if not any(within(module_path, root) for root in allowed_pyboy_roots):
            roots = ", ".join(str(root) for root in allowed_pyboy_roots)
            problems.append(
                f"{module_name} module resolves outside the selected runtime roots "
                f"({roots}): {raw_modules.get(module_name)}"
            )

    for field_name, module_name in (
        ("pyboy_module", "pyboy"),
        ("serial_module", "pyboy.core.serial"),
    ):
        field_path = resolved_path(runtime.get(field_name), field_name)
        module_path = resolved_modules.get(module_name)
        if field_path is not None and module_path is not None and field_path != module_path:
            problems.append(
                f"runtime {field_name} does not match the probed {module_name} module path"
            )
    return problems


def _resolve_child_path(value: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve(strict=False)


def _asset_record_for_path(records: Iterable[AssetRecord], path: Path) -> AssetRecord | None:
    resolved = path.resolve(strict=False)
    for record in records:
        if Path(record.path).resolve(strict=False) == resolved:
            return record
    return None


def environment_policy_problems(
    *,
    project_root: Path,
    environment: dict[str, str],
    assets: Iterable[AssetRecord],
) -> list[str]:
    """Reject inherited settings that could make the gate test another input."""

    problems: list[str] = []
    if os.environ.get("POKERED_SKIP_SHA1", "").strip():
        problems.append("POKERED_SKIP_SHA1 is set; release gates cannot use the hash bypass")

    asset_list = list(assets)
    rom_record: AssetRecord | None = None
    sym_record: AssetRecord | None = None
    rom_value = environment.get("POKERED_ROM_PATH")
    sym_value = environment.get("POKERED_SYM_PATH")

    if rom_value:
        rom_path = _resolve_child_path(rom_value, project_root)
        rom_record = _asset_record_for_path(asset_list, rom_path)
        if rom_record is None or rom_record.kind != "rom":
            problems.append(f"primary ROM is outside the inspected asset set: {rom_value}")
        elif rom_record.status != "ok":
            problems.append(
                f"primary ROM is not a verified asset: {rom_record.label} {rom_record.status}"
            )
    elif rom_value is not None:
        problems.append("POKERED_ROM_PATH is blank")

    if sym_value:
        sym_path = _resolve_child_path(sym_value, project_root)
        sym_record = _asset_record_for_path(asset_list, sym_path)
        if sym_record is None or sym_record.kind != "symbol":
            problems.append(f"primary symbols are outside the inspected asset set: {sym_value}")
        elif sym_record.status != "ok":
            problems.append(
                f"primary symbols are not a verified asset: {sym_record.label} {sym_record.status}"
            )
    elif sym_value is not None:
        problems.append("POKERED_SYM_PATH is blank")

    if rom_record is not None and sym_record is not None:
        rom_parent = Path(rom_record.path).parent.name
        sym_parent = Path(sym_record.path).parent.name
        if rom_parent != sym_parent:
            problems.append(
                "primary ROM and symbol files are from different version directories: "
                f"{rom_parent!r} vs {sym_parent!r}"
            )

    if rom_record is not None and rom_record.kind == "rom":
        selected_sha = environment.get("POKERED_ROM_SHA1", "").strip().lower()
        if not selected_sha:
            problems.append("POKERED_ROM_SHA1 is missing for the primary ROM")
        elif rom_record.actual_sha1 != selected_sha:
            problems.append(
                f"primary ROM SHA-1 does not match inspected bytes: "
                f"expected {rom_record.actual_sha1!r}, got {selected_sha!r}"
            )
    if sym_record is not None and sym_record.kind == "symbol":
        selected_sha = environment.get("POKERED_SYM_SHA1", "").strip().lower()
        if not selected_sha:
            problems.append("POKERED_SYM_SHA1 is missing for the primary symbols")
        elif sym_record.actual_sha1 != selected_sha:
            problems.append(
                f"primary symbols SHA-1 does not match inspected bytes: "
                f"expected {sym_record.actual_sha1!r}, got {selected_sha!r}"
            )
    return problems


def load_required_test_keys(
    project_root: Path,
) -> tuple[dict[str, frozenset[tuple[str, str]]], str]:
    """Load the strict acceptance manifest from the checked-out tier config."""

    config_path = project_root / "tests" / "_tier_config.py"
    if not config_path.is_file():
        return {}, f"tier configuration is missing: {config_path}"
    try:
        namespace = runpy.run_path(str(config_path))
    except Exception as exc:  # noqa: BLE001 - fail closed on any config-load error
        return {}, f"tier configuration could not be loaded: {type(exc).__name__}: {exc}"

    raw = namespace.get("TIER_REQUIRED_TESTS")
    if not isinstance(raw, dict):
        return {}, "tier configuration has no TIER_REQUIRED_TESTS manifest"

    result: dict[str, frozenset[tuple[str, str]]] = {}
    for tier_name, raw_keys in raw.items():
        if not isinstance(tier_name, str) or not isinstance(
            raw_keys, (set, frozenset, tuple, list)
        ):
            return {}, f"invalid required-test manifest entry for {tier_name!r}"
        normalized: set[tuple[str, str]] = set()
        for key in raw_keys:
            if not isinstance(key, (tuple, list)) or len(key) != 2:
                return {}, f"invalid required-test key in tier {tier_name!r}: {key!r}"
            module, test_name = key
            if not isinstance(module, str) or not isinstance(test_name, str):
                return {}, f"invalid required-test key in tier {tier_name!r}: {key!r}"
            normalized.add((module, test_name))
        result[tier_name] = frozenset(normalized)
    return result, ""


def load_required_nodeids(
    project_root: Path,
) -> tuple[dict[str, frozenset[str]], str]:
    """Load exact matrix/role node IDs required by the production gate."""

    config_path = project_root / "tests" / "_tier_config.py"
    if not config_path.is_file():
        return {}, f"tier configuration is missing: {config_path}"
    try:
        namespace = runpy.run_path(str(config_path))
    except Exception as exc:  # noqa: BLE001 - fail closed on any config-load error
        return {}, f"tier configuration could not be loaded: {type(exc).__name__}: {exc}"

    raw = namespace.get("TIER_REQUIRED_NODEIDS")
    if not isinstance(raw, dict):
        return {}, "tier configuration has no TIER_REQUIRED_NODEIDS manifest"

    result: dict[str, frozenset[str]] = {}
    for tier_name, raw_nodeids in raw.items():
        if not isinstance(tier_name, str) or not isinstance(
            raw_nodeids, (set, frozenset, tuple, list)
        ):
            return {}, f"invalid required-nodeid manifest entry for {tier_name!r}"
        normalized: set[str] = set()
        for nodeid in raw_nodeids:
            if not isinstance(nodeid, str) or not nodeid or "::" not in nodeid:
                return {}, f"invalid required node ID in tier {tier_name!r}: {nodeid!r}"
            normalized.add(_normalize_nodeid(nodeid))
        result[tier_name] = frozenset(normalized)
    return result, ""


def _reason_counter(payload: dict[str, Any]) -> dict[str, int]:
    reasons = Counter()
    for record in payload.get("tests", []):
        if record.get("outcome") == "skipped" or record.get("was_xfail"):
            reason = str(record.get("reason") or "(no reason reported)").strip()
            reasons[reason] += 1
    for entry in payload.get("collection_skips", []):
        reason = str(entry.get("reason") or "(collection skip without a reason)").strip()
        reasons[reason] += 1
    return dict(sorted(reasons.items()))


def _load_gate_report(
    path: Path,
    *,
    expected_returncode: int | None = None,
    allow_partial: bool = False,
) -> GateReport:
    """Read and validate a report produced by the gate pytest plugin.

    A timed-out pytest process cannot run ``pytest_sessionfinish``.  The
    plugin therefore maintains a separate progress report while tests run;
    that report is allowed to contain the selected node set plus only the
    terminal outcomes observed before termination.  It is never accepted as
    a complete report.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return GateReport(
            Counts(errors=1),
            error=f"could not read pytest report: {type(exc).__name__}: {exc}",
        )
    if not isinstance(payload, dict):
        return GateReport(Counts(errors=1), error="pytest report root is not an object")

    try:
        counts = Counts.from_report(payload)
        records = payload.get("tests")
        if not isinstance(records, list):
            raise TypeError("pytest report has no tests list")
        collection_only = payload.get("collection_only", False)
        if not isinstance(collection_only, bool):
            raise TypeError("pytest report collection_only is not boolean")

        collection_errors = payload.get("collection_errors")
        if not isinstance(collection_errors, list):
            raise TypeError("pytest report has no collection_errors list")
        collection_skips = payload.get("collection_skips", [])
        if not isinstance(collection_skips, list):
            raise TypeError("pytest report collection_skips is not a list")
        for collection_entry in (*collection_errors, *collection_skips):
            if not isinstance(collection_entry, dict):
                raise TypeError("pytest report collection entry is not an object")
            if not isinstance(collection_entry.get("nodeid"), str):
                raise TypeError("pytest report collection nodeid is not a string")
            if not isinstance(collection_entry.get("reason"), str):
                raise TypeError("pytest report collection reason is not a string")

        exitstatus = payload.get("exitstatus")
        if isinstance(exitstatus, bool) or not isinstance(exitstatus, int):
            raise TypeError("pytest report exitstatus is not an integer")
        if (
            expected_returncode is not None
            and expected_returncode >= 0
            and exitstatus != expected_returncode
        ):
            raise ValueError(
                "pytest report exitstatus does not match process returncode: "
                f"{exitstatus} != {expected_returncode}"
            )

        collected = payload.get("collected")
        nodeids = payload.get("nodeids")
        if isinstance(collected, bool) or not isinstance(collected, int) or collected < 0:
            raise ValueError("pytest report collected count is invalid")
        if not isinstance(nodeids, list) or any(not isinstance(nodeid, str) for nodeid in nodeids):
            raise ValueError("pytest report nodeids is invalid")
        if collected != len(nodeids) or len(set(nodeids)) != len(nodeids):
            raise ValueError("pytest report collected/nodeids are inconsistent")
        if not collection_only and not allow_partial and collected != len(records):
            raise ValueError(
                "pytest report collected item count does not match reported test outcomes: "
                f"{collected} != {len(records)}"
            )
        if collection_only and records:
            raise ValueError("collection-only pytest report contains test outcomes")

        derived = Counts(total=len(records), errors=len(collection_errors))
        seen_nodeids: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise TypeError("pytest report contains a non-object test record")
            nodeid = record.get("nodeid")
            if not isinstance(nodeid, str) or not nodeid or nodeid in seen_nodeids:
                raise ValueError("pytest report contains an invalid or duplicate nodeid")
            seen_nodeids.add(nodeid)
            outcome = record.get("outcome")
            if outcome not in {"passed", "failed", "skipped", "error"}:
                raise ValueError(f"pytest report contains unknown outcome {outcome!r}")
            if record.get("when") not in {"setup", "call", "teardown"}:
                raise ValueError("pytest report contains an invalid test phase")
            if not isinstance(record.get("was_xfail"), bool):
                raise TypeError("pytest report was_xfail must be boolean")
            if not isinstance(record.get("reason"), str):
                raise TypeError("pytest report reason must be a string")

            if record["was_xfail"] and outcome == "skipped":
                derived.xfailed += 1
            elif record["was_xfail"] and outcome == "passed":
                derived.xpassed += 1
            elif outcome == "passed":
                derived.passed += 1
            elif outcome == "skipped":
                derived.skipped += 1
            elif outcome == "failed":
                derived.failed += 1
            else:
                derived.errors += 1

        if derived.total != sum(
            getattr(derived, field_name)
            for field_name in ("passed", "failed", "skipped", "xfailed", "xpassed")
        ) + (derived.errors - len(collection_errors)):
            raise ValueError("pytest report test outcomes do not add up to total")

        if not collection_only and not allow_partial and seen_nodeids != set(nodeids):
            raise ValueError("pytest report nodeids do not match test records")
        if allow_partial and not seen_nodeids.issubset(set(nodeids)):
            raise ValueError("partial pytest report contains an unselected test outcome")
        if counts != derived:
            raise ValueError(
                "pytest report count fields do not match individual outcomes: "
                f"declared={counts!r}, derived={derived!r}"
            )
    except (TypeError, ValueError) as exc:
        return GateReport(Counts(errors=1), error=f"invalid pytest report: {exc}")

    return GateReport(
        counts=counts,
        skip_reasons=_reason_counter(payload),
        nodeids=tuple(nodeids),
        collection_errors=tuple(collection_errors),
        collection_skips=tuple(collection_skips),
        collection_only=collection_only,
        failed_records=tuple(
            {
                "nodeid": record["nodeid"],
                "outcome": ("xfailed" if record["outcome"] == "skipped" else "xpassed")
                if record["was_xfail"] and record["outcome"] in {"skipped", "passed"}
                else record["outcome"],
                "reason": record["reason"],
            }
            for record in records
            if record["outcome"] != "passed" or record["was_xfail"]
        ),
    )


def load_gate_report(path: Path) -> tuple[Counts, dict[str, int], str]:
    """Load the stable JSON emitted by ``tests._gate_report``.

    Keep this small compatibility wrapper for focused callers; subprocess
    execution uses the richer validated :class:`GateReport` directly.
    """

    report = _load_gate_report(path)
    return report.counts, report.skip_reasons, report.error
