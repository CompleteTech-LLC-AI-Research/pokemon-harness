#!/usr/bin/env python3
"""Install the pinned PyBoy source runtime into the active environment.

The normal harness distribution already bundles this source tree.  This
bootstrap is for the two explicit runtime modes used by development and
performance testing:

* ``source`` (default): disable Cython and install the harness distribution,
  including its bundled Python sources;
* ``cython``: install the current checkout as an editable harness distribution
  and build the checked-in PyBoy fork with its Cython extensions. This is an
  optional source-checkout diagnostic; source mode is the supported production
  runtime and a Cython compile failure is reported without weakening the
  source-runtime contract.

Both modes use the checked-in source snapshot.  No network VCS checkout,
``PYTHONPATH`` override, or machine-specific path is involved.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.machinery
import importlib.metadata
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYBOY_SOURCE = ROOT / "vendor" / "pyboy-src"
REVISION_FILE = PYBOY_SOURCE / "POKERED_HARNESS_PYBOY_REVISION"
EXPECTED_PYBOY_VERSION = "2.7.0"
EXPECTED_REVISION = "c565df66c3731fad2856169a90f6bbec99925915"
CYTHON_REQUIREMENT = "cython==3.0.12"
PROJECT_DISTRIBUTION = "pokered-harness"
RUNTIME_MODULES = (
    "pyboy",
    "pyboy.pyboy",
    "pyboy.utils",
    "pyboy.core.mb",
    "pyboy.core.serial",
)
CYTHON_MODULES = tuple(name for name in RUNTIME_MODULES if name != "pyboy")


def _pip_command() -> list[str]:
    """Return a usable install-command prefix for the active interpreter.

    ``uv venv`` and some embedded Python distributions intentionally omit
    pip.  The bootstrap command must still work in those environments, so
    install the standard-library copy first instead of assuming that
    ``python -m pip`` is already available.
    """
    command = [sys.executable, "-m", "pip"]
    probe = subprocess.run(
        [*command, "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode == 0:
        return [*command, "install"]

    bootstrap = subprocess.run(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        check=False,
    )
    if bootstrap.returncode != 0:
        # ``uv venv`` deliberately omits pip unless seeded, and distro Python
        # builds may omit ensurepip altogether. Use uv's interpreter-targeted
        # installer when it is available rather than silently falling back to
        # a different Python executable.
        uv = shutil.which("uv")
        if uv is not None:
            return [uv, "pip", "install", "--python", sys.executable]
        raise SystemExit(
            "pip is unavailable and ensurepip failed; install pip in the "
            "active environment (or install uv) before running bootstrap_pyboy.py"
        )

    verify = subprocess.run(
        [*command, "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if verify.returncode != 0:
        raise SystemExit(
            "ensurepip completed but the active interpreter still cannot run python -m pip"
        )
    return [*command, "install"]


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


def _normalise_distribution_name(name: str) -> str:
    return name.lower().replace("_", "-")


def _package_distributions(package: str) -> set[str]:
    """Return normalized distributions that claim ``package``.

    Editable installs can expose the same distribution more than once; the
    set removes that harmless duplication while preserving a competing owner
    such as a separately installed stock PyBoy.
    """
    return {
        _normalise_distribution_name(name)
        for name in importlib.metadata.packages_distributions().get(package, ())
    }


def _new_serial_instance() -> object:
    """Construct the serial object after the module import contract passes."""
    from pyboy.core.serial import Serial

    return Serial(False)


def _verify_runtime(mode: str) -> None:
    """Fail closed unless the installed runtime matches the requested mode."""
    try:
        modules = {name: importlib.import_module(name) for name in RUNTIME_MODULES}
        import pyboy
        from pyboy import utils
    except Exception as exc:
        raise SystemExit(
            "PyBoy runtime cannot be imported after bootstrap; install the "
            f"harness dependencies first or inspect the build output: {type(exc).__name__}: {exc}"
        ) from exc

    expected_modules = (
        {name: "cython" for name in CYTHON_MODULES}
        if mode == "cython"
        else {name: "source" for name in RUNTIME_MODULES}
    )
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

    for name, expected_kind in expected_modules.items():
        actual_kind = _module_kind(modules[name])
        if actual_kind != expected_kind:
            problems.append(f"{name} is {actual_kind}, expected {expected_kind}")

    owners = _package_distributions("pyboy")
    if PROJECT_DISTRIBUTION not in owners:
        problems.append("pyboy is not provided by the installed pokered-harness distribution")
    if mode == "source":
        unexpected = owners - {PROJECT_DISTRIBUTION}
        if unexpected:
            problems.append(
                "pyboy has competing installed owners: " + ", ".join(sorted(unexpected))
            )
    else:
        # Cython mode intentionally installs the checked-in PyBoy project as
        # an extension-backed distribution. Its revision marker remains
        # authoritative, so an unmodified stock package cannot pass below.
        unexpected = owners - {PROJECT_DISTRIBUTION, "pyboy"}
        if unexpected:
            problems.append("pyboy has foreign installed owners: " + ", ".join(sorted(unexpected)))

    if bool(getattr(utils, "cython_compiled", False)) != (mode == "cython"):
        problems.append(
            f"cython_compiled={getattr(utils, 'cython_compiled', None)!r}, "
            f"expected {mode == 'cython'!r}"
        )

    try:
        serial = _new_serial_instance()
    except Exception as exc:  # noqa: BLE001 - an incompatible ABI must fail closed
        # A separately installed stock PyBoy may import successfully while
        # exposing an incompatible constructor or ABI. Report it alongside
        # the ownership/module-kind violations instead of leaking a traceback.
        problems.append(
            "serial contract could not be constructed: "
            f"{type(exc).__name__}: {exc}"
        )
    else:
        missing = [
            name
            for name in ("backend", "apply_external_edge", "peek_out_bit")
            if not hasattr(serial, name)
        ]
        if missing:
            problems.append(f"serial contract missing {', '.join(missing)}")

    if problems:
        raise SystemExit(f"PyBoy runtime contract failed for --mode {mode}: " + "; ".join(problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("source", "cython"),
        default="source",
        help=("production mode, cython is an optional source-checkout diagnostic"),
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
        install_target = ROOT
    else:
        env.pop("PYBOY_NO_CYTHON", None)
        install_target = PYBOY_SOURCE

    pip_install = _pip_command()
    if args.mode == "cython":
        # Keep the harness distribution installed in native environments too.
        # The vendored fork has its own ``pyboy`` distribution metadata, but
        # that package alone cannot provide the MCP entry point or harness
        # modules. Install the checkout first so the native fork can overlay
        # its extension-backed PyBoy modules without losing project ownership.
        project_command = [
            *pip_install,
            "--force-reinstall",
            "--no-deps",
            "-e",
            str(ROOT),
        ]
        project_result = subprocess.run(project_command, cwd=ROOT, env=env, check=False)
        if project_result.returncode:
            return project_result.returncode

    command = [
        *pip_install,
        "--force-reinstall",
        "--no-deps",
        CYTHON_REQUIREMENT,
        str(install_target),
    ]
    result = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if result.returncode:
        if args.mode == "cython":
            print(
                "Cython runtime build failed for the pinned PyBoy source; "
                "the supported production runtime remains --mode source.",
                file=sys.stderr,
            )
        return result.returncode
    _verify_runtime(args.mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
