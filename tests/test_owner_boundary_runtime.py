"""Native-extension regressions for owner-boundary scheduling.

These tests intentionally use the public PyBoy memory/register surface and
real CPU opcodes.  The source-only introspection tests live separately in
``test_owner_boundary_post.py`` because cdef-private fields are not available
from a compiled PyBoy extension.  The STOP deadline regression is explicitly
diagnostic: it uses the serial scheduler's private countdown field to force
the overdue-deadline ordering that ordinary public frame stepping cannot
reach deterministically.
"""
from __future__ import annotations

import threading
import io
from pathlib import Path

import pytest

from pyboy import PyBoy
from pyboy.core.serial import SerialBackendError


def _blank_cgb_rom(path: Path) -> None:
    data = bytearray(32768)
    data[0x100:0x103] = b"\xc3\x50\x01"
    data[0x150:0x153] = b"\xc3\x50\x01"
    data[0x143] = 0x80
    data[0x14D] = (-sum(data[0x134:0x14D]) - 25) & 0xFF
    path.write_bytes(data)


def test_native_poll_observations_do_not_leak_post_tokens(tmp_path: Path) -> None:
    rom = tmp_path / "poll-boundaries.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    try:
        serial = pyboy.mb.serial
        pre = []
        post = []
        serial.set_owner_boundary_callbacks(pre.append, post.append)
        claim = serial.claim_owner_pump(lambda _event: None, poll=True)
        try:
            # A full frame performs many kind-4 observations.  Releasing the
            # owner and disabling callbacks must remain possible afterward;
            # an observation-only boundary cannot leave a pending post token.
            pyboy.tick(1, render=False, sound=False)
        finally:
            serial.release_owner_pump(claim)
        serial.set_owner_boundary_callbacks(None, None)
        assert any(item.kind == 4 for item in pre)
        assert not any(item.kind == 4 for item in post)
    finally:
        pyboy.stop(save=False)


def test_native_stop_keeps_overdue_internal_deadline_mapping(tmp_path: Path) -> None:
    rom = tmp_path / "stop-deadline.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    try:
        pyboy.set_emulation_speed(0)
        pyboy.memory[0xFF50] = 1

        # Arm a real master transfer through FF01/FF02.  The test then lets a
        # single CPU scheduling slice run past the first (512 T-cycle) edge;
        # the STOP opcode changes CGB speed before serial.tick services it.
        pyboy.memory[0xFF01] = 0xA5
        pyboy.memory[0xFF02] = 0x81
        serial = pyboy.mb.serial
        serial._cycles_to_interrupt = 1 << 31

        # 150 NOPs advance the CPU to 600 T-cycles, then KEY1+STOP performs an
        # actual speed switch while the deadline at raw serial cycle 512 is
        # still pending.  The remainder keeps execution in harmless NOPs.
        program = [0x00] * 150 + [0x3E, 0x01, 0xEA, 0x4D, 0xFF, 0x10, 0x00]
        program += [0x00] * 32
        pyboy.memory[0, 0x150 : 0x150 + len(program)] = program
        pyboy.register_file.PC = 0x150

        posts = []
        serial.set_owner_boundary_callbacks(None, posts.append)
        pyboy.tick(1, render=False, sound=False)

        first_edge = next(
            item
            for item in posts
            if item.kind == 3 and item.effective_cycles == 512
        )
        assert first_edge.committed
        assert pyboy.memory[0xFF02] & 0x80 == 0
        assert pyboy.memory[0xFF01] == 0xFF
    finally:
        pyboy.stop(save=False)


def test_native_two_speed_switches_preserve_overdue_mapping_segments(
    tmp_path: Path,
) -> None:
    """Two STOP switches must not erase the middle catch-up rate segment."""
    rom = tmp_path / "two-stop-deadline.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    try:
        pyboy.set_emulation_speed(0)
        pyboy.memory[0xFF50] = 1
        pyboy.memory[0xFF01] = 0xA5
        pyboy.memory[0xFF02] = 0x81
        serial = pyboy.mb.serial

        # This private scheduler countdown is intentionally diagnostic: it
        # lets one CPU slice execute both STOP speed switches before the
        # serial owner services the overdue deadlines.  All observations and
        # assertions remain on the public callback records.
        serial._cycles_to_interrupt = 1 << 31

        # The first switch occurs at raw 624 (after 150 NOPs and the KEY1 /
        # STOP sequence).  The second occurs at raw 1224 after 143 NOPs.  The
        # first two serial deadlines (512 and 1024) therefore map to 1024 and
        # 1648 physical half-cycles respectively.
        program = [0x00] * 150 + [0x3E, 0x01, 0xEA, 0x4D, 0xFF, 0x10, 0x00]
        program += [0x00] * 143 + [0x3E, 0x01, 0xEA, 0x4D, 0xFF, 0x10, 0x00]
        program += [0x00] * 100
        pyboy.memory[0, 0x150 : 0x150 + len(program)] = program
        pyboy.register_file.PC = 0x150

        posts = []
        serial.set_owner_boundary_callbacks(None, posts.append)
        pyboy.tick(1, render=False, sound=False)

        edge_posts = [item for item in posts if item.kind == 3]
        assert len(edge_posts) == 8
        assert [item.effective_cycles for item in edge_posts] == [
            512 * index for index in range(1, 9)
        ]
        assert [item.effective_physical_units for item in edge_posts] == [
            1024,
            1648,
            2472,
            3496,
            4520,
            5544,
            6568,
            7592,
        ]
        assert all(item.committed for item in edge_posts)
    finally:
        pyboy.stop(save=False)


def test_native_mapper_registration_does_not_bind_constructor_thread(tmp_path: Path) -> None:
    rom = tmp_path / "mapper-thread.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    try:
        serial = pyboy.mb.serial
        errors: list[BaseException] = []

        def install_from_worker() -> None:
            try:
                serial.set_owner_time_mapper(lambda _kind, _observed, effective: (0, effective))
            except BaseException as error:  # pragma: no cover - assertion below reports it
                errors.append(error)

        worker = threading.Thread(target=install_from_worker)
        worker.start()
        worker.join(3)
        assert not worker.is_alive()
        assert errors == []
        # Do not leave the worker-created callable attached to the stopped
        # emulator while the next native test constructs another instance.
        serial.set_owner_time_mapper(None)
    finally:
        pyboy.stop(save=False)
        pyboy = None
        serial = None
        worker = None


def test_native_clearing_callbacks_releases_idle_owner_for_worker_claim(tmp_path: Path) -> None:
    rom = tmp_path / "callback-release.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    try:
        serial = pyboy.mb.serial
        serial.set_owner_boundary_callbacks(lambda _item: None, None)

        # An active owner claim keeps callback affinity and rejects an
        # off-owner callback mutation.
        claim = serial.claim_owner_pump(lambda _event: None)
        active_errors: list[BaseException] = []

        def mutate_while_claimed() -> None:
            try:
                serial.set_owner_boundary_callbacks(None, None)
            except BaseException as error:  # pragma: no cover - assertion below
                active_errors.append(error)

        active_worker = threading.Thread(target=mutate_while_claimed)
        active_worker.start()
        active_worker.join(3)
        assert not active_worker.is_alive()
        assert len(active_errors) == 1
        assert isinstance(active_errors[0], RuntimeError)

        serial.release_owner_pump(claim)
        serial.set_owner_boundary_callbacks(None, None)

        # After the last explicit callback is cleared, a distinct worker can
        # claim and release the now-idle serial owner.
        worker_errors: list[BaseException] = []

        def claim_from_worker() -> None:
            try:
                token = serial.claim_owner_pump(lambda _event: None)
                serial.release_owner_pump(token)
            except BaseException as error:  # pragma: no cover - assertion below
                worker_errors.append(error)

        worker = threading.Thread(target=claim_from_worker)
        worker.start()
        worker.join(3)
        assert not worker.is_alive()
        assert worker_errors == []
    finally:
        pyboy.stop(save=False)


def test_native_distinct_threads_can_claim_after_mapper_only_release(tmp_path: Path) -> None:
    rom = tmp_path / "claim-release.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    first_released = threading.Event()
    allow_first_exit = threading.Event()
    errors: list[BaseException] = []
    try:
        serial = pyboy.mb.serial

        def first_owner() -> None:
            try:
                token = serial.claim_owner_pump(lambda _event: None)
                serial.release_owner_pump(token)
                first_released.set()
                allow_first_exit.wait(3)
            except BaseException as error:  # pragma: no cover - assertion below reports it
                errors.append(error)
                first_released.set()

        first = threading.Thread(target=first_owner)
        first.start()
        assert first_released.wait(3)

        def second_owner() -> None:
            try:
                token = serial.claim_owner_pump(lambda _event: None)
                serial.release_owner_pump(token)
            except BaseException as error:  # pragma: no cover - assertion below reports it
                errors.append(error)

        second = threading.Thread(target=second_owner)
        second.start()
        second.join(3)
        allow_first_exit.set()
        first.join(3)
        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
    finally:
        allow_first_exit.set()
        pyboy.stop(save=False)


def test_native_mmio_boundaries_have_exact_pre_post_pairs(tmp_path: Path) -> None:
    """Each public FF01/FF02 write has one matching committed pair."""
    rom = tmp_path / "mmio-pairs.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    try:
        serial = pyboy.mb.serial
        pre = []
        post = []
        serial.set_owner_boundary_callbacks(pre.append, post.append)

        pyboy.memory[0xFF01] = 0x3C
        pyboy.memory[0xFF02] = 0x81

        assert [(item.kind, item.event[4], item.event[5]) for item in pre] == [
            (2, 0xFF01, 0x3C),
            (2, 0xFF02, 0x81),
        ]
        assert len(post) == len(pre) == 2
        assert [item.boundary_seq for item in post] == [
            item.boundary_seq for item in pre
        ]
        assert all(item.kind == 2 and item.committed for item in post)
        assert all(item.parent_boundary_seq is None for item in pre + post)
        assert all(item.physical_epoch == 0 for item in pre + post)
        assert all(item.effective_physical_units == 0 for item in pre + post)
        assert serial.SB == 0x3C
        assert serial.SC & 0x80
    finally:
        pyboy.stop(save=False)


def test_native_internal_transfer_emits_eight_committed_edge_posts(tmp_path: Path) -> None:
    """A public master transfer produces exactly eight committed edge pairs."""
    rom = tmp_path / "eight-edges.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    try:
        serial = pyboy.mb.serial
        pre = []
        post = []
        serial.set_owner_boundary_callbacks(pre.append, post.append)

        pyboy.memory[0xFF01] = 0xA5
        pyboy.memory[0xFF02] = 0x81
        mmio_pre = [item for item in pre if item.kind == 2]
        mmio_post = [item for item in post if item.kind == 2]
        assert len(mmio_pre) == len(mmio_post) == 2

        pyboy.tick(1, render=False, sound=False)
        edge_pre = [item for item in pre if item.kind == 3]
        edge_post = [item for item in post if item.kind == 3]
        assert len(edge_pre) == len(edge_post) == 8
        assert [item.boundary_seq for item in edge_pre] == [
            item.boundary_seq for item in edge_post
        ]
        assert all(item.committed for item in edge_post)
        assert all(item.parent_boundary_seq is None for item in edge_pre + edge_post)
        assert [item.effective_cycles for item in edge_post] == [
            512 * index for index in range(1, 9)
        ]
        assert all(item.physical_epoch == 0 for item in edge_post)
        assert serial.SB == 0xFF
        assert not (serial.SC & 0x80)
    finally:
        pyboy.stop(save=False)


def test_native_post_failure_latches_first_cause_after_committed_edge(
    tmp_path: Path,
) -> None:
    """A failing post observer sees the already-committed edge and first cause."""
    rom = tmp_path / "post-failure.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    marker = RuntimeError("first post failure")
    posts = []

    def fail_on_first_edge(item) -> None:
        posts.append(item)
        if item.kind == 3:
            raise marker

    try:
        serial = pyboy.mb.serial
        serial.set_owner_boundary_callbacks(None, fail_on_first_edge)
        pyboy.memory[0xFF01] = 0xA5
        pyboy.memory[0xFF02] = 0x81

        with pytest.raises(SerialBackendError) as raised:
            pyboy.tick(1, render=False, sound=False)

        edge_posts = [item for item in posts if item.kind == 3]
        assert len(edge_posts) == 1
        edge = edge_posts[0]
        assert edge.committed
        assert edge.effective_cycles == 512
        assert edge.snapshot.transfer_enabled
        assert edge.snapshot.bits_remaining == 7
        assert edge.snapshot.shift_register == 0x4B
        assert raised.value.__cause__ is marker
        with pytest.raises(SerialBackendError) as checked:
            serial.check_error()
        assert checked.value.__cause__ is marker
    finally:
        pyboy.stop(save=False)


def test_native_load_state_starts_new_epoch_and_replays_edges(tmp_path: Path) -> None:
    """A public save/load restores the transfer and replays it in a new epoch."""
    rom = tmp_path / "load-replay.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    try:
        serial = pyboy.mb.serial
        pre = []
        post = []
        serial.set_owner_boundary_callbacks(pre.append, post.append)
        pyboy.memory[0xFF01] = 0x3C
        pyboy.memory[0xFF02] = 0x81

        state = io.BytesIO()
        pyboy.save_state(state)
        state.seek(0)
        pyboy.tick(1, render=False, sound=False)
        first_edge_posts = [item for item in post if item.kind == 3]
        assert len(first_edge_posts) == 8
        assert serial.SB == 0xFF

        state.seek(0)
        pyboy.load_state(state)
        # State loading itself can perform the restored serial MMIO reads;
        # only the subsequent edge replay is relevant to this assertion.
        pre.clear()
        post.clear()
        pyboy.tick(1, render=False, sound=False)

        replay_pre = [item for item in pre if item.kind == 3]
        replay_post = [item for item in post if item.kind == 3]
        assert len(replay_pre) == len(replay_post) == 8
        assert [item.boundary_seq for item in replay_pre] == [
            item.boundary_seq for item in replay_post
        ]
        assert all(item.committed for item in replay_post)
        assert all(item.physical_epoch == 1 for item in replay_pre + replay_post)
        assert [item.effective_cycles for item in replay_post] == [
            512 * index for index in range(1, 9)
        ]
        assert serial.SB == 0xFF
        assert not (serial.SC & 0x80)
    finally:
        pyboy.stop(save=False)


def test_native_active_claim_rejects_pre_and_post_callback_replacement(
    tmp_path: Path,
) -> None:
    """An exclusive owner claim rejects both callback replacement paths."""
    rom = tmp_path / "claim-callback-replacement.gbc"
    _blank_cgb_rom(rom)
    pyboy = PyBoy(str(rom), window="null", sound_emulated=False)
    try:
        serial = pyboy.mb.serial
        original_pre = lambda _item: None
        original_post = lambda _item: None
        serial.set_owner_boundary_callbacks(original_pre, original_post)
        claim = serial.claim_owner_pump(lambda _event: None)
        try:
            with pytest.raises(RuntimeError, match="exclusively claimed"):
                serial.set_owner_boundary_callbacks(lambda _item: None, original_post)
            with pytest.raises(RuntimeError, match="exclusively claimed"):
                serial.set_owner_boundary_callbacks(original_pre, lambda _item: None)
        finally:
            serial.release_owner_pump(claim)
        serial.set_owner_boundary_callbacks(None, None)
    finally:
        pyboy.stop(save=False)
