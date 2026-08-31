"""Deterministic pytest tier classification for the production gate.

The project deliberately keeps the ROM-dependent tests in the normal pytest
tree so developers can run them directly.  This module gives the production
gate a stable, reviewable classification without relying on test names alone
for the broad ROM boundary.  The small set of acceptance names is explicit so
that a new test cannot silently move into an optional tier.

Every collected test module must be listed below.  Failing collection for an
unlisted module is intentional: silently classifying a newly added ROM test as
``unit`` would let the production gate report green without exercising it.
"""

from __future__ import annotations

from pathlib import Path

MARKERS = (
    "unit",
    "real_rom",
    "local_link",
    "remote_link",
    "mcp_stdio",
    "acceptance",
    "trade",
    "trade_acceptance",
    "battle",
    "battle_acceptance",
    "timing_sensitive",
)

# These modules instantiate PyBoy, load a ROM/symbol file, or spawn a
# real-ROM peer.  Everything outside this allowlist is ROM-free by default.
REAL_ROM_MODULES = frozenset(
    {
        "test_golden_paths.py",
        "test_link_integration.py",
        "test_link_integration_remote.py",
        "test_mcp_real_link.py",
        "test_link_symbols_real_roms.py",
        "test_mcp_stdio_integration.py",
        "test_pyboy_link_session_roms.py",
        "test_pyboy_link_session_subprocess.py",
    }
)

# Keep the ROM-free side explicit as well.  A new ``test_*.py`` file must be
# reviewed and added to exactly one of these sets before it can enter pytest's
# collection path.  This is deliberately a little repetitive: the manifest is
# a guard against a test silently falling into the wrong production tier.
UNIT_MODULES = frozenset(
    {
        "test_agent_sync.py",
        "test_config.py",
        "test_events.py",
        "test_game_state.py",
        "test_link_orchestrator.py",
        "test_link_pair.py",
        "test_link_protocol.py",
        "test_link_serial_bridge.py",
        "test_link_symbols.py",
        "test_link_transport.py",
        "test_mcp_server.py",
        "test_network_backend.py",
        "test_production_gate.py",
        "test_pyboy_link_session.py",
        "test_remote_endpoint.py",
        "test_runtime_packaging.py",
        "test_serial_coordinator.py",
        "test_serial_core.py",
        "test_serial_link.py",
        "test_session.py",
        "test_state_bag.py",
        "test_state_battle.py",
        "test_state_menu.py",
        "test_state_overworld.py",
        "test_state_party.py",
        "test_state_progress.py",
        "test_state_status.py",
        "test_state_text.py",
        "test_symbol_loader.py",
    }
)

KNOWN_TEST_MODULES = REAL_ROM_MODULES | UNIT_MODULES

LOCAL_LINK_MODULES = frozenset(
    {
        "test_link_integration.py",
        "test_pyboy_link_session_roms.py",
    }
)

REMOTE_LINK_MODULES = frozenset(
    {
        "test_link_integration_remote.py",
        "test_mcp_real_link.py",
        "test_pyboy_link_session_subprocess.py",
    }
)

MCP_STDIO_MODULES = frozenset({"test_mcp_stdio_integration.py"})

# The broad trade set keeps ROM milestones visible in diagnostics. The strict
# acceptance set below is deliberately narrower and is what the production
# gate uses for the required trade tier.
TRADE_TESTS = frozenset(
    {
        ("test_link_integration.py", "test_link_trade_roundtrip"),
        ("test_link_integration_remote.py", "test_remote_trade_reaches_link_menu_via_tcp"),
        (
            "test_link_integration_remote.py",
            "test_remote_rpc_kinds_flow_over_tcp_reaching_link_menu",
        ),
        ("test_link_integration_remote.py", "test_remote_rpc_flow_past_link_menu_over_tcp"),
        (
            "test_link_integration_remote.py",
            "test_remote_menu_vote_converges_and_warps_to_trade_center",
        ),
        (
            "test_link_integration_remote.py",
            "test_remote_exchange_bytes_fires_in_trade_center_blue_blue",
        ),
        (
            "test_link_integration_remote.py",
            "test_remote_agent_sync_coordinates_link_menu_vote_blue_blue",
        ),
        ("test_pyboy_link_session_roms.py", "test_pair_completes_trade_end_to_end"),
        (
            "test_pyboy_link_session_roms.py",
            "test_red_yellow_trade_swaps_real_party_records",
        ),
        ("test_pyboy_link_session_roms.py", "test_yellow_pair_warps_to_trade_center"),
        (
            "test_pyboy_link_session_subprocess.py",
            "test_subprocess_pair_completes_trade_over_tcp",
        ),
    }
)

BATTLE_TESTS = frozenset(
    {
        ("test_pyboy_link_session_roms.py", "test_yellow_pair_warps_to_colosseum"),
        ("test_pyboy_link_session_roms.py", "test_yellow_pair_starts_link_battle"),
        ("test_pyboy_link_session_roms.py", "test_pair_completes_battle_turn"),
        (
            "test_pyboy_link_session_roms.py",
            "test_red_yellow_battle_turn_is_resolved",
        ),
        (
            "test_pyboy_link_session_subprocess.py",
            "test_subprocess_pair_resolves_battle_turn_over_tcp",
        ),
    }
)

# The broader TRADE_TESTS/BATTLE_TESTS sets remain useful diagnostics, but
# the production gate must select only tests that assert the resulting game
# state rather than a hook or menu milestone.
TRADE_ACCEPTANCE_TESTS = frozenset(
    {
        (
            "test_pyboy_link_session_roms.py",
            "test_red_yellow_trade_swaps_real_party_records",
        ),
        (
            "test_pyboy_link_session_subprocess.py",
            "test_subprocess_pair_completes_trade_over_tcp",
        ),
    }
)

BATTLE_ACCEPTANCE_TESTS = frozenset(
    {
        (
            "test_pyboy_link_session_roms.py",
            "test_red_yellow_battle_turn_is_resolved",
        ),
        (
            "test_pyboy_link_session_subprocess.py",
            "test_subprocess_pair_resolves_battle_turn_over_tcp",
        ),
    }
)

# These are the ordered, real-ROM matrix rows that the production gate must
# never silently lose.  For the remote rows the first version is the listener
# (internal clock) and the second is the connector (external clock); therefore
# both ``red-blue`` and ``blue-red`` are required cases rather than aliases.
SUPPORTED_VERSIONS = ("red", "blue", "yellow")
REMOTE_VERSION_PAIR_NODEIDS = frozenset(
    "tests/test_link_integration_remote.py::"
    f"test_remote_handshake_writes_status_on_both_sides[{listener}-{connector}]"
    for listener in SUPPORTED_VERSIONS
    for connector in SUPPORTED_VERSIONS
)
LOCAL_VERSION_PAIR_NODEIDS = frozenset(
    "tests/test_pyboy_link_session_roms.py::"
    f"test_pair_reaches_link_menu_via_pyboy_link_session[{version_a}-{version_b}]"
    for version_a in SUPPORTED_VERSIONS
    for version_b in SUPPORTED_VERSIONS
)

_ROM_VARIANTS = (
    ("red", ("vanilla", "color")),
    ("blue", ("vanilla", "color")),
    ("yellow", ("cgb",)),
)
LOCAL_VARIANT_NODEIDS = frozenset(
    "tests/test_pyboy_link_session_roms.py::"
    f"test_same_version_variants_reach_link_menu[{version}-{variant_a}-x-{variant_b}]"
    for version, variants in _ROM_VARIANTS
    for variant_a in variants
    for variant_b in variants
)

# A positive aggregate count is not enough to prove matrix coverage: pytest
# deselection or a removed parametrization can still leave one passing case.
# Keep these exact node IDs separate from the broader diagnostic marker sets.
TIER_REQUIRED_NODEIDS = {
    "local": LOCAL_VERSION_PAIR_NODEIDS | LOCAL_VARIANT_NODEIDS,
    "remote": REMOTE_VERSION_PAIR_NODEIDS
    | frozenset(
        {
            "tests/test_mcp_real_link.py::test_mcp_remote_link_attaches_native_serial_backend",
            "tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_reaches_link_menu_over_tcp",
        }
    ),
    "trade": frozenset(
        {
            "tests/test_pyboy_link_session_roms.py::test_red_yellow_trade_swaps_real_party_records",
            "tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_completes_trade_over_tcp",
        }
    ),
    "battle": frozenset(
        {
            "tests/test_pyboy_link_session_roms.py::test_red_yellow_battle_turn_is_resolved",
            "tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_resolves_battle_turn_over_tcp",
        }
    ),
}

# The gate uses these keys to prove that each strict acceptance tier still
# contains every required end-to-end assertion.  A positive aggregate count is
# not enough: one surviving test could otherwise mask deletion/deselection of
# the other acceptance case.
TIER_REQUIRED_TESTS = {
    "trade": TRADE_ACCEPTANCE_TESTS,
    "battle": BATTLE_ACCEPTANCE_TESTS,
}

# These tests exercise socket/thread scheduling.  The name-based fallback is
# intentional for the late-rearm regression added by the link reliability
# lane, whose exact name is owned by that lane.
TIMING_SENSITIVE_TESTS = frozenset(
    {
        ("test_network_backend.py", "test_on_edge_sends_REQ_and_waits_for_RESP"),
        (
            "test_network_backend.py",
            "test_two_serialcores_exchange_byte_via_network_backend",
        ),
        ("test_network_backend.py", "test_multiple_bytes_exchange"),
        (
            "test_network_backend.py",
            "test_listen_and_connect_over_loopback_exchange_byte",
        ),
        ("test_network_backend.py", "test_sync_with_peer_rendezvous"),
    }
)


def classify_test(path: str | Path, test_name: str) -> frozenset[str]:
    """Return all production-gate markers for one collected test.

    ``test_name`` should be pytest's ``originalname`` so parametrized suffixes
    do not alter tier membership.
    """

    filename = Path(path).name
    marks: set[str] = set()

    if filename in REAL_ROM_MODULES:
        marks.add("real_rom")
    elif filename in UNIT_MODULES:
        marks.add("unit")
    else:
        raise ValueError(
            f"test module {filename!r} is not classified; add it to "
            "UNIT_MODULES or REAL_ROM_MODULES before collection"
        )

    if filename in LOCAL_LINK_MODULES:
        marks.add("local_link")
    if filename in REMOTE_LINK_MODULES:
        marks.add("remote_link")
    if filename in MCP_STDIO_MODULES:
        marks.add("mcp_stdio")

    test_key = (filename, test_name)
    if test_key in TRADE_TESTS:
        marks.update(("acceptance", "trade"))
    if test_key in TRADE_ACCEPTANCE_TESTS:
        marks.add("trade_acceptance")
    if test_key in BATTLE_TESTS:
        marks.update(("acceptance", "battle"))
    if test_key in BATTLE_ACCEPTANCE_TESTS:
        marks.add("battle_acceptance")
    if test_key in TIMING_SENSITIVE_TESTS:
        marks.add("timing_sensitive")

    # A future late-rearm test is timing-sensitive by definition.  Keep this
    # narrow to avoid repeating every expensive remote test five times.
    lowered = test_name.lower()
    if "rearm" in lowered or "timing_sensitive" in lowered:
        marks.add("timing_sensitive")

    return frozenset(marks)


__all__ = [
    "BATTLE_ACCEPTANCE_TESTS",
    "BATTLE_TESTS",
    "KNOWN_TEST_MODULES",
    "LOCAL_LINK_MODULES",
    "LOCAL_VARIANT_NODEIDS",
    "LOCAL_VERSION_PAIR_NODEIDS",
    "MARKERS",
    "MCP_STDIO_MODULES",
    "REAL_ROM_MODULES",
    "REMOTE_LINK_MODULES",
    "REMOTE_VERSION_PAIR_NODEIDS",
    "SUPPORTED_VERSIONS",
    "TIER_REQUIRED_NODEIDS",
    "TIER_REQUIRED_TESTS",
    "TIMING_SENSITIVE_TESTS",
    "TRADE_ACCEPTANCE_TESTS",
    "TRADE_TESTS",
    "UNIT_MODULES",
    "classify_test",
]
