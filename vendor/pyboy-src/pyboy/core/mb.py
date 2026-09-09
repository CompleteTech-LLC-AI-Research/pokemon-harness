#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#

from array import array

import pyboy
import cython
from pyboy.utils import (
    STATE_VERSION,
    PyBoyException,
    PyBoyOutOfBoundsException,
    PyBoyInvalidOperationException,
    INTR_TIMER,
    INTR_SERIAL,
    INTR_HIGHTOLOW,
    OPCODE_BRK,
    MAX_CYCLES,
)

from . import bootrom, cartridge, cpu, interaction, lcd, ram, serial, sound, timer

logger = pyboy.logging.get_logger(__name__)
PHYSICAL_CLOCK_MAX = 0xFFFFFFFFFFFFFFFF


def _serial_check_error(serial_device):
    callback = getattr(serial_device, "check_error", None)
    if callback is not None:
        callback()


def _serial_check_execution_allowed(serial_device):
    callback = getattr(serial_device, "check_execution_allowed", None)
    if callback is not None:
        callback()


class Motherboard:
    def __init__(
        self,
        gamerom_file,
        ram_file,
        rtc_file,
        bootrom_file,
        color_palette,
        cgb_color_palette,
        sound_volume,
        sound_emulated,
        sound_sample_rate,
        cgb,
        randomize=False,
    ):
        if bootrom_file is not None:
            logger.info("Boot-ROM file provided")

        self.cartridge = cartridge.load_cartridge(gamerom_file, ram_file, rtc_file)
        logger.debug("Cartridge started:\n%s", str(self.cartridge))

        # If the user requested cgb hardware emulation as True or False, it takes
        # precedence. Otherwise we auto-detect from the cartridge.
        if cgb is None:  # Probe
            self.cgb = self.cartridge.cgb
            self.bootrom = bootrom.BootROM(bootrom_file, self.cgb)
            if bootrom_file is not None:
                self.cgb = self.bootrom.cgb
            logger.debug("Auto-detected emulation mode: %s", ("CGB" if self.cgb else "DMG"))
        else:
            self.cgb = cgb
            self.bootrom = bootrom.BootROM(bootrom_file, self.cgb)
            if self.bootrom.cgb != self.cgb:
                raise PyBoyInvalidOperationException("Invalid bootrom for emulation-mode")

        # self.cgb  Controls hw initialization
        self.cgb_mode = self.cgb and self.cartridge.cgb  # Controls access

        self.timer = timer.Timer()
        self.serial = serial.Serial(self.cgb_mode)
        self.interaction = interaction.Interaction()
        self.ram = ram.RAM(self.cgb, randomize=randomize)
        self.cpu = cpu.CPU(self)

        self.lcd = lcd.LCD(
            self.cgb,
            self.cgb_mode,
            color_palette,
            cgb_color_palette,
            randomize=randomize,
        )

        self.sound = sound.Sound(sound_volume, sound_emulated, sound_sample_rate, self.cgb)

        self.key1 = 0
        self.key0 = 0
        self.wram_select = 0
        self.cgb_undocumented = array("B", [0] * 4)
        self.double_speed = False
        self.execution_before = None
        self.execution_after = None
        self._execution_governor_enabled = False
        self._execution_governor_active = False
        self.speed_transition_count = 0
        self.speed_transition_clock = 0
        self.speed_transition_double_speed = False
        # Scheduler time is independent of LCD wrapping and CPU speed. One
        # unit is half a normal-speed T-cycle; raw serial time stays CPU T.
        self._physical_clock = 0
        self._physical_last_cycles = self.cpu.cycles
        self._physical_clock_epoch = 0
        self._physical_clock_fault = False
        self._physical_load_incomplete = False
        # Serial raw deadlines use their own clock domain.  Keep a compact
        # piecewise mapping to the monotonic physical clock so owner-boundary
        # metadata never treats ``clock_target`` as CPU or physical time.
        self._serial_time_segments = [(0, 0, 2)]
        serial_clock = getattr(self.serial, "clock", 0)
        serial_last_cycles = getattr(self.serial, "last_cycles", 0)
        self._serial_raw_offset = int(serial_clock) - int(serial_last_cycles)
        set_owner_time_mapper = getattr(self.serial, "set_owner_time_mapper", None)
        if set_owner_time_mapper is not None:
            set_owner_time_mapper(self._map_serial_boundary_time)

        if self.cgb:
            self.hdma = HDMA()
        else:
            self.hdma = None

        self.bootrom_enabled = True
        self.serialbuffer = [0] * 1024
        self.serialbuffer_count = 0

        self.breakpoints = {}  # {(0, 0x150): (0x100) (0, 0x0040): 0x200, (0, 0x0048): 0x300, (0, 0x0050): 0x44}
        self.breakpoint_singlestep = False
        self.breakpoint_singlestep_latch = False
        self.breakpoint_waiting = -1

    def switch_speed(self):
        if self.key1 & 0b1:
            self._sync_physical_clock()
            with cython.gil:
                self._align_serial_time_offset()
                raw_at_switch = self.cpu.cycles + self._serial_raw_offset
                old_rate = 1 if self.double_speed else 2
                # Native PyBoy builds disable Cython wraparound; spell out
                # the final element instead of using ``[-1]`` here.
                latest_segment = self._serial_time_segments[len(self._serial_time_segments) - 1]
                if latest_segment != (raw_at_switch, self._physical_clock, old_rate):
                    self._serial_time_segments.append((raw_at_switch, self._physical_clock, old_rate))
                self.double_speed = not self.double_speed
                self.speed_transition_count += 1
                self.speed_transition_clock = self.cpu.cycles
                self.speed_transition_double_speed = self.double_speed
                new_rate = 1 if self.double_speed else 2
                self._serial_time_segments.append((raw_at_switch, self._physical_clock, new_rate))
                self._prune_serial_time_segments()
            self.lcd.tick(self.cpu.cycles)
            self.lcd.speed_shift = 1 if self.double_speed else 0
            self.sound.tick(self.cpu.cycles)
            self.sound.speed_shift = 1 if self.double_speed else 0
            # Keep the legacy serial speed-domain hint synchronized for
            # untagged save-state migration and native integrations.
            self.serial.cpu_speed_shift = 1 if self.double_speed else 0
            logger.debug("CGB double speed is now: %d", self.double_speed)
            self.key1 ^= 0b10000001

    def _align_serial_time_offset(self):
        """Rebase raw serial coordinates when a state/load owner does so."""
        offset = int(getattr(self.serial, "clock", 0)) - int(getattr(self.serial, "last_cycles", 0))
        if offset == self._serial_raw_offset:
            return
        delta = offset - self._serial_raw_offset
        self._serial_time_segments = [
            (raw + delta, physical, rate)
            for raw, physical, rate in self._serial_time_segments
        ]
        self._serial_raw_offset = offset

    def _prune_serial_time_segments(self):
        """Retain mappings needed until the next overdue interval is serviced.

        A catch-up ``serial.tick`` can service several overdue edges in one
        call.  Keeping only the segment that anchors the earliest deadline is
        insufficient: later overdue deadlines may fall after one or more
        speed-switch segments and need each intermediate rate.  When an edge
        is overdue, retain the complete suffix from its anchor through the
        current segment.  Once all deadlines are in the future, the current
        segment alone maps every future deadline and older history can be
        discarded.  This keeps retention bounded by the unserviced overdue
        interval rather than by the lifetime of the emulator.
        """
        segments = self._serial_time_segments
        if len(segments) <= 1:
            return
        pending_edge_deadlines = getattr(self.serial, "_owner_pending_edge_deadlines", None)
        deadlines = list(pending_edge_deadlines()) if pending_edge_deadlines is not None else []
        # A scheduled master edge exists before its pre-boundary callback is
        # entered.  Keep its historical mapping anchor across a CGB speed
        # switch as well; otherwise an overdue deadline (for example 512 after
        # the CPU has advanced to 600) predates the newly retained segment and
        # fails closed instead of being serviced.  Slave transfers have no
        # CPU-time deadline and are intentionally excluded.
        if (
            getattr(self.serial, "transfer_enabled", False)
            and getattr(self.serial, "internal_clock", False)
            and getattr(self.serial, "_bits_remaining", 0) > 0
        ):
            deadlines.append(self.serial.clock_target)
        if not deadlines:
            self._serial_time_segments = [segments[len(segments) - 1]]
            return

        # CPU cycles plus this offset is the current raw serial coordinate.
        # ``serial.clock`` can lag while the CPU is executing a long slice;
        # those are precisely the slices that can accumulate multiple
        # overdue edge deadlines before the next serial tick.
        current_raw = int(self.cpu.cycles) + int(self._serial_raw_offset)
        overdue = [deadline for deadline in deadlines if deadline <= current_raw]
        if not overdue:
            self._serial_time_segments = [segments[len(segments) - 1]]
            return

        earliest = min(overdue)
        anchor = 0
        for index, segment in enumerate(segments):
            if earliest >= segment[0]:
                anchor = index
            else:
                break
        # Keep every intermediate segment through the current one.  A single
        # catch-up may visit every raw deadline in this interval.
        self._serial_time_segments = segments[anchor:]

    def _map_serial_boundary_time(self, kind, observed_cycles, effective_cycles):
        """Map one boundary's serial-domain time to ``(epoch, physical)``."""
        self._sync_physical_clock()
        if self._physical_clock_fault:
            raise RuntimeError("physical clock regressed or overflowed; recreate runtime")
        if self._physical_load_incomplete:
            raise RuntimeError("physical clock state load is incomplete")
        self._align_serial_time_offset()
        if kind != 3:
            if observed_cycles != self.cpu.cycles:
                raise RuntimeError("owner boundary CPU time is not current")
            return (self._physical_clock_epoch, self._physical_clock)
        raw_target = int(effective_cycles)
        for raw_origin, physical_origin, rate in reversed(self._serial_time_segments):
            if raw_target >= raw_origin:
                if (
                    raw_origin < 0
                    or physical_origin < 0
                    or rate <= 0
                    or physical_origin > PHYSICAL_CLOCK_MAX
                    or raw_target - raw_origin
                    > (PHYSICAL_CLOCK_MAX - physical_origin) // rate
                ):
                    self._physical_clock_fault = True
                    raise RuntimeError("serial physical-time mapping overflowed")
                return (
                    self._physical_clock_epoch,
                    physical_origin + (raw_target - raw_origin) * rate,
                )
        raise RuntimeError("serial deadline predates the physical mapping")

    def _sync_physical_clock(self):
        if self._physical_clock_fault or self._physical_load_incomplete:
            return
        if self.cpu.cycles < 0:
            self._physical_clock_fault = True
            return
        cycles = self.cpu.cycles
        if cycles < self._physical_last_cycles:
            self._physical_clock_fault = True
            return
        delta = cycles - self._physical_last_cycles
        rate = 1 if self.double_speed else 2
        if delta > (PHYSICAL_CLOCK_MAX - self._physical_clock) // rate:
            self._physical_clock_fault = True
            return
        self._physical_clock += delta * rate
        self._physical_last_cycles = cycles

    def get_physical_clock(self):
        """Return (load epoch, monotonic half-normal-T elapsed time).

        Lazy synchronization also makes this exact inside a pre-MMIO or
        serial owner callback. KEY1's writable bits are not the speed source.
        State loads preserve elapsed time but change epoch and rebase CPU time;
        this metadata is intentionally not part of the save-state format.
        """
        self._sync_physical_clock()
        if self._physical_clock_fault:
            raise RuntimeError("physical clock regressed or overflowed; recreate runtime")
        if self._physical_load_incomplete:
            raise RuntimeError("physical clock state load is incomplete")
        return (self._physical_clock_epoch, self._physical_clock)

    def breakpoint_add(self, bank, addr):
        # Replace instruction at address with OPCODE_BRK and save original opcode
        # for later reinsertion and when breakpoint is deleted.
        if addr < 0x100 and bank == -1:
            opcode = self.bootrom.bootrom[addr]
            self.bootrom.bootrom[addr] = OPCODE_BRK
        elif addr < 0x4000:
            if self.cartridge.external_rom_count < bank:
                raise PyBoyOutOfBoundsException(
                    f"ROM bank out of bounds. Asked for {bank}, max is {self.cartridge.external_rom_count}"
                )
            opcode = self.cartridge.rombanks[bank, addr]
            self.cartridge.rombanks[bank, addr] = OPCODE_BRK
        elif 0x4000 <= addr < 0x8000:
            if self.cartridge.external_rom_count < bank:
                raise PyBoyOutOfBoundsException(
                    f"ROM bank out of bounds. Asked for {bank}, max is {self.cartridge.external_rom_count}"
                )
            opcode = self.cartridge.rombanks[bank, addr - 0x4000]
            self.cartridge.rombanks[bank, addr - 0x4000] = OPCODE_BRK
        elif 0x8000 <= addr < 0xA000:
            if bank == 0:
                opcode = self.lcd.VRAM0[addr - 0x8000]
                self.lcd.VRAM0[addr - 0x8000] = OPCODE_BRK
            else:
                opcode = self.lcd.VRAM1[addr - 0x8000]
                self.lcd.VRAM1[addr - 0x8000] = OPCODE_BRK
        elif 0xA000 <= addr < 0xC000:
            if self.cartridge.external_ram_count < bank:
                raise PyBoyOutOfBoundsException(
                    f"RAM bank out of bounds. Asked for {bank}, max is {self.cartridge.external_ram_count}"
                )
            opcode = self.cartridge.rambanks[bank, addr - 0xA000]
            self.cartridge.rambanks[bank, addr - 0xA000] = OPCODE_BRK
        elif 0xC000 <= addr <= 0xE000:
            opcode = self.ram.internal_ram0[addr - 0xC000]
            self.ram.internal_ram0[addr - 0xC000] = OPCODE_BRK
        else:
            raise PyBoyOutOfBoundsException(
                "Unsupported breakpoint address. If this a mistake, reach out to the developers"
            )

        self.breakpoints[(bank, addr)] = opcode

    def breakpoint_find(self, bank, addr):
        opcode = self.breakpoints.get((bank, addr))
        if opcode is not None:
            return (bank, addr, opcode)
        return tuple()

    def breakpoint_remove(self, bank, addr):
        logger.debug(f"Breakpoint remove: ({bank}, {addr})")
        opcode = self.breakpoints.pop((bank, addr), None)
        if opcode is not None:
            logger.debug(f"Breakpoint remove: {bank:02x}:{addr:04x} {opcode:02x}")

            # Restore opcode
            if addr < 0x100 and bank == -1:
                self.bootrom.bootrom[addr] = opcode
            elif addr < 0x4000:
                self.cartridge.rombanks[bank, addr] = opcode
            elif 0x4000 <= addr < 0x8000:
                self.cartridge.rombanks[bank, addr - 0x4000] = opcode
            elif 0x8000 <= addr < 0xA000:
                if bank == 0:
                    self.lcd.VRAM0[addr - 0x8000] = opcode
                else:
                    self.lcd.VRAM1[addr - 0x8000] = opcode
            elif 0xA000 <= addr < 0xC000:
                self.cartridge.rambanks[bank, addr - 0xA000] = opcode
            elif 0xC000 <= addr <= 0xE000:
                self.ram.internal_ram0[addr - 0xC000] = opcode
            else:
                raise PyBoyException("Unsupported breakpoint address. If this a mistake, reach out to the developers")
        else:
            raise PyBoyException("Breakpoint not found. If this a mistake, reach out to the developers")

    def breakpoint_reached(self):
        pc = self.cpu.PC
        bank = None
        if pc < 0x100 and self.bootrom_enabled:
            bank = -1
        elif pc < 0x4000:
            bank = 0
        elif 0x4000 <= pc < 0x8000:
            bank = self.cartridge.rombank_selected
        elif 0xA000 <= pc < 0xC000:
            bank = self.cartridge.rambank_selected
        elif 0xC000 <= pc <= 0xFFFF:
            bank = 0
        opcode = self.breakpoints.get((bank, pc))
        if opcode is not None:
            # Breakpoint hit
            addr = pc
            logger.debug("Breakpoint reached: %02x:%04x %02x", bank, addr, opcode)
            self.breakpoint_waiting = (bank & 0xFF) << 24 | (addr & 0xFFFF) << 8 | (opcode & 0xFF)
            logger.debug("Breakpoint waiting: %08x", self.breakpoint_waiting)
            return (bank, addr, opcode)
        if not self.breakpoint_singlestep:
            logger.debug("Invalid breakpoint reached: %04x", self.cpu.PC)
        return (-1, -1, -1)

    def breakpoint_reinject(self):
        if self.breakpoint_waiting < 0:
            return  # skip
        bank = (self.breakpoint_waiting >> 24) & 0xFF
        # TODO: Improve signedness
        if bank == 0xFF:
            bank = -1
        addr = (self.breakpoint_waiting >> 8) & 0xFFFF
        logger.debug("Breakpoint reinjecting: %02x:%02x", bank, addr)
        self.breakpoint_add(bank, addr)
        self.breakpoint_waiting = -1

    def getserial(self):
        b = "".join([chr(x) for x in self.serialbuffer[: self.serialbuffer_count]])
        self.serialbuffer_count = 0
        return b

    def buttonevent(self, key):
        if self.interaction.key_event(key):
            self.cpu.set_interruptflag(INTR_HIGHTOLOW)

    def stop(self, save, ram_file, rtc_file):
        if save:
            self.cartridge.stop(ram_file, rtc_file)

    def save_state(self, f):
        _serial_check_error(self.serial)
        logger.debug("Saving state...")
        f.write(STATE_VERSION)
        f.write(self.bootrom_enabled)
        f.write(self.key0)
        f.write(self.key1)
        f.write(self.double_speed)
        f.write(self.cgb)
        if self.cgb:
            self.hdma.save_state(f)
        self.cpu.save_state(f)
        self.lcd.save_state(f)
        self.sound.save_state(f)
        self.lcd.renderer.save_state(f)
        self.ram.save_state(f)
        f.write(self.wram_select)
        self.timer.save_state(f)
        self.cartridge.save_state(f)
        self.interaction.save_state(f)
        self.serial.save_state(f)
        f.flush()
        logger.debug("State saved.")

    def load_state(self, f):
        _serial_check_error(self.serial)
        if self._execution_governor_enabled or self._execution_governor_active:
            raise PyBoyInvalidOperationException("Detach execution governor before loading state and establish a new epoch")
        if self._physical_clock_epoch >= PHYSICAL_CLOCK_MAX:
            self._physical_clock_fault = True
            raise RuntimeError("physical clock load epoch exhausted")
        self._sync_physical_clock()
        if self._physical_clock_fault:
            raise RuntimeError("physical clock regressed or overflowed; recreate runtime")
        self._physical_clock_epoch += 1
        self._physical_load_incomplete = True
        logger.debug("Loading state...")
        state_version = f.read()
        if state_version >= 2:
            logger.debug("State version: %d", state_version)
            # From version 2 and above, this is the version number
            self.bootrom_enabled = f.read()
        else:
            logger.debug("State version: 0-1")
            # HACK: The byte wasn't a state version, but the bootrom flag
            self.bootrom_enabled = state_version

        if state_version < STATE_VERSION:
            logger.warning("Loading state from an older version of PyBoy. This might cause compatibility issues.")
        elif state_version > STATE_VERSION:
            raise PyBoyException("Cannot load state from a newer version of PyBoy")

        if state_version >= 8:
            if state_version >= 16:
                self.key0 = f.read()
            self.key1 = f.read()
            self.double_speed = bool(f.read())
            _cgb = f.read()
            if self.cgb and not _cgb:
                raise PyBoyException("Loading state which *is not* CGB-mode, but PyBoy *is* in CGB mode!")
            if not self.cgb and _cgb:
                raise PyBoyException("Loading state which *is* CGB-mode, but PyBoy *is not* in CGB mode!")

            if self.cgb:
                self.hdma.load_state(f, state_version)
        self.cpu.load_state(f, state_version)
        self.lcd.load_state(f, state_version)
        if state_version >= 8:
            self.sound.load_state(f, state_version)
        self.lcd.renderer.load_state(f, state_version)
        self.lcd.renderer.clear_cache()
        self.ram.load_state(f, state_version)

        if state_version <= 15:
            # NON_IO_INTERNAL_RAM1 0x34
            # self.ram.non_io_internal_ram1[i - 0xFF4C]

            for n in range(0x20):
                f.read()  # Discard unused registers
            # FF6C
            self.lcd.object_priority_mode = f.read()
            for n in range(0x03):
                f.read()  # Discard unused registers
            # FF70
            self.wram_select = f.read()
            for n in range(0x0F):
                f.read()  # Discard unused registers
        else:
            # See LCD for object_priority_mode from version 16
            self.wram_select = f.read()  # FF70

        if state_version < 5:
            # Interrupt register moved from RAM to CPU
            self.cpu.interrupts_enabled_register = f.read()
        if state_version >= 5:
            self.timer.load_state(f, state_version)
        self.cartridge.load_state(f, state_version)
        self.interaction.load_state(f, state_version)
        if state_version >= 15:
            # Legacy serial blocks need the motherboard's restored CPU-speed
            # domain before they can retime an untagged in-flight transfer.
            self.serial.cpu_speed_shift = 1 if self.double_speed else 0
            self.serial.load_state(f, state_version)
        f.flush()
        logger.debug("State loaded.")
        self._physical_last_cycles = self.cpu.cycles
        self._physical_load_incomplete = False
        self._serial_raw_offset = int(getattr(self.serial, "clock", 0)) - int(getattr(self.serial, "last_cycles", 0))
        self._serial_time_segments = [
            (int(getattr(self.serial, "clock", 0)), self._physical_clock, 1 if self.double_speed else 2)
        ]

    ###################################################################
    # Coordinator
    #

    def set_execution_governor(self, before, after):
        """Attach a reservation/report pair, or detach with (None, None).

        before(raw_cpu_clock, double_speed, kind, required_cpu_cycles) returns
        a strict int grant >= required_cpu_cycles and <= 9223372036854775807.
        kind is "cpu" (required 24, tick target 4) or "hdma" (required 206).
        after(start_raw, end_raw, start_double_speed, end_double_speed, kind,
              required_cpu_cycles, transition_count, transition_clock,
              transition_double_speed) reports actual execution. Clocks/counts
        are ints and speeds are bools; transition_count is the per-step delta.
        Transition clock/speed are meaningful only when that delta is one.

        A reservation commits no progress. Only the after callback's actual
        timestamps describe execution; errors never imply rollback. Detach
        before loading state and establish a new timeline epoch before reuse.
        """
        if self._execution_governor_active:
            raise PyBoyInvalidOperationException("Cannot replace execution governor during a callback pair")
        if not ((before is None and after is None) or (callable(before) and callable(after))):
            raise PyBoyInvalidOperationException("Execution governor requires two callables or two None values")
        self.execution_before = before
        self.execution_after = after
        self._execution_governor_enabled = before is not None

    def _execution_step(self):
        """Reserve one bounded step and report actual metadata before dispatch.

        Transition clock/speed describe this step only when count delta is one.
        Native noexcept CPU/HDMA failures cannot be caught here; propagated
        execution exceptions still receive a best-effort report without masking
        the original exception if reporting also fails.
        """
        if self._execution_governor_active:
            raise PyBoyInvalidOperationException("Recursive governed execution")
        if not self._execution_governor_enabled:
            raise PyBoyInvalidOperationException("Execution governor is not configured")
        self._execution_governor_active = True
        try:
            before = self.execution_before
            after = self.execution_after
            start_raw = int(self.cpu.cycles)
            start_speed = bool(self.double_speed)
            start_count = int(self.speed_transition_count)
            start_transition_clock = int(self.speed_transition_clock)
            start_transition_speed = bool(self.speed_transition_double_speed)
            is_hdma = bool(
                self.cgb_mode
                and not self.cpu.halted
                and self.hdma.transfer_active
                and self.lcd._STAT._mode & 0b11 == 0
            )
            kind = "hdma" if is_hdma else "cpu"
            required = 206 if is_hdma else 24
            if start_raw < 0 or start_raw > 9223372036854775807 - required:
                raise PyBoyInvalidOperationException("Execution clock cannot safely fit the required step in int64")
            grant = before(start_raw, start_speed, kind, required)
            if type(grant) is not int or grant < required or grant > 9223372036854775807:
                raise PyBoyInvalidOperationException("Execution grant must be an int within the required bound and int64")
            if (
                self.cpu.cycles != start_raw
                or self.double_speed != start_speed
                or self.speed_transition_count != start_count
                or self.speed_transition_clock != start_transition_clock
                or self.speed_transition_double_speed != start_transition_speed
                or self.execution_before is not before
                or self.execution_after is not after
                or not self._execution_governor_enabled
                or is_hdma != bool(
                    self.cgb_mode
                    and not self.cpu.halted
                    and self.hdma.transfer_active
                    and self.lcd._STAT._mode & 0b11 == 0
                )
            ):
                raise PyBoyInvalidOperationException(
                    "Execution reservation changed clock, speed, callbacks or HDMA eligibility"
                )
            try:
                if is_hdma:
                    self.cpu.cycles = self.cpu.cycles + self.hdma.tick(self)
                else:
                    # Excess credit is not permission to batch; HALT also uses 4.
                    self.cpu.tick(4)
            except BaseException:
                try:
                    after(
                        start_raw, int(self.cpu.cycles), start_speed, bool(self.double_speed), kind, required,
                        int(self.speed_transition_count) - start_count,
                        int(self.speed_transition_clock), bool(self.speed_transition_double_speed),
                    )
                except BaseException:  # noqa: S110, BLE001 - Preserve the primary execution exception.
                    pass
                raise
            end_raw = int(self.cpu.cycles)
            end_speed = bool(self.double_speed)
            transition_count = int(self.speed_transition_count) - start_count
            transition_clock = int(self.speed_transition_clock)
            transition_speed = bool(self.speed_transition_double_speed)
            actual_delta = end_raw - start_raw
            after(
                start_raw, end_raw, start_speed, end_speed, kind, required,
                transition_count, transition_clock, transition_speed,
            )
            if (
                self.cpu.cycles != end_raw
                or self.double_speed != end_speed
                or int(self.speed_transition_count) != start_count + transition_count
                or self.speed_transition_clock != transition_clock
                or self.speed_transition_double_speed != transition_speed
                or self.execution_before is not before
                or self.execution_after is not after
                or not self._execution_governor_enabled
            ):
                raise PyBoyInvalidOperationException("Execution report changed clock, speed, transitions or callbacks")
            if actual_delta < 0 or actual_delta > required or transition_count < 0 or transition_count > 1:
                raise PyBoyInvalidOperationException("Execution exceeded reserved bound or speed-transition limit; no rollback")
            if (
                (transition_count == 0 and end_speed != start_speed)
                or (actual_delta == 0 and transition_count != 0)
                or (
                    transition_count == 1
                    and (
                        transition_clock < start_raw
                        or transition_clock > end_raw
                        or transition_speed != end_speed
                        or end_speed == start_speed
                    )
                )
            ):
                raise PyBoyInvalidOperationException("Execution speed-transition metadata is inconsistent; no rollback")
        finally:
            self._execution_governor_active = False

    def tick(self):
        if self._execution_governor_active:
            raise PyBoyInvalidOperationException("Recursive motherboard tick during execution callback pair")
        _serial_check_execution_allowed(self.serial)
        _serial_check_error(self.serial)
        while not self.lcd.frame_done:
            if getattr(self.serial, "owner_poll_enabled", False):
                if not self.serial.owner_boundary(4, self.cpu.cycles, -1, -1):
                    _serial_check_error(self.serial)
            if self._execution_governor_enabled:
                self._execution_step()
            elif (
                self.cgb_mode
                and (not self.cpu.halted)
                and self.hdma.transfer_active
                and self.lcd._STAT._mode & 0b11 == 0
            ):
                self.cpu.cycles = self.cpu.cycles + self.hdma.tick(self)
            else:
                # Fast-forward to next interrupt:
                # As we are halted, we are guaranteed, that our state
                # cannot be altered by other factors than time.
                # For HiToLo interrupt it is indistinguishable whether
                # it gets triggered mid-frame or by next frame
                # Serial is not implemented, so this isn't a concern

                mode0_cycles = MAX_CYCLES
                if self.cgb_mode and self.hdma.transfer_active:
                    mode0_cycles = self.lcd.cycles_to_mode0()

                cycles_target = max(
                    4,
                    min(
                        self.timer._cycles_to_interrupt,
                        # https://gbdev.io/pandocs/STAT.html
                        # STAT (_cycles_to_interrupt) vs. VBLANK interrupt (_cycles_to_interrupt) vs. end frame (_cycles_to_frame)
                        self.lcd._cycles_to_interrupt,  # TODO: Be more agreesive. Only if actual interrupt enabled.
                        self.lcd._cycles_to_frame,
                        self.sound._cycles_to_interrupt,
                        self.serial._cycles_to_interrupt,
                        mode0_cycles,
                    ),
                )
                if self.breakpoint_singlestep:
                    cycles_target = 4
                self.cpu.tick(cycles_target)
                _serial_check_error(self.serial)

            # Preserve the original link-owner callback contract.  This is
            # a safe post-instruction boundary; the b443 owner-pump path is
            # still serviced separately by serial.owner_boundary/tick.
            self.serial.dispatch_owner()

            # TODO: Support General Purpose DMA
            # https://gbdev.io/pandocs/CGB_Registers.html#bit-7--0---general-purpose-dma

            self._sync_physical_clock()
            self.sound.tick(self.cpu.cycles)

            serial_interrupt = self.serial.tick(self.cpu.cycles)
            self._prune_serial_time_segments()
            if serial_interrupt:
                self.cpu.set_interruptflag(INTR_SERIAL)
            _serial_check_error(self.serial)

            if self.timer.tick(self.cpu.cycles):
                self.cpu.set_interruptflag(INTR_TIMER)

            if lcd_interrupt := self.lcd.tick(self.cpu.cycles):
                self.cpu.set_interruptflag(lcd_interrupt)

            if self.breakpoint_singlestep:
                break

        return self.breakpoint_singlestep

    ###################################################################
    # MemoryManager
    #
    def getitem(self, i):
        if 0x0000 <= i < 0x4000:  # 16kB ROM bank #0
            if self.bootrom_enabled and (i <= 0xFF or (self.bootrom.cgb and 0x200 <= i < 0x900)):
                return self.bootrom.bootrom[i]
            else:
                return self.cartridge.rombanks[self.cartridge.rombank_selected_low, i]
        elif 0x4000 <= i < 0x8000:  # 16kB switchable ROM bank
            return self.cartridge.rombanks[self.cartridge.rombank_selected, i - 0x4000]
        elif 0x8000 <= i < 0xA000:  # 8kB Video RAM
            if not self.cgb or self.lcd.vbk.active_bank == 0:
                return self.lcd.VRAM0[i - 0x8000]
            else:
                return self.lcd.VRAM1[i - 0x8000]
        elif 0xA000 <= i < 0xC000:  # 8kB switchable RAM bank
            return self.cartridge.getitem(i)
        elif 0xC000 <= i < 0xE000:  # 8kB Internal RAM
            bank_offset = 0
            if self.cgb and 0xD000 <= i:
                # Find which bank to read from at wram_select
                bank = self.wram_select
                if bank == 0x0:
                    bank = 0x01
                bank_offset = (bank - 1) * 0x1000
            return self.ram.internal_ram0[i - 0xC000 + bank_offset]
        elif 0xE000 <= i < 0xFE00:  # Echo of 8kB Internal RAM
            # Redirect to internal RAM
            return self.getitem(i - 0x2000)
        elif 0xFE00 <= i < 0xFEA0:  # Sprite Attribute Memory (OAM)
            return self.lcd.OAM[i - 0xFE00]
        elif 0xFEA0 <= i < 0xFF00:  # Empty but unusable for I/O
            return self.ram.non_io_internal_ram0[i - 0xFEA0]
        else:
            return self.getitem_io_ports(i)

    def getitem_io_ports(self, i):
        if 0xFF00 <= i < 0xFF4C:  # I/O ports
            if 0xFF01 <= i <= 0xFF02:
                if not self.serial.owner_boundary(1, self.cpu.cycles, i, -1):
                    self.cpu.bail = True
                    return 0xFF
                boundary_seq = self.serial._boundary_seq
                serial_interrupt = self.serial.tick(self.cpu.cycles)
                self._prune_serial_time_segments()
                if serial_interrupt:
                    self.cpu.set_interruptflag(INTR_SERIAL)
                if self.serial.backend_failed:
                    self.serial.owner_boundary_abort(boundary_seq)
                    self.cpu.bail = True
                    return 0xFF
                if i == 0xFF01:
                    value = self.serial.SB
                elif i == 0xFF02:
                    value = self.serial.SC
                if not self.serial.owner_boundary_post(boundary_seq, True):
                    self.cpu.bail = True
                    return 0xFF
                return value
            elif i == 0xFF03:
                # Undocumented register
                return 0xFF
            elif 0xFF04 <= i <= 0xFF07:
                if self.timer.tick(self.cpu.cycles):
                    self.cpu.set_interruptflag(INTR_TIMER)

                if i == 0xFF04:
                    return self.timer.DIV
                elif i == 0xFF05:
                    return self.timer.TIMA
                elif i == 0xFF06:
                    return self.timer.TMA
                elif i == 0xFF07:
                    # TODO: Move logic to Timer class. Read-only bit mask
                    return self.timer.TAC | 0b1111_1000
            elif 0xFF08 <= i <= 0xFF0E:
                # Undocumented register
                return 0xFF
            elif i == 0xFF0F:
                return self.cpu.interrupts_flag_register | 0b1110_0000
            elif 0xFF10 <= i < 0xFF40:
                self.sound.tick(self.cpu.cycles)
                return self.sound.get(i - 0xFF10)
            elif 0xFF40 <= i <= 0xFF4B:
                if lcd_interrupt := self.lcd.tick(self.cpu.cycles):
                    self.cpu.set_interruptflag(lcd_interrupt)

                if i == 0xFF40:
                    return self.lcd._LCDC.value
                elif i == 0xFF41:
                    return self.lcd._STAT.value
                elif i == 0xFF42:
                    return self.lcd.SCY
                elif i == 0xFF43:
                    return self.lcd.SCX
                elif i == 0xFF44:
                    return self.lcd.LY
                elif i == 0xFF45:
                    return self.lcd.LYC
                elif i == 0xFF46:
                    return 0x00  # DMA
                elif i == 0xFF47:
                    return self.lcd.BGP.get()
                elif i == 0xFF48:
                    return self.lcd.OBP0.get()
                elif i == 0xFF49:
                    return self.lcd.OBP1.get()
                elif i == 0xFF4A:
                    return self.lcd.WY
                elif i == 0xFF4B:
                    return self.lcd.WX
            else:
                return self.ram.io_ports[i - 0xFF00]
        elif 0xFF4C <= i < 0xFF80:  # Empty but unusable for I/O
            # CGB registers
            if self.cgb_mode and i == 0xFF4C:
                return self.key0 | 0b11111011
            elif self.cgb_mode and i == 0xFF4D:
                return self.key1
            elif self.cgb and i == 0xFF4F:
                return self.lcd.vbk.get()
            elif self.cgb and i == 0xFF68:
                return self.lcd.bcps.get() | 0x40
            elif self.cgb_mode and i == 0xFF69:
                return self.lcd.bcpd.get()
            elif self.cgb and i == 0xFF6A:
                return self.lcd.ocps.get() | 0x40
            elif self.cgb_mode and i == 0xFF6B:
                return self.lcd.ocpd.get()
            elif self.cgb_mode and i == 0xFF55:
                # HDMA1, HDMA2, HDMA3, HDMA4 are read-only and fallsthrough
                return self.hdma.hdma5 & 0xFF
            elif self.cgb and i == 0xFF56:
                # IR Port
                return 0xFF
            elif self.cgb and i == 0xFF76:
                self.sound.tick(self.cpu.cycles)
                return self.sound.pcm12()
            elif self.cgb and i == 0xFF77:
                self.sound.tick(self.cpu.cycles)
                return self.sound.pcm34()
            elif self.cgb_mode and i == 0xFF6C:
                # Object Priority Mode
                return self.lcd.object_priority_mode | 0b1111_1110
            elif self.cgb_mode and i == 0xFF70:
                # WRAM Bank select
                return self.wram_select | 0b1111_1000
            elif self.cgb and 0xFF72 <= i <= 0xFF73:
                return self.cgb_undocumented[i - 0xFF72]
            elif self.cgb_mode and i == 0xFF74:
                return self.cgb_undocumented[2]
            elif self.cgb and i == 0xFF75:
                return self.cgb_undocumented[3]
            else:
                return 0xFF
        elif 0xFF80 <= i < 0xFFFF:  # Internal RAM
            return self.ram.internal_ram1[i - 0xFF80]
        elif i == 0xFFFF:  # Interrupt Enable Register
            return self.cpu.interrupts_enabled_register

    def setitem(self, i, value):
        if 0x0000 <= i < 0x4000:  # 16kB ROM bank #0
            # Doesn't change the data. This is for MBC commands
            self.cartridge.setitem(i, value)
            self.cpu.bail = True
        elif 0x4000 <= i < 0x8000:  # 16kB switchable ROM bank
            # Doesn't change the data. This is for MBC commands
            self.cartridge.setitem(i, value)
            self.cpu.bail = True
        elif 0x8000 <= i < 0xA000:  # 8kB Video RAM
            if not self.cgb or self.lcd.vbk.active_bank == 0:
                self.lcd.VRAM0[i - 0x8000] = value
                if i < 0x9800:  # Is within tile data -- not tile maps
                    # Mask out the byte of the tile
                    self.lcd.renderer.invalidate_tile(((i & 0xFFF0) - 0x8000) // 16, 0)
            else:
                self.lcd.VRAM1[i - 0x8000] = value
                if i < 0x9800:  # Is within tile data -- not tile maps
                    # Mask out the byte of the tile
                    self.lcd.renderer.invalidate_tile(((i & 0xFFF0) - 0x8000) // 16, 1)
        elif 0xA000 <= i < 0xC000:  # 8kB switchable RAM bank
            self.cartridge.setitem(i, value)
        elif 0xC000 <= i < 0xE000:  # 8kB Internal RAM
            bank_offset = 0
            if self.cgb and 0xD000 <= i:
                # Find which bank to read from at wram_select
                bank = self.wram_select
                if bank == 0x0:
                    bank = 0x01
                bank_offset = (bank - 1) * 0x1000
            self.ram.internal_ram0[i - 0xC000 + bank_offset] = value
        elif 0xE000 <= i < 0xFE00:  # Echo of 8kB Internal RAM
            self.setitem(i - 0x2000, value)  # Redirect to internal RAM
        elif 0xFE00 <= i < 0xFEA0:  # Sprite Attribute Memory (OAM)
            self.lcd.OAM[i - 0xFE00] = value
        elif 0xFEA0 <= i < 0xFF00:  # Empty but unusable for I/O
            self.ram.non_io_internal_ram0[i - 0xFEA0] = value
        else:
            self.setitem_io_ports(i, value)

    def setitem_io_ports(self, i, value):
        if 0xFF00 <= i < 0xFF4C:  # I/O ports
            if i == 0xFF00:
                self.ram.io_ports[i - 0xFF00] = self.interaction.pull(value)
            elif 0xFF01 <= i <= 0xFF02:
                if not self.serial.owner_boundary(2, self.cpu.cycles, i, value):
                    self.cpu.bail = True
                    return
                boundary_seq = self.serial._boundary_seq
                serial_interrupt = self.serial.tick(self.cpu.cycles)
                self._prune_serial_time_segments()
                if serial_interrupt:
                    self.cpu.set_interruptflag(INTR_SERIAL)
                if self.serial.backend_failed:
                    self.serial.owner_boundary_abort(boundary_seq)
                    self.cpu.bail = True
                    return
                if i == 0xFF01:
                    self.serialbuffer[self.serialbuffer_count] = value
                    self.serialbuffer_count += 1
                    self.serialbuffer_count &= 0x3FF
                    self.serial.set_SB(value)
                elif i == 0xFF02:
                    self.serial.set_SC(value)
                if not self.serial.owner_boundary_post(boundary_seq, True):
                    self.cpu.bail = True
                    return
            elif 0xFF04 <= i <= 0xFF07:
                if self.timer.tick(self.cpu.cycles):
                    self.cpu.set_interruptflag(INTR_TIMER)

                if i == 0xFF04:
                    # Pan docs:
                    # “DIV-APU” ... is increased every time DIV’s bit 4 (5 in double-speed mode) goes from 1 to 0 ...
                    # the counter can be made to increase faster by writing to DIV while its relevant bit is set (which
                    # clears DIV, and triggers the falling edge).
                    if self.timer.DIV & (0b1_0000 << self.sound.speed_shift):
                        self.sound.tick(self.cpu.cycles)  # Process outstanding cycles
                        # TODO: Force a falling edge tick
                        self.sound.reset_apu_div()

                    self.timer.reset()
                elif i == 0xFF05:
                    self.timer.TIMA = value
                elif i == 0xFF06:
                    self.timer.TMA = value
                elif i == 0xFF07:
                    self.timer.TAC = value & 0b111  # TODO: Move logic to Timer class
            elif i == 0xFF0F:
                self.cpu.interrupts_flag_register = value & 0b0001_1111
            elif 0xFF10 <= i < 0xFF40:
                self.sound.tick(self.cpu.cycles)
                self.sound.set(i - 0xFF10, value)
            elif 0xFF40 <= i <= 0xFF4B:
                if lcd_interrupt := self.lcd.tick(self.cpu.cycles):
                    self.cpu.set_interruptflag(lcd_interrupt)

                if i == 0xFF40:
                    self.lcd.set_lcdc(value)
                elif i == 0xFF41:
                    self.lcd._STAT.set(value)
                elif i == 0xFF42:
                    self.lcd.SCY = value
                elif i == 0xFF43:
                    self.lcd.SCX = value
                elif i == 0xFF44:
                    # LCDC Read-only
                    return
                elif i == 0xFF45:
                    self.lcd.LYC = value
                    if self.lcd._LCDC.lcd_enable:
                        if lcdc_interrupt := self.lcd._STAT.update_LYC(self.lcd.LYC, self.lcd.LY):
                            self.cpu.set_interruptflag(lcdc_interrupt)
                elif i == 0xFF46:
                    self.transfer_DMA(value)
                elif i == 0xFF47:
                    self.lcd.BGP.set(value)
                elif i == 0xFF48:
                    self.lcd.OBP0.set(value)
                elif i == 0xFF49:
                    self.lcd.OBP1.set(value)
                elif i == 0xFF4A:
                    self.lcd.WY = value
                elif i == 0xFF4B:
                    self.lcd.WX = value
            else:
                self.ram.io_ports[i - 0xFF00] = value
            self.cpu.bail = True
        elif 0xFF4C <= i < 0xFF80:  # Empty but unusable for I/O
            if self.bootrom_enabled and i == 0xFF50 and (value == 0x1 or value == 0x11):
                logger.debug("Bootrom disabled!")
                self.bootrom_enabled = False
                self.cpu.bail = True
            # CGB registers
            elif i == 0xFF4C:
                # Lock FF4C https://gbdev.io/pandocs/CGB_Registers.html#ff4c--key0-cgb-mode-only-cpu-mode-select
                if self.cgb and self.bootrom_enabled:
                    if value == 0xC0:
                        logger.debug("key0: Game identified as CGB-only")
                    elif value == 0x80:
                        logger.debug("key0: Game identified as CGB-compatible")
                    elif value == 0x04:
                        logger.debug("key0: Game identified as DMG-only")
                    else:
                        logger.error("key0: Unknown write: %x", value)
                    self.key0 = value & 0b100

                    if self.key0 and self.lcd.renderer.cgb:
                        logger.debug("Migrating from CGB renderer to DMG renderer")
                        self.lcd.switch_cgb(self.key0)  # TODO: save/load state

            elif self.cgb and i == 0xFF4D:
                self.key1 = value
                self.cpu.bail = True
            elif self.cgb and i == 0xFF4F:
                self.lcd.vbk.set(value)
            elif self.cgb_mode and i == 0xFF51:
                self.hdma.hdma1 = value
            elif self.cgb_mode and i == 0xFF52:
                self.hdma.hdma2 = value  # & 0xF0
            elif self.cgb_mode and i == 0xFF53:
                self.hdma.hdma3 = value  # & 0x1F
            elif self.cgb_mode and i == 0xFF54:
                self.hdma.hdma4 = value  # & 0xF0
            elif self.cgb_mode and i == 0xFF55:
                self.hdma.set_hdma5(value, self)
                self.cpu.bail = True
            elif self.cgb and i == 0xFF56:
                pass  # IR Port
            elif self.cgb and i == 0xFF68:
                self.lcd.bcps.set(value)
            elif self.cgb and i == 0xFF69:
                self.lcd.bcpd.set(value)
            elif self.cgb and i == 0xFF6A:
                self.lcd.ocps.set(value)
            elif self.cgb_mode and i == 0xFF6B:
                self.lcd.ocpd.set(value)
            elif self.cgb_mode and i == 0xFF6C:
                # Object Priority Mode
                self.lcd.object_priority_mode = value & 0b0000_0001
            elif self.cgb_mode and i == 0xFF70:
                # WRAM Bank select
                self.wram_select = value & 0b111
            elif self.cgb and 0xFF72 <= i <= 0xFF73:
                self.cgb_undocumented[i - 0xFF72] = value
            elif self.cgb_mode and i == 0xFF74:
                self.cgb_undocumented[2] = value
            elif self.cgb and i == 0xFF75:
                self.cgb_undocumented[3] = value | 0b1000_1111
            else:
                pass  # All other registers are read-only
        elif 0xFF80 <= i < 0xFFFF:  # Internal RAM
            self.ram.internal_ram1[i - 0xFF80] = value
        elif i == 0xFFFF:  # Interrupt Enable Register
            self.cpu.interrupts_enabled_register = value
            self.cpu.bail = True

    def transfer_DMA(self, src):
        # http://problemkaputt.de/pandocs.htm#lcdoamdmatransfers
        # TODO: Add timing delay of 160µs and disallow access to RAM!
        dst = 0xFE00
        offset = src * 0x100
        for n in range(0xA0):
            self.setitem(dst + n, self.getitem(n + offset))


class HDMA:
    def __init__(self):
        self.hdma1 = 0
        self.hdma2 = 0
        self.hdma3 = 0
        self.hdma4 = 0
        self.hdma5 = 0xFF

        self.transfer_active = False
        self.curr_src = 0
        self.curr_dst = 0

    def save_state(self, f):
        f.write(self.hdma1)
        f.write(self.hdma2)
        f.write(self.hdma3)
        f.write(self.hdma4)
        f.write(self.hdma5)
        f.write(self.transfer_active)
        f.write_16bit(self.curr_src)
        f.write_16bit(self.curr_dst)

    def load_state(self, f, state_version):
        self.hdma1 = f.read()
        self.hdma2 = f.read()
        self.hdma3 = f.read()
        self.hdma4 = f.read()
        self.hdma5 = f.read()
        if STATE_VERSION <= 8:
            # NOTE: Deprecated read to self._hdma5
            f.read()
        self.transfer_active = f.read()
        self.curr_src = f.read_16bit()
        self.curr_dst = f.read_16bit()

    def set_hdma5(self, value, mb):
        if self.transfer_active:
            bit7 = value & 0x80
            if bit7 == 0:
                # terminate active transfer
                self.transfer_active = False
                self.hdma5 = (self.hdma5 & 0x7F) | 0x80
            else:
                self.hdma5 = value & 0x7F
        else:
            self.hdma5 = value & 0xFF
            bytes_to_transfer = ((value & 0x7F) * 16) + 16
            src = (self.hdma1 << 8) | (self.hdma2 & 0xF0)
            dst = ((self.hdma3 & 0x1F) << 8) | (self.hdma4 & 0xF0)
            dst |= 0x8000

            transfer_type = value >> 7
            if transfer_type == 0:
                # General purpose DMA transfer
                for i in range(bytes_to_transfer):
                    mb.setitem((dst + i) & 0xFFFF, mb.getitem((src + i) & 0xFFFF))

                # Number of blocks of 16-bytes transfered. Set 7th bit for "completed".
                self.hdma5 = 0xFF  # (value & 0x7F) | 0x80 #0xFF
                self.hdma4 = 0xFF
                self.hdma3 = 0xFF
                self.hdma2 = 0xFF
                self.hdma1 = 0xFF
                # TODO: Progress cpu cycles!
                # https://gist.github.com/drhelius/3394856
                # cpu is halted during dma transfer
            else:
                # Hblank DMA transfer
                # set 7th bit to 0
                self.hdma5 = self.hdma5 & 0x7F
                self.transfer_active = True
                self.curr_dst = dst
                self.curr_src = src

    def tick(self, mb):
        # HBLANK HDMA routine
        src = self.curr_src & 0xFFF0
        dst = (self.curr_dst & 0x1FF0) | 0x8000

        for i in range(0x10):
            mb.setitem(dst + i, mb.getitem(src + i))

        self.curr_dst += 0x10
        self.curr_src += 0x10

        if self.curr_dst == 0xA000:
            self.curr_dst = 0x8000

        if self.curr_src == 0x8000:
            self.curr_src = 0xA000

        self.hdma1 = (self.curr_src & 0xFF00) >> 8
        self.hdma2 = self.curr_src & 0x00FF

        self.hdma3 = (self.curr_dst & 0xFF00) >> 8
        self.hdma4 = self.curr_dst & 0x00FF

        if self.hdma5 == 0:
            self.transfer_active = False
            self.hdma5 = 0xFF
        else:
            self.hdma5 -= 1

        return 206  # TODO: adjust for double speed
