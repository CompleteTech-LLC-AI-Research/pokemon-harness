"""ROM-free link-menu history regressions for the two-subprocess TCP peer.

Split from ``tests/test_pyboy_link_session_subprocess.py`` for #131 with no behavior
change: every assertion and test ID is preserved verbatim. These cases run
without ROM assets and drive the link-menu history recorder through its public API.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tests._pyboy_link_session_subprocess_support import (
    _REPO,
    _assert_peer_success,
    _collect_pair,
    _complete_peer_result,
    _CompletedPeer,
)
from tests._tcp_trade_peer import _TRADE_DIAG_SYMBOLS


class _HistoryMemory(dict):
    """Seed through dict.update; record every helper-originated write."""

    def __init__(self):
        super().__init__()
        self.writes = []
        self.reads = []

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        self.writes.append((key, value))
        super().__setitem__(key, value)


def _history_session(*, target_bank=0, target_address=0x2247, opcode=0xCD):
    from pokered_harness.symbols.loader import load_sym_text
    from tests._tcp_trade_peer import _LINK_MENU_HISTORY_EVENTS

    locations = {name: (3, 0x5000 + index * 0x10) for index, name in enumerate(_TRADE_DIAG_SYMBOLS)}
    locations["Serial_ExchangeLinkMenuSelection"] = (target_bank, target_address)
    locations["LinkMenu.exchangeMenuSelectionLoop"] = (3, 0x5800)
    ram_names = (
        "wCurrentMenuItem",
        "wMaxMenuItem",
        "wCableClubDestinationMap",
        "wLinkState",
        "hSerialConnectionStatus",
        "wCurMap",
        "wLinkMenuSelectionSendBuffer",
        "wLinkMenuSelectionReceiveBuffer",
    )
    locations.update({name: (0, 0xC000 + i * 8) for i, name in enumerate(ram_names)})
    symbols = load_sym_text(
        "\n".join(f"{bank:02x}:{address:04x} {name}" for name, (bank, address) in locations.items())
    )
    memory = _HistoryMemory()
    for name in ram_names:
        memory.update({symbols.addr_of(name) + offset: 0 for offset in range(2)})
    bank, address = locations["LinkMenu.exchangeMenuSelectionLoop"]
    memory.update(
        {
            (bank, address): opcode,
            (bank, address + 1): target_address & 255,
            (bank, address + 2): target_address >> 8,
        }
    )
    # A different currently mapped bank must not be used to validate the CALL.
    memory.update({address: 0, address + 1: 0, address + 2: 0})
    hooks = {}
    registrations = []
    failures = set()

    def register(bank, address, callback, context):
        registrations.append((bank, address))
        if (bank, address) in failures:
            raise ValueError("registration unavailable")
        assert (bank, address) not in hooks, "duplicate hook registration"
        hooks[bank, address] = (callback, context)

    session = SimpleNamespace(
        symbols=symbols,
        tick=0,
        _pyboy=SimpleNamespace(memory=memory, hook_register=register),
    )
    session.current_tick = lambda: session.tick

    def fire(event):
        location = (bank, address + 3) if event == "LinkMenu.afterExchange" else locations[event]
        callback, context = hooks[location]
        callback(context)

    assert set(_LINK_MENU_HISTORY_EVENTS) <= set(locations) | {"LinkMenu.afterExchange"}
    return session, memory, hooks, registrations, failures, fire


def test_link_menu_history_preserves_first_samples_across_buffer_reuse():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="listen", version="blue", limit=3)
    buckets = {name: [0] for name in _TRADE_DIAG_SYMBOLS}
    history.install(buckets)
    installed = list(registrations)
    history.install(buckets)
    assert registrations == installed
    send = session.symbols.addr_of("wLinkMenuSelectionSendBuffer")
    receive = session.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
    memory.update({send: 0xD4, send + 1: 0xFF, receive: 0xD4, receive + 1: 0xFF})
    events = [
        "LinkMenu.doneChoosingMenuSelection",
        "LinkMenu.afterExchange",
        "PrepareForSpecialWarp",
        "SpecialEnterMap",
    ]
    for tick, event in enumerate(events, 1):
        session.tick = tick
        fire(event)
    original = history.snapshot()
    memory.update({send: 0x12, receive: 0x34})
    for tick in range(5, 10):
        session.tick = tick
        fire("LinkMenu.afterExchange")
    result = history.snapshot()
    assert result["total"] == 9
    assert {event: count for event, count in result["counts"].items() if count} == {
        event: (6 if event == "LinkMenu.afterExchange" else 1) for event in events
    }
    assert [sample["tick"] for sample in result["recent"]] == [7, 8, 9]
    assert result["first"] == original["first"]
    for event in events:
        sample = result["first"][event]
        assert sample["event"] == event
        assert (sample["role"], sample["version"]) == ("listen", "blue")
        assert sample["wLinkMenuSelectionSendBuffer"] == [0xD4, 0xFF]
        assert sample["wLinkMenuSelectionReceiveBuffer"] == [0xD4, 0xFF]
    assert result["recent"][-1]["wLinkMenuSelectionReceiveBuffer"] == [0x34, 0xFF]
    assert buckets["SpecialEnterMap"] == [1]
    assert memory.writes == []
    # Neither caller mutation nor later ROM buffer reuse can alter retained evidence.
    result["first"][events[0]]["wLinkMenuSelectionSendBuffer"][0] = 0
    result["recent"].clear()
    assert history.snapshot()["first"] == original["first"]
    assert len(history.snapshot()["recent"]) == 3
    json.dumps(history.snapshot())


@pytest.mark.parametrize(
    "opcode,target_bank,target_address,valid",
    [
        (0xCD, 0, 0x2247, True),
        (0x00, 0, 0x2247, False),
        (0xCD, 3, 0x6247, True),
        (0xCD, 2, 0x6247, False),
        (0xCD, 2, 0x2247, False),
        (0xCD, 0, 0x6247, False),
    ],
)
def test_link_menu_history_validates_call_and_bank(opcode, target_bank, target_address, valid):
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, hooks, _registrations, _failures, _fire = _history_session(
        opcode=opcode,
        target_bank=target_bank,
        target_address=target_address,
    )
    history = _LinkMenuHistory(session, role="connect", version="yellow")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    bank, address = session.symbols.bank_addr("LinkMenu.exchangeMenuSelectionLoop")
    assert ((bank, address + 3) in hooks) is valid
    assert history.snapshot()["hooks"]["LinkMenu.afterExchange"]["available"] is valid
    assert (bank, address) in memory.reads
    assert memory.writes == []


def test_link_menu_history_rejects_call_to_wrong_target():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, hooks, _registrations, _failures, _fire = _history_session()
    bank, address = session.symbols.bank_addr("LinkMenu.exchangeMenuSelectionLoop")
    memory.update({(bank, address + 1): 0x48})
    history = _LinkMenuHistory(session, role="listen", version="red")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    assert (bank, address + 3) not in hooks
    assert history.snapshot()["hooks"]["LinkMenu.afterExchange"]["available"] is False
    assert memory.writes == []


def test_link_menu_history_additive_result_compatibility():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, _memory, _hooks, _registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="listen", version="yellow")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    fire("LinkMenu")
    legacy = _complete_peer_result(role="connect")
    enriched = _complete_peer_result()
    enriched["_link_menu_history"] = history.snapshot()
    first, second = _collect_pair(
        _CompletedPeer(enriched), _CompletedPeer(legacy), deadline_at=time.monotonic() + 1.0
    )
    assert first["_link_menu_history"] == enriched["_link_menu_history"]
    assert "_link_menu_history" not in second
    for result in (first, second):
        _assert_peer_success(result, label="fake")


def test_link_menu_history_reports_missing_symbols_and_registration_errors():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, _registrations, failures, fire = _history_session()
    symbols = session.symbols

    def bank_addr(name):
        if name == "LinkMenu.choseCancel":
            raise KeyError(name)
        return symbols.bank_addr(name)

    session.symbols = SimpleNamespace(bank_addr=bank_addr, addr_of=symbols.addr_of)
    failures.add(symbols.bank_addr("PrepareForSpecialWarp"))
    history = _LinkMenuHistory(session, role="listen", version="red")
    buckets = {name: [0] for name in _TRADE_DIAG_SYMBOLS}
    history.install(buckets)
    fire("LinkMenu")
    result = history.snapshot()
    assert result["hooks"]["LinkMenu.choseCancel"]["available"] is False
    assert result["hooks"]["PrepareForSpecialWarp"]["available"] is False
    assert result["hooks"]["LinkMenu"]["available"] is True
    assert {(error["event"], error["stage"]) for error in result["errors"]} == {
        ("LinkMenu.choseCancel", "resolve"),
        ("PrepareForSpecialWarp", "register"),
    }
    assert result["error_count"] == 2
    assert result["total"] == 1
    assert buckets["LinkMenu"] == [1]
    assert buckets["PrepareForSpecialWarp"] == [0]
    assert memory.writes == []


def test_link_menu_history_bounds_callback_errors_and_keeps_partial_samples():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, _registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="connect", version="yellow", limit=2)
    buckets = {name: [0] for name in _TRADE_DIAG_SYMBOLS}
    history.install(buckets)
    symbols = session.symbols

    def addr_of(name):
        if name == "wCurrentMenuItem":
            raise KeyError("missing symbol " + "x" * 400)
        return symbols.addr_of(name)

    def broken_tick():
        raise RuntimeError("tick unavailable " + "x" * 400)

    session.symbols = SimpleNamespace(addr_of=addr_of)
    session.current_tick = broken_tick
    del memory[symbols.addr_of("wLinkMenuSelectionReceiveBuffer") + 1]
    for _ in range(4):
        fire("LinkMenu")
    result = history.snapshot()
    assert result["total"] == result["counts"]["LinkMenu"] == 4
    assert result["error_count"] == 12
    assert result["errors_dropped"] == 10
    assert result["errors_truncated"] is True
    assert len(result["errors"]) == len(result["recent"]) == 2
    assert [error["stage"] for error in result["errors"]] == [
        "wCurrentMenuItem",
        "wLinkMenuSelectionReceiveBuffer",
    ]
    assert all(len(error["error"]) <= 256 for error in result["errors"])
    for sample in [result["first"]["LinkMenu"], *result["recent"]]:
        assert sample["tick"] is None
        assert "wCurrentMenuItem" not in sample
        assert "wLinkMenuSelectionReceiveBuffer" not in sample
        assert sample["wLinkMenuSelectionSendBuffer"] == [0, 0]
    assert buckets["LinkMenu"] == [4]
    assert memory.writes == []
    result["errors"][0]["error"] = "caller mutation"
    assert history.snapshot()["errors"][0]["error"] != "caller mutation"
    json.dumps(result)


def test_link_menu_history_decisive_directions_ignore_stale_second_bytes():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, _registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="listen", version="blue")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    assert history.snapshot()["limit"] == 8
    event = "LinkMenu.afterExchange"
    send = session.symbols.addr_of("wLinkMenuSelectionSendBuffer")
    receive = session.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
    memory.update({send + 1: 0xD4, receive: 0xD0, receive + 1: 0xD8})
    for tick in range(1, 21):
        session.tick = tick
        fire(event)
    idle = history.snapshot()
    assert idle["first_decisive"] == {}
    session.tick = 21
    memory.update({send: 0xD4})
    fire(event)
    sent = history.snapshot()
    assert sent["first_decisive"]["sent"]["tick"] == 21
    assert "received" not in sent["first_decisive"]
    session.tick = 22
    memory.update({send: 0, receive: 0xD8})
    fire(event)
    for tick in range(23, 40):
        session.tick = tick
        memory.update({send: 0x12, receive: 0x34})
        fire(event)
    result = history.snapshot()
    assert result["first"][event]["tick"] == 1
    assert result["first_decisive"]["sent"] == sent["first_decisive"]["sent"]
    for direction, tick, symbol, value in (
        ("sent", 21, "wLinkMenuSelectionSendBuffer", 0xD4),
        ("received", 22, "wLinkMenuSelectionReceiveBuffer", 0xD8),
    ):
        sample = result["first_decisive"][direction]
        assert sample["tick"] == sample["seq"] == tick
        assert sample[symbol][0] == value
        assert sample["wCurMap"] == 0
    assert [sample["tick"] for sample in result["recent"]] == list(range(32, 40))
    assert result["total"] == result["counts"][event] == 39
    assert result["recent_dropped"] == 31
    assert result["recent_truncated"] is True
    assert result["errors_dropped"] == 0
    assert result["errors_truncated"] is False
    result["first_decisive"]["received"]["wLinkMenuSelectionReceiveBuffer"][0] = 0
    assert (
        history.snapshot()["first_decisive"]["received"]["wLinkMenuSelectionReceiveBuffer"][0]
        == 0xD8
    )
    assert memory.writes == []


@pytest.mark.parametrize(
    "first,second,candidate,index",
    [
        (0x12, 0xD4, 0xD4, 1),
        (0xD0, 0xD4, None, None),
        (0x12, 0x34, None, None),
        (0xD8, 0xD4, 0xD8, 0),
    ],
)
def test_link_menu_history_received_candidate_follows_rom_order(first, second, candidate, index):
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, _registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="connect", version="yellow")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    address = session.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
    memory.update({address: first, address + 1: second})
    fire("LinkMenu.afterExchange")
    decisive = history.snapshot()["first_decisive"]
    if first == 0xD0:
        assert history.snapshot()["recent"][-1]["recv_candidate"] == {"value": 0xD0, "index": 0}
    if candidate is None:
        assert "received" not in decisive
    else:
        assert decisive["received"]["recv_candidate"] == {"value": candidate, "index": index}
        assert decisive["received"]["wLinkMenuSelectionReceiveBuffer"] == [first, second]
    assert memory.writes == []


def test_link_menu_history_failure_summary_survives_large_result_tail(tmp_path):
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, _registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="connect", version="yellow", limit=1)
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    address = session.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
    memory.update({address: 0x12, address + 1: 0xD4})
    memory.update({session.symbols.addr_of("wLinkMenuSelectionSendBuffer"): 0xD8})
    for event in (
        "LinkMenu.doneChoosingMenuSelection",
        "LinkMenu.afterExchange",
        "PrepareForSpecialWarp",
        "SpecialEnterMap",
    ):
        fire(event)
    result = _complete_peer_result(role="connect")
    result.update(
        _drive_status="error",
        _drive_error="warp timeout",
        _link_menu_history=history.snapshot(),
        large_trace="x" * 10000,
    )
    with pytest.raises(AssertionError) as raised:
        _assert_peer_success(result, label="connector")
    message = str(raised.value)
    assert len(message) > 10000
    summary = message.rsplit("\n", 1)[1]
    assert len(summary) <= 1000
    assert summary in message[-4000:]
    decoded = json.loads(summary.removeprefix("link-menu-summary="))
    assert (decoded["endpoint"], decoded["role"], decoded["version"]) == (
        "connector",
        "connect",
        "yellow",
    )
    assert decoded["votes"]["received"]["candidate"] == {"value": 0xD4, "index": 1}
    assert decoded["votes"]["sent"]["candidate"] == {"value": 0xD8, "index": 0}
    assert decoded["milestones"]["warp"] == {
        "seq": 4,
        "available": True,
        "count": 1,
    }
    assert decoded["recent_dropped"] == 3
    assert decoded["recent_truncated"] is True
    # Pytest renders captured stdout after the traceback; stderr must carry
    # the same bounded summary after that large output as well.
    source = tmp_path / "test_fake_failure.py"
    source.write_text(
        "from tests._pyboy_link_session_subprocess_support import _assert_peer_success\n"
        "def test_fake_failure():\n"
        "    print('captured-large-output-' + 'x' * 10000)\n"
        f"    _assert_peer_success({result!r}, label='connector')\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "pytest_asyncio.plugin", "-q", str(source)],
        cwd=_REPO,
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                (str(_REPO), str(_REPO / "src"), str(_REPO / "vendor/pyboy-src"))
            ),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 1
    assert "Captured stdout" in completed.stdout
    assert "Captured stderr" in completed.stdout
    assert summary in completed.stdout[-4000:]


@pytest.mark.parametrize(
    "missing",
    [
        "LinkMenu.exchangeMenuSelectionLoop",
        "Serial_ExchangeLinkMenuSelection",
    ],
)
def test_link_menu_history_missing_call_symbols_remains_observable(missing):
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, hooks, _registrations, _failures, fire = _history_session()
    symbols = session.symbols

    def bank_addr(name):
        if name == missing:
            raise KeyError(name)
        return symbols.bank_addr(name)

    session.symbols = SimpleNamespace(bank_addr=bank_addr, addr_of=symbols.addr_of)
    history = _LinkMenuHistory(session, role="listen", version="blue")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    bank, address = symbols.bank_addr("LinkMenu.exchangeMenuSelectionLoop")
    assert (bank, address + 3) not in hooks
    fire("SpecialEnterMap")
    result = history.snapshot()
    assert result["hooks"]["LinkMenu.afterExchange"]["available"] is False
    assert result["counts"]["SpecialEnterMap"] == 1
    assert any(
        error["event"] == "LinkMenu.afterExchange" and error["stage"] == "resolve"
        for error in result["errors"]
    )
    assert memory.writes == []


@pytest.mark.parametrize("bank,address", [(0, 0x3FFD), (3, 0x7FFD)])
def test_link_menu_history_rejects_post_call_outside_bank(bank, address):
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, hooks, _registrations, _failures, _fire = _history_session()
    symbols = session.symbols
    session.symbols = SimpleNamespace(
        bank_addr=lambda name: (
            (bank, address)
            if name == "LinkMenu.exchangeMenuSelectionLoop"
            else symbols.bank_addr(name)
        ),
        addr_of=symbols.addr_of,
    )
    memory.update({(bank, address): 0xCD, (bank, address + 1): 0x47, (bank, address + 2): 0x22})
    history = _LinkMenuHistory(session, role="listen", version="blue")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    assert (bank, address + 3) not in hooks
    assert history.snapshot()["hooks"]["LinkMenu.afterExchange"]["available"] is False
    assert memory.writes == []
