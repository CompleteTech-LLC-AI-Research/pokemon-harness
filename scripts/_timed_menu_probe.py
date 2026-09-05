"""Stateless menu suggestions and bounded owner-thread observations.

Querying the schedule does not enable integration or execute input. Callers
must explicitly opt in, supply actual completed-frame offsets, and step one
frame at a time themselves. This module never ticks or increments progress;
repeated queries are deterministic. Snapshots are observations, not gameplay
proof, and perform no writes, hooks, or menu-selection heuristics.
"""

import threading

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
