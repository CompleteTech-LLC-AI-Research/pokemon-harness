"""Synthetic authenticated-protocol refusal controls; authored NOTRUN.

These exercise actual decoders/accounting validators, not Linux custody, broker
admission, measured producers, timing, gameplay or release qualification.
"""

import hashlib
import hmac

import pytest

from scripts.production_gate_parallel_probe_protocol import (
    MAX_PROBE_FRAME,
    ProbeContext,
    ProbeProtocolError,
    canonical_probe_frame,
    decode_probe_frame,
    registration_problems,
    terminal_problems,
)


def context_and_registration():
    context = ProbeContext("synthetic-scope", "n" * 64, b"k" * 32, "a" * 64, "b" * 64, 10.0, 12.0)
    body = {
        "scope": context.scope,
        "nonce": context.nonce,
        "source_sha256": context.source_sha256,
        "program_sha256": context.program_sha256,
        "phase": "register",
        "role": "holder",
        "pid": 100,
        "start": 10,
        "sequence": 2,
        "parent_pid": 90,
        "parent_start": 9,
        "guard": 15,
        "subreaper": True,
        "job_deadline": 10.0,
        "cleanup_deadline": 12.0,
    }
    return context, body


def signed_bytes(body, context):
    mac = hmac.new(context.secret, canonical_probe_frame(body), hashlib.sha256).hexdigest()
    return canonical_probe_frame(dict(body, mac=mac))


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "another"),
        ("nonce", "another"),
        ("source_sha256", "c" * 64),
        ("program_sha256", "c" * 64),
        ("role", "unknown"),
        ("role", []),
        ("phase", "unknown"),
        ("phase", []),
        ("pid", True),
        ("start", None),
        ("sequence", 0),
        ("sequence", True),
    ],
)
def test_authenticated_wrong_or_malformed_instance_bindings_are_rejected(field, value):
    context, body = context_and_registration()
    body[field] = value
    with pytest.raises(ProbeProtocolError):
        decode_probe_frame(signed_bytes(body, context), context)


def test_valid_mac_is_not_enough_for_replay_parent_guard_or_budget_admission():
    context, body = context_and_registration()
    decoded = decode_probe_frame(signed_bytes(body, context), context)
    kwargs = {
        "peer_instance": (100, 10),
        "parent_instance": (90, 9),
        "previous_sequence": 1,
        "cleanup_bound": 12.0,
    }
    assert registration_problems(decoded, context, **kwargs) == []
    for change in (
        {"peer_instance": (101, 10)},
        {"parent_instance": (91, 9)},
        {"previous_sequence": 2},
        {"cleanup_bound": 11.0},
    ):
        assert registration_problems(decoded, context, **dict(kwargs, **change))
    for field, value in (
        ("guard", 9),
        ("subreaper", False),
        ("job_deadline", 11.0),
        ("cleanup_deadline", 13.0),
    ):
        assert registration_problems(dict(decoded, **{field: value}), context, **kwargs)


@pytest.mark.parametrize(
    "raw", [b"", b"[]", b"{", b"x" * (MAX_PROBE_FRAME + 1), b'{"x":1,"x":2}', b'{"x":NaN}']
)
def test_oversize_missing_nonobject_and_non_strict_frames_are_rejected(raw):
    context, _body = context_and_registration()
    with pytest.raises(ProbeProtocolError):
        decode_probe_frame(raw, context)


def test_wrong_mac_is_rejected_without_trusting_its_other_fields():
    context, body = context_and_registration()
    with pytest.raises(ProbeProtocolError, match="authentication"):
        decode_probe_frame(canonical_probe_frame(dict(body, mac="0" * 64)), context)


def test_authenticated_final_requires_complete_exact_instance_accounting():
    context, registration = context_and_registration()
    final = dict(
        registration,
        phase="final",
        sequence=3,
        physical_empty=True,
        echild=True,
        subreaper_start=True,
        subreaper_end=True,
        proof=True,
        timely_cleanup=True,
        success=True,
        custody_errors=[],
        operation_errors=[],
        remaining=[],
        forks=[[101, 11]],
        adopted=[[102, 12]],
        reaped=[[101, 11], [102, 12]],
        killed=[[102, 12]],
        child_status=0,
    )
    decoded = decode_probe_frame(signed_bytes(final, context), context)
    kwargs = {"stage_instance": (100, 10), "child_instance": (101, 11), "previous_sequence": 2}
    assert terminal_problems(decoded, **kwargs) == []
    for change in (
        {"proof": False},
        {"echild": False},
        {"custody_errors": ["unknown"]},
        {"remaining": [101]},
        {"reaped": [[101, 11]]},
        {"killed": [[999, 99]]},
        {"forks": [[999, 99]]},
        {"adopted": [[101, 11]]},
        {"child_status": 1},
        {"operation_errors": ["original failure"]},
        {"sequence": 2},
        {"reaped": [[101, 11], [102, 12], [102, 12]]},
    ):
        assert terminal_problems(dict(decoded, **change), **kwargs)
    failed_operation = dict(
        decoded, success=False, child_status=1, operation_errors=["original failure"]
    )
    assert terminal_problems(failed_operation, **kwargs) == [], (
        "failure may have proven cleanup; cannot become success"
    )
