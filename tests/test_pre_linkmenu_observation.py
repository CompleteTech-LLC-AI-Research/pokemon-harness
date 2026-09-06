"""Asset-free checks of the inert pre-LinkMenu observation schema."""

import hashlib
import json

import pytest

from tests import _tcp_trade_peer as peer
from tests._tcp_trade_peer import _PreLinkMenuHistory


def history(**overrides):
    options = {"enabled": True, "role": "listen", "version": "yellow"}
    options.update(overrides)
    return _PreLinkMenuHistory(**options)


def test_pre_linkmenu_exact_snapshot_fields():
    snapshot = history().snapshot()
    assert snapshot == {
        "schema_version": 1,
        "enabled": True,
        "available": False,
        "reason": "pins_unverified",
        "role": "listen",
        "version": "yellow",
        "pins": {},
        "observed_pins": {},
        "sites": {},
        "limit": 32,
        "total": 0,
        "counts": {},
        "first": {},
        "recent": [],
        "recent_dropped": 0,
        "recent_truncated": False,
        "hooks": {},
        "cleanup_pending": [],
        "error_limit": 8,
        "error_count": 0,
        "errors": [],
        "errors_dropped": 0,
        "errors_truncated": False,
    }
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_pre_linkmenu_disabled_overrides_pin_mismatch():
    snapshot = history(enabled=False, pins=[("rom", "expected")]).snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["available"] is False
    assert snapshot["reason"] == "disabled"
    assert snapshot["hooks"] == {}


def test_pre_linkmenu_enabled_never_claims_installed_hooks():
    assert history().snapshot()["reason"] == "pins_unverified"
    snapshot = history(pins=[("rom", "same")], observed_pins=[("rom", "same")]).snapshot()
    assert snapshot["reason"] == "hooks_not_installed"
    assert snapshot["available"] is False
    assert snapshot["hooks"] == {}


@pytest.mark.parametrize("reverse", [False, True])
def test_pre_linkmenu_pin_mismatch_is_stably_sorted(reverse):
    pins = [("z", "expected"), ("b", "same")]
    observed = [("z", "different"), ("b", "same"), ("a", "extra")]
    if reverse:
        pins.reverse()
        observed.reverse()
    snapshot = history(pins=pins, observed_pins=observed).snapshot()
    assert snapshot["reason"] == "pin_mismatch:a"
    assert list(snapshot["pins"]) == ["b", "z"]
    assert list(snapshot["observed_pins"]) == ["a", "b", "z"]
    assert history(pins=[("missing", "digest")]).snapshot()["reason"] == "pin_mismatch:missing"


@pytest.mark.parametrize("field,row", [("pins", "ab"), ("observed_pins", "ab"), ("sites", "abc")])
def test_pre_linkmenu_raw_string_rows_rejected(field, row):
    with pytest.raises(ValueError, match="rows must be tuples or lists"):
        history(**{field: [row]})


@pytest.mark.parametrize("value", [0, 1, None, "true"])
def test_pre_linkmenu_enabled_requires_actual_bool(value):
    with pytest.raises(ValueError, match="enabled must be a bool"):
        history(enabled=value)


@pytest.mark.parametrize("value", [False, True, 0, 1, 8, 31, 33, -1, 1.0, "32", None])
def test_pre_linkmenu_limit_rejects_bool_noninteger_and_out_of_bounds(value):
    with pytest.raises(ValueError, match="limit must be exactly 32"):
        history(limit=value)


@pytest.mark.parametrize(
    "row",
    [
        ("event", True, 0),
        ("event", False, 0),
        ("event", 1.0, 0),
        ("event", -1, 0),
        ("event", 0, True),
        ("event", 0, False),
        ("event", 0, 1.0),
        ("event", 0, -1),
        ("event", 0, 0x10000),
        (False, 0, 0),
        ("event", 0),
    ],
)
def test_pre_linkmenu_invalid_site_bounds_rejected(row):
    with pytest.raises(ValueError, match="event/bank/address triples"):
        history(sites=[row])


def test_pre_linkmenu_valid_site_and_limit_boundaries():
    snapshot = history(sites=[("low", 0, 0), ("high", 1, 0xFFFF)]).snapshot()
    assert snapshot["sites"] == {
        "low": {"bank": 0, "address": 0},
        "high": {"bank": 1, "address": 0xFFFF},
    }
    assert snapshot["counts"] == {"low": 0, "high": 0}
    assert snapshot["limit"] == 32


@pytest.mark.parametrize("field", ["pins", "observed_pins"])
@pytest.mark.parametrize("rows", [[("a", True)], [("a",)], [("a", "x"), ("a", "y")]])
def test_pre_linkmenu_invalid_pin_pairs_rejected(field, rows):
    with pytest.raises(ValueError):
        history(**{field: rows})


def test_pre_linkmenu_duplicate_site_rejected():
    with pytest.raises(ValueError, match="duplicate site event"):
        history(sites=[("event", 0, 1), ("event", 1, 2)])


def test_pre_linkmenu_inputs_and_snapshots_are_deeply_isolated():
    pins = [["rom", "digest"]]
    sites = [["event", 1, 0x1234]]
    observer = history(pins=pins, observed_pins=pins, sites=sites)
    pins[0][1] = "changed"
    sites[0][2] = 0
    # Explicit schema-state fixture: no session, emulator, or callback is used.
    observer.first["event"] = {"bytes": [1, 2]}
    observer.recent.append({"bytes": [3]})
    observer.hooks["event"] = {"nested": [4]}
    observer.cleanup_pending.append({"nested": [5]})
    observer.errors.append({"nested": [6]})
    original = observer.snapshot()
    mutated = observer.snapshot()
    mutated["pins"]["rom"] = "mutated"
    mutated["observed_pins"]["rom"] = "mutated"
    mutated["sites"]["event"]["address"] = 9
    mutated["counts"]["event"] = 9
    mutated["first"]["event"]["bytes"].append(9)
    mutated["recent"][0]["bytes"].append(9)
    mutated["hooks"]["event"]["nested"].append(9)
    mutated["cleanup_pending"][0]["nested"].append(9)
    mutated["errors"][0]["nested"].append(9)
    assert observer.snapshot() == original
    assert original["pins"] == {"rom": "digest"}
    assert original["sites"] == {"event": {"bank": 1, "address": 0x1234}}


def test_pre_linkmenu_ring_bounds_and_exact_drop_accounting():
    observer = history()
    # Populate inert schema storage, not fabricated emulator progress.
    count = 34
    for index in range(count):
        observer.recent.append({"index": index})
        observer.errors.append({"index": index})
    observer.total = count
    observer.error_count = count
    snapshot = observer.snapshot()
    assert snapshot["recent"] == [{"index": index} for index in range(2, count)]
    assert snapshot["errors"] == [{"index": index} for index in range(26, count)]
    assert observer.recent.maxlen == 32
    assert observer.errors.maxlen == 8
    assert snapshot["total"] == snapshot["error_count"] == count
    assert snapshot["recent_dropped"] == 2
    assert snapshot["errors_dropped"] == 26
    assert snapshot["recent_truncated"] is snapshot["errors_truncated"] is True


def synthetic_resolver_fixture(monkeypatch):
    """One complete fake Blue profile, with no ROM or symbol asset involved."""
    rom = bytearray(0x8000)
    npc, call, sync, timeout, connected, menu, close = (
        0x4100, 0x4100, 0x0200, 0x0180, 0x4145, 0x4300, 0x4180
    )

    def put(bank, address, data):
        offset = address if bank == 0 else bank * 0x4000 + address - 0x4000
        rom[offset : offset + len(data)] = data

    put(1, call, bytes((0xCD, sync & 0xFF, sync >> 8)) + bytes.fromhex("2147cc2a3c203b7e3c2037060a"))
    put(1, connected, bytes.fromhex("af3277"))
    put(0, sync, bytes.fromhex("3effea3ecc"))
    put(0, sync + 0x1C, bytes((0xAF, 0xC3, timeout & 0xFF, timeout >> 8)))
    put(0, sync + 0x43, b"\xc9")
    put(0, sync + 0x4A, bytes.fromhex("c660"))
    put(0, sync + 0x5F, bytes.fromhex("fe60c0"))
    put(0, timeout, bytes.fromhex("3dea47ccea48ccc9"))
    put(1, call + 25, bytes((0xCD, close & 0xFF, close >> 8)))
    put(1, call + 28, bytes((0x21, (close - 15) & 0xFF, (close - 15) >> 8)))
    put(1, call + 44, bytes((0xCD, close & 0xFF, close >> 8)))
    put(1, call + 47, bytes((0x21, (close - 10) & 0xFF, (close - 10) >> 8)))
    put(1, menu, bytes.fromhex("afea58"))
    symbols = "\n".join((
        "; fake rgblink output", "01 AGATHASROOM_AGATHA", f"01:{npc:04X} CableClubNPC",
        f"00:{sync:04X} Serial_SyncAndExchangeNybble",
        f"00:{timeout:04X} SetUnknownCounterToFFFF", f"01:{connected:04X} CableClubNPC.connected",
        f"01:{menu:04X} LinkMenu", f"01:{close:04X} CloseLinkConnection",
        f"01:{call + 44:04X} CableClubNPC.choseNo",
    )).encode("ascii")
    rom = bytes(rom)
    monkeypatch.setattr(peer, "_PRE_LINK_MENU_PROFILES", {
        ("listen", "blue_color"): (
            hashlib.sha1(rom).hexdigest(), hashlib.sha1(symbols).hexdigest(), npc, call,
            sync, timeout, connected, 1, menu, close,
        )
    })
    return rom, symbols


def test_pre_linkmenu_resolver_accepts_complete_synthetic_profile(monkeypatch):
    rom, symbols = synthetic_resolver_fixture(monkeypatch)
    snapshot = peer._resolve_pre_link_menu(
        enabled=True, role="listen", version="blue_color", rom_bytes=rom, symbol_bytes=symbols,
    ).snapshot()
    assert snapshot["reason"] == "hooks_not_installed"
    assert snapshot["available"] is False
    assert len(snapshot["sites"]) == 17
    assert "Serial_SyncAndExchangeNybble.receiveCompare" in snapshot["sites"]
    assert "CableClubNPC.inactivityCloseReturn" in snapshot["sites"]
    assert "CableClubNPC.choseNoCloseReturn" in snapshot["sites"]


def test_pre_linkmenu_resolver_disabled_and_unsupported_do_not_read_assets(monkeypatch):
    rom, symbols = synthetic_resolver_fixture(monkeypatch)
    disabled = peer._resolve_pre_link_menu(
        enabled=False, role="listen", version="blue_color", rom_bytes=object(), symbol_bytes=object(),
    ).snapshot()
    unsupported = peer._resolve_pre_link_menu(
        enabled=True, role="connect", version="blue_color", rom_bytes=rom, symbol_bytes=symbols,
    ).snapshot()
    assert disabled["reason"] == "disabled"
    assert unsupported["reason"] == "unsupported_role_version"


@pytest.mark.parametrize("offset,reason", [
    (0x0200 + 0x4A, "Serial_SyncAndExchangeNybble.publish60"),
    (0x0200 + 0x5F, "Serial_SyncAndExchangeNybble.receiveCompare"),
    (0x4100 + 25, "CableClubNPC.inactivityCloseCall"),
    (0x4100 + 44, "CableClubNPC.choseNoCloseCall"),
])
def test_pre_linkmenu_resolver_rejects_decision_signature_mutation(monkeypatch, offset, reason):
    rom, symbols = synthetic_resolver_fixture(monkeypatch)
    corrupted = bytearray(rom)
    corrupted[offset] ^= 1
    # Re-pin the deliberate synthetic mutation so this reaches signature validation.
    profile = list(peer._PRE_LINK_MENU_PROFILES[("listen", "blue_color")])
    profile[0] = hashlib.sha1(corrupted).hexdigest()
    monkeypatch.setattr(peer, "_PRE_LINK_MENU_PROFILES", {("listen", "blue_color"): tuple(profile)})
    snapshot = peer._resolve_pre_link_menu(
        enabled=True, role="listen", version="blue_color", rom_bytes=bytes(corrupted), symbol_bytes=symbols,
    ).snapshot()
    assert snapshot["reason"] == "signature_mismatch:" + reason


class _FakeCPU:
    cycles = 99
    PC = 0x1234
    SP = 0xFFFE
    A = 0x12
    F = 0xB0


class _FakePyBoy:
    def __init__(self, *, fail_address=None):
        self.memory = {}
        self.mb = type("MB", (), {"cpu": _FakeCPU()})()
        self.fail_address = fail_address
        self.registered = {}
        self.deregistered = []

    def hook_register(self, bank, address, callback, context):
        if address == self.fail_address:
            raise RuntimeError("planned registration failure")
        self.registered[(bank, address)] = callback

    def hook_deregister(self, bank, address):
        self.deregistered.append((bank, address))
        self.registered.pop((bank, address))


class _FakeSymbols:
    def __init__(self, addresses):
        self.addresses = addresses

    def bank_addr(self, name):
        return self.addresses[name]


class _FakeSession:
    def __init__(self, addresses, *, fail_address=None):
        self.symbols = _FakeSymbols(addresses)
        self._pyboy = _FakePyBoy(fail_address=fail_address)

    def current_tick(self):
        return 17


def _installed_observer(monkeypatch):
    rom, symbols = synthetic_resolver_fixture(monkeypatch)
    observer = peer._resolve_pre_link_menu(
        enabled=True, role="listen", version="blue_color", rom_bytes=rom, symbol_bytes=symbols,
    )
    addresses = {
        name: (0, 0xFF80 + offset)
        for offset, (name, _length) in enumerate(peer._PRE_LINK_MENU_FIELDS)
    }
    for offset, (name, _length) in enumerate(peer._PRE_LINK_MENU_FIELDS):
        addresses[name] = (0, 0xFF80 + offset * 2)
    return observer, addresses


def test_pre_linkmenu_installs_fifteen_owned_hooks_and_cleans_them(monkeypatch):
    observer, addresses = _installed_observer(monkeypatch)
    session = _FakeSession(addresses)
    for _name, address in addresses.values():
        session._pyboy.memory[address] = 7
        session._pyboy.memory[address + 1] = 8

    observer.install(session)
    assert observer.reason == "external_pending"
    assert observer.available is False
    assert len(session._pyboy.registered) == 15
    serial = observer.snapshot()["sites"]["Serial_SyncAndExchangeNybble"]
    assert (serial["bank"], serial["address"]) not in session._pyboy.registered

    observer.external_registered()
    before = observer.snapshot()["sites"]["CableClubNPC.beforeSync"]
    session._pyboy.registered[(before["bank"], before["address"])](None)
    snapshot = observer.snapshot()
    assert snapshot["available"] is True
    assert snapshot["counts"]["CableClubNPC.beforeSync"] == 1
    assert snapshot["recent"][-1]["frame"] == 17
    assert snapshot["recent"][-1]["fields"]["wUnknownSerialCounter"] == [7, 8]

    observer.close()
    assert observer.snapshot()["reason"] == "closed"
    assert session._pyboy.registered == {}
    assert len(session._pyboy.deregistered) == 15
    assert (serial["bank"], serial["address"]) not in session._pyboy.deregistered


def test_pre_linkmenu_owned_registration_failure_rolls_back_without_serial(monkeypatch):
    observer, addresses = _installed_observer(monkeypatch)
    owned = [site for site in observer.config.sites if site[0] not in (
        "LinkMenu", "Serial_SyncAndExchangeNybble",
    )]
    fail = owned[2]
    session = _FakeSession(addresses, fail_address=fail[2])

    observer.install(session)
    snapshot = observer.snapshot()
    assert snapshot["available"] is False
    assert snapshot["reason"] == "hook_registration_error:" + fail[0]
    assert snapshot["hooks"][fail[0]]["status"] == "registration_failed"
    assert len(session._pyboy.deregistered) == 2
    assert session._pyboy.registered == {}
    assert all(address != observer.snapshot()["sites"]["Serial_SyncAndExchangeNybble"]["address"]
               for _bank, address in session._pyboy.deregistered)


def test_linkmenu_serial_callback_fans_out_to_available_pre_observer(monkeypatch):
    observer, addresses = _installed_observer(monkeypatch)
    serial = next(site for site in observer.config.sites
                  if site[0] == "Serial_SyncAndExchangeNybble")
    addresses["Serial_SyncAndExchangeNybble"] = (serial[1], serial[2])
    session = _FakeSession(addresses)
    for _name, address in addresses.values():
        session._pyboy.memory[address] = 1
        session._pyboy.memory[address + 1] = 2
    observer.install(session)
    menu = peer._LinkMenuHistory(session, role="listen", version="blue_color")
    # Narrow the pre-existing history object to its serial owner for this fake
    # session; the callback's counter must still run alongside the fan-out.
    menu.counts = {}
    buckets = {"Serial_SyncAndExchangeNybble": [0]}
    menu.install(buckets, observers_by_address={(serial[1], serial[2]): observer})
    session._pyboy.registered[(serial[1], serial[2])](None)
    assert buckets["Serial_SyncAndExchangeNybble"] == [1]
    assert observer.snapshot()["available"] is True
    assert observer.snapshot()["counts"]["Serial_SyncAndExchangeNybble"] == 1
