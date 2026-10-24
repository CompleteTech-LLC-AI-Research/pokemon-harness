"""Apply the Pokémon Red Full Color Hack (vanilla) IPS to a stock ROM.

The patch is distributed as an IPS file from romhacking.net/hacks/1385/
(BYO-patch, same as BYO-ROM). Given the stock ROM and the `.ips`, this
writes a colorized `.gb` and verifies its SHA-1 against the pin in
VERSIONS.md.

Usage:
  python scripts/apply_color_patch.py <stock_rom.gb> <patch.ips> <output.gb>
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

EXPECTED_COLOR_SHA1 = "e1deed63080bc24cad5fba18ecb3184f905d16d4"


def apply_ips(rom: bytes, ips: bytes) -> bytes:
    if ips[:5] != b"PATCH":
        raise ValueError("not an IPS file (missing PATCH header)")
    out = bytearray(rom)
    i = 5
    records = 0
    while True:
        if ips[i : i + 3] == b"EOF":
            i += 3
            break
        off = int.from_bytes(ips[i : i + 3], "big")
        i += 3
        size = int.from_bytes(ips[i : i + 2], "big")
        i += 2
        if size == 0:
            rle_size = int.from_bytes(ips[i : i + 2], "big")
            i += 2
            val = ips[i]
            i += 1
            end = off + rle_size
            if end > len(out):
                out.extend(b"\x00" * (end - len(out)))
            for k in range(rle_size):
                out[off + k] = val
        else:
            data = ips[i : i + size]
            i += size
            end = off + size
            if end > len(out):
                out.extend(b"\x00" * (end - len(out)))
            out[off : off + size] = data
        records += 1
    # Optional truncate suffix (3 trailing bytes after EOF).
    if i + 3 == len(ips):
        new_len = int.from_bytes(ips[i : i + 3], "big")
        out = out[:new_len]
    print(f"applied {records} IPS records", file=sys.stderr)
    return bytes(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("rom", help="stock Pokémon Red .gb")
    p.add_argument("ips", help="pokered_color_vanilla.ips")
    p.add_argument("out", help="destination .gb (will be overwritten)")
    p.add_argument("--expected-sha1", default=EXPECTED_COLOR_SHA1,
                   help="expected SHA-1 of the patched output")
    args = p.parse_args()

    rom = Path(args.rom).read_bytes()
    ips = Path(args.ips).read_bytes()
    patched = bytearray(apply_ips(rom, ips))
    # Some vanilla IPS hacks (e.g. pokeblue_color_vanilla) rewrite title-region
    # bytes (CGB flag at 0x143, SGB flag at 0x146) but ship a stale header
    # checksum at 0x14D, which PyBoy rejects. Recompute both header and global
    # checksums from the final patched data so any emulator will accept it.
    s = 0
    for i in range(0x134, 0x14D):
        s = (s - patched[i] - 1) & 0xff
    patched[0x14D] = s
    gs = sum(patched) & 0xffff
    gs = (gs - patched[0x14E] - patched[0x14F]) & 0xffff
    patched[0x14E] = (gs >> 8) & 0xff
    patched[0x14F] = gs & 0xff
    patched = bytes(patched)
    sha = hashlib.sha1(patched).hexdigest()
    Path(args.out).write_bytes(patched)
    print(f"wrote {args.out} ({len(patched)} bytes) sha1={sha}", file=sys.stderr)
    if sha != args.expected_sha1:
        print(f"WARN: SHA-1 mismatch, expected {args.expected_sha1}", file=sys.stderr)
        return 1
    print("SHA-1 matches pin.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
