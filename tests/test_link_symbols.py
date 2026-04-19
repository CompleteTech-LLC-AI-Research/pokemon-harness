"""Tests for the link-cable symbol registry.

These tests deliberately do not import the real
:class:`pokered_harness.symbols.loader.SymbolTable` — the registry only
relies on the ``.get(name) -> Symbol | None`` shape, so a tiny fake keeps
the tests focused on the registry's own logic.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pokered_harness.link.symbols import (
    LINK_SYMBOLS,
    LinkRole,
    LinkSymbol,
    resolve_link_symbols,
    symbols_for_role,
)


# ---------------------------------------------------------------------------
# Lightweight SymbolTable fake — just the minimum surface the registry uses.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeSym:
    name: str
    bank: int
    addr: int


class FakeSymbolTable:
    """Dict-backed stand-in for ``SymbolTable``."""

    def __init__(self, mapping: dict[str, tuple[int, int]]) -> None:
        self._by_name = {
            name: _FakeSym(name=name, bank=bank, addr=addr)
            for name, (bank, addr) in mapping.items()
        }

    def get(self, name: str) -> _FakeSym | None:
        return self._by_name.get(name)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_is_non_empty() -> None:
    assert len(LINK_SYMBOLS) > 0


def test_every_entry_has_all_three_versions() -> None:
    required_versions = {"red", "blue", "yellow"}
    for link_sym in LINK_SYMBOLS:
        assert set(link_sym.per_version.keys()) >= required_versions, (
            f"{link_sym.key} missing version keys: "
            f"{required_versions - set(link_sym.per_version.keys())}"
        )
        # Values are non-empty label strings.
        for v in required_versions:
            assert link_sym.per_version[v], f"{link_sym.key}[{v}] is empty"


def test_keys_are_unique() -> None:
    keys = [s.key for s in LINK_SYMBOLS]
    assert len(keys) == len(set(keys))


def test_progress_entries_have_event_name() -> None:
    progress = symbols_for_role(LinkRole.PROGRESS)
    assert len(progress) > 0
    for s in progress:
        assert s.event_name is not None, f"{s.key} is PROGRESS but event_name is None"
        assert s.event_name  # non-empty


def test_non_progress_entries_event_name_is_none() -> None:
    # We don't strictly require this, but the current registry follows it;
    # if we ever change the policy, update this test.
    for s in LINK_SYMBOLS:
        if s.role is not LinkRole.PROGRESS:
            assert s.event_name is None, (
                f"{s.key} has role {s.role} but event_name={s.event_name!r}"
            )


# ---------------------------------------------------------------------------
# symbols_for_role
# ---------------------------------------------------------------------------


def test_symbols_for_role_bridge_contains_exchange_bytes() -> None:
    bridge = symbols_for_role(LinkRole.BRIDGE)
    assert len(bridge) >= 1
    keys = {s.key for s in bridge}
    assert "exchange_bytes" in keys


def test_symbols_for_role_returns_only_matching_role() -> None:
    for role in LinkRole:
        for s in symbols_for_role(role):
            assert s.role is role


# ---------------------------------------------------------------------------
# resolve_link_symbols
# ---------------------------------------------------------------------------


def _full_symbol_table() -> FakeSymbolTable:
    """A table that resolves every label for every version."""
    mapping: dict[str, tuple[int, int]] = {}
    addr = 0x4000
    for link_sym in LINK_SYMBOLS:
        for label in link_sym.per_version.values():
            # Deterministic, distinct (bank, addr) per label.
            mapping.setdefault(label, (0, addr))
            addr += 1
    return FakeSymbolTable(mapping)


def test_resolve_returns_dict_of_resolvable_keys() -> None:
    table = _full_symbol_table()
    resolved = resolve_link_symbols(table, "red")

    # Every registry key should be present because the fake has every label.
    assert set(resolved.keys()) == {s.key for s in LINK_SYMBOLS}

    # Every value is a (bank, addr) int tuple.
    for key, (bank, addr) in resolved.items():
        assert isinstance(bank, int)
        assert isinstance(addr, int)


def test_resolve_works_for_all_three_versions() -> None:
    table = _full_symbol_table()
    for version in ("red", "blue", "yellow"):
        resolved = resolve_link_symbols(table, version)
        assert "exchange_bytes" in resolved
        assert "handshake" in resolved


def test_resolve_skips_optional_labels_silently() -> None:
    # Only the two required labels are present in the table.
    required_labels = {
        s.per_version["red"] for s in LINK_SYMBOLS if s.required
    }
    mapping = {lbl: (0, 0x5000 + i) for i, lbl in enumerate(required_labels)}
    table = FakeSymbolTable(mapping)

    resolved = resolve_link_symbols(table, "red")

    required_keys = {s.key for s in LINK_SYMBOLS if s.required}
    assert set(resolved.keys()) == required_keys
    # Optional keys must not appear.
    for s in LINK_SYMBOLS:
        if not s.required:
            assert s.key not in resolved


def test_resolve_raises_when_required_label_missing() -> None:
    # Empty table → required labels cannot resolve.
    table = FakeSymbolTable({})

    with pytest.raises(LookupError) as exc:
        resolve_link_symbols(table, "red")

    msg = str(exc.value)
    # Message names at least one of the required keys so operators can fix it.
    assert "exchange_bytes" in msg or "handshake" in msg
    assert "red" in msg


def test_resolve_rejects_unknown_version() -> None:
    table = _full_symbol_table()
    with pytest.raises(ValueError):
        resolve_link_symbols(table, "green")  # type: ignore[arg-type]


def test_link_symbol_is_frozen() -> None:
    s = LINK_SYMBOLS[0]
    with pytest.raises((AttributeError, Exception)):
        s.key = "mutated"  # type: ignore[misc]


def test_required_set_matches_spec() -> None:
    required_keys = {s.key for s in LINK_SYMBOLS if s.required}
    assert required_keys == {"exchange_bytes", "handshake"}


def test_link_symbol_dataclass_shape() -> None:
    # Sanity: LinkSymbol is what we expect — defends against accidental edits.
    sample = LinkSymbol(
        key="k",
        role=LinkRole.BRIDGE,
        event_name=None,
        per_version={"red": "X", "blue": "X", "yellow": "X"},
        required=True,
    )
    assert sample.key == "k"
    assert sample.role is LinkRole.BRIDGE
