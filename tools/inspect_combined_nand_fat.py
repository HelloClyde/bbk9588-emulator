#!/usr/bin/env python3
"""Inspect the FAT16 area embedded in the BBK9588 combined NAND image."""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


PAGE_SIZE = 2048
SPARE_SIZE = 64


def ascii_name(text: str) -> str:
    return text.encode("unicode_escape").decode("ascii")


class Fat16View:
    def __init__(self, data: bytes, volume_lba: int = 0x20):
        self.data = data
        self.volume_lba = volume_lba
        boot = data[volume_lba * 512 : volume_lba * 512 + 512]
        if len(boot) < 512 or boot[510:512] != b"\x55\xaa":
            raise ValueError(f"missing FAT boot signature at LBA 0x{volume_lba:x}")
        self.bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
        self.sectors_per_cluster = boot[13]
        self.reserved = struct.unpack_from("<H", boot, 14)[0]
        self.fat_count = boot[16]
        self.root_entries = struct.unpack_from("<H", boot, 17)[0]
        self.sectors_per_fat = struct.unpack_from("<H", boot, 22)[0]
        self.hidden = struct.unpack_from("<I", boot, 28)[0]
        if self.bytes_per_sector != 512:
            raise ValueError(f"unsupported sector size {self.bytes_per_sector}")
        self.root_lba = self.hidden + self.reserved + self.fat_count * self.sectors_per_fat
        self.root_sectors = (self.root_entries * 32 + 511) // 512
        self.first_data_lba = self.root_lba + self.root_sectors
        fat_start = (self.hidden + self.reserved) * 512
        self.fat = data[fat_start : fat_start + self.sectors_per_fat * 512]

    def cluster_bytes(self, cluster: int) -> bytes:
        out = bytearray()
        seen: set[int] = set()
        while 2 <= cluster < 0xFFF8 and cluster not in seen:
            seen.add(cluster)
            off = (self.first_data_lba + (cluster - 2) * self.sectors_per_cluster) * 512
            out += self.data[off : off + self.sectors_per_cluster * 512]
            cluster = struct.unpack_from("<H", self.fat, cluster * 2)[0]
        return bytes(out)

    @staticmethod
    def _decode_lfns(entries: list[bytes]) -> str | None:
        if not entries:
            return None
        units: list[int] = []
        for entry in reversed(entries):
            for pos in (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30):
                value = struct.unpack_from("<H", entry, pos)[0]
                if value in (0, 0xFFFF):
                    continue
                units.append(value)
        raw = bytes(byte for unit in units for byte in (unit & 0xFF, unit >> 8))
        return raw.decode("utf-16le", errors="replace")

    @staticmethod
    def _short_name(raw: bytes) -> str:
        stem = raw[:8].decode("gbk", errors="replace").rstrip()
        ext = raw[8:11].decode("ascii", errors="replace").rstrip()
        return stem + (("." + ext) if ext else "")

    def parse_dir(self, data: bytes) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        lfns: list[bytes] = []
        for offset in range(0, len(data), 32):
            entry = data[offset : offset + 32]
            if len(entry) < 32 or entry[0] == 0:
                break
            if entry[0] == 0xE5:
                lfns.clear()
                continue
            if entry[11] == 0x0F:
                lfns.append(entry)
                continue
            raw = entry[:11]
            short = self._short_name(raw)
            name = self._decode_lfns(lfns) or short
            lfns.clear()
            out.append(
                {
                    "name": name,
                    "short": short,
                    "raw": raw,
                    "attr": entry[11],
                    "cluster": struct.unpack_from("<H", entry, 26)[0],
                    "size": struct.unpack_from("<I", entry, 28)[0],
                }
            )
        return out

    def root(self) -> list[dict[str, object]]:
        start = self.root_lba * 512
        return self.parse_dir(self.data[start : start + self.root_sectors * 512])

    @staticmethod
    def _matches(entry: dict[str, object], part: str) -> bool:
        name = str(entry["name"])
        short = str(entry["short"])
        if name.lower() == part.lower() or short.lower() == part.lower():
            return True
        part_raw = part.encode("gbk", errors="replace").upper().rstrip()
        raw = bytes(entry["raw"]).upper().rstrip()
        return raw == part_raw

    def find(self, parts: list[str]) -> dict[str, object] | None:
        current = self.root()
        match: dict[str, object] | None = None
        for part in parts:
            match = next((entry for entry in current if self._matches(entry, part)), None)
            if match is None:
                return None
            if part != parts[-1]:
                current = self.parse_dir(self.cluster_bytes(int(match["cluster"])))
        return match

    def read_file(self, entry: dict[str, object]) -> bytes:
        return self.cluster_bytes(int(entry["cluster"]))[: int(entry["size"])]


def extract_fat_from_nand(path: Path, fat_page_base: int, page_size: int, spare_size: int) -> bytes:
    nand = path.read_bytes()
    stride = page_size + spare_size
    if len(nand) < fat_page_base * stride:
        raise ValueError("fat page base is beyond the image")
    out = bytearray()
    for page in range(fat_page_base, len(nand) // stride):
        off = page * stride
        out += nand[off : off + page_size]
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("nand", type=Path)
    ap.add_argument("--fat-page-base", type=lambda text: int(text, 0), default=0x1C40)
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE)
    ap.add_argument("--spare-size", type=int, default=SPARE_SIZE)
    ap.add_argument("--check", action="append", default=[])
    ns = ap.parse_args()

    fat = extract_fat_from_nand(ns.nand, ns.fat_page_base, ns.page_size, ns.spare_size)
    view = Fat16View(fat)
    print(
        f"FAT16 boot ok: hidden=0x{view.hidden:x} root_lba=0x{view.root_lba:x} "
        f"spc={view.sectors_per_cluster} spf={view.sectors_per_fat}"
    )
    print("root:")
    for entry in view.root():
        print(
            f"  {ascii_name(str(entry['name']))} short={ascii_name(str(entry['short']))} "
            f"cluster={entry['cluster']} size={entry['size']}"
        )

    for spec in ns.check:
        parts = [part for part in spec.replace("/", "\\").split("\\") if part and part != "A:"]
        entry = view.find(parts)
        if entry is None:
            print(f"CHECK missing {ascii_name(spec)}")
            continue
        data = view.read_file(entry)
        print(
            f"CHECK ok {ascii_name(spec)} name={ascii_name(str(entry['name']))} "
            f"short={ascii_name(str(entry['short']))} cluster={entry['cluster']} "
            f"size={len(data)} magic={data[:16].hex()}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
