from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from emu.qemu.ftl import (
    PAGE_SIZE,
    PAGE_STRIDE,
    PAGES_PER_BLOCK,
    RAW_BLOCK_SIZE,
    count_legacy_logical_tail_pages,
    inject_remap_power_cut,
    inject_tail_power_cut,
    normalize_c200_logical_tail_pages,
    scan_ftl_image,
    sequence_is_newer,
)
from emu.qemu.system import commit_runtime_nand_checkpoint, ensure_runtime_nand_checkpoint


def _write_tag(
    image: bytearray,
    physical: int,
    logical: int,
    sequence: int,
    *,
    last_valid_page: int = 63,
    high: int = 0xFFFF,
) -> None:
    tail = sequence.to_bytes(2, "little") + (
        (high << 16) | logical
    ).to_bytes(4, "little")
    for page in (0, last_valid_page):
        oob = physical * RAW_BLOCK_SIZE + page * PAGE_STRIDE + PAGE_SIZE
        image[oob + 1] = 0
        image[oob + 2 : oob + 4] = last_valid_page.to_bytes(2, "little")
        image[oob + 58 : oob + 64] = tail


class FtlTests(unittest.TestCase):
    def test_sequence_order_matches_firmware_wraparound(self) -> None:
        self.assertTrue(sequence_is_newer(1, 0xFFFE))
        self.assertFalse(sequence_is_newer(0xFFFE, 1))
        self.assertFalse(sequence_is_newer(7, 7))
        self.assertFalse(sequence_is_newer(0x8000, 0))

    def test_scan_rejects_torn_tail_and_uses_serial_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nand.bin"
            image = bytearray(b"\xff" * (RAW_BLOCK_SIZE * 4))
            _write_tag(image, 0, 0, 0xFFFE)
            _write_tag(image, 1, 0, 1)
            _write_tag(image, 2, 1, 4)
            page = 63
            oob = 2 * RAW_BLOCK_SIZE + page * PAGE_STRIDE + PAGE_SIZE
            image[oob + 63] = 0xFE
            path.write_bytes(image)

            result = scan_ftl_image(path, scan_start_block=0)

        self.assertEqual(result.mapping[0].physical, 1)
        self.assertNotIn(1, result.mapping)
        self.assertEqual(result.counts["torn"], 1)
        self.assertEqual(result.records[2].tail, 0xFFFF0001)

    def test_last_page_bad_block_marker_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nand.bin"
            image = bytearray(b"\xff" * RAW_BLOCK_SIZE)
            _write_tag(image, 0, 0, 1)
            last_oob = (PAGES_PER_BLOCK - 1) * PAGE_STRIDE + PAGE_SIZE
            image[last_oob] = 0
            path.write_bytes(image)

            result = scan_ftl_image(path, scan_start_block=0)

        self.assertEqual(result.counts, {"bad": 1})
        self.assertEqual(result.mapping, {})

    def test_power_cut_injection_only_clears_bits_and_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            output = root / "cut.bin"
            image = bytearray(b"\xff" * RAW_BLOCK_SIZE)
            _write_tag(image, 0, 0, 1)
            source.write_bytes(image)

            inject_tail_power_cut(source, output, 0)
            before = source.read_bytes()
            after = output.read_bytes()
            result = scan_ftl_image(output, scan_start_block=0)

        self.assertTrue(all((old & new) == new for old, new in zip(before, after)))
        self.assertEqual(result.counts["torn"], 1)
        self.assertEqual(result.mapping, {})

    def test_remap_power_cut_falls_back_to_previous_physical_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = root / "reference.bin"
            committed = root / "committed.bin"
            output = root / "cut.bin"
            before = bytearray(b"\xff" * (RAW_BLOCK_SIZE * 3))
            _write_tag(before, 0, 0, 1)
            before[0] = 0x11
            reference.write_bytes(before)
            after = bytearray(before)
            after[:RAW_BLOCK_SIZE] = b"\xff" * RAW_BLOCK_SIZE
            _write_tag(after, 1, 0, 2)
            after[RAW_BLOCK_SIZE] = 0x22
            committed.write_bytes(after)

            inject_remap_power_cut(reference, committed, output, 0)
            result = scan_ftl_image(output, scan_start_block=0)
            output_data = output.read_bytes()

        self.assertEqual(result.mapping[0].physical, 0)
        self.assertEqual(result.counts["torn"], 1)
        self.assertEqual(output_data[0], 0x11)

    def test_legacy_logical_tail_migration_repairs_mixed_high_halves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nand.bin"
            image = bytearray(b"\xff" * RAW_BLOCK_SIZE)
            _write_tag(image, 0, 0, 3, high=0)
            last_oob = (PAGES_PER_BLOCK - 1) * PAGE_STRIDE + PAGE_SIZE
            image[last_oob + 62 : last_oob + 64] = b"\xff\xff"
            path.write_bytes(image)

            before = scan_ftl_image(path, scan_start_block=0)
            legacy = count_legacy_logical_tail_pages(path)
            changed = normalize_c200_logical_tail_pages(path)
            after = scan_ftl_image(path, scan_start_block=0)

        self.assertEqual(before.counts["torn"], 1)
        self.assertEqual(legacy, 1)
        self.assertEqual(changed, 1)
        self.assertEqual(after.mapping[0].physical, 0)
        self.assertEqual(after.mapping[0].tail, 0xFFFF0000)

    def test_stamp_ftl_uses_c200_erased_high_half(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            output = root / "output.bin"
            image = bytearray(b"\xff" * (RAW_BLOCK_SIZE * 2))
            image[:PAGE_SIZE] = b"A" * PAGE_SIZE
            source.write_bytes(image)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "tools" / "stamp_ftl_oob.py"),
                    str(source),
                    str(output),
                    "--fat-page-base",
                    "0",
                    "--logical-blocks",
                    "1",
                    "--max-physical-blocks",
                    "2",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            data = output.read_bytes()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(data[PAGE_SIZE + 58 : PAGE_SIZE + 64], b"\x01\x00\x00\x00\xff\xff")

    def test_checkpoint_creation_and_reuse_migrate_legacy_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            image = bytearray(b"\xff" * RAW_BLOCK_SIZE)
            _write_tag(image, 0, 0, 1, high=0)
            source.write_bytes(image)
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                checkpoint = ensure_runtime_nand_checkpoint(source)
                self.assertEqual(count_legacy_logical_tail_pages(checkpoint), 0)
                self.assertEqual(count_legacy_logical_tail_pages(source), 2)

                legacy = bytearray(checkpoint.read_bytes())
                for page in (0, PAGES_PER_BLOCK - 1):
                    oob = page * PAGE_STRIDE + PAGE_SIZE
                    legacy[oob + 62 : oob + 64] = b"\x00\x00"
                checkpoint.write_bytes(legacy)
                reopened = ensure_runtime_nand_checkpoint(source)
                self.assertEqual(reopened, checkpoint)
                self.assertEqual(count_legacy_logical_tail_pages(reopened), 0)
            finally:
                os.chdir(old_cwd)

    def test_checkpoint_commit_maps_from_migrated_torn_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            runtime = root / "runtime.bin"
            checkpoint = root / "checkpoint.bin"
            image = bytearray(b"\xff" * RAW_BLOCK_SIZE)
            _write_tag(image, 0, 0, 3, high=0)
            last_oob = (PAGES_PER_BLOCK - 1) * PAGE_STRIDE + PAGE_SIZE
            image[last_oob + 62 : last_oob + 64] = b"\xff\xff"
            source.write_bytes(image)
            runtime.write_bytes(image)
            normalize_c200_logical_tail_pages(runtime)
            runtime_data = bytearray(runtime.read_bytes())
            runtime_data[0] = 0xA5
            runtime.write_bytes(runtime_data)

            commit_runtime_nand_checkpoint(source, runtime, checkpoint)
            result = scan_ftl_image(checkpoint, scan_start_block=0)
            checkpoint_first_byte = checkpoint.read_bytes()[0]

        self.assertEqual(result.mapping[0].physical, 0)
        self.assertEqual(checkpoint_first_byte, 0xA5)


if __name__ == "__main__":
    unittest.main()
