"""Real atomic writer and publication wrapper; no gate or child-process execution."""

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import production_gate_parallel_worker as worker
from scripts.production_gate_parallel_identity import (
    CHUNK_BYTES,
    IdentityDeadline,
    expected_identity,
)
from scripts.production_gate_parallel_io import read_json_evidence
from tests._parallel_runtime_holder_kernel import Clock
from tests._parallel_runtime_report_support import gate_tiers, make_plan


def publication_deadline_case(tmp_path, monkeypatch, point):
    plan, clock = make_plan(deadline_seconds=1.0), Clock()
    (tmp_path / "out").mkdir()
    job = plan.job("source-job")
    report = {"overall": "PASS", "tiers": gate_tiers(job, plan.as_dict()), "gate_problems": []}
    identity = dict(clean_head=True, **expected_identity(plan.as_dict(), "source"))
    artifact = worker.build_artifact(
        plan_body=plan.as_dict(),
        plan_digest=plan.digest,
        job=job,
        pid=42,
        started=datetime(2026, 10, 9, tzinfo=UTC).isoformat(),
        exit_status=0,
        report=report,
        problem="",
        before=identity,
        after=identity,
        fresh=True,
    )
    handles, published = [], []
    actual_open, actual_replace = worker._private_output_fd, worker.os.replace
    actual_encode = worker.json.JSONEncoder.iterencode

    def opener(path, flags):
        fd = actual_open(path, flags)
        handles.append(fd)
        return fd

    def encode(encoder, value, *args, **kwargs):
        for index, chunk in enumerate(actual_encode(encoder, value, *args, **kwargs)):
            yield chunk
            if point == "serialize" and index == 3 and clock.now < 1.0:
                clock.now = 1.0

    def replace(source, target):
        actual_replace(source, target)
        published.append(json.loads(target.read_text())["overall"])
        if point == "rename" and len(published) == 1:
            clock.now = 1.0

    def checkpoint():
        if clock.now >= 1.0:
            raise IdentityDeadline("controlled publication deadline exhausted")

    monkeypatch.setattr(worker, "_private_output_fd", opener)
    monkeypatch.setattr(worker.json.JSONEncoder, "iterencode", encode)
    monkeypatch.setattr(worker.os, "replace", replace)
    if point == "before":
        clock.now = 1.0
    status = worker.publish_artifact(tmp_path, artifact, checkpoint=checkpoint)
    target = tmp_path / "out" / worker.TERMINAL_ARTIFACT_NAME
    result = json.loads(target.read_text())
    assert not target.with_suffix(".tmp").exists()
    assert handles
    for fd in handles:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    if point == "within":
        assert status == result["child_exit_status"] == 0
        assert result["overall"] == "PASS" and published == ["PASS"]
        assert target.read_text() == json.dumps(artifact, sort_keys=True) + "\n"
    else:
        assert status == result["child_exit_status"] == worker.TIMEOUT_EXIT_STATUS
        assert result["overall"] == "FAIL"
        assert any(
            "publication" in p and "gate exit status 0" in p for p in result["gate_problems"]
        )
        # Expiry during atomic rename is detected afterward and replaced by timeout evidence.
        # The parent's frozen-deadline validation independently refuses a late intermediate file.
        assert published == (["PASS", "FAIL"] if point == "rename" else ["FAIL"])
    return result


def evidence_reader_case(tmp_path, monkeypatch, point):
    """Cancellation and size limits close the real file and return no partial document."""

    path = tmp_path / "report.json"
    document = {"payload": "x" * (3 * CHUNK_BYTES), "overall": "PASS"}
    raw = json.dumps(document).encode() if point != "malformed" else b'{"x": 1, "x": 2}'
    path.write_bytes(raw)
    actual_open, handles, clock = Path.open, [], Clock()

    def opened(source, *args, **kwargs):
        handle = actual_open(source, *args, **kwargs)
        if source == path:
            handles.append(handle)
        return handle

    def checkpoint():
        clock.now += 0.1
        if point == "expired" and clock.now >= 0.5:
            raise IdentityDeadline("controlled read deadline")

    monkeypatch.setattr(Path, "open", opened)
    maximum = CHUNK_BYTES if point == "oversize" else 64 * CHUNK_BYTES
    if point == "valid":
        observed, size, digest = read_json_evidence(path, checkpoint=checkpoint, max_bytes=maximum)
        assert observed == document and size == len(raw)
        assert digest == hashlib.sha256(raw).hexdigest()
    else:
        failure = IdentityDeadline if point == "expired" else ValueError
        with pytest.raises(failure):
            read_json_evidence(path, checkpoint=checkpoint, max_bytes=maximum)
    assert handles and all(handle.closed for handle in handles)
    assert path.read_bytes() == raw
