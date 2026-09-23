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

import runpy
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
        "test_rom_boot_matrix.py",
        "test_link_integration.py",
        "test_link_integration_remote.py",
        "test_mcp_real_link.py",
        "test_link_symbols_real_roms.py",
        "test_mcp_stdio_integration.py",
        "test_mcp_timed_rom.py",
        "test_mcp_trade_records_rom.py",
        "test_battle_healing_items_rom.py",
        "test_pyboy_link_session_roms.py",
        "test_pyboy_link_session_subprocess.py",
        "test_rom_boot.py",
    }
)

# Keep the ROM-free side explicit as well.  A new ``test_*.py`` file must be
# reviewed and added to exactly one of these sets before it can enter pytest's
# collection path.  This is deliberately a little repetitive: the manifest is
# a guard against a test silently falling into the wrong production tier.
UNIT_MODULES = frozenset(
    {
        "test_agent_sync.py",
        "test_battle_coverage_accounting.py",
        "test_battle_coverage_catalog.py",
        "test_battle_coverage_gate_assets.py",
        "test_battle_coverage_identity.py",
        "test_battle_coverage_mechanics.py",
        "test_battle_healing_fixture_producer.py",
        "test_battle_healing_registration.py",
        "test_battle_item_evidence_inventory.py",
        "test_battle_item_evidence_medicine.py",
        "test_battle_item_evidence_targets.py",
        "test_battle_item_evidence_timeline.py",
        "test_battle_medicine_boundary_matrix.py",
        "test_battle_medicine_oracle.py",
        "test_battle_scenario_fixtures.py",
        "test_battle_turn_evidence.py",
        "test_config.py",
        "test_coordinator_detach_callback.py",
        "test_diagnose_pair_trade.py",
        "test_diagnostic_script_safety.py",
        "test_emulator_ownership.py",
        "test_pair_checkpoints.py",
        "test_party_record_audit.py",
        "test_peer_frame_shutdown.py",
        "test_network_detach_references.py",
        "test_network_provider_locking.py",
        "test_ownership_core.py",
        "test_cpu_instruction_counter.py",
        "test_emulated_time_budget.py",
        "test_emulated_time_episode.py",
        "test_emulated_time_failure.py",
        "test_emulated_time_held_delivery.py",
        "test_emulated_time_lateness.py",
        "test_emulated_time_segments.py",
        "test_events.py",
        "test_execution_adapter.py",
        "test_fixture_provenance.py",
        "test_game_state.py",
        "test_gate_early_smoke.py",
        "test_gate_failure_retention.py",
        "test_link_orchestrator.py",
        "test_link_pair.py",
        "test_link_protocol.py",
        "test_link_serial_bridge.py",
        "test_link_symbols.py",
        "test_local_acceptance_driver_contract.py",
        "test_live_trade_demo_contract.py",
        "test_local_scheduler_candidate.py",
        "test_mcp_lifecycle_hardening.py",
        "test_mcp_local_locking.py",
        "test_mcp_local_runtime_contract.py",
        "test_mcp_main_cleanup.py",
        "test_mcp_mode_exclusion.py",
        "test_mcp_remote_runtime_contract.py",
        "test_mcp_runtime_errors.py",
        "test_link_transport.py",
        "test_mcp_server_endpoint.py",
        "test_mcp_server_endpoint_cleanup.py",
        "test_mcp_server_handlers.py",
        "test_mcp_server_link.py",
        "test_mcp_server_session.py",
        "test_mcp_timed_remote_cached_failure.py",
        "test_mcp_timed_remote_cancellation.py",
        "test_mcp_timed_remote_owner.py",
        "test_mcp_timed_stdio.py",
        "test_native_execution_governor.py",
        "test_native_hook_exceptions.py",
        "test_network_backend_dispatch.py",
        "test_network_backend_rearm.py",
        "test_network_backend_serial_transcript.py",
        "test_network_backend_transport.py",
        "test_network_backend_wire_idle.py",
        "test_network_backend_shutdown.py",
        "test_network_byte_progress.py",
        "test_network_cpu_owner.py",
        "test_network_frame_batching.py",
        "test_pre_linkmenu_observation.py",
        "test_network_owner_execution.py",
        "test_network_public_backpressure.py",
        "test_network_write_deadlines.py",
        "test_owner_boundary_runtime.py",
        "test_physical_clock_runtime.py",
        "test_probe_timed_rom_pair.py",
        "test_probe_owner_phases.py",
        "test_probe_timed_battle_pair.py",
        "test_probe_timed_trade_pair_cli.py",
        "test_probe_timed_trade_pair_driver.py",
        "test_probe_timed_trade_pair_evidence.py",
        "test_probe_timed_trade_pair_proof.py",
        "test_production_gate_diagnostics.py",
        "test_production_gate_matrix_manifest.py",
        "test_production_gate_report_loader.py",
        "test_production_gate_run_tier_failures.py",
        "test_production_gate_strict_matrix.py",
        "test_pyboy_link_imports.py",
        "test_pyboy_link_session.py",
        "test_remote_endpoint.py",
        "test_runtime_packaging_bootstrap.py",
        "test_runtime_packaging_build_contract.py",
        "test_runtime_packaging_dependency_pins.py",
        "test_runtime_packaging_hygiene.py",
        "test_local_ci_policy.py",
        "test_scheduler_lcd_phase.py",
        "test_scheduler_physical_time.py",
        "test_serial_coordinator.py",
        "test_serial_core.py",
        "test_serial_link.py",
        "test_serial_backend_boundary.py",
        "test_serial_owner_claim.py",
        "test_serial_owner_pump.py",
        "test_serial_owner_pump_review.py",
        "test_serial_ownership.py",
        "test_serial_rearm_phase.py",
        "test_serial_state_deadlines.py",
        "test_session.py",
        "test_session_close_concurrency.py",
        "test_session_link_ownership.py",
        "test_session_timed_execution.py",
        "test_speed_state_restore.py",
        "test_state_bag.py",
        "test_state_battle.py",
        "test_state_menu.py",
        "test_state_overworld.py",
        "test_state_party.py",
        "test_state_progress.py",
        "test_state_status.py",
        "test_state_text.py",
        "test_stepping_loop_profile.py",
        "test_symbol_loader.py",
        "test_timed_link_session_completeness.py",
        "test_timed_link_session_control.py",
        "test_timed_link_session_lifecycle.py",
        "test_timed_link_session_v3.py",
        "test_timed_battle_probe_contract.py",
        "test_timed_battle_probe_action_economy.py",
        "test_timed_battle_probe_admission.py",
        "test_timed_battle_probe_ownership.py",
        "test_timed_battle_probe_terminal.py",
        "test_timed_input_observation.py",
        "test_timed_menu_probe.py",
        "test_timed_mcp_matrix.py",
        "test_timed_menu_milestones.py",
        "test_timed_remote.py",
        "test_timed_remote_facade.py",
        "test_timed_wire_admission.py",
        "test_timed_wire_codec.py",
        "test_timed_wire_progress.py",
        "test_timed_wire_transport.py",
        "test_timed_wire_wirecontrol.py",
        "test_timed_wire_batch.py",
        "test_timed_trade_probe.py",
    }
)

# Reviewed opt-in modules.  These tests need assets the gate environment does
# not provision - a local pinned upstream checkout, for example - and skip when
# it is absent.  A required tier fails closed on any skip, so these modules
# carry no tier marker and are never selected by a tier marker expression; run
# them explicitly by path, as their module docstrings document.  The manifest
# entry is still required: an unlisted module keeps failing collection.
OPT_IN_MODULES = frozenset(
    {
        "test_battle_medicine_source_conformance.py",
    }
)

KNOWN_TEST_MODULES = REAL_ROM_MODULES | UNIT_MODULES | OPT_IN_MODULES

# Reviewed ROM-free exceptions in mixed modules. Only actual test functions
# belong here; unlisted future functions retain the module's real-ROM tier.
ROM_FREE_TESTS = frozenset(
    {
        ("test_pyboy_link_session_subprocess.py", name)
        for name in (
            "test_link_menu_history_preserves_first_samples_across_buffer_reuse",
            "test_link_menu_history_validates_call_and_bank",
            "test_link_menu_history_rejects_call_to_wrong_target",
            "test_link_menu_history_additive_result_compatibility",
            "test_link_menu_history_reports_missing_symbols_and_registration_errors",
            "test_link_menu_history_bounds_callback_errors_and_keeps_partial_samples",
            "test_link_menu_history_decisive_directions_ignore_stale_second_bytes",
            "test_link_menu_history_received_candidate_follows_rom_order",
            "test_link_menu_history_failure_summary_survives_large_result_tail",
            "test_link_menu_history_missing_call_symbols_remains_observable",
            "test_link_menu_history_rejects_post_call_outside_bank",
            "test_peer_trace_watchdog",
            "test_setup_handshake_failure_returns_bounded_non_success_sentinels",
            "test_collect_pair_rejects_missing_or_partial_required_rows",
            "test_strict_acceptance_rejects_link_menu_only_result",
            "test_strict_acceptance_rejects_missing_native_edge_req",
            "test_peer_shutdown_drains_live_serial_work_before_starting_teardown_marker",
            "test_peer_shutdown_protocol_propagates_backend_errors",
            "test_peer_shutdown_ready_marker_times_out_without_post_marker_ticks",
            "test_link_menu_finish_starts_teardown_only_through_draining_helper",
            "test_hold_at_sync_boundary_does_not_tick_past_ready_marker",
            "test_hold_at_sync_boundary_ticks_timed_rom_phase",
            "test_collect_pair_enforces_hard_deadline_without_waiting_for_peers",
            "test_partial_peer_sentinel_is_fatal_before_gameplay_assertions",
        )
    }
    | {
        (
            "test_mcp_timed_rom.py",
            "test_rom_client_load_state_timeout_redacts_data",
        ),
    }
    | {
        # This module drives a real ROM for its capture assertions, but its
        # manifest cross-check, its producer guards, and its turn-timeline
        # negative control touch no asset and have no skip path, so they belong
        # in the always-selected unit tier rather than being masked by the
        # module's `real_rom` classification.  The two asset-consuming tests stay
        # in `real_rom`: they skip when the operator fixture or the BYO ROM/SYM
        # is absent, and the unit tier fails closed on any skip.
        (
            "test_battle_healing_items_rom.py",
            "test_release_evidence_names_the_pinned_assets_and_producer",
        ),
        (
            "test_battle_healing_items_rom.py",
            "test_producer_refuses_existing_output_and_unpinned_assets",
        ),
        (
            "test_battle_healing_items_rom.py",
            "test_turn_evidence_is_required_rather_than_supplied",
        ),
    }
)

LOCAL_LINK_MODULES = frozenset(
    {
        "test_link_integration.py",
        "test_mcp_trade_records_rom.py",
        "test_pyboy_link_session_roms.py",
    }
)

REMOTE_LINK_MODULES = frozenset(
    {
        "test_link_integration_remote.py",
        "test_mcp_real_link.py",
        "test_mcp_timed_rom.py",
        "test_pyboy_link_session_subprocess.py",
    }
)

MCP_STDIO_MODULES = frozenset(
    {
        "test_mcp_stdio_integration.py",
        "test_mcp_timed_rom.py",
        "test_mcp_trade_records_rom.py",
    }
)

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
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_exchanges_party_records",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_exchanges_party_records_over_tcp",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_exchanges_multi_member_slot_records",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_cancel_before_commitment_keeps_records",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_eof_during_setup_exits_cleanly",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_eof_during_active_trade_exits_cleanly",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_eof_after_commitment_exits_cleanly",
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
            "test_pair_completes_trade_end_to_end",
        ),
        (
            "test_pyboy_link_session_roms.py",
            "test_red_yellow_trade_swaps_real_party_records",
        ),
        (
            "test_pyboy_link_session_subprocess.py",
            "test_subprocess_pair_completes_trade_over_tcp",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_exchanges_party_records",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_exchanges_party_records_over_tcp",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_exchanges_multi_member_slot_records",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_cancel_before_commitment_keeps_records",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_eof_during_setup_exits_cleanly",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_eof_during_active_trade_exits_cleanly",
        ),
        (
            "test_mcp_trade_records_rom.py",
            "test_real_rom_mcp_trade_eof_after_commitment_exits_cleanly",
        ),
    }
)

BATTLE_ACCEPTANCE_TESTS = frozenset(
    {
        (
            "test_pyboy_link_session_roms.py",
            "test_pair_completes_battle_turn",
        ),
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
# never silently lose.  Keep their construction in the collection-only
# auditor as the single source of truth so the executable audit and the gate
# manifest cannot drift apart. The first remote version is the listener and
# the second is the connector; their defaults are internal-clock master and
# external-clock slave, respectively. Native cross-family Yellow pairs may
# negotiate the non-Yellow endpoint as the initial master, so ordered cases
# remain distinct transport configurations.
_MATRIX = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "tcp_link_matrix.py")
)
SUPPORTED_VERSIONS = _MATRIX["SUPPORTED_VERSIONS"]
REMOTE_VERSION_PAIR_NODEIDS = _MATRIX["REMOTE_VERSION_PAIR_NODEIDS"]
LOCAL_VERSION_PAIR_NODEIDS = _MATRIX["LOCAL_VERSION_PAIR_NODEIDS"]
LOCAL_VARIANT_NODEIDS = _MATRIX["LOCAL_VARIANT_NODEIDS"]

# A positive aggregate count is not enough to prove matrix coverage: pytest
# deselection or a removed parametrization can still leave one passing case.
# Keep these exact node IDs separate from the broader diagnostic marker sets.
CANONICAL_ROM_BOOT_NODEIDS = frozenset(
    f"tests/test_rom_boot.py::test_canonical_rom_boot_state_roundtrip[{variant}]"
    for variant in ("red-color", "blue-color", "yellow")
)

TIER_REQUIRED_NODEIDS = {
    **_MATRIX["required_matrix_nodeids"](),
}
TIER_REQUIRED_NODEIDS["local"] |= CANONICAL_ROM_BOOT_NODEIDS

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
        (
            "test_probe_owner_phases.py",
            "test_spawn_serializes_and_reports_exact_owner_phase_schema",
        ),
        (
            "test_timed_wire_batch.py",
            "test_batch_lock_contention_failure_is_nonterminal",
        ),
        (
            "test_timed_wire_batch.py",
            "test_batch_writer_barrier_excludes_interleaving",
        ),
        (
            "test_timed_wire_batch.py",
            "test_batch_real_receive_order_and_sequence",
        ),
        (
            "test_timed_wire_batch.py",
            "test_batch_real_receiver_rejects_sequence_replay",
        ),
        (
            "test_timed_wire_batch.py",
            "test_batch_real_receiver_preserves_queue_bound",
        ),
        (
            "test_timed_menu_milestones.py",
            "test_authored_cartridge_actual_helper_is_non_mutating",
        ),
        (
            "test_mcp_timed_stdio.py",
            "test_authored_timed_stdio_pair_frames_and_cleanup",
        ),
        (
            "test_mcp_timed_stdio.py",
            "test_authored_timed_stdio_expected_peer_mismatch",
        ),
        (
            "test_timed_input_observation.py",
            "test_actual_foreign_thread_cannot_observe",
        ),
        (
            "test_mcp_timed_remote_cached_failure.py",
            "test_cached_status_is_immutable_and_available_while_busy",
        ),
        (
            "test_mcp_timed_remote_cached_failure.py",
            "test_queue_bound_and_cancelled_request_never_runs",
        ),
        (
            "test_mcp_timed_remote_cached_failure.py",
            "test_expired_queued_request_never_executes_later",
        ),
        (
            "test_mcp_timed_remote_cached_failure.py",
            "test_queued_cancel_preserves_active_real_epoch",
        ),
        (
            "test_mcp_timed_remote_owner.py",
            "test_real_partial_progress_active_interrupt_is_terminal",
        ),
        (
            "test_mcp_timed_remote_owner.py",
            "test_async_cancel_keeps_event_loop_responsive_during_endpoint_cleanup",
        ),
        (
            "test_mcp_timed_remote_owner.py",
            "test_close_retains_busy_owner_until_retry_and_closes_session_on_owner",
        ),
        (
            "test_mcp_timed_remote_owner.py",
            "test_mcp_cached_status_tool_and_resource_return_while_owner_blocked",
        ),
        (
            "test_mcp_timed_remote_cancellation.py",
            "test_stored_protocol_error_precedes_active_caller_cancellation",
        ),
        (
            "test_mcp_timed_remote_cached_failure.py",
            "test_cached_failure_survives_cancel_and_cleanup",
        ),
        (
            "test_mcp_timed_remote_cached_failure.py",
            "test_cached_failure_first_per_epoch_and_attributed_across_reconnect",
        ),
        (
            "test_mcp_timed_remote_cached_failure.py",
            "test_cached_failure_mcp_status_never_reads_native_while_blocked",
        ),
        (
            "test_mcp_timed_remote_cached_failure.py",
            "test_cached_failure_first_observed_at_cleanup_before_unbind",
        ),
        (
            "test_mcp_timed_remote_cached_failure.py",
            "test_cached_failure_attach_finally_preserves_setup_exception",
        ),
        (
            "test_mcp_timed_remote_cancellation.py",
            "test_blocked_cancellation_keeps_queue_and_cleanup_deadlines_live",
        ),
        (
            "test_mcp_timed_remote_cancellation.py",
            "test_blocked_cancellation_retains_failed_unbind_until_explicit_retry",
        ),
        (
            "test_mcp_timed_remote_cancellation.py",
            "test_server_outer_deadline_preserves_canonical_protocol_error",
        ),
        (
            "test_mcp_timed_remote_cancellation.py",
            "test_request_wait_has_independent_deadline_without_supervisor",
        ),
        ("test_session_timed_execution.py", "test_binding_lock_wait_is_bounded"),
        (
            "test_session_timed_execution.py",
            "test_cancel_reaches_active_real_credit_wait_without_session_lock",
        ),
        (
            "test_session_timed_execution.py",
            "test_paired_authored_full_frame_calls_preserve_count_render_buttons_and_events",
        ),
        (
            "test_session_timed_execution.py",
            "test_real_partial_public_tick_failure_counts_only_completed_frames",
        ),
        # Synthetic owners still exercise real supervisor/thread waits.
        (
            "test_probe_timed_rom_pair.py",
            "test_supervisor_cancels_both_and_owner_detaches_before_stop",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_interrupted_multiframe_call_records_actual_partial_progress",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_detach_failure_never_stops_session_with_live_binding",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_unpublished_factory_failure_cancels_peer_original_event",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_no_progress_return_is_explicit_and_does_not_count_requested_frames",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_first_supervisor_cancel_exception_does_not_skip_second_endpoint",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_live_owners_report_incomplete_cleanup_without_mutating_returned_report",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_attach_failure_retains_loaded_native_evidence_without_binding",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_partial_normal_public_return_is_not_frame_bound_success",
        ),
        # Spawned owners, cancellation watchers, and pipe drainers use real
        # scheduling and bounded waits even with synthetic or missing assets.
        (
            "test_probe_timed_rom_pair.py",
            "test_process_spawn_missing_assets_reports_both_child_failures",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_spawn_failure_cancels_waiting_peer",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_spawn_deadline_terminates_unresponsive_owners",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_spawn_rejects_invalid_child_report",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_cancellation_bridge_sets_local_event_before_publication",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_cancellation_bridge_continues_after_endpoint_cancel_error",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_stderr_capture_drains_native_fd_flood_with_bounded_retention",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_stdout_capture_drains_native_fd_flood_with_bounded_retention",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_startup_failure_signals_without_shared_event_locks",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_early_startup_failure_forces_termination_without_shared_event_locks",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_partial_report_kill_reaps_reader_without_shared_event_locks",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_spawn_propagates_explicit_menu_profile",
        ),
        (
            "test_probe_timed_rom_pair.py",
            "test_process_report_overflow_is_explicit_bounded_and_not_success",
        ),
        ("test_network_backend_transport.py", "test_on_edge_sends_REQ_and_waits_for_RESP"),
        (
            "test_network_backend_serial_transcript.py",
            "test_two_serialcores_exchange_byte_via_network_backend",
        ),
        ("test_network_backend_serial_transcript.py", "test_multiple_bytes_exchange"),
        (
            "test_network_backend_rearm.py",
            "test_listen_and_connect_over_loopback_exchange_byte",
        ),
        ("test_network_backend_wire_idle.py", "test_sync_with_peer_rendezvous"),
        (
            "test_timed_link_session_control.py",
            "test_real_pair_repeated_public_frames_bounded_wire_volume",
        ),
        (
            "test_timed_link_session_completeness.py",
            "test_real_v3_complete_progress_equal_watermark_accepts_current_prefix",
        ),
        (
            "test_timed_link_session_completeness.py",
            "test_real_v3_partial_complete_progress_write_preserves_sent_frontiers",
        ),
        (
            "test_timed_link_session_v3.py",
            "test_real_v3_external_cancel_interrupts_active_receive",
        ),
        (
            "test_timed_link_session_v3.py",
            "test_real_v3_poll_returning_after_deadline_never_applies_edge",
        ),
        # Real peer scheduling, fragmented ingress, and bounded owner waits;
        # facade doubles and validation-only remote cases stay unit-only.
        (
            "test_timed_remote.py",
            "test_exact_prelude_zero_nonce_and_coalesced_hello_not_overread",
        ),
        (
            "test_timed_remote.py",
            "test_prelude_and_hello_waits_share_deadline_and_cancel_cleanup",
        ),
        (
            "test_timed_remote.py",
            "test_connect_listen_share_exact_factory_deadline_through_hello",
        ),
        ("test_timed_remote.py", "test_accept_wait_is_bounded_and_closes_listener"),
        (
            "test_timed_remote_facade.py",
            "test_authored_runtime_two_owner_factory_attach_passive_sync_and_bilateral_fence",
        ),
        (
            "test_timed_remote_facade.py",
            "test_external_cancel_survives_factory_into_real_endpoint_wait",
        ),
        (
            "test_timed_wire_wirecontrol.py",
            "test_wirecontrol_handshake_rejects_caps_and_revision_without_downgrade",
        ),
        ("test_timed_wire_codec.py", "test_handshake_writer_preserves_terminal_reason"),
        (
            "test_timed_wire_progress.py",
            "test_bidirectional_concurrent_requests_and_responses",
        ),
        (
            "test_timed_wire_admission.py",
            "test_fast_response_before_writer_return_and_before_request_consumption",
        ),
        (
            "test_timed_wire_admission.py",
            "test_waiting_writer_admission_is_bounded_and_does_not_skip_sequence",
        ),
        (
            "test_timed_wire_transport.py",
            "test_peer_application_waits_for_local_hello_send_publication",
        ),
        (
            "test_timed_wire_transport.py",
            "test_one_absolute_deadline_covers_admission_and_partial_write",
        ),
        ("test_timed_wire_progress.py", "test_close_wakes_receive"),
        ("test_timed_wire_transport.py", "test_close_wakes_partial_frame_reader"),
    }
)


def classify_test(path: str | Path, test_name: str) -> frozenset[str]:
    """Return all production-gate markers for one collected test.

    ``test_name`` should be pytest's ``originalname`` so parametrized suffixes
    do not alter tier membership.
    """

    filename = Path(path).name
    test_key = (filename, test_name)
    if test_key in ROM_FREE_TESTS:
        return frozenset({"unit"})
    if filename in OPT_IN_MODULES:
        # Opt-in audit: no tier marker, so no tier expression can select a
        # skip that only a fully provisioned checkout could satisfy.
        return frozenset()

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
    "CANONICAL_ROM_BOOT_NODEIDS",
    "KNOWN_TEST_MODULES",
    "LOCAL_LINK_MODULES",
    "LOCAL_VARIANT_NODEIDS",
    "LOCAL_VERSION_PAIR_NODEIDS",
    "MARKERS",
    "MCP_STDIO_MODULES",
    "OPT_IN_MODULES",
    "REAL_ROM_MODULES",
    "REMOTE_LINK_MODULES",
    "REMOTE_VERSION_PAIR_NODEIDS",
    "ROM_FREE_TESTS",
    "SUPPORTED_VERSIONS",
    "TIER_REQUIRED_NODEIDS",
    "TIER_REQUIRED_TESTS",
    "TIMING_SENSITIVE_TESTS",
    "TRADE_ACCEPTANCE_TESTS",
    "TRADE_TESTS",
    "UNIT_MODULES",
    "classify_test",
]
