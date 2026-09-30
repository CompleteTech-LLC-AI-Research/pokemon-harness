"""Row budget overruns are classified, and the command line is argv, not shell. (#84; split from tests/test_timed_frame_admission.py for #122.)

Pure relocation: every test and helper definition is byte-identical at the AST
level; only the module file changed.
"""

from __future__ import annotations

import sys

import pytest

from scripts import timed_frame_admission as admission
from tests._timed_frame_admission_support import (
    ALLOWED_CPUS,
    FakeClock,
    SeriesReader,
    passing_wrapper,
    quiet_series,
    write_allocation,
)

# --- a row that overruns its budget is classified, not dropped --------------
#
# §5.1 leans on the failure record being preserved: a row's budget is part of
# its declared identity, and "an observed failure is never converted into a
# pass by a longer wait" (§3).  An overrun that escaped as an exception would
# produce *no run record at all*, so the row would vanish from the matrix
# instead of being reported.  These rows pin the classifying behaviour.


def test_a_row_that_overruns_its_budget_is_recorded_as_a_failure_not_an_abort(tmp_path):
    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)

    record = runner.run_wrapper(
        commands=[
            runner.RowCommand(
                row="row-a",
                command=f"{sys.executable} -c 'import time; time.sleep(30)'",
                timeout_seconds=1.0,
            )
        ],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    # The record exists, names the row, and reports the overrun honestly.
    assert record["row_runs"], "an overrun must still produce a run record"
    run = record["row_runs"][0]
    assert run["timed_out"] is True
    assert run["returncode"] == runner.ROW_TIMEOUT_RETURN_CODE
    assert run["failed"] is True, "an overrun is a failure, never a pass"
    assert run["command"].endswith("time.sleep(30)'")
    assert "budget" in run["stderr"]
    # It is classified in place of the row, and never re-run.
    assert len(record["row_runs"]) == 1
    assert record["rows"][0]["state"] == admission.S6_FAILED_IN_WINDOW
    assert record["rows"][0]["failed"] is True
    assert record["rows"][0]["passed"] is False
    assert record["matrix_success"] is False


def test_a_row_that_overruns_the_wrapper_budget_is_also_classified(tmp_path):
    """The wrapper-level ``--timeout-seconds`` budget behaves identically."""

    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)

    record = runner.run_wrapper(
        commands=[
            runner.RowCommand(
                row="row-a", command=f"{sys.executable} -c 'import time; time.sleep(30)'"
            )
        ],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        timeout_seconds=1.0,
        wrapper=wrapper,
    )

    assert record["row_runs"][0]["timed_out"] is True
    assert record["rows"][0]["state"] == admission.S6_FAILED_IN_WINDOW
    assert record["matrix_success"] is False


def test_a_row_within_its_budget_is_not_marked_as_timed_out(tmp_path):
    """The positive control: the sentinel must not label every row an overrun."""

    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)

    record = runner.run_wrapper(
        commands=[runner.RowCommand(row="row-a", command=f"{sys.executable} -c pass")],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        timeout_seconds=60.0,
        wrapper=wrapper,
    )

    assert record["row_runs"][0]["timed_out"] is False
    assert record["matrix_success"] is True


def test_a_command_that_exits_with_the_sentinel_code_is_not_reported_as_an_overrun(tmp_path):
    """The ``timed_out`` flag is authoritative, not the exit status."""

    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)

    record = runner.run_wrapper(
        commands=[
            runner.RowCommand(
                row="row-a",
                command=f"{sys.executable} -c 'raise SystemExit({runner.ROW_TIMEOUT_RETURN_CODE})'",
            )
        ],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    assert record["row_runs"][0]["returncode"] == runner.ROW_TIMEOUT_RETURN_CODE
    assert record["row_runs"][0]["timed_out"] is False, (
        "a command that exited with the sentinel status did not overrun its budget"
    )
    assert record["row_runs"][0]["failed"] is True


# --- F4: the command line is argv, not shell -------------------------------


def test_a_row_command_is_split_into_argv_and_never_run_through_a_shell(tmp_path):
    """F4: the documented command form must match what is executed.

    Shell metacharacters are literal arguments, so a row cannot acquire pipes,
    redirection, or variable expansion that its record does not show.  The row
    below would print ``expanded`` if a shell were involved; as argv it prints
    the literal text instead.
    """

    from scripts import timed_frame_runner as runner

    clock = FakeClock()
    reader = SeriesReader(clock, quiet_series(40))
    wrapper = passing_wrapper(clock, reader, ["row-a"], max_replacement_attempts=0)
    literal = "${HOME:-expanded}"
    # The program prints its first argv element, and the literal metacharacters
    # are passed as a *separate* argv element so no shell-like quoting is
    # needed.  A shell would expand this to the reader's HOME; argv must not.
    script = "import sys; print(sys.argv[1])"

    record = runner.run_wrapper(
        commands=[
            runner.RowCommand(
                row="row-a",
                command=f'{sys.executable} -c "{script}" {literal}',
            )
        ],
        allocation_path=write_allocation(tmp_path),
        allowed_cpus=ALLOWED_CPUS,
        wrapper=wrapper,
    )

    run = record["row_runs"][0]
    assert run["returncode"] == 0
    assert run["stdout"].strip() == literal, (
        "a shell would have expanded this argument; argv execution must not"
    )


def test_the_command_help_text_does_not_promise_a_shell(capsys):
    """F4: the documented form and the executed form must agree."""

    with pytest.raises(SystemExit):
        admission._main(["--help"])
    text = capsys.readouterr().out

    assert "argv-style" in text
    assert "<shell command>" not in text, "the help must not claim a shell"
