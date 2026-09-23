"""Runtime identity measurement and PyBoy module probes.

Split out of ``scripts/produce_battle_scenario.py`` for the #122 file-size
contract with no behavior change: the code below is copied verbatim except that
calls to facade-owned, monkeypatch-patched entry points resolve through
``_entry`` so attribute patches on the loaded producer module stay visible.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.produce_battle_scenario as _entry
from scripts.produce_battle_scenario_model import (
    _CAPTURE_MESSAGE,
    _RUNTIME_MODES,
    CaptureNotAvailable,
    ScenarioRefusal,
)


def _default_session_factory(
    *,
    rom: str | Path,
    sym: str | Path,
    repo_root: str | Path,
    runtime: str,
    python: str | Path | None,
    plan: dict[str, Any],
) -> Any:
    """Open the pinned PyBoy session, or refuse when none can be established.

    ``runtime`` and ``python`` are not consumed here: the caller measures the
    executing runtime and refuses a mismatch before this factory runs, so the
    requested labels can never be recorded as if they had been observed.
    """
    del runtime, python, plan
    try:
        from pokered_harness.config import load_versions
        from pokered_harness.session import Session
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} The pinned PyBoy runtime is not importable: {exc}"
        ) from exc
    pins = load_versions(Path(repo_root) / "VERSIONS.md")
    return Session.from_files(
        rom,
        sym,
        expected_rom_sha1=pins.sha1_for_path(rom),
        expected_symbol_sha1=pins.symbol_sha1_for_path(sym),
        expected_pyboy_version=pins.pyboy_version,
        expected_pyboy_revision=pins.pyboy_revision,
    )


def _pyboy_cython_flag() -> bool:
    """Return the executing PyBoy's compiled-extension flag."""
    import pyboy.utils

    return bool(getattr(pyboy.utils, "cython_compiled", False))


def _pyboy_module_version() -> str | None:
    """Return the version of the *imported* PyBoy module, or ``None``."""
    import pyboy

    for attribute in ("__version__", "VERSION"):
        value = getattr(pyboy, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _pyboy_module_revision() -> str | None:
    """Return the build revision of the *imported* PyBoy module, or ``None``."""
    import pyboy
    import pyboy.utils

    for module in (pyboy, pyboy.utils):
        for attribute in ("__pokered_harness_revision__", "__revision__", "revision"):
            value = getattr(module, attribute, None)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _interpreter_environment(path: Path) -> str:
    """Return the environment root a ``<environment>/bin/python`` path belongs to.

    ``.venv-source/bin/python`` and ``.venv-native/bin/python`` can both be
    symlinks to the same ``/usr/bin/python3.11``, so the resolved binary cannot
    tell two environments apart.  The directory above ``bin`` is what identifies
    the environment, exactly as ``sys.prefix`` does for the running process.
    """
    return str(path.parent.parent)


def measure_runtime_identity(
    *,
    runtime: str,
    python: str | Path | None,
    repo_root: str | Path,
) -> dict[str, Any]:
    """Measure the executing runtime and enforce the requested labels.

    A caller's ``runtime``/``python`` arguments are requests, not evidence.  The
    mode is read from the imported PyBoy build, the interpreter *environment*
    from the running process, and the recorded module versions from the imported
    modules; a request that disagrees is refused instead of being copied into a
    record as if it had been observed.
    """
    if runtime not in _RUNTIME_MODES:
        raise ScenarioRefusal(f"runtime must be one of {list(_RUNTIME_MODES)}, got {runtime!r}")
    try:
        compiled = _entry._pyboy_cython_flag()
        observed_version = _entry._pyboy_module_version()
        observed_revision = _entry._pyboy_module_revision()
    except ImportError as exc:
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} The pinned PyBoy runtime is not importable: {exc}"
        ) from exc
    mode = "cython" if compiled else "source"
    if runtime != mode:
        raise ScenarioRefusal(
            f"requested runtime {runtime!r} but the executing PyBoy reports {mode!r}; refusing "
            "to record a runtime this process does not provide"
        )
    executable = str(sys.executable)
    environment = str(sys.prefix)
    if python is not None:
        requested_environment = _interpreter_environment(Path(python))
        if requested_environment != environment:
            raise ScenarioRefusal(
                f"requested interpreter {python} belongs to the environment "
                f"{requested_environment!r}, which is not the executing interpreter "
                f"environment {environment!r} ({executable}); refusing to record an interpreter "
                "this process does not run in, even when both binaries resolve to the same file"
            )
    from pokered_harness.config import load_versions

    pins = load_versions(Path(repo_root) / "VERSIONS.md")
    return {
        "mode": mode,
        "executable": str(Path(sys.executable).resolve()),
        "executable_path": executable,
        "environment": environment,
        "base_environment": str(sys.base_prefix),
        "isolated_environment": environment != str(sys.base_prefix),
        "requested_python": str(python) if python is not None else None,
        "python_version": sys.version.split()[0],
        "pyboy_version": pins.pyboy_version,
        "pyboy_revision": pins.pyboy_revision,
        "pyboy_version_observed": observed_version,
        "pyboy_revision_observed": observed_revision,
        "measured": True,
    }


def _producer_revision(repo_root: str | Path) -> str | None:
    """Best-effort enclosing commit for the produced provenance record."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    revision = result.stdout.strip()
    return revision or None
