"""Bounded real-PyBoy hook regression, using only authored cartridge/boot bytes.

Run with the selected source or native interpreter. No source loader override,
commercial assets, fake CPU progress, or native build/install is used here.
"""

import importlib
import importlib.machinery
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

import pytest

DEADLINE_SECONDS = 20
OUTPUT_LIMIT = 32 * 1024
RESULT_PREFIX = "HOOK_RESULT="


def _runtime_identity():
    modules = {}
    for name in ("pyboy.pyboy", "pyboy.core.mb", "pyboy.core.cpu"):
        module = importlib.import_module(name)
        path = str(Path(module.__file__).resolve())
        native = any(path.endswith(s) for s in importlib.machinery.EXTENSION_SUFFIXES)
        assert native or path.endswith(".py"), path
        modules[name] = {"path": path, "native": native}
    assert len({m["native"] for m in modules.values()}) == 1, modules
    return modules


def _probe(case, directory):
    from pyboy import PyBoy
    from pyboy.core import cpu, mb

    identity = _runtime_identity()
    # Original LR35902 program: initialize SP, then unmap boot at PC=$0100.
    boot = bytearray(256)
    boot[:6] = bytes([0x31, 0x00, 0xD0, 0xC3, 0xFC, 0x00])
    boot[252:] = bytes([0x3E, 0x01, 0xE0, 0x50])
    rom = bytearray(0x8000)
    rom[0x100:0x103] = bytes([0xC3, 0x50, 0x01])
    rom[0x134:0x13D] = b"HOOKCHECK"
    # DI; LD A,$91; LDH ($40),A; NOP; JR back to NOP. LCD runs normally.
    rom[0x150:0x158] = bytes([0xF3, 0x3E, 0x91, 0xE0, 0x40, 0x00, 0x18, 0xFD])
    rom[0x14D] = (-sum(rom[0x134:0x14D]) - 25) & 0xFF
    rom_path, boot_path = directory / "hooks.gb", directory / "hooks.boot"
    rom_path.write_bytes(rom)
    boot_path.write_bytes(boot)
    game = PyBoy(str(rom_path), bootrom=str(boot_path), window="null", sound_emulated=False)
    try:
        assert type(game) is importlib.import_module("pyboy.pyboy").PyBoy
        assert type(game.mb) is mb.Motherboard
        assert type(game.mb.cpu) is cpu.CPU
        game.set_emulation_speed(0)

        def state():
            return (game.mb.cpu.cycles, game.register_file.PC, game.frame_count)

        initial = state()
        assert game._handle_hooks() is False
        assert state() == initial
        calls = []
        later = case.endswith("_later")
        error = {"runtime": RuntimeError, "interrupt": KeyboardInterrupt}.get(case.split("_")[0])
        original = error("authored hook sentinel") if error else None
        context = object()

        def callback(received):
            assert received is context
            if later and game.frame_count == initial[2]:
                return
            calls.append(state())
            if original is not None:
                raise original

        if case != "no_hook":
            # Only visited once; the steady-state loop begins at $0155.
            game.hook_register(0, 0x155 if later else 0x150, callback, context)
        if original is not None:
            try:
                game.tick(3, render=False, sound=False)
            except BaseException as caught:  # noqa: BLE001 - assert KeyboardInterrupt identity too
                assert caught is original, (type(caught), str(caught))
                assert len(calls) == 1
                assert state() == calls[0], (state(), calls)
                assert calls[0][0] > initial[0]  # Real boot/JP CPU work survived.
                assert calls[0][1:] == (0x155 if later else 0x150, initial[2] + int(later))
                assert not game.mb.lcd.frame_done
            else:
                raise AssertionError("tick swallowed the hook exception")
        else:
            assert game.tick(3, render=False, sound=False) == 1
            assert game.frame_count == initial[2] + 3
            assert game.mb.cpu.cycles > initial[0]
            assert game.mb.lcd.frame_done
            assert game.register_file.PC in (0x155, 0x156)
            assert len(calls) == (0 if case == "no_hook" else 1)
            if calls:
                assert calls[0][0] > initial[0]
                assert calls[0][1:] == (0x150, initial[2])
        return {
            "case": case,
            "runtime": identity,
            "initial": initial,
            "final": state(),
            "calls": calls,
        }
    finally:
        game.stop(save=False)


def _bounded_child(case, directory):
    # Preserve the invoking interpreter's import selection (including a gate's
    # explicit paths), without prepending the vendor tree or forcing source.
    command = [
        sys.executable,
        "-B",
        "-u",
        "-c",
        (
            "import runpy,sys; sys.path[:]=__import__('json').loads(sys.argv[1]); "
            "runpy.run_path(sys.argv[2], run_name='__main__')"
        ),
        json.dumps(sys.path),
        str(Path(__file__).resolve()),
        case,
        str(directory),
    ]
    output = bytearray()
    deadline = time.monotonic() + DEADLINE_SECONDS
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                assert remaining > 0, "hook child exceeded hard deadline"
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, min(4096, OUTPUT_LIMIT - len(output) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    assert len(output) + len(chunk) <= OUTPUT_LIMIT, (
                        "hook child output limit exceeded"
                    )
                    output.extend(chunk)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        assert process.returncode == 0, f"hook child exit={process.returncode}"
    except (AssertionError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"{exc}\n{output.decode(errors='replace')}")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
    lines = output.decode().splitlines()
    results = [
        json.loads(line[len(RESULT_PREFIX) :]) for line in lines if line.startswith(RESULT_PREFIX)
    ]
    assert len(results) == 1, lines
    return results[0]


@pytest.mark.parametrize(
    "case", ["runtime", "interrupt", "runtime_later", "interrupt_later", "nonraising", "no_hook"]
)
def test_real_hook_tick_semantics(case, tmp_path):
    expected = _runtime_identity()
    result = _bounded_child(case, tmp_path)
    assert result["case"] == case
    assert result["runtime"] == expected  # Native cannot silently become source.
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    print(RESULT_PREFIX + json.dumps(_probe(sys.argv[3], Path(sys.argv[4]))), flush=True)
