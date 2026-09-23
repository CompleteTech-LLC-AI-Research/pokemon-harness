"""Read-only session helpers for the bounded timed battle probe (#159).

Split from ``scripts/_timed_battle_probe.py`` with no behavior change. These
helpers only read emulator memory and symbols through an injected session;
they never advance a CPU or write memory, registers, or save state.
"""

from __future__ import annotations


def _integer(value, lo, hi, label):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _byte(value):
    return _integer(value, 0, 255, "memory byte")


def _location(session, name):
    bank, address = session.symbols.bank_addr(name)
    _integer(bank, 0, 255, f"{name} bank")
    _integer(
        address, 0 if bank == 0 else 0x4000, 0x3FFF if bank == 0 else 0x7FFF, f"{name} address"
    )
    return bank, address


def _read(session, name, size=1):
    address = session.symbols.addr_of(name)
    _integer(address, 0x8000, 0x10000 - size, f"{name} RAM address")
    values = [_byte(session._pyboy.memory[address + i]) for i in range(size)]
    return values[0] if size == 1 else values


def _rom(session, bank, address, size):
    return bytes(_byte(session._pyboy.memory[bank, address + i]) for i in range(size))


def _party(session):
    count = _integer(_read(session, "wPartyCount"), 1, 6, "party count")
    base = session.symbols.addr_of("wPartyMons")
    _integer(base, 0x8000, 0x10000 - count * 44, "party records address")
    result = {
        "count": count,
        "species": _read(session, "wPartySpecies", count + 1),
        "records": [
            bytes(_byte(session._pyboy.memory[base + i * 44 + j]) for j in range(44)).hex()
            for i in range(count)
        ],
    }
    _validate_party(result)
    return result


def _validate_party(party):
    count = _integer(party["count"], 1, 6, "party count")
    if len(party["species"]) != count + 1 or party["species"][-1] != 255:
        raise ValueError("invalid party species terminator")
    if len(party["records"]) != count:
        raise ValueError("invalid party record count")
    living = []
    for i, raw in enumerate(party["records"]):
        record = bytes.fromhex(raw)
        species = _integer(party["species"][i], 1, 190, "party species")
        if len(record) != 44 or record[0] != species:
            raise ValueError("invalid party record/species")
        if int.from_bytes(record[1:3], "big"):
            living.append(i)
    if not living:
        raise ValueError("ordinary battle requires at least one living Pokemon")
    return living[0]
