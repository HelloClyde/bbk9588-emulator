#!/usr/bin/env python3
"""Build a simple FAT16 disk image from host directories.

The BBK 9588 firmware refers to paths such as A:\\绯荤粺\\鏁版嵁\\Config.inf.
This builder keeps those names as FAT long-file-name entries and gives every
entry a deterministic ASCII 8.3 alias for compatibility.
"""

from __future__ import annotations

import argparse
import math
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


BYTES_PER_SECTOR = 512
DEFAULT_SECTORS_PER_CLUSTER = 32
RESERVED_SECTORS = 1
FAT_COPIES = 2
ROOT_ENTRIES = 512
MEDIA_DESCRIPTOR = 0xF8
END_CLUSTER = 0xFFFF
DEFAULT_PREFIX_BYTES = 0
DEFAULT_PARTITION_OFFSET_SECTORS = 32
DEFAULT_FREE_CLUSTERS = 4096
DEFAULT_VOLUME_SECTORS = 0xF7AE0


@dataclass
class FsNode:
    name: str
    source: Path | None
    is_dir: bool
    size: int = 0
    short_name: bytes = b""
    first_cluster: int = 0
    parent_cluster: int = 0
    children: list["FsNode"] = field(default_factory=list)


def align_up(value: int, align: int) -> int:
    return (value + align - 1) // align * align


def fat_date_time() -> tuple[int, int]:
    # 2026-01-01 00:00:00, enough for deterministic images.
    date = ((2026 - 1980) << 9) | (1 << 5) | 1
    time = 0
    return date, time


def sanitize_short_part(text: str, fallback: str) -> str:
    text = text.upper()
    text = re.sub(r"[^A-Z0-9$%'-_@~`!(){}^#&]", "_", text)
    text = text.strip(" .")
    return text or fallback


def make_gbk_short_name(name: str, is_dir: bool) -> bytes | None:
    stem, dot, suffix = name.rpartition(".")
    if not dot or is_dir:
        stem, suffix = name, ""
    try:
        stem_bytes = stem.upper().encode("gbk")
        suffix_bytes = suffix.upper().encode("ascii") if suffix else b""
    except UnicodeEncodeError:
        return None

    invalid = set(b' "+,./:;<=>?[\\]|*')
    all_bytes = stem_bytes + suffix_bytes
    if (
        not stem_bytes
        or len(stem_bytes) > 8
        or len(suffix_bytes) > 3
        or not any(byte >= 0x80 for byte in stem_bytes)
        or any(byte < 0x20 or byte in invalid for byte in all_bytes)
    ):
        return None
    return stem_bytes.ljust(8, b" ") + suffix_bytes.ljust(3, b" ")


def make_short_name(name: str, used: set[bytes], is_dir: bool, index: int) -> bytes:
    gbk_plain = make_gbk_short_name(name, is_dir)
    if gbk_plain is not None and gbk_plain not in used:
        used.add(gbk_plain)
        return gbk_plain

    stem, dot, suffix = name.rpartition(".")
    if not dot or is_dir:
        stem, suffix = name, ""
    base = sanitize_short_part(stem, "DIR" if is_dir else "FILE")
    ext = sanitize_short_part(suffix, "")[:3]

    plain = (base[:8].ljust(8) + ext.ljust(3)).encode("ascii")
    valid_plain = name.upper() == (base[:8] + (("." + ext) if ext else ""))
    if valid_plain and plain not in used:
        used.add(plain)
        return plain

    for serial in range(1, 1000):
        tail = f"~{serial}"
        keep = max(1, 8 - len(tail))
        alias_base = (base[:keep] + tail)[:8]
        candidate = (alias_base.ljust(8) + ext.ljust(3)).encode("ascii")
        if candidate not in used:
            used.add(candidate)
            return candidate

    prefix = "D" if is_dir else "F"
    for serial in range(index, index + 100000):
        alias = f"{prefix}{serial:06d}"
        candidate = (alias[:8].ljust(8) + ext.ljust(3)).encode("ascii")
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise RuntimeError(f"unable to allocate short name for {name!r}")


def assign_short_names(node: FsNode) -> None:
    used: set[bytes] = set()
    for idx, child in enumerate(sorted(node.children, key=lambda item: item.name.lower()), 1):
        child.short_name = make_short_name(child.name, used, child.is_dir, idx)
        if child.is_dir:
            assign_short_names(child)


def build_tree(paths: list[Path]) -> FsNode:
    root = FsNode(name="", source=None, is_dir=True)
    for source in paths:
        source = source.resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        node = read_node(source, source.name)
        root.children.append(node)
    add_bbk_compat_aliases(root)
    assign_short_names(root)
    return root


def find_child_dir(node: FsNode, name: str) -> FsNode | None:
    for child in node.children:
        if child.is_dir and child.name == name:
            return child
    return None


def find_child(node: FsNode, name: str) -> FsNode | None:
    for child in node.children:
        if child.name == name:
            return child
    return None


def add_systp_compat_alias(root: FsNode) -> None:
    system = find_child_dir(root, "系统")
    if system is None or find_child(system, "SysTp.cfg") is not None:
        return
    data_dir = find_child_dir(system, "数据")
    if data_dir is None:
        return
    systp = find_child(data_dir, "SysTp.cfg")
    if systp is None or systp.is_dir or systp.source is None:
        return
    system.children.append(
        FsNode(
            name="SysTp.cfg",
            source=systp.source,
            is_dir=False,
            size=systp.size,
        )
    )


def add_bbk_compat_aliases(root: FsNode) -> None:
    add_systp_compat_alias(root)


def read_node(path: Path, name: str) -> FsNode:
    if path.is_dir():
        node = FsNode(name=name, source=path, is_dir=True)
        entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        node.children = [read_node(child, child.name) for child in entries]
        return node
    return FsNode(name=name, source=path, is_dir=False, size=path.stat().st_size)


def iter_nodes(node: FsNode) -> list[FsNode]:
    out: list[FsNode] = []
    for child in node.children:
        out.append(child)
        if child.is_dir:
            out.extend(iter_nodes(child))
    return out


def lfn_checksum(short_name: bytes) -> int:
    total = 0
    for byte in short_name:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


def utf16_units(name: str) -> list[int]:
    raw = name.encode("utf-16le")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def make_lfn_entries(name: str, checksum: int) -> list[bytes]:
    units = utf16_units(name)
    chunks = [units[i : i + 13] for i in range(0, len(units), 13)] or [[]]
    entries: list[bytes] = []
    for disk_index, chunk in enumerate(reversed(chunks), 1):
        sequence = len(chunks) - disk_index + 1
        if disk_index == 1:
            sequence |= 0x40
        padded = list(chunk)
        if len(padded) < 13:
            padded.append(0)
        while len(padded) < 13:
            padded.append(0xFFFF)
        entry = bytearray(32)
        entry[0] = sequence
        entry[11] = 0x0F
        entry[12] = 0
        entry[13] = checksum
        entry[26:28] = b"\x00\x00"
        positions = [1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30]
        for pos, value in zip(positions, padded):
            struct.pack_into("<H", entry, pos, value)
        entries.append(bytes(entry))
    return entries


def make_short_entry(node: FsNode, parent_cluster: int = 0) -> bytes:
    entry = bytearray(32)
    entry[0:11] = node.short_name
    entry[11] = 0x10 if node.is_dir else 0x20
    date, time = fat_date_time()
    for off in (14, 22):
        struct.pack_into("<H", entry, off, time)
    for off in (16, 18, 24):
        struct.pack_into("<H", entry, off, date)
    struct.pack_into("<H", entry, 26, node.first_cluster if node.is_dir or node.size else 0)
    struct.pack_into("<I", entry, 28, 0 if node.is_dir else node.size)
    return bytes(entry)


def make_dot_entry(name: bytes, target_cluster: int) -> bytes:
    node = FsNode(name="", source=None, is_dir=True, short_name=name.ljust(11), first_cluster=target_cluster)
    return make_short_entry(node)


def directory_entries(node: FsNode, parent_cluster: int) -> bytes:
    entries: list[bytes] = []
    if node.first_cluster:
        entries.append(make_dot_entry(b".", node.first_cluster))
        entries.append(make_dot_entry(b"..", parent_cluster))
    for child in sorted(node.children, key=lambda item: item.name.lower()):
        checksum = lfn_checksum(child.short_name)
        entries.extend(make_lfn_entries(child.name, checksum))
        entries.append(make_short_entry(child))
    data = b"".join(entries)
    return data + b"\x00" * 32


def cluster_count_for_size(size: int, sectors_per_cluster: int) -> int:
    if size <= 0:
        return 0
    return math.ceil(size / (BYTES_PER_SECTOR * sectors_per_cluster))


def assign_clusters(root: FsNode, sectors_per_cluster: int) -> int:
    next_cluster = 2
    for node in iter_nodes(root):
        if node.is_dir:
            # First pass estimate; LFN counts are already fixed.
            size = len(directory_entries(node, 0))
        else:
            size = node.size
        count = max(1, cluster_count_for_size(size, sectors_per_cluster)) if node.is_dir or node.size else 0
        if count:
            node.first_cluster = next_cluster
            next_cluster += count
    assign_parent_clusters(root, 0)
    return next_cluster


def assign_parent_clusters(node: FsNode, parent_cluster: int) -> None:
    for child in node.children:
        child.parent_cluster = parent_cluster
        if child.is_dir:
            assign_parent_clusters(child, child.first_cluster)


def build_fat(root: FsNode, cluster_count: int, sectors_per_cluster: int) -> bytearray:
    fat = bytearray((cluster_count + 2) * 2)
    struct.pack_into("<H", fat, 0, MEDIA_DESCRIPTOR | 0xFF00)
    struct.pack_into("<H", fat, 2, END_CLUSTER)
    for node in iter_nodes(root):
        count = cluster_count_for_size(node.size, sectors_per_cluster)
        if node.is_dir:
            count = max(1, cluster_count_for_size(len(directory_entries(node, node.parent_cluster)), sectors_per_cluster))
        if not count:
            continue
        for i in range(count):
            value = END_CLUSTER if i == count - 1 else node.first_cluster + i + 1
            struct.pack_into("<H", fat, (node.first_cluster + i) * 2, value)
    return fat


def write_cluster(
    data: bytearray,
    first_data_sector: int,
    cluster: int,
    payload: bytes,
    sectors_per_cluster: int,
) -> None:
    offset = (first_data_sector + (cluster - 2) * sectors_per_cluster) * BYTES_PER_SECTOR
    data[offset : offset + len(payload)] = payload


def resolve_fat_layout(
    used_cluster_count: int,
    sectors_per_cluster: int,
    free_clusters: int,
    requested_volume_sectors: int,
) -> tuple[int, int, int, int]:
    root_dir_sectors = (ROOT_ENTRIES * 32 + BYTES_PER_SECTOR - 1) // BYTES_PER_SECTOR
    if requested_volume_sectors > 0:
        cluster_count = max(used_cluster_count, used_cluster_count + max(0, free_clusters))
        while True:
            sectors_per_fat = math.ceil(((cluster_count + 2) * 2) / BYTES_PER_SECTOR)
            first_data_sector = RESERVED_SECTORS + FAT_COPIES * sectors_per_fat + root_dir_sectors
            if requested_volume_sectors <= first_data_sector:
                raise ValueError("--volume-sectors is too small for FAT metadata")
            available_clusters = (requested_volume_sectors - first_data_sector) // sectors_per_cluster
            if available_clusters < used_cluster_count:
                raise ValueError("--volume-sectors is too small for the selected files")
            if available_clusters == cluster_count:
                return cluster_count, sectors_per_fat, first_data_sector, requested_volume_sectors
            cluster_count = available_clusters

    cluster_count = used_cluster_count + max(0, free_clusters)
    sectors_per_fat = math.ceil(((cluster_count + 2) * 2) / BYTES_PER_SECTOR)
    first_data_sector = RESERVED_SECTORS + FAT_COPIES * sectors_per_fat + root_dir_sectors
    volume_sectors = first_data_sector + cluster_count * sectors_per_cluster
    return cluster_count, sectors_per_fat, first_data_sector, volume_sectors


def build_image(
    root: FsNode,
    volume_label: str,
    prefix_bytes: int,
    partition_offset_sectors: int,
    free_clusters: int,
    sectors_per_cluster: int,
    volume_sectors: int,
) -> bytes:
    if sectors_per_cluster <= 0 or sectors_per_cluster > 128:
        raise ValueError("--sectors-per-cluster must be between 1 and 128")
    last_cluster = assign_clusters(root, sectors_per_cluster)
    used_cluster_count = last_cluster - 2
    cluster_count, sectors_per_fat, first_data_sector, volume_sectors = resolve_fat_layout(
        used_cluster_count,
        sectors_per_cluster,
        free_clusters,
        volume_sectors,
    )
    if cluster_count > 0xFFF5:
        raise ValueError("FAT16 cluster count exceeds firmware-compatible range")
    root_dir_sectors = (ROOT_ENTRIES * 32 + BYTES_PER_SECTOR - 1) // BYTES_PER_SECTOR
    volume_base = prefix_bytes + partition_offset_sectors * BYTES_PER_SECTOR
    image = bytearray(volume_base + volume_sectors * BYTES_PER_SECTOR)

    boot = bytearray(BYTES_PER_SECTOR)
    boot[0:3] = b"\xEB\x3C\x90"
    boot[3:11] = b"MSWIN4.1"
    struct.pack_into("<H", boot, 11, BYTES_PER_SECTOR)
    boot[13] = sectors_per_cluster
    struct.pack_into("<H", boot, 14, RESERVED_SECTORS)
    boot[16] = FAT_COPIES
    struct.pack_into("<H", boot, 17, ROOT_ENTRIES)
    struct.pack_into("<H", boot, 19, volume_sectors if volume_sectors < 0x10000 else 0)
    boot[21] = MEDIA_DESCRIPTOR
    struct.pack_into("<H", boot, 22, sectors_per_fat)
    struct.pack_into("<H", boot, 24, 63)
    struct.pack_into("<H", boot, 26, 255)
    struct.pack_into("<I", boot, 28, partition_offset_sectors)
    struct.pack_into("<I", boot, 32, volume_sectors if volume_sectors >= 0x10000 else 0)
    boot[36] = 0x80
    boot[38] = 0x29
    struct.pack_into("<I", boot, 39, 0x95884740)
    boot[43:54] = volume_label.upper().encode("ascii", errors="replace")[:11].ljust(11)
    boot[54:62] = b"FAT16   "
    boot[510:512] = b"\x55\xAA"
    if partition_offset_sectors and prefix_bytes == 0:
        mbr = bytearray(BYTES_PER_SECTOR)
        # One CHS/LBA FAT16 partition. The firmware path proven so far reads
        # LBA 0x20 directly, but a valid MBR makes the image usable by other
        # tools and by any alternate firmware mount path.
        mbr[446] = 0x00
        mbr[447:450] = b"\x01\x01\x00"
        mbr[450] = 0x06
        mbr[451:454] = b"\xfe\xff\xff"
        struct.pack_into("<I", mbr, 454, partition_offset_sectors)
        struct.pack_into("<I", mbr, 458, volume_sectors)
        mbr[510:512] = b"\x55\xAA"
        image[0:BYTES_PER_SECTOR] = mbr
    image[volume_base : volume_base + BYTES_PER_SECTOR] = boot

    fat = build_fat(root, cluster_count, sectors_per_cluster)
    fat = fat + b"\x00" * (sectors_per_fat * BYTES_PER_SECTOR - len(fat))
    fat_start = volume_base + RESERVED_SECTORS * BYTES_PER_SECTOR
    for copy in range(FAT_COPIES):
        start = fat_start + copy * sectors_per_fat * BYTES_PER_SECTOR
        image[start : start + len(fat)] = fat

    root_data = directory_entries(root, 0)
    root_start = volume_base + (RESERVED_SECTORS + FAT_COPIES * sectors_per_fat) * BYTES_PER_SECTOR
    image[root_start : root_start + min(len(root_data), root_dir_sectors * BYTES_PER_SECTOR)] = root_data[
        : root_dir_sectors * BYTES_PER_SECTOR
    ]

    cluster_size = BYTES_PER_SECTOR * sectors_per_cluster
    for node in iter_nodes(root):
        if not node.first_cluster:
            continue
        if node.is_dir:
            payload = directory_entries(node, node.parent_cluster)
        else:
            assert node.source is not None
            payload = node.source.read_bytes()
        payload = payload + b"\x00" * (align_up(len(payload), cluster_size) - len(payload))
        write_cluster(image, partition_offset_sectors + first_data_sector, node.first_cluster, payload, sectors_per_cluster)
    return bytes(image)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a FAT16 image for the BBK 9588 emulator.")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--volume-label", default="BBK9588")
    ap.add_argument(
        "--prefix-bytes",
        type=int,
        default=DEFAULT_PREFIX_BYTES,
        help="Byte prefix before the FAT16 volume.",
    )
    ap.add_argument(
        "--partition-offset-sectors",
        type=int,
        default=DEFAULT_PARTITION_OFFSET_SECTORS,
        help="Sector offset for the FAT16 volume. BBK firmware probes boot sector at LBA 0x20.",
    )
    ap.add_argument(
        "--free-clusters",
        type=int,
        default=DEFAULT_FREE_CLUSTERS,
        help="Append unused clusters so firmware free-space scans can find zero FAT entries.",
    )
    ap.add_argument(
        "--sectors-per-cluster",
        type=int,
        default=DEFAULT_SECTORS_PER_CLUSTER,
        help="FAT sectors per allocation unit. The dumped BBK9588 volume uses 32 sectors, i.e. 16 KiB.",
    )
    ap.add_argument(
        "--volume-sectors",
        type=lambda text: int(text, 0),
        default=DEFAULT_VOLUME_SECTORS,
        help="FAT volume sector count. Use 0 to size the image from file data plus --free-clusters.",
    )
    ap.add_argument("paths", type=Path, nargs="+", help="Top-level directories/files to place in the FAT root.")
    ns = ap.parse_args()

    root = build_tree(ns.paths)
    image = build_image(
        root,
        ns.volume_label,
        ns.prefix_bytes,
        ns.partition_offset_sectors,
        ns.free_clusters,
        ns.sectors_per_cluster,
        ns.volume_sectors,
    )
    ns.output.parent.mkdir(parents=True, exist_ok=True)
    ns.output.write_bytes(image)
    print(f"wrote {ns.output} ({len(image)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
