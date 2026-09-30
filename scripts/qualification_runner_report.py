"""Report rendering, redaction and job directories.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.qualification_runner_facts import CheckResult, RunnerFacts
from scripts.qualification_runner_host import _process_start_time, _resolve_declared_path
from scripts.qualification_runner_model import _ABSOLUTE_PATH_RE


def overall_status(results: list[CheckResult]) -> str:
    active = [item for item in results if item.status != "skipped"]
    if not active:
        return "blocked"
    for status in ("fail", "blocked", "unsupported"):
        if any(item.status == status for item in active):
            return status
    if all(item.status == "ok" for item in active):
        return "ok"
    return "blocked"


def load_declaration(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"declaration not found: {path}"
    except OSError as exc:
        return None, f"cannot read declaration: {exc}"
    except UnicodeDecodeError as exc:
        return None, f"cannot read declaration: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"declaration is not valid JSON: {exc}"
    if not isinstance(document, dict):
        return None, "declaration must be a JSON object"
    return document, None


def render_text(payload: dict[str, Any]) -> str:
    lines = [f"qualification runner: {payload['overall']}", f"mode: {payload['mode']}"]
    if payload.get("declaration"):
        lines.append(f"declaration: {payload['declaration']}")
    for item in payload["checks"]:
        lines.append(
            f"  {item['status']:>11}  {item['name']}: required={item['required']!r} "
            f"observed={item['observed']!r} :: {item['detail']}"
        )
    facts = payload.get("facts")
    if facts:
        lines.append("facts:")
        for key in (
            "platform",
            "logical_cpus",
            "affinity_cpus",
            "cgroup_version",
            "cpu_quota_cores",
            "cpu_weight",
            "memory_total_bytes",
            "memory_available_bytes",
            "load_average",
            "psi_cpu_some_avg300",
            "repo_disk_free_bytes",
            "temp_disk_free_bytes",
            "shm_size_bytes",
            "shm_writable",
            "unsupported",
        ):
            lines.append(f"  {key}={facts.get(key)!r}")
    if payload.get("message"):
        lines.append(payload["message"])
    return "\n".join(lines)


def _sanitize_path(value: Any, root: Path) -> str:
    """Return a relative path when possible, never a machine-local absolute."""

    text = str(value)
    candidate = Path(text)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        relative = candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return candidate.name
    return relative.as_posix() or "."


_PATH_FRAGMENT_RE = re.compile(
    r"(?<![\w./-])(/(?:[^\s:'\"()\[\],;]+/)*"
    r"(?:lib/python[0-9.]*|site-packages|dist-packages|bin/python[0-9.]*)"
    r"(?:/[^\s:'\"()\[\],;]+)*)"
)


def _scrub_absolute_paths(text: str) -> str:
    """Redact any remaining absolute path, including unregistered diagnostics.

    Registered paths are already replaced by relative or basename forms before
    this runs; this fallback removes arbitrary absolute build, temporary, and
    compiler diagnostic paths that were never declared.
    """

    return _ABSOLUTE_PATH_RE.sub("<redacted-path>", _PATH_FRAGMENT_RE.sub("<redacted-path>", text))


def _redact_text(text: str, redactions: dict[str, str]) -> str:
    for token in sorted(redactions, key=len, reverse=True):
        if token:
            text = text.replace(token, redactions[token])
    return _scrub_absolute_paths(text)


def _redact_payload(value: Any, redactions: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, redactions)
    if isinstance(value, list):
        return [_redact_payload(item, redactions) for item in value]
    if isinstance(value, dict):
        return {key: _redact_payload(item, redactions) for key, item in value.items()}
    return value


def _collect_redactions(
    repo_root: Path, declaration: dict[str, Any] | None, declaration_path: Path | None
) -> dict[str, str]:
    redactions = {str(repo_root): _sanitize_path(repo_root, repo_root)}

    def register(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            redactions[value] = _sanitize_path(value, repo_root)

    if declaration_path is not None:
        register(str(declaration_path))
    if isinstance(declaration, dict):
        assets = declaration.get("assets")
        if isinstance(assets, dict):
            for key in ("rom_root", "fixture_root"):
                register(assets.get(key))
            inputs = assets.get("inputs")
            if isinstance(inputs, list):
                for entry in inputs:
                    if isinstance(entry, dict):
                        register(entry.get("path"))
        interpreters = declaration.get("interpreters")
        if isinstance(interpreters, dict):
            for value in interpreters.values():
                if not isinstance(value, str) or not value.strip():
                    continue
                register(value)
                candidate = Path(value)
                register(str(candidate.parent))
                register(str(candidate.parent.parent))
                try:
                    resolved = candidate.resolve()
                except (OSError, RuntimeError, ValueError):
                    continue
                register(str(resolved))
                register(str(resolved.parent))
                register(str(resolved.parent.parent))
        reservation = declaration.get("reservation")
        if isinstance(reservation, dict):
            for key in (
                "descriptor_path",
                "job_dir",
                "exclusive_marker_path",
                "host_lock_path",
            ):
                register(reservation.get(key))
    return redactions


def _job_directory(args: argparse.Namespace, declaration: dict[str, Any], repo_root: Path) -> Path:
    if getattr(args, "job_dir", None) is not None:
        candidate = Path(args.job_dir).expanduser()
        return candidate if candidate.is_absolute() else repo_root / candidate
    reservation = declaration.get("reservation")
    if isinstance(reservation, dict):
        declared = _resolve_declared_path(reservation.get("job_dir"), repo_root)
        if declared is not None:
            return declared
    runner_id = str(declaration.get("runner_id") or "default")
    return repo_root / "target" / "qualification-runs" / runner_id


def _prepare_job_directory(job_dir: Path) -> None:
    """Create the owner-only per-job layout without touching shared assets."""

    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(job_dir, 0o700)
    for name in ("tmp", "evidence", "logs"):
        child = job_dir / name
        child.mkdir(exist_ok=True)
        os.chmod(child, 0o700)


def _populate_job_filesystem_facts(facts: RunnerFacts, job_dir: Path) -> None:
    """Record free space on the filesystems the job will actually write to.

    Admission must measure the destination, not the caller's ``TMPDIR``.  The
    spawned job receives ``TMPDIR=<job-dir>/tmp`` and writes evidence under
    ``<job-dir>/evidence``; either can live on a different filesystem from
    ``/tmp``.  The directories are created first (a private job directory is
    about to be prepared anyway) so ``statvfs`` measures the real target.
    """

    for label, path in (
        ("job_tmp", job_dir / "tmp"),
        ("job_evidence", job_dir / "evidence"),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            path = path.parent
        setattr(facts, f"{label}_path", str(path))
        try:
            setattr(facts, f"{label}_disk_free_bytes", shutil.disk_usage(path).free)
        except OSError:
            setattr(facts, f"{label}_disk_free_bytes", None)


def _holder_is_owned(holder: Any, start_time: Any) -> bool:
    if not isinstance(holder, int) or isinstance(holder, bool) or holder <= 0:
        return False
    if start_time != _process_start_time(holder):
        return False
    try:
        raw = Path(f"/proc/{holder}/cmdline").read_bytes()
    except OSError:
        return False
    command = b" ".join(part for part in raw.split(b"\0") if part).decode("utf-8", "replace")
    return "qualification_runner" in command or "qualification-runner" in command
