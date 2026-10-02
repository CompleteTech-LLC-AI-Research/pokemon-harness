"""Explicit profiling wrapper around the unchanged production MCP main."""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from pokered_harness.qualification_profile import (
    external_directory,
    owner_cpu,
    runtime_identity,
    source_identity,
)

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def profiles(directory: Path, enabled: bool):
    """Observe main-thread Python calls only; owner-worker execution is opaque.

    Python 3.12 simultaneous cProfile instances compete for a monitoring tool
    slot. Do not intercept Thread.run: instrumentation must never skip a worker.
    Process CPU records include threads, but cannot attribute their call stacks.
    """
    errors = []
    if not enabled:
        yield errors
        return
    profile = cProfile.Profile()
    profile.enable()
    try:
        yield errors
    finally:
        profile.disable()
        try:
            profile.dump_stats(str(directory / "main.pstats"))
        except (OSError, ValueError) as exc:
            errors.append(type(exc).__name__)


def run_owner(
    directory: Path,
    mode: str,
    owners: tuple[int, ...],
    *,
    cprofile=False,
    serve=None,
    expected_head=None,
):
    if any(type(owner) is not int for owner in owners) or owners not in ((0,), (1,), (0, 1)):
        raise ValueError("unknown owner role")
    if serve is None and expected_head is None:
        raise ValueError("production owner profile requires exact source commit")
    source = source_identity(ROOT, expected_head) if expected_head else {"status": "unit_control"}
    directory = external_directory(directory, ROOT)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    identity = runtime_identity(ROOT, mode)
    identity.update(
        {
            "owner_ids": list(owners),
            "process_cpu_identity": owner_cpu(os.getpid()),
            "role": "local_pair" if len(owners) == 2 else "tcp_owner",
            "wall_start_ns": time.perf_counter_ns(),
            "process_cpu_start_ns": time.process_time_ns(),
            "cprofile_enabled": cprofile,
            "profile_scope": "main-thread Python calls only; owner-worker and native instruction attribution unavailable",
            "source_identity": source,
        }
    )
    (directory / "owner-start.json").write_text(json.dumps(identity, indent=2) + "\n")
    if serve is None:
        from pokered_harness.mcp_server import main

        serve = main
    failure = None
    errors = []
    try:
        with profiles(directory, cprofile) as errors:
            serve()
    except BaseException as exc:
        failure = exc
        raise
    finally:
        report = {
            "status": "RETURNED" if failure is None else "RAISED",
            "error_type": type(failure).__name__ if failure is not None else None,
            "wall_end_ns": time.perf_counter_ns(),
            "process_cpu_end_ns": time.process_time_ns(),
            "profile_errors": errors,
        }
        try:
            report["runtime_identity_after"] = runtime_identity(ROOT, mode)
            (directory / "owner-end.json").write_text(json.dumps(report, indent=2) + "\n")
        except Exception as evidence_error:
            if failure is None:
                raise
            failure.add_note(f"owner profile evidence failed: {type(evidence_error).__name__}")
    if errors:
        raise RuntimeError("owner cProfile output incomplete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", choices=("source", "cython"), required=True)
    parser.add_argument("--owners", choices=("0", "1", "0,1"), required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--cprofile", action="store_true")
    arguments = parser.parse_args()
    run_owner(
        arguments.output,
        arguments.runtime,
        tuple(int(value) for value in arguments.owners.split(",")),
        cprofile=arguments.cprofile,
        expected_head=arguments.expected_head,
    )


if __name__ == "__main__":
    main()
