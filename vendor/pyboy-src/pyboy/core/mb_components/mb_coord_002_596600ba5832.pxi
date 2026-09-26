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


