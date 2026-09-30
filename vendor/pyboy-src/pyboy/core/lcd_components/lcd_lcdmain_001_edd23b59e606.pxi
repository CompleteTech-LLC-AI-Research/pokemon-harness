class LCD:
    def __init__(self, cgb, cgb_mode, color_palette, cgb_color_palette, randomize=False):
        self.VRAM0 = array("B", [0] * VIDEO_RAM)
        self.OAM = array("B", [0] * OBJECT_ATTRIBUTE_MEMORY)
        self.disable_renderer = False

        if randomize:
            for i in range(VIDEO_RAM):
                self.VRAM0[i] = getrandbits(8)
            for i in range(OBJECT_ATTRIBUTE_MEMORY):
                self.OAM[i] = getrandbits(8)

        self._LCDC = LCDCRegister(0)
        self._STAT = STATRegister()  # Bit 7 is always set.

        self.speed_shift = 0
        self.clock = 0
        self.clock_target = FRAME_CYCLES
        self.frame_done = False
        self.first_frame = False
        self.reset = False
        self._cycles_to_interrupt = 0
        self._cycles_to_frame = (FRAME_CYCLES - self.clock) << self.speed_shift
        self.next_stat_mode = 2
        self.LY = 0x00
        self._STAT.set_mode(0)

        self.SCY = 0x00
        self.SCX = 0x00
        self.LYC = 0x00
        # self.DMA = 0x00
        self.WY = 0x00
        self.WX = 0x00
        self.object_priority_mode = 0xFF  # TODO: Not implemented

        self.cgb = cgb
        self._downgraded_to_dmg = False
        self._scanlineparameters = [[0, 0, 0, 0, 0] for _ in range(ROWS)]
        self.last_cycles = 0
        self._cycles_to_interrupt = 0
        self._cycles_to_frame = (FRAME_CYCLES - self.clock) << self.speed_shift

        if self.cgb:
            logger.debug("Starting CGB renderer")
            # Setting for both modes, even though CGB is ignoring them. BGP[0] used in scanline_blank.
            bg_pal, obj0_pal, obj1_pal = cgb_color_palette
            self.BGP = PaletteRegister(0xFC, bg_pal)
            self.OBP0 = PaletteRegister(0xFF, obj0_pal)
            self.OBP1 = PaletteRegister(0xFF, obj1_pal)
        else:
            logger.debug("Starting DMG renderer")
            self.BGP = PaletteRegister(0xFC, color_palette)
            self.OBP0 = PaletteRegister(0xFF, color_palette)
            self.OBP1 = PaletteRegister(0xFF, color_palette)
        self.renderer = Renderer(self, self.cgb)

        # CGB specific
        if self.cgb:
            self.VRAM1 = array("B", [0] * VIDEO_RAM)

            self.vbk = VBKregister()
            self.bcps = PaletteIndexRegister()  # Only stored here for access in mb
            self.bcpd = PaletteColorRegister(self.bcps)
            self.ocps = PaletteIndexRegister()  # Only stored here for access in mb
            self.ocpd = PaletteColorRegister(self.ocps)

    def switch_cgb(self, is_dmg_rom):
        # Bootrom requests CGB hardware to switch to DMG rendering. This is only
        # done once, and only downgraded from CGB to DMG at the end of CGB bootrom.
        if self.cgb and is_dmg_rom:
            logger.debug("Migrating from CGB renderer to DMG renderer")
            self.cgb = False
            self.renderer.cgb = False
            self._downgraded_to_dmg = True
        else:
            logger.debug("Ignoring key0 change")

    def set_lcdc(self, value):
        _lcd_enable = self._LCDC.lcd_enable
        self._LCDC.set(value)

        if _lcd_enable and (not self._LCDC.lcd_enable):
            # https://www.reddit.com/r/Gameboy/comments/a1c8h0/what_happens_when_a_gameboy_screen_is_disabled/
            # 1. LY (current rendering line) resets to zero. A few games rely on this behavior, namely Mr. Do! When LY
            # is reset to zero, no LYC check is done, so no STAT interrupt happens either.
            # 2. The LCD clock is reset to zero as far as I can tell.
            # 3. I believe the LCD enters Mode 0.
            self.clock = 0
            self.clock_target = 0
            self._cycles_to_frame = 0
            self._STAT.set_mode(0)
            if self.LY < 144:
                logger.debug("LCD disabled outside of VBLANK!")
            self.LY = 0
        elif (not _lcd_enable) and self._LCDC.lcd_enable:  # When switching from disabled to enabled
            # Registers are actually supposed to be frozen when LCD is disabled. This is mimicked by reseting them again
            self.clock_target = 0  # This will trigger an immediate update in tick()

            # The Cycle Accurate Game Boy Docs:
            # the LCD won't show any image during the first frame it is turned on. The first drawn frame is the second one.
            self.first_frame = True  # used to postpose rendering of first frame

            # Will schedule a full reset on next call to LCD.tick. This will happen immediately, as CPU.tick returns
            # because of CPU.bail and MB.tick proceeds to call LCD.tick.
            self.reset = True

    def cycles_to_mode0(self):
        mode2 = 80
        mode3 = 170
        mode1 = 456

        mode = self._STAT._mode
        # Remaining cycles for this already active mode
        remainder = self.clock_target - self.clock

        mode &= 0b11
        if mode == 2:
            return remainder + mode3
        elif mode == 3:
            return remainder
        elif mode == 0:
            return 0
        elif mode == 1:
            remaining_ly = 153 - self.LY
            return remainder + mode1 * remaining_ly + mode2 + mode3
        # else:
        #     logger.critical("Unsupported STAT mode: %d", mode)
        #     return 0

    def tick(self, _cycles):
        cycles = _cycles - self.last_cycles
        if cycles == 0:
            return False
        self.last_cycles = _cycles

        interrupt_flag = 0
        self.clock += cycles >> self.speed_shift

        if self.clock >= self.clock_target:
            if self._LCDC.lcd_enable and (self.LY == 153 or self.reset):
                if self.reset:
                    # RESET
                    self.clock = 0
                    self.clock_target = 0
                    self._STAT.set_mode(0)  # Side-effects?
                    self.reset = False

                self.frame_done = True

                # Reset to new frame and start from mode 2
                self.LY = 0
                self.clock %= FRAME_CYCLES
                self.clock_target = 0
                self.next_stat_mode = 2

                # Change to next mode
                interrupt_flag |= self._STAT.set_mode(self.next_stat_mode)

                # self._STAT._mode == 2:  # Searching OAM
                self.clock_target += 80
                self.next_stat_mode = 3
                interrupt_flag |= self._STAT.update_LYC(self.LYC, self.LY)
                # FIX: also evaluate wy_activated_frame when mode 2 is
                # entered via the LY=153 frame-reset inline path above.
                # The elif branch below checks this for "normal" mode 2
                # entries but missed the frame-start entry, so WY==0
                # never got a chance to match LY==0 and the window
                # layer never activated for that frame. Manifested as
                # Gen 1 dialog boxes being written to VRAM but never
                # composited on screen in CGB mode.
                self.renderer.wy_activated_frame = self.renderer.wy_activated_frame | (self.WY == self.LY)

            elif self._LCDC.lcd_enable:
                # Change to next mode
                interrupt_flag |= self._STAT.set_mode(self.next_stat_mode)

                # Pan Docs:
                # The following are typical when the display is enabled:
                #   Mode 2  2_____2_____2_____2_____2_____2___________________2____
                #   Mode 3  _33____33____33____33____33____33__________________3___
                #   Mode 0  ___000___000___000___000___000___000________________000
                #   Mode 1  ____________________________________11111111111111_____

                # LCD state machine
                if self._STAT._mode == 2:  # Searching OAM
                    self.LY += 1
                    self.clock_target += 80
                    self.next_stat_mode = 3
                    interrupt_flag |= self._STAT.update_LYC(self.LYC, self.LY)
                    # If this condition is not met in mode 2, at some point in this
                    # frame, the window will not show.
                    # FIXME: Strange Cython work-around. I thought I had fixed this.
                    self.renderer.wy_activated_frame = self.renderer.wy_activated_frame | (self.WY == self.LY)
                elif self._STAT._mode == 3:
                    self.clock_target += 170
                    self.next_stat_mode = 0
                elif self._STAT._mode == 0:  # HBLANK
                    self.clock_target += 206

                    # Recorded for API
                    bx, by = self.getviewport()
                    wx, wy = self.getwindowpos()
                    self._scanlineparameters[self.LY][0] = bx
                    self._scanlineparameters[self.LY][1] = by
                    self._scanlineparameters[self.LY][2] = wx + 7
                    self._scanlineparameters[self.LY][3] = wy
                    self._scanlineparameters[self.LY][4] = self._LCDC.tiledata_select

                    if self.cgb:
                        self.renderer.cgb_scanline(self.LY)
                    else:
                        self.renderer.scanline(self.LY)
                    self.renderer.scanline_sprites(
                        self.LY, self.renderer._screenbuffer, self.renderer._screenbuffer_attributes, False
                    )
                    if self.LY < 143:
                        self.next_stat_mode = 2
                    else:
                        self.next_stat_mode = 1
                elif self._STAT._mode == 1:  # VBLANK
                    self.clock_target += 456
                    self.next_stat_mode = 1

                    self.LY += 1
                    interrupt_flag |= self._STAT.update_LYC(self.LYC, self.LY)

                    if self.LY == 144:
                        interrupt_flag |= INTR_VBLANK
                        if self.first_frame:
                            self.renderer.wy_activated_frame = False
                            # Pan Docs: https://gbdev.io/pandocs/LCDC.html#lcdc7--lcd-enable
                            # When re-enabling the LCD, the PPU will immediately start drawing again, but the screen
                            # will stay blank during the first frame.
                            self.renderer.blank_screen()
                            self.first_frame = False
            else:
                # See also `self.set_lcdc`
                self.frame_done = True
                self.clock %= FRAME_CYCLES
                self.clock_target = FRAME_CYCLES

                # Renderer
                self.renderer.blank_screen()

        # NOTE: speed_shift because they are using in externally in mb
        self._cycles_to_interrupt = (self.clock_target - self.clock) << self.speed_shift
        # TODO: STAT Cycles to interrupts
        self._cycles_to_frame = (FRAME_CYCLES - self.clock) << self.speed_shift
        return interrupt_flag

    def save_state(self, f):
        for n in range(VIDEO_RAM):
            f.write(self.VRAM0[n])

        for n in range(OBJECT_ATTRIBUTE_MEMORY):
            f.write(self.OAM[n])

        f.write(self._LCDC.value)  # TODO: Mode to class
        f.write(self.BGP.value)
        f.write(self.OBP0.value)
        f.write(self.OBP1.value)

        self._STAT.save_state(f)
        f.write(self.LY)
        f.write(self.LYC)

        f.write(self.SCY)
        f.write(self.SCX)
        f.write(self.WY)
        f.write(self.WX)
        f.write(self.object_priority_mode)

        for y in range(ROWS):
            f.write(self._scanlineparameters[y][0])
            f.write(self._scanlineparameters[y][1])
            # We store (WX + 7). We added 7 earlier to make it easier to serialize
            f.write(self._scanlineparameters[y][2])
            f.write(self._scanlineparameters[y][3])
            f.write(self._scanlineparameters[y][4])

        f.write(self.cgb)
        f.write(self._downgraded_to_dmg)
        f.write(self.speed_shift)
        f.write(self.frame_done)
        f.write(self.first_frame)
        f.write(self.reset)
        f.write_64bit(self.last_cycles)
        f.write_64bit(self.clock)
        f.write_64bit(self.clock_target)
        f.write(self.next_stat_mode)

        if self.cgb:
            for n in range(VIDEO_RAM):
                f.write(self.VRAM1[n])
            f.write(self.vbk.active_bank)
            self.bcps.save_state(f)
            self.bcpd.save_state(f)
            self.ocps.save_state(f)
            self.ocpd.save_state(f)

    def load_state(self, f, state_version):
        for n in range(VIDEO_RAM):
            self.VRAM0[n] = f.read()

        for n in range(OBJECT_ATTRIBUTE_MEMORY):
            self.OAM[n] = f.read()

        self.set_lcdc(f.read())  # TODO: Mode to class
        self.BGP.set(f.read())
        self.OBP0.set(f.read())
        self.OBP1.set(f.read())

        if state_version >= 5:
            self._STAT.load_state(f, state_version)
            self.LY = f.read()
            self.LYC = f.read()

        self.SCY = f.read()
        self.SCX = f.read()
        self.WY = f.read()
        self.WX = f.read()
        if state_version >= 16:
            # See motherboard before version 16
            self.object_priority_mode = f.read()

        if state_version >= 11:
            for y in range(ROWS):
                self._scanlineparameters[y][0] = f.read()
                self._scanlineparameters[y][1] = f.read()
                # Restore (WX - 7) as described above
                self._scanlineparameters[y][2] = f.read()
                self._scanlineparameters[y][3] = f.read()
                if state_version > 3:
                    self._scanlineparameters[y][4] = f.read()

        if state_version >= 8:
            _cgb = f.read()
            if state_version >= 17:
                self._downgraded_to_dmg = f.read()
                if self._downgraded_to_dmg:
                    logger.debug("Load-state from downgraded CGB renderer to DMG renderer")
                    self.renderer.cgb = False

            if self._downgraded_to_dmg and _cgb:
                raise PyBoyException("LCD was supposed to be downgraded!")
            elif not self._downgraded_to_dmg:
                if self.cgb and not _cgb:
                    raise PyBoyException("Loading state which *is not* CGB-mode, but PyBoy *is* in CGB mode!")
                if not self.cgb and _cgb:
                    raise PyBoyException("Loading state which *is* CGB-mode, but PyBoy *is not* in CGB mode!")

            self.cgb = _cgb
            self.speed_shift = f.read()
            if state_version >= 13:
                self.frame_done = f.read()
                self.first_frame = f.read()
                self.reset = f.read()

            if state_version >= 12:
                self.last_cycles = f.read_64bit()
            self.clock = f.read_64bit()
            self.clock_target = f.read_64bit()
            # NOTE: speed_shift because they are using in externally in mb
            self._cycles_to_interrupt = (self.clock_target - self.clock) << self.speed_shift
            self._cycles_to_frame = (FRAME_CYCLES - self.clock) << self.speed_shift
            self.next_stat_mode = f.read()

            if self.cgb:
                for n in range(VIDEO_RAM):
                    self.VRAM1[n] = f.read()
                self.vbk.active_bank = f.read()
                self.bcps.load_state(f, state_version)
                self.bcpd.load_state(f, state_version)
                self.ocps.load_state(f, state_version)
                self.ocpd.load_state(f, state_version)

    def getwindowpos(self):
        return (self.WX - 7, self.WY)

    def getviewport(self):
        return (self.SCX, self.SCY)


