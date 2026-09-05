"""Observation-only milestone contracts; authored evidence is not gameplay proof."""

import hashlib
import importlib
import importlib.machinery
import json
import os
import selectors
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import _timed_menu_probe as helper

TEXT = "CableClubNPCPleaseApplyHereHaveToSaveText"
SITES = {
    "PrintText": (0, 0x1200),
    "YesNoChoice": (1, 0x4100),
    "SaveGameData": (2, 0x4200),
    "LinkMenu": (3, 0x4300),
}
NAMES = ("save_request", "yes_no", "save_game", "link_menu")


class Symbols:
    def __init__(self):
        self.sites = {**SITES, TEXT: (4, 0x4400)}
        self.lookups = []

    def bank_addr(self, name):
        self.lookups.append(name)
        return self.sites[name]

    def addr_of(self, name):
        self.lookups.append(name)
        assert name == "hLoadedROMBank"
        return 0xFFB8


class ReadOnly:
    def __init__(self, values):
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "reads", [])
        object.__setattr__(self, "writes", [])

    def __getitem__(self, key):
        self.reads.append(key)
        return self.values[key]

    def __getattr__(self, key):
        return self[key]

    def __setitem__(self, key, value):
        self.writes.append((key, value))
        raise AssertionError("observation attempted a write")

    __setattr__ = __setitem__


class Harness:
    def __init__(self):
        self.symbols = Symbols()
        self.memory = ReadOnly({0xFFB8: 4})
        self.registers = ReadOnly({"PC": 0x1200, "HL": 0x4400})
        self.frames = 77
        self.frame_reads = 0
        self.hooks = {}
        self.installed = []
        self.removed = []
        self.fail_register = None
        self.fail_remove = set()

    def frame_count(self):
        self.frame_reads += 1
        return self.frames

    def register(self, bank, address, callback, context):
        assert set(self.symbols.lookups) == {*SITES, TEXT, "hLoadedROMBank"}
        site = (bank, address)
        if site == self.fail_register or site in self.hooks:
            raise ValueError("atomic registration collision")
        self.hooks[site] = (callback, context)
        self.installed.append(site)

    def remove(self, bank, address):
        site = (bank, address)
        self.removed.append(site)
        if site in self.fail_remove:
            raise RuntimeError(f"remove failed {site}")
        del self.hooks[site]

    def observe(self, **overrides):
        kwargs = {
            "symbols": self.symbols,
            "memory": self.memory,
            "register_file": self.registers,
            "frame_count": self.frame_count,
            "hook_register": self.register,
            "hook_deregister": self.remove,
            "owner_thread_id": threading.get_ident(),
            "enabled": True,
        }
        kwargs.update(overrides)
        return helper.observe_rom_milestones(**kwargs)

    def fire(self, name):
        callback, context = self.hooks[SITES[name]]
        callback(context)


def off_thread(operation):
    errors = []

    def run():
        try:
            operation()
        except BaseException as exc:  # noqa: BLE001 - retain any thread failure for assertions
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    return errors


def test_disabled_touches_no_dependencies_and_still_checks_owner():
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled observer accessed dependency")

    with helper.observe_rom_milestones(
        symbols=None,
        memory=None,
        register_file=None,
        frame_count=forbidden,
        hook_register=forbidden,
        hook_deregister=forbidden,
        owner_thread_id=threading.get_ident(),
    ) as observer:
        assert observer.snapshot() == {
            "enabled": False,
            "counts": dict.fromkeys(NAMES, 0),
            "events": [],
            "error": None,
        }
        observer.check()
        for operation in (observer.snapshot, observer.check):
            errors = off_thread(operation)
            assert len(errors) == 1 and isinstance(errors[0], RuntimeError)


@pytest.mark.parametrize("limit", [True, False, 0, -1, 257, 1.0, None, "32"])
def test_invalid_event_bound_never_installs(limit):
    harness = Harness()
    with pytest.raises((TypeError, ValueError)), harness.observe(max_events=limit):
        pytest.fail("accepted invalid bound")
    assert harness.installed == []


@pytest.mark.parametrize("missing", [*SITES, TEXT])
def test_missing_symbol_resolved_before_any_install(missing):
    harness = Harness()
    del harness.symbols.sites[missing]
    with pytest.raises((KeyError, ValueError, RuntimeError)), harness.observe():
        pytest.fail("accepted missing symbol")
    assert harness.installed == []


@pytest.mark.parametrize("site", [(0, 0x4000), (1, 0x3FFF), (256, 0x4000), (-1, 0), (1, 0x8000)])
def test_invalid_executable_bank_address_never_installs(site):
    harness = Harness()
    harness.symbols.sites["LinkMenu"] = site
    with pytest.raises((ValueError, RuntimeError)), harness.observe():
        pytest.fail("accepted invalid executable site")
    assert harness.installed == []


def test_duplicate_executable_sites_rejected_before_install():
    harness = Harness()
    harness.symbols.sites["LinkMenu"] = SITES["YesNoChoice"]
    with pytest.raises((ValueError, RuntimeError)), harness.observe():
        pytest.fail("accepted alias")
    assert harness.installed == []


def test_actual_callbacks_exact_context_detached_snapshot_and_no_writes():
    harness = Harness()
    with harness.observe() as observer:
        assert set(harness.hooks) == set(SITES.values())
        for name in SITES:
            harness.registers.values["PC"] = SITES[name][1]
            harness.fire(name)
        snapshot = observer.snapshot()
        assert snapshot["counts"] == dict.fromkeys(NAMES, 1)
        assert snapshot["error"] is None
        assert snapshot["events"] == [
            {
                "milestone": milestone,
                "bank": bank,
                "address": address,
                "completed_so_far": 77,
                "PC": address,
                "HL": 0x4400,
                "hLoadedROMBank": 4,
            }
            for milestone, (bank, address) in zip(NAMES, SITES.values(), strict=True)
        ]
        assert json.loads(json.dumps(snapshot, allow_nan=False)) == snapshot
        snapshot["counts"]["yes_no"] = 100
        snapshot["events"][0]["PC"] = 0
        assert observer.snapshot()["counts"]["yes_no"] == 1
        assert observer.snapshot()["events"][0]["PC"] == 0x1200
        observer.check()
    assert harness.removed == list(reversed(harness.installed))
    assert not harness.memory.writes and not harness.registers.writes


@pytest.mark.parametrize(
    "text_bank,loaded,hl,expected",
    [
        (4, 4, 0x4400, 1),
        (4, 5, 0x4400, 0),
        (4, 4, 0x4401, 0),
        (0, 9, 0x2400, 1),
    ],
)
def test_save_request_requires_exact_pointer_and_bank(text_bank, loaded, hl, expected):
    harness = Harness()
    harness.symbols.sites[TEXT] = (text_bank, 0x2400 if text_bank == 0 else 0x4400)
    harness.memory.values[0xFFB8] = loaded
    harness.registers.values["HL"] = hl
    with harness.observe() as observer:
        harness.fire("PrintText")
        assert observer.snapshot()["counts"]["save_request"] == expected
        assert (text_bank, harness.symbols.sites[TEXT][1]) not in harness.hooks


def test_overflow_is_sticky_counts_continue_without_context_access():
    harness = Harness()
    with pytest.raises(RuntimeError), harness.observe(max_events=1) as observer:
        harness.fire("YesNoChoice")
        harness.fire("YesNoChoice")
        snapshot = observer.snapshot()
        assert snapshot["error"]
        accesses = (
            len(harness.registers.reads),
            len(harness.memory.reads),
            harness.frame_reads,
        )
        for _ in range(300):
            harness.fire("YesNoChoice")
        assert accesses == (
            len(harness.registers.reads),
            len(harness.memory.reads),
            harness.frame_reads,
        )
        assert observer.snapshot()["counts"]["yes_no"] == 302
        assert len(observer.snapshot()["events"]) == 1
        assert observer.snapshot()["error"] == snapshot["error"]
        with pytest.raises(RuntimeError):
            observer.check()
    assert harness.removed == list(reversed(harness.installed))


def test_wrong_owner_callback_defers_failure_without_reading_dependencies():
    harness = Harness()
    with pytest.raises(RuntimeError), harness.observe() as observer:
        assert off_thread(lambda: harness.fire("YesNoChoice")) == []
        assert not harness.memory.reads and not harness.registers.reads
        assert harness.frame_reads == 0
        assert observer.snapshot()["error"]
    assert harness.removed == list(reversed(harness.installed))


def test_wrong_owner_enter_precedes_symbol_access():
    harness = Harness()
    with pytest.raises(RuntimeError), harness.observe(owner_thread_id=-1):
        pytest.fail("accepted wrong owner")
    assert harness.symbols.lookups == []
    assert harness.installed == []


@pytest.mark.parametrize("failed_index", range(4))
def test_atomic_partial_install_preserves_failed_preexisting_slot(failed_index):
    harness = Harness()
    site = list(SITES.values())[failed_index]
    original = (lambda _: None, object())
    harness.hooks[site] = original
    with pytest.raises((ValueError, RuntimeError)), harness.observe():
        pytest.fail("accepted occupied slot")
    assert harness.hooks == {site: original}
    assert site not in harness.removed
    assert harness.removed == list(reversed(harness.installed))


def leaves(exc):
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for child in exc.exceptions for leaf in leaves(child)]
    return [exc]


def test_body_and_all_cleanup_failures_are_preserved_and_callbacks_inert():
    harness = Harness()
    harness.fail_remove = {SITES["YesNoChoice"], SITES["LinkMenu"]}
    sentinel = KeyboardInterrupt("body sentinel")
    with pytest.raises(BaseExceptionGroup) as caught, harness.observe() as observer:
        callbacks = list(harness.hooks.values())
        raise sentinel
    errors = leaves(caught.value)
    assert sentinel in errors
    assert sum("remove failed" in str(error) for error in errors) >= 2
    assert harness.removed == list(reversed(harness.installed))
    before = observer.snapshot()
    for callback, context in callbacks:
        callback(context)
    assert observer.snapshot() == before


def test_counter_saturation_fails_explicitly_instead_of_silently_losing_hits():
    harness = Harness()
    with (
        pytest.raises(RuntimeError, match="milestone hit count overflow"),
        harness.observe() as observer,
    ):
        observer.counts["yes_no"] = (1 << 63) - 2
        harness.fire("YesNoChoice")
        assert observer.snapshot()["counts"]["yes_no"] == (1 << 63) - 1
        assert observer.snapshot()["error"] is None
        harness.fire("YesNoChoice")
        harness.fire("YesNoChoice")
        assert observer.snapshot()["counts"]["yes_no"] == (1 << 63) - 1
        assert observer.snapshot()["error"] == "milestone hit count overflow"
        assert len(observer.snapshot()["events"]) == 1


def test_callback_error_and_body_error_both_survive_cleanup_and_snapshot():
    harness = Harness()
    body = ValueError("distinct body sentinel")
    with pytest.raises(BaseExceptionGroup) as caught, harness.observe() as observer:
        harness.registers.values["PC"] = "invalid register"
        harness.fire("YesNoChoice")  # Ordinary errors never escape callback.
        snapshot = observer.snapshot()
        assert snapshot["error"] and "PC" in snapshot["error"]
        raise body
    errors = leaves(caught.value)
    assert body in errors
    assert any(
        isinstance(error, RuntimeError) and str(error) == snapshot["error"] for error in errors
    )
    assert observer.snapshot() == snapshot
    assert harness.removed == list(reversed(harness.installed))


def test_boundary_check_error_is_not_duplicated_on_exit():
    harness = Harness()
    with pytest.raises(RuntimeError) as caught, harness.observe() as observer:
        harness.registers.values["HL"] = True
        harness.fire("PrintText")
        observer.check()
    assert str(caught.value) == observer.snapshot()["error"]
    assert harness.removed == list(reversed(harness.installed))


@pytest.mark.parametrize("site", [(0, 0x4000), (1, 0x3FFF), (256, 0x4000), SITES["PrintText"]])
def test_text_site_invalid_or_executable_alias_rejected_before_install(site):
    harness = Harness()
    harness.symbols.sites[TEXT] = site
    with pytest.raises(ValueError), harness.observe():
        pytest.fail("accepted invalid text location")
    assert harness.installed == []


@pytest.mark.parametrize("field,value", [("PC", -1), ("PC", 65536), ("HL", True), ("HL", 65536)])
def test_callback_invalid_register_is_deferred(field, value):
    harness = Harness()
    harness.registers.values[field] = value
    with pytest.raises(RuntimeError), harness.observe() as observer:
        harness.fire("YesNoChoice")
        assert observer.snapshot()["error"]
        assert observer.snapshot()["events"] == []


def _authored_cartridge_check(directory):
    """Execute our own LR35902 bytes through the actual selected PyBoy runtime."""
    from pyboy import PyBoy

    identity = {}
    for name in ("pyboy.pyboy", "pyboy.core.mb", "pyboy.core.cpu"):
        path = importlib.import_module(name).__file__
        identity[name] = {
            "path": path,
            "native": any(
                path.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES
            ),
        }
    assert len({item["native"] for item in identity.values()}) == 1
    boot = bytearray(256)
    boot[:6] = bytes([0x31, 0, 0xD0, 0xC3, 0xFC, 0])
    boot[252:] = bytes([0x3E, 1, 0xE0, 0x50])
    rom = bytearray(0x8000)
    rom[0x100:0x103] = bytes([0xC3, 0x50, 1])
    rom[0x134:0x13D] = b"MILESTONE"
    # DI, LCD on, HL points at authored text, call four RET routines, loop.
    program = [0xF3, 0x3E, 0x91, 0xE0, 0x40, 0x21, 0, 3]
    sites = {name: (0, 0x200 + i * 0x10) for i, name in enumerate(SITES)}
    for _, address in sites.values():
        program.extend([0xCD, address & 255, address >> 8])
        rom[address] = 0xC9
    program.extend([0x18, 0xFE])
    rom[0x150 : 0x150 + len(program)] = bytes(program)
    rom[0x14D] = (-sum(rom[0x134:0x14D]) - 25) & 255
    rom_path, boot_path = directory / "milestones.gb", directory / "milestones.boot"
    rom_path.write_bytes(rom)
    boot_path.write_bytes(boot)
    symbols = Symbols()
    symbols.sites = {**sites, TEXT: (0, 0x300)}
    results = []
    for enabled in (False, True):
        game = PyBoy(str(rom_path), bootrom=str(boot_path), window="null", sound_emulated=False)
        try:
            game.set_emulation_speed(0)
            callback_frames = []

            def frame_count(game=game, callback_frames=callback_frames):
                callback_frames.append(game.frame_count)
                return game.frame_count

            with helper.observe_rom_milestones(
                symbols=symbols,
                memory=game.memory,
                register_file=game.register_file,
                frame_count=frame_count,
                hook_register=game.hook_register,
                hook_deregister=game.hook_deregister,
                owner_thread_id=threading.get_ident(),
                enabled=enabled,
            ) as observer:
                assert game.tick(3, render=False, sound=False)
                observer.check()
                snapshot = observer.snapshot()
            assert game.tick(1, render=False, sound=False)
            results.append(
                {
                    "registers": {
                        name: getattr(game.register_file, name)
                        for name in (
                            "PC",
                            "SP",
                            "A",
                            "F",
                            "B",
                            "C",
                            "D",
                            "E",
                            "HL",
                        )
                    },
                    "memory": list(game.memory[0xC000:0xE000]),
                    "cycles": game.mb.cpu.cycles,
                    "frames": game.frame_count,
                }
            )
            if enabled:
                assert snapshot["counts"] == dict.fromkeys(NAMES, 1)
                assert [event["PC"] for event in snapshot["events"]] == [
                    address for _, address in sites.values()
                ]
                assert [
                    event["completed_so_far"] for event in snapshot["events"]
                ] == callback_frames
                assert snapshot["error"] is None
        finally:
            game.stop(save=False)
    assert results[0] == results[1], "observer altered CPU, RAM, or completed frames"
    return {
        "runtime": identity,
        "counts": snapshot["counts"],
        "frames": results[1]["frames"],
        "callback_frames": callback_frames,
    }


def bounded_child(command):
    output = bytearray()
    deadline = time.monotonic() + 45
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                assert remaining > 0, "authored child exceeded deadline"
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, min(4096, 32769 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    assert len(output) <= 32768, "authored child exceeded output cap"
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        assert process.returncode == 0, output.decode(errors="replace")
        return output.decode()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()


def test_authored_cartridge_actual_helper_is_non_mutating(tmp_path):
    command = [
        sys.executable,
        "-B",
        "-c",
        (
            "import json,runpy,sys; from pathlib import Path; "
            "sys.path[:]=json.loads(sys.argv[1]); "
            "ns=runpy.run_path(sys.argv[2]); "
            "print('MILESTONE_RESULT='+json.dumps(ns['_authored_cartridge_check'](Path(sys.argv[3]))))"
        ),
        json.dumps(sys.path),
        str(Path(__file__).resolve()),
        str(tmp_path),
    ]
    output = bounded_child(command)
    rows = [
        line.removeprefix("MILESTONE_RESULT=")
        for line in output.splitlines()
        if line.startswith("MILESTONE_RESULT=")
    ]
    assert len(rows) == 1
    result = json.loads(rows[0])
    assert result["counts"] == dict.fromkeys(NAMES, 1)
    assert result["frames"] == 4
    for name, item in result["runtime"].items():
        assert (
            Path(item["path"]).resolve() == Path(importlib.import_module(name).__file__).resolve()
        )
    print(json.dumps(result, sort_keys=True))


def probe_args(*extra):
    from scripts import probe_timed_rom_pair as probe

    return probe.parse_args(
        [
            "--operation-timeout",
            "1",
            "--rearm-budget",
            "32",
            "--rearm-instruction-cap",
            "16",
            "--max-edge-lateness",
            "32",
            "--output",
            "/tmp/unused-milestone-unit-output.json",
            *extra,
        ]
    )


def run_owner(monkeypatch, tmp_path, *, stream=True, milestones=True, terminal=None):
    """Deterministic owner orchestration; only CPU advancement/transport are faked."""
    from scripts import probe_timed_rom_pair as probe

    args = probe_args("--input-profile", "menu", "--connector-chunk", "1", "--frame-limit", "300")
    args.call_retention = "stream" if stream else "inline"
    args.rom_milestones = milestones
    harness = Harness()
    lifecycle, checkpoints, inputs = [], [], []
    game = SimpleNamespace(
        frame_count=10,
        memory=harness.memory,
        register_file=harness.registers,
    )

    def register(*values):
        lifecycle.append("register")
        harness.register(*values)

    def remove(*values):
        lifecycle.append("remove")
        harness.remove(*values)

    game.hook_register, game.hook_deregister = register, remove

    def step(count, **kwargs):
        index = game.frame_count - 10
        lifecycle.append("step")
        if milestones and index in (100, 150, 250):
            harness.fire("YesNoChoice")
        if index == 270 and terminal is not None and terminal != "observation_error":
            if terminal == "interrupted":
                raise RuntimeError("authored owner interruption")
            if terminal == "completed_no_progress":
                return
            game.frame_count += 2
            return
        game.frame_count += count

    session = SimpleNamespace(
        _pyboy=game,
        symbols=harness.symbols,
        load_state=lambda data: lifecycle.append("load"),
        locked=lambda **kwargs: nullcontext(),
        bind_timed_execution=lambda *a, **kw: lifecycle.append("bind"),
        unbind_timed_execution=lambda *a, **kw: lifecycle.append("unbind"),
        close=lambda **kw: lifecycle.append("close"),
        press=lambda button, **kw: inputs.append((button, kw)),
        step=step,
    )
    endpoint = SimpleNamespace(
        metadata={},
        attach=lambda *a, **kw: lifecycle.append("attach"),
        close=lambda: lifecycle.append("endpoint_close"),
        cancel=lambda: None,
    )
    sock = SimpleNamespace(getsockopt=lambda *a: 1, close=lambda: None)
    cancelled = threading.Event()
    record = {"side": "listener", "calls": [], "cleanup": [], "errors": []}
    path = tmp_path / "calls.jsonl"

    def checkpoint(value):
        phase = value["phase"]
        if phase in ("public_tick", "input_queued_before_public_tick", "public_tick_returned"):
            checkpoints.append(json.loads(json.dumps(value)))
        if stream and phase == "public_tick_returned" and "unspooled_call" not in value:
            data = path.read_bytes()
            assert value["call_log"]["bytes"] == len(data)
            assert value["call_log"]["sha256"] == hashlib.sha256(data).hexdigest()

    def observe(*args):
        if terminal == "observation_error" and game.frame_count >= 281:
            raise RuntimeError("authored observation failure")
        return {"frame_count": game.frame_count}

    monkeypatch.setattr(probe, "observe", observe)
    monkeypatch.setattr(helper, "read_menu_snapshot", lambda **kw: {"menu": 0})
    monkeypatch.setattr(helper, "read_input_snapshot", lambda **kw: {"PC": 0x1200})
    probe._run_owner(
        0,
        args,
        [record],
        [sock],
        cancelled,
        SimpleNamespace(set=cancelled.set),
        SimpleNamespace(wait=lambda **kw: None),
        [None],
        threading.Lock(),
        time.monotonic() + 20,
        time.monotonic() + 21,
        lambda *a, **kw: session,
        lambda *a, **kw: endpoint,
        lambda *a: {
            "rom": None,
            "sym": None,
            "pins": {},
            "state": b"authored",
            "family": "blue",
            "provenance": {},
        },
        checkpoint=checkpoint,
        evidence_path=path,
    )
    return record, path, lifecycle, checkpoints


def test_stream_over_240_calls_keeps_every_record_hash_and_milestone_context(monkeypatch, tmp_path):
    record, path, lifecycle, checkpoints = run_owner(monkeypatch, tmp_path)
    assert record["errors"] == []
    assert record["termination"] == "frame_bound"
    raw = path.read_bytes()
    calls = [json.loads(line) for line in raw.splitlines()]
    assert len(calls) == 300
    assert [call["call_index"] for call in calls] == list(range(300))
    assert all(call["actual_completed_frames"] == 1 for call in calls)
    assert record["call_log"] == {
        "path": str(path),
        "bytes": len(raw),
        "record_count": 300,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "complete": True,
        "error": None,
    }
    assert record["call_counts"] == {
        "total": 300,
        "completed": 300,
        "interrupted": 0,
        "completed_no_progress": 0,
        "completed_partial": 0,
        "noncompleted": 0,
        "requested_frames": 300,
        "actual_completed_frames": 300,
        "unknown_actual_calls": 0,
    }
    retained = [call["call_index"] for call in record["calls"]]
    assert retained == [0, 1, 2, 3, 100, 150, 250, 296, 297, 298, 299]
    assert [call["call_index"] for call in calls if call["milestones"]] == [100, 150, 250]
    assert record["milestones"]["counts"]["yes_no"] == 3
    assert [event["completed_so_far"] for event in record["milestones"]["events"]] == [
        110,
        160,
        260,
    ]
    assert lifecycle.index("load") < lifecycle.index("register") < lifecycle.index("attach")
    assert lifecycle.index("unbind") < lifecycle.index("remove") < lifecycle.index("close")
    assert "in_flight" not in record
    pending = [item for item in checkpoints if item["phase"] == "public_tick"]
    assert [item["in_flight"]["call_index"] for item in pending] == list(range(300))
    queued = [item for item in checkpoints if item["phase"] == "input_queued_before_public_tick"]
    assert queued and all(item["in_flight"]["input"]["status"] == "queued" for item in queued)


def test_default_retention_keeps_full_calls_and_installs_no_hooks(monkeypatch, tmp_path):
    baseline = probe_args()
    assert baseline.call_retention == "inline" and baseline.rom_milestones is False
    record, path, lifecycle, _ = run_owner(monkeypatch, tmp_path, stream=False, milestones=False)
    assert record["errors"] == []
    assert len(record["calls"]) == 300
    assert "call_log" not in record and "milestones" not in record
    assert "register" not in lifecycle and not path.exists()


@pytest.mark.parametrize("status", ["interrupted", "completed_no_progress", "completed_partial"])
def test_late_noncompleted_calls_have_exact_counts_and_full_evidence(monkeypatch, tmp_path, status):
    record, path, _, _ = run_owner(monkeypatch, tmp_path, terminal=status)
    calls = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert len(calls) == 271 and calls[-1]["call_index"] == 270
    assert calls[-1]["status"] == status
    assert record["call_counts"]["total"] == 271
    assert record["call_counts"]["completed"] == 270
    assert record["call_counts"][status] == 1
    assert record["call_counts"]["noncompleted"] == 1
    assert record["call_log"]["record_count"] == 271
    assert record["call_counts"]["requested_frames"] == 271
    assert record["call_counts"]["actual_completed_frames"] == (
        272 if status == "completed_partial" else 270
    )
    assert record["call_counts"]["unknown_actual_calls"] == 0


def test_cap_failure_preserves_unspooled_call_and_incomplete_artifact(monkeypatch, tmp_path):
    from scripts import probe_timed_rom_pair as probe

    real_log = probe.CallEvidenceLog
    monkeypatch.setattr(probe, "CallEvidenceLog", lambda path: real_log(path, byte_limit=1200))
    record, path, _, _ = run_owner(monkeypatch, tmp_path)
    assert record["termination"] == "owner_failure" and record["errors"]
    assert len(path.read_bytes()) <= 1200
    assert record["call_log"]["complete"] is False
    assert "cap" in record["call_log"]["error"]
    assert record["unspooled_call"] == record["in_flight"]
    assert record["unspooled_call"] in record["calls"]
    assert record["unspooled_call"]["call_index"] == record["call_log"]["record_count"]
    assert record["call_counts"]["total"] == record["call_log"]["record_count"] + 1


@pytest.mark.parametrize("option", [("--rom-milestones",), ("--call-retention", "stream")])
def test_new_features_reject_nonmenu_in_parser_and_direct_run(option):
    from scripts import probe_timed_rom_pair as probe

    with pytest.raises(SystemExit):
        probe_args(*option)
    args = probe_args()
    if len(option) == 1:
        args.rom_milestones = True
    else:
        args.call_retention = "stream"
    with pytest.raises(ValueError, match="menu"):
        probe.run_pair(args)


class WriteFault:
    def __init__(self, stream, *, fail_after=None, fail_flush=False):
        self.stream = stream
        self.fail_after = fail_after
        self.fail_flush = fail_flush
        self.written = 0

    @property
    def closed(self):
        return self.stream.closed

    def write(self, data):
        if self.fail_after is not None and self.written >= self.fail_after:
            raise OSError("authored write failure")
        size = min(7, len(data))
        if self.fail_after is not None:
            size = min(size, self.fail_after - self.written)
        result = self.stream.write(data[:size])
        self.written += result
        return result

    def flush(self):
        if self.fail_flush:
            raise OSError("authored flush failure")
        self.stream.flush()

    def close(self):
        self.stream.close()


def test_log_short_writes_flush_and_incremental_hash_cover_exact_bytes(tmp_path):
    from scripts.probe_timed_rom_pair import CallEvidenceLog

    path = tmp_path / "short.jsonl"
    log = CallEvidenceLog(path)
    log._stream = WriteFault(log._stream)
    calls = [
        {"call_index": index, "status": "completed", "evidence": "é" * 8} for index in range(301)
    ]
    try:
        for count, call in enumerate(calls, 1):
            log.append(call)
            raw = path.read_bytes()
            state = log.snapshot()
            assert state["bytes"] == len(raw)
            assert state["sha256"] == hashlib.sha256(raw).hexdigest()
            assert state["record_count"] == count
            assert state["complete"] is False
        log.close(complete=True)
        assert log.snapshot()["complete"] is True
        assert [json.loads(line) for line in path.read_bytes().splitlines()] == calls
    finally:
        log._stream.close()


@pytest.mark.parametrize("flush", [False, True])
def test_partial_write_or_flush_failure_keeps_exact_hash_and_incomplete_metadata(tmp_path, flush):
    from scripts.probe_timed_rom_pair import CallEvidenceLog

    path = tmp_path / "partial.jsonl"
    log = CallEvidenceLog(path)
    original = log._stream
    log._stream = WriteFault(original, fail_after=None if flush else 11, fail_flush=flush)
    try:
        with pytest.raises(OSError):
            log.append({"call_index": 0, "payload": "x" * 100})
        raw = path.read_bytes()
        state = log.snapshot()
        assert state["record_count"] == 0 and state["complete"] is False
        assert state["error"]
        assert state["bytes"] == len(raw) > 0
        assert state["sha256"] == hashlib.sha256(raw).hexdigest()
        with pytest.raises(RuntimeError):
            log.append({"call_index": 1})
        assert path.read_bytes() == raw
        log._stream.fail_flush = False
        log.close(complete=True)
        assert log.snapshot()["complete"] is False
    finally:
        original.close()


def test_log_existing_artifact_is_never_overwritten(tmp_path):
    from scripts.probe_timed_rom_pair import CallEvidenceLog

    path = tmp_path / "owned.jsonl"
    path.write_bytes(b"existing evidence")
    with pytest.raises(FileExistsError):
        CallEvidenceLog(path)
    assert path.read_bytes() == b"existing evidence"


@pytest.mark.parametrize("fault", ["noncompleted", "incomplete", "log_error", "forced", "missing"])
def test_main_fails_from_aggregate_or_incomplete_evidence_despite_completed_inline(
    monkeypatch, tmp_path, fault
):
    from scripts import probe_timed_rom_pair as probe

    log = probe.CallEvidenceLog(tmp_path / "main-calls.jsonl")
    log.append({"call_index": 0, "status": "completed"})
    log.close()
    owner = {
        "errors": [],
        "termination": "frame_bound",
        "calls": [{"status": "completed"}],
        "call_counts": {"total": 1, "noncompleted": 0},
        "call_log": log.snapshot(),
    }
    if fault == "noncompleted":
        owner["call_counts"]["noncompleted"] = 1
    elif fault == "incomplete":
        owner["call_log"]["complete"] = False
    elif fault == "log_error":
        owner["call_log"]["error"] = "partial write"
    elif fault == "forced":
        owner["forced_termination"] = True
        owner["call_log"]["complete"] = False
    else:
        owner["actual_missing"] = True
    pair = {"threads_alive": [], "stop_reason": "owner_completion_or_failure", "owners": [owner]}
    monkeypatch.setattr(probe, "run_probe", lambda args: {"pairs": [pair]})
    args = probe_args()
    args.output = tmp_path / "report.json"
    monkeypatch.setattr(probe, "parse_args", lambda argv: args)
    assert probe.main([]) == 2
    assert json.loads(args.output.read_text())["pairs"][0]["owners"][0] == owner


def test_owner_log_open_failure_is_terminal_before_session_creation(monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    path.write_bytes(b"preexisting")
    record, _, lifecycle, _ = run_owner(monkeypatch, tmp_path)
    assert record["termination"] == "owner_failure"
    assert any("FileExistsError" in error for error in record["errors"])
    assert lifecycle == []
    assert path.read_bytes() == b"preexisting"


@pytest.mark.parametrize(
    "fault", [None, "deleted", "truncated", "hash", "count", "total", "incomplete"]
)
def test_final_artifact_validation_rejects_missing_partial_or_inconsistent_log(tmp_path, fault):
    from scripts import probe_timed_rom_pair as probe

    path = tmp_path / "validate.jsonl"
    log = probe.CallEvidenceLog(path)
    log.append({"call_index": 0, "status": "completed"})
    log.close()
    owner = {"call_log": log.snapshot(), "call_counts": {"total": 1}}
    if fault == "deleted":
        path.unlink()
    elif fault == "truncated":
        path.write_bytes(path.read_bytes()[:-1])
    elif fault == "hash":
        owner["call_log"]["sha256"] = "0" * 64
    elif fault == "count":
        owner["call_log"]["record_count"] = 2
    elif fault == "total":
        owner["call_counts"]["total"] = 2
    elif fault == "incomplete":
        owner["call_log"]["complete"] = False
    if fault is None:
        assert probe.validate_call_artifact(owner) is None
    else:
        with pytest.raises((ValueError, OSError)):
            probe.validate_call_artifact(owner)


def test_unknown_actual_progress_is_counted_and_retained_as_interruption(monkeypatch, tmp_path):
    record, path, _, _ = run_owner(monkeypatch, tmp_path, terminal="observation_error")
    calls = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert len(calls) == 271 and calls[-1]["status"] == "interrupted"
    assert "actual_completed_frames" not in calls[-1]
    assert "observation_error" in calls[-1]
    assert record["call_counts"]["unknown_actual_calls"] == 1
    assert record["call_counts"]["actual_completed_frames"] == 270
    assert record["call_counts"]["noncompleted"] == 1
    assert record["termination"] == "owner_failure"


def test_owner_partial_write_failure_retains_unspooled_evidence(monkeypatch, tmp_path):
    from scripts import probe_timed_rom_pair as probe

    real_log = probe.CallEvidenceLog

    def faulty_log(path):
        log = real_log(path)
        log._stream = WriteFault(log._stream, fail_after=1000)
        return log

    monkeypatch.setattr(probe, "CallEvidenceLog", faulty_log)
    record, path, _, _ = run_owner(monkeypatch, tmp_path)
    raw = path.read_bytes()
    assert record["termination"] == "owner_failure"
    assert record["call_log"]["complete"] is False
    assert record["call_log"]["bytes"] == len(raw) == 1000
    assert record["call_log"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert record["unspooled_call"] == record["in_flight"]
    assert record["unspooled_call"] in record["calls"]
    assert record["call_counts"]["total"] == record["call_log"]["record_count"] + 1
