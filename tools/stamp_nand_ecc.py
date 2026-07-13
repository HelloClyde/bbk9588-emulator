from __future__ import annotations

import argparse
import concurrent.futures
import mmap
import os
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_PAGE_SIZE = 2048
DEFAULT_SPARE_SIZE = 64
DEFAULT_BOOT_ECC_END_PAGE = 0x200
BOOT_ECC_OOB_OFFSET = 6
DATA_ECC_OOB_OFFSET = 4


def stamp_page_range(
    output: str,
    start_page: int,
    end_page: int,
    page_size: int,
    spare_size: int,
    boot_ecc_end_page: int,
) -> int:
    from emu.qemu.ecc import jz4740_page_oob_ecc

    stride = page_size + spare_size
    erased_page = b"\xff" * page_size
    stamped_pages = 0
    with Path(output).open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as image:
        for page in range(start_page, end_page):
            page_offset = page * stride
            page_data = bytes(image[page_offset : page_offset + page_size])
            oob_offset = page_offset + page_size
            page_oob = image[oob_offset : oob_offset + spare_size]
            if page_data == erased_page and page_oob == b"\xff" * spare_size:
                continue
            ecc_oob_offset = (
                BOOT_ECC_OOB_OFFSET
                if page < boot_ecc_end_page
                else DATA_ECC_OOB_OFFSET
            )
            parity = jz4740_page_oob_ecc(page_data, offset=ecc_oob_offset)
            image[
                oob_offset + ecc_oob_offset : oob_offset + len(parity)
            ] = parity[ecc_oob_offset:]
            stamped_pages += 1
    return stamped_pages


def main() -> None:
    from emu.qemu.ecc import jz4740_page_oob_ecc

    ap = argparse.ArgumentParser(
        description="Copy a raw BBK9588 NAND image and stamp JZ4740 RS parity into programmed-page OOB."
    )
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    ap.add_argument("--spare-size", type=int, default=DEFAULT_SPARE_SIZE)
    ap.add_argument(
        "--boot-ecc-end-page",
        type=lambda text: int(text, 0),
        default=DEFAULT_BOOT_ECC_END_PAGE,
        help=(
            "first page using the U-Boot data-area ECC layout; defaults to 0x200 "
            "for the BBK9588 0x40 + 0xe0000-byte boot copy"
        ),
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="parallel parity workers; defaults to min(8, host CPU count)",
    )
    args = ap.parse_args()

    source = args.input.resolve()
    output = args.output.resolve()
    if source == output:
        raise SystemExit("input and output must differ; write a new image before replacing the original")
    if args.page_size <= 0 or args.spare_size < 0:
        raise SystemExit("page and spare sizes must be positive")
    if args.workers <= 0:
        raise SystemExit("--workers must be positive")
    if args.boot_ecc_end_page < 0:
        raise SystemExit("--boot-ecc-end-page must be non-negative")
    probe_oob = jz4740_page_oob_ecc(
        b"\xff" * args.page_size,
        offset=BOOT_ECC_OOB_OFFSET,
    )
    if len(probe_oob) > args.spare_size:
        raise SystemExit("spare area is too small for JZ4740 ECC parity")

    stride = args.page_size + args.spare_size
    image_size = source.stat().st_size
    if image_size == 0 or image_size % stride:
        raise SystemExit(f"input size 0x{image_size:x} is not aligned to raw NAND stride 0x{stride:x}")

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    page_count = image_size // stride
    worker_count = min(args.workers, page_count)
    if page_count < 4096:
        worker_count = 1
    if worker_count == 1:
        stamped_pages = stamp_page_range(
            str(output),
            0,
            page_count,
            args.page_size,
            args.spare_size,
            args.boot_ecc_end_page,
        )
    else:
        task_count = worker_count * 4
        pages_per_task = (page_count + task_count - 1) // task_count
        ranges = [
            (start, min(start + pages_per_task, page_count))
            for start in range(0, page_count, pages_per_task)
        ]
        with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as pool:
            futures = [
                pool.submit(
                    stamp_page_range,
                    str(output),
                    start,
                    end,
                    args.page_size,
                    args.spare_size,
                    args.boot_ecc_end_page,
                )
                for start, end in ranges
            ]
            stamped_pages = sum(future.result() for future in futures)

    print(
        f"wrote {output} size=0x{image_size:x} "
        f"pages=0x{page_count:x} ecc_pages=0x{stamped_pages:x} "
        f"boot_ecc_end_page=0x{args.boot_ecc_end_page:x} workers={worker_count}"
    )


if __name__ == "__main__":
    main()
