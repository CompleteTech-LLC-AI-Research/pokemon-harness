"""Sentinels that keep the #261 guard and the retention counts load-bearing (#270).

``tests/test_timed_menu_milestones.py`` holds the frame-bound retention
contract, but two of its assertions can be removed or relaxed with every
behavioural test still green. This module pins them structurally.

Both gaps were confirmed on ``master`` before this file existed:

* ``if clock_step == 0.0:`` -> ``if False:`` leaves the milestones + frame-bound
  files at **83 passed, 0 failed** (Finding 1: the #261 guard is unwired).
* additionally relaxing ``assert len(calls) == 300`` to ``>= 1`` also leaves
  **83 passed, 0 failed** (Finding 2: the exact counts are unpinned).

These checks are structural -- AST inspection -- because that is the only way
to catch a *deletion*. The #261 guard is proven non-vacuous behaviourally (it
fires with 8 failures when production is mutated so the wall clock wins), so
what is missing is only the proof that it is still wired. The behavioural
assertions in the milestones module remain the contract; this file is the
backstop that keeps them from being quietly removed.
"""
# ruff: noqa: I001  # export order preserves the original pytest collection order

import ast
import asyncio
import contextlib
import inspect
import sys
import types
from types import SimpleNamespace

import pytest

from tests import _timed_menu_milestone_sentinel_support as support
from tests._timed_menu_milestone_sentinel_support import (
    DEADLINE_TERMINATION,
    GUARD_FUNCTION,
    PINNED_COUNT_COMPARISONS,
    RETENTION_COUNT_SITES,
    RETENTION_SUBSCRIPT_COUNT_SITES,
    RUN_OWNER,
    _bypassing_sites,  # noqa: F401
    _is_canonical_module_import,  # noqa: F401
    _is_enforced,  # noqa: F401
    _is_tautology,  # noqa: F401
    _may_bypass,  # noqa: F401
    count_sites_that_bypass_the_guard,
    guard_is_wired_on_the_fast_clock_path,
    guard_rejects_the_deadline_terminal_state,
    observed_count_comparisons,
    retention_sites_observed,
    retention_subscript_sites_observed,
)


from tests._timed_menu_sentinel_guard_retention import (
    _sentinel_uses,  # noqa: F401
    test_the_short_circuit_rule_is_wired_into_every_enforcement_call_site,
    test_the_261_guard_is_still_wired_to_the_fast_clock_path,
    test_the_four_retention_counts_are_still_exact_equalities,
    test_the_full_set_of_pinned_count_comparisons_has_not_shrunk,
    test_the_261_guard_still_rejects_the_deadline_terminal_state,
    test_the_pinned_counts_are_reached_with_the_261_precondition_active,
    CLOCK_STEP_SPELLINGS,
    test_every_clock_step_spelling_is_classified,
    test_a_bypass_in_any_pinned_site_is_reported_not_just_the_first,
    test_the_production_entry_point_is_what_actually_reports_a_bypass,
    test_the_pinned_record_subscript_counts_are_still_exact_equalities,
    BYPASS_SHAPES,
    test_the_bypass_check_separates_live_comparisons_from_short_circuited_ones,
    test_tautology_detection_only_fires_on_provably_always_true_forms,
)

from tests._timed_menu_sentinel_reachability_shapes import (
    UNREACHABLE_SHAPES,
    test_reachability_rejects_exactly_the_shapes_that_cannot_fail,
    MIXED_REACHABILITY_SHAPES,
    test_reachability_decides_each_assert_by_its_own_position,
)

from tests._timed_menu_sentinel_binding_reachability import (
    RESIDUAL_DEFEAT_SHAPES,
    BARE_SUPPRESS_ROWS,
    test_a_bare_suppress_is_not_read_as_universal,
    test_the_residual_defeats_of_287_are_rejected,
    WALRUS_SHAPES,
)

from tests._timed_menu_sentinel_walrus_with_aliases import (
    DUNDER_SPELLING_SHAPES,
    test_walrus_bound_suppressors_in_a_with_header_are_rejected,
    test_the_dunder_spelling_is_covered_and_scoped_to_real_suppression,
    DISAGREEING_BRANCH_ORDERS,
    test_branch_order_never_decides_the_alias_verdict,
    test_a_name_bound_to_different_suppressors_is_ambiguous_not_ordered,
    test_plain_assignment_aliasing_needs_no_import_node,
    ASYNC_CONTEXT_SHAPES,
    test_async_with_is_loud_but_a_sync_suppressor_inside_it_is_not,
    SAME_BLOCK_ORDER_SHAPES,
    test_both_orders_in_one_block_keep_their_own_verdict,
)

from tests._timed_menu_sentinel_alias_reentry import (
    WALRUS_REENTRY_SHAPES,
    test_a_walrus_bound_alias_reaches_the_headers_that_re_enter_it,
)

from tests._timed_menu_sentinel_import_context_managers import (
    WALRUS_REBINDING_SHAPES,
    CARRIER_ONLY_SHAPES,
    MODULE_CARRIER_SHAPES,
    UNDECIDABLE_IMPORT_FROM_SHAPES,
    test_an_import_from_as_is_resolved_rather_than_declined,
    test_an_import_from_as_can_bind_a_real_context_manager,
    CONTEXT_MANAGER_CLASS_SHAPES,
    test_a_context_manager_class_is_not_itself_enterable,
    test_a_context_manager_instance_stays_enterable,
    test_a_local_class_with_an_enterable_metaclass_enters_the_bare_header,
    test_a_local_class_whose_metaclass_is_unreadable_is_declined,
    test_a_class_with_an_enterable_metaclass_is_enterable,
    test_the_unenterable_class_kind_is_the_one_the_module_already_uses,
    test_a_with_header_bound_by_a_carrier_is_dead_entry,
    test_a_module_scope_carrier_defeats_the_assert_below_it,
)

from tests._timed_menu_sentinel_carrier_attributes import (
    SUBSCRIPT_ATTRIBUTE_SUPPRESSOR_ROWS,
    test_a_suppressor_reached_through_a_subscript_or_attribute_is_read,
    test_an_unenterable_nested_container_is_not_read_as_a_defeat,
    SUPPRESSOR_REACHED_THROUGH_AN_UNREADABLE_STEP_ROWS,
    test_previously_unreadable_steps_match_executed_suppression,
    test_a_block_nested_module_carrier_is_still_declined,
    test_a_conditional_module_store_after_a_carrier_is_declined,
    FUNCTION_CARRIER_SUPERSESSION_SHAPES,
)

from tests._timed_menu_sentinel_conditional_carriers import (
    test_a_conditional_function_store_after_a_carrier_is_declined,
    FUNCTION_CARRIER_NON_ENTERABLE_SUPERSEDERS,
    test_a_function_carrier_decline_ignores_non_enterable_stores,
)

from tests._timed_menu_sentinel_entry_contracts import (
    MODULE_SCOPED_DECLARATION_SHAPES,
    test_a_module_carrier_reads_global_and_nonlocal_apart,
    NON_ENTERABLE_EXCLUSION_ROWS,
    _decline_fires_for,  # noqa: F401
    test_each_non_enterable_exclusion_is_load_bearing,
    FUNCTION_CARRIER_SUPERSESSION_LIMIT_SHAPES,
    test_a_function_carrier_supersession_limit_is_still_declined,
    test_a_walrus_alias_is_retired_by_every_binding_form,
)

from tests._timed_menu_sentinel_import_context_managers import (
    _assert_entry_contract,  # noqa: F401
)

from tests._timed_menu_sentinel_entry_contracts import (
    test_a_header_that_cannot_be_entered_defeats_the_assert,
    test_a_live_header_is_not_read_as_dead_by_the_unenterable_rules,
    CAPTURE_SCOPE_BOUNDARY_ROWS,
)

from tests._timed_menu_sentinel_capture_namespaces import (
    STARRED_TARGET_ENTRY_UNREACHABLE_ROWS,
    CARRIER_ENTRY_UNREACHABLE_ROWS,
    test_a_capture_binds_only_the_namespace_it_was_written_in,
    NONLOCAL_CAPTURE_SCOPE_ROWS,
    test_a_nonlocal_capture_binds_the_enclosing_function,
    MATCH_CAPTURE_SHADOWED_ROWS,
    test_a_capture_shadowed_by_a_later_store_is_not_the_value_in_force,
)

from tests._timed_menu_sentinel_starred_bindings import (
    BINDING_FORM_SHAPES,
    test_a_string_field_carrier_cannot_be_entered_so_the_assert_is_unreachable,
    test_a_starred_target_binds_a_list_so_the_assert_is_unreachable,
    test_starred_target_kind_uses_the_final_store,
    test_later_chained_plain_target_clears_a_starred_binding,
    CARRIER_IN_HEADER_ROWS,
    test_a_binding_form_reads_the_value_that_lands_on_the_name,
    test_a_carrier_inside_the_reading_header_leaves_the_name_unenterable,
)

from tests._timed_menu_sentinel_tied_store_shapes import (
    TIED_STORE_ROWS,
)

from tests._timed_menu_sentinel_loop_elements import (
    UNANIMOUS_LOOP_ELEMENT_ROWS,
    EVIDENCE_DECLINED_WHILE_SWALLOWED,
    test_a_loop_literal_whose_elements_agree_is_decided_in_the_loop_body,
    test_every_unanimous_loop_element_row_matches_what_cpython_actually_does,
)

from tests._timed_menu_sentinel_after_loop_targets import (
    AFTER_LOOP_ROWS,
    test_a_loop_target_read_after_its_loop_is_answered_from_the_last_element,
    test_every_after_loop_row_matches_what_cpython_actually_does,
    test_two_stores_sharing_one_statement_resolve_without_walk_order,
    _async_loop_target_is_undecidable,  # noqa: F401
)

from tests._timed_menu_sentinel_loop_capture import (
    LOOP_ELEMENT_LIVE_SHAPES,
    test_a_loop_binds_the_element_it_leaves_behind,
    test_an_except_as_handler_is_decidable_even_after_a_conditional_store,
    WALRUS_NON_ASSIGN_SHAPES,
    test_a_walrus_bound_outside_an_assignment_still_reaches_a_later_header,
    MATCH_CAPTURE_SHAPES,
    MATCH_CAPTURE_ALWAYS_BINDS_SHAPES,
    MATCH_CAPTURE_GUARANTEE_ROWS,
    MATCH_CAPTURE_MATCHING_SUBJECT,
    _capture_fixture,  # noqa: F401
    _verdicts,  # noqa: F401
    _execute_outer,  # noqa: F401
    _execute_guarantee,  # noqa: F401
    test_a_capture_binds_guaranteedly_only_on_an_irrefutable_last_unguarded_clause,
    test_an_irrefutable_capture_always_retires_a_carried_suppressor,
    test_a_refutable_capture_does_not_retire_a_binding_it_never_made,
    test_a_selected_capture_binds_a_value_that_cannot_be_entered,
)

from tests._timed_menu_sentinel_match_capture import (
    LITERAL_SUBJECT_ROWS,
    _literal_subject_source,  # noqa: F401
    _execute_literal_subject,  # noqa: F401
    test_a_written_out_match_subject_decides_its_own_selection,
    MATCH_IN_UNREACHABLE_LOOP_ROWS,
    test_a_capture_inside_a_zero_iteration_loop_is_not_decided,
    MATCH_CAPTURE_SCOPE_ROWS,
    MATCH_CAPTURE_OWNS_NESTED_HEADER_ROWS,
    test_a_capture_reaches_a_header_nested_in_its_own_clause_body,
    test_a_match_capture_respects_its_own_scope_and_position,
)

from tests._timed_menu_sentinel_user_exit_classes import (
    USER_EXIT_SWALLOW_SHAPES,
    test_a_class_defined_inside_a_function_is_still_read,
    test_a_user_exit_that_swallows_assertion_error_is_a_defeat,
    test_user_exit_class_resolution_respects_scope_and_construction_position,
    test_a_future_local_class_definition_cannot_supply_the_current_constructor,
    test_a_captured_class_is_resolved_at_the_nested_function_invocation,
    test_the_user_exit_rule_does_not_fire_on_an_ordinary_context_manager,
    test_an_unreadable_constructor_is_not_assumed_to_suppress,
)

from tests._timed_menu_sentinel_scope_lookup import (
    test_only_a_module_level_class_counts,
    test_a_module_scope_lookup_does_not_descend_into_a_function_body,
    STALE_LOCAL_CARRIER_SHAPES,
    DEAD_CONDITION_AFTER_CARRIER_SHAPES,
    test_a_conditional_store_superseding_a_local_carrier_is_declined,
    ELIF_LINK_ARMS_SHAPES,
    test_an_elif_link_is_reached_only_when_every_test_above_failed,
)

from tests._timed_menu_sentinel_elif_links import (
    ELIF_LINK_SUPPRESSOR_SHAPES,
    _assert_suppression_contract,  # noqa: F401
    test_an_elif_link_suppressor_is_live_when_the_carried_store_is_not,
    ELSE_ARM_SUPPRESSOR_SHAPES,
    test_an_else_arm_suppressor_is_live_when_the_carried_store_is_not,
    NESTED_ELIF_LINK_SHAPES,
    test_a_nested_link_inside_a_literal_true_elif_stays_conditional,
)

from tests._timed_menu_sentinel_conditional_stores import (
    test_a_nonenterable_conditional_store_does_not_revive_a_stale_carrier,
    SHADOWED_CALLEE_SOURCES,
    _shadowed_callee_reaches_assert,  # noqa: F401
    MODULE_SCOPE_FALSE_LIVE_SOURCES,
    test_a_name_the_module_does_not_bind_at_call_time_is_reported_dead,
    test_a_constructor_callee_shadowed_outside_the_body_is_reported_live,
    ROUND_EIGHT_SOURCES,
)

from tests._timed_menu_sentinel_module_binding_order import (
    ROUND_SEVEN_SOURCES,
    ROUND_SIX_SOURCES,
    _fixture_is_entered,  # noqa: F401
    test_module_scope_order_scope_and_builtins_reading,
    test_a_self_alias_excludes_its_own_store_and_not_an_earlier_one,
    test_loop_body_header_reads_current_iteration_not_final_element,
    test_try_reachability_matches_executed_exception_paths,
    test_inline_user_exit_acceptance_matches_execution,
    test_inline_class_name_shadowing_keeps_a_live_context_manager,
    test_inline_class_shadowing_in_annotations_and_captured_parameters,
    test_inline_class_constructor_declines_nonclass_lexical_stores,
    test_inline_class_constructor_declines_a_captured_callable_binding,
    STARRED_LOOP_ENTRY_UNREACHABLE_ROWS,
)

from tests._timed_menu_sentinel_nested_loops import (
    test_a_starred_target_reached_through_a_loop_binds_a_list,
    test_starred_loop_store_must_remain_in_force_at_entry,
    test_starred_store_after_a_loop_header_does_not_rewrite_its_value,
    test_starred_target_after_a_loop_respects_whether_it_ran,
    NONLOCAL_SUPPRESSOR_SHAPES,
    test_a_nonlocal_name_reaches_its_enclosing_binding,
    test_a_closure_name_that_is_not_declared_nonlocal_is_not_followed,
    AFTER_LOOP_FOR_TARGET_SHAPES,
    test_a_loop_target_read_after_the_loop_keeps_its_real_value,
    test_the_after_loop_for_target_expectation_matches_executed_cpython,
    NONLOCAL_REBIND_REGRESSIONS,
    test_nonlocal_scope_and_rebinding_match_executed_calls,
    ALWAYS_RUN_ARM_EXECUTED_ROWS,
    test_always_run_arms_follow_every_enclosing_conditional,
    test_a_decided_inner_arm_still_depends_on_a_conditional_outer_else,
    test_elif_suppression_resolution_requires_an_enterable_failing_skipped_path,
)

from tests._timed_menu_sentinel_loop_else_suppression import (
    LOOP_ELSE_SUPPRESSOR_SHAPES,
    FUNCTION_LOCAL_IMPORT_SHAPES,
    test_a_loop_else_suppressor_is_live_when_a_break_skips_it,
    test_a_loop_else_suppressor_is_live_under_a_function_local_import,
    IMPORT_SHAPE_ROWS,
    test_only_the_resolved_root_is_admitted_as_a_transparent_import,
)

from tests._timed_menu_sentinel_import_roots import (
    test_an_alias_that_rebinds_the_walked_root_is_not_transparent,
    test_a_dotted_import_of_a_leaf_is_not_a_transparent_root,
    test_an_elif_cannot_invent_a_nullcontext_from_a_shadowed_import,
    test_an_elif_failure_witness_cannot_skip_earlier_control_or_opaque_calls,
    test_an_elif_cannot_invent_a_nullcontext_from_a_shadowed_module_import,
    UNREACHED_LOOP_BODY_ROWS,
    test_an_unreachable_loop_body_does_not_rebind_the_name,
    test_empty_loop_filter_keeps_conditional_entry_outcomes,
    test_empty_loop_filter_declines_shadowed_exception_argument,
    test_literal_match_selection_preserves_runtime_truth,
    test_literal_match_records_captured_manager,
    test_literal_match_declines_subject_construction_failure,
)

from tests._timed_menu_sentinel_literal_match_imports import (
    test_literal_match_declines_guard_or_body_effects,
    test_literal_match_declines_unreachable_assertion_witness,
    test_literal_match_declines_contextlib_member_monkeypatch,
    test_literal_match_requires_reachable_assertion_failure,
    FROM_IMPORT_SUBJECT_ROWS,
    test_a_from_import_subject_alias_settles_its_own_selection,
    test_a_from_import_of_an_unexported_name_is_still_a_rebinding,
    FROM_IMPORT_CARRIER_ROWS,
    test_a_from_import_in_the_body_is_a_plain_import_for_this_walk,
    test_literal_match_supersedes_a_carried_suppressor,
    test_literal_match_declines_a_rebound_callee,
    test_from_import_attribute_is_not_a_canonical_builtin_module,
    test_literal_subject_callee_import_must_be_absolute_and_precede_call,
    test_literal_subject_callee_declines_enclosing_shadow,
    test_literal_subject_declines_modified_from_import_manager_class,
    test_walrus_literal_entry_declines_shadowed_module_attributes,
    CARRIED_ELIF_SUPPRESSOR_SHAPES,
)

from tests._timed_menu_sentinel_loop_else_witness import (
    test_an_elif_binding_a_plain_manager_does_not_retire_a_carried_suppressor,
    LOOP_ELSE_IMPORT_SPELLINGS,
    LOOP_ELSE_IMPORT_SHADOWS,
    test_a_loop_else_witness_survives_the_import_spelling,
    test_a_loop_else_witness_declines_an_import_that_shadows_its_root,
    LOOP_ELSE_RAISING_GUARDS,
    test_a_loop_else_witness_declines_a_guard_that_raises,
    WALRUS_NAME_ENTRY_SHAPES,
    test_a_walrus_header_named_to_an_unenterable_value_is_not_a_live_assert,
)

from tests._timed_menu_sentinel_walrus_binding import (
    WALRUS_NAME_ENTRY_DECLINES,
    test_a_walrus_name_the_scope_cannot_resolve_is_left_live,
    test_walrus_name_uses_lexical_store_and_alias_snapshot,
    test_walrus_name_declines_module_binding_shadowed_by_closure,
    test_walrus_name_does_not_assume_decorated_definition_is_function,
    test_walrus_name_does_not_assume_class_metaclass_is_unenterable,
    test_walrus_name_does_not_read_module_future_store_into_earlier_invocation,
    test_walrus_module_snapshot_declines_implicit_decorator_invocation,
    test_walrus_module_snapshot_declines_implicit_truthiness_invocation,
    test_walrus_module_snapshot_declines_late_import_callback,
    test_loop_else_identity_uses_runtime_object_not_ast_object,
    test_loop_else_witness_declines_effectful_definition_creation,
    test_loop_else_witness_declines_definition_overwriting_failure_parameter,
    test_class_carrier_proves_metaclass_exit_before_admitting_body,
    test_class_carrier_declines_opaque_setup_before_bare_header,
    test_class_carrier_unresolved_shape_keeps_live_runtime_as_known_decline,
    test_class_carrier_declines_member_overwrite_before_entry,
    test_class_carrier_type_spelling_does_not_resolve_caller_parameter,
    test_class_carrier_declines_decorator_replacing_class_value,
    test_class_carrier_requires_inert_assertion_message,
    test_class_carrier_declines_generic_metaclass_lookup_scope,
    test_class_carrier_variadic_collectors_are_not_primitive_failure_witnesses,
    test_class_carrier_class_definitions_cannot_be_parameter_failure_witnesses,
    test_try_witness_requires_every_reachable_arm_to_resume,
    test_try_witness_declines_opaque_helper_even_when_one_call_is_harmless,
    TRY_UNKNOWN_IMPORT_RUNTIME_ROWS,
)

from tests._timed_menu_sentinel_class_try_witnesses import (
    test_try_unknown_import_above_the_loop_else_is_transparent,
    TRY_ABOVE_LOOP_ELSE_BOUNDARY_ROWS,
    test_try_above_loop_else_boundary,
    TRY_ABOVE_LOOP_ELSE_NEIGHBOURING_ROWS,
    test_485_records_neighbouring_forms_without_claiming_them,
    UNSELECTED_MATCH_CASE_STORE_ROWS,
    test_a_store_in_an_unselected_match_case_does_not_retire_a_carried_suppressor,
    UNREACHABLE_BRANCH_STORE_ROWS,
    _unreachable_branch_store_fixture,  # noqa: F401
    test_a_store_in_a_branch_that_cannot_run_does_not_retire_a_carried_suppressor,
    test_issue365_full_two_assertions_keep_carried_suppressor,
    test_unreachable_literal_branch_does_not_replace_alias_source,
    test_issue365_top_level_retirement_remains_live,
    test_unreachable_if_store_proof_does_not_trust_enclosing_constructor_parameter,
    test_literal_if_alias_filter_preserves_other_enclosing_loop_callables,
    test_unselected_store_literal_pattern_semantics_are_executed,
    test_match_subject_store_proof_refuses_with_body_import_rebinding,
    test_match_subject_store_proof_refuses_context_enter_callback,
    NESTED_SCOPE_REBIND_ROWS,
    test_a_nested_scope_store_does_not_retire_a_carried_suppressor,
    DEFINITION_TIME_REBIND_ROWS,
    test_definition_metadata_rebinds_the_containing_carrier,
    NESTED_DECLARATION_SCOPE_ROWS,
)

from tests._timed_menu_sentinel_binding_metadata import (
    test_nested_store_reaches_only_its_actual_binding,
    test_annotation_reads_do_not_retire_a_carrier,
    test_a_nested_global_rebind_reaches_a_global_carrier,
    test_failing_definition_metadata_does_not_create_a_live_assertion,
    test_opaque_setup_before_metadata_keeps_the_known_decline,
    test_module_definition_metadata_cannot_install_a_fake_nullcontext,
    test_completed_user_instance_loop_last_element_has_executed_protocol,
)


__all__ = [
    "AFTER_LOOP_FOR_TARGET_SHAPES",
    "AFTER_LOOP_ROWS",
    "ALWAYS_RUN_ARM_EXECUTED_ROWS",
    "ASYNC_CONTEXT_SHAPES",
    "BARE_SUPPRESS_ROWS",
    "BINDING_FORM_SHAPES",
    "BYPASS_SHAPES",
    "CAPTURE_SCOPE_BOUNDARY_ROWS",
    "CARRIED_ELIF_SUPPRESSOR_SHAPES",
    "CARRIER_ENTRY_UNREACHABLE_ROWS",
    "CARRIER_IN_HEADER_ROWS",
    "CARRIER_ONLY_SHAPES",
    "CLOCK_STEP_SPELLINGS",
    "CONTEXT_MANAGER_CLASS_SHAPES",
    "DEADLINE_TERMINATION",
    "DEAD_CONDITION_AFTER_CARRIER_SHAPES",
    "DEFINITION_TIME_REBIND_ROWS",
    "DISAGREEING_BRANCH_ORDERS",
    "DUNDER_SPELLING_SHAPES",
    "ELIF_LINK_ARMS_SHAPES",
    "ELIF_LINK_SUPPRESSOR_SHAPES",
    "ELSE_ARM_SUPPRESSOR_SHAPES",
    "EVIDENCE_DECLINED_WHILE_SWALLOWED",
    "FROM_IMPORT_CARRIER_ROWS",
    "FROM_IMPORT_SUBJECT_ROWS",
    "FUNCTION_CARRIER_NON_ENTERABLE_SUPERSEDERS",
    "FUNCTION_CARRIER_SUPERSESSION_LIMIT_SHAPES",
    "FUNCTION_CARRIER_SUPERSESSION_SHAPES",
    "FUNCTION_LOCAL_IMPORT_SHAPES",
    "GUARD_FUNCTION",
    "IMPORT_SHAPE_ROWS",
    "LITERAL_SUBJECT_ROWS",
    "LOOP_ELEMENT_LIVE_SHAPES",
    "LOOP_ELSE_IMPORT_SHADOWS",
    "LOOP_ELSE_IMPORT_SPELLINGS",
    "LOOP_ELSE_RAISING_GUARDS",
    "LOOP_ELSE_SUPPRESSOR_SHAPES",
    "MATCH_CAPTURE_ALWAYS_BINDS_SHAPES",
    "MATCH_CAPTURE_GUARANTEE_ROWS",
    "MATCH_CAPTURE_MATCHING_SUBJECT",
    "MATCH_CAPTURE_OWNS_NESTED_HEADER_ROWS",
    "MATCH_CAPTURE_SCOPE_ROWS",
    "MATCH_CAPTURE_SHADOWED_ROWS",
    "MATCH_CAPTURE_SHAPES",
    "MATCH_IN_UNREACHABLE_LOOP_ROWS",
    "MIXED_REACHABILITY_SHAPES",
    "MODULE_CARRIER_SHAPES",
    "MODULE_SCOPED_DECLARATION_SHAPES",
    "MODULE_SCOPE_FALSE_LIVE_SOURCES",
    "NESTED_DECLARATION_SCOPE_ROWS",
    "NESTED_ELIF_LINK_SHAPES",
    "NESTED_SCOPE_REBIND_ROWS",
    "NONLOCAL_CAPTURE_SCOPE_ROWS",
    "NONLOCAL_REBIND_REGRESSIONS",
    "NONLOCAL_SUPPRESSOR_SHAPES",
    "NON_ENTERABLE_EXCLUSION_ROWS",
    "PINNED_COUNT_COMPARISONS",
    "RESIDUAL_DEFEAT_SHAPES",
    "RETENTION_COUNT_SITES",
    "RETENTION_SUBSCRIPT_COUNT_SITES",
    "ROUND_EIGHT_SOURCES",
    "ROUND_SEVEN_SOURCES",
    "ROUND_SIX_SOURCES",
    "RUN_OWNER",
    "SAME_BLOCK_ORDER_SHAPES",
    "SHADOWED_CALLEE_SOURCES",
    "STALE_LOCAL_CARRIER_SHAPES",
    "STARRED_LOOP_ENTRY_UNREACHABLE_ROWS",
    "STARRED_TARGET_ENTRY_UNREACHABLE_ROWS",
    "SUBSCRIPT_ATTRIBUTE_SUPPRESSOR_ROWS",
    "SUPPRESSOR_REACHED_THROUGH_AN_UNREADABLE_STEP_ROWS",
    "TIED_STORE_ROWS",
    "TRY_ABOVE_LOOP_ELSE_BOUNDARY_ROWS",
    "TRY_ABOVE_LOOP_ELSE_NEIGHBOURING_ROWS",
    "TRY_UNKNOWN_IMPORT_RUNTIME_ROWS",
    "UNANIMOUS_LOOP_ELEMENT_ROWS",
    "UNDECIDABLE_IMPORT_FROM_SHAPES",
    "UNREACHABLE_BRANCH_STORE_ROWS",
    "UNREACHABLE_SHAPES",
    "UNREACHED_LOOP_BODY_ROWS",
    "UNSELECTED_MATCH_CASE_STORE_ROWS",
    "USER_EXIT_SWALLOW_SHAPES",
    "WALRUS_NAME_ENTRY_DECLINES",
    "WALRUS_NAME_ENTRY_SHAPES",
    "WALRUS_NON_ASSIGN_SHAPES",
    "WALRUS_REBINDING_SHAPES",
    "WALRUS_REENTRY_SHAPES",
    "WALRUS_SHAPES",
    "SimpleNamespace",
    "ast",
    "asyncio",
    "contextlib",
    "count_sites_that_bypass_the_guard",
    "guard_is_wired_on_the_fast_clock_path",
    "guard_rejects_the_deadline_terminal_state",
    "inspect",
    "observed_count_comparisons",
    "pytest",
    "retention_sites_observed",
    "retention_subscript_sites_observed",
    "support",
    "sys",
    "test_485_records_neighbouring_forms_without_claiming_them",
    "test_a_bare_suppress_is_not_read_as_universal",
    "test_a_binding_form_reads_the_value_that_lands_on_the_name",
    "test_a_block_nested_module_carrier_is_still_declined",
    "test_a_bypass_in_any_pinned_site_is_reported_not_just_the_first",
    "test_a_capture_binds_guaranteedly_only_on_an_irrefutable_last_unguarded_clause",
    "test_a_capture_binds_only_the_namespace_it_was_written_in",
    "test_a_capture_inside_a_zero_iteration_loop_is_not_decided",
    "test_a_capture_reaches_a_header_nested_in_its_own_clause_body",
    "test_a_capture_shadowed_by_a_later_store_is_not_the_value_in_force",
    "test_a_captured_class_is_resolved_at_the_nested_function_invocation",
    "test_a_carrier_inside_the_reading_header_leaves_the_name_unenterable",
    "test_a_class_defined_inside_a_function_is_still_read",
    "test_a_class_with_an_enterable_metaclass_is_enterable",
    "test_a_closure_name_that_is_not_declared_nonlocal_is_not_followed",
    "test_a_conditional_function_store_after_a_carrier_is_declined",
    "test_a_conditional_module_store_after_a_carrier_is_declined",
    "test_a_conditional_store_superseding_a_local_carrier_is_declined",
    "test_a_constructor_callee_shadowed_outside_the_body_is_reported_live",
    "test_a_context_manager_class_is_not_itself_enterable",
    "test_a_context_manager_instance_stays_enterable",
    "test_a_decided_inner_arm_still_depends_on_a_conditional_outer_else",
    "test_a_dotted_import_of_a_leaf_is_not_a_transparent_root",
    "test_a_from_import_in_the_body_is_a_plain_import_for_this_walk",
    "test_a_from_import_of_an_unexported_name_is_still_a_rebinding",
    "test_a_from_import_subject_alias_settles_its_own_selection",
    "test_a_function_carrier_decline_ignores_non_enterable_stores",
    "test_a_function_carrier_supersession_limit_is_still_declined",
    "test_a_future_local_class_definition_cannot_supply_the_current_constructor",
    "test_a_header_that_cannot_be_entered_defeats_the_assert",
    "test_a_live_header_is_not_read_as_dead_by_the_unenterable_rules",
    "test_a_local_class_whose_metaclass_is_unreadable_is_declined",
    "test_a_local_class_with_an_enterable_metaclass_enters_the_bare_header",
    "test_a_loop_binds_the_element_it_leaves_behind",
    "test_a_loop_else_suppressor_is_live_under_a_function_local_import",
    "test_a_loop_else_suppressor_is_live_when_a_break_skips_it",
    "test_a_loop_else_witness_declines_a_guard_that_raises",
    "test_a_loop_else_witness_declines_an_import_that_shadows_its_root",
    "test_a_loop_else_witness_survives_the_import_spelling",
    "test_a_loop_literal_whose_elements_agree_is_decided_in_the_loop_body",
    "test_a_loop_target_read_after_its_loop_is_answered_from_the_last_element",
    "test_a_loop_target_read_after_the_loop_keeps_its_real_value",
    "test_a_match_capture_respects_its_own_scope_and_position",
    "test_a_module_carrier_reads_global_and_nonlocal_apart",
    "test_a_module_scope_carrier_defeats_the_assert_below_it",
    "test_a_module_scope_lookup_does_not_descend_into_a_function_body",
    "test_a_name_bound_to_different_suppressors_is_ambiguous_not_ordered",
    "test_a_name_the_module_does_not_bind_at_call_time_is_reported_dead",
    "test_a_nested_global_rebind_reaches_a_global_carrier",
    "test_a_nested_link_inside_a_literal_true_elif_stays_conditional",
    "test_a_nested_scope_store_does_not_retire_a_carried_suppressor",
    "test_a_nonenterable_conditional_store_does_not_revive_a_stale_carrier",
    "test_a_nonlocal_capture_binds_the_enclosing_function",
    "test_a_nonlocal_name_reaches_its_enclosing_binding",
    "test_a_refutable_capture_does_not_retire_a_binding_it_never_made",
    "test_a_selected_capture_binds_a_value_that_cannot_be_entered",
    "test_a_self_alias_excludes_its_own_store_and_not_an_earlier_one",
    "test_a_starred_target_binds_a_list_so_the_assert_is_unreachable",
    "test_a_starred_target_reached_through_a_loop_binds_a_list",
    "test_a_store_in_a_branch_that_cannot_run_does_not_retire_a_carried_suppressor",
    "test_a_store_in_an_unselected_match_case_does_not_retire_a_carried_suppressor",
    "test_a_string_field_carrier_cannot_be_entered_so_the_assert_is_unreachable",
    "test_a_suppressor_reached_through_a_subscript_or_attribute_is_read",
    "test_a_user_exit_that_swallows_assertion_error_is_a_defeat",
    "test_a_walrus_alias_is_retired_by_every_binding_form",
    "test_a_walrus_bound_alias_reaches_the_headers_that_re_enter_it",
    "test_a_walrus_bound_outside_an_assignment_still_reaches_a_later_header",
    "test_a_walrus_header_named_to_an_unenterable_value_is_not_a_live_assert",
    "test_a_walrus_name_the_scope_cannot_resolve_is_left_live",
    "test_a_with_header_bound_by_a_carrier_is_dead_entry",
    "test_a_written_out_match_subject_decides_its_own_selection",
    "test_always_run_arms_follow_every_enclosing_conditional",
    "test_an_alias_that_rebinds_the_walked_root_is_not_transparent",
    "test_an_elif_binding_a_plain_manager_does_not_retire_a_carried_suppressor",
    "test_an_elif_cannot_invent_a_nullcontext_from_a_shadowed_import",
    "test_an_elif_cannot_invent_a_nullcontext_from_a_shadowed_module_import",
    "test_an_elif_failure_witness_cannot_skip_earlier_control_or_opaque_calls",
    "test_an_elif_link_is_reached_only_when_every_test_above_failed",
    "test_an_elif_link_suppressor_is_live_when_the_carried_store_is_not",
    "test_an_else_arm_suppressor_is_live_when_the_carried_store_is_not",
    "test_an_except_as_handler_is_decidable_even_after_a_conditional_store",
    "test_an_import_from_as_can_bind_a_real_context_manager",
    "test_an_import_from_as_is_resolved_rather_than_declined",
    "test_an_irrefutable_capture_always_retires_a_carried_suppressor",
    "test_an_unenterable_nested_container_is_not_read_as_a_defeat",
    "test_an_unreachable_loop_body_does_not_rebind_the_name",
    "test_an_unreadable_constructor_is_not_assumed_to_suppress",
    "test_annotation_reads_do_not_retire_a_carrier",
    "test_async_with_is_loud_but_a_sync_suppressor_inside_it_is_not",
    "test_both_orders_in_one_block_keep_their_own_verdict",
    "test_branch_order_never_decides_the_alias_verdict",
    "test_class_carrier_class_definitions_cannot_be_parameter_failure_witnesses",
    "test_class_carrier_declines_decorator_replacing_class_value",
    "test_class_carrier_declines_generic_metaclass_lookup_scope",
    "test_class_carrier_declines_member_overwrite_before_entry",
    "test_class_carrier_declines_opaque_setup_before_bare_header",
    "test_class_carrier_proves_metaclass_exit_before_admitting_body",
    "test_class_carrier_requires_inert_assertion_message",
    "test_class_carrier_type_spelling_does_not_resolve_caller_parameter",
    "test_class_carrier_unresolved_shape_keeps_live_runtime_as_known_decline",
    "test_class_carrier_variadic_collectors_are_not_primitive_failure_witnesses",
    "test_completed_user_instance_loop_last_element_has_executed_protocol",
    "test_definition_metadata_rebinds_the_containing_carrier",
    "test_each_non_enterable_exclusion_is_load_bearing",
    "test_elif_suppression_resolution_requires_an_enterable_failing_skipped_path",
    "test_empty_loop_filter_declines_shadowed_exception_argument",
    "test_empty_loop_filter_keeps_conditional_entry_outcomes",
    "test_every_after_loop_row_matches_what_cpython_actually_does",
    "test_every_clock_step_spelling_is_classified",
    "test_every_unanimous_loop_element_row_matches_what_cpython_actually_does",
    "test_failing_definition_metadata_does_not_create_a_live_assertion",
    "test_from_import_attribute_is_not_a_canonical_builtin_module",
    "test_inline_class_constructor_declines_a_captured_callable_binding",
    "test_inline_class_constructor_declines_nonclass_lexical_stores",
    "test_inline_class_name_shadowing_keeps_a_live_context_manager",
    "test_inline_class_shadowing_in_annotations_and_captured_parameters",
    "test_inline_user_exit_acceptance_matches_execution",
    "test_issue365_full_two_assertions_keep_carried_suppressor",
    "test_issue365_top_level_retirement_remains_live",
    "test_later_chained_plain_target_clears_a_starred_binding",
    "test_literal_if_alias_filter_preserves_other_enclosing_loop_callables",
    "test_literal_match_declines_a_rebound_callee",
    "test_literal_match_declines_contextlib_member_monkeypatch",
    "test_literal_match_declines_guard_or_body_effects",
    "test_literal_match_declines_subject_construction_failure",
    "test_literal_match_declines_unreachable_assertion_witness",
    "test_literal_match_records_captured_manager",
    "test_literal_match_requires_reachable_assertion_failure",
    "test_literal_match_selection_preserves_runtime_truth",
    "test_literal_match_supersedes_a_carried_suppressor",
    "test_literal_subject_callee_declines_enclosing_shadow",
    "test_literal_subject_callee_import_must_be_absolute_and_precede_call",
    "test_literal_subject_declines_modified_from_import_manager_class",
    "test_loop_body_header_reads_current_iteration_not_final_element",
    "test_loop_else_identity_uses_runtime_object_not_ast_object",
    "test_loop_else_witness_declines_definition_overwriting_failure_parameter",
    "test_loop_else_witness_declines_effectful_definition_creation",
    "test_match_subject_store_proof_refuses_context_enter_callback",
    "test_match_subject_store_proof_refuses_with_body_import_rebinding",
    "test_module_definition_metadata_cannot_install_a_fake_nullcontext",
    "test_module_scope_order_scope_and_builtins_reading",
    "test_nested_store_reaches_only_its_actual_binding",
    "test_nonlocal_scope_and_rebinding_match_executed_calls",
    "test_only_a_module_level_class_counts",
    "test_only_the_resolved_root_is_admitted_as_a_transparent_import",
    "test_opaque_setup_before_metadata_keeps_the_known_decline",
    "test_plain_assignment_aliasing_needs_no_import_node",
    "test_previously_unreadable_steps_match_executed_suppression",
    "test_reachability_decides_each_assert_by_its_own_position",
    "test_reachability_rejects_exactly_the_shapes_that_cannot_fail",
    "test_starred_loop_store_must_remain_in_force_at_entry",
    "test_starred_store_after_a_loop_header_does_not_rewrite_its_value",
    "test_starred_target_after_a_loop_respects_whether_it_ran",
    "test_starred_target_kind_uses_the_final_store",
    "test_tautology_detection_only_fires_on_provably_always_true_forms",
    "test_the_261_guard_is_still_wired_to_the_fast_clock_path",
    "test_the_261_guard_still_rejects_the_deadline_terminal_state",
    "test_the_after_loop_for_target_expectation_matches_executed_cpython",
    "test_the_bypass_check_separates_live_comparisons_from_short_circuited_ones",
    "test_the_dunder_spelling_is_covered_and_scoped_to_real_suppression",
    "test_the_four_retention_counts_are_still_exact_equalities",
    "test_the_full_set_of_pinned_count_comparisons_has_not_shrunk",
    "test_the_pinned_counts_are_reached_with_the_261_precondition_active",
    "test_the_pinned_record_subscript_counts_are_still_exact_equalities",
    "test_the_production_entry_point_is_what_actually_reports_a_bypass",
    "test_the_residual_defeats_of_287_are_rejected",
    "test_the_short_circuit_rule_is_wired_into_every_enforcement_call_site",
    "test_the_unenterable_class_kind_is_the_one_the_module_already_uses",
    "test_the_user_exit_rule_does_not_fire_on_an_ordinary_context_manager",
    "test_try_above_loop_else_boundary",
    "test_try_reachability_matches_executed_exception_paths",
    "test_try_unknown_import_above_the_loop_else_is_transparent",
    "test_try_witness_declines_opaque_helper_even_when_one_call_is_harmless",
    "test_try_witness_requires_every_reachable_arm_to_resume",
    "test_two_stores_sharing_one_statement_resolve_without_walk_order",
    "test_unreachable_if_store_proof_does_not_trust_enclosing_constructor_parameter",
    "test_unreachable_literal_branch_does_not_replace_alias_source",
    "test_unselected_store_literal_pattern_semantics_are_executed",
    "test_user_exit_class_resolution_respects_scope_and_construction_position",
    "test_walrus_bound_suppressors_in_a_with_header_are_rejected",
    "test_walrus_literal_entry_declines_shadowed_module_attributes",
    "test_walrus_module_snapshot_declines_implicit_decorator_invocation",
    "test_walrus_module_snapshot_declines_implicit_truthiness_invocation",
    "test_walrus_module_snapshot_declines_late_import_callback",
    "test_walrus_name_declines_module_binding_shadowed_by_closure",
    "test_walrus_name_does_not_assume_class_metaclass_is_unenterable",
    "test_walrus_name_does_not_assume_decorated_definition_is_function",
    "test_walrus_name_does_not_read_module_future_store_into_earlier_invocation",
    "test_walrus_name_uses_lexical_store_and_alias_snapshot",
    "types",
]
