"""Exclusive native pump admission and fault-safe release contract."""
import threading

import pytest
from pyboy.core.serial import Serial, SerialBackendError
from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


def armed():
    core = Serial(False)
    core.set_SB(0xA5)
    core.set_SC(0x81)
    return core


def test_network_scope_preserves_existing_callback():
    core, seen = armed(), []
    core.set_owner_pump(seen.append)
    a, b = NetworkBackend.pair()
    try:
        b.start_receiver(local_core=core)
        with pytest.raises(RuntimeError, match="already installed"):
            with b.owner_scope():
                pytest.fail("scope admitted foreign callback")
        core.tick(512)
        core.check_error()
        assert len(seen) == 1
    finally:
        a.stop()
        b.stop()


def test_unclaimed_setter_keeps_replacement_semantics():
    core, old, new = armed(), [], []
    core.set_owner_pump(old.append)
    core.set_owner_pump(new.append)
    core.tick(512)
    assert not old and len(new) == 1
    core.set_owner_pump(None)
    core.tick(1024)
    assert len(new) == 1


def test_claim_rejects_replacement_and_wrong_release_without_mutation():
    core, seen = armed(), []
    token = core.claim_owner_pump(seen.append, poll=True)
    for callback in (None, lambda event: None):
        with pytest.raises(RuntimeError, match="exclusively claimed"):
            core.set_owner_pump(callback)
    for bad in (None, object()):
        with pytest.raises(RuntimeError, match="invalid.*token"):
            core.release_owner_pump(bad)
    errors = []
    def wrong_thread():
        try:
            core.release_owner_pump(token)
        except RuntimeError as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=wrong_thread)
    worker.start()
    worker.join(2)
    assert not worker.is_alive() and errors == ["serial owner pump release off owner thread"]
    core.tick(512)
    assert len(seen) == 1 and core.owner_poll_enabled
    core.release_owner_pump(token)
    assert not core.owner_poll_enabled
    core.set_owner_pump(None)


def test_release_after_fault_preserves_first_cause():
    core = armed()
    cause = ValueError("first cable fault")
    def fail(event):
        raise cause
    token = core.claim_owner_pump(fail, poll=True)
    core.tick(512)
    core.release_owner_pump(token)
    assert core.backend_failed and not core.owner_poll_enabled
    with pytest.raises(SerialBackendError) as caught:
        core.check_error()
    assert caught.value.__cause__ is cause


def test_network_scope_releases_after_callback_fault():
    core = armed()
    cause = ValueError("network pump fault")
    a, b = NetworkBackend.pair()
    def fail(event):
        raise cause
    b.owner_poll = fail
    try:
        b.start_receiver(local_core=core)
        with b.owner_scope():
            core.tick(512)
        assert core.backend_failed and not core.owner_poll_enabled
        with pytest.raises(SerialBackendError) as caught:
            core.check_error()
        assert caught.value.__cause__ is cause
        # A failed core still admits teardown without rebinding its pump.
        with b.owner_scope():
            pass
    finally:
        a.stop()
        b.stop()


def test_coreless_network_scope_remains_compatible():
    """Transport-only receivers still support an owner scope without a core."""
    a, b = NetworkBackend.pair()
    try:
        b.start_receiver(local_core=None)
        with b.owner_scope():
            b.owner_poll()
    finally:
        a.stop()
        b.stop()


def test_network_scope_rejects_stale_setter_only_core():
    class StaleCore:
        backend_failed = False
        called = False
        def set_owner_pump(self, callback, poll=False):
            self.called = True
    core = StaleCore()
    a, b = NetworkBackend.pair()
    try:
        with pytest.raises(NetworkBackendError, match="lacks exclusive"):
            b.start_receiver(local_core=core)
        assert b._reader is None and b._edge_worker is None
        assert not core.called
    finally:
        a.stop()
        b.stop()


def test_network_scope_rejects_attached_core_without_claim_api():
    class MissingSetterCore:
        backend_failed = False
        transfer_enabled = False
        internal_clock = False

        def __init__(self):
            self.mutations = 0

        def peek_out_bit(self):
            self.mutations += 1
            return 1

        def apply_external_edge(self, _bit):
            self.mutations += 1
            return False

    core = MissingSetterCore()
    a, b = NetworkBackend.pair()
    try:
        with pytest.raises(NetworkBackendError, match="exclusive owner pump"):
            b.start_receiver(local_core=core)
        assert b._reader is None and b._edge_worker is None
        assert core.mutations == 0
    finally:
        a.stop()
        b.stop()


def test_callback_cannot_release_or_replace_active_binding():
    core, rejected = armed(), []
    def callback(event):
        for action in (lambda: core.release_owner_pump(token),
                       lambda: core.set_owner_pump(None),
                       lambda: core.claim_owner_pump(callback)):
            with pytest.raises(RuntimeError, match="recursive CPU execution"):
                action()
            rejected.append(True)
    token = core.claim_owner_pump(callback)
    core.tick(512)
    core.check_error()
    assert rejected == [True] * 3
    core.release_owner_pump(token)


def test_concurrent_claims_have_exactly_one_winner():
    core = Serial(False)
    start, attempted = threading.Barrier(2), threading.Barrier(2)
    won, lost, errors = [], [], []
    def compete():
        token = None
        try:
            start.wait(2)
            try:
                token = core.claim_owner_pump(lambda event: None)
                won.append(threading.get_ident())
            except RuntimeError as exc:
                lost.append(str(exc))
            attempted.wait(2)
            if token is not None:
                core.release_owner_pump(token)
        except BaseException as exc:
            errors.append(exc)
    workers = [threading.Thread(target=compete) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()
    assert not errors and len(won) == len(lost) == 1
    assert "already installed or claimed" in lost[0]
