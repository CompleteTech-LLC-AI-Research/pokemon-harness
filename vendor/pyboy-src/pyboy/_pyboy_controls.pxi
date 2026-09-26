
    def button(self, input, delay=1):
        """
        Send input to PyBoy in the form of "a", "b", "start", "select", "left", "right", "up" and "down".

        The button will automatically be released at the following call to `PyBoy.tick`.

        Example:
        ```python
        >>> pyboy.button('a') # Press button 'a' and release after `pyboy.tick()`
        >>> pyboy.tick() # Button 'a' pressed
        True
        >>> pyboy.tick() # Button 'a' released
        True
        >>> pyboy.button('a', 3) # Press button 'a' and release after 3 `pyboy.tick()`
        >>> pyboy.tick() # Button 'a' pressed
        True
        >>> pyboy.tick() # Button 'a' still pressed
        True
        >>> pyboy.tick() # Button 'a' still pressed
        True
        >>> pyboy.tick() # Button 'a' released
        True
        ```

        Args:
            input (str): button to press
            delay (int, optional): Number of frames to delay the release. Defaults to 1
        """
        input = input.lower()
        if input == "left":
            self.send_input(WindowEvent.PRESS_ARROW_LEFT)
            self.send_input(WindowEvent.RELEASE_ARROW_LEFT, delay)
        elif input == "right":
            self.send_input(WindowEvent.PRESS_ARROW_RIGHT)
            self.send_input(WindowEvent.RELEASE_ARROW_RIGHT, delay)
        elif input == "up":
            self.send_input(WindowEvent.PRESS_ARROW_UP)
            self.send_input(WindowEvent.RELEASE_ARROW_UP, delay)
        elif input == "down":
            self.send_input(WindowEvent.PRESS_ARROW_DOWN)
            self.send_input(WindowEvent.RELEASE_ARROW_DOWN, delay)
        elif input == "a":
            self.send_input(WindowEvent.PRESS_BUTTON_A)
            self.send_input(WindowEvent.RELEASE_BUTTON_A, delay)
        elif input == "b":
            self.send_input(WindowEvent.PRESS_BUTTON_B)
            self.send_input(WindowEvent.RELEASE_BUTTON_B, delay)
        elif input == "start":
            self.send_input(WindowEvent.PRESS_BUTTON_START)
            self.send_input(WindowEvent.RELEASE_BUTTON_START, delay)
        elif input == "select":
            self.send_input(WindowEvent.PRESS_BUTTON_SELECT)
            self.send_input(WindowEvent.RELEASE_BUTTON_SELECT, delay)
        else:
            raise PyBoyInvalidInputException("Unrecognized input:", input)

    def button_press(self, input):
        """
        Send input to PyBoy in the form of "a", "b", "start", "select", "left", "right", "up" and "down".

        The button will remain press until explicitly released with `PyBoy.button_release` or `PyBoy.send_input`.

        Example:
        ```python
        >>> pyboy.button_press('a') # Press button 'a' and keep pressed after `PyBoy.tick()`
        >>> pyboy.tick() # Button 'a' pressed
        True
        >>> pyboy.tick() # Button 'a' still pressed
        True
        >>> pyboy.button_release('a') # Release button 'a' on next call to `PyBoy.tick()`
        >>> pyboy.tick() # Button 'a' released
        True

        ```

        Args:
            input (str): button to press
        """
        input = input.lower()

        if input == "left":
            self.send_input(WindowEvent.PRESS_ARROW_LEFT)
        elif input == "right":
            self.send_input(WindowEvent.PRESS_ARROW_RIGHT)
        elif input == "up":
            self.send_input(WindowEvent.PRESS_ARROW_UP)
        elif input == "down":
            self.send_input(WindowEvent.PRESS_ARROW_DOWN)
        elif input == "a":
            self.send_input(WindowEvent.PRESS_BUTTON_A)
        elif input == "b":
            self.send_input(WindowEvent.PRESS_BUTTON_B)
        elif input == "start":
            self.send_input(WindowEvent.PRESS_BUTTON_START)
        elif input == "select":
            self.send_input(WindowEvent.PRESS_BUTTON_SELECT)
        else:
            raise PyBoyInvalidInputException("Unrecognized input")

    def button_release(self, input):
        """
        Send input to PyBoy in the form of "a", "b", "start", "select", "left", "right", "up" and "down".

        This will release a button after a call to `PyBoy.button_press` or `PyBoy.send_input`.

        Example:
        ```python
        >>> pyboy.button_press('a') # Press button 'a' and keep pressed after `PyBoy.tick()`
        >>> pyboy.tick() # Button 'a' pressed
        True
        >>> pyboy.tick() # Button 'a' still pressed
        True
        >>> pyboy.button_release('a') # Release button 'a' on next call to `PyBoy.tick()`
        >>> pyboy.tick() # Button 'a' released
        True

        ```

        Args:
            input (str): button to release
        """
        input = input.lower()
        if input == "left":
            self.send_input(WindowEvent.RELEASE_ARROW_LEFT)
        elif input == "right":
            self.send_input(WindowEvent.RELEASE_ARROW_RIGHT)
        elif input == "up":
            self.send_input(WindowEvent.RELEASE_ARROW_UP)
        elif input == "down":
            self.send_input(WindowEvent.RELEASE_ARROW_DOWN)
        elif input == "a":
            self.send_input(WindowEvent.RELEASE_BUTTON_A)
        elif input == "b":
            self.send_input(WindowEvent.RELEASE_BUTTON_B)
        elif input == "start":
            self.send_input(WindowEvent.RELEASE_BUTTON_START)
        elif input == "select":
            self.send_input(WindowEvent.RELEASE_BUTTON_SELECT)
        else:
            raise PyBoyInvalidInputException("Unrecognized input")

    def send_input(self, event, delay=0):
        """
        Send a single input to control the emulator. This is both Game Boy buttons and emulator controls. See
        `pyboy.utils.WindowEvent` for which events to send.

        Consider using `PyBoy.button` instead for easier access.

        Example:
        ```python
        >>> from pyboy.utils import WindowEvent
        >>> pyboy.send_input(WindowEvent.PRESS_BUTTON_A) # Press button 'a' and keep pressed after `PyBoy.tick()`
        >>> pyboy.tick() # Button 'a' pressed
        True
        >>> pyboy.tick() # Button 'a' still pressed
        True
        >>> pyboy.send_input(WindowEvent.RELEASE_BUTTON_A) # Release button 'a' on next call to `PyBoy.tick()`
        >>> pyboy.tick() # Button 'a' released
        True
        ```

        And even simpler with delay:
        ```python
        >>> from pyboy.utils import WindowEvent
        >>> pyboy.send_input(WindowEvent.PRESS_BUTTON_A) # Press button 'a' and keep pressed after `PyBoy.tick()`
        >>> pyboy.send_input(WindowEvent.RELEASE_BUTTON_A, 2) # Release button 'a' on third call to `PyBoy.tick()`
        >>> pyboy.tick() # Button 'a' pressed
        True
        >>> pyboy.tick() # Button 'a' still pressed
        True
        >>> pyboy.tick() # Button 'a' released
        True
        ```

        Args:
            event (pyboy.WindowEvent): The event to send
            delay (int): 0 for immediately, number of frames to delay the input
        """

        if delay:
            if not (delay > 0):
                raise PyBoyInvalidInputException("Only positive integers allowed")
            heapq.heappush(self.queued_input, (self.frame_count + delay, event))
        else:
            self.events.append(WindowEvent(event))

    def save_state(self, file_like_object):
        """
        Saves the complete state of the emulator. It can be called at any time, and enable you to revert any progress in
        a game.

        You can either save it to a file, or in-memory. The following two examples will provide the file handle in each
        case. Remember to `seek` the in-memory buffer to the beginning before calling `PyBoy.load_state`:

        ```python
        >>> # Save to file
        >>> with open("state_file.state", "wb") as f:
        ...     pyboy.save_state(f)
        >>>
        >>> # Save to memory
        >>> import io
        >>> with io.BytesIO() as f:
        ...     f.seek(0)
        ...     pyboy.save_state(f)
        0

        ```

        Args:
            file_like_object (io.BufferedIOBase): A file-like object for which to write the emulator state.
        """

        if isinstance(file_like_object, str):
            raise PyBoyInvalidInputException(
                "String not allowed. Did you specify a filepath instead of a file-like object?"
            )

        if file_like_object.__class__.__name__ == "TextIOWrapper":
            raise PyBoyInvalidInputException("Text file not allowed. Did you specify open(..., 'wb')?")

        self.mb.save_state(IntIOWrapper(file_like_object))

    def load_state(self, file_like_object):
        """
        Restores the complete state of the emulator. It can be called at any time, and enable you to revert any progress
        in a game.

        You can either load it from a file, or from memory. See `PyBoy.save_state` for how to save the state, before you
        can load it here.

        To load a file, remember to load it as bytes:
        ```python
        >>> # Load file
        >>> with open("state_file.state", "rb") as f:
        ...     pyboy.load_state(f)
        >>>
        ```

        Args:
            file_like_object (io.BufferedIOBase): A file-like object for which to read the emulator state.
        """

        if isinstance(file_like_object, str):
            raise PyBoyInvalidInputException(
                "String not allowed. Did you specify a filepath instead of a file-like object?"
            )

        if file_like_object.__class__.__name__ == "TextIOWrapper":
            raise PyBoyInvalidInputException("Text file not allowed. Did you specify open(..., 'rb')?")

        self.mb.load_state(IntIOWrapper(file_like_object))
