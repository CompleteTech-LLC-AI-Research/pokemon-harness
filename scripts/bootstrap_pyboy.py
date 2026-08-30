#!/usr/bin/env python3
"""Install the pinned PyBoy source runtime into the active environment.

The normal harness distribution already bundles this source tree.  This
bootstrap is for the two explicit runtime modes used by development and
performance testing:

* ``source`` (default): disable Cython and install the same Python sources;
* ``cython``: build the same sources with PyBoy's Cython extensions.

Both modes use the checked-in source snapshot.  No network VCS checkout,
``PYTHONPATH`` override, or machine-specific path is involved.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.machinery
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYBOY_SOURCE = ROOT / "vendor" / "pyboy-src"
REVISION_FILE = PYBOY_SOURCE / "POKERED_HARNESS_PYBOY_REVISION"
EXPECTED_PYBOY_VERSION = "2.7.0"
EXPECTED_REVISION = "c565df66c3731fad2856169a90f6bbec99925915"
CYTHON_REQUIREMENT = "cython==3.0.12"
RUNTIME_MODULES = (
    "pyboy",
    "pyboy.pyboy",
    "pyboy.core.mb",
    "pyboy.core.serial",
)


def _validate_source() -> None:
    if not PYBOY_SOURCE.is_dir():
        raise SystemExit(f"vendored PyBoy source is missing: {PYBOY_SOURCE}")
    try:
        revision = REVISION_FILE.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read PyBoy revision marker: {REVISION_FILE}: {exc}") from exc
    if revision != EXPECTED_REVISION:
        raise SystemExit(
            "vendored PyBoy revision mismatch: "
            f"expected {EXPECTED_REVISION}, got {revision or '<empty>'}"
        )


def _module_kind(module: object) -> str:
    """Classify the loaded module as source Python or a Cython extension."""
    filename = str(getattr(module, "__file__", "") or "")
    if filename.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
        return "cython"
    if filename.endswith(".py"):
        return "source"
    return "unknown"


def _verify_runtime(mode: str) -> None:
    """Fail closed unless the installed runtime matches the requested mode."""
    try:
        modules = {name: importlib.import_module(name) for name in RUNTIME_MODULES}
        import pyboy
        from pyboy.core.serial import Serial
        from pyboy import utils
    except Exception as exc:  # noqa: BLE001 - turn import failures into an actionable CLI error
        raise SystemExit(
            "PyBoy runtime cannot be imported after bootstrap; install the "
            f"harness dependencies first or inspect the build output: {type(exc).__name__}: {exc}"
        ) from exc

    expected_kind = "cython" if mode == "cython" else "source"
    problems: list[str] = []
    if getattr(pyboy, "__version__", None) != EXPECTED_PYBOY_VERSION:
        problems.append(
            f"version={getattr(pyboy, '__version__', None)!r}, expected {EXPECTED_PYBOY_VERSION!r}"
        )
    if getattr(pyboy, "__pokered_harness_revision__", None) != EXPECTED_REVISION:
        problems.append(
            "revision="
            f"{getattr(pyboy, '__pokered_harness_revision__', None)!r}, expected {EXPECTED_REVISION!r}"
        )

    for name, module in modules.items():
        actual_kind = _module_kind(module)
        if actual_kind != expected_kind:
            problems.append(f"{name} is {actual_kind}, expected {expected_kind}")

    if bool(getattr(utils, "cython_compiled", False)) != (mode == "cython"):
        problems.append(
            f"cython_compiled={getattr(utils, 'cython_compiled', None)!r}, "
            f"expected {mode == 'cython'!r}"
        )

    serial = Serial(False)
    missing = [
        name
        for name in ("backend", "apply_external_edge", "peek_out_bit")
        if not hasattr(serial, name)
    ]
    if missing:
        problems.append(f"serial contract missing {', '.join(missing)}")

    if problems:
        raise SystemExit(
            "PyBoy runtime contract failed for "
            f"--mode {mode}: "
            + "; ".join(problems)
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("source", "cython"),
        default="source",
        help="runtime build mode (default: source)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the active PyBoy runtime without reinstalling it",
    )
    args = parser.parse_args(argv)

    _validate_source()
    if args.check:
        _verify_runtime(args.mode)
        return 0

    env = os.environ.copy()
    if args.mode == "source":
        env["PYBOY_NO_CYTHON"] = "1"
    else:
        env.pop("PYBOY_NO_CYTHON", None)

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        CYTHON_REQUIREMENT,
        str(PYBOY_SOURCE),
    ]
    result = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if result.returncode:
        return result.returncode
    _verify_runtime(args.mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
