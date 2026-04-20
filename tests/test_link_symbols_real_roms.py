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


# Symbols RemoteLinkEndpoint depends on. These MUST resolve on every
# supported ROM or the two-agent link-cable mode is broken for that
# version. Unlike LINK_SYMBOLS (which abstracts per-version name drift
# behind keys like "exchange_bytes"), RemoteLinkEndpoint reads symbols
# by their literal pret name and translates addresses cross-version via
# the symbol table directly.
_REMOTE_ENDPOINT_REQUIRED_SYMBOLS = (
    "Serial_TryEstablishingExternallyClockedConnection",
    "Serial_ExchangeBytes",
    "Serial_ExchangeNybble",
    "Serial_ExchangeLinkMenuSelection",
    "hSerialConnectionStatus",
    "wSerialExchangeNybbleSendData",
    "wSerialExchangeNybbleReceiveData",
    "wLinkMenuSelectionSendBuffer",
    "wLinkMenuSelectionReceiveBuffer",
)

# Buffer symbols used as the "kind" in cross-version Serial_ExchangeBytes
# exchanges. At least the trade/battle-setup blocks must resolve so Blue's
# hl=0xD152 and Yellow's hl=0xD151 both translate to the same wire kind.
_REMOTE_ENDPOINT_BUFFER_SYMBOLS = (
    "wSerialPlayerDataBlock",
    "wSerialRandomNumberListBlock",
    "wSerialPartyMonsPatchList",
    "wSerialEnemyDataBlock",
    "wSerialOtherGameboyRandomNumberListBlock",
    "wSerialEnemyMonsPatchList",
)


@pytest.mark.parametrize("version", ["blue", "yellow", "red"])
def test_remote_endpoint_required_symbols_resolve(version: str):
    """Every symbol RemoteLinkEndpoint looks up at install() time must
    resolve on Red, Blue, and Yellow — otherwise the endpoint silently
    skips installing one of its hooks and the peer-to-peer exchange
    breaks in a hard-to-debug way."""
    path = SYM_PATHS[version]
    if not path.exists():
        pytest.skip(f"symbol file not present: {path}")
    table = load_sym_file(path)
    missing = [s for s in _REMOTE_ENDPOINT_REQUIRED_SYMBOLS if s not in table]
    assert not missing, (
        f"RemoteLinkEndpoint requires these symbols but they are missing "
        f"on {version}: {missing}"
    )


@pytest.mark.parametrize("version", ["blue", "yellow", "red"])
def test_remote_endpoint_buffer_symbols_resolve(version: str):
    """Every serial-buffer symbol in RemoteLinkEndpoint's
    ``candidate_symbols`` tuple must resolve so cross-version
    Serial_ExchangeBytes translation works for every buffer the game
    passes in hl."""
    path = SYM_PATHS[version]
    if not path.exists():
        pytest.skip(f"symbol file not present: {path}")
    table = load_sym_file(path)
    missing = [s for s in _REMOTE_ENDPOINT_BUFFER_SYMBOLS if s not in table]
    assert not missing, (
        f"RemoteLinkEndpoint treats these symbols as known exchange-bytes "
        f"buffers; they must resolve on {version}: {missing}"
    )


@pytest.mark.parametrize("version", ["blue", "yellow", "red"])
def test_remote_endpoint_cross_version_addresses_differ_where_expected(version: str):
    """Sanity check: buffer addresses differ between Yellow and Red/Blue
    for WRAM0 blocks in the ``d000-dfff`` range. This is the whole
    reason RemoteLinkEndpoint routes Serial_ExchangeBytes via symbol
    name instead of raw address."""
    if version != "yellow":
        pytest.skip("cross-version address divergence check only for yellow")
    blue_path = SYM_PATHS["blue"]
    yellow_path = SYM_PATHS["yellow"]
    if not (blue_path.exists() and yellow_path.exists()):
        pytest.skip("blue and yellow sym files both required")
    blue = load_sym_file(blue_path)
    yellow = load_sym_file(yellow_path)
    # WRAM0 buffers must differ — proves the symbol-based translation
    # actually does real work rather than being a no-op.
    for sym in ("wSerialPlayerDataBlock", "wSerialRandomNumberListBlock"):
        b = blue[sym].addr
        y = yellow[sym].addr
        assert b != y, (
            f"{sym} at same address on blue (0x{b:04x}) and yellow (0x{y:04x}); "
            f"cross-version translation test invariant no longer holds"
        )


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
