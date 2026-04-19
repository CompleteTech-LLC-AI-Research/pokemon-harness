"""Validate the LINK_SYMBOLS registry against real pokered / pokeyellow
symbol files.

Skipped unless the repo-root `rom/*/pokemon-*.sym` files are present. These
tests are what would have caught the original `Serial_TryEstablishingLink`
bug — label names change between upstream commits and we need a check that
runs against actual artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from pokered_harness.link.serial_bridge import (
    HRAM_SERIAL_RECEIVE,
    HRAM_SERIAL_SEND,
    HRAM_SERIAL_STATUS,
)
from pokered_harness.link.symbols import (
    LINK_SYMBOLS,
    LinkRole,
    resolve_link_symbols,
)
from pokered_harness.symbols.loader import load_sym_file


REPO_ROOT = Path(__file__).resolve().parents[1]
# Worktrees live under .claude/worktrees/<name>/; the shared rom/ directory
# lives at the true repo root. Walk up until we find it.
for parent in [REPO_ROOT, *REPO_ROOT.parents]:
    if (parent / "rom").is_dir():
        ROM_ROOT = parent / "rom"
        break
else:  # pragma: no cover - defensive
    ROM_ROOT = REPO_ROOT / "rom"


SYM_PATHS = {
    "blue": ROM_ROOT / "blue" / "pokemon-blue.sym",
    "yellow": ROM_ROOT / "yellow" / "pokemon-yellow.sym",
    "red": ROM_ROOT / "red" / "pokemon-red.sym",
}


@pytest.mark.parametrize("version", ["blue", "yellow", "red"])
def test_required_link_symbols_resolve_on_real_sym(version: str):
    path = SYM_PATHS[version]
    if not path.exists():
        pytest.skip(f"symbol file not present: {path}")
    table = load_sym_file(path)
    resolved = resolve_link_symbols(table, version)
    required_keys = {s.key for s in LINK_SYMBOLS if s.required}
    missing = required_keys - resolved.keys()
    assert not missing, f"required link symbols did not resolve on {version}: {missing}"


@pytest.mark.parametrize("version", ["blue", "yellow", "red"])
def test_hram_labels_resolve_on_real_sym(version: str):
    path = SYM_PATHS[version]
    if not path.exists():
        pytest.skip(f"symbol file not present: {path}")
    table = load_sym_file(path)
    for label in (HRAM_SERIAL_SEND, HRAM_SERIAL_RECEIVE, HRAM_SERIAL_STATUS):
        assert table.get(label) is not None, f"missing HRAM label {label!r} on {version}"


@pytest.mark.parametrize("version", ["blue", "yellow", "red"])
def test_bridge_and_handshake_labels_present(version: str):
    """The actual callbacks installed by SerialBridge cover BRIDGE and
    HANDSHAKE roles. Verify every non-optional such entry resolves, and
    that at least the primary exchange label is present."""
    path = SYM_PATHS[version]
    if not path.exists():
        pytest.skip(f"symbol file not present: {path}")
    table = load_sym_file(path)
    resolved = resolve_link_symbols(table, version)
    assert "exchange_bytes" in resolved
    assert "handshake" in resolved
    bridge_or_handshake = [
        s
        for s in LINK_SYMBOLS
        if s.role in (LinkRole.BRIDGE, LinkRole.HANDSHAKE) and s.required
    ]
    for sym in bridge_or_handshake:
        assert sym.key in resolved, (
            f"required {sym.role.value} symbol {sym.key} ({sym.per_version[version]}) "
            f"did not resolve on {version}"
        )
