"""ROM-free contracts for scheduled input and observation, not gameplay proof."""

import importlib
import json
import threading
from collections import Counter

import pytest

SCALARS = (
    "wCurMap",
    "wXCoord",
    "wYCoord",
    "wCurrentMenuItem",
    "wMaxMenuItem",
    "wCableClubDestinationMap",
    "wLinkState",
    "hSerialConnectionStatus",
)
BUFFERS = ("wLinkMenuSelectionSendBuffer", "wLinkMenuSelectionReceiveBuffer")
FIELDS = SCALARS + BUFFERS


@pytest.fixture
def probe():
    return importlib.import_module("scripts._timed_menu_probe")


class Symbols:
    def __init__(self, missing=None):
        self.addresses = {name: 0xC100 + index * 7 for index, name in enumerate(FIELDS)}
        if missing is not None:
            del self.addresses[missing]
        self.lookups = []

    def addr_of(self, name):
        self.lookups.append(name)
        return self.addresses[name]


class ByteValue:
    """Arbitrary int conversion is not the memory byte contract."""

    def __int__(self):
        return 1


class ReadOnlyMemory:
    def __init__(self, symbols):
        self.symbols = symbols
        self.values = {}
        self.reads = []
        self.forbidden = []
        for index, name in enumerate(SCALARS):
            self.values[symbols.addresses[name]] = index * 31
        for name, values in zip(BUFFERS, ((0, 255), (209, 210)), strict=True):
            for offset, value in enumerate(values):
                self.values[symbols.addresses[name] + offset] = value

    def __getitem__(self, address):
        assert set(self.symbols.lookups) == set(FIELDS), "resolve symbols before reads"
        if isinstance(address, slice):
            return [self[index] for index in range(address.start, address.stop, address.step or 1)]
        self.reads.append(address)
        return self.values[address]

    def __setitem__(self, address, value):
        self.forbidden.append("write")
        raise AssertionError("snapshot must not write memory")

    def tick(self, *args, **kwargs):
        self.forbidden.append("tick")
        raise AssertionError("snapshot must not tick")

    def send_input(self, *args, **kwargs):
        self.forbidden.append("input")
        raise AssertionError("snapshot must not invoke input")

    press = send_input
    button = send_input


def test_exact_schedule_including_every_gap_and_boundary(probe):
    expected = {0: ("up", 6), 20: ("up", 6), 40: ("up", 6)}
    expected.update({frame: ("a", 4) for frame in range(60, 301, 8)})
    for frame in range(301):
        actual = probe.menu_input_at(frame)
        assert actual == expected.get(frame), frame
        if actual is not None:
            assert type(actual) is tuple
            assert type(actual[0]) is str
            assert type(actual[1]) is int


def test_schedule_is_stateless_for_duplicate_and_out_of_order_queries(probe):
    for frame, expected in (
        (68, ("a", 4)),
        (68, ("a", 4)),
        (0, ("up", 6)),
        (59, None),
        (40, ("up", 6)),
        (60, ("a", 4)),
        (1, None),
        (20, ("up", 6)),
        (0, ("up", 6)),
        (60 + 8 * 10**20, ("a", 4)),
        (61 + 8 * 10**20, None),
    ):
        assert probe.menu_input_at(frame) == expected


@pytest.mark.parametrize("value", [True, False, 0.0, 60.0, "60", None, [], {}])
def test_schedule_rejects_non_integer_offsets(probe, value):
    with pytest.raises(TypeError):
        probe.menu_input_at(value)


@pytest.mark.parametrize("value", [-1, -20, -60, -(10**20)])
def test_schedule_rejects_negative_offsets(probe, value):
    with pytest.raises(ValueError):
        probe.menu_input_at(value)


def test_snapshot_reads_exact_fields_as_json_safe_values_without_side_effects(probe):
    symbols = Symbols()
    memory = ReadOnlyMemory(symbols)
    before = {address: int(value) for address, value in memory.values.items()}
    result = probe.read_menu_snapshot(
        symbols=symbols, memory=memory, owner_thread_id=threading.get_ident()
    )
    expected = {name: index * 31 for index, name in enumerate(SCALARS)}
    expected.update(dict(zip(BUFFERS, ([0, 255], [209, 210]), strict=True)))
    assert result == expected
    assert type(result) is dict
    assert all(type(result[name]) is int for name in SCALARS)
    for name in BUFFERS:
        assert type(result[name]) is list
        assert all(type(value) is int for value in result[name])
    assert json.loads(json.dumps(result, allow_nan=False)) == expected
    assert Counter(memory.reads) == Counter(before.keys())
    assert set(symbols.lookups) == set(FIELDS)
    assert {address: int(value) for address, value in memory.values.items()} == before
    assert memory.forbidden == []


@pytest.mark.parametrize("missing", FIELDS)
def test_missing_symbol_raises_named_key_error_before_memory_reads(probe, missing):
    symbols = Symbols()
    memory = ReadOnlyMemory(symbols)
    del symbols.addresses[missing]
    with pytest.raises(KeyError, match=missing):
        probe.read_menu_snapshot(
            symbols=symbols, memory=memory, owner_thread_id=threading.get_ident()
        )
    assert memory.reads == []
    assert memory.forbidden == []


def test_wrong_owner_rejected_before_symbol_resolution_or_memory_access(probe):
    symbols = Symbols()
    memory = ReadOnlyMemory(symbols)
    with pytest.raises(RuntimeError, match="(?i)(owner|thread)"):
        probe.read_menu_snapshot(
            symbols=symbols, memory=memory, owner_thread_id=threading.get_ident() + 1
        )
    assert symbols.lookups == []
    assert memory.reads == []
    assert memory.forbidden == []


def test_snapshot_returns_fresh_observations_and_independent_buffers(probe):
    symbols = Symbols()
    memory = ReadOnlyMemory(symbols)
    kwargs = {
        "symbols": symbols,
        "memory": memory,
        "owner_thread_id": threading.get_ident(),
    }
    first = probe.read_menu_snapshot(**kwargs)
    first[BUFFERS[0]][0] = 99
    memory.values[symbols.addresses["wCurMap"]] = 255
    second = probe.read_menu_snapshot(**kwargs)
    assert first is not second
    assert first["wCurMap"] == 0
    assert second["wCurMap"] == 255
    assert second[BUFFERS[0]] == [0, 255]
    assert second[BUFFERS[1]] == [209, 210]
    assert memory.forbidden == []


@pytest.mark.parametrize("name", (SCALARS[0], *BUFFERS))
@pytest.mark.parametrize("address", [True, False, 1.0, "49152", -1, 0x10000])
def test_invalid_addresses_fail_before_memory_reads(probe, name, address):
    symbols = Symbols()
    memory = ReadOnlyMemory(symbols)
    symbols.addresses[name] = address
    with pytest.raises(ValueError, match=name):
        probe.read_menu_snapshot(
            symbols=symbols, memory=memory, owner_thread_id=threading.get_ident()
        )
    assert memory.reads == []
    assert memory.forbidden == []


@pytest.mark.parametrize("name", FIELDS)
def test_none_symbol_address_is_explicit_missing_symbol(probe, name):
    symbols = Symbols()
    memory = ReadOnlyMemory(symbols)
    symbols.addresses[name] = None
    with pytest.raises(KeyError, match=name):
        probe.read_menu_snapshot(
            symbols=symbols, memory=memory, owner_thread_id=threading.get_ident()
        )
    assert memory.reads == []


@pytest.mark.parametrize("name", BUFFERS)
def test_buffer_address_cannot_cross_memory_end(probe, name):
    symbols = Symbols()
    memory = ReadOnlyMemory(symbols)
    symbols.addresses[name] = 0xFFFF
    with pytest.raises(ValueError, match=name):
        probe.read_menu_snapshot(
            symbols=symbols, memory=memory, owner_thread_id=threading.get_ident()
        )
    assert memory.reads == []


@pytest.mark.parametrize("name,size", [(SCALARS[0], 1), (BUFFERS[0], 2), (BUFFERS[1], 2)])
@pytest.mark.parametrize("at_end", [False, True])
def test_address_range_accepts_both_exact_limits(probe, name, size, at_end):
    symbols = Symbols()
    symbols.addresses[name] = 0x10000 - size if at_end else 0
    memory = ReadOnlyMemory(symbols)
    result = probe.read_menu_snapshot(
        symbols=symbols, memory=memory, owner_thread_id=threading.get_ident()
    )
    expected = [int(memory.values[symbols.addresses[name] + i]) for i in range(size)]
    assert result[name] == (expected[0] if size == 1 else expected)
    assert memory.forbidden == []


@pytest.mark.parametrize(
    "name,offset",
    [(SCALARS[0], 0), (BUFFERS[0], 0), (BUFFERS[0], 1), (BUFFERS[1], 0), (BUFFERS[1], 1)],
)
@pytest.mark.parametrize(
    "value,error",
    [
        (True, TypeError),
        (False, TypeError),
        (1.0, TypeError),
        ("1", TypeError),
        (None, TypeError),
        (ByteValue(), TypeError),
        (-1, ValueError),
        (256, ValueError),
    ],
)
def test_invalid_memory_bytes_raise_explicit_error(probe, name, offset, value, error):
    symbols = Symbols()
    memory = ReadOnlyMemory(symbols)
    memory.values[symbols.addresses[name] + offset] = value
    with pytest.raises(error, match=name):
        probe.read_menu_snapshot(
            symbols=symbols, memory=memory, owner_thread_id=threading.get_ident()
        )
    assert memory.forbidden == []


@pytest.mark.parametrize("owner", [None, True, False, "owner", 1.0])
def test_owner_must_be_explicit_integer(probe, owner):
    symbols = Symbols()
    memory = ReadOnlyMemory(symbols)
    with pytest.raises(RuntimeError, match="(?i)(owner|thread)"):
        probe.read_menu_snapshot(symbols=symbols, memory=memory, owner_thread_id=owner)
    assert symbols.lookups == []
    assert memory.reads == []
