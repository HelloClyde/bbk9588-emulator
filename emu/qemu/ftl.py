"""BBK 9588 NAND FTL metadata parsing and fault injection.

The mapping rules mirror the U-Boot/C200 scan at 0x80903d1c and
0x8017db6c.  This module is diagnostic host code; the guest firmware still
owns the live FTL implementation.
"""

from __future__ import annotations

import mmap
import os
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

PAGE_SIZE = 2048
SPARE_SIZE = 64
PAGE_STRIDE = PAGE_SIZE + SPARE_SIZE
PAGES_PER_BLOCK = 64
RAW_BLOCK_SIZE = PAGE_STRIDE * PAGES_PER_BLOCK
LOGICAL_BLOCK_SIZE = PAGE_SIZE * PAGES_PER_BLOCK
UBOOT_FTL_SCAN_DATA_OFFSET = 0xB40000
UBOOT_FTL_SCAN_START_BLOCK = UBOOT_FTL_SCAN_DATA_OFFSET // LOGICAL_BLOCK_SIZE
BBT8_TAG = 0x38746262
SUPPORTED_FULL_NAND_BLOCK_COUNTS = frozenset({1024, 2048, 4096, 8192, 16384})


@dataclass(frozen=True)
class FtlBlockRecord:
    physical: int
    kind: str
    sequence: int | None = None
    logical: int | None = None
    tail: int | None = None
    last_valid_page: int | None = None
    marker: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class FtlScanResult:
    path: Path
    block_count: int
    scan_start_block: int
    records: tuple[FtlBlockRecord, ...]
    mapping: dict[int, FtlBlockRecord]
    candidates: dict[int, tuple[FtlBlockRecord, ...]]

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(record.kind for record in self.records))

    @property
    def duplicate_logical_blocks(self) -> dict[int, tuple[FtlBlockRecord, ...]]:
        return {
            logical: records
            for logical, records in self.candidates.items()
            if len(records) > 1
        }


def firmware_scan_start_block(block_count: int) -> int:
    """Use the firmware's reserved prefix for real NAND and zero for fixtures."""

    return (
        UBOOT_FTL_SCAN_START_BLOCK
        if block_count in SUPPORTED_FULL_NAND_BLOCK_COUNTS
        else 0
    )


def sequence_is_newer(candidate: int, current: int) -> bool:
    """Return the C200/U-Boot 16-bit serial-number ordering result."""

    return ((current - candidate) & 0xFFFF) > 0x8000


def _read_oob(stream, physical: int, page: int) -> bytes:
    offset = physical * RAW_BLOCK_SIZE + page * PAGE_STRIDE + PAGE_SIZE
    stream.seek(offset)
    value = stream.read(SPARE_SIZE)
    if len(value) != SPARE_SIZE:
        raise IOError(
            f"short NAND OOB read at physical block 0x{physical:x} page {page}"
        )
    return value


def scan_ftl_image(
    path: Path,
    *,
    scan_start_block: int | None = None,
) -> FtlScanResult:
    """Scan a raw 2 KiB+64-byte NAND image using firmware FTL rules."""

    image = path.resolve()
    size = image.stat().st_size
    if size == 0 or size % RAW_BLOCK_SIZE:
        raise ValueError(f"unsupported NAND geometry: {image} size={size}")
    block_count = size // RAW_BLOCK_SIZE
    start = (
        firmware_scan_start_block(block_count)
        if scan_start_block is None
        else scan_start_block
    )
    if start < 0 or start > block_count:
        raise ValueError(
            f"FTL scan start block 0x{start:x} is outside 0x{block_count:x} blocks"
        )

    records: list[FtlBlockRecord] = []
    candidates: dict[int, list[FtlBlockRecord]] = defaultdict(list)
    mapping: dict[int, FtlBlockRecord] = {}
    with image.open("rb") as stream:
        for physical in range(start, block_count):
            bad_marker = _read_oob(stream, physical, PAGES_PER_BLOCK - 1)[0]
            if bad_marker != 0xFF:
                records.append(
                    FtlBlockRecord(
                        physical=physical,
                        kind="bad",
                        marker=bad_marker,
                        reason="last-page bad-block marker is programmed",
                    )
                )
                continue

            first = _read_oob(stream, physical, 0)
            marker = first[1]
            last_valid_page = int.from_bytes(first[2:4], "little")
            sequence = int.from_bytes(first[-6:-4], "little")
            tail = int.from_bytes(first[-4:], "little")
            if marker != 0xFF and last_valid_page < PAGES_PER_BLOCK:
                last = _read_oob(stream, physical, last_valid_page)
                if last[-6:] != first[-6:]:
                    records.append(
                        FtlBlockRecord(
                            physical=physical,
                            kind="torn",
                            sequence=sequence,
                            logical=tail & 0xFFFF,
                            tail=tail,
                            last_valid_page=last_valid_page,
                            marker=marker,
                            reason="first and last-valid-page FTL tails differ",
                        )
                    )
                    continue

            if tail == 0xFFFFFFFF:
                records.append(
                    FtlBlockRecord(
                        physical=physical,
                        kind="free",
                        sequence=sequence,
                        tail=tail,
                        last_valid_page=last_valid_page,
                        marker=marker,
                    )
                )
                continue
            if tail == BBT8_TAG:
                records.append(
                    FtlBlockRecord(
                        physical=physical,
                        kind="bbt",
                        sequence=sequence,
                        tail=tail,
                        last_valid_page=last_valid_page,
                        marker=marker,
                    )
                )
                continue

            logical = tail & 0xFFFF
            if logical >= block_count:
                records.append(
                    FtlBlockRecord(
                        physical=physical,
                        kind="invalid",
                        sequence=sequence,
                        logical=logical,
                        tail=tail,
                        last_valid_page=last_valid_page,
                        marker=marker,
                        reason="logical block is outside the NAND geometry",
                    )
                )
                continue

            record = FtlBlockRecord(
                physical=physical,
                kind="mapped",
                sequence=sequence,
                logical=logical,
                tail=tail,
                last_valid_page=last_valid_page,
                marker=marker,
            )
            records.append(record)
            candidates[logical].append(record)
            current = mapping.get(logical)
            if current is None or sequence_is_newer(sequence, current.sequence or 0):
                mapping[logical] = record

    return FtlScanResult(
        path=image,
        block_count=block_count,
        scan_start_block=start,
        records=tuple(records),
        mapping=mapping,
        candidates={logical: tuple(values) for logical, values in candidates.items()},
    )


def inject_tail_power_cut(
    source_path: Path,
    output_path: Path,
    physical: int,
) -> Path:
    """Create a NAND-valid bit-clear fault in a block's commit tail.

    The first-page tag is left intact while one programmed bit is cleared in
    the last-valid-page copy.  Firmware must reject the resulting block as a
    torn FTL commit.  Page zero cannot represent this two-copy failure mode.
    """

    source = source_path.resolve()
    output = output_path.resolve()
    if output == source:
        raise ValueError("power-cut output must differ from the source image")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        _clear_ftl_commit_tail_bit(temporary, physical)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _clear_ftl_commit_tail_bit(path: Path, physical: int) -> None:
    result = scan_ftl_image(path, scan_start_block=0)
    record = next((item for item in result.records if item.physical == physical), None)
    if record is None or record.kind != "mapped":
        raise ValueError(f"physical block 0x{physical:x} is not a valid FTL mapping")
    page = record.last_valid_page
    if page is None or page <= 0 or page >= PAGES_PER_BLOCK:
        raise ValueError(
            f"physical block 0x{physical:x} has no separate last-valid-page tag"
        )
    tail_offset = (
        physical * RAW_BLOCK_SIZE
        + page * PAGE_STRIDE
        + PAGE_SIZE
        + SPARE_SIZE
        - 6
    )
    with path.open("r+b") as stream:
        stream.seek(tail_offset)
        tail = bytearray(stream.read(6))
        if len(tail) != 6:
            raise IOError("short NAND tail read while injecting power cut")
        for index, value in enumerate(tail):
            if value:
                tail[index] = value & (value - 1)
                break
        else:
            raise ValueError("FTL tail has no programmed bit available to clear")
        stream.seek(tail_offset)
        stream.write(tail)
        stream.flush()
        os.fsync(stream.fileno())


def inject_remap_power_cut(
    reference_path: Path,
    committed_path: Path,
    output_path: Path,
    logical: int,
) -> Path:
    """Build a remap snapshot where the new commit tears before old erase."""

    reference = reference_path.resolve()
    committed = committed_path.resolve()
    output = output_path.resolve()
    if output in {reference, committed}:
        raise ValueError("power-cut output must differ from both input images")
    reference_scan = scan_ftl_image(reference)
    committed_scan = scan_ftl_image(committed)
    if reference_scan.block_count != committed_scan.block_count:
        raise ValueError("reference and committed NAND geometries differ")
    previous = reference_scan.mapping.get(logical)
    current = committed_scan.mapping.get(logical)
    if previous is None or current is None:
        raise ValueError(f"logical block 0x{logical:x} is not mapped in both images")
    if previous.physical == current.physical:
        raise ValueError(f"logical block 0x{logical:x} was not remapped")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(committed, temporary)
        with reference.open("rb") as source, temporary.open("r+b") as target:
            source.seek(previous.physical * RAW_BLOCK_SIZE)
            old_block = source.read(RAW_BLOCK_SIZE)
            if len(old_block) != RAW_BLOCK_SIZE:
                raise IOError("short old physical block read")
            target.seek(previous.physical * RAW_BLOCK_SIZE)
            target.write(old_block)
            target.flush()
            os.fsync(target.fileno())
        _clear_ftl_commit_tail_bit(temporary, current.physical)
        cut_scan = scan_ftl_image(temporary)
        fallback = cut_scan.mapping.get(logical)
        if fallback is None or fallback.physical != previous.physical:
            raise ValueError(
                f"power-cut image did not fall back logical 0x{logical:x} "
                f"to physical 0x{previous.physical:x}"
            )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def count_legacy_logical_tail_pages(path: Path) -> int:
    """Count FTL page tags whose logical high half is not erased."""

    image = path.resolve()
    size = image.stat().st_size
    if size == 0 or size % RAW_BLOCK_SIZE:
        raise ValueError(f"unsupported NAND geometry: {image} size={size}")
    block_count = size // RAW_BLOCK_SIZE
    start = firmware_scan_start_block(block_count)
    count = 0
    with image.open("rb") as stream:
        view = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            for physical in range(start, block_count):
                for page in range(PAGES_PER_BLOCK):
                    offset = (
                        physical * RAW_BLOCK_SIZE
                        + page * PAGE_STRIDE
                        + PAGE_SIZE
                        + SPARE_SIZE
                        - 6
                    )
                    sequence = int.from_bytes(view[offset : offset + 2], "little")
                    tail = int.from_bytes(view[offset + 2 : offset + 6], "little")
                    logical = tail & 0xFFFF
                    if (
                        sequence != 0xFFFF
                        and tail not in {0xFFFFFFFF, BBT8_TAG}
                        and logical < block_count
                        and tail >> 16 != 0xFFFF
                    ):
                        count += 1
        finally:
            view.close()
    return count


def normalize_c200_logical_tail_pages(path: Path) -> int:
    """Offline-migrate synthetic 32-bit logical tags to C200's 16-bit form."""

    image = path.resolve()
    size = image.stat().st_size
    if size == 0 or size % RAW_BLOCK_SIZE:
        raise ValueError(f"unsupported NAND geometry: {image} size={size}")
    block_count = size // RAW_BLOCK_SIZE
    start = firmware_scan_start_block(block_count)
    changed = 0
    with image.open("r+b") as stream:
        view = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_WRITE)
        try:
            for physical in range(start, block_count):
                for page in range(PAGES_PER_BLOCK):
                    offset = (
                        physical * RAW_BLOCK_SIZE
                        + page * PAGE_STRIDE
                        + PAGE_SIZE
                        + SPARE_SIZE
                        - 6
                    )
                    sequence = int.from_bytes(view[offset : offset + 2], "little")
                    tail = int.from_bytes(view[offset + 2 : offset + 6], "little")
                    logical = tail & 0xFFFF
                    if (
                        sequence != 0xFFFF
                        and tail not in {0xFFFFFFFF, BBT8_TAG}
                        and logical < block_count
                        and tail >> 16 != 0xFFFF
                    ):
                        view[offset + 4 : offset + 6] = b"\xff\xff"
                        changed += 1
            view.flush()
        finally:
            view.close()
        os.fsync(stream.fileno())
    return changed
