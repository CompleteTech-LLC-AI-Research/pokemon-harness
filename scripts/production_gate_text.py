"""Pure text sanitising, failure formatting and count formatting helpers.

Split from ``scripts/production_gate.py`` for issue #124 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on the loaded gate module stay visible.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_model import (
    _ABSOLUTE_PATH_RE,
    _BYTE_LITERAL_RE,
    _CREDENTIAL_TEXT_RE,
    _LONG_TOKEN_RE,
    _QUOTED_ABSOLUTE_PATH_RE,
    _SPACED_ABSOLUTE_PATH_RE,
    _URI_CREDENTIAL_RE,
    _WINDOWS_ABSOLUTE_PATH_RE,
    MAX_FAILURE_DETAIL_CHARS,
    MAX_FAILURE_DETAILS,
    MAX_FAILURE_NODEID_CHARS,
    Counts,
    FailureDetail,
    TierResult,
)


def _failure_excerpt(safe: str, limit: int) -> tuple[str, int]:
    """Clip already-sanitized text, preserving its test line and exception."""
    if len(safe) <= limit:
        return safe, 0
    marker = "\n[...middle truncated; see omitted_chars metadata...]\n"
    available = limit - len(marker)
    head = (available + 1) // 2
    tail = available // 2
    return safe[:head] + marker + safe[-tail:], len(safe) - available


def _retain_failure_detail(
    details: list[FailureDetail],
    record: dict[str, str],
    *,
    iteration: int,
    remaining_chars: int,
) -> tuple[int, int]:
    """Return remaining text budget and exactly zero or one omitted record.

    Only retained evidence is bounded here. Existing subprocess capture and
    report loading are unchanged. Sanitize one existing record before slicing;
    never collect additional logs or split a credential/blob before redaction.
    """
    if len(details) >= MAX_FAILURE_DETAILS or remaining_chars < MAX_FAILURE_NODEID_CHARS + 128:
        return remaining_chars, 1
    nodeid = _safe_text(record["nodeid"], limit=sys.maxsize)
    nodeid_original_chars = len(nodeid)
    nodeid, nodeid_omitted = _failure_excerpt(nodeid, MAX_FAILURE_NODEID_CHARS)
    # Runtime outcomes are validated vocabulary; serializers may receive a
    # directly constructed record, which still needs redaction and budgeting.
    outcome = _safe_text(record["outcome"], limit=sys.maxsize)
    if remaining_chars - len(nodeid) - len(outcome) < 128:
        return remaining_chars, 1
    safe = _safe_text(record["reason"], limit=sys.maxsize)
    original_chars = len(safe)
    limit = min(MAX_FAILURE_DETAIL_CHARS, remaining_chars - len(nodeid) - len(outcome))
    reason, omitted = _failure_excerpt(safe, limit)
    details.append(
        FailureDetail(
            iteration=iteration,
            nodeid=nodeid,
            outcome=outcome,
            reason=reason,
            original_chars=original_chars,
            omitted_chars=omitted,
            truncated=bool(omitted or nodeid_omitted),
            nodeid_original_chars=nodeid_original_chars,
            nodeid_omitted_chars=nodeid_omitted,
        )
    )
    return remaining_chars - len(nodeid) - len(outcome) - len(reason), 0


def _failure_detail_lines(details: Iterable[dict[str, Any]], omitted: int) -> Iterable[str]:
    for detail in details:
        yield (
            f"    failure-detail: iteration {detail['iteration']}: "
            f"{detail['nodeid']}: {detail['outcome']} "
            f"original_chars={detail['original_chars']} "
            f"omitted_chars={detail['omitted_chars']} "
            f"truncated={detail['truncated']} "
            f"nodeid_original_chars={detail['nodeid_original_chars']} "
            f"nodeid_omitted_chars={detail['nodeid_omitted_chars']}"
        )
        yield detail["reason"]
    if omitted:
        yield f"    failure-details-omitted: {omitted}"


def _bounded_failure_text(value: str, limit: int, *, tail: bool = False) -> str:
    # Redact the whole string before any slice can detach a sensitive suffix
    # from its credential/path prefix. Keep node IDs at the start of entries.
    safe = _safe_text(value, limit=sys.maxsize)
    if len(safe) <= limit:
        return safe
    marker = "[...truncated...]"
    available = limit - len(marker)
    return marker + safe[-available:] if tail else safe[:available] + marker


def _format_counts(counts: Counts) -> str:
    return (
        f"total={counts.total} passed={counts.passed} failed={counts.failed} "
        f"skipped={counts.skipped} xfailed={counts.xfailed} "
        f"xpassed={counts.xpassed} errors={counts.errors}"
    )


def _jsonable_tier(tier: TierResult) -> dict[str, Any]:
    data = asdict(tier)
    data["counts"] = asdict(tier.counts)
    return data


def _safe_text(value: Any, *, limit: int = 8000) -> str:
    """Bound and redact free-form diagnostics before retaining them."""

    text = "" if value is None else str(value)
    # Redact paths before credential matching. A path component such as
    # ``secret`` must not cause the credential regex to consume only the
    # suffix of a spaced path and leave the remainder machine-specific.
    text = _QUOTED_ABSOLUTE_PATH_RE.sub("<external-path>", text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub("<external-path>", text)
    text = _SPACED_ABSOLUTE_PATH_RE.sub("<external-path>", text)
    text = _ABSOLUTE_PATH_RE.sub("<external-path>", text)
    text = _URI_CREDENTIAL_RE.sub(r"\1:[REDACTED]@", text)
    text = _BYTE_LITERAL_RE.sub("[BINARY DATA REDACTED]", text)
    text = _CREDENTIAL_TEXT_RE.sub("[CREDENTIAL REDACTED]", text)
    text = _LONG_TOKEN_RE.sub("[LONG TOKEN REDACTED]", text)
    text = "".join(
        character
        if character in "\n\r\t" or character.isprintable()
        else f"\\x{ord(character):02x}"
        for character in text
    )
    if len(text) > limit:
        text = "[...truncated...]\n" + text[-limit:]
    return text


def _payload_counts_text(counts: dict[str, Any]) -> str:
    fields = ("total", "passed", "failed", "skipped", "xfailed", "xpassed", "errors")
    return " ".join(f"{field}={counts.get(field, 0)}" for field in fields)
