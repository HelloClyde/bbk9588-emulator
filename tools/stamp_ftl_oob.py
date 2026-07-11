from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


DEFAULT_PAGE_SIZE = 2048
DEFAULT_SPARE_SIZE = 64
DEFAULT_PAGES_PER_BLOCK = 64
DEFAULT_NAND_BLOCKS = 4096


def main() -> None:
    ap = argparse.ArgumentParser(description="Stamp minimal BBK9588/C200 FTL OOB mapping tags into a raw NAND image.")
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--fat-page-base", type=lambda text: int(text, 0), default=0x1C40)
    ap.add_argument(
        "--logical-blocks",
        type=lambda text: int(text, 0),
        default=None,
        help="number of FAT physical blocks to map; defaults to non-erased data blocks after --fat-page-base",
    )
    ap.add_argument("--sequence", type=lambda text: int(text, 0), default=1)
    ap.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    ap.add_argument("--spare-size", type=int, default=DEFAULT_SPARE_SIZE)
    ap.add_argument("--pages-per-block", type=int, default=DEFAULT_PAGES_PER_BLOCK)
    ap.add_argument(
        "--max-physical-blocks",
        type=lambda text: int(text, 0),
        default=DEFAULT_NAND_BLOCKS,
        help="FTL physical block window managed by C200; blocks beyond this remain invisible to firmware",
    )
    ap.add_argument(
        "--reserve-free-blocks",
        type=lambda text: int(text, 0),
        default=0,
        help="erased blocks to leave unstamped inside --max-physical-blocks for firmware FTL writes",
    )
    ap.add_argument(
        "--keep-existing-tags",
        action="store_true",
        help="do not clear stale first-page FTL tags before stamping the synthetic FAT mapping",
    )
    args = ap.parse_args()

    if args.fat_page_base % args.pages_per_block:
        raise SystemExit("--fat-page-base must be block-aligned")
    stride = args.page_size + args.spare_size
    physical_block_base = args.fat_page_base // args.pages_per_block
    data = bytearray(args.input.read_bytes())
    page_count = len(data) // stride
    max_physical_blocks = min(page_count // args.pages_per_block, args.max_physical_blocks)
    available = max_physical_blocks - physical_block_base
    if args.logical_blocks is None:
        count = 0
        for physical_block in range(physical_block_base, max_physical_blocks):
            block_start = physical_block * args.pages_per_block * stride
            block_end = block_start + args.pages_per_block * stride
            if block_end > len(data):
                break
            block = data[block_start:block_end]
            if any(byte != 0xFF for byte in block):
                count = physical_block - physical_block_base + 1
        reserved = max(0, args.reserve_free_blocks)
        count = min(count, max(0, available - reserved))
    else:
        count = min(args.logical_blocks, max(0, available - max(0, args.reserve_free_blocks)))
    if count <= 0:
        raise SystemExit("no physical blocks available for stamping")

    if not args.keep_existing_tags:
        for physical_block in range(max_physical_blocks):
            page = physical_block * args.pages_per_block
            oob = page * stride + args.page_size
            if oob + args.spare_size > len(data):
                break
            data[oob + args.spare_size - 6 : oob + args.spare_size] = b"\xff" * 6

    for logical_block in range(count):
        physical_block = physical_block_base + logical_block
        first_page = physical_block * args.pages_per_block
        block_start = first_page * stride
        block_end = block_start + args.pages_per_block * stride
        if block_end > len(data):
            break
        valid_pages: list[int] = []
        for page_in_block in range(args.pages_per_block):
            page = first_page + page_in_block
            page_off = page * stride
            page_data = data[page_off : page_off + args.page_size]
            if any(byte != 0xFF for byte in page_data):
                valid_pages.append(page_in_block)
        if not valid_pages:
            valid_pages.append(0)

        last_valid_page = valid_pages[-1] & 0xFF
        for page_in_block in valid_pages:
            page = first_page + page_in_block
            oob = page * stride + args.page_size
            if oob + args.spare_size > len(data):
                break
            # C200 marks valid programmed pages in OOB byte 1. Runtime-written
            # blocks keep the highest programmed page in byte 2.
            data[oob + 0] = 0xFF
            data[oob + 1] = 0x00
            data[oob + 2] = last_valid_page
            data[oob + 3] = 0x00
            struct.pack_into("<H", data, oob + args.spare_size - 6, args.sequence & 0xFFFF)
            struct.pack_into("<I", data, oob + args.spare_size - 4, logical_block & 0xFFFF)

        oob = first_page * stride + args.page_size
        # C200's FTL scan reads the first page spare area of each block.
        # At spare[-6] it compares a 16-bit generation counter; at spare[-4]
        # it accepts normal mappings when the low 16 bits are < block_count.
        # The scan also compares first-page and last-valid-page spare[-6:]
        # byte-for-byte, so every stamped valid page must use the same 32-bit
        # logical tail rather than leaving the high half erased.
        struct.pack_into("<H", data, oob + args.spare_size - 6, args.sequence & 0xFFFF)
        struct.pack_into("<I", data, oob + args.spare_size - 4, logical_block & 0xFFFF)

    free_start = physical_block_base + count
    for physical_block in range(free_start, max_physical_blocks):
        block_start = physical_block * args.pages_per_block * stride
        block_end = block_start + args.pages_per_block * stride
        if block_end > len(data):
            break
        data[block_start:block_end] = b"\xFF" * (block_end - block_start)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    print(
        f"wrote {args.output} size=0x{len(data):x} "
        f"physical_block_base=0x{physical_block_base:x} stamped_blocks=0x{count:x}"
    )


if __name__ == "__main__":
    main()
