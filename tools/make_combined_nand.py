from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


BOOTROM_MAGIC = b"BBKUBOOT"
BOOTROM_HEADER_VERSION = 1
DEFAULT_PAGE_SIZE = 2048
DEFAULT_SPARE_SIZE = 64
DEFAULT_PAGES_PER_BLOCK = 64
DEFAULT_NAND_BLOCKS = 4096
DEFAULT_NAND_TOTAL_SIZE = (
    DEFAULT_NAND_BLOCKS * DEFAULT_PAGES_PER_BLOCK * (DEFAULT_PAGE_SIZE + DEFAULT_SPARE_SIZE)
)
DEFAULT_BOOTROM_BACKUP_ADDR = 0x2000


def ranges_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and start_b < end_a


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a BBK9588 NAND image with raw boot stages plus a FAT logical area.")
    ap.add_argument("--base-nand", type=Path, help="Optional legacy raw C200 OS image to place in NAND page data.")
    ap.add_argument("--os-page-base", type=lambda text: int(text, 0), default=0x200)
    ap.add_argument("--loader-image", type=Path, help="Optional first-stage loader image to place in the NAND boot block.")
    ap.add_argument("--loader-page-base", type=lambda text: int(text, 0), default=0)
    ap.add_argument("--bootrom-backup-addr", type=lambda text: int(text, 0), default=DEFAULT_BOOTROM_BACKUP_ADDR)
    ap.add_argument(
        "--no-bootrom-backup-loader",
        dest="bootrom_backup_loader",
        action="store_false",
        help="do not mirror the first-stage loader at the JZ4740 BootROM backup address",
    )
    ap.add_argument("--uboot-image", type=Path, help="Optional U-Boot image to place in the raw NAND boot area.")
    ap.add_argument("--uboot-page-base", type=lambda text: int(text, 0), default=0x40)
    ap.add_argument(
        "--legacy-uboot-header",
        action="store_true",
        help="write the old BBKUBOOT simulator header before U-Boot; off by default",
    )
    ap.add_argument(
        "--uboot-loader-copy-bytes",
        type=lambda text: int(text, 0),
        default=0xE0000,
        help="Bytes the first-stage loader copies from the U-Boot NAND area; used to mark boot-block OOB.",
    )
    ap.add_argument("--uboot-load-phys", type=lambda text: int(text, 0), default=0x00900000)
    ap.add_argument("--uboot-entry", type=lambda text: int(text, 0), default=0x80900000)
    ap.add_argument("--fat-image", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--fat-page-base", type=lambda text: int(text, 0), default=0x1C40)
    ap.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    ap.add_argument("--spare-size", type=int, default=DEFAULT_SPARE_SIZE)
    ap.add_argument(
        "--free-blocks",
        type=lambda text: int(text, 0),
        default=0,
        help="erased physical blocks to append after the FAT payload for FTL writes",
    )
    ap.add_argument("--pages-per-block", type=int, default=DEFAULT_PAGES_PER_BLOCK)
    ap.add_argument(
        "--physical-blocks",
        type=lambda text: int(text, 0),
        default=DEFAULT_NAND_BLOCKS,
        help=(
            "raw NAND physical block count; defaults to 4096 blocks "
            f"(0x{DEFAULT_NAND_TOTAL_SIZE:x} bytes with 2 KiB+64-byte pages)"
        ),
    )
    args = ap.parse_args()

    stride = args.page_size + args.spare_size
    if args.page_size <= 0 or args.spare_size < 0 or args.pages_per_block <= 0:
        raise SystemExit("page, spare, and block geometry must be positive")
    if args.physical_blocks < 0:
        raise SystemExit("--physical-blocks must be non-negative")
    os_image = args.base_nand.read_bytes() if args.base_nand is not None else b""
    fat = args.fat_image.read_bytes()
    os_pages = (len(os_image) + args.page_size - 1) // args.page_size
    page_count = (len(fat) + args.page_size - 1) // args.page_size
    loader = args.loader_image.read_bytes() if args.loader_image is not None else b""
    loader_pages = (len(loader) + args.page_size - 1) // args.page_size
    uboot = args.uboot_image.read_bytes() if args.uboot_image is not None else b""
    uboot_pages = (len(uboot) + args.page_size - 1) // args.page_size
    if args.fat_page_base < 0:
        raise SystemExit("--fat-page-base must be non-negative")
    if args.os_page_base < 0:
        raise SystemExit("--os-page-base must be non-negative")
    os_end = args.os_page_base + os_pages if os_image else 0
    loader_end = args.loader_page_base + loader_pages if loader else 0
    if os_image and os_end > args.fat_page_base:
        raise SystemExit(
            f"OS boot area overlaps FAT area: end page 0x{os_end:x}, fat page 0x{args.fat_page_base:x}"
        )
    if loader and args.loader_page_base < 0:
        raise SystemExit("--loader-page-base must be non-negative")
    if loader and loader_end > args.fat_page_base:
        raise SystemExit(
            f"loader boot area overlaps FAT area: end page 0x{loader_end:x}, fat page 0x{args.fat_page_base:x}"
        )
    if os_image and loader and ranges_overlap(args.os_page_base, os_end, args.loader_page_base, loader_end):
        raise SystemExit(
            f"OS boot area overlaps loader area: os pages 0x{args.os_page_base:x}..0x{os_end:x}, "
            f"loader pages 0x{args.loader_page_base:x}..0x{loader_end:x}"
        )
    loader_backup_page = None
    loader_backup_end = 0
    if loader and args.bootrom_backup_loader:
        if args.bootrom_backup_addr < 0 or args.bootrom_backup_addr % args.page_size:
            raise SystemExit("--bootrom-backup-addr must be non-negative and page-aligned")
        loader_backup_page = args.bootrom_backup_addr // args.page_size
        loader_backup_end = loader_backup_page + loader_pages
        if ranges_overlap(args.loader_page_base, loader_end, loader_backup_page, loader_backup_end):
            raise SystemExit(
                f"loader normal area overlaps BootROM backup area: "
                f"normal pages 0x{args.loader_page_base:x}..0x{loader_end:x}, "
                f"backup pages 0x{loader_backup_page:x}..0x{loader_backup_end:x}"
            )
        if loader_backup_end > args.fat_page_base:
            raise SystemExit(
                f"BootROM backup loader area overlaps FAT area: end page 0x{loader_backup_end:x}, "
                f"fat page 0x{args.fat_page_base:x}"
            )
        if os_image and ranges_overlap(args.os_page_base, os_end, loader_backup_page, loader_backup_end):
            raise SystemExit(
                f"OS boot area overlaps BootROM backup loader area: os pages 0x{args.os_page_base:x}..0x{os_end:x}, "
                f"backup pages 0x{loader_backup_page:x}..0x{loader_backup_end:x}"
            )

    uboot_marker_end = 0
    uboot_payload_page = args.uboot_page_base
    if uboot:
        uboot_payload_page = args.uboot_page_base + (1 if args.legacy_uboot_header else 0)
        uboot_end = uboot_payload_page + uboot_pages
        uboot_marker_end = uboot_end
        if loader:
            uboot_loader_copy_pages = 0
            if args.uboot_loader_copy_bytes > 0:
                uboot_loader_copy_pages = (args.uboot_loader_copy_bytes + args.page_size - 1) // args.page_size
            uboot_marker_end = args.uboot_page_base + max(uboot_pages, uboot_loader_copy_pages)
            uboot_marker_end = max(uboot_marker_end, args.fat_page_base)
        if args.uboot_page_base < 0 or args.uboot_page_base >= args.fat_page_base:
            raise SystemExit("--uboot-page-base must be before --fat-page-base")
        if loader_end > args.uboot_page_base:
            raise SystemExit(
                f"loader boot area overlaps U-Boot area: end page 0x{loader_end:x}, uboot page 0x{args.uboot_page_base:x}"
            )
        if loader_backup_page is not None and ranges_overlap(
            loader_backup_page, loader_backup_end, args.uboot_page_base, uboot_end
        ):
            raise SystemExit(
                f"BootROM backup loader area overlaps U-Boot area: backup pages 0x{loader_backup_page:x}..0x{loader_backup_end:x}, "
                f"uboot pages 0x{args.uboot_page_base:x}..0x{uboot_end:x}"
            )
        if os_image and ranges_overlap(args.os_page_base, os_end, args.uboot_page_base, uboot_end):
            raise SystemExit(
                f"OS boot area overlaps U-Boot area: os pages 0x{args.os_page_base:x}..0x{os_end:x}, "
                f"uboot pages 0x{args.uboot_page_base:x}..0x{uboot_end:x}"
            )
        if uboot_marker_end > args.fat_page_base:
            raise SystemExit(
                f"U-Boot boot area overlaps FAT area: end page 0x{uboot_marker_end:x}, fat page 0x{args.fat_page_base:x}"
            )
    free_pages = max(0, args.free_blocks) * args.pages_per_block
    boot_area_pages = max(loader_end, loader_backup_end, uboot_marker_end)
    required_pages = max(os_end, boot_area_pages, args.fat_page_base + page_count + free_pages)
    physical_pages = args.physical_blocks * args.pages_per_block
    if physical_pages and required_pages > physical_pages:
        raise SystemExit(
            f"image contents require 0x{required_pages:x} pages but --physical-blocks only provides 0x{physical_pages:x} pages"
        )
    out_size = max(required_pages, physical_pages) * stride

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as f:
        chunk = b"\xFF" * (1024 * 1024)
        remaining = out_size
        while remaining:
            n = min(remaining, len(chunk))
            f.write(chunk[:n])
            remaining -= n
        for page in range(os_pages):
            chunk = os_image[page * args.page_size : (page + 1) * args.page_size]
            if len(chunk) < args.page_size:
                chunk += b"\xFF" * (args.page_size - len(chunk))
            f.seek((args.os_page_base + page) * stride)
            f.write(chunk)
            f.write(b"\xFF" * args.spare_size)
        for page in range(loader_pages):
            chunk = loader[page * args.page_size : (page + 1) * args.page_size]
            if len(chunk) < args.page_size:
                chunk += b"\xFF" * (args.page_size - len(chunk))
            f.seek((args.loader_page_base + page) * stride)
            f.write(chunk)
            f.write(b"\xFF" * args.spare_size)
        if loader_backup_page is not None:
            for page in range(loader_pages):
                chunk = loader[page * args.page_size : (page + 1) * args.page_size]
                if len(chunk) < args.page_size:
                    chunk += b"\xFF" * (args.page_size - len(chunk))
                f.seek((loader_backup_page + page) * stride)
                f.write(chunk)
                f.write(b"\xFF" * args.spare_size)
        if uboot:
            if args.legacy_uboot_header:
                header = bytearray(args.page_size)
                header[0:8] = BOOTROM_MAGIC
                struct.pack_into(
                    "<IIII",
                    header,
                    8,
                    BOOTROM_HEADER_VERSION,
                    args.uboot_load_phys,
                    args.uboot_entry,
                    len(uboot),
                )
                f.seek(args.uboot_page_base * stride)
                f.write(header)
                f.write(b"\xFF" * args.spare_size)
            for page in range(uboot_pages):
                chunk = uboot[page * args.page_size : (page + 1) * args.page_size]
                if len(chunk) < args.page_size:
                    chunk += b"\xFF" * (args.page_size - len(chunk))
                f.seek((uboot_payload_page + page) * stride)
                f.write(chunk)
                f.write(b"\xFF" * args.spare_size)
        for page in range(page_count):
            chunk = fat[page * args.page_size : (page + 1) * args.page_size]
            if len(chunk) < args.page_size:
                chunk += b"\x00" * (args.page_size - len(chunk))
            f.seek((args.fat_page_base + page) * stride)
            f.write(chunk)
            f.write(b"\xFF" * args.spare_size)
        if args.spare_size >= 5:
            spare = bytearray(b"\xFF" * args.spare_size)
            spare[2:5] = b"\x00\x00\x00"
            boot_ranges = [
                (args.loader_page_base, loader_end),
                (args.uboot_page_base, uboot_marker_end),
            ]
            if loader_backup_page is not None:
                boot_ranges.append((loader_backup_page, loader_backup_end))
            for start_page, end_page in boot_ranges:
                if end_page <= start_page:
                    continue
                for page in range(start_page, end_page):
                    f.seek(page * stride + args.page_size)
                    f.write(spare)

    print(
        f"wrote {args.output} size=0x{out_size:x} "
        f"os_page_base=0x{args.os_page_base:x} os_pages=0x{os_pages:x} "
        f"fat_page_base=0x{args.fat_page_base:x} pages=0x{page_count:x} "
        f"free_blocks=0x{max(0, args.free_blocks):x} "
        f"loader_page_base=0x{args.loader_page_base:x} loader_pages=0x{loader_pages:x} "
        f"loader_backup_page={('none' if loader_backup_page is None else f'0x{loader_backup_page:x}')} "
        f"uboot_page_base=0x{args.uboot_page_base:x} uboot_pages=0x{uboot_pages:x} "
        f"uboot_marker_end=0x{uboot_marker_end:x} "
        f"physical_blocks=0x{args.physical_blocks:x}"
    )


if __name__ == "__main__":
    main()
