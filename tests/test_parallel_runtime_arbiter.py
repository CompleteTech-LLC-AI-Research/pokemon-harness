"""Authored controls: the live parent arbiter and the child admission hook (#107 F3).

A real hub and real clients talk over the abstract unix socket protocol; no gate, ROM or emulator is
started.  The admission controller is driven with a stand-in policy object, so the existing
admission tests are untouched and ``arbiter=None`` stays the ordinary standalone behaviour.
"""

import socket
import time
from types import SimpleNamespace

import pytest

from scripts import gate_capacity_arbiter as wire
from scripts.gate_capacity_admission import CapacityAdmission
from scripts.gate_capacity_arbiter import ENV_KEY, ENV_NAME, ArbiterClient, ArbiterError
from scripts.production_gate_parallel_arbiter import ArbiterHub

POLICY = SimpleNamespace(max_concurrent_pairs=2, admission_deadline_seconds=60.0)
PIN = "a" * 64


@pytest.fixture
def hub():
    lost = []
    running = ArbiterHub(2, on_loss=lost.append, lease_seconds=0.3).start()
    running.lost = lost
    yield running
    running.stop()


def connect(hub, job, pairs, *, policy=PIN, announce=None):
    env = hub.register(job, pairs, policy)
    return ArbiterClient(
        env[ENV_NAME], bytes.fromhex(env[ENV_KEY]), job, announce or policy, heartbeat=None
    )


def raw(hub):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    conn.settimeout(3.0)
    conn.connect("\0" + hub.name)
    return conn


def test_a_reservation_is_granted_only_under_the_share_and_the_single_global_budget(hub):
    first, second = connect(hub, "source-job", 1), connect(hub, "cython-job", 2)
    assert first.reserve("p1") == ("granted", "")
    assert first.reserve("p1")[0] == "granted", "a held reservation is idempotent"
    assert first.reserve("p2")[0] == "denied", "the job's share is full"
    assert second.reserve("q1") == ("granted", "")
    verdict, reason = second.reserve("q2")
    assert verdict == "denied" and "global" in reason, "the one total budget is full"
    first.release("p1")
    assert second.reserve("q2") == ("granted", "")
    ledger = hub.unregister("cython-job")
    assert ledger["grants"] == 2 and ledger["denials"] == 1 and ledger["unreleased"] == ["q1", "q2"]


def test_an_unknown_release_is_counted_and_never_applied(hub):
    client = connect(hub, "source-job", 1)
    client.release("never-reserved")
    assert client.reserve("p")[0] == "granted"
    client.release("p")
    ledger = hub.unregister("source-job")
    assert ledger["stray_releases"] == 1 and ledger["releases"] == 1 and ledger["unreleased"] == []


def test_a_forged_message_is_refused_without_a_reply_and_counted(hub):
    hub.register("source-job", 1, PIN)
    conn = raw(hub)
    forged = {"v": 1, "job": "source-job", "seq": 1, "op": "hello", "policy": PIN, "mac": "0" * 64}
    conn.send(wire.json.dumps(forged).encode())
    assert conn.recv(4096) == b"", "no acknowledgment for a message that is not authenticated"
    assert hub.unregister("source-job")["violations"] == 1


def test_a_replayed_message_is_refused(hub):
    env = hub.register("source-job", 1, PIN)
    key = bytes.fromhex(env[ENV_KEY])
    conn = raw(hub)
    message = wire.seal(key, {"v": 1, "job": "source-job", "seq": 1, "op": "hello", "policy": PIN})
    conn.send(message)
    assert wire.unseal(key, conn.recv(4096))["result"] == "ok"
    conn.send(message)
    assert conn.recv(4096) == b"", "a repeated sequence number is a replay"
    assert hub.unregister("source-job")["violations"] == 1


def test_three_protocol_violations_lose_the_arbiter_for_everyone(hub):
    hub.register("source-job", 1, PIN)
    for _ in range(3):
        conn = raw(hub)
        conn.send(
            wire.json.dumps({"v": 1, "job": "source-job", "seq": 1, "op": "x", "mac": "0"}).encode()
        )
        assert conn.recv(4096) == b""
    assert "violated" in hub.closed_reason and hub.lost == [hub.closed_reason]
    assert connect(hub, "cython-job", 1).reserve("p")[0] == "closed"


def test_a_policy_that_differs_from_the_plan_pin_closes_admission_for_everyone(hub):
    other = connect(hub, "cython-job", 1)
    client = connect(hub, "source-job", 1, announce="b" * 64)
    assert client.closed_reason and "policy" in client.closed_reason
    assert hub.lost and "plan-pinned" in hub.lost[0]
    assert other.reserve("p")[0] == "closed", "the sibling's admission closed at once"


def test_a_capacity_loss_event_is_acknowledged_closes_admission_and_notifies_the_owner(hub):
    client = connect(hub, "source-job", 1)
    client.event("capacity_loss", "p", "memory pressure")
    assert client.closed_reason and "memory pressure" in hub.closed_reason
    assert len(hub.lost) == 1
    assert client.reserve("q") == ("closed", client.closed_reason)
    assert hub.unregister("source-job")["events"][0]["kind"] == "capacity_loss"


def test_a_silent_child_loses_its_lease(hub):
    connect(hub, "source-job", 1)
    time.sleep(0.5)
    hub.check_leases()
    assert "lease expired" in hub.closed_reason and len(hub.lost) == 1


def test_the_client_latches_closed_when_the_arbiter_stops_answering(hub):
    client = connect(hub, "source-job", 1)
    hub.unregister("source-job")
    verdict, reason = client.reserve("p")
    assert verdict == "closed" and client.closed_reason == reason
    assert client.reserve("q")[0] == "closed", "once closed, always closed"
    with pytest.raises(ArbiterError):
        client.release("p")


def test_a_concurrent_job_never_falls_back_to_static_shares():
    assert ArbiterClient.from_environment({}) is None
    with pytest.raises(ArbiterError, match="incomplete"):
        ArbiterClient.from_environment({ENV_NAME: "x"})
    with pytest.raises(ArbiterError, match="incomplete"):
        ArbiterClient.from_environment({ENV_NAME: "x", ENV_KEY: "zz", wire.ENV_JOB: "j"})
    good = {ENV_NAME: "no-such-arbiter", ENV_KEY: "11" * 32, wire.ENV_JOB: "source-job"}
    with pytest.raises(ArbiterError, match="cannot be reached"):
        ArbiterClient.from_environment(good)


def test_the_wire_is_bounded_authenticated_and_strict():
    key = b"k" * 32
    with pytest.raises(ArbiterError, match="bound"):
        wire.seal(key, {"op": "x" * 3000})
    sealed = wire.seal(key, {"op": "ping", "seq": 1})
    assert wire.unseal(key, sealed)["op"] == "ping"
    with pytest.raises(ArbiterError, match="not authenticated"):
        wire.unseal(b"z" * 32, sealed)
    with pytest.raises(ArbiterError, match="strict JSON"):
        wire.peek(b'{"seq": NaN}')
    with pytest.raises(ArbiterError):
        wire.peek(b"[1]")
    with pytest.raises(ArbiterError):
        wire.peek(b"x" * 4000)


def test_a_slot_exists_only_with_the_arbiters_grant_and_a_release_promotes_a_granted_waiter(hub):
    client = connect(hub, "source-job", 1)
    admission = CapacityAdmission(POLICY, arbiter=client)
    assert admission.admit("a").status == "ok"
    queued = admission.admit("b")
    assert queued.status == "queued" and "denied" in queued.reason, "the local policy would allow 2"
    promoted = admission.release("a")
    assert promoted is not None and promoted.pair_id == "b" and promoted.promoted
    ledger = hub.unregister("source-job")
    assert ledger["grants"] == 2 and ledger["releases"] == 1 and ledger["unreleased"] == ["b"]


def test_a_closed_arbiter_blocks_new_admissions_terminally(hub):
    client = connect(hub, "source-job", 1)
    admission = CapacityAdmission(POLICY, arbiter=client)
    hub.lose("capacity lost elsewhere")
    decision = admission.admit("c")
    assert decision.status == "blocked" and "live arbiter closed admission" in decision.reason
    assert admission.state("c") == "blocked"
    assert admission.admit("c").status == "blocked", "a retry cannot turn a block into a pass"


def test_block_and_abandon_are_reported_to_the_arbiter_with_acknowledgment(hub):
    client = connect(hub, "source-job", 1)
    admission = CapacityAdmission(POLICY, arbiter=client)
    admission.block("x", "capacity unavailable")
    admission.abandon("y", "never started")
    kinds = [event["kind"] for event in hub.unregister("source-job")["events"]]
    assert kinds == ["blocked", "abandoned"]


def test_without_an_arbiter_the_controller_behaves_exactly_as_before():
    admission = CapacityAdmission(POLICY)
    assert [admission.admit(name).status for name in ("a", "b", "c")] == ["ok", "ok", "queued"]
    assert admission.release("a").promoted


def test_a_loss_of_the_childs_own_capacity_observation_reaches_the_live_book_at_once(hub):
    sibling = connect(hub, "cython-job", 1)
    admission = CapacityAdmission(POLICY, arbiter=connect(hub, "source-job", 1))
    admission.report_loss("memory pressure rose above the policy")
    assert "memory pressure" in hub.closed_reason and len(hub.lost) == 1
    assert sibling.reserve("p")[0] == "closed", "the sibling's admission closed at once"


def test_a_child_that_drops_its_connection_while_holding_reservations_is_reported_not_forgotten(
    hub,
):
    client = connect(hub, "source-job", 1)
    assert client.reserve("p")[0] == "granted"
    client._conn.close()  # the gate died without a bye
    deadline = time.monotonic() + 3.0
    ledger = None
    while time.monotonic() < deadline:
        with hub._lock:
            if hub._jobs["source-job"].dropped:
                break
        time.sleep(0.02)
    ledger = hub.unregister("source-job")
    assert ledger["dropped"] is True and ledger["unreleased"] == ["p"]
    assert connect(hub, "cython-job", 1).reserve("q")[0] == "granted", (
        "the dropped slot was returned"
    )


def test_an_arbiter_that_stops_serving_is_a_loss_for_everyone(hub):
    hub._server.close()  # the serving thread's accept now fails and it ends
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and hub._thread.is_alive():
        time.sleep(0.02)
    hub.check_leases()
    assert "stopped serving" in hub.closed_reason and len(hub.lost) == 1


def test_the_hello_binds_the_digest_of_the_policy_bytes_the_gate_actually_loaded(hub, tmp_path):
    policy = tmp_path / "capacity-policy.json"
    policy.write_text('{"max_concurrent_pairs": 1}', encoding="utf-8")
    loaded = wire.policy_file_digest(policy)
    assert (
        loaded is not None and len(loaded) == 64 and wire.policy_file_digest(tmp_path / "x") is None
    )
    env = hub.register("source-job", 1, loaded)
    other = hub.register("cython-job", 1, "c" * 64)
    good = ArbiterClient.from_environment(env, policy=loaded)
    assert good is not None and not good.closed_reason
    forged = ArbiterClient.from_environment(other, policy=loaded)
    assert forged is not None and forged.closed_reason and "plan-pinned" in hub.closed_reason


def test_a_real_session_in_a_concurrent_job_joins_the_live_book_and_reports_a_lost_observation(
    hub, tmp_path, monkeypatch
):
    from scripts import gate_capacity
    from tests._gate_capacity_support import SequenceSampler, make_facts, make_policy

    for name, value in hub.register("source-job", 1, PIN).items():
        monkeypatch.setenv(name, value)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1),
        repo_root=tmp_path,
        sampler=SequenceSampler([make_facts(), make_facts(psi_cpu_some_avg300=100)]),
        policy_sha256=PIN,
    )
    assert session.admission.arbiter is not None and not session.admission.arbiter.closed_reason
    session.observe()
    assert session.availability_status == "ok" and not hub.closed_reason
    session.observe()
    assert session.availability_status != "ok"
    assert hub.closed_reason and len(hub.lost) == 1, "the lost observation reached the live book"


def test_a_session_whose_policy_digest_differs_from_the_plan_pin_closes_admission(
    hub, tmp_path, monkeypatch
):
    from scripts import gate_capacity
    from tests._gate_capacity_support import SequenceSampler, make_facts, make_policy

    for name, value in hub.register("source-job", 1, PIN).items():
        monkeypatch.setenv(name, value)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1),
        repo_root=tmp_path,
        sampler=SequenceSampler([make_facts()]),
        policy_sha256="b" * 64,
    )
    assert session.admission.arbiter.closed_reason and "plan-pinned" in hub.closed_reason
    assert session.admission.admit("a").status == "blocked"
