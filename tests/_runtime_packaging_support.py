"""Shared constants and helpers for the runtime-packaging tests (#152).

Split from ``tests/test_runtime_packaging.py`` for #152 with no behavior
change. Every constant and helper below is copied verbatim from the
original module.
"""

import importlib.metadata
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


EXPECTED_PYBOY_REVISION = "d9b9648ab865080ac0e8404c8f1cdfb9c08b5b47"


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
