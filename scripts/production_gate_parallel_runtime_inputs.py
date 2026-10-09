"""Private immutable runtime inputs for concurrent jobs, or proof that the OS already protects them (#107 F2).

A job must not execute against a dependency tree any process of this UID can rewrite.  For each
runtime interpreter this module resolves what will really execute (the interpreter, its stdlib, its
default ``sys.path``, its site directories and the shared libraries a real run maps), and then:

* a virtual environment is COPIED into a private tree, its editable-install ``.pth`` files are
  redirected from the original checkout to the immutable snapshot, and the copy is frozen (every
  write bit removed, a real write is attempted and must be refused).  The job runs the copy's
  interpreter, so its default ``sys.path`` and site-packages are the frozen copy;
* the base interpreter, its stdlib and every other mapped library must already be unwritable for
  the executing UID (the kernel's own ``access`` check, not a digest taken afterwards).  If any is
  writable no run is started: copying a base prefix is not implemented, so this fails closed.

No permission of any shared tree is changed; only this module's own copies are frozen.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_isolation import (
    IsolationError,
    freeze_tree,
    write_denied_problems,
)

PROBE = r"""
import json, os, site, sys, sysconfig
paths = sysconfig.get_paths()
for name in ("numpy", "pyboy"):
    try:
        __import__(name)
    except Exception:
        pass
maps = set()
try:
    with open("/proc/self/maps") as handle:
        for line in handle:
            parts = line.split(None, 5)
            if len(parts) == 6 and parts[5].startswith("/") and ".so" in parts[5]:
                maps.add(parts[5].strip())
except OSError:
    pass
print(json.dumps({
    "real_executable": os.path.realpath(sys.executable), "prefix": sys.prefix,
    "base_prefix": sys.base_prefix, "path": list(sys.path), "stdlib": paths["stdlib"],
    "platstdlib": paths["platstdlib"], "sites": site.getsitepackages(),
    "libraries": sorted(maps)}))
"""
PROBE_TIMEOUT_SECONDS = 120
WRITABLE_REPORT_LIMIT = 8


def probe_runtime(interpreter: str, pythonpath: str) -> dict[str, Any]:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": pythonpath,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        result = subprocess.run(
            [interpreter, "-c", PROBE],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
        info = json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        raise IsolationError(f"the runtime of {interpreter} could not be resolved: {exc}") from exc
    if result.returncode != 0 or not isinstance(info, dict):
        raise IsolationError(f"the runtime of {interpreter} could not be resolved")
    return info


def writable_entries(root: Path, limit: int = WRITABLE_REPORT_LIMIT) -> list[str]:
    """Entries under ``root`` the executing UID could modify, by the kernel's own access check."""

    found: list[str] = []
    stack = [root]
    while stack and len(found) < limit:
        path = stack.pop()
        try:
            if os.access(path, os.W_OK):
                found.append(str(path))
            elif path.is_dir() and not path.is_symlink():
                stack.extend(path.iterdir())
        except OSError:
            continue
    return found


def redirect_editable_paths(copy: Path, original: Path, snapshot: Path) -> int:
    """Point editable-install files of the private copy at the immutable snapshot checkout."""

    spellings = {str(original).encode(), os.path.realpath(original).encode()}
    new, changed = str(snapshot).encode(), 0
    for path in copy.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix == ".pth" or path.name.startswith("__editable__"):
            data = original_data = path.read_bytes()
            for old in spellings:
                data = data.replace(old, new)
            if data != original_data:
                path.write_bytes(data)
                changed += 1
    return changed


def build_private_runtimes(
    interpreters: Mapping[str, str],
    *,
    project_root: Path,
    checkout: Path,
    destination: Path,
) -> dict[str, dict[str, Any]]:
    """Resolve, classify and (for virtual environments) copy and freeze each runtime."""

    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    pythonpath = os.pathsep.join(str(checkout / part) for part in ("src", "vendor/pyboy-src", "."))
    copies: dict[str, Path] = {}
    results: dict[str, dict[str, Any]] = {}
    for mode, interpreter in sorted(interpreters.items()):
        info = probe_runtime(interpreter, pythonpath)
        prefix, base = Path(info["prefix"]), Path(info["base_prefix"])
        problems: list[str] = []
        protected: list[str] = []
        for label, root in (
            ("interpreter", info["real_executable"]),
            ("stdlib", info["stdlib"]),
            ("platstdlib", info["platstdlib"]),
        ):
            if not Path(root).exists():
                continue
            found = writable_entries(Path(root))
            if found:
                problems.append(
                    f"{mode}: the {label} is writable by the executing UID and a private copy of "
                    f"the base prefix is not implemented: {found[:3]}"
                )
            else:
                protected.append(str(root))
        private_prefix: Path | None = None
        run_interpreter = interpreter
        if prefix != base:
            if str(prefix) not in copies:
                target = destination / f"env-{len(copies)}"
                shutil.copytree(
                    prefix, target, symlinks=True, ignore=shutil.ignore_patterns("__pycache__")
                )
                redirect_editable_paths(target, project_root, checkout)
                freeze_tree(target)
                problems.extend(f"{mode}: {item}" for item in write_denied_problems(target))
                copies[str(prefix)] = target
            private_prefix = copies[str(prefix)]
            candidate = Path(interpreter)
            run_interpreter = str(
                private_prefix / candidate.relative_to(prefix)
                if candidate.is_relative_to(prefix)
                else private_prefix / "bin" / "python"
            )
            # The default sys.path of the interpreter the job will really run, not of the original.
            info = {**info, "path": probe_runtime(run_interpreter, pythonpath)["path"]}
            for entry in info["path"]:
                where = Path(entry) if entry else None
                if (
                    where is None
                    or where.is_relative_to(private_prefix)
                    or where.is_relative_to(checkout)
                ):
                    continue
                if where.exists() and writable_entries(where, 1):
                    problems.append(
                        f"{mode}: the private interpreter's default sys.path names {entry}, which "
                        "the executing UID can modify"
                    )
        else:
            for site_dir in info["sites"]:
                found = writable_entries(Path(site_dir))
                if found:
                    problems.append(
                        f"{mode}: a non-venv interpreter with writable site directories needs a "
                        f"private copy: {found[:3]}"
                    )
        libraries: list[str] = []
        for library in info["libraries"]:
            path = Path(library)
            if private_prefix is not None and path.is_relative_to(prefix):
                continue  # served from the frozen private copy
            if writable_entries(path, 1) or os.access(path.parent, os.W_OK):
                problems.append(f"{mode}: the linked library {library} is writable by this UID")
            else:
                libraries.append(library)
        results[mode] = {
            "interpreter": run_interpreter,
            "problems": problems,
            "manifest": {
                "resolved_executable": info["real_executable"],
                "prefix": str(prefix),
                "base_prefix": str(base),
                "private_prefix": str(private_prefix) if private_prefix else None,
                "kernel_protected": protected,
                "protected_libraries": libraries,
                "default_sys_path": list(info["path"]),
            },
        }
    return results


__all__ = ("build_private_runtimes", "probe_runtime", "redirect_editable_paths", "writable_entries")
