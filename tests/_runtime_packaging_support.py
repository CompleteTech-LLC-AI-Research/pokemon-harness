"""Shared constants and helpers for the runtime-packaging tests (#152).

Split from ``tests/test_runtime_packaging.py`` for #152. This module also
contains shared test-only utilities used by the split modules.
"""

import importlib.metadata
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pid_is_alive(pid: int) -> bool:
    """Treat a terminated zombie as exited without reaping another process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            raw = (proc_root / str(pid) / "stat").read_text(encoding="ascii")
        except FileNotFoundError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except OSError:
                return True
            return True
        except (OSError, UnicodeError):
            return True
        closing = raw.rfind(")")
        if closing < 0:
            return True
        fields = raw[closing + 1 :].split()
        if (
            len(fields) < 3
            or len(fields[0]) != 1
            or fields[0] not in "RSDZTtXxKWPI"
            or not fields[1].isdigit()
            or not fields[2].isdigit()
        ):
            return True
        # The outer child subreaper owns this exited child until pytest exits.
        # Do not reap it here; a zombie consumes no execution capacity.
        return fields[0] != "Z"
    return True


EXPECTED_PYBOY_REVISION = "fd765b1808ac9cb192b42ae971987158ff36ae48"


EXPECTED_RUNTIME_DEPENDENCIES = {
    "mcp": "==1.29.1",
    "cython": "==3.0.12",
    "pydantic": "==2.13.5",
    "pysdl2": "==0.9.17",
    "pysdl2-dll": "==2.32.10",
}


EXPECTED_NUMPY_DEPENDENCIES = (
    ("==2.4.6", "python_version < '3.12'"),
    ("==2.5.2", "python_version >= '3.12'"),
)


EXPECTED_DEV_DEPENDENCIES = {
    "pytest": "==9.1.1",
    "pytest-asyncio": "==1.4.0",
    "pytest-cov": "==7.1.0",
    "ruff": "==0.16.5",
}


def _split_exact_requirement(requirement: str) -> tuple[str, str, str | None]:
    requirement, separator_marker, marker = requirement.partition(";")
    name, separator, version = requirement.partition("==")
    assert separator == "==", requirement
    assert name and version, requirement
    return name.lower(), f"=={version}", marker.strip() if separator_marker else None


def _normalise_marker(marker: str | None) -> str | None:
    """Keep uv's full-version spelling comparable with project markers."""
    if marker is None:
        return None
    return marker.replace("python_full_version", "python_version")


def _expected_runtime_requirements() -> list[tuple[str, str, str | None]]:
    requirements = [
        (name, specifier, None) for name, specifier in EXPECTED_RUNTIME_DEPENDENCIES.items()
    ]
    requirements.extend(
        ("numpy", specifier, marker) for specifier, marker in EXPECTED_NUMPY_DEPENDENCIES
    )
    return sorted(requirements)


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location(
        "pokered_bootstrap_runtime_test", ROOT / "scripts" / "bootstrap_pyboy.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_bootstrap_with_owned_src_metadata_last(monkeypatch):
    """Load bootstrap with this checkout's source metadata last on sys.path."""
    source_root = (ROOT / "src").resolve()
    untouched = []
    owned = []
    for entry in list(sys.path):
        if Path(entry).resolve() == source_root:
            owned.append(entry)
        else:
            untouched.append(entry)
    monkeypatch.setattr(sys, "path", [*untouched, *owned])
    return _load_bootstrap()
