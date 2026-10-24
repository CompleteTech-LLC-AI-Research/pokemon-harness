"""Behavioral admission probes for the selected PyBoy runtime."""

from __future__ import annotations

from typing import Any

ProbeApi = dict[str, Any]


def _verify_serial_features(mode: str, serial_module, *, api: ProbeApi) -> None:
    """Probe standalone serial cores, never an operator emulator or save.

    Version and revision markers alone cannot distinguish older working
    snapshots of the fork. These small ROM-free probes check required behavior.
    """

    def require(condition, detail):
        if not condition:
            raise SystemExit(f"PyBoy {mode} unsupported runtime: {detail}")

    for name, expected in (
        ("CYCLES_PER_EDGE_DMG", 512),
        ("CYCLES_PER_BYTE_DMG", 4096),
        ("CYCLES_8192HZ", 512),
    ):
        require(getattr(serial_module, name, None) == expected, f"serial {name} must be {expected}")
    serial = serial_module.Serial(False)
    require(callable(getattr(serial, "check_error", None)), "serial check_error API is missing")
    require(hasattr(serial, "backend_failed"), "serial backend_failed state is missing")
    require(not serial.backend_failed, "fresh serial core is already failed")
    error_type = getattr(serial_module, "SerialBackendError", None)
    require(
        isinstance(error_type, type) and issubclass(error_type, Exception),
        "SerialBackendError exception is missing",
    )
    calls = []

    class RecordingBackend:
        def on_edge(self, bit, role):
            calls.append((bit, role))
            return 1

    serial.backend = RecordingBackend()
    serial.set_SB(0x80)
    serial.set_SC(0x81)
    require(serial.tick(511) is False and not calls, "serial clock shifts before 512 T-cycles")
    require(
        serial.tick(512) is False and calls == [(1, 1)],
        "serial first bit does not occur at 512 T-cycles",
    )
    serial.check_error()

    failing = serial_module.Serial(False)
    failure = RuntimeError("bootstrap serial backend fault probe")
    failures = []

    class FailingBackend:
        def on_edge(self, bit, role):
            failures.append((bit, role))
            raise failure

    failing.backend = FailingBackend()
    failing.set_SB(0xA5)
    failing.set_SC(0x81)
    before = (failing._bits_remaining, failing._shift_register, failing.SB, failing.SC)
    require(failing.tick(512) is False, "failed serial edge generated an IRQ")
    require(failing.backend_failed, "serial backend exception was not latched")
    require(
        (failing._bits_remaining, failing._shift_register, failing.SB, failing.SC) == before,
        "failed serial edge advanced transfer state",
    )
    for _ in range(2):
        try:
            failing.check_error()
        except error_type as exc:
            require(exc.__cause__ is failure, "serial fault lost its original cause")
        else:
            require(False, "serial check_error did not raise the latched fault")
    require(
        failing.tick(4096) is False and failures == [(1, 1)],
        "failed serial core retried the backend or generated an IRQ",
    )
    require(
        (failing._bits_remaining, failing._shift_register, failing.SB, failing.SC) == before,
        "failed serial core mutated transfer state on a later tick",
    )


def _verify_owner_clock_features(mode: str, pyboy_module, serial_module, *, api: ProbeApi) -> None:
    """Bounded authored CGB instructions; never open BYO assets or saves."""
    PyBoyAssertException = api["importlib"].import_module("pyboy.utils").PyBoyAssertException

    def require(condition, detail):
        if not condition:
            raise SystemExit(f"PyBoy {mode} unsupported runtime: {detail}")

    # Original JP/header/NOP bytes, not a commercial ROM or derived fixture.
    cartridge = bytearray(32768)
    cartridge[0x100:0x103] = b"\xc3\x50\x01"
    cartridge[0x143] = 0x80
    cartridge[0x14D] = (-sum(cartridge[0x134:0x14D]) - 25) & 255
    with api["tempfile"].TemporaryDirectory(prefix="pokered-bootstrap-") as temporary:
        path = api["Path"](temporary) / "authored-cgb-probe.gb"
        path.write_bytes(cartridge)
        emulator = pyboy_module.PyBoy(str(path), window="null", sound_emulated=False)
        try:
            emulator.set_emulation_speed(0)
            emulator.memory[0xFF50] = 1
            emulator.register_file.PC = 0x150

            def clock():
                value = emulator.mb.get_physical_clock()
                require(
                    isinstance(value, tuple)
                    and len(value) == 2
                    and all(type(item) is int and item >= 0 for item in value),
                    "get_physical_clock must return nonnegative (generation, time) integers",
                )
                return value

            def instruction(opcodes):
                emulator.memory[0, 0x150 : 0x150 + len(opcodes)] = opcodes
                emulator.register_file.PC = 0x150
                emulator.mb.breakpoint_singlestep = True
                emulator.mb.lcd.frame_done = False
                emulator.mb.tick()

            saved = api["io"].BytesIO()
            emulator.save_state(saved)
            generation, start = clock()
            for opcodes, elapsed in (
                ([0], 8),
                ([0x10, 0], 12),
                ([0], 16),
                ([0x10, 0], 24),
                ([0], 32),
            ):
                # KEY1 is writable; rewriting it during double speed must not
                # change the physical rate before the next actual STOP.
                if opcodes[0] == 0x10 or elapsed == 16:
                    emulator.memory[0xFF4D] = 1
                instruction(opcodes)
                require(
                    clock() == (generation, start + elapsed),
                    "physical clock does not account normal/double STOP boundaries",
                )
            before = clock()
            saved.seek(0)
            emulator.load_state(saved)
            require(
                clock() == (before[0] + 1, before[1]),
                "state load must change generation without rewinding physical time",
            )
            instruction([0])
            require(
                clock() == (before[0] + 1, before[1] + 8),
                "state load did not restore normal-speed physical accounting",
            )
            before = clock()
            empty_load_failed = False
            try:
                emulator.load_state(api["io"].BytesIO(b""))
            except PyBoyAssertException:
                empty_load_failed = True
            require(empty_load_failed, "empty state load unexpectedly succeeded")
            try:
                clock()
            except RuntimeError as exc:
                require("load is incomplete" in str(exc), "incomplete load lacks its clock fault")
            else:
                require(False, "incomplete load left physical clock available")
            saved.seek(0)
            emulator.load_state(saved)
            require(
                clock() == (before[0] + 2, before[1]),
                "successful reload did not recover the incomplete-load generation",
            )

            serial = emulator.mb.serial
            serial.set_SB(0xA5)
            serial.set_SC(0)
            emulator.register_file.A = 0x2A
            before = clock()
            owner_thread = api["threading"].get_ident()
            observed = []

            def pump(event):
                require(
                    isinstance(event, tuple)
                    and len(event) >= 6
                    and event[1] == 1
                    and event[4] == 0xFF01,
                    "owner pump did not observe the serial MMIO read",
                )
                require(
                    api["threading"].get_ident() == owner_thread
                    and emulator.register_file.PC == 0x150
                    and emulator.register_file.A == 0x2A
                    and serial.SB == 0xA5,
                    "owner pump did not preserve owner-thread precommit state",
                )
                require(
                    clock() == clock() == (before[0], before[1] + 8),
                    "owner pump physical clock is stale or double-counted",
                )
                try:
                    emulator.mb.tick()
                except RuntimeError as exc:
                    require("recursive CPU" in str(exc), "owner pump recursion guard is missing")
                else:
                    require(False, "owner pump allowed recursive CPU execution")
                observed.append(event)

            serial.set_owner_pump(pump)
            instruction([0xF0, 1])
            serial.set_owner_pump(None)
            require(
                len(observed) == 1
                and emulator.register_file.A == 0xA5
                and emulator.register_file.PC == 0x152
                and clock() == (before[0], before[1] + 24),
                "owner pump did not resume the original read exactly once",
            )

            failure = RuntimeError("bootstrap owner pump fault probe")

            def failing_pump(_event):
                raise failure

            serial.set_owner_pump(failing_pump)
            emulator.register_file.A = 0x2A
            try:
                instruction([0xE0, 1])
            except serial_module.SerialBackendError as exc:
                require(exc.__cause__ is failure, "owner pump fault lost its cause")
            else:
                require(False, "owner pump fault did not reach the owner boundary")
            require(
                serial.backend_failed and not serial.owner_pump_active and serial.SB == 0xA5,
                "owner pump fault committed a serial write or lost its latch",
            )
            try:
                serial.check_error()
            except serial_module.SerialBackendError as exc:
                require(exc.__cause__ is failure, "owner pump fault latch lost its cause")
            else:
                require(False, "owner pump fault latch cleared unexpectedly")
        finally:
            emulator.stop(save=False)


def _verify_owner_poll_features(mode: str, pyboy_module, serial_module, *, api: ProbeApi) -> None:
    """Require exclusive bindings and real pre-CPU/HALT owner polling."""

    def require(condition, detail):
        if not condition:
            raise SystemExit(f"PyBoy {mode} unsupported runtime: {detail}")

    def rejected(operation, detail):
        try:
            operation()
        except RuntimeError:
            return
        require(False, detail)

    core = serial_module.Serial(False)
    core.set_SB(0xA5)
    core.set_SC(0x81)
    legacy, claimed = [], []
    core.set_owner_pump(legacy.append)
    rejected(
        lambda: core.claim_owner_pump(claimed.append, poll=True),
        "owner claim replaced an existing callback",
    )
    core.tick(512)
    core.check_error()
    require(len(legacy) == 1 and not claimed, "rejected claim lost the existing callback")
    core.set_owner_pump(None)

    def claimed_callback(event):
        require(core.owner_pump_active, "claimed callback lacks its active boundary")
        rejected(lambda: core.release_owner_pump(token), "active callback released its own claim")
        rejected(lambda: core.set_owner_pump(None), "active callback replaced its binding")
        rejected(
            lambda: core.claim_owner_pump(legacy.append), "active callback acquired another claim"
        )
        claimed.append(event)

    token = core.claim_owner_pump(claimed_callback, poll=True)
    require(
        token is not None and core.owner_poll_enabled,
        "owner claim did not enable requested polling",
    )
    for callback in (None, legacy.append):
        rejected(
            lambda callback=callback: core.set_owner_pump(callback),
            "setter replaced an exclusively claimed callback",
        )
    rejected(lambda: core.claim_owner_pump(legacy.append), "second claim replaced its owner")
    for invalid in (None, object()):
        rejected(
            lambda invalid=invalid: core.release_owner_pump(invalid),
            "wrong token released an owner claim",
        )
    thread_results = []

    def off_thread_release():
        try:
            core.release_owner_pump(token)
        except (RuntimeError, TypeError, ValueError) as exc:
            thread_results.append(exc)
        else:
            thread_results.append(None)

    worker = api["threading"].Thread(target=off_thread_release, daemon=True)
    worker.start()
    worker.join(2)
    require(
        not worker.is_alive()
        and len(thread_results) == 1
        and isinstance(thread_results[0], RuntimeError),
        "claim release did not reject a foreign thread",
    )
    core.tick(1024)
    core.check_error()
    require(
        len(claimed) == 1 and len(legacy) == 1 and core.owner_poll_enabled,
        "rejected binding operations changed the claimed callback",
    )
    core.release_owner_pump(token)
    require(not core.owner_poll_enabled, "claim release left owner polling enabled")
    core.tick(1536)
    core.check_error()
    require(len(claimed) == 1, "claim release left its callback installed")
    replacement = core.claim_owner_pump(legacy.append)
    require(
        replacement is not None and replacement is not token and not core.owner_poll_enabled,
        "fresh default claim reused a token or enabled unsolicited polling",
    )
    rejected(lambda: core.release_owner_pump(token), "stale token released a newer claim")
    core.tick(2048)
    core.check_error()
    require(len(legacy) == 2, "stale-token rejection lost the newer callback")
    core.release_owner_pump(replacement)

    failed = serial_module.Serial(False)
    failed.set_SB(0xA5)
    failed.set_SC(0x81)
    cause = RuntimeError("bootstrap claimed-pump fault probe")

    def fault(_event):
        raise cause

    fault_token = failed.claim_owner_pump(fault, poll=True)
    failed.tick(512)
    failed.release_owner_pump(fault_token)
    require(
        failed.backend_failed and not failed.owner_poll_enabled and not failed.owner_pump_active,
        "fault-safe claim release cleared the error or retained its binding",
    )
    for operation in (failed.check_error, lambda: failed.claim_owner_pump(legacy.append)):
        try:
            operation()
        except serial_module.SerialBackendError as exc:
            require(exc.__cause__ is cause, "claim release lost the original latched fault cause")
        else:
            require(False, "released failed core admitted continuation")

    cartridge = bytearray(32768)
    cartridge[0x100:0x103] = b"\xc3\x50\x01"
    cartridge[0x143] = 0x80
    cartridge[0x14D] = (-sum(cartridge[0x134:0x14D]) - 25) & 255
    with api["tempfile"].TemporaryDirectory(prefix="pokered-owner-poll-") as temporary:
        path = api["Path"](temporary) / "authored-poll-probe.gb"
        path.write_bytes(cartridge)
        emulator = pyboy_module.PyBoy(str(path), window="null", sound_emulated=False)
        try:
            emulator.set_emulation_speed(0)
            emulator.memory[0xFF50] = 1
            emulator.memory[0xFFFF] = emulator.memory[0xFF0F] = 0
            emulator.memory[0, 0x150:0x152] = [0, 0x76]  # NOP, then actual HALT.
            emulator.register_file.PC, emulator.register_file.A = 0x150, 0x2A
            serial = emulator.mb.serial
            polls = []
            owner_thread = api["threading"].get_ident()

            def poll(event):
                require(
                    isinstance(event, tuple)
                    and len(event) == 12
                    and event[1] == 4
                    and event[4:6] == (-1, -1),
                    "owner poll did not publish a kind-4 pre-CPU boundary",
                )
                require(
                    api["threading"].get_ident() == owner_thread
                    and serial.owner_pump_active
                    and emulator.register_file.PC == expected_pc
                    and emulator.register_file.A == 0x2A
                    and emulator.mb.get_physical_clock() == before,
                    "owner poll ran after CPU/HALT progress or off its owner thread",
                )
                polls.append(event)

            def cpu_slice():
                emulator.mb.breakpoint_singlestep = True
                emulator.mb.lcd.frame_done = False
                emulator.mb.tick()

            poll_token = serial.claim_owner_pump(poll, poll=True)
            for index, expected_pc in enumerate((0x150, 0x151, 0x151), 1):
                before = emulator.mb.get_physical_clock()
                cpu_slice()
                require(
                    len(polls) == index
                    and emulator.register_file.PC == 0x151
                    and emulator.mb.get_physical_clock() == (before[0], before[1] + 8),
                    "owner poll did not precede each original NOP/HALT slice exactly once",
                )
            serial.release_owner_pump(poll_token)
            cpu_slice()
            require(
                len(polls) == 3 and not serial.owner_poll_enabled,
                "released poll callback remained installed",
            )
            default_token = serial.claim_owner_pump(polls.append)
            cpu_slice()
            require(
                len(polls) == 3 and not serial.owner_poll_enabled,
                "default owner claim emitted unsolicited CPU polls",
            )
            serial.release_owner_pump(default_token)
        finally:
            emulator.stop(save=False)
