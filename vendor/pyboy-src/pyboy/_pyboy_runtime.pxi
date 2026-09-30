
    def _tick(self, render, sound):
        # Keep this one-frame seam overridable by the network owner in both
        # runtimes. The Cython wrapper acquires the GIL for Python dispatch,
        # then releases it again for the unchanged native frame body.
        with cython.nogil:
            return self._tick_frame(render, sound)

    def _tick_frame(self, render, sound):
        if self.stopped:
            return False

        self._handle_events(self.events)
        if not self.paused:
            self.gameshark.tick()
            self.mb.lcd.frame_done = False
            self.mb.lcd.disable_renderer = not render
            self.mb.sound.disable_sampling = not sound
            self.mb.sound.clear_buffer()
            # Reenter mb.tick until we eventually get a clean exit without breakpoints
            while self.mb.tick() and (not self.quitting):
                # Breakpoint reached
                # NOTE: Potentially reinject breakpoint that we have now stepped passed
                self.mb.breakpoint_reinject()

                with cython.gil:
                    # NOTE: PC has not been incremented when hitting breakpoint!
                    breakpoint_meta = self.mb.breakpoint_reached()
                    if breakpoint_meta != (-1, -1, -1):
                        bank, addr, _ = breakpoint_meta
                        self.mb.breakpoint_remove(bank, addr)
                        self.mb.breakpoint_singlestep_latch = 0

                        if not self._handle_hooks():
                            self._plugin_manager.handle_breakpoint()
                    else:
                        if self.mb.breakpoint_singlestep_latch:
                            if not self._handle_hooks():
                                self._plugin_manager.handle_breakpoint()
                        # Keep singlestepping on, if that's what we're doing
                        self.mb.breakpoint_singlestep = self.mb.breakpoint_singlestep_latch

            self.frame_count += 1
        self._post_handle_events()

        return not self.quitting

    def tick(self, count=1, render=True, sound=True):
        """
        Progresses the emulator ahead by `count` frame(s).

        To run the emulator in real-time, it will need to process 60 frames a second (for example in a while-loop).
        This function will block for roughly 16,67ms per frame, to not run faster than real-time, unless you specify
        otherwise with the `PyBoy.set_emulation_speed` method.

        If you need finer control than 1 frame, have a look at `PyBoy.hook_register` to inject code at a specific point
        in the game.

        Setting `render` to `True` will make PyBoy render the screen for *the last frame* of this tick. This can be seen
        as a type of "frameskipping" optimization.

        For AI training, it's adviced to use as high a count as practical, as it will otherwise reduce performance
        substantially. While setting `render` to `False`, you can still access the `PyBoy.game_area` to get a simpler
        representation of the game.

        If `render` was enabled, use `pyboy.api.screen.Screen` to get a NumPy buffer or raw memory buffer.

        Example:
        ```python
        >>> pyboy.tick() # Progress 1 frame with rendering
        True
        >>> pyboy.tick(1) # Progress 1 frame with rendering
        True
        >>> pyboy.tick(60, False) # Progress 60 frames *without* rendering
        True
        >>> pyboy.tick(60, True) # Progress 60 frames and render *only the last frame*
        True
        >>> for _ in range(60): # Progress 60 frames and render every frame
        ...     if not pyboy.tick(1, True):
        ...         break
        >>>
        ```

        Args:
            count (int): Number of ticks to process
            render (bool): Whether to render an image for this tick
        Returns
        -------
        (True or False):
            False if emulation has ended otherwise True
        """

        self.mb.serial.check_execution_allowed()
        self.mb.serial.check_error()
        _count = count
        running = False
        t_start = time.perf_counter_ns()
        with cython.nogil:
            while count != 0:
                # Only render screen and sample sound on last tick to improve performance
                _render = render and count == 1
                _sound = sound and count == 1
                running = self._tick(_render, _sound)
                count -= 1
        t_tick = time.perf_counter_ns()
        self._post_tick()
        t_post = time.perf_counter_ns()

        if _count > 0:
            nsecs = t_tick - t_start
            self.avg_tick = 0.9 * (self.avg_tick / _count) + (0.1 * nsecs / 1_000_000_000)
            nsecs = t_post - t_start
            self.avg_emu = 0.9 * (self.avg_emu / _count) + (0.1 * nsecs / 1_000_000_000)
        return running

    def _cycle_palette(self):
        """Cycles to the next DMG palette."""
        palette_name, new_palette = next(self._palette_cycle)
        try:
            self.set_color_palette(new_palette)
        except PyBoyInvalidOperationException as ex:
            logger.warning("Error cycling palette: %s", ex)
            return
        logger.info("Palette: %s", palette_name)

    def _handle_events(self, events):
        if not self.no_input:
            # This feeds events into the tick-loop from the window. There might already be events in the list from the API.
            events = self._plugin_manager.handle_events(events)
        for event in events:
            if event == WindowEvent.QUIT:
                self.quitting = True
            elif event == WindowEvent.RELEASE_SPEED_UP:
                # Switch between unlimited and 1x real-time emulation speed
                self.target_emulationspeed = int(bool(self.target_emulationspeed) ^ True)
                logger.debug("Speed limit: %d", self.target_emulationspeed)
            elif event == WindowEvent.STATE_SAVE:
                if self.gamerom:
                    with open(self.gamerom + ".state", "wb") as f:
                        self.mb.save_state(IntIOWrapper(f))
                else:
                    logger.error("Failed to save game state. PyBoy is loaded without a filepath.")
            elif event == WindowEvent.STATE_LOAD:
                if self.gamerom:
                    state_path = self.gamerom + ".state"
                    if not os.path.isfile(state_path):
                        logger.error("State file not found: %s", state_path)
                        continue
                    with open(state_path, "rb") as f:
                        self.mb.load_state(IntIOWrapper(f))
                else:
                    logger.error("Failed to load game state. PyBoy is loaded without a filepath.")
            elif event == WindowEvent.PASS:
                pass  # Used in place of None in Cython, when key isn't mapped to anything
            elif event == WindowEvent.PAUSE_TOGGLE:
                if self.paused:
                    self._unpause()
                else:
                    self._pause()
            elif event == WindowEvent.PAUSE:
                self._pause()
            elif event == WindowEvent.UNPAUSE:
                self._unpause()
            elif event == WindowEvent._INTERNAL_RENDERER_FLUSH:
                self._plugin_manager._post_tick_windows()
            elif event == WindowEvent.CYCLE_PALETTE:
                self._cycle_palette()
            else:
                self.mb.buttonevent(event)

    def _pause(self):
        if self.paused:
            return
        self.paused = True
        self.save_target_emulationspeed = self.target_emulationspeed
        self.target_emulationspeed = 1
        logger.info("Emulation paused!")
        self._update_window_title()
        self._plugin_manager.paused(True)

    def _unpause(self):
        if not self.paused:
            return
        self.paused = False
        self.target_emulationspeed = self.save_target_emulationspeed
        logger.info("Emulation unpaused!")
        self._update_window_title()
        self._plugin_manager.paused(False)

    def _post_tick(self):
        # Fix buggy PIL. They will copy our image buffer and destroy the
        # reference on some user operations like .save().
        if self.screen.image and not self.screen.image.readonly:
            self.screen._set_image()

        if self.frame_count % 60 == 0:
            self._update_window_title()
        self._plugin_manager.post_tick()
        self._plugin_manager.frame_limiter(self.target_emulationspeed)

    def _post_handle_events(self):
        # Prepare an empty list, as the API might be used to send in events between ticks
        self.events = []
        while self.queued_input and self.frame_count == self.queued_input[0][0]:
            _, _event = heapq.heappop(self.queued_input)
            self.events.append(WindowEvent(_event))

    def _update_window_title(self):
        if self.title_status:
            self.window_title = f"CPU/frame: {(self.avg_tick) / SPF * 100:0.2f}%"
            self.window_title += f' Emulation: x{(round(SPF / self.avg_emu) if self.avg_emu > 0 else "INF")}'
        else:
            self.window_title = "PyBoy"
        if self.paused:
            self.window_title += " [PAUSED]"
        self.window_title += self._plugin_manager.window_title()
        self._plugin_manager._set_title()

    def __del__(self):
        # Construction can fail before ``initialized`` is assigned (and
        # callers may use ``__new__`` for instance-level API probes).  A
        # best-effort destructor must not turn that partial object into an
        # unraisable exception during interpreter or test cleanup.
        if getattr(self, "initialized", False) and not getattr(self, "stopped", True):
            self.stop(save=False)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.stop()

    def _quit(self):
        self.quitting = True

    def stop(self, save=True, ram_file=None, rtc_file=None):
        """
        Gently stops the emulator and all sub-modules.

        Example:
        ```python
        >>> pyboy.stop() # Stop emulator and save game progress (cartridge RAM)
        >>> pyboy.stop(False) # Stop emulator and discard game progress (cartridge RAM)
        >>> import io
        >>> sav = io.BytesIO()
        >>> pyboy.stop(ram_file=sav) # Stop emulator and save game progress (cartridge RAM)
        ```

        Args:
            save (bool): Specify whether to save the game upon stopping. It will always be saved in a file next to the
                provided game-ROM.
            ram_file (file-like object): A bytes buffer to write the RAM (save) data to
            rtc_file (file-like object): A bytes buffer to write the RTC (real-time clock) data to, if present on cartridge
        """
        if self.initialized and not self.stopped:
            logger.info("###########################")
            logger.info("# Emulator is turning off #")
            logger.info("###########################")
            self._plugin_manager.stop()

            # Battery implies saving RAM
            ram_file_handled = False
            rtc_file_handled = False
            if save and self.mb.cartridge.battery and ram_file is None:
                ram_file = open(self.gamerom + ".ram", "w+b")
                ram_file_handled = True

            if save and self.mb.cartridge.rtc_enabled and rtc_file is None:
                rtc_file = open(self.gamerom + ".rtc", "w+b")
                rtc_file_handled = True

            self.mb.stop(save, ram_file, rtc_file)

            if ram_file_handled:
                ram_file.close()

            if rtc_file_handled:
                rtc_file.close()

            self.stopped = True

    ###################################################################
    # Scripts and bot methods
    #
