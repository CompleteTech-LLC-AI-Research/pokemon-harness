"""Small observation-fixture objects shared by timed-menu milestone tests.

This module owns setup data and helpers; collected tests and owner/runpy checks
remain in tests.test_timed_menu_milestones.
"""

import threading

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


def leaves(exc):
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for child in exc.exceptions for leaf in leaves(child)]
    return [exc]


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
