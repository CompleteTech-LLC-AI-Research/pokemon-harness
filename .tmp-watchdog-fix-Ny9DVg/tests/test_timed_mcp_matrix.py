"""Asset-free declaration regressions; no ROM or MCP process is executed."""

from __future__ import annotations

import pytest

from scripts import tcp_link_matrix as matrix

_VERSIONS = ("red", "blue", "yellow")
_PROFILES = ("red_color", "blue_color", "yellow")
_SMOKE_PREFIX = "tests/test_mcp_timed_rom.py::test_timed_rom_stdio_pair"
_EXPECTED_SMOKE = frozenset(
    f"{_SMOKE_PREFIX}[{listener}-listen-{connector}-connect]"
    for listener in _PROFILES
    for connector in _PROFILES
)
_LOCAL_PAIRS = frozenset(
    "tests/test_pyboy_link_session_roms.py::"
    f"test_pair_reaches_link_menu_via_pyboy_link_session[{left}-{right}]"
    for left in _VERSIONS
    for right in _VERSIONS
)
_REMOTE_PAIRS = frozenset(
    "tests/test_link_integration_remote.py::"
    f"test_remote_handshake_writes_status_on_both_sides[{left}-{right}]"
    for left in _VERSIONS
    for right in _VERSIONS
)
_LOCAL_VARIANTS = frozenset(
    "tests/test_pyboy_link_session_roms.py::"
    f"test_same_version_variants_reach_link_menu[{version}-{left}-x-{right}]"
    for version, variants in (
        ("red", ("vanilla", "color")),
        ("blue", ("vanilla", "color")),
        ("yellow", ("cgb",)),
    )
    for left in variants
    for right in variants
)
_LEGACY_REMOTE_SMOKE = frozenset(
    {
        "tests/test_mcp_real_link.py::test_mcp_remote_link_attaches_native_serial_backend",
        (
            "tests/test_pyboy_link_session_subprocess.py::"
            "test_subprocess_pair_reaches_link_menu_over_tcp"
        ),
    }
)


def _strict_nodes(local_test, special_test, remote_test):
    return (
        frozenset(
            f"tests/test_pyboy_link_session_roms.py::{local_test}[{left}-{right}]"
            for left in _VERSIONS
            for right in _VERSIONS
        )
        | {
            f"tests/test_pyboy_link_session_roms.py::{special_test}",
        }
        | frozenset(
            "tests/test_pyboy_link_session_subprocess.py::"
            f"{remote_test}[{listener}-listen-{connector}-connect]"
            for listener in _PROFILES
            for connector in _PROFILES
        )
    )


_TRADE = _strict_nodes(
    "test_pair_completes_trade_end_to_end",
    "test_red_yellow_trade_swaps_real_party_records",
    "test_subprocess_pair_completes_trade_over_tcp",
)
_BATTLE = _strict_nodes(
    "test_pair_completes_battle_turn",
    "test_red_yellow_battle_turn_is_resolved",
    "test_subprocess_pair_resolves_battle_turn_over_tcp",
)
_COMPLETE_COLLECTION = (
    _LOCAL_PAIRS
    | _LOCAL_VARIANTS
    | _REMOTE_PAIRS
    | _LEGACY_REMOTE_SMOKE
    | _TRADE
    | _BATTLE
    | _EXPECTED_SMOKE
)


def test_timed_mcp_smoke_constant_has_exact_nine_canonical_ordered_nodeids():
    assert isinstance(matrix.TIMED_MCP_SMOKE_NODEIDS, frozenset)
    assert len(matrix.TIMED_MCP_SMOKE_NODEIDS) == 9
    assert matrix.TIMED_MCP_SMOKE_NODEIDS == _EXPECTED_SMOKE


def test_timed_mcp_smoke_remote_union_preserves_existing_required_sets():
    assert matrix.LOCAL_VERSION_PAIR_NODEIDS == _LOCAL_PAIRS
    assert matrix.LOCAL_VARIANT_NODEIDS == _LOCAL_VARIANTS
    assert matrix.REMOTE_VERSION_PAIR_NODEIDS == _REMOTE_PAIRS
    assert matrix.REMOTE_REVERSED_ROLE_NODEIDS == frozenset(
        nodeid
        for nodeid in _REMOTE_PAIRS
        if not any(nodeid.endswith(f"[{version}-{version}]") for version in _VERSIONS)
    )
    assert matrix.STRICT_TRADE_NODEIDS == _TRADE
    assert matrix.STRICT_BATTLE_NODEIDS == _BATTLE
    assert matrix.STRICT_ACCEPTANCE_NODEIDS == {"trade": _TRADE, "battle": _BATTLE}
    assert matrix.required_matrix_nodeids() == {
        "local": _LOCAL_PAIRS | _LOCAL_VARIANTS,
        "remote": _REMOTE_PAIRS | _LEGACY_REMOTE_SMOKE | _EXPECTED_SMOKE,
        "trade": _TRADE,
        "battle": _BATTLE,
    }
    assert _EXPECTED_SMOKE.isdisjoint(_TRADE | _BATTLE | _LOCAL_PAIRS | _REMOTE_PAIRS)


def test_complete_timed_mcp_smoke_audit_is_structural_only():
    audit = matrix.audit_collection(_COMPLETE_COLLECTION)

    assert audit["structural_pass"] is True
    assert audit["groups"]["timed-mcp-smoke"] == {
        "expected": 9,
        "present": 9,
        "missing": (),
    }
    assert all(not group["missing"] for group in audit["groups"].values())
    assert audit["acceptance_matrix_complete"] is True
    assert audit["runtime"] == "not-run"


@pytest.mark.parametrize("missing", sorted(_EXPECTED_SMOKE))
def test_timed_mcp_smoke_audit_rejects_each_omitted_canonical_row(missing):
    audit = matrix.audit_collection(_COMPLETE_COLLECTION - {missing})

    assert audit["structural_pass"] is False
    assert audit["groups"]["timed-mcp-smoke"] == {
        "expected": 9,
        "present": 8,
        "missing": (missing,),
    }
    assert all(
        not group["missing"] for name, group in audit["groups"].items() if name != "timed-mcp-smoke"
    )
    assert audit["collection_errors"] == ()
    assert audit["collection_skips"] == ()
    assert audit["duplicate_nodeids"] == ()
    assert audit["acceptance_matrix_complete"] is True
    assert audit["runtime"] == "not-run"


def test_timed_mcp_smoke_audit_rejects_entire_missing_smoke_matrix():
    audit = matrix.audit_collection(_COMPLETE_COLLECTION - _EXPECTED_SMOKE)

    assert audit["structural_pass"] is False
    assert audit["groups"]["timed-mcp-smoke"] == {
        "expected": 9,
        "present": 0,
        "missing": tuple(sorted(_EXPECTED_SMOKE)),
    }
    assert audit["runtime"] == "not-run"
