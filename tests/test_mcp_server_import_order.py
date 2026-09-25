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

Measured cost: a per-module child interpreter was tried first and cost ~24 s per
module (58 minutes for the package) because the harness pulls in PyBoy/sdl2 and
the ``mcp`` SDK on every cold start; the purge-based form runs the whole package
in about 85 s on the same host and was verified to reproduce the identical
failing set (7 modules) against unfixed master, so it is not a weaker check.
"""

from __future__ import annotations

import importlib
import sys
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
    for name in [
        key for key in sys.modules if key == "pokered_harness" or key.startswith("pokered_harness.")
    ]:
        del sys.modules[name]


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
        _purge_package()
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - the message is the assertion payload
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    assert not failures, (
        "these modules cannot be imported first in a fresh interpreter:\n  " + "\n  ".join(failures)
    )


@pytest.mark.parametrize("module", FAILING_AT_BASE)
def test_previously_broken_module_imports_first(module: str):
    """Keep a per-module row so a single regression names the exact module."""

    _purge_package()
    importlib.import_module(module)
