"""ROM-free public imports in the selected source OR installed native runtime.

Run this module once with each gate interpreter. Never add a vendor path here:
the caller selects the runtime, and each child must retain the parent's identity.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_DEADLINE_SECONDS = 20
_LOG_BYTES = 32 * 1024
_IDENTITY_CODE = """
import importlib
import importlib.machinery
import json
from pathlib import Path
import sys

def identity():
    result = {"executable": sys.executable, "prefix": sys.prefix}
    for name in ("pyboy", "pyboy.pyboy", "pyboy.core.serial"):
        module = importlib.import_module(name)
        path = str(Path(module.__file__).resolve())
        native = any(path.endswith(s) for s in importlib.machinery.EXTENSION_SUFFIXES)
        result[name] = {"path": path, "native": native}
    return result
"""


def _parent_identity():
    result = {"executable": sys.executable, "prefix": sys.prefix}
    for name in ("pyboy", "pyboy.pyboy", "pyboy.core.serial"):
        module = importlib.import_module(name)
        path = str(Path(module.__file__).resolve())
        native = any(path.endswith(s) for s in importlib.machinery.EXTENSION_SUFFIXES)
        result[name] = {"path": path, "native": native}
    return result


def _probe(body: str) -> None:
    expected = _parent_identity()
    # Preserve the selected search order, including gate configuration and
    # editable installs, without substituting another source/native runtime.
    code = (
        "import sys\nsys.path[:] = "
        + repr(sys.path)
        + "\n"
        + _IDENTITY_CODE
        + "\nexpected = "
        + repr(expected)
        + "\n"
        + "assert identity() == expected, (identity(), expected)\n"
        + "print('RUNTIME=' + json.dumps(identity(), sort_keys=True), flush=True)\n"
        + body
        + "\nassert identity() == expected, (identity(), expected)\n"
        + "print('IMPORT_PROBE_OK', flush=True)\n"
    )
    output = bytearray()
    done = threading.Event()
    with subprocess.Popen(
        [sys.executable, "-B", "-u", "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as process:

        def read_bounded():
            try:
                output.extend(process.stdout.read(_LOG_BYTES + 1))
            finally:
                done.set()

        reader = threading.Thread(target=read_bounded, daemon=True)
        reader.start()
        try:
            assert done.wait(_DEADLINE_SECONDS), (
                f"import probe exceeded {_DEADLINE_SECONDS}s; runtime={expected}"
            )
            assert len(output) <= _LOG_BYTES, "import probe exceeded log bound"
            process.wait(timeout=1)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            reader.join(timeout=1)
        diagnostic = output.decode("utf-8", errors="replace")
        assert process.returncode == 0, diagnostic
        assert diagnostic.splitlines()[-1] == "IMPORT_PROBE_OK", diagnostic
        identities = [line[8:] for line in diagnostic.splitlines() if line.startswith("RUNTIME=")]
        assert len(identities) == 1, diagnostic
        assert json.loads(identities[0]) == expected


@pytest.mark.parametrize(
    "requested_import",
    [
        "pyboy.core.serial",
        "pyboy.link",
        "pyboy.link.network",
        "pyboy.link.coordinator",
        "pyboy.link.session",
    ],
)
def test_selected_runtime_public_link_imports(requested_import):
    _probe(
        f"importlib.import_module({requested_import!r})\n"
        + """
from pyboy.core import serial
import pyboy.link as link
assert serial.SerialCore is serial.Serial
assert "SerialCore" in serial.__all__
assert "Serial" in serial.__all__
assert link.SerialCore is serial.Serial
for module in (serial, link):
    namespace = {}
    exec("from " + module.__name__ + " import *", namespace)
    for name in module.__all__:
        assert getattr(module, name) is namespace[name], name
for name in ("network", "coordinator", "session"):
    module = importlib.import_module("pyboy.link." + name)
    for exported in module.__all__:
        assert getattr(module, exported) is getattr(link, exported), exported
"""
    )


def test_selected_runtime_serial_constructor_and_pair_cleanup():
    _probe("""
from pyboy.core.serial import Serial, SerialCore, NullBackend
from pyboy.link import CoordinatedBackend, LockstepCoordinator, LinkSession
assert SerialCore is Serial
backend_a, backend_b = NullBackend(), NullBackend()
a = SerialCore(cgb_mode=False, backend=backend_a)
b = SerialCore(cgb_mode=False, backend=backend_b)
assert a.backend is backend_a and b.backend is backend_b
pair = LockstepCoordinator(a, b)
try:
    assert pair.attached
    assert pair.core_a is a and pair.core_b is b
    assert isinstance(a.backend, CoordinatedBackend)
    assert isinstance(b.backend, CoordinatedBackend)
    assert a.backend.peer is b and b.backend.peer is a
finally:
    pair.detach()
assert not pair.attached
assert a.backend is backend_a and b.backend is backend_b
pair.detach()
session = LinkSession.local()
try:
    assert session.cores == ()
finally:
    session.detach_all()
""")
