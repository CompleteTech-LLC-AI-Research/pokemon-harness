"""RGBDS `.sym` file loader and typed memory accessors.

A `pokered.sym` file (produced by `pret/pokered` built with `make DEBUG=1`)
lists one symbol per line in the form::

    BB:AAAA SymbolName

where ``BB`` is the bank in hex and ``AAAA`` is the 16-bit address in hex.
Comments start with ``;``. Blank lines are permitted.

The loader is deliberately tolerant: it skips any line it cannot parse
rather than raising, because RGBDS occasionally emits directive-like header
lines and the exact set of those lines varies across toolchain versions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Protocol


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    bank: int
    addr: int

    def __repr__(self) -> str:
        return f"Symbol({self.name!r}, bank=0x{self.bank:02x}, addr=0x{self.addr:04x})"


class MemoryLike(Protocol):
    """Minimal subset of PyBoy's ``memory`` interface.

    ``pyboy.memory[addr]`` returns an int; ``pyboy.memory[a:b]`` returns a
    slice of ints. The protocol keeps the loader testable against a plain
    dict/bytearray backing store.
    """

    def __getitem__(self, key: int | slice) -> int | Iterable[int]: ...


_SYM_LINE = re.compile(
    r"""
    ^\s*
    ([0-9A-Fa-f]{1,4})  # bank
    :
    ([0-9A-Fa-f]{4})    # address
    \s+
    (\S+)               # symbol name
    \s*(?:;.*)?$        # optional trailing comment
    """,
    re.VERBOSE,
)


class SymbolTable:
    """Name → :class:`Symbol` mapping with typed memory readers.

    All read_* helpers assume WRAM/HRAM addresses that live in CPU space, so
    the bank is implicit to the memory backend. ROM- or SRAM-banked reads
    need an explicit bank switch at the PyBoy layer — that's intentionally
    not this class's job in v1.
    """

    def __init__(self, symbols: Iterable[Symbol]) -> None:
        self._by_name: dict[str, Symbol] = {}
        self._by_addr: dict[tuple[int, int], list[str]] = {}
        for sym in symbols:
            # Last-write-wins on duplicate names — matches PyBoy's own
            # symbol_lookup semantics when multiple .sym files are loaded.
            self._by_name[sym.name] = sym
            self._by_addr.setdefault((sym.bank, sym.addr), []).append(sym.name)

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name

    def __iter__(self) -> Iterator[Symbol]:
        return iter(self._by_name.values())

    def __getitem__(self, name: str) -> Symbol:
        try:
            return self._by_name[name]
        except KeyError as e:
            raise KeyError(f"unknown symbol: {name!r}") from e

    def get(self, name: str) -> Symbol | None:
        return self._by_name.get(name)

    def addr_of(self, name: str) -> int:
        return self[name].addr

    def bank_addr(self, name: str) -> tuple[int, int]:
        s = self[name]
        return s.bank, s.addr

    def names_at(self, bank: int, addr: int) -> list[str]:
        return list(self._by_addr.get((bank, addr), ()))

    # --- typed reads (WRAM/HRAM CPU-space) -------------------------------

    def read_u8(self, memory: MemoryLike, name: str) -> int:
        value = memory[self.addr_of(name)]
        return int(value) & 0xFF

    def read_bytes(self, memory: MemoryLike, name: str, length: int) -> bytes:
        if length <= 0:
            raise ValueError(f"length must be positive, got {length}")
        addr = self.addr_of(name)
        raw = memory[addr : addr + length]
        return bytes(raw)  # type: ignore[arg-type]

    def read_u16_le(self, memory: MemoryLike, name: str) -> int:
        lo, hi = self.read_bytes(memory, name, 2)
        return lo | (hi << 8)

    def read_u16_be(self, memory: MemoryLike, name: str) -> int:
        """Big-endian 2-byte read.

        Pokémon Red stores most multi-byte fields (HP, experience, stats)
        big-endian inside party/box/battle structs, despite the Game Boy
        CPU being little-endian. Prefer this reader for game-state fields.
        """
        hi, lo = self.read_bytes(memory, name, 2)
        return (hi << 8) | lo

    def read_bit(self, memory: MemoryLike, name: str, bit: int) -> bool:
        if not 0 <= bit <= 7:
            raise ValueError(f"bit must be in 0..7, got {bit}")
        return bool((self.read_u8(memory, name) >> bit) & 1)


def load_sym_file(path: str | Path) -> SymbolTable:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return SymbolTable(_parse_sym_lines(f))


def load_sym_text(text: str) -> SymbolTable:
    return SymbolTable(_parse_sym_lines(text.splitlines()))


def _parse_sym_lines(lines: Iterable[str]) -> Iterator[Symbol]:
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        m = _SYM_LINE.match(line)
        if not m:
            continue
        bank_hex, addr_hex, name = m.groups()
        yield Symbol(name=name, bank=int(bank_hex, 16), addr=int(addr_hex, 16))
