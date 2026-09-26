"""Actual authored opcodes exercise source/native physical time, without BYO assets."""

import io

import pytest
from pyboy import PyBoy
from pyboy.utils import PyBoyAssertException

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


@pytest.fixture
def cgb_emulator(tmp_path):
    data = bytearray(32768)
    data[0x100:0x103] = b"\xc3\x50\x01"
    data[0x143] = 0x80
    data[0x14D] = (-sum(data[0x134:0x14D]) - 25) & 255
    path = tmp_path / "original-physical-clock.gb"
    path.write_bytes(data)
    p = PyBoy(str(path), window="null", sound_emulated=False)
    p.set_emulation_speed(0)
    p.memory[0xFF50] = 1
    p.register_file.PC = 0x150
    try:
        yield p
    finally:
        p.stop(save=False)


def instruction(p, opcodes):
    p.memory[0, 0x150 : 0x150 + len(opcodes)] = opcodes
    p.register_file.PC = 0x150
    p.mb.breakpoint_singlestep = True
    p.mb.lcd.frame_done = False
    p.mb.tick()


def switch(p):
    p.memory[0xFF4D] = 1
    instruction(p, [0x10, 0])  # Actual STOP opcode, not a speed-field write.


def test_stop_transitions_account_at_actual_boundary(cgb_emulator):
    p = cgb_emulator
    epoch, start = p.mb.get_physical_clock()
    instruction(p, [0])
    assert p.mb.get_physical_clock() == (epoch, start + 8)
    switch(p)
    assert p.mb.get_physical_clock() == (epoch, start + 12)
    instruction(p, [0])
    assert p.mb.get_physical_clock() == (epoch, start + 16)
    switch(p)
    assert p.mb.get_physical_clock() == (epoch, start + 24)
    instruction(p, [0])
    assert p.mb.get_physical_clock() == (epoch, start + 32)


@pytest.mark.parametrize("double", [False, True])
def test_key1_write_does_not_fabricate_actual_speed(cgb_emulator, double):
    p = cgb_emulator
    if double:
        switch(p)
    before = p.mb.get_physical_clock()
    p.memory[0xFF4D] = 1  # Native implementation stores this raw; bit7 becomes0.
    instruction(p, [0])
    assert p.mb.get_physical_clock() == (before[0], before[1] + (4 if double else 8))


@pytest.mark.parametrize("double", [False, True])
def test_serial_period_stays_512_cpu_tcycles(cgb_emulator, double):
    p = cgb_emulator
    if double:
        switch(p)
    observed = []

    class Backend:
        def on_edge(self, _bit, _role):
            observed.append((p.mb.serial.clock, p.mb.get_physical_clock()[1]))
            return 1

    p.mb.serial.backend = Backend()
    p.memory[0xFF01] = 0
    p.memory[0xFF02] = 0x81
    start = (p.mb.serial.clock, p.mb.get_physical_clock()[1])
    for _ in range(1024):
        instruction(p, [0])
    assert len(observed) == 8
    assert [clock - start[0] for clock, _ in observed] == [512 * n for n in range(1, 9)]
    assert [physical - start[1] for _, physical in observed] == [
        512 * (1 if double else 2) * n for n in range(1, 9)
    ]
    assert not p.mb.serial.transfer_enabled


def test_lazy_clock_visible_inside_pre_mmio_owner_callback(cgb_emulator):
    p = cgb_emulator
    switch(p)
    before = p.mb.get_physical_clock()
    observed = []
    p.mb.serial.set_owner_pump(lambda event: observed.append((event, p.mb.get_physical_clock())))
    instruction(p, [0xF0, 1])  # LDH A,[$FF01]: callback before read commit.
    assert len(observed) == 1
    assert observed[0][0][1] == 1
    # LDH_F0 advances four CPU cycles before its MMIO read, then eight after.
    assert observed[0][1] == (before[0], before[1] + 4)
    assert p.mb.get_physical_clock() == (before[0], before[1] + 12)
    p.mb.serial.set_owner_pump(None)


def test_load_rebases_raw_cycles_and_changes_epoch_without_rewinding_time(cgb_emulator):
    p = cgb_emulator
    saved = io.BytesIO()
    p.save_state(saved)
    switch(p)
    instruction(p, [0])
    before = p.mb.get_physical_clock()
    saved.seek(0)
    p.load_state(saved)
    assert p.mb.get_physical_clock() == (before[0] + 1, before[1])
    instruction(p, [0])
    assert p.mb.get_physical_clock() == (before[0] + 1, before[1] + 8)


def test_incomplete_load_faults_clock_until_successful_reload(cgb_emulator):
    p = cgb_emulator
    saved = io.BytesIO()
    p.save_state(saved)
    with pytest.raises(PyBoyAssertException):
        p.load_state(io.BytesIO(b""))
    with pytest.raises(RuntimeError, match="state load is incomplete"):
        p.mb.get_physical_clock()
    saved.seek(0)
    p.load_state(saved)
    assert p.mb.get_physical_clock()[0] == 2


def test_clock_does_not_wrap_at_lcd_frames(cgb_emulator):
    p = cgb_emulator
    p.memory[0, 0x150:0x153] = [0xC3, 0x50, 0x01]
    p.register_file.PC = 0x150
    p.tick(2, False, False)
    first = p.mb.get_physical_clock()
    p.tick(2, False, False)
    second = p.mb.get_physical_clock()
    assert first[0] == second[0] == 0 and second[1] > first[1] > 0


@pytest.mark.parametrize(
    "double_a,double_b", [(False, False), (False, True), (True, False), (True, True)]
)
def test_actual_mixed_speed_pair_frame_accounting(cgb_emulator, tmp_path, double_a, double_b):
    a = cgb_emulator
    b = PyBoy(str(tmp_path / "original-physical-clock.gb"), window="null", sound_emulated=False)
    link = PyBoyLinkSession.local()
    try:
        b.memory[0xFF50] = 1
        for p, double in ((a, double_a), (b, double_b)):
            if double:
                switch(p)
            p.memory[0, 0x150:0x153] = [0xC3, 0x50, 0x01]
            p.register_file.PC = 0x150
            link.attach(p)
        link.step(1)
        elapsed = [now - origin for now, origin in zip(link._physical_now, link._physical_origins)]
        raw = [now - origin for now, origin in zip(link._epoch_expected, link._epoch_origins)]
        assert all(value > 0 for value in elapsed)
        assert abs(elapsed[0] - elapsed[1]) <= 32
        assert elapsed == [raw[0] * (1 if double_a else 2), raw[1] * (1 if double_b else 2)]
    finally:
        link.detach_all()
        b.stop(save=False)


@pytest.mark.parametrize("initial_double", [False, True])
def test_actual_stop_transition_during_owned_pair_frame(cgb_emulator, tmp_path, initial_double):
    a = cgb_emulator
    b = PyBoy(str(tmp_path / "original-physical-clock.gb"), window="null", sound_emulated=False)
    link = PyBoyLinkSession.local()
    try:
        b.memory[0xFF50] = 1
        if initial_double:
            switch(a)
        # LD A,1; LDH [$FF4D],A; STOP; JP $0156. Twenty old-rate
        # CPU cycles precede STOP; STOP's four cycles use the new rate.
        a.memory[0, 0x150:0x159] = [0x3E, 1, 0xE0, 0x4D, 0x10, 0, 0xC3, 0x56, 1]
        b.memory[0, 0x150:0x153] = [0xC3, 0x50, 1]
        a.register_file.PC = b.register_file.PC = 0x150
        link.attach(a)
        link.attach(b)
        link.step_interleaved(1)
        raw = link._epoch_expected[0] - link._epoch_origins[0]
        physical = link._physical_now[0] - link._physical_origins[0]
        assert physical == 20 * (1 if initial_double else 2) + (raw - 20) * (
            2 if initial_double else 1
        )
        other = link._physical_now[1] - link._physical_origins[1]
        assert abs(physical - other) <= 32
    finally:
        link.detach_all()
        b.stop(save=False)


def test_repeated_speed_changes_and_loads_keep_exact_elapsed_units(cgb_emulator):
    # Retained from the independent physical-time review.
    p = cgb_emulator
    saved = io.BytesIO()
    p.save_state(saved)
    epoch, expected = p.mb.get_physical_clock()
    for _ in range(12):
        instruction(p, [0])
        expected += 8
        switch(p)
        expected += 4
        instruction(p, [0])
        expected += 4
        switch(p)
        expected += 8
        assert p.mb.get_physical_clock() == (epoch, expected)
        saved.seek(0)
        p.load_state(saved)
        epoch += 1
        assert p.mb.get_physical_clock() == (epoch, expected)


@pytest.mark.parametrize("double", [False, True])
def test_repeated_callback_queries_do_not_double_charge_time(cgb_emulator, double):
    # Retained from the independent physical-time review.
    p = cgb_emulator
    if double:
        switch(p)
    rate = 1 if double else 2
    epoch, before = p.mb.get_physical_clock()
    observations = []

    def pump(_event):
        observations.append(p.mb.get_physical_clock())
        assert p.mb.get_physical_clock() == observations[-1]

    p.mb.serial.set_owner_pump(pump)
    try:
        instruction(p, [0xF0, 1])
    finally:
        p.mb.serial.set_owner_pump(None)
    assert observations == [(epoch, before + 4 * rate)]
    assert p.mb.get_physical_clock() == (epoch, before + 12 * rate)


@pytest.mark.parametrize(
    "double_a,double_b", [(False, False), (False, True), (True, False), (True, True)]
)
@pytest.mark.parametrize("internal_side", [0, 1])
def test_actual_pair_exchanges_byte_and_services_each_irq_once(
    cgb_emulator,
    tmp_path,
    double_a,
    double_b,
    internal_side,
):
    a = cgb_emulator
    b = PyBoy(str(tmp_path / "original-physical-clock.gb"), window="null", sound_emulated=False)
    link = PyBoyLinkSession.local()
    values = (0xA5, 0x3C)
    try:
        b.memory[0xFF50] = 1
        for side, (p, double) in enumerate(((a, double_a), (b, double_b))):
            if double:
                switch(p)
            # IRQ handler: LD A,[$C000]; INC A; LD [$C000],A; RETI.
            # The counter measures executed serial IRQ handlers, not callbacks.
            p.memory[0, 0x58:0x60] = [0xFA, 0, 0xC0, 0x3C, 0xEA, 0, 0xC0, 0xD9]
            program = [
                0xF3,  # DI
                0x31,
                0xFE,
                0xDF,  # LD SP,$DFFE
                0xAF,  # XOR A
                0xEA,
                0,
                0xC0,  # LD [$C000],A: initialize IRQ counter
                0xE0,
                0x0F,  # LDH [$FF0F],A: clear pending IRQs
                0x3E,
                8,  # LD A,8
                0xEA,
                0xFF,
                0xFF,  # LD [$FFFF],A: serial IRQ only
                0xFB,  # EI
                0x3E,
                values[side],  # LD A, outgoing byte
                0xE0,
                1,  # LDH [$FF01],A: SB
                0x3E,
                0x81 if side == internal_side else 0x80,
                0xE0,
                2,  # LDH [$FF02],A: SC
            ]
            loop = 0x150 + len(program)
            program += [0xC3, loop & 255, loop >> 8]
            p.memory[0, 0x150 : 0x150 + len(program)] = program
            p.register_file.PC = 0x150
            link.attach(p)
        link.step(1)
        assert a.mb.serial.SB == values[1] and b.mb.serial.SB == values[0]
        assert a.mb.serial.SC & 0x80 == b.mb.serial.SC & 0x80 == 0
        assert a.memory[0xC000] == b.memory[0xC000] == 1
        physical = [now - origin for now, origin in zip(link._physical_now, link._physical_origins)]
        raw = [now - origin for now, origin in zip(link._epoch_expected, link._epoch_origins)]
        assert physical == [raw[0] * (1 if double_a else 2), raw[1] * (1 if double_b else 2)]
        assert abs(physical[0] - physical[1]) <= 64
    finally:
        link.detach_all()
        b.stop(save=False)
