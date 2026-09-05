"""Authored CGB state regressions using real CPU speed switches and governors.

No commercial assets or Python RAM/register edits. These selected-runtime
regressions do not establish Pokemon gameplay acceptance.
"""

from io import BytesIO

import pytest

from pokered_harness.link.emulated_time import EmulatedTimeCoordinator
from pokered_harness.link.execution_adapter import ExecutionGovernorAdapter


def save_bytes(game):
    state = BytesIO()
    game.save_state(state)
    return state.getvalue()


@pytest.fixture(params=[False, True], ids=["normal-speed", "double-speed"])
def authored_state(tmp_path, request):
    from pyboy import PyBoy
    from pyboy.core import cpu, mb, serial

    double_speed = request.param
    boot = bytearray(0x900)  # CGB boot-ROM size, including the unmapped header gap.
    boot[:6] = bytes([0x31, 0x00, 0xD0, 0xC3, 0xFC, 0x00])  # SP; JP $00fc
    boot[252:256] = bytes([0x3E, 0x01, 0xE0, 0x50])  # CPU unmaps boot at $0100.
    cartridge = bytearray(0x8000)
    cartridge[0x100:0x103] = bytes([0xC3, 0x50, 0x01])
    cartridge[0x134:0x13C] = b"SPEEDROM"
    cartridge[0x143] = 0x80  # CGB capable.
    # DI; clear serial control; enable LCD. All device writes are CPU opcodes.
    program = bytes([0xF3, 0xAF, 0xE0, 0x02, 0x3E, 0x91, 0xE0, 0x40])
    # LDH A,(KEY1); OR 1; LDH (KEY1),A; STOP 0. Preserve the current rate
    # bit while arming each switch. Normal speed is reached by switching
    # twice, so both cases prove the actual CPU speed-transition path.
    switches = 1 if double_speed else 2
    program += bytes([0xF0, 0x4D, 0xF6, 0x01, 0xE0, 0x4D, 0x10, 0x00]) * switches
    loop_pc = 0x150 + len(program)
    program += bytes([0x18, 0xFE])  # JR to itself, 12 CPU cycles per retirement.
    cartridge[0x150 : 0x150 + len(program)] = program
    cartridge[0x14D] = (-sum(cartridge[0x134:0x14D]) - 25) & 0xFF
    rom_path, boot_path = tmp_path / "speed.gb", tmp_path / "speed.boot"
    rom_path.write_bytes(cartridge)
    boot_path.write_bytes(boot)
    game = PyBoy(
        str(rom_path), bootrom=str(boot_path), cgb=True, window="null", sound_emulated=False
    )
    try:
        assert type(game.mb) is mb.Motherboard
        assert type(game.mb.cpu) is cpu.CPU
        assert type(game.mb.serial) is serial.Serial
        game.set_emulation_speed(0)
        # LCD enable can end the first public tick before the SPEED program.
        assert game.tick(2, render=False, sound=False)
        # Reaching the cartridge loop after its STOP transitions demonstrates
        # boot unmapping through public execution evidence in both runtimes.
        assert game.register_file.PC == loop_pc
        assert game.mb.speed_transition_count == switches
        assert game.mb.speed_transition_double_speed is double_speed
        assert game.mb.double_speed is double_speed
        assert game.memory[0xFF4D] & 0x81 == (0x80 if double_speed else 0)
        assert game.mb.serial.cpu_speed_shift == int(double_speed)
        state = save_bytes(game)
        saved_clock = game.mb.cpu.cycles
        # Move the real CPU forward before restoring the independently captured state.
        assert game.tick(1, render=False, sound=False)
        assert game.mb.cpu.cycles > saved_clock
        game.load_state(BytesIO(state))
        assert game.mb.cpu.cycles == saved_clock
        assert game.register_file.PC == loop_pc
        yield game, double_speed, state
    finally:
        game.stop(save=False)


def test_save_load_save_preserves_speed_type_and_bytes(authored_state):
    game, double_speed, state = authored_state
    # Byte equality alone cannot distinguish restored int 0/1 from bool.
    assert save_bytes(game) == state
    assert game.mb.double_speed is double_speed
    assert game.memory[0xFF4D] & 0x81 == (0x80 if double_speed else 0)
    assert game.mb.serial.cpu_speed_shift == int(double_speed)


def test_loaded_speed_admits_real_governor_and_cpu_execution(authored_state):
    game, double_speed, state = authored_state
    board = game.mb
    start_clock, start_count = board.cpu.cycles, board.cpu.retired_instructions
    coordinator = EmulatedTimeCoordinator(
        epoch="restored-speed",
        raw_cpu_clock=start_clock,
        double_speed=double_speed,
        rearm_budget=0,
        max_edge_lateness=0,
        quantum_cycles=200_000,
    )
    coordinator.record_peer_progress(epoch="restored-speed", sequence=1, committed_half_cycles=0)

    def unexpected_wait(timeout):
        pytest.fail(f"single authored frame exhausted governor credit: {timeout}")

    adapter = ExecutionGovernorAdapter(
        coordinator,
        instruction_counter=lambda: board.cpu.retired_instructions,
        wait_for_progress=unexpected_wait,
    )
    try:
        # Use the restored value unchanged: strict attach rejects the old int loader.
        adapter.attach(board)
        assert save_bytes(game) == state
        assert game.tick(1, render=False, sound=False)
        elapsed = board.cpu.cycles - start_clock
        retired = board.cpu.retired_instructions - start_count
        assert retired > 0
        assert elapsed == retired * 12
        snapshot = coordinator.snapshot()
        assert snapshot.raw_cpu_clock == board.cpu.cycles
        assert snapshot.local_half_cycles == elapsed * (1 if double_speed else 2)
        assert snapshot.double_speed is double_speed
        assert not snapshot.pending_permit
        assert not snapshot.closed
    finally:
        adapter.detach()
    assert board.execution_before is None
    assert board.execution_after is None
