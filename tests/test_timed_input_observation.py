"""Raw, bounded input observations; authored/fake state is not gameplay proof."""

import json
import threading
from collections import Counter

import pytest

from scripts import _timed_menu_probe as helper

REGISTERS = {
    "PC": 65535,
    "SP": 65535,
    "A": 255,
    "F": 255,
    "B": 255,
    "C": 255,
    "D": 255,
    "E": 255,
    "HL": 65535,
}
FIXED = {"IE": 0xFFFF, "IF": 0xFF0F, "JOYP": 0xFF00, "LY": 0xFF44, "STAT": 0xFF41, "LCDC": 0xFF40}
SYMBOLS = (
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


class Guarded:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected access: {name}")

    def __setattr__(self, name, value):
        raise AssertionError(f"unexpected write: {name}")

    def __setitem__(self, address, value):
        raise AssertionError("snapshot wrote memory")


class Symbols(Guarded):
    def __init__(self):
        object.__setattr__(
            self, "addresses", {name: 0xC100 + i * 3 for i, name in enumerate(SYMBOLS)}
        )
        object.__setattr__(self, "reads", [])

    def addr_of(self, name):
        self.reads.append(name)
        return self.addresses[name]


class Memory(Guarded):
    def __init__(self, symbols):
        object.__setattr__(
            self,
            "values",
            {
                address: (i * 19) % 256
                for i, address in enumerate((*FIXED.values(), *symbols.addresses.values()))
            },
        )
        object.__setattr__(self, "reads", [])

    def __getitem__(self, address):
        assert type(address) is int, "only fixed mapped byte reads are allowed"
        self.reads.append(address)
        return self.values[address]


class Registers(Guarded):
    def __init__(self):
        object.__setattr__(
            self, "values", {name: limit - i for i, (name, limit) in enumerate(REGISTERS.items())}
        )
        object.__setattr__(self, "reads", [])

    def __getattr__(self, name):
        if name not in REGISTERS:
            return super().__getattr__(name)
        self.reads.append(name)
        if name not in self.values:
            raise AttributeError(name)
        return self.values[name]


@pytest.fixture
def inputs():
    symbols = Symbols()
    return {
        "symbols": symbols,
        "memory": Memory(symbols),
        "register_file": Registers(),
        "owner_thread_id": threading.get_ident(),
    }


def test_exact_flat_numeric_snapshot_is_bounded_and_read_only(inputs):
    symbols, memory, registers = (inputs[key] for key in ("symbols", "memory", "register_file"))
    before = dict(memory.values)
    expected = dict(registers.values)
    expected.update({name: before[address] for name, address in FIXED.items()})
    expected.update({name: before[address] for name, address in symbols.addresses.items()})
    result = helper.read_input_snapshot(**inputs)
    assert type(result) is dict
    assert result == expected
    assert all(type(value) is int for value in result.values())
    encoded = json.dumps(result, allow_nan=False)
    assert len(encoded.encode()) < 1024
    assert json.loads(encoded) == expected
    assert Counter(memory.reads) == Counter(before.keys())
    assert Counter(registers.reads) == Counter(REGISTERS.keys())
    assert Counter(symbols.reads) == Counter(SYMBOLS)
    assert memory.values == before
    assert registers.values == {name: expected[name] for name in REGISTERS}


@pytest.mark.parametrize("owner", [None, True, False, "owner", 1.0, -1])
def test_owner_rejected_before_any_access(owner):
    with pytest.raises(RuntimeError) as caught:
        helper.read_input_snapshot(
            symbols=Guarded(), memory=Guarded(), register_file=Guarded(), owner_thread_id=owner
        )
    assert caught.value.args == ("input snapshot requires the explicit owner thread",)


def test_actual_foreign_thread_cannot_observe(inputs):
    errors = []

    def observe():
        try:
            helper.read_input_snapshot(**inputs)
        except RuntimeError as exc:
            errors.append(exc)

    thread = threading.Thread(target=observe)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(errors) == 1 and type(errors[0]) is RuntimeError
    assert errors[0].args == ("input snapshot requires the explicit owner thread",)
    assert all(inputs[name].reads == [] for name in ("symbols", "memory", "register_file"))


@pytest.mark.parametrize("name", SYMBOLS)
@pytest.mark.parametrize("missing_none", [False, True])
def test_exact_missing_symbol_fails_before_register_or_memory_reads(inputs, name, missing_none):
    if missing_none:
        inputs["symbols"].addresses[name] = None
    else:
        del inputs["symbols"].addresses[name]
    with pytest.raises(KeyError) as caught:
        helper.read_input_snapshot(**inputs)
    assert caught.value.args == (name,)
    assert inputs["memory"].reads == inputs["register_file"].reads == []


@pytest.mark.parametrize("name", REGISTERS)
def test_exact_missing_public_register_error(inputs, name):
    del inputs["register_file"].values[name]
    with pytest.raises(KeyError) as caught:
        helper.read_input_snapshot(**inputs)
    assert caught.value.args == (name,)


@pytest.mark.parametrize("name", (*FIXED, *SYMBOLS))
def test_exact_missing_memory_field_error(inputs, name):
    address = FIXED[name] if name in FIXED else inputs["symbols"].addresses[name]
    del inputs["memory"].values[address]
    with pytest.raises(KeyError) as caught:
        helper.read_input_snapshot(**inputs)
    assert caught.value.args == (name,)


@pytest.mark.parametrize("address", [True, False, -1, 65536, 1.0, "49152"])
def test_invalid_symbol_address_has_exact_error_before_reads(inputs, address):
    name = SYMBOLS[-1]
    inputs["symbols"].addresses[name] = address
    with pytest.raises(ValueError) as caught:
        helper.read_input_snapshot(**inputs)
    assert caught.value.args == (f"invalid memory address for {name}",)
    assert inputs["memory"].reads == inputs["register_file"].reads == []


class Convertible:
    def __int__(self):
        raise AssertionError("must not coerce arbitrary objects")


@pytest.mark.parametrize("name", (*REGISTERS, *FIXED, *SYMBOLS))
@pytest.mark.parametrize("value", [True, False, 1.0, "1", None, Convertible(), -1, 65536])
def test_invalid_numeric_values_have_exact_field_errors(inputs, name, value):
    register = name in REGISTERS
    limit = REGISTERS[name] if register else 255
    if type(value) is int and value == 65536:
        value = limit + 1
    label = "register" if register else "memory byte"
    if register:
        inputs["register_file"].values[name] = value
    else:
        address = FIXED[name] if name in FIXED else inputs["symbols"].addresses[name]
        inputs["memory"].values[address] = value
    numeric = type(value) is int
    error = ValueError if numeric else TypeError
    message = f"{label} for {name} must be " + (f"between 0 and {limit}" if numeric else "an int")
    with pytest.raises(error) as caught:
        helper.read_input_snapshot(**inputs)
    assert caught.value.args == (message,)


@pytest.mark.parametrize("upper", [False, True])
def test_exact_numeric_limits_and_int_subclasses_are_normalized(inputs, upper):
    class Integer(int):
        pass

    for name, limit in REGISTERS.items():
        inputs["register_file"].values[name] = Integer(limit if upper else 0)
    for address in inputs["memory"].values:
        inputs["memory"].values[address] = Integer(255 if upper else 0)
    result = helper.read_input_snapshot(**inputs)
    assert all(type(value) is int for value in result.values())
    assert result == {
        name: (REGISTERS.get(name, 255) if upper else 0) for name in (*REGISTERS, *FIXED, *SYMBOLS)
    }


def test_snapshots_are_fresh_raw_values_without_interpretation(inputs):
    first = helper.read_input_snapshot(**inputs)
    inputs["register_file"].values["HL"] = 0x1234
    inputs["memory"].values[inputs["symbols"].addresses["hJoyInput"]] = 64
    second = helper.read_input_snapshot(**inputs)
    assert first is not second
    assert first["HL"] != second["HL"] == 0x1234
    assert first["hJoyInput"] != second["hJoyInput"] == 64
    assert set(first) == set(second) == {*REGISTERS, *FIXED, *SYMBOLS}


def test_actual_public_pyboy_register_object_without_ticks(tmp_path, inputs):
    from pyboy import PyBoy

    rom = bytearray(0x8000)
    rom[0x134:0x13D] = b"INPUTTEST"
    rom[0x14D] = (-sum(rom[0x134:0x14D]) - 25) & 255
    rom_path, boot_path = tmp_path / "authored.gb", tmp_path / "authored.boot"
    rom_path.write_bytes(rom)
    boot_path.write_bytes(bytes(256))
    game = PyBoy(str(rom_path), bootrom=str(boot_path), window="null", sound_emulated=False)
    try:
        registers = game.register_file
        expected = {name: getattr(registers, name) for name in REGISTERS}
        frame = game.frame_count
        inputs["register_file"] = registers
        result = helper.read_input_snapshot(**inputs)
        assert {name: result[name] for name in REGISTERS} == expected
        assert {name: getattr(registers, name) for name in REGISTERS} == expected
        assert game.frame_count == frame
    finally:
        game.stop(save=False)
