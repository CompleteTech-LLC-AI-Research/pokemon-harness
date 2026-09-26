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

