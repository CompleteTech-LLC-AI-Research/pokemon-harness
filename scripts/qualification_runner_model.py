"""Constants, schemas and dataclasses.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

import re

SCHEMA_VERSION = 3


_DECLARATION_ENV = "POKERED_QUALIFICATION_DECLARATION"


_RESERVATION_MECHANISMS = ("dedicated-host", "cgroup-quota", "cpuset-affinity")


_CPU_BINDING_MECHANISMS = ("dedicated-host", "cpuset-affinity")


_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


_DESCRIPTOR_VERSION = 1


_PROBE_TIMEOUT_ENV = "POKERED_QUALIFICATION_COMMAND_TIMEOUT_SECONDS"


_QUALIFICATION_TIMEOUT_ENV = "POKERED_QUALIFICATION_RUN_TIMEOUT_SECONDS"


_DEFAULT_PROBE_TIMEOUT_SECONDS = 300.0


_DEFAULT_QUALIFICATION_TIMEOUT_SECONDS = 86400.0


_NATIVE_BUILD_EVIDENCE_PROCEDURE = "bootstrap_pyboy --mode cython"


_NATIVE_BUILD_EVIDENCE_VERSION = 2


_TIMEOUT_RETURNCODE = 124


_CHILD_TERMINATION_GRACE_SECONDS = 5.0


_FOREIGN_SAMPLE_ENV = "POKERED_QUALIFICATION_FOREIGN_SAMPLE_SECONDS"


_DEFAULT_FOREIGN_SAMPLE_SECONDS = 2.0


_DEFAULT_FOREIGN_CPU_CORES_TOLERANCE = 0.25


# Descriptors this process holds open for an allocation lease, keyed by the
# lock's kernel identity.  Release must explicitly close them: removing the
# state file while a descriptor still pins the flock would report a released
# lease that this process still holds.
_HELD_LEASE_FDS: dict[tuple[int, int], list[int]] = {}


_RUNTIME_MODULES = (
    "pyboy",
    "pyboy.pyboy",
    "pyboy.utils",
    "pyboy.core.mb",
    "pyboy.core.serial",
    "pyboy.link",
)


_EXTENSION_BACKED_MODULES = (
    "pyboy.pyboy",
    "pyboy.utils",
    "pyboy.core.mb",
    "pyboy.core.serial",
)


_NATIVE_PROBE = """\
import hashlib, importlib, importlib.machinery, json, sys
from pathlib import Path

names = %(modules)r
try:
    import pyboy
except Exception as exc:
    print(json.dumps({"error": f"pyboy: {type(exc).__name__}: {exc}"}))
    raise SystemExit(2)

package_root = Path(pyboy.__file__).resolve().parent


def _relative(filename):
    path = Path(filename)
    try:
        return path.resolve().relative_to(package_root).as_posix()
    except ValueError:
        return path.name


report = {}
for name in names:
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        print(json.dumps({"error": f"{name}: {type(exc).__name__}: {exc}"}))
        raise SystemExit(2)
    filename = str(getattr(module, "__file__", "") or "")
    kind = "unknown"
    digest = None
    if filename:
        if filename.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
            kind = "cython"
        elif filename.endswith(".py"):
            kind = "source"
        try:
            with open(filename, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
        except OSError:
            digest = None
    report[name] = {
        "kind": kind,
        "sha256": digest,
        "artifact": _relative(filename) if filename else None,
    }

# The complete installed output set, not only the named entry modules: a mixed
# build can replace a compiled module the module list never names (for example
# ``pyboy/core/cpu*.so``) while every listed module stays byte-identical.
artifacts = {}
for entry in sorted(package_root.rglob("*")):
    if "__pycache__" in entry.parts or entry.suffix in (".pyc", ".pyo"):
        continue
    if not entry.is_file():
        continue
    try:
        artifacts[entry.relative_to(package_root).as_posix()] = hashlib.sha256(
            entry.read_bytes()
        ).hexdigest()
    except OSError:
        print(json.dumps({"error": f"unreadable installed artifact: {entry.name}"}))
        raise SystemExit(3)

from pyboy import utils

identity = {
    "python": sys.version.split()[0],
    "version": getattr(pyboy, "__version__", None),
    "revision": getattr(pyboy, "__pokered_harness_revision__", None),
    "cython_compiled": bool(getattr(utils, "cython_compiled", False)),
    "modules": report,
    "artifacts": artifacts,
}
fingerprint = hashlib.sha256(
    json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
print(json.dumps({"identity": identity, "fingerprint": fingerprint}))
"""


_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w./-])(/(?:[^\s:'\"()\[\],;]+)+)")
