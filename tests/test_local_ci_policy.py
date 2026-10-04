"""Static policy checks for the reproducible local CI runner.

Most rows inspect the runner and workflow text.  Two of them instead *execute*
the runner against a stub ``python`` that records argument vectors, so that
exit-status propagation is measured rather than inferred; the stub keeps that
run inside the unit tier instead of installing dependencies, building a wheel,
or running the bounded production gate.
"""

# Preserve the baseline pytest node order; suppress only import sorting for this facade.
from __future__ import annotations  # noqa: I001

from ._local_ci_policy_ruff_selection import (
    test_main_ruff_lanes_cover_every_test_file,  # noqa: F401
    test_main_ruff_lane_tests_directory_covers_every_test_file_on_disk,  # noqa: F401
    test_main_ruff_lanes_cover_every_script_file,  # noqa: F401
    test_main_ruff_lane_scripts_directory_covers_every_script_file_on_disk,  # noqa: F401
    test_excluded_fixture_producer_still_matches_the_manifest_sha1,  # noqa: F401
    test_runner_ruff_file_lists_match_the_workflow_exactly,  # noqa: F401
    test_ruff_invocation_parser_keeps_options_that_change_the_lint_set,  # noqa: F401
)

from ._local_ci_policy_runner import (
    test_hosted_job_requires_explicit_public_visibility,  # noqa: F401
    test_local_runner_is_a_bash_script,  # noqa: F401
    test_local_runner_copies_every_workflow_check_command,  # noqa: F401
    test_local_runner_retains_external_evidence,  # noqa: F401
    test_local_runner_requires_supported_active_virtualenv,  # noqa: F401
    test_local_runner_has_no_hosted_or_paid_service_dependency,  # noqa: F401
)

from ._local_ci_policy_ruff_effective import (
    test_main_ruff_lanes_do_not_narrow_their_own_verdict,  # noqa: F401
    test_each_check_probe_detects_its_own_rule,  # noqa: F401
    test_matrix_benchmark_is_linted_by_every_main_ruff_lane,  # noqa: F401
    test_benchmark_cannot_opt_out_of_linting_with_in_file_suppression,  # noqa: F401
)

from ._local_ci_policy_ruff_directives import (
    test_directive_patterns_track_the_installed_ruff,  # noqa: F401
    test_no_lane_file_opts_out_of_linting_with_a_blanket_directive,  # noqa: F401
)

from ._local_ci_policy_runner import (
    test_main_ruff_lanes_run_in_executable_control_flow,  # noqa: F401
    test_main_ruff_lanes_stop_the_local_runner_on_failure,  # noqa: F401
    test_workflow_lint_step_has_no_disabled_shell_control_flow,  # noqa: F401
    test_workflow_lint_step_cannot_tolerate_a_failure,  # noqa: F401
    test_workflow_runs_the_lint_step_unconditionally,  # noqa: F401
)

from ._local_ci_policy_paths import (
    ROOT,  # noqa: F401
    RUNNER,  # noqa: F401
    WORKFLOW,  # noqa: F401
)

from ._local_ci_policy_ruff_selection import (
    _main_lane,  # noqa: F401
    _ruff_excluded_patterns,  # noqa: F401
    _ruff_invocations,  # noqa: F401
    _ruff_lint_resolved_files,  # noqa: F401
)
