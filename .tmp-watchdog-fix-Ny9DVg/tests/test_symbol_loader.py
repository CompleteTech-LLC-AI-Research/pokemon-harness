"""Tests for the RGBDS .sym loader and typed memory readers.

Uses a hand-crafted synthetic .sym resembling pret/pokered output so the
test suite has no dependency on a real built ROM. Address choices match
real pokered conventions (wCurMap at $D35E, etc.) but the parser itself is
game-agnostic.
"""

from __future__ import annotations

from pokered_harness.symbols.loader import (
    Symbol,
    SymbolTable,
    load_sym_file,
    load_sym_text,
)

SYNTHETIC_SYM = """\
; RGBDS symbol file (synthetic, for tests)
00:D35E wCurMap
00:D361 wYCoord
00:D362 wXCoord
00:D057 wIsInBattle
00:D16C wPartyMon1HP        ; big-endian 2-byte HP
00:D347 wObtainedBadges
00:FF8C hJoyHeld             ; HRAM, one byte
; trailing comment line

00:D057 wIsInBattleDupe     ; duplicate address — both names should map back
"""


class DictMemory:
    """Minimal MemoryLike backing store: dict[int, int] supporting slices."""

    def __init__(self, initial: dict[int, int] | None = None) -> None:
        self._m: dict[int, int] = dict(initial or {})

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.start, key.stop, key.step or 1
            return [self._m.get(a, 0) for a in range(start, stop, step)]
        return self._m.get(int(key), 0)

    def __setitem__(self, key: int, value: int) -> None:
        self._m[int(key)] = int(value) & 0xFF


def test_parses_all_real_symbol_lines():
    t = load_sym_text(SYNTHETIC_SYM)
    # 8 symbol lines defined above (wIsInBattleDupe included).
    assert len(t) == 8
    assert "wCurMap" in t
    assert t["wCurMap"].addr == 0xD35E
    assert t["wCurMap"].bank == 0


def test_skips_comments_and_blank_lines():
    t = load_sym_text(
        """
        ; header comment
        ; another comment

        00:D35E wCurMap
        not-a-valid-line-should-be-ignored
        00:D361 wYCoord
        """
    )
    assert {s.name for s in t} == {"wCurMap", "wYCoord"}


def test_trailing_inline_comment_is_stripped():
    t = load_sym_text("00:D16C wPartyMon1HP ; big-endian 2-byte HP\n")
    assert t["wPartyMon1HP"].addr == 0xD16C


def test_duplicate_address_reverse_lookup():
    t = load_sym_text(SYNTHETIC_SYM)
    names = t.names_at(bank=0, addr=0xD057)
    assert set(names) == {"wIsInBattle", "wIsInBattleDupe"}


def test_duplicate_name_last_wins():
    t = load_sym_text(
        """
        00:D000 foo
        00:D100 foo
        """
    )
    assert t["foo"].addr == 0xD100


def test_missing_symbol_raises_keyerror():
    t = load_sym_text("00:D35E wCurMap\n")
    try:
        _ = t["wMissing"]
    except KeyError as e:
        assert "wMissing" in str(e)
    else:
        raise AssertionError("expected KeyError for unknown symbol")


def test_get_returns_none_for_missing():
    t = load_sym_text("00:D35E wCurMap\n")
    assert t.get("wMissing") is None
    assert t.get("wCurMap") is not None


def test_read_u8_and_bit():
    t = load_sym_text(SYNTHETIC_SYM)
    mem = DictMemory({0xD347: 0b0000_0101})  # badges: Boulder + Thunder
    assert t.read_u8(mem, "wObtainedBadges") == 0x05
    assert t.read_bit(mem, "wObtainedBadges", 0) is True
    assert t.read_bit(mem, "wObtainedBadges", 1) is False
    assert t.read_bit(mem, "wObtainedBadges", 2) is True
    assert t.read_bit(mem, "wObtainedBadges", 7) is False


def test_read_u16_le_vs_be():
    t = load_sym_text(SYNTHETIC_SYM)
    mem = DictMemory({0xD16C: 0x01, 0xD16D: 0x2C})
    # Little-endian interpretation: lo=$01, hi=$2C → 0x2C01
    assert t.read_u16_le(mem, "wPartyMon1HP") == 0x2C01
    # Big-endian (pokered HP convention): hi=$01, lo=$2C → 0x012C = 300
    assert t.read_u16_be(mem, "wPartyMon1HP") == 0x012C
    assert t.read_u16_be(mem, "wPartyMon1HP") == 300


def test_read_bytes_respects_length():
    t = load_sym_text("00:D000 wBlob\n")
    mem = DictMemory({0xD000: 0xAA, 0xD001: 0xBB, 0xD002: 0xCC, 0xD003: 0xDD})
    assert t.read_bytes(mem, "wBlob", 4) == bytes([0xAA, 0xBB, 0xCC, 0xDD])


def test_bit_out_of_range_raises():
    t = load_sym_text("00:D000 wFlag\n")
    mem = DictMemory()
    try:
        t.read_bit(mem, "wFlag", 8)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for bit=8")


def test_load_sym_file_roundtrip(tmp_path):
    p = tmp_path / "synthetic.sym"
    p.write_text(SYNTHETIC_SYM, encoding="utf-8")
    t = load_sym_file(p)
    assert t["wCurMap"].addr == 0xD35E
    assert t["hJoyHeld"].addr == 0xFF8C


def test_symboltable_iterable_and_contains():
    t = SymbolTable([Symbol("a", 0, 0xD000), Symbol("b", 0, 0xD001)])
    assert "a" in t
    assert "c" not in t
    assert 42 not in t  # type: ignore[comparison-overlap]
    assert {s.name for s in t} == {"a", "b"}


def test_bank_addr_helper():
    t = load_sym_text("01:4A12 SomeBankedRoutine\n")
    assert t.bank_addr("SomeBankedRoutine") == (1, 0x4A12)
