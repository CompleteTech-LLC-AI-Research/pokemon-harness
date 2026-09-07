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
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYBOY_SOURCE = ROOT / "vendor" / "pyboy-src"
REVISION_FILE = PYBOY_SOURCE / "POKERED_HARNESS_PYBOY_REVISION"
EXPECTED_REVISION = "c565df66c3731fad2856169a90f6bbec99925915"


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


def _check_contract() -> None:
    """Validate the checked-in source can satisfy the harness import contract."""
    _validate_source()
    old_no_cython = os.environ.get("PYBOY_NO_CYTHON")
    os.environ["PYBOY_NO_CYTHON"] = "1"
    sys.path.insert(0, str(PYBOY_SOURCE.parent))
    try:
        import pyboy  # noqa: F401
        from pyboy.core.serial import Serial, SerialCore
        from pyboy.link import LinkSession  # noqa: F401
    except Exception as exc:
        raise SystemExit(f"PyBoy source contract failed: {exc}") from exc
    finally:
        if old_no_cython is None:
            os.environ.pop("PYBOY_NO_CYTHON", None)
        else:
            os.environ["PYBOY_NO_CYTHON"] = old_no_cython
    if SerialCore is not Serial:
        raise SystemExit("PyBoy source contract failed: SerialCore is not Serial")
    print(f"PyBoy source contract OK: revision={EXPECTED_REVISION} serial={Serial.__name__}")


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
        help="validate the vendored source/import contract without installing",
    )
    args = parser.parse_args(argv)

    _validate_source()
    if args.check:
        _check_contract()
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
        "--no-deps",
        str(PYBOY_SOURCE),
    ]
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
