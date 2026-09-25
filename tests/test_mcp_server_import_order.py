"""Every ``pokered_harness`` module must import first in a fresh interpreter (#240).

The #125 split gave the ``mcp_server`` support modules a module-level back-edge
into the facade.  That made seven of them fail when one was the very first
``pokered_harness`` module imported, because the facade reaches them through its
own ``from ... import <name>`` lines while it is still executing::

    ImportError: cannot import name '_dispatch_session_tool' from partially
    initialized module 'pokered_harness.mcp_server_tools' (most likely due to a
    circular import)

The defect is order-dependent — importing the facade first hides it completely —
so a test that runs after any other import in the same process cannot see it, and
neither can one that merely re-imports inside a warm interpreter.  Each row below
purges every ``pokered_harness`` module from ``sys.modules`` before importing its
subject, so the subject is the first harness module loaded and nothing else from
the package is present.

The purge is scoped: the modules that were already loaded are restored
immediately afterwards.  Leaving the freshly imported copies in ``sys.modules``
would be a real bug, not a cosmetic one — other test modules in the same pytest
process hold references to the *original* module objects, while a facade-level
``monkeypatch.setattr("pokered_harness.mcp_server.X", …)`` resolves through
``sys.modules`` at call time.  Without the restore, later rows would patch a
second copy of the facade that the already-bound callers never see, and they
would fail for a reason that has nothing to do with the code under test.

Measured cost: a per-module child interpreter was tried first and cost ~24 s per
module (58 minutes for the package) because the harness pulls in PyBoy/sdl2 and
the ``mcp`` SDK on every cold start; the purge-based form runs the whole package
in about 85 s on the same host and was verified to reproduce the identical
failing set (7 modules) against unfixed master, so it is not a weaker check.
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
PACKAGE_ROOT = SOURCE_ROOT / "pokered_harness"

FAILING_AT_BASE = (
    "pokered_harness.mcp_server_link_dispatch",
    "pokered_harness.mcp_server_remote",
    "pokered_harness.mcp_server_remote_contract",
    "pokered_harness.mcp_server_resources",
    "pokered_harness.mcp_server_serve",
    "pokered_harness.mcp_server_timed",
    "pokered_harness.mcp_server_tools",
)


def _package_modules() -> list[str]:
    """Every importable module in the package, as dotted names."""

    names: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        parts = list(path.relative_to(SOURCE_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            names.append(".".join(parts))
    return names


MODULES = _package_modules()


def _purge_package() -> None:
    for name in _package_module_names():
        del sys.modules[name]


def _package_module_names() -> list[str]:
    """The names of every currently loaded ``pokered_harness`` module."""

    return [
        key for key in sys.modules if key == "pokered_harness" or key.startswith("pokered_harness.")
    ]


@contextlib.contextmanager
def _imported_first(name: str) -> Iterator[None]:
    """Import ``name`` into an otherwise empty package, then restore the old one.

    The subject must be the first harness module loaded, so the package is
    purged first.  The saved modules are put back afterwards: ``sys.modules`` is
    process-global, and leaving the replacement copies behind would leave later
    tests holding one module object while ``monkeypatch`` patches another.
    """

    saved = {key: sys.modules[key] for key in _package_module_names()}
    _purge_package()
    try:
        importlib.import_module(name)
        yield
    finally:
        for key in _package_module_names():
            del sys.modules[key]
        sys.modules.update(saved)


def test_the_probe_covers_the_whole_package_and_the_split_modules():
    """The rows below only mean something if they enumerate the split modules."""

    assert len(MODULES) > 55, f"expected the whole package, found {len(MODULES)}"
    for split_module in FAILING_AT_BASE:
        assert split_module in MODULES, f"{split_module} is missing from the probe"


def test_no_module_needs_another_harness_module_imported_first():
    """Import every module with nothing else from the package loaded.

    This is the regression row for #240: on the unfixed tree it reports the seven
    modules listed in :data:`FAILING_AT_BASE`.
    """

    failures: list[str] = []
    for name in MODULES:
        try:
            with _imported_first(name):
                pass
        except Exception as exc:  # noqa: BLE001 - the message is the assertion payload
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    assert not failures, (
        "these modules cannot be imported first in a fresh interpreter:\n  " + "\n  ".join(failures)
    )


@pytest.mark.parametrize("module", FAILING_AT_BASE)
def test_previously_broken_module_imports_first(module: str):
    """Keep a per-module row so a single regression names the exact module."""

    with _imported_first(module):
        pass


def test_the_purge_restores_the_already_loaded_modules():
    """The probe must not leave a second copy of the package in ``sys.modules``.

    This is the regression row for the leak described in the module docstring:
    if the freshly imported copies survive, every later test that patches
    ``pokered_harness.mcp_server`` patches a module object the callers never
    bound, and it fails for an unrelated reason.
    """

    before = {
        key: id(value) for key, value in sys.modules.items() if key.startswith("pokered_harness")
    }
    assert before, "the package should already be imported by the time this runs"

    with _imported_first("pokered_harness.mcp_server"):
        pass

    after = {
        key: id(value) for key, value in sys.modules.items() if key.startswith("pokered_harness")
    }
    assert after == before, (
        "the import probe replaced live package modules; "
        f"changed={sorted(set(before) ^ set(after))} "
        f"replaced={sorted(k for k in set(before) & set(after) if before[k] != after[k])}"
    )
