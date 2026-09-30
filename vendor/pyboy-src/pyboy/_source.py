#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#
"""Single source assembly for Python loading and the optional Cython build.

The .pxi components are hand-authored Python, split at method/class boundaries.
They are not separate modules: keeping one namespace preserves Cython's
augmenting declarations, private name mangling, public identity and globals.
This module is deliberately excluded from Cythonization. It may also be loaded
by setup.py via run_path, without importing the emulator during a build.
"""

from __future__ import annotations

import linecache
from pathlib import Path
from typing import Any

COMPONENTS = (
    "_pyboy_init.pxi",
    "_pyboy_runtime.pxi",
    "_pyboy_controls.pxi",
    "_pyboy_api.pxi",
    "_pyboy_memory.pxi",
)


def assemble_source(package_dir: str | Path | None = None) -> bytes:
    """Return the ordered source bytes, without rewriting or normalizing them.

    Only the fixed component names above are read. A missing component is a
    packaging error and propagates; there is no stock-runtime fallback.
    """
    root = Path(package_dir) if package_dir is not None else Path(__file__).parent
    return b"".join((root / name).read_bytes() for name in COMPONENTS)


def load_module(namespace: dict[str, Any]) -> None:
    """Execute trusted installed source in the public module's own namespace."""
    filename = namespace["__file__"]
    source = assemble_source(Path(filename).parent)
    code = compile(source, filename, "exec", dont_inherit=True)
    # CPython's inspect and traceback machinery must see the assembled lines,
    # not the short on-disk facade. None keeps checkcache from substituting it.
    text = source.decode("utf-8")
    linecache.cache[filename] = (len(source), None, text.splitlines(keepends=True), filename)
    exec(code, namespace, namespace)


def _write_if_changed(path: Path, contents: bytes) -> None:
    if not path.is_file() or path.read_bytes() != contents:
        path.write_bytes(contents)


def stage_native_source(package_dir: str | Path, build_root: str | Path) -> Path:
    """Stage the identical .py and augmenting .pxd under a disposable build root.

    The returned path is compiler input, not a new import path. Extension names
    and all cimports remain pyboy.pyboy; setup.py supplies the real source root
    as a Cython include directory. No generated input is committed or packaged.
    """
    package_dir = Path(package_dir)
    destination = Path(build_root) / "pyboy"
    resolved_source = package_dir.resolve()
    resolved_destination = destination.resolve()
    if resolved_destination == resolved_source or resolved_source in resolved_destination.parents:
        raise ValueError("Native staging must be outside the importable source package")
    # Read both first, so an absent declaration fails before staging anything.
    source = assemble_source(package_dir)
    declarations = (package_dir / "pyboy.pxd").read_bytes()
    destination.mkdir(parents=True, exist_ok=True)
    _write_if_changed(destination / "pyboy.py", source)
    _write_if_changed(destination / "pyboy.pxd", declarations)
    return destination / "pyboy.py"
