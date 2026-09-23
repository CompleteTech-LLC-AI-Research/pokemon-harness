"""CLI parser, payload assembly and entry point.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.qualification_runner_allocation import (
    UnresolvedAllocationError,
    _blocked_allocation,
    _mark_allocation_unresolved,
    _pin_declaration,
    _recover_allocation,
    _release_allocation,
    _remove_launch_intent,
    _reserve_allocation,
    _update_job_run_record,
    _write_job_launch_intent,
)
from scripts.qualification_runner_command import (
    _JOB_OWNERSHIP_TOKEN_ENV,
    _JOB_RUN_RECORD_NAME,
    JobOwnershipError,
    _install_signal_handlers,
    _qualification_timeout,
    last_command_containment,
)
from scripts.qualification_runner_declaration import validate_declaration
from scripts.qualification_runner_facts import CheckResult, RunnerFacts, _result
from scripts.qualification_runner_host import _directory_is_private
from scripts.qualification_runner_model import _DECLARATION_ENV, SCHEMA_VERSION
from scripts.qualification_runner_report import (
    _collect_redactions,
    _job_directory,
    _populate_job_filesystem_facts,
    _prepare_job_directory,
    _redact_payload,
    _sanitize_path,
    load_declaration,
    overall_status,
    render_text,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a declared qualification-runner allocation."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify declaration and prerequisites")
    mode.add_argument("--report", action="store_true", help="print observed host facts only")
    mode.add_argument("--setup", action="store_true", help="prepare the private job directory")
    mode.add_argument("--reserve", action="store_true", help="reserve and hold the allocation")
    mode.add_argument("--release", action="store_true", help="release an owned lease")
    mode.add_argument("--recover", action="store_true", help="remove only stale lease state")
    parser.add_argument("--declaration", type=Path, default=None)
    parser.add_argument("--job-dir", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="emit a structured JSON report")
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=None,
        help="deadline in seconds for --run, separate from the prerequisite timeout",
    )
    parser.add_argument(
        "--run",
        nargs=argparse.REMAINDER,
        default=[],
        help="command to run under a held lease (must be last)",
    )
    return parser


def _emit(payload: dict[str, Any], args: argparse.Namespace) -> None:
    print(json.dumps(payload, indent=2) if args.json else render_text(payload))


def _facts_payload(facts: RunnerFacts, repo_root: Path) -> dict[str, Any]:
    """Serialize facts for a report, keeping machine-local paths out of the JSON."""

    payload = asdict(facts)
    payload["shm_path"] = _sanitize_path(facts.shm_path, repo_root)
    for key in ("job_tmp_path", "job_evidence_path"):
        value = payload.get(key)
        if value is not None:
            payload[key] = _sanitize_path(value, repo_root)
    return payload


def _do_reserve(
    args: argparse.Namespace,
    declaration: dict[str, Any],
    declaration_path: Path,
    repo_root: Path,
    facts: RunnerFacts,
) -> tuple[str, str, dict[str, Any]]:
    admission = validate_declaration(declaration)
    admission_docs = [asdict(item) for item in admission]
    if overall_status(admission) != "ok":
        return (
            "fail",
            "the declaration failed prerequisite admission; refusing to reserve or run",
            {"checks": admission_docs, "facts": _facts_payload(facts, repo_root)},
        )
    prerequisites = _entry.prerequisite_checks(declaration, repo_root)
    prerequisites_docs = [asdict(item) for item in prerequisites]
    if overall_status(prerequisites) != "ok":
        return (
            "fail",
            "runtime and asset prerequisites failed admission; refusing to reserve or run",
            {
                "checks": [*admission_docs, *prerequisites_docs],
                "facts": _facts_payload(facts, repo_root),
            },
        )
    admission_checks = [*admission_docs, *prerequisites_docs]

    # Resolve the job directory before admission so disk checks measure the
    # filesystems the job will actually write to, not the caller's TMPDIR.
    job_dir = _job_directory(args, declaration, repo_root)
    _populate_job_filesystem_facts(facts, job_dir)
    try:
        descriptor_path, _descriptor, _lock_fd = _reserve_allocation(
            declaration, repo_root, job_dir, facts
        )
        digest = _pin_declaration(declaration_path, declaration, job_dir, descriptor_path)
    except UnresolvedAllocationError as exc:
        # A prior lease on this host lock was never proven contained.  Its flock
        # died with the holder, but its host-wide record still forbids admitting
        # a new lease until containment is proven or an operator recovers it.
        return (
            "blocked",
            str(exc),
            {"checks": admission_checks, "facts": _facts_payload(facts, repo_root)},
        )
    except (OSError, ValueError) as exc:
        return (
            "fail",
            f"could not reserve the allocation: {exc}",
            {"checks": admission_checks, "facts": _facts_payload(facts, repo_root)},
        )
    extra = {
        "job_dir": _sanitize_path(job_dir, repo_root),
        "descriptor": _sanitize_path(descriptor_path, repo_root),
        "descriptor_sha256": digest,
    }
    if not args.run:
        return (
            "blocked",
            (
                "descriptor written and pinned; rerun with --run <command> to hold the lease "
                "while the qualification job executes (a bare reserve releases the lease)"
            ),
            {
                **extra,
                "checks": admission_checks,
                "facts": _facts_payload(facts, repo_root),
            },
        )

    # The prerequisite probes above can run for minutes and can spawn work that
    # competes for this host, so observations taken before the lease was held
    # are stale by admission time.  Recollect them *while holding the lease*
    # and admit against what is true now, not against what was true earlier.
    admitted_facts = _entry.collect_facts(repo_root)
    _populate_job_filesystem_facts(admitted_facts, job_dir)
    resources = _entry.evaluate_resources(declaration, admitted_facts, repo_root)
    resource_docs = [asdict(item) for item in resources]
    # The report must carry the exact facts admission used, not the pre-lease
    # sample: those can differ (e.g. affinity narrowed while waiting for the
    # lease), and reporting the stale sample would misrepresent the allocation.
    admitted_view = _facts_payload(admitted_facts, repo_root)
    complete_checks = [*admission_checks, *resource_docs]
    if overall_status(resources) != "ok":
        _release_allocation(declaration, repo_root)
        return (
            "fail",
            "the observed allocation failed verification; refusing to launch the qualification job",
            {**extra, "checks": complete_checks, "facts": admitted_view},
        )

    command = list(args.run)
    # The per-launch token is durable ownership evidence: only this job and its
    # descendants can carry it, so a later recovery can tell them apart from an
    # unrelated process that happens to reuse the recorded pid or group id.
    job_token = secrets.token_hex(16)
    child_env = dict(os.environ)
    for name, child in (
        ("TMPDIR", job_dir / "tmp"),
        ("POKERED_QUALIFICATION_JOB_DIR", job_dir),
        ("POKERED_QUALIFICATION_EVIDENCE_DIR", job_dir / "evidence"),
    ):
        child.mkdir(parents=True, exist_ok=True)
        child_env[name] = str(child)
    child_env[_JOB_OWNERSHIP_TOKEN_ENV] = job_token

    # Durable job ownership is written before the command runs, so a holder that
    # dies mid-run still leaves a verifiable record of what it launched.
    # The host-wide record is persisted *before* the command is spawned so that
    # an abrupt holder death (SIGKILL, which cannot run any handler) still leaves
    # the exclusion in force; ``_release_allocation`` clears it only after
    # containment is positively confirmed.  Persisting it is mandatory: if the
    # record cannot be written (for example a writable lock file inside an
    # unwritable directory), the flock alone would not exclude a later holder,
    # so refuse to launch and release the unused lease instead of running work
    # whose death could not be contained.
    exclusion_record = _mark_allocation_unresolved(
        descriptor_path,
        "an allocation is active on this host lock; it may be released only after its "
        "launched work is proven contained",
    )
    if exclusion_record is None:
        release_status, release_message = _release_allocation(declaration, repo_root)
        trailer = (
            ""
            if release_status == "ok"
            else f"; the unused lease could not be released ({release_message})"
        )
        return (
            "fail",
            "the host-wide unresolved-allocation record could not be written, so a later "
            "holder could be admitted over this job's uncontained work; refusing to launch "
            "the qualification command" + trailer,
            {**extra, "checks": complete_checks, "facts": admitted_view},
        )
    try:
        _write_job_launch_intent(job_dir, command)
    except JobOwnershipError as exc:
        # Nothing was spawned, so the intent, if a partial write landed, does
        # not describe anything running and must not block the clean release.
        _remove_launch_intent(job_dir)
        _release_allocation(declaration, repo_root)
        return (
            "fail",
            f"the qualification command was not launched: {exc}",
            {**extra, "checks": complete_checks, "facts": admitted_view},
        )

    def _record_start(process: subprocess.Popen[str]) -> None:
        _entry._write_job_run_record(
            job_dir, process, containment_confirmed=False, job_token=job_token
        )

    try:
        process = _entry.run_command(
            command,
            repo_root,
            timeout=_qualification_timeout(getattr(args, "run_timeout", None)),
            env=child_env,
            on_start=_record_start,
        )
        if not (job_dir / _JOB_RUN_RECORD_NAME).is_file():
            # ``run_command`` reports a spawn that never happened (for example a
            # missing executable) as return code 127 only after proving no
            # descendant exists.  No process was created, so the recorded launch
            # intent must not keep the host blocked: remove it and release the
            # unused lease instead of stranding capacity behind a phantom job.
            _remove_launch_intent(job_dir)
            release_status, release_message = _release_allocation(declaration, repo_root)
            detail = (process.stderr or "").strip()
            trailer = (
                ""
                if release_status == "ok"
                else f"; the lease could not be released ({release_message})"
            )
            return (
                "fail",
                "the qualification command was not launched; no process was created "
                f"(exit {process.returncode})" + (f": {detail}" if detail else "") + trailer,
                {**extra, "checks": complete_checks, "facts": admitted_view},
            )
        containment = last_command_containment()
        containment_proven = containment is not None and containment.proven
        _update_job_run_record(job_dir, containment_confirmed=containment_proven)
        if process.stdout or process.stderr:
            # The child's streams are retained in the report rather than echoed
            # to this process's stdout, so a ``--json`` report stays a single
            # parseable document and the streams are sanitized by the same
            # redaction pass as the rest of the payload (absolute paths never
            # leak and can never prepend raw text to the JSON).
            extra = {
                **extra,
                "command_stdout": process.stdout or "",
                "command_stderr": process.stderr or "",
            }
        if not containment_proven:
            detail = (
                containment.detail
                if containment is not None
                else "the command's containment outcome was not observed"
            )
            status, message = _blocked_allocation(
                descriptor_path,
                "the leased command's descendant containment could not be proven "
                f"({detail}); the lease is kept because the allocation may still be in use",
            )
            return status, message, {**extra, "checks": complete_checks, "facts": admitted_view}
        release_status, _release_message = _release_allocation(declaration, repo_root)
        if release_status != "ok":
            return (
                "fail",
                f"leased command exited {process.returncode}; {_release_message}",
                {**extra, "checks": complete_checks, "facts": admitted_view},
            )
        status = "ok" if process.returncode == 0 else "fail"
        return (
            status,
            f"leased command exited {process.returncode}",
            {**extra, "checks": complete_checks, "facts": admitted_view},
        )
    except JobOwnershipError as exc:
        # The command was spawned but could not be attributed to this lease, so
        # ``run_command`` already tore it down.  The lease is kept because the
        # launch intent remains and no containment proof exists for it.  The
        # unresolved record persists that exclusion beyond this process's exit.
        status, message = _blocked_allocation(
            descriptor_path, f"the launched command could not be recorded: {exc}"
        )
        return status, message, {**extra, "checks": complete_checks, "facts": admitted_view}
    except BaseException as exc:
        # A cancellation (SIGINT/SIGTERM raises ``SystemExit`` out of the run) or
        # any other error can end this process while a spawned command's
        # containment is unproven.  The flock dies with the process, so persist
        # the host-wide exclusion first; a later reservation then refuses to
        # admit until containment is proven or an operator recovers the lease.
        _mark_allocation_unresolved(
            descriptor_path,
            "the leased command could not be finalized before the run ended "
            f"({type(exc).__name__}: {exc}); the lease is kept because the allocation "
            "may still be in use",
        )
        raise


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _install_signal_handlers()
    repo_root = args.repo_root.resolve()
    facts = _entry.collect_facts(repo_root)

    mode = "report"
    for candidate in ("check", "setup", "reserve", "release", "recover"):
        if getattr(args, candidate):
            mode = candidate
            break
    payload: dict[str, Any] = {
        "mode": mode,
        "declaration_version": SCHEMA_VERSION,
        "repo_root": _sanitize_path(repo_root, repo_root),
        "checks": [],
        "facts": _facts_payload(facts, repo_root),
        "overall": "report" if mode == "report" else "blocked",
    }

    if mode == "report":
        payload = _redact_payload(payload, _collect_redactions(repo_root, None, None))
        _emit(payload, args)
        return 0

    declaration_path = args.declaration or (
        Path(os.environ[_DECLARATION_ENV]) if os.environ.get(_DECLARATION_ENV) else None
    )
    checks: list[CheckResult] = []
    declaration: dict[str, Any] | None = None
    if declaration_path is None:
        payload["message"] = (
            "no declaration provided; set --declaration or "
            f"{_DECLARATION_ENV}. Actual capacity requires an operator-owned "
            "allocation (see issue #85)."
        )
    else:
        declaration, error = load_declaration(declaration_path)
        payload["declaration"] = _sanitize_path(declaration_path, repo_root)
        if declaration is None:
            payload["message"] = error
        elif mode == "check":
            _populate_job_filesystem_facts(facts, _job_directory(args, declaration, repo_root))
            checks.extend(validate_declaration(declaration))
            if overall_status(checks) == "ok":
                checks.extend(_entry.evaluate_resources(declaration, facts, repo_root))
                checks.extend(_entry.prerequisite_checks(declaration, repo_root))
        elif mode == "setup":
            job_dir = _job_directory(args, declaration, repo_root)
            try:
                _prepare_job_directory(job_dir)
                ready = True
            except OSError as exc:
                ready = False
                payload["message"] = f"could not prepare the job directory: {exc}"
            checks.append(
                _result(
                    "job-directory",
                    "ok" if ready and _directory_is_private(job_dir) else "fail",
                    "owner-only private job directory",
                    job_dir.name,
                    "private per-job directory prepared"
                    if ready
                    else "private per-job directory could not be prepared",
                )
            )
            payload["job_dir"] = _sanitize_path(job_dir, repo_root)
            checks.extend(_entry.prerequisite_checks(declaration, repo_root))
            payload["native"] = {
                result.name: result.observed
                for result in checks
                if result.name in {"native-build-inputs", "native-runtime-fingerprint"}
            }
        elif mode == "reserve":
            status, message, extra = _do_reserve(
                args, declaration, declaration_path, repo_root, facts
            )
            payload["overall"] = status
            payload["message"] = message
            # ``extra`` carries the admission snapshot and the complete check
            # results; retain the pre-lease observation separately so a reader
            # can tell what admission actually saw from what was true earlier.
            payload["initial_facts"] = _facts_payload(facts, repo_root)
            payload.update(extra)
            payload = _redact_payload(
                payload, _collect_redactions(repo_root, declaration, declaration_path)
            )
            _emit(payload, args)
            return 0 if status == "ok" else 1
        elif mode == "release":
            status, message = _release_allocation(declaration, repo_root)
            payload["message"] = message
            payload["overall"] = status
        elif mode == "recover":
            status, message = _recover_allocation(declaration, repo_root)
            payload["message"] = message
            payload["overall"] = status

    if mode in {"check", "setup"}:
        payload["overall"] = overall_status(checks) if declaration is not None else "blocked"
    payload["checks"] = [asdict(item) for item in checks]
    payload["facts"] = _facts_payload(facts, repo_root)
    payload = _redact_payload(
        payload, _collect_redactions(repo_root, declaration, declaration_path)
    )
    _emit(payload, args)
    return 0 if payload["overall"] == "ok" else 1


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.qualification_runner as _entry
