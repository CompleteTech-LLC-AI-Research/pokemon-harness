"""Authored controls: explicit production enrolment, credential isolation, captured policy bytes (#107 V11).

Nothing here starts a gate, a ROM or an emulator.  The hub and the clients are real; the capacity
session is the existing one with its injectable sampler.
"""

import hashlib
import json
from pathlib import Path

import pytest

from scripts import gate_capacity
from scripts.gate_capacity_arbiter import ARBITER_ENV_NAMES, ArbiterError, enroll_production
from scripts.production_gate_parallel_arbiter import ArbiterHub
from scripts.production_gate_runtime import build_test_environment
from tests._gate_capacity_support import SequenceSampler, make_facts, make_policy

PIN = "a" * 64


@pytest.fixture
def hub():
    lost = []
    running = ArbiterHub(2, on_loss=lost.append, lease_seconds=5.0).start()
    running.lost = lost
    yield running
    running.stop()


def session_for(tmp_path):
    return gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1),
        repo_root=tmp_path,
        sampler=SequenceSampler([make_facts()]),
    )


def test_a_session_built_while_the_credentials_exist_never_connects_by_itself(
    hub, tmp_path, monkeypatch
):
    for name, value in hub.register("source-job", 1, PIN).items():
        monkeypatch.setenv(name, value)
    session = session_for(tmp_path)
    assert session.admission.arbiter is None
    assert hub.unregister("source-job")["hello"] is False, (
        "a synthetic session never reaches the hub"
    )


def test_production_enrolment_binds_the_job_and_the_parsed_digest_and_removes_the_credentials(
    hub, tmp_path
):
    environment = dict(hub.register("source-job", 1, PIN))
    session = session_for(tmp_path)
    client = enroll_production(session, policy_sha256=PIN, environ=environment)
    assert client is session.admission.arbiter and not client.closed_reason
    assert not [name for name in ARBITER_ENV_NAMES if name in environment]
    assert hub.unregister("source-job")["hello"] is True


def test_a_parsed_digest_that_is_not_the_plan_pin_closes_admission_for_every_job(hub, tmp_path):
    sibling_env = dict(hub.register("cython-job", 1, PIN))
    environment = dict(hub.register("source-job", 1, PIN))
    enroll_production(session_for(tmp_path), policy_sha256="b" * 64, environ=environment)
    assert "plan-pinned" in hub.closed_reason and len(hub.lost) == 1
    late = enroll_production(session_for(tmp_path), policy_sha256=PIN, environ=sibling_env)
    assert late.closed_reason, "the sibling's admission is closed too"


def test_enrolment_inside_a_concurrent_job_needs_the_parsed_digest_and_still_drops_the_credentials(
    hub, tmp_path
):
    environment = dict(hub.register("source-job", 1, PIN))
    with pytest.raises(ArbiterError, match="digest"):
        enroll_production(session_for(tmp_path), policy_sha256=None, environ=environment)
    assert not [name for name in ARBITER_ENV_NAMES if name in environment]


def test_outside_a_concurrent_job_enrolment_is_a_no_op(tmp_path):
    assert enroll_production(session_for(tmp_path), policy_sha256=None, environ={}) is None


def test_the_pytest_environment_never_carries_the_job_credentials(tmp_path, monkeypatch):
    for name in ARBITER_ENV_NAMES:
        monkeypatch.setenv(name, "secret")
    environment = build_test_environment(tmp_path, tmp_path, tmp_path, {})
    assert not [name for name in ARBITER_ENV_NAMES if name in environment]


def policy_bytes():
    return json.dumps(make_policy().as_dict()).encode()


def test_the_digest_is_that_of_the_bytes_parsed_not_of_the_pathname_read_again(
    tmp_path, monkeypatch
):
    path = tmp_path / "policy.json"
    path.write_bytes(b'{"changed": "after the capture"}')
    captured = policy_bytes()
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: captured if self == path else real(self))
    policy, error, digest = gate_capacity.load_capacity_policy_captured(path)
    assert error is None and policy == make_policy()
    assert digest == hashlib.sha256(captured).hexdigest()
    assert digest != hashlib.sha256(b'{"changed": "after the capture"}').hexdigest()


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (b"[]", "must be a JSON object"),
        (b"\xff\xfe", "cannot decode"),
        (b"{not json", "not valid JSON"),
        (b"\xef\xbb\xbf{}", "not valid JSON"),
        (b"{}", ""),
        (json.dumps({**make_policy().as_dict(), "effective_cpus": "four"}).encode(), ""),
        (json.dumps({**make_policy().as_dict(), "effective_cpus": 0}).encode(), "effective_cpus"),
    ],
)
def test_a_malformed_policy_is_an_explicit_error_with_the_digest_of_those_bytes(
    tmp_path, data, fragment
):
    path = tmp_path / "policy.json"
    path.write_bytes(data)
    policy, error, digest = gate_capacity.load_capacity_policy_captured(path)
    assert policy is None and error and fragment in error
    assert digest == hashlib.sha256(data).hexdigest()


def test_a_missing_policy_has_no_digest_and_the_valid_one_matches_the_original_loader(tmp_path):
    assert gate_capacity.load_capacity_policy_captured(tmp_path / "absent.json")[2] is None
    path = tmp_path / "policy.json"
    path.write_bytes(policy_bytes())
    original, original_error = gate_capacity.load_capacity_policy(path)
    captured, error, _digest = gate_capacity.load_capacity_policy_captured(path)
    assert original_error is None and error is None and original == captured


def test_the_parallel_producer_captures_the_policy_once_and_refuses_a_file_that_changed(
    tmp_path, monkeypatch
):
    from scripts.production_gate_parallel_capacity import capture_policy

    path = tmp_path / "policy.json"
    data = policy_bytes() + b"\r\n"  # newline bytes must reach the plan exactly as the gate parsed
    path.write_bytes(data)
    parsed = hashlib.sha256(data).hexdigest()
    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: (reads.append(self), real(self))[1])
    text, digest, problems = capture_policy(path, parsed)
    assert problems == [] and digest == parsed and text.encode() == data and len(reads) == 1
    path.write_bytes(policy_bytes() + b" ")  # the file changed after the gate parsed it
    text, digest, problems = capture_policy(path, parsed)
    assert text == "" and digest != parsed and "changed after the gate parsed it" in problems[0]
    assert capture_policy(path, None)[2] == [], "with no parsed digest there is nothing to compare"
    path.write_bytes(b"\xff\xfe")
    assert "cannot be captured" in capture_policy(path, None)[2][0]
    missing = capture_policy(tmp_path / "absent.json", None)
    assert missing[0] == "" and "cannot be captured" in missing[2][0]
