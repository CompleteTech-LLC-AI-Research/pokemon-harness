#!/usr/bin/env python3
"""Verify a declared qualification-runner allocation and its prerequisites.

The production gate runs emulator pairs whose deadlines are sensitive to CPU
scheduling.  This tool verifies, without stopping or reprioritizing any other
process, whether the current host matches an operator-declared allocation.

Modes:

* ``--report`` prints the observed host facts (CPU affinity, cgroup quota and
  weight, throttling counters, memory, disk, shared memory, load).
* ``--check`` compares those facts against an explicit declaration plus a
  pinned, immutable allocation descriptor and runs the runtime/asset
  prerequisites.  It exits ``0`` only when every check is ``ok``; a missing
  declaration is reported as ``blocked``, never as a pass.
* ``--setup`` creates the private per-job directory tree and validates
  prerequisites and the native build fingerprint without reserving capacity.
* ``--reserve`` writes the allocation descriptor, takes an exclusive lease on
  it, and holds that lease while running an optional command under it.
* ``--release`` ends a lease this tool owns; ``--recover`` removes only stale
  lease state whose owner is already gone.  Neither reprioritizes unrelated
  processes.

A matching declaration alone is never accepted as a reservation.  The run is
bound to an operator-owned allocation descriptor whose bytes are pinned by
SHA-256, and whose holder, lease lock, cgroup membership, cpuset, and quota are
re-observed on this host.

The declaration is operator-owned and deliberately external to the repository.
Use ``--declaration`` or ``POKERED_QUALIFICATION_DECLARATION``.  No ROM bytes,
credentials, or machine-local paths are written into Git or the report; paths
are emitted relative to the repository root or reduced to their basename.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

# Register the canonical name first so the split modules'
# ``import scripts.qualification_runner as _entry`` resolves to this same
# object instead of a second copy.
_sys.modules.setdefault("scripts.qualification_runner", _sys.modules[__name__])


# The split modules' names are re-exported verbatim so the module surface
# (and every ``qualification_runner.<name>`` monkeypatch target) is unchanged.
from scripts.qualification_runner_allocation import (
    _UNRESOLVED_ALLOCATION_SUFFIX,  # noqa: F401  (retained facade attribute: runner._UNRESOLVED_ALLOCATION_SUFFIX)
    UnresolvedAllocationError,  # noqa: F401  (retained facade attribute: runner.UnresolvedAllocationError)
    _allocation_descriptor_path,  # noqa: F401  (retained facade attribute: runner._allocation_descriptor_path)
    _blocked_allocation,  # noqa: F401  (retained facade attribute: runner._blocked_allocation)
    _clear_unresolved_allocation,  # noqa: F401  (retained facade attribute: runner._clear_unresolved_allocation)
    _confirm_recorded_job_containment,  # noqa: F401  (retained facade attribute: runner._confirm_recorded_job_containment)
    _declared_host_lock,  # noqa: F401  (retained facade attribute: runner._declared_host_lock)
    _descriptor_lock_path,  # noqa: F401  (retained facade attribute: runner._descriptor_lock_path)
    _mark_allocation_unresolved,  # noqa: F401  (retained facade attribute: runner._mark_allocation_unresolved)
    _pin_declaration,  # noqa: F401  (retained facade attribute: runner._pin_declaration)
    _process_environ,  # noqa: F401  (retained facade attribute: runner._process_environ)
    _read_job_run_record,  # noqa: F401  (retained facade attribute: runner._read_job_run_record)
    _read_unresolved_allocation,  # noqa: F401  (retained facade attribute: runner._read_unresolved_allocation)
    _recorded_job_group_ownership,  # noqa: F401  (retained facade attribute: runner._recorded_job_group_ownership)
    _recorded_start_time_at_or_after,  # noqa: F401  (retained facade attribute: runner._recorded_start_time_at_or_after)
    _recover_allocation,  # noqa: F401  (retained facade attribute: runner._recover_allocation)
    _release_allocation,  # noqa: F401  (retained facade attribute: runner._release_allocation)
    _release_confirm_holder_tree,  # noqa: F401  (retained facade attribute: runner._release_confirm_holder_tree)
    _remove_allocation_state,  # noqa: F401  (retained facade attribute: runner._remove_allocation_state)
    _remove_launch_intent,  # noqa: F401  (retained facade attribute: runner._remove_launch_intent)
    _reserve_allocation,  # noqa: F401  (retained facade attribute: runner._reserve_allocation)
    _terminate_and_confirm,  # noqa: F401  (retained facade attribute: runner._terminate_and_confirm)
    _unresolved_allocation_path,  # noqa: F401  (retained facade attribute: runner._unresolved_allocation_path)
    _unresolved_allocation_present,  # noqa: F401  (retained facade attribute: runner._unresolved_allocation_present)
    _update_job_run_record,  # noqa: F401  (retained facade attribute: runner._update_job_run_record)
    _write_job_launch_intent,  # noqa: F401  (retained facade attribute: runner._write_job_launch_intent)
    _write_job_run_record,  # noqa: F401  (retained facade attribute: runner._write_job_run_record)
    _write_unresolved_allocation,  # noqa: F401  (retained facade attribute: runner._write_unresolved_allocation)
)
from scripts.qualification_runner_assets import (
    _asset_tree_immutability_problem,  # noqa: F401  (retained facade attribute: runner._asset_tree_immutability_problem)
    _AssetRequirement,  # noqa: F401  (retained facade attribute: runner._AssetRequirement)
    _build_evidence_producer_problems,  # noqa: F401  (retained facade attribute: runner._build_evidence_producer_problems)
    _canonical_asset_key,  # noqa: F401  (retained facade attribute: runner._canonical_asset_key)
    _fixture_requirements,  # noqa: F401  (retained facade attribute: runner._fixture_requirements)
    _immutable_asset_checks,  # noqa: F401  (retained facade attribute: runner._immutable_asset_checks)
    _load_bootstrap_module,  # noqa: F401  (retained facade attribute: runner._load_bootstrap_module)
    _load_native_build_evidence,  # noqa: F401  (retained facade attribute: runner._load_native_build_evidence)
    _load_versions_pins,  # noqa: F401  (retained facade attribute: runner._load_versions_pins)
    _native_build_evidence,  # noqa: F401  (retained facade attribute: runner._native_build_evidence)
    _native_build_evidence_check,  # noqa: F401  (retained facade attribute: runner._native_build_evidence_check)
    _native_build_fingerprint,  # noqa: F401  (retained facade attribute: runner._native_build_fingerprint)
    _native_source_digest,  # noqa: F401  (retained facade attribute: runner._native_source_digest)
    _required_asset_entries,  # noqa: F401  (retained facade attribute: runner._required_asset_entries)
    _sha1_of_file,  # noqa: F401  (retained facade attribute: runner._sha1_of_file)
    _symlinked_path_component,  # noqa: F401  (retained facade attribute: runner._symlinked_path_component)
    _verify_required_asset,  # noqa: F401  (retained facade attribute: runner._verify_required_asset)
    prerequisite_checks,  # noqa: F401  (retained facade attribute: runner.prerequisite_checks)
    validate_asset_inputs,  # noqa: F401  (retained facade attribute: runner.validate_asset_inputs)
)
from scripts.qualification_runner_cgroup import (
    _cgroup_cpu_quota_v1,  # noqa: F401  (retained facade attribute: runner._cgroup_cpu_quota_v1)
    _cgroup_cpu_quota_v2,  # noqa: F401  (retained facade attribute: runner._cgroup_cpu_quota_v2)
    _cgroup_descendant_counts,  # noqa: F401  (retained facade attribute: runner._cgroup_descendant_counts)
    _cgroup_memory_headroom,  # noqa: F401  (retained facade attribute: runner._cgroup_memory_headroom)
    _cgroup_memory_limit,  # noqa: F401  (retained facade attribute: runner._cgroup_memory_limit)
    _cgroup_relative_path,  # noqa: F401  (retained facade attribute: runner._cgroup_relative_path)
    _cgroup_sibling_competitors,  # noqa: F401  (retained facade attribute: runner._cgroup_sibling_competitors)
    _cgroup_subtree_populated,  # noqa: F401  (retained facade attribute: runner._cgroup_subtree_populated)
    _is_cgroup_directory,  # noqa: F401  (retained facade attribute: runner._is_cgroup_directory)
    _iter_cgroup_paths,  # noqa: F401  (retained facade attribute: runner._iter_cgroup_paths)
    _parse_cpu_max,  # noqa: F401  (retained facade attribute: runner._parse_cpu_max)
    _read_cgroup_facts,  # noqa: F401  (retained facade attribute: runner._read_cgroup_facts)
    _read_memory_facts,  # noqa: F401  (retained facade attribute: runner._read_memory_facts)
    _read_psi_cpu,  # noqa: F401  (retained facade attribute: runner._read_psi_cpu)
    parse_cpuset,  # noqa: F401  (retained facade attribute: runner.parse_cpuset)
)
from scripts.qualification_runner_cli import (
    _do_reserve,  # noqa: F401  (retained facade attribute: runner._do_reserve)
    _emit,  # noqa: F401  (retained facade attribute: runner._emit)
    _facts_payload,  # noqa: F401  (retained facade attribute: runner._facts_payload)
    build_parser,  # noqa: F401  (retained facade attribute: runner.build_parser)
    main,
)
from scripts.qualification_runner_command import (
    _CLEANUP_ACTIVE,  # noqa: F401  (retained facade attribute: runner._CLEANUP_ACTIVE)
    _DEFERRED_SIGNAL,  # noqa: F401  (retained facade attribute: runner._DEFERRED_SIGNAL)
    _JOB_LAUNCH_INTENT_NAME,  # noqa: F401  (retained facade attribute: runner._JOB_LAUNCH_INTENT_NAME)
    _JOB_OWNERSHIP_TOKEN_ENV,  # noqa: F401  (retained facade attribute: runner._JOB_OWNERSHIP_TOKEN_ENV)
    _JOB_RUN_RECORD_NAME,  # noqa: F401  (retained facade attribute: runner._JOB_RUN_RECORD_NAME)
    _LEFTOVER_OWNED_PIDS,  # noqa: F401  (retained facade attribute: runner._LEFTOVER_OWNED_PIDS)
    _OWNED_PROCESSES,  # noqa: F401  (retained facade attribute: runner._OWNED_PROCESSES)
    _PR_GET_CHILD_SUBREAPER,  # noqa: F401  (retained facade attribute: runner._PR_GET_CHILD_SUBREAPER)
    _PR_SET_CHILD_SUBREAPER,  # noqa: F401  (retained facade attribute: runner._PR_SET_CHILD_SUBREAPER)
    _PROC_ENUMERATION_FAILURES,  # noqa: F401  (retained facade attribute: runner._PROC_ENUMERATION_FAILURES)
    _SIGNAL_HANDLERS_INSTALLED,  # noqa: F401  (retained facade attribute: runner._SIGNAL_HANDLERS_INSTALLED)
    CommandContainment,
    JobOwnershipError,  # noqa: F401  (retained facade attribute: runner.JobOwnershipError)
    _adopted_descendants,  # noqa: F401  (retained facade attribute: runner._adopted_descendants)
    _adopted_orphan_pids,  # noqa: F401  (retained facade attribute: runner._adopted_orphan_pids)
    _await_no_token_owned_survivors,  # noqa: F401  (retained facade attribute: runner._await_no_token_owned_survivors)
    _begin_cleanup,  # noqa: F401  (retained facade attribute: runner._begin_cleanup)
    _child_subreaper_state,  # noqa: F401  (retained facade attribute: runner._child_subreaper_state)
    _clock_ticks_per_second,  # noqa: F401  (retained facade attribute: runner._clock_ticks_per_second)
    _command_timeout,  # noqa: F401  (retained facade attribute: runner._command_timeout)
    _contain_adopted_descendants,  # noqa: F401  (retained facade attribute: runner._contain_adopted_descendants)
    _direct_child_pids,  # noqa: F401  (retained facade attribute: runner._direct_child_pids)
    _drain_after_termination,  # noqa: F401  (retained facade attribute: runner._drain_after_termination)
    _end_cleanup,  # noqa: F401  (retained facade attribute: runner._end_cleanup)
    _foreign_sample_seconds,  # noqa: F401  (retained facade attribute: runner._foreign_sample_seconds)
    _install_signal_handlers,  # noqa: F401  (retained facade attribute: runner._install_signal_handlers)
    _libc_prctl,  # noqa: F401  (retained facade attribute: runner._libc_prctl)
    _note_proc_enumeration_failure,  # noqa: F401  (retained facade attribute: runner._note_proc_enumeration_failure)
    _posix_child_preexec,  # noqa: F401  (retained facade attribute: runner._posix_child_preexec)
    _proc_enumeration_failures,  # noqa: F401  (retained facade attribute: runner._proc_enumeration_failures)
    _process_group_members,  # noqa: F401  (retained facade attribute: runner._process_group_members)
    _process_group_options,  # noqa: F401  (retained facade attribute: runner._process_group_options)
    _qualification_timeout,  # noqa: F401  (retained facade attribute: runner._qualification_timeout)
    _read_proc_stat,  # noqa: F401  (retained facade attribute: runner._read_proc_stat)
    _register_owned,  # noqa: F401  (retained facade attribute: runner._register_owned)
    _reset_proc_enumeration_failures,  # noqa: F401  (retained facade attribute: runner._reset_proc_enumeration_failures)
    _set_child_subreaper,  # noqa: F401  (retained facade attribute: runner._set_child_subreaper)
    _sweep_leftover_owned_processes,  # noqa: F401  (retained facade attribute: runner._sweep_leftover_owned_processes)
    _terminate_owned_process,  # noqa: F401  (retained facade attribute: runner._terminate_owned_process)
    _terminate_owned_processes,  # noqa: F401  (retained facade attribute: runner._terminate_owned_processes)
    _terminate_pids,  # noqa: F401  (retained facade attribute: runner._terminate_pids)
    _terminate_process_group,  # noqa: F401  (retained facade attribute: runner._terminate_process_group)
    _timeout_partial_streams,  # noqa: F401  (retained facade attribute: runner._timeout_partial_streams)
    _token_owned_live_pids,  # noqa: F401  (retained facade attribute: runner._token_owned_live_pids)
    _unregister_owned,  # noqa: F401  (retained facade attribute: runner._unregister_owned)
    last_command_containment,  # noqa: F401  (retained facade attribute: runner.last_command_containment)
    run_command,  # noqa: F401  (retained facade attribute: runner.run_command)
)
from scripts.qualification_runner_declaration import (
    _cgroup_members_are_owned,  # noqa: F401  (retained facade attribute: runner._cgroup_members_are_owned)
    _descriptor_lease_status,  # noqa: F401  (retained facade attribute: runner._descriptor_lease_status)
    _effective_cpu_cores,  # noqa: F401  (retained facade attribute: runner._effective_cpu_cores)
    _load_allocation_descriptor,  # noqa: F401  (retained facade attribute: runner._load_allocation_descriptor)
    _pinned_descriptor_binding_error,  # noqa: F401  (retained facade attribute: runner._pinned_descriptor_binding_error)
    _reservation_owned_pids,  # noqa: F401  (retained facade attribute: runner._reservation_owned_pids)
    validate_declaration,  # noqa: F401  (retained facade attribute: runner.validate_declaration)
)
from scripts.qualification_runner_facts import (
    CheckResult,  # noqa: F401  (retained facade attribute: runner.CheckResult)
    RunnerFacts,  # noqa: F401  (retained facade attribute: runner.RunnerFacts)
    _is_sha1,  # noqa: F401  (retained facade attribute: runner._is_sha1)
    _is_sha256,  # noqa: F401  (retained facade attribute: runner._is_sha256)
    _probe_shm,  # noqa: F401  (retained facade attribute: runner._probe_shm)
    _result,  # noqa: F401  (retained facade attribute: runner._result)
    collect_facts,  # noqa: F401  (retained facade attribute: runner.collect_facts)
)
from scripts.qualification_runner_host import (
    _close_held_lease_descriptors,  # noqa: F401  (retained facade attribute: runner._close_held_lease_descriptors)
    _descriptor_is_private,  # noqa: F401  (retained facade attribute: runner._descriptor_is_private)
    _directory_is_private,  # noqa: F401  (retained facade attribute: runner._directory_is_private)
    _flock_holder_pids,  # noqa: F401  (retained facade attribute: runner._flock_holder_pids)
    _foreign_process_affinity,  # noqa: F401  (retained facade attribute: runner._foreign_process_affinity)
    _host_cpu_totals,  # noqa: F401  (retained facade attribute: runner._host_cpu_totals)
    _iso_now,  # noqa: F401  (retained facade attribute: runner._iso_now)
    _lock_identity,  # noqa: F401  (retained facade attribute: runner._lock_identity)
    _lock_identity_matches,  # noqa: F401  (retained facade attribute: runner._lock_identity_matches)
    _lock_owned_by_this_process,  # noqa: F401  (retained facade attribute: runner._lock_owned_by_this_process)
    _measure_competing_cpu_cores,  # noqa: F401  (retained facade attribute: runner._measure_competing_cpu_cores)
    _observe_flock_holders,  # noqa: F401  (retained facade attribute: runner._observe_flock_holders)
    _observed_affinity,  # noqa: F401  (retained facade attribute: runner._observed_affinity)
    _observed_task_affinities,  # noqa: F401  (retained facade attribute: runner._observed_task_affinities)
    _path_within,  # noqa: F401  (retained facade attribute: runner._path_within)
    _pid_alive,  # noqa: F401  (retained facade attribute: runner._pid_alive)
    _pid_exists,  # noqa: F401  (retained facade attribute: runner._pid_exists)
    _pid_is_zombie,  # noqa: F401  (retained facade attribute: runner._pid_is_zombie)
    _process_ancestor_pids,  # noqa: F401  (retained facade attribute: runner._process_ancestor_pids)
    _process_start_time,  # noqa: F401  (retained facade attribute: runner._process_start_time)
    _process_tree_pids,  # noqa: F401  (retained facade attribute: runner._process_tree_pids)
    _read_text,  # noqa: F401  (retained facade attribute: runner._read_text)
    _reap_zombie,  # noqa: F401  (retained facade attribute: runner._reap_zombie)
    _resolve_declared_path,  # noqa: F401  (retained facade attribute: runner._resolve_declared_path)
    _sha256_of_file,  # noqa: F401  (retained facade attribute: runner._sha256_of_file)
)
from scripts.qualification_runner_model import (
    _ABSOLUTE_PATH_RE,  # noqa: F401  (retained facade attribute: runner._ABSOLUTE_PATH_RE)
    _CHILD_TERMINATION_GRACE_SECONDS,  # noqa: F401  (retained facade attribute: runner._CHILD_TERMINATION_GRACE_SECONDS)
    _CPU_BINDING_MECHANISMS,  # noqa: F401  (retained facade attribute: runner._CPU_BINDING_MECHANISMS)
    _DECLARATION_ENV,  # noqa: F401  (retained facade attribute: runner._DECLARATION_ENV)
    _DEFAULT_FOREIGN_CPU_CORES_TOLERANCE,  # noqa: F401  (retained facade attribute: runner._DEFAULT_FOREIGN_CPU_CORES_TOLERANCE)
    _DEFAULT_FOREIGN_SAMPLE_SECONDS,  # noqa: F401  (retained facade attribute: runner._DEFAULT_FOREIGN_SAMPLE_SECONDS)
    _DEFAULT_PROBE_TIMEOUT_SECONDS,  # noqa: F401  (retained facade attribute: runner._DEFAULT_PROBE_TIMEOUT_SECONDS)
    _DEFAULT_QUALIFICATION_TIMEOUT_SECONDS,  # noqa: F401  (retained facade attribute: runner._DEFAULT_QUALIFICATION_TIMEOUT_SECONDS)
    _DESCRIPTOR_VERSION,  # noqa: F401  (retained facade attribute: runner._DESCRIPTOR_VERSION)
    _EXTENSION_BACKED_MODULES,  # noqa: F401  (retained facade attribute: runner._EXTENSION_BACKED_MODULES)
    _FOREIGN_SAMPLE_ENV,  # noqa: F401  (retained facade attribute: runner._FOREIGN_SAMPLE_ENV)
    _HELD_LEASE_FDS,  # noqa: F401  (retained facade attribute: runner._HELD_LEASE_FDS)
    _NATIVE_BUILD_EVIDENCE_PROCEDURE,  # noqa: F401  (retained facade attribute: runner._NATIVE_BUILD_EVIDENCE_PROCEDURE)
    _NATIVE_BUILD_EVIDENCE_VERSION,  # noqa: F401  (retained facade attribute: runner._NATIVE_BUILD_EVIDENCE_VERSION)
    _NATIVE_PROBE,  # noqa: F401  (retained facade attribute: runner._NATIVE_PROBE)
    _PROBE_TIMEOUT_ENV,  # noqa: F401  (retained facade attribute: runner._PROBE_TIMEOUT_ENV)
    _QUALIFICATION_TIMEOUT_ENV,  # noqa: F401  (retained facade attribute: runner._QUALIFICATION_TIMEOUT_ENV)
    _RESERVATION_MECHANISMS,  # noqa: F401  (retained facade attribute: runner._RESERVATION_MECHANISMS)
    _RUNTIME_MODULES,  # noqa: F401  (retained facade attribute: runner._RUNTIME_MODULES)
    _SHA1_RE,  # noqa: F401  (retained facade attribute: runner._SHA1_RE)
    _SHA256_RE,  # noqa: F401  (retained facade attribute: runner._SHA256_RE)
    _TIMEOUT_RETURNCODE,  # noqa: F401  (retained facade attribute: runner._TIMEOUT_RETURNCODE)
    SCHEMA_VERSION,  # noqa: F401  (retained facade attribute: runner.SCHEMA_VERSION)
)
from scripts.qualification_runner_report import (
    _PATH_FRAGMENT_RE,  # noqa: F401  (retained facade attribute: runner._PATH_FRAGMENT_RE)
    _collect_redactions,  # noqa: F401  (retained facade attribute: runner._collect_redactions)
    _holder_is_owned,  # noqa: F401  (retained facade attribute: runner._holder_is_owned)
    _job_directory,  # noqa: F401  (retained facade attribute: runner._job_directory)
    _populate_job_filesystem_facts,  # noqa: F401  (retained facade attribute: runner._populate_job_filesystem_facts)
    _prepare_job_directory,  # noqa: F401  (retained facade attribute: runner._prepare_job_directory)
    _redact_payload,  # noqa: F401  (retained facade attribute: runner._redact_payload)
    _redact_text,  # noqa: F401  (retained facade attribute: runner._redact_text)
    _sanitize_path,  # noqa: F401  (retained facade attribute: runner._sanitize_path)
    _scrub_absolute_paths,  # noqa: F401  (retained facade attribute: runner._scrub_absolute_paths)
    load_declaration,  # noqa: F401  (retained facade attribute: runner.load_declaration)
    overall_status,  # noqa: F401  (retained facade attribute: runner.overall_status)
    render_text,  # noqa: F401  (retained facade attribute: runner.render_text)
)
from scripts.qualification_runner_reservation import (
    _usable_memory_bytes,  # noqa: F401  (retained facade attribute: runner._usable_memory_bytes)
    _verify_cgroup_reservation,  # noqa: F401  (retained facade attribute: runner._verify_cgroup_reservation)
    _verify_cpuset_reservation,  # noqa: F401  (retained facade attribute: runner._verify_cpuset_reservation)
    _verify_dedicated_reservation,  # noqa: F401  (retained facade attribute: runner._verify_dedicated_reservation)
    evaluate_reservation,  # noqa: F401  (retained facade attribute: runner.evaluate_reservation)
    evaluate_resources,  # noqa: F401  (retained facade attribute: runner.evaluate_resources)
)

# Facade-owned mutable seam: ``run_command`` writes it and
# ``last_command_containment`` reads it, both through ``_entry``.
_LAST_COMMAND_CONTAINMENT: CommandContainment | None = None


if __name__ == "__main__":
    raise SystemExit(main())
