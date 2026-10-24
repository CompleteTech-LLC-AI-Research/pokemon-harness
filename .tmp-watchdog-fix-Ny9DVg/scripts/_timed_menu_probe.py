"""Stateless menu suggestions and bounded owner-thread observations.

Querying the schedule does not enable integration or execute input. Callers
must explicitly opt in, supply actual completed-frame offsets, and step one
frame at a time themselves. This module never ticks or increments progress;
repeated queries are deterministic. Snapshots are observations, not gameplay
proof, and perform no writes, hooks, or menu-selection heuristics. The separate
opt-in milestone observer installs public executable hooks for observation.
"""

import threading
from contextlib import contextmanager

_MENU_FIELDS = (
    ("wCurMap", 1),
    ("wXCoord", 1),
    ("wYCoord", 1),
    ("wCurrentMenuItem", 1),
    ("wMaxMenuItem", 1),
    ("wCableClubDestinationMap", 1),
    ("wLinkState", 1),
    ("hSerialConnectionStatus", 1),
    ("wLinkMenuSelectionSendBuffer", 2),
    ("wLinkMenuSelectionReceiveBuffer", 2),
)


def menu_input_at(frame_offset: int) -> tuple[str, int] | None:
    """Return a suggested (button, hold_frames) for this completed-frame offset."""
    if type(frame_offset) is not int:
        raise TypeError("frame_offset must be an int, not bool")
    if frame_offset < 0:
        raise ValueError("frame_offset must be nonnegative")
    if frame_offset in (0, 20, 40):
        return ("up", 6)
    if frame_offset >= 60 and (frame_offset - 60) % 8 == 0:
        return ("a", 4)
    return None


def read_menu_snapshot(*, symbols, memory, owner_thread_id: int) -> dict:
    """Read eight scalar bytes and two byte pairs on the explicit owner thread.

    Resolve every exact symbol before reading memory. Missing symbols raise
    KeyError naming the symbol; a wrong owner raises RuntimeError before any
    symbol or memory access. Results contain only ints and lists of ints.
    """
    if type(owner_thread_id) is not int or threading.get_ident() != owner_thread_id:
        raise RuntimeError("menu snapshot requires the explicit owner thread")

    resolved = []
    for name, size in _MENU_FIELDS:
        try:
            address = symbols.addr_of(name)
        except KeyError as exc:
            raise KeyError(name) from exc
        if address is None:
            raise KeyError(name)
        if type(address) is not int or not 0 <= address <= 0x10000 - size:
            raise ValueError(f"invalid memory address for {name}")
        resolved.append((name, address, size))

    snapshot = {}
    for name, address, size in resolved:
        values = []
        for offset in range(size):
            value = memory[address + offset]
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"memory byte for {name} must be an int")
            if not 0 <= value <= 255:
                raise ValueError(f"memory byte for {name} must be between 0 and 255")
            values.append(int(value))
        snapshot[name] = values[0] if size == 1 else values
    return snapshot


_MILESTONE_HOOKS = (
    ("save_request", "PrintText"),
    ("yes_no", "YesNoChoice"),
    ("save_game", "SaveGameData"),
    ("link_menu", "LinkMenu"),
)
_SAVE_REQUEST = "CableClubNPCPleaseApplyHereHaveToSaveText"
_MAX_HITS = (1 << 63) - 1


class _MilestoneObserver:
    def __init__(self, *, owner_thread_id, enabled, max_events):
        self.owner_thread_id = owner_thread_id
        self.enabled = enabled
        self.max_events = max_events
        self.counts = {name: 0 for name, _ in _MILESTONE_HOOKS}
        self.events = []
        self.error = None

    def _owner(self):
        if type(self.owner_thread_id) is not int or threading.get_ident() != self.owner_thread_id:
            raise RuntimeError("milestone observation requires the explicit owner thread")

    def _fail(self, message):
        if self.error is None:
            self.error = message[:256]

    def snapshot(self):
        """Return detached lifetime evidence, including any deferred failure."""
        self._owner()
        return {
            "enabled": self.enabled,
            "counts": dict(self.counts),
            "events": [dict(event) for event in self.events],
            "error": self.error,
        }

    def check(self):
        """Fail closed at an owner-safe boundary after any callback failure."""
        self._owner()
        if self.error is not None:
            raise RuntimeError(self.error)


def _milestone_number(value, name, maximum):
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int")
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")
    return int(value)


def _milestone_location(symbols, name):
    try:
        location = symbols.bank_addr(name)
    except KeyError as exc:
        raise KeyError(name) from exc
    if location is None:
        raise KeyError(name)
    if not isinstance(location, tuple) or len(location) != 2:
        raise ValueError(f"invalid ROM location for {name}")
    bank, address = location
    if (
        type(bank) is not int
        or type(address) is not int
        or not 0 <= bank <= 255
        or not (0 <= address < 0x4000 if bank == 0 else 0x4000 <= address < 0x8000)
    ):
        raise ValueError(f"invalid ROM location for {name}")
    return bank, address


@contextmanager
def observe_rom_milestones(
    *,
    symbols,
    memory,
    register_file,
    frame_count,
    hook_register,
    hook_deregister,
    owner_thread_id: int,
    enabled=False,
    max_events=32,
):
    """Observe executable entries with bounded lifetime evidence, opt-in only.

    ``frame_count`` reads the public completed frame counter, without adding one.
    PrintText recognizes the save-request wrapper by HL and its bank shadow;
    text data is never hooked. Call ``check`` before and after stepping outside
    this helper. Callbacks defer ordinary errors; snapshots retain that evidence.
    Counts continue after the lifetime event cap is exceeded,
    saturating at 2**63-1 with an explicit failure rather than wrapping.

    Public registration must fail atomically: a failed slot is not ours to
    deregister. All successful registrations are removed in reverse order, even
    after partial installation or body failure. Multiple failures are grouped.
    Exit this context on the owner thread before stopping the emulator.
    """
    observer = _MilestoneObserver(
        owner_thread_id=owner_thread_id, enabled=enabled, max_events=max_events
    )
    observer._owner()
    if type(enabled) is not bool:
        raise TypeError("enabled must be a bool")
    if type(max_events) is not int:
        raise TypeError("max_events must be an int")
    if not 1 <= max_events <= 256:
        raise ValueError("max_events must be between 1 and 256")
    installed = []
    failures = []
    active = True
    try:
        if enabled:
            locations = [
                (milestone, _milestone_location(symbols, name))
                for milestone, name in _MILESTONE_HOOKS
            ]
            text_location = _milestone_location(symbols, _SAVE_REQUEST)
            sites = [location for _, location in locations]
            if len(set(sites)) != len(sites) or text_location in sites:
                raise ValueError("milestone ROM locations must be distinct")
            try:
                bank_address = symbols.addr_of("hLoadedROMBank")
            except KeyError as exc:
                raise KeyError("hLoadedROMBank") from exc
            if bank_address is None:
                raise KeyError("hLoadedROMBank")
            if type(bank_address) is not int or not 0 <= bank_address <= 0xFFFF:
                raise ValueError("invalid memory address for hLoadedROMBank")

            def callback(context):
                if not active:
                    return
                try:
                    observer._owner()
                    milestone, (bank, address) = context
                    # These two reads remain necessary to count matching text
                    # entries after overflow; other context collection stops.
                    if milestone == "save_request":
                        hl = _milestone_number(register_file.HL, "HL", 65535)
                        loaded_bank = _milestone_number(memory[bank_address], "hLoadedROMBank", 255)
                        if hl != text_location[1] or (
                            text_location[0] != 0 and loaded_bank != text_location[0]
                        ):
                            return
                    if observer.counts[milestone] == _MAX_HITS:
                        observer._fail("milestone hit count overflow")
                        return
                    observer.counts[milestone] += 1
                    if observer.error is not None:
                        return
                    if len(observer.events) >= max_events:
                        observer._fail("milestone event evidence overflow")
                        return
                    if milestone != "save_request":
                        hl = _milestone_number(register_file.HL, "HL", 65535)
                        loaded_bank = _milestone_number(memory[bank_address], "hLoadedROMBank", 255)
                    observer.events.append(
                        {
                            "milestone": milestone,
                            "bank": bank,
                            "address": address,
                            "completed_so_far": _milestone_number(
                                frame_count(), "completed_so_far", _MAX_HITS
                            ),
                            "PC": _milestone_number(register_file.PC, "PC", 65535),
                            "HL": hl,
                            "hLoadedROMBank": loaded_bank,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - defer native callback errors to check().
                    observer._fail(f"milestone callback failed: {type(exc).__name__}: {exc}")

            for context in locations:
                bank, address = context[1]
                hook_register(bank, address, callback, context)
                installed.append((bank, address))
        yield observer
    except BaseException as exc:  # noqa: BLE001 - preserve body failure while removing all hooks.
        failures.append(exc)
    finally:
        # Failed deregistration must not leave a callback collecting evidence
        # after the observation scope has ended.
        active = False
        # Attempt every owned deregistration, including when one fails.
        for bank, address in reversed(installed):
            try:
                observer._owner()
                hook_deregister(bank, address)
            except BaseException as exc:  # noqa: BLE001 - attempt remaining cleanup, then reraise.
                failures.append(exc)
        try:
            observer.check()
        except BaseException as exc:  # noqa: BLE001 - aggregate deferred failure without hiding body.
            # A boundary check may already be the body's exception. Preserve
            # it once, but do not hide a separate deferred callback failure.
            if not any(type(error) is type(exc) and error.args == exc.args for error in failures):
                failures.append(exc)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("milestone observation failures", failures)


_INPUT_REGISTERS = (
    ("PC", 65535),
    ("SP", 65535),
    ("A", 255),
    ("F", 255),
    ("B", 255),
    ("C", 255),
    ("D", 255),
    ("E", 255),
    ("HL", 65535),
)
_INPUT_IO = (
    ("IE", 0xFFFF),
    ("IF", 0xFF0F),
    ("JOYP", 0xFF00),
    ("LY", 0xFF44),
    ("STAT", 0xFF41),
    ("LCDC", 0xFF40),
)
_INPUT_SYMBOLS = (
    "hLoadedROMBank",
    "hJoyInput",
    "hJoyPressed",
    "hJoyHeld",
    "hJoyLast",
    "wJoyIgnore",
    "wStatusFlags5",
    "wWalkCounter",
    "hVBlankOccurred",
    "hSerialReceivedNewData",
    "hSerialSendData",
    "hSerialReceiveData",
)


def read_input_snapshot(*, symbols, memory, register_file, owner_thread_id: int) -> dict:
    """Read 27 numeric fields on the owner thread through public register APIs.

    PC, SP and HL are unsigned 16-bit registers; other fields are bytes.
    Resolve all exact symbols before register or memory reads. Missing fields
    raise KeyError(field_name); invalid types and widths raise TypeError and
    ValueError respectively. No fallback lookup, writes, ticks or hooks occur.
    """
    if type(owner_thread_id) is not int or threading.get_ident() != owner_thread_id:
        raise RuntimeError("input snapshot requires the explicit owner thread")

    resolved = []
    for name in _INPUT_SYMBOLS:
        try:
            address = symbols.addr_of(name)
        except KeyError as exc:
            raise KeyError(name) from exc
        if address is None:
            raise KeyError(name)
        if type(address) is not int or not 0 <= address <= 0xFFFF:
            raise ValueError(f"invalid memory address for {name}")
        resolved.append((name, address))

    def numeric(value, name, kind, maximum):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{kind} for {name} must be an int")
        if not 0 <= value <= maximum:
            raise ValueError(f"{kind} for {name} must be between 0 and {maximum}")
        return int(value)

    snapshot = {}
    for name, maximum in _INPUT_REGISTERS:
        try:
            value = getattr(register_file, name)
        except (AttributeError, KeyError) as exc:
            raise KeyError(name) from exc
        snapshot[name] = numeric(value, name, "register", maximum)
    for name, address in (*_INPUT_IO, *resolved):
        try:
            value = memory[address]
        except (KeyError, IndexError) as exc:
            raise KeyError(name) from exc
        snapshot[name] = numeric(value, name, "memory byte", 255)
    return snapshot
