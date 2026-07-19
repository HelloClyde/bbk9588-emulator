from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from pyfatfs.PyFat import PyFat
from pyfatfs.PyFatFS import PyFatFS

from emu.qemu.check_source_tree import inspect_qemu_source
from emu.qemu.ecc import jz4740_page_oob_ecc, jz4740_rs_encode
from emu.qemu.nand_fs import (
    LOGICAL_BLOCK_SIZE,
    PAGE_SIZE,
    PAGE_STRIDE,
    PAGES_PER_BLOCK,
    RAW_BLOCK_SIZE,
    list_nand_directory,
    mutate_nand_files,
    normalize_nand_path,
    read_nand_file,
    replace_fat_file,
    validate_nand_image,
)
from emu.qemu.nand_lock import NandImageInUseError, NandImageLease
from emu.qemu.nand_source import import_nand_source
from emu.qemu.system import (
    DEFAULT_BBK9588_FIRMWARE_PATCHES,
    DEFAULT_C200_BASE,
    DEFAULT_QEMU_CPU,
    DEFAULT_QEMU_EXECUTABLE,
    DEFAULT_QEMU_FIRMWARE_PATCHES,
    DEFAULT_QEMU_MACHINE,
    DEFAULT_QEMU_NAND_IMAGE,
    KNOWN_STALL_REGIONS,
    QEMU_BBK_AIC_PERF_PAYLOAD,
    QEMU_BBK_AUDIO_FORMAT_S16LE,
    QEMU_BBK_AUDIO_MAGIC,
    QEMU_BBK_FRAME_FORMAT_RGB565,
    QEMU_BBK_FRAME_HEADER,
    QEMU_BBK_FRAME_MAGIC,
    QEMU_BBK_PERF_FORMAT_AIC,
    QEMU_BBK_PERF_MAGIC,
    QemuPayload,
    QemuProcessBackend,
    QemuSystemConfig,
    _assign_windows_kill_on_close_job,
    _close_windows_job,
    build_bbk_qemu_config,
    build_qemu_command,
    classify_guest_pc,
    decode_cp0,
    find_qemu,
    find_workspace_file,
    migrate_legacy_nand_checkpoint,
    qemu_subprocess_env,
)
from emu.web.frontend_server import stream_upload_to_path
from emu.web.frontend_state import (
    DEFAULT_WEB_QEMU_ICOUNT,
    FRONTEND_INPUT_CALIBRATION_TARGETS,
    WS_AUDIO_HEADER,
    WS_AUDIO_MAGIC,
    FrontendState,
    display_to_touch_point,
    web_qemu_audio_options,
    web_qemu_timing_options,
)
from tests.qemu_audio_wav import (
    analyze_pcm_wav,
    finalize_qemu_wav_header,
    validate_audio_regression,
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json(port: int, method: str, path: str) -> dict[str, object]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    if res.status >= 400:
        raise RuntimeError(f"{method} {path} returned {res.status}: {data[:200]!r}")
    return json.loads(data.decode("utf-8") or "{}")


def _http_bytes(port: int, path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, data


def _place_test_nand_page(
    image: bytearray,
    *,
    page: int,
    page_data: bytes,
    valid: bool,
    page_size: int = 2048,
    spare_size: int = 64,
) -> None:
    stride = page_size + spare_size
    if len(page_data) != page_size:
        raise ValueError("test NAND page must match page_size")
    offset = page * stride
    oob = jz4740_page_oob_ecc(page_data)
    image[offset : offset + page_size] = page_data
    image[offset + page_size + 6 : offset + page_size + len(oob)] = oob[6:]
    if valid:
        image[offset + page_size + 2 : offset + page_size + 5] = b"\x00\x00\x00"


def _make_nand_file_manager_fixture(root: Path) -> tuple[Path, Path]:
    fat_image = root / "fat.img"
    nand_image = root / "nand.bin"
    volume_offset = 0x20 * 512
    volume_size = 32 * 1024 * 1024
    fat_image.write_bytes(b"\0" * (volume_offset + volume_size))
    formatter = PyFat(encoding="gbk", offset=volume_offset)
    formatter.mkfs(
        str(fat_image),
        fat_type=PyFat.FAT_TYPE_FAT16,
        size=volume_size,
    )
    formatter.close()
    with fat_image.open("r+b") as stream:
        stream.seek(volume_offset + 28)
        stream.write((0x20).to_bytes(4, "little"))
    fs = PyFatFS(
        str(fat_image),
        encoding="gbk",
        offset=volume_offset,
        preserve_case=True,
    )
    fs.makedir("/应用")
    fs.close()

    fat_size = fat_image.stat().st_size
    logical_blocks = (fat_size + LOGICAL_BLOCK_SIZE - 1) // LOGICAL_BLOCK_SIZE
    nand_image.write_bytes(b"\xff" * ((logical_blocks + 1) * RAW_BLOCK_SIZE))
    with fat_image.open("rb") as source, nand_image.open("r+b") as nand:
        for logical in range(logical_blocks):
            physical = logical + 1
            block = source.read(LOGICAL_BLOCK_SIZE)
            block += b"\0" * (LOGICAL_BLOCK_SIZE - len(block))
            for page in range(PAGES_PER_BLOCK):
                start = page * PAGE_SIZE
                nand.seek(physical * RAW_BLOCK_SIZE + page * PAGE_STRIDE)
                page_data = block[start : start + PAGE_SIZE]
                nand.write(page_data)
                parity = jz4740_page_oob_ecc(page_data, offset=4)
                nand.seek(physical * RAW_BLOCK_SIZE + page * PAGE_STRIDE + PAGE_SIZE + 4)
                nand.write(parity[4:])
            oob = physical * RAW_BLOCK_SIZE + PAGE_SIZE
            nand.seek(oob + 58)
            nand.write((1).to_bytes(2, "little"))
            nand.write(logical.to_bytes(4, "little"))
    return fat_image, nand_image


class _QTestClient:
    def __init__(
        self,
        executable: str,
        *,
        nand_image: Path | None = None,
        extra_args: tuple[str, ...] = (),
        hibernate_poweroff: bool = False,
        hibernate_wakeup: bool = False,
    ) -> None:
        command = [
            executable,
            "-M",
            "bbk9588,hibernate-poweroff=" +
            ("on" if hibernate_poweroff else "off") +
            ",hibernate-wakeup=" +
            ("on" if hibernate_wakeup else "off"),
            "-accel",
            "qtest",
            "-display",
            "none",
            "-serial",
            "none",
            "-monitor",
            "none",
            "-qtest",
            "stdio",
        ]
        if nand_image is not None:
            command.extend(["-drive", f"if=mtd,index=0,format=raw,file={nand_image.as_posix()}"])
        command.extend(extra_args)
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="ascii",
        )

    def __enter__(self) -> _QTestClient:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()

    def command(self, command: str) -> str:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("qtest stdio is unavailable")
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        response = self.process.stdout.readline().strip()
        if not response.startswith("OK"):
            raise RuntimeError(f"qtest command failed: {command!r}: {response!r}")
        return response

    def writel(self, address: int, value: int) -> None:
        self.command(f"writel 0x{address:x} 0x{value & 0xFFFFFFFF:x}")

    def writeb(self, address: int, value: int) -> None:
        self.command(f"writeb 0x{address:x} 0x{value & 0xFF:x}")

    def writew(self, address: int, value: int) -> None:
        self.command(f"writew 0x{address:x} 0x{value & 0xFFFF:x}")

    def readl(self, address: int) -> int:
        return int(self.command(f"readl 0x{address:x}").split()[1], 0)

    def readb(self, address: int) -> int:
        return int(self.command(f"readb 0x{address:x}").split()[1], 0)

    def readw(self, address: int) -> int:
        return int(self.command(f"readw 0x{address:x}").split()[1], 0)


class _FakeFrontendQemuBackend:
    def __init__(self) -> None:
        self.config = argparse.Namespace(machine="bbk9588")
        self.touches: list[tuple[int, int, bool]] = []
        self.completed = False
        self.trace_calls: list[str] = []

    def running(self) -> bool:
        return True

    def snapshot(self) -> dict[str, object]:
        return {"pc": "0x80017ba4", "running": True}

    def guest_queue_snapshot(self, global_va: int = 0x80473F6C) -> dict[str, object]:
        return {"global_addr": f"0x{global_va:08x}"}

    def guest_display_queue_snapshot(self, queue_va: int = 0x80825840) -> dict[str, object]:
        return {"queue_va": f"0x{queue_va:08x}"}

    def guest_gui_state_snapshot(self) -> dict[str, object]:
        ready = len(self.touches) >= len(FRONTEND_INPUT_CALIBRATION_TARGETS) * 2
        return {
            "active_object_ready": ready,
            "active_object_80474048": "0x80959670" if ready else "0x00000000",
        }

    def guest_scheduler_state_snapshot(self) -> dict[str, object]:
        return {"available": True}

    def guest_touch_device_snapshot(self) -> dict[str, object]:
        return {"available": True}

    def guest_runtime_table_snapshot(self) -> dict[str, object]:
        return {"available": True}

    def guest_display_surface_snapshot(self) -> dict[str, object]:
        return {"available": True}

    def guest_surface_trace_snapshot(self) -> dict[str, object]:
        self.trace_calls.append("surface")
        return {"available": True}

    def guest_storage_trace_snapshot(self) -> dict[str, object]:
        self.trace_calls.append("storage")
        return {"available": True}

    def guest_msc_trace_snapshot(self) -> dict[str, object]:
        self.trace_calls.append("msc")
        return {"available": True}

    def guest_fs_probe_trace_snapshot(self) -> dict[str, object]:
        self.trace_calls.append("fs_probe")
        return {"available": True}

    def guest_progress_trace_snapshot(self) -> dict[str, object]:
        self.trace_calls.append("progress")
        return {"available": True}

    def apply_touch_state(self, x: int, y: int, down: bool) -> dict[str, object]:
        self.touches.append((x, y, down))
        return {"applied": True, "source": "qemu-c-machine-chardev"}

    def enable_lcd_mirror(self) -> dict[str, object]:
        return {"source": "qemu-c-machine", "skipped": True}

    def settle_initial_gui(self) -> dict[str, object]:
        self.completed = True
        return {"source": "qemu-c-machine", "skipped_python_services": True}


def _fat16_boot_sector(
    *,
    hidden: int,
    sectors_per_cluster: int = 0x20,
    reserved: int = 1,
    fats: int = 2,
    root_entries: int = 0x200,
    total_sectors: int = 0xF7AE0,
    sectors_per_fat: int = 0x7C,
) -> bytes:
    boot = bytearray(512)
    boot[0:3] = b"\xeb<\x90"
    boot[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", boot, 0x0B, 512)
    boot[0x0D] = sectors_per_cluster
    struct.pack_into("<H", boot, 0x0E, reserved)
    boot[0x10] = fats
    struct.pack_into("<H", boot, 0x11, root_entries)
    struct.pack_into("<H", boot, 0x13, 0)
    boot[0x15] = 0xF8
    struct.pack_into("<H", boot, 0x16, sectors_per_fat)
    struct.pack_into("<I", boot, 0x1C, hidden)
    struct.pack_into("<I", boot, 0x20, total_sectors)
    boot[0x36:0x3B] = b"FAT16"
    boot[510:512] = b"\x55\xaa"
    return bytes(boot)


class QemuSystemCommandTests(unittest.TestCase):
    LEGACY_PYTHON_GDB_HOOK_TEST_FRAGMENTS = (
        "_gdb_",
        "dirent_path_match",
        "event_loop_empty",
        "fast_forward",
        "fastpath",
        "fat16_cluster_read",
        "file_read_context",
        "first_path_segment",
        "frontend_qemu_backend_status_and_stop",
        "probe_resource_path",
        "resource_" + "cache16",
        "resource_object",
        "resource_open",
        "settle_initial_gui",
        "storage_seed",
        "synthetic_desktop",
    )

    def setUp(self) -> None:
        method = self._testMethodName
        if any(fragment in method for fragment in self.LEGACY_PYTHON_GDB_HOOK_TEST_FRAGMENTS):
            self.skipTest("legacy Python/GDB hook or fastpath test; current default path is QEMU C machine modeling")

    @staticmethod
    def _frontend_state_without_qemu(args: argparse.Namespace) -> FrontendState:
        with mock.patch.object(FrontendState, "reset", return_value={}):
            return FrontendState(args)

    def test_decode_cp0_interrupt_state(self) -> None:
        decoded = decode_cp0(status=0x10000403, cause=0x00800400, epc=0x800043CC)

        self.assertEqual(decoded["exception"], "interrupt")
        self.assertTrue(decoded["ie"])
        self.assertTrue(decoded["exl"])
        self.assertEqual(decoded["pending_interrupts"], "0x04")
        self.assertEqual(decoded["interrupt_mask"], "0x04")
        self.assertFalse(decoded["cpu_interrupts_enabled"])
        self.assertEqual(decoded["pending_enabled_interrupts"], "0x00")
        self.assertEqual(decoded["epc"], "0x800043cc")

        accepting = decode_cp0(status=0x10000401, cause=0x00800400, epc=0x800043CC)
        self.assertTrue(accepting["cpu_interrupts_enabled"])
        self.assertEqual(accepting["pending_enabled_interrupts"], "0x04")

    def test_classifies_touch_mode_flag_getter(self) -> None:
        classified = classify_guest_pc("0x8005c384")
        self.assertIsInstance(classified, dict)
        assert classified is not None
        self.assertEqual(classified.get("region"), "touch-controller-mode-flag")

    def test_classifies_touch_gpio_level_helper(self) -> None:
        classified = classify_guest_pc("0x80059f6c")
        self.assertIsInstance(classified, dict)
        assert classified is not None
        self.assertEqual(classified.get("region"), "touch-gpio-level-helper")

    def test_classifies_uart_status_wait(self) -> None:
        classified = classify_guest_pc("0x80005cdc")
        self.assertIsInstance(classified, dict)
        assert classified is not None
        self.assertEqual(classified.get("region"), "uart-status-wait")

    def test_classifies_usb_udc_service(self) -> None:
        classified = classify_guest_pc("0x8000e658")
        self.assertIsInstance(classified, dict)
        assert classified is not None
        self.assertEqual(classified.get("region"), "usb-udc-service")

    def test_classifies_irq24_udc_service_loop(self) -> None:
        classified = classify_guest_pc("0x8000985c")
        self.assertIsInstance(classified, dict)
        assert classified is not None
        self.assertEqual(classified.get("region"), "irq24-udc-service-loop")

    def test_classifies_semaphore_wait_and_release(self) -> None:
        wait = classify_guest_pc("0x8000ba84")
        release = classify_guest_pc("0x8000bb98")
        self.assertIsInstance(wait, dict)
        self.assertIsInstance(release, dict)
        assert wait is not None
        assert release is not None
        self.assertEqual(wait.get("region"), "c200-semaphore-wait")
        self.assertEqual(release.get("region"), "c200-semaphore-release")

    def test_classifies_heap_lock_paths(self) -> None:
        free = classify_guest_pc("0x80006818")
        alloc = classify_guest_pc("0x8000766c")
        self.assertIsInstance(free, dict)
        self.assertIsInstance(alloc, dict)
        assert free is not None
        assert alloc is not None
        self.assertEqual(free.get("region"), "heap-free-with-semaphore")
        self.assertEqual(alloc.get("region"), "heap-alloc-with-semaphore")

    def test_classifies_low_power_wait(self) -> None:
        classified = classify_guest_pc("0x8005bcd8")
        self.assertIsInstance(classified, dict)
        assert classified is not None
        self.assertEqual(classified.get("region"), "low-power-wait")

    def test_classifies_resource_object_release(self) -> None:
        classified = classify_guest_pc("0x80170c90")
        self.assertIsInstance(classified, dict)
        assert classified is not None
        self.assertEqual(classified.get("region"), "resource-object-release")

    def test_classifies_resource_release_locked_wrapper(self) -> None:
        classified = classify_guest_pc("0x8017a94c")
        self.assertIsInstance(classified, dict)
        assert classified is not None
        self.assertEqual(classified.get("region"), "resource-release-locked-wrapper")

    def test_classifies_firmware_fat16_resource_cache_lookup_as_diagnostic(self) -> None:
        classified = classify_guest_pc("0x8017ca10")
        self.assertIsInstance(classified, dict)
        assert classified is not None
        self.assertEqual(classified.get("region"), "firmware-fat16-resource-cache-lookup")
        description = str(classified.get("description"))
        self.assertIn("diagnostic PC classification", description)
        self.assertNotIn("cache miss-load", description)
        self.assertNotIn("bridge", description.lower())

    def test_classifies_exception_report_tcu_restore(self) -> None:
        classified = classify_guest_pc("0x80004a48")
        self.assertIsInstance(classified, dict)
        assert classified is not None
        self.assertEqual(classified.get("region"), "c200-exception-report-tcu-restore")

    def test_classifies_gui_event_and_irq_return_paths(self) -> None:
        cases = {
            "0x80005208": "c200-irq-handler-return",
            "0x8001aa18": "touch-irq-ack-return",
            "0x800dc588": "gui-event-poller",
            "0x8012bbb8": "gui-tick-event-service",
            "0x8012ccfc": "event-loop-empty-return",
        }
        for pc, region in cases.items():
            with self.subTest(pc=pc):
                classified = classify_guest_pc(pc)
                self.assertIsInstance(classified, dict)
                assert classified is not None
                self.assertEqual(classified.get("region"), region)

    def test_known_stall_descriptions_do_not_claim_ready_magic(self) -> None:
        descriptions = "\n".join(row[3] for row in KNOWN_STALL_REGIONS)

        self.assertNotIn("stub now supplies this ready bit", descriptions)
        self.assertNotIn("graphics stub supplies this ready bit", descriptions)
        self.assertIn("command completion", descriptions)
        self.assertIn("the bbk9588 LCD status model sets this from controller/frame activity", descriptions)
        self.assertNotIn("optional lcd-status only as a diagnostic override", descriptions)
        self.assertNotIn("optional graphics-status only as a diagnostic override", descriptions)
        self.assertNotIn("cache miss-load", descriptions)
        self.assertNotIn("semaphore-fastpath", descriptions)
        self.assertNotIn("fastpaths serve", descriptions)
        self.assertNotIn("file reads through GDB", descriptions)

    def test_builds_c200_loader_with_physical_load_and_virtual_pc(self) -> None:
        image = Path("C200.bin")
        config = QemuSystemConfig(boot_payload=QemuPayload(image, 0x4000), boot_pc=0x80004000)

        command = build_qemu_command(config)

        self.assertIn("qemu-system-mipsel", command[0])
        self.assertIn("-accel", command)
        self.assertIn("tcg,thread=multi,tb-size=256", command)
        image_qemu = str(image.resolve()).replace("\\", "/")
        self.assertIn(f"loader,file={image_qemu},addr=0x4000,force-raw=on", command)
        self.assertIn("loader,addr=0x80004000,cpu-num=0", command)

    def test_builds_bbk9588_machine_with_raw_kernel_loader(self) -> None:
        image = Path("C200.bin")
        config = QemuSystemConfig(
            boot_payload=QemuPayload(image, 0x4000),
            boot_pc=0x80004000,
            machine="bbk9588",
        )

        command = build_qemu_command(config)

        self.assertIn("-M", command)
        self.assertIn("bbk9588,firmware-phys=0x4000,reset-pc=0x80004000", command)
        self.assertIn("-kernel", command)
        self.assertIn(str(image.resolve()), command)
        self.assertNotIn("loader,addr=0x80004000,cpu-num=0", command)

    def test_builds_bbk9588_nand_machine_with_raw_first_stage_by_default(self) -> None:
        nand = Path("build") / "bbk9588_nand_loader0_uboot40_fat_page1c40_root512_ftloob.bin"

        config = build_bbk_qemu_config(
            nand_image=nand,
            machine="bbk9588",
        )

        command = build_qemu_command(config)

        machine_arg = command[command.index("-M") + 1]
        self.assertIn("bbk9588", machine_arg)
        self.assertIn("bootrom-nand=on", machine_arg)
        self.assertNotIn("bootrom-page=0x0", machine_arg)
        self.assertNotIn("bootrom-size=0x2000", machine_arg)
        self.assertNotIn("bootrom-fat-kernel=on", machine_arg)
        self.assertNotIn("legacy-storage-bridge=on", machine_arg)
        self.assertNotIn("tcu-period-ms=", machine_arg)
        self.assertIn("firmware-phys=0x0", machine_arg)
        self.assertIn("reset-pc=0x80000004", machine_arg)
        self.assertIn("-drive", command)
        self.assertNotIn("-kernel", command)
        self.assertFalse(any("C200.bin" in arg for arg in command), command)
        self.assertFalse(any("u_boot_9588_4740.bin" in arg for arg in command), command)
        self.assertFalse(any(arg.startswith("loader,file=") for arg in command), command)
        drive = command[command.index("-drive") + 1]
        self.assertIn("cache=writeback", drive)
        self.assertIn(nand.resolve().as_posix(), drive)
        self.assertNotIn("qemu_nand_runs", drive)

    def test_bbk9588_defaults_to_dedicated_jz4740_cpu(self) -> None:
        config = build_bbk_qemu_config(nand_image=Path("build") / "bbk9588_nand.bin")

        command = build_qemu_command(config)

        self.assertEqual(DEFAULT_QEMU_CPU, "JZ4740")
        self.assertEqual(command[command.index("-cpu") + 1], DEFAULT_QEMU_CPU)

    def test_jz4740_cpu_model_matches_documented_cp0_geometry(self) -> None:
        overlay = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        cpu_source = (overlay / "target" / "mips" / "cpu.c").read_text(
            encoding="utf-8"
        )
        board_source = (overlay / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        start = cpu_source.index("static const mips_def_t jz4740_cpu_def")
        end = cpu_source.index("static void mips_cpu_reset_hold", start)
        model = cpu_source[start:end]

        self.assertIn('.name = "JZ4740"', model)
        self.assertIn(".CP0_PRid = 0x0ad0024f", model)
        self.assertIn("(MMU_TYPE_R4000 << CP0C0_MT)", model)
        self.assertNotIn("CP0C0_AR", model)
        self.assertIn("(31 << CP0C1_MMU)", model)
        self.assertIn("(1 << CP0C1_IS) | (4 << CP0C1_IL) | (3 << CP0C1_IA)", model)
        self.assertIn("(1 << CP0C1_DS) | (4 << CP0C1_DL) | (3 << CP0C1_DA)", model)
        self.assertIn(".insn_flags = CPU_MIPS32R1 | ASE_MXU", model)
        self.assertNotIn("CP0C1_FP", model)
        self.assertNotIn("ASE_MIPS16", model)
        self.assertIn("mips_register_cpudef_type(&jz4740_cpu_def);", cpu_source)
        self.assertIn('MIPS_CPU_TYPE_NAME("JZ4740")', board_source)

    def test_qemu_jz4740_cpu_reports_expected_cp0_configuration(self) -> None:
        executable = os.environ.get("BBK9588_QEMU") or find_qemu()
        if executable is None:
            self.skipTest("qemu-system-mipsel is not installed")

        result = subprocess.run(
            [
                executable,
                "-M",
                "bbk9588",
                "-cpu",
                DEFAULT_QEMU_CPU,
                "-accel",
                "tcg",
                "-S",
                "-display",
                "none",
                "-serial",
                "none",
                "-monitor",
                "stdio",
            ],
            input="\ninfo registers\nquit\n",
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=qemu_subprocess_env(executable),
        )
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("PRId 0x0ad0024f", output)
        self.assertIn("Config0 0x80000082 Config1 0xbe63318a", output)
        self.assertIn("Config2 0x80000000 Config3 0x00000000", output)

    def test_bbk9588_bootrom_source_does_not_load_fat_kernel(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "qemu"
            / "overlay"
            / "hw"
            / "mips"
            / "bbk9588.c"
        ).read_text(encoding="utf-8")
        start = source.index("static bool bbk9588_bootrom_load_from_nand")
        end = source.index("static void bbk9588_tcu_irq_handler", start)
        bootrom_load = source[start:end]
        helper_start = source.index("static bool bbk9588_bootrom_nand_page_valid")
        helper_end = source.index("static bool bbk9588_bootrom_load_raw_payload", helper_start)
        bootrom_helpers = source[helper_start:helper_end]

        self.assertIn("bbk9588_bootrom_load_first_stage", bootrom_load)
        self.assertIn("BBK9588_BOOTROM_BACKUP_NAND_ADDR", bootrom_load)
        self.assertIn("bbk9588_bootrom_nand_page_valid", bootrom_helpers)
        self.assertIn("bbk9588_bootrom_nand_area_has_valid_page", bootrom_helpers)
        self.assertIn("spare_off + 2", bootrom_helpers)
        self.assertIn("spare_off + 3", bootrom_helpers)
        self.assertIn("spare_off + 4", bootrom_helpers)
        self.assertIn("spare_off + 6", bootrom_helpers)
        self.assertIn("jz4740_rs_decode", bootrom_helpers)
        self.assertIn("jz4740_rs_apply_correction", bootrom_helpers)
        self.assertIn("!bbk9588_bootrom_nand_area_has_valid_page", bootrom_helpers)
        self.assertNotIn("bootrom-fat-kernel", source)
        self.assertNotIn("BOOTROM_KERNEL_PATH", source)
        self.assertNotIn("bootrom_fat_kernel", source)
        self.assertNotIn("bbk9588_bootrom_load_fat_kernel", source)
        self.assertNotIn("FAT kernel", source)
        self.assertNotIn("bbk9588_find_fat16_layout", bootrom_load)
        self.assertNotIn("BBK9588_BOOTROM_MAGIC", source)
        self.assertNotIn('"BBKUBOOT"', source)
        self.assertNotIn("bbk9588_bootrom_load_legacy_payload", source)
        self.assertNotIn("memcmp(header", source)
        self.assertIn("bbk9588_bootrom_load_raw_payload", source)

    def test_qemu_bbk9588_bootrom_tries_backup_when_normal_area_is_erased(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        page_size = 2048
        spare_size = 64
        stride = page_size + spare_size
        backup_page = 0x2000 // page_size
        backup_pages = 0x2000 // page_size

        with tempfile.TemporaryDirectory() as tmp:
            nand = Path(tmp) / "nand-backup-only.bin"
            image = bytearray(b"\xff" * ((backup_page + backup_pages) * stride))
            stage = bytearray(b"\x00" * 0x2000)
            struct.pack_into("<I", stage, 4, 0x1000FFFF)  # branch to self at reset PC.
            for page in range(backup_pages):
                src = page * page_size
                _place_test_nand_page(
                    image,
                    page=backup_page + page,
                    page_data=bytes(stage[src : src + page_size]),
                    valid=True,
                )
            nand.write_bytes(image)

            config = build_bbk_qemu_config(
                nand_image=nand,
                executable=qemu,
                boot_mode="nand",
                serial="none",
                monitor="none",
                timeout_seconds=1.5,
            )
            backend = QemuProcessBackend(config)
            try:
                backend.start()
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if any("from NAND backup address 0x00002000" in line for line in backend.stderr_tail):
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    any("from NAND backup address 0x00002000" in line for line in backend.stderr_tail),
                    backend.stderr_tail,
                )
            finally:
                backend.stop()

    def test_qemu_bbk9588_bootrom_tries_backup_when_normal_oob_is_invalid(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        page_size = 2048
        spare_size = 64
        stride = page_size + spare_size
        backup_page = 0x2000 // page_size
        boot_pages = 0x2000 // page_size

        with tempfile.TemporaryDirectory() as tmp:
            nand = Path(tmp) / "nand-invalid-normal-oob.bin"
            image = bytearray(b"\xff" * ((backup_page + boot_pages) * stride))
            stage = bytearray(b"\x00" * 0x2000)
            struct.pack_into("<I", stage, 4, 0x1000FFFF)  # branch to self at reset PC.
            for page in range(boot_pages):
                src = page * page_size
                page_data = bytes(stage[src : src + page_size])
                _place_test_nand_page(image, page=page, page_data=page_data, valid=False)
                _place_test_nand_page(
                    image,
                    page=backup_page + page,
                    page_data=page_data,
                    valid=True,
                )
            nand.write_bytes(image)

            config = build_bbk_qemu_config(
                nand_image=nand,
                executable=qemu,
                boot_mode="nand",
                serial="none",
                monitor="none",
                timeout_seconds=1.5,
            )
            backend = QemuProcessBackend(config)
            try:
                backend.start()
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if any("from NAND backup address 0x00002000" in line for line in backend.stderr_tail):
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    any("from NAND backup address 0x00002000" in line for line in backend.stderr_tail),
                    backend.stderr_tail,
                )
            finally:
                backend.stop()

    def test_qemu_bbk9588_bootrom_corrects_four_normal_area_rs_errors(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        page_size = 2048
        spare_size = 64
        stride = page_size + spare_size
        boot_pages = 0x2000 // page_size

        with tempfile.TemporaryDirectory() as tmp:
            nand = Path(tmp) / "nand-correctable-normal.bin"
            image = bytearray(b"\xff" * (boot_pages * stride))
            stage = bytearray(b"\x00" * 0x2000)
            struct.pack_into("<I", stage, 4, 0x1000FFFF)
            for page in range(boot_pages):
                src = page * page_size
                _place_test_nand_page(
                    image,
                    page=page,
                    page_data=bytes(stage[src : src + page_size]),
                    valid=True,
                )
            for byte in (0, 64, 128, 192):
                image[byte] ^= 1
            nand.write_bytes(image)

            config = build_bbk_qemu_config(
                nand_image=nand,
                executable=qemu,
                boot_mode="nand",
                serial="none",
                monitor="none",
                timeout_seconds=1.5,
            )
            backend = QemuProcessBackend(config)
            try:
                backend.start()
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if any("from NAND normal address 0x00000000" in line for line in backend.stderr_tail):
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    any("corrected 4 NAND RS symbol errors" in line for line in backend.stderr_tail),
                    backend.stderr_tail,
                )
                self.assertTrue(
                    any("from NAND normal address 0x00000000" in line for line in backend.stderr_tail),
                    backend.stderr_tail,
                )
            finally:
                backend.stop()

    def test_qemu_bbk9588_bootrom_stops_at_first_invalid_page(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        page_size = 2048
        spare_size = 64
        stride = page_size + spare_size
        boot_pages = 0x2000 // page_size

        with tempfile.TemporaryDirectory() as tmp:
            nand = Path(tmp) / "nand-short-first-stage.bin"
            image = bytearray(b"\xff" * (boot_pages * stride))
            stage = bytearray(b"\x00" * page_size)
            struct.pack_into("<I", stage, 4, 0x1000FFFF)
            _place_test_nand_page(
                image,
                page=0,
                page_data=bytes(stage),
                valid=True,
            )
            nand.write_bytes(image)

            config = build_bbk_qemu_config(
                nand_image=nand,
                executable=qemu,
                boot_mode="nand",
                serial="none",
                monitor="none",
                timeout_seconds=1.5,
            )
            backend = QemuProcessBackend(config)
            try:
                backend.start()
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if any("loaded 2048-byte first-stage" in line for line in backend.stderr_tail):
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    any("loaded 2048-byte first-stage" in line for line in backend.stderr_tail),
                    backend.stderr_tail,
                )
                self.assertTrue(
                    any("from NAND normal address 0x00000000" in line for line in backend.stderr_tail),
                    backend.stderr_tail,
                )
            finally:
                backend.stop()

    def test_qemu_bbk9588_bootrom_falls_back_after_five_normal_area_rs_errors(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        page_size = 2048
        spare_size = 64
        stride = page_size + spare_size
        backup_page = 0x2000 // page_size
        boot_pages = 0x2000 // page_size

        with tempfile.TemporaryDirectory() as tmp:
            nand = Path(tmp) / "nand-uncorrectable-normal.bin"
            image = bytearray(b"\xff" * ((backup_page + boot_pages) * stride))
            stage = bytearray(b"\x00" * 0x2000)
            struct.pack_into("<I", stage, 4, 0x1000FFFF)
            for page in range(boot_pages):
                src = page * page_size
                page_data = bytes(stage[src : src + page_size])
                _place_test_nand_page(image, page=page, page_data=page_data, valid=True)
                _place_test_nand_page(
                    image,
                    page=backup_page + page,
                    page_data=page_data,
                    valid=True,
                )
            for byte in (0, 64, 128, 192, 256):
                image[byte] ^= 1
            nand.write_bytes(image)

            config = build_bbk_qemu_config(
                nand_image=nand,
                executable=qemu,
                boot_mode="nand",
                serial="none",
                monitor="none",
                timeout_seconds=1.5,
            )
            backend = QemuProcessBackend(config)
            try:
                backend.start()
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if any("from NAND backup address 0x00002000" in line for line in backend.stderr_tail):
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    any("uncorrectable RS ECC" in line for line in backend.stderr_tail),
                    backend.stderr_tail,
                )
                self.assertTrue(
                    any("from NAND backup address 0x00002000" in line for line in backend.stderr_tail),
                    backend.stderr_tail,
                )
            finally:
                backend.stop()

    def test_bbk9588_source_removes_legacy_storage_bridge_and_fat_scan(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        dma_bridge = (root / "hw" / "dma" / "bbk9588_dma_bridge.c").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("msc_oob_lba", source)
        msc_start = dma_bridge.index("static bool bridge_msc_dma_transfer")
        msc_end = dma_bridge.index("static void bridge_dmac_trace", msc_start)
        msc_dma = dma_bridge[msc_start:msc_end]
        self.assertNotIn("nand_dev", msc_dma)
        self.assertNotIn("Bbk9588NandState", msc_dma)
        self.assertNotIn("bbk9588_read_ftl_logical_sector", source)
        self.assertNotIn("bbk9588_write_ftl_logical_sector", source)
        self.assertNotIn("legacy-storage-bridge", source)
        self.assertNotIn("legacy_storage_bridge", source)
        self.assertNotIn("Bbk9588Fat16Layout", source)
        self.assertNotIn("fat16_layout", source)
        self.assertNotIn("bbk9588_find_fat16_layout", source)
        self.assertNotIn("bbk9588_fat16_layout_from_sector", source)
        self.assertNotIn("bbk9588_read_logical_sector", source)
        self.assertNotIn("bbk9588_write_logical_sector", source)
        self.assertNotIn("bbk9588_storage_read_fat_sector", source)
        self.assertNotIn("bbk9588_storage_read_cluster", source)
        self.assertNotIn("bbk9588_storage_write_cluster", source)
        self.assertNotIn("bbk9588_storage_first_dirent_for_pattern", source)
        self.assertNotIn("logical FAT" + " sector reads", source)
        self.assertNotIn("bbk9588-cluster" + "-cache", source)
        self.assertIn("Trace bbk9588 NAND/MSC page and DMA diagnostics", source)
        self.assertNotIn("bbk9588-diagnostic-guest-object-snapshot", source)
        self.assertNotIn("bbk9588-diagnostic-guest-storage-snapshot", source)
        self.assertNotIn("0x00b714cc", source)
        self.assertNotIn("0x0095a26c", source)
        self.assertNotIn("0x003695b8", source)
        self.assertNotIn("bbk9588-msc-read", source)
        self.assertNotIn("at_icon", source)
        self.assertNotIn("bbk9588-guest-cache", source)
        self.assertNotIn("bbk9588-guest-storage-cache", source)

    def test_release_readme_documents_frontend_calibration_as_explicit_helper(self) -> None:
        readme = (
            Path(__file__).resolve().parents[1]
            / "packaging"
            / "RELEASE_README.md"
        ).read_text(encoding="utf-8")

        self.assertNotIn("--no-auto-calibration", readme)
        self.assertNotIn("auto-calibration", readme)
        self.assertIn("--frontend-input-calibration", readme)
        self.assertIn("默认关闭", readme)
        self.assertIn("Web smoke test", readme)

    def test_qemu_python_backing_fat_caches_are_diagnostic_named(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "emu"
            / "qemu"
            / "system.py"
        ).read_text(encoding="utf-8")

        self.assertIn("Diagnostic-only backing image caches", source)
        self.assertIn("diagnostic_fat16_layout_cache", source)
        self.assertIn("diagnostic_fat16_long_name_alias_cache", source)
        self.assertIn("diagnostic_nand_fat_sector0_cache", source)
        self.assertIn("diagnostic_backing_sector_cache", source)
        self.assertNotIn("self.fat16_layout_cache", source)
        self.assertNotIn("self.fat16_long_name_alias_cache", source)
        self.assertNotIn("self.nand_fat_sector0_cache", source)
        self.assertNotIn("self.backing_sector_cache", source)

    def test_bbk9588_event_queue_source_is_diagnostic_mirror_only(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "misc" / "bbk9588_diag.c").read_text(
            encoding="utf-8"
        )
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        self.assertIn("static void diag_mirror_input", source)
        self.assertIn("bbk9588_diag_queue_input", source)
        self.assertNotIn("event_queue_pop_to_record", source)
        self.assertNotIn("record + 0x04 + word * 4", source)
        self.assertNotIn("EVENT_QUEUE_SLOTS", board)

    def test_bbk9588_fs_probe_helper_is_storage_trace_gated(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "qemu"
            / "overlay"
            / "target"
            / "mips"
            / "tcg"
            / "op_helper.c"
        ).read_text(encoding="utf-8")
        start = source.index("void helper_bbk9588_fs_probe")
        end = source.index("target_ulong helper_rotx", start)
        helper = source[start:end]

        self.assertIn("!env->bbk9588_storage_trace", helper)
        self.assertIn("bbk9588_probe_write_u32(BBK9588_FS_PROBE_VA + 0x00", helper)

    def test_bbk9588_progress_trace_timer_not_named_legacy_python_resource_hook(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        diag = (root / "hw" / "misc" / "bbk9588_diag.c").read_text(
            encoding="utf-8"
        )

        self.assertIn("progress_trace_timer", source)
        self.assertIn("progress_trace_period_ms", source)
        self.assertIn('"progress-trace-period-ms"', source)
        self.assertIn("Trace bbk9588 CPU/IRQ/runtime progress into diagnostic guest RAM", source)
        self.assertIn("bbk9588_progress_trace_schedule(board);", source)
        self.assertIn("bbk9588_diag_progress_record(board->diag, 2);", source)
        self.assertIn("void bbk9588_diag_progress_record", diag)
        self.assertIn("uint32_t progress_seq;", diag)
        self.assertNotIn("BBK9588_PROGRESS_TRACE_MAGIC", source)
        self.assertNotIn("CPU/IRQ/resource progress", source)
        self.assertNotIn("legacy_python_resource_hook_timer", source)
        self.assertNotIn("legacy_python_resource_hook_period_ms", source)
        self.assertNotIn('"resource-pump-period-ms"', source)

    def test_bbk9588_tcu_period_property_is_diagnostic_performance_only(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "qemu"
            / "overlay"
            / "hw"
            / "mips"
            / "bbk9588.c"
        ).read_text(encoding="utf-8")
        start = source.index('oc, "tcu-period-ms"')
        end = source.index('oc, "progress-trace-period-ms"', start)
        property_block = source[start:end]

        self.assertIn("Diagnostic/performance TCU sampling period", property_block)
        self.assertIn("hardware correctness must not depend", property_block)
        self.assertNotIn("TCU compare interrupt period", property_block)

    def test_bbk9588_source_has_no_synthetic_irq24_timer(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "qemu"
            / "overlay"
            / "hw"
            / "mips"
            / "bbk9588.c"
        ).read_text(encoding="utf-8")

        self.assertNotIn("irq24_timer", source)
        self.assertNotIn("irq24_period_ms", source)
        self.assertNotIn('"irq24-period-ms"', source)
        self.assertNotIn("bbk9588_irq24_schedule", source)
        self.assertNotIn("bbk9588_irq24_raise_pending", source)
        self.assertNotIn("bbk9588_irq24_timer_cb", source)

    def test_bbk9588_touch_diagnostics_do_not_expose_machine_frontend_input_calibration(self) -> None:
        root = Path(__file__).resolve().parents[1]
        stale_token = "touch_" + "autocal"
        c_source = (
            root / "qemu" / "overlay" / "hw" / "mips" / "bbk9588.c"
        ).read_text(encoding="utf-8")
        system_source = (root / "emu" / "qemu" / "system.py").read_text(encoding="utf-8")
        probe_source = (root / "tests" / "run_qemu_system_probe.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn(stale_token, c_source)
        self.assertNotIn(stale_token, system_source)
        self.assertNotIn(stale_token, probe_source)
        self.assertNotIn('"touch-' + 'autocal"', c_source)
        self.assertIn('"reserved_04"', system_source)
        self.assertIn('"reserved_08"', system_source)
        self.assertIn('"reserved_0c"', system_source)
        self.assertIn('"reserved_04"', probe_source)
        self.assertIn('"reserved_08"', probe_source)
        self.assertIn('"reserved_0c"', probe_source)

    def test_bbk9588_touch_trace_is_explicit_diagnostic(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        diag = (root / "hw" / "misc" / "bbk9588_diag.c").read_text(
            encoding="utf-8"
        )

        self.assertIn("bool touch_enabled;", diag)
        self.assertIn("void bbk9588_diag_touch_record", diag)
        self.assertIn("!s || !s->touch_enabled || !board ||", diag)
        self.assertIn("TOUCH_TRACE_VA", diag)
        self.assertIn('object_class_property_add_bool(oc, "touch-trace"', board)
        self.assertIn("bbk9588_get_touch_trace", board)
        self.assertIn("bbk9588_set_touch_trace", board)
        self.assertIn("bbk9588_diag_touch_record", board)
        self.assertNotIn("BBK9588_TOUCH_TRACE_VA", board)
        self.assertNotIn("static void bbk9588_touch_trace_update", board)

    def test_builds_bbk9588_uboot_machine_with_raw_first_stage_by_default(self) -> None:
        nand = Path("build") / "bbk9588_nand_uboot40_fat_page1c40_root512_ftloob.bin"

        config = build_bbk_qemu_config(
            boot_mode="uboot",
            nand_image=nand,
            machine="bbk9588",
        )

        command = build_qemu_command(config)

        machine_arg = command[command.index("-M") + 1]
        self.assertIn("bbk9588", machine_arg)
        self.assertIn("bootrom-nand=on", machine_arg)
        self.assertNotIn("bootrom-page=0x40", machine_arg)
        self.assertNotIn("bootrom-size=0x80000", machine_arg)
        self.assertIn("firmware-phys=0x0", machine_arg)
        self.assertIn("reset-pc=0x80000004", machine_arg)
        self.assertIn("-drive", command)
        self.assertNotIn("-kernel", command)
        self.assertFalse(any("C200.bin" in arg for arg in command), command)
        self.assertFalse(any("u_boot_9588_4740.bin" in arg for arg in command), command)
        self.assertFalse(any(arg.startswith("loader,file=") for arg in command), command)

    def test_builds_bbk9588_uboot_machine_with_explicit_diagnostic_bootrom_copy(self) -> None:
        nand = Path("build") / "bbk9588_nand_uboot40_fat_page1c40_root512_ftloob.bin"

        config = build_bbk_qemu_config(
            boot_mode="uboot",
            nand_image=nand,
            machine="bbk9588",
            bbk_machine_options=(
                "bootrom-page=0x40",
                "bootrom-size=0x80000",
                "firmware-phys=0x900000",
                "reset-pc=0x80900000",
            ),
        )

        command = build_qemu_command(config)

        machine_arg = command[command.index("-M") + 1]
        self.assertIn("bootrom-nand=on", machine_arg)
        self.assertIn("bootrom-page=0x40", machine_arg)
        self.assertIn("bootrom-size=0x80000", machine_arg)
        self.assertIn("firmware-phys=0x900000", machine_arg)
        self.assertIn("reset-pc=0x80900000", machine_arg)
        self.assertNotIn("-kernel", command)

    def test_builds_bbk9588_uboot_machine_with_explicit_direct_bootloader_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            boot = root / "u_boot_9588_4740.bin"
            boot.write_bytes(b"\0" * 4)

            config = build_bbk_qemu_config(
                boot_mode="uboot",
                image=boot,
                machine="bbk9588",
            )

        command = build_qemu_command(config)

        machine_arg = command[command.index("-M") + 1]
        self.assertIn("bbk9588", machine_arg)
        self.assertIn("firmware-phys=0x900000", machine_arg)
        self.assertIn("reset-pc=0x80900000", machine_arg)
        self.assertIn("-kernel", command)
        self.assertIn(str(boot.resolve()), command)
        self.assertFalse(any("C200.bin" in arg for arg in command), command)
        self.assertFalse(any(arg.startswith("loader,file=") for arg in command), command)

    def test_builds_bbk9588_uboot_machine_with_explicit_legacy_c200_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            boot = root / "u_boot_9588_4740.bin"
            payload = root / "C200.bin"
            boot.write_bytes(b"\0" * 4)
            payload.write_bytes(b"\0" * 4)

            config = build_bbk_qemu_config(
                boot_mode="uboot",
                image=boot,
                payload=payload,
                machine="bbk9588",
            )

        command = build_qemu_command(config)

        self.assertIn("-kernel", command)
        self.assertIn(str(boot.resolve()), command)
        payload_qemu = str(payload.resolve()).replace("\\", "/")
        self.assertIn(f"loader,file={payload_qemu},addr=0x4000,force-raw=on", command)

    def test_builds_bbk9588_machine_with_nand_mtd_drive(self) -> None:
        image = Path("C200.bin")
        nand = Path("build") / "bbk9588_nand.bin"
        config = QemuSystemConfig(
            boot_payload=QemuPayload(image, 0x4000),
            boot_pc=0x80004000,
            machine="bbk9588",
            nand_image=nand,
        )

        command = build_qemu_command(config)

        nand_qemu = str(nand.resolve()).replace("\\", "/")
        self.assertNotIn("-initrd", command)
        machine_arg = command[command.index("-M") + 1]
        self.assertTrue(machine_arg.startswith("bbk9588,"), machine_arg)
        self.assertIn("-drive", command)
        self.assertIn(
            f"if=mtd,index=0,format=raw,cache=writeback,file={nand_qemu}",
            command,
        )

    def test_builds_bbk9588_machine_with_input_chardev(self) -> None:
        image = Path("C200.bin")
        config = QemuSystemConfig(
            boot_payload=QemuPayload(image, 0x4000),
            boot_pc=0x80004000,
            machine="bbk9588",
            bbk_input="socket,id=bbk9588-input,host=127.0.0.1,port=12345,server=on,wait=off,nodelay=on",
            bbk_frame="socket,id=bbk9588-frame,host=127.0.0.1,port=12346,server=on,wait=off,nodelay=on",
        )

        command = build_qemu_command(config)

        machine_arg = command[command.index("-M") + 1]
        self.assertIn("input-chardev=bbk9588-input", machine_arg)
        self.assertIn("frame-chardev=bbk9588-frame", machine_arg)
        self.assertIn("-chardev", command)
        self.assertIn(
            "socket,id=bbk9588-input,host=127.0.0.1,port=12345,server=on,wait=off,nodelay=on",
            command,
        )
        self.assertIn(
            "socket,id=bbk9588-frame,host=127.0.0.1,port=12346,server=on,wait=off,nodelay=on",
            command,
        )

    def test_builds_bbk9588_machine_with_extra_machine_options(self) -> None:
        image = Path("C200.bin")
        config = QemuSystemConfig(
            boot_payload=QemuPayload(image, 0x4000),
            boot_pc=0x80004000,
            machine="bbk9588",
            bbk_machine_options=(
                "cpu-irq-output=on",
                "progress-trace=on",
                "progress-trace-period-ms=100",
                "tcu-period-ms=1",
                "lcd-refresh-period-ms=100",
            ),
        )

        command = build_qemu_command(config)

        self.assertIn(
            "bbk9588,cpu-irq-output=on,progress-trace=on,progress-trace-period-ms=100,tcu-period-ms=1,lcd-refresh-period-ms=100,firmware-phys=0x4000,reset-pc=0x80004000",
            command,
        )

    def test_hibernate_wakeup_uses_pin_reset_without_gpio_key(self) -> None:
        config = build_bbk_qemu_config(
            boot_mode="nand",
            hibernate_wakeup=True,
        )

        self.assertIn("hibernate-wakeup=on", config.bbk_machine_options)
        self.assertFalse(config.startup_power_key)
        self.assertIn(
            "hibernate-wakeup=on",
            build_qemu_command(config)[build_qemu_command(config).index("-M") + 1],
        )

    def test_startup_power_key_release_waits_for_first_frame(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "emu/qemu/system.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("if self.latest_frame_chardev is not None:", source)
        self.assertIn("time.monotonic() < deadline", source)

    def test_bbk9588_default_patches_skip_c_device_stubs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "C200.bin"
            image.write_bytes(b"\0" * 0x200000)

            config = build_bbk_qemu_config(boot_mode="c200", image=image, machine="bbk9588")

        self.assertEqual(config.firmware_patches, DEFAULT_BBK9588_FIRMWARE_PATCHES)
        self.assertEqual(config.firmware_patches, ())
        self.assertNotIn("c200-lcd-ready", config.firmware_patches)
        self.assertNotIn("c200-uart-ready", config.firmware_patches)
        self.assertNotIn("c200-cp0-irq-enable-noop", config.firmware_patches)
        self.assertNotIn("c200-no-event-poll-empty", config.firmware_patches)
        self.assertNotIn("c200-wait-noop", config.firmware_patches)

    def test_bbk9588_rejects_removed_legacy_machine_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "C200.bin"
            image.write_bytes(b"\0" * 0x200000)

            with self.assertRaisesRegex(ValueError, "removed bbk9588 machine option"):
                build_bbk_qemu_config(
                    boot_mode="c200",
                    image=image,
                    machine="bbk9588",
                    bbk_machine_options=("semaphore-fastpath=off",),
                )

    def test_malta_default_patches_stay_full_compatibility_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "C200.bin"
            image.write_bytes(b"\0" * 0x200000)

            config = build_bbk_qemu_config(boot_mode="c200", image=image, machine="malta")

        self.assertEqual(config.firmware_patches, DEFAULT_QEMU_FIRMWARE_PATCHES)
        self.assertIn("c200-lcd-ready", config.firmware_patches)

    def test_builds_command_with_gdb_stub_when_requested(self) -> None:
        image = Path("C200.bin")
        config = QemuSystemConfig(
            boot_payload=QemuPayload(image, 0x4000),
            boot_pc=0x80004000,
            gdb="tcp:127.0.0.1:1234",
        )

        command = build_qemu_command(config)

        self.assertIn("-gdb", command)
        self.assertIn("tcp:127.0.0.1:1234", command)

    def test_builds_command_with_qemu_plugin_when_requested(self) -> None:
        image = Path("C200.bin")
        plugin = Path("build") / "bbk9588_qemu_fastpath.dll"
        config = QemuSystemConfig(
            boot_payload=QemuPayload(image, 0x4000),
            boot_pc=0x80004000,
            plugins=(plugin,),
        )

        command = build_qemu_command(config)

        plugin_qemu = str(plugin.resolve()).replace("\\", "/")
        self.assertIn("-plugin", command)
        self.assertIn(f"file={plugin_qemu}", command)

    def test_bbk9588_guest_ips_uses_qemu_tb_counter_and_frame_metrics_packet(self) -> None:
        root = Path(__file__).resolve().parents[1]
        board = (root / "qemu" / "overlay" / "hw" / "mips" / "bbk9588.c").read_text(encoding="utf-8")
        host_bridge = (
            root / "qemu" / "overlay" / "hw" / "display" /
            "bbk9588_host_bridge.c"
        ).read_text(encoding="utf-8")
        cpu_h = (root / "qemu" / "overlay" / "target" / "mips" / "cpu.h").read_text(encoding="utf-8")
        translate = (root / "qemu" / "overlay" / "target" / "mips" / "tcg" / "translate.c").read_text(encoding="utf-8")
        system = (root / "emu" / "qemu" / "system.py").read_text(encoding="utf-8")

        self.assertIn(
            "#define BBK9588_PERF_MAGIC             0x504b4242u",
            host_bridge,
        )
        self.assertIn(
            "#define BBK9588_PERF_FORMAT_GUEST_INSNS 0x00004950u",
            host_bridge,
        )
        self.assertIn(
            "#define BBK9588_PERF_FORMAT_AIC        0x00434941u",
            host_bridge,
        )
        self.assertIn(
            "jz4740_aic_get_diagnostics(s->aic, &diagnostics);",
            host_bridge,
        )
        self.assertIn("host_maybe_send_metrics(s, now);", host_bridge)
        self.assertIn("static uint64_t bbk9588_guest_insn_count", board)
        self.assertIn("bbk9588_guest_insn_count_enabled", cpu_h)
        self.assertIn("uint64_t bbk9588_guest_insn_count;", cpu_h)
        self.assertIn("static void gen_bbk9588_guest_insn_count", translate)
        self.assertIn("tcg_gen_addi_i64(count, count, ctx->base.num_insns);", translate)
        self.assertIn("QEMU_BBK_PERF_MAGIC = 0x504B4242", system)
        self.assertIn("QEMU_BBK_PERF_PAYLOAD = struct.Struct(\"<QQ\")", system)
        self.assertIn("QEMU_BBK_AIC_PERF_PAYLOAD = struct.Struct(\"<24Q\")", system)

    def test_qemu_source_tree_check_rejects_binary_install_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "qemu-system-mipsel.exe").write_bytes(b"binary")

            result = inspect_qemu_source(root)

            self.assertFalse(result["is_qemu_source"], result)
            self.assertIn("configure", result["missing_required_paths"])
            self.assertIn("hw/mips/meson.build", result["missing_required_paths"])

    def test_qemu_source_tree_check_accepts_qemu_source_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "configure").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "meson.build").write_text("project('qemu')\n", encoding="utf-8")
            (root / "hw" / "mips").mkdir(parents=True)
            (root / "hw" / "mips" / "meson.build").write_text("", encoding="utf-8")
            (root / "hw" / "mips" / "Kconfig").write_text("", encoding="utf-8")
            (root / "target" / "mips").mkdir(parents=True)

            result = inspect_qemu_source(root)

            self.assertTrue(result["is_qemu_source"], result)
            self.assertEqual(result["missing_required_paths"], [])
            self.assertIn("hw/mips/bbk9588.c", result["missing_overlay_paths"])

    def test_install_qemu_overlay_refreshes_destination_mtime_for_ninja(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qemu_source = root / "qemu-src"
            overlay = root / "overlay"
            script = (
                Path(__file__).resolve().parents[1]
                / "qemu"
                / "scripts"
                / "install_qemu_overlay.py"
            )
            src = overlay / "hw" / "mips" / "bbk9588.c"
            dst = qemu_source / "hw" / "mips" / "bbk9588.c"
            old_time = time.time() - 86400

            qemu_source.mkdir()
            (qemu_source / "configure").write_text("#!/bin/sh\n", encoding="utf-8")
            (qemu_source / "meson.build").write_text("project('qemu')\n", encoding="utf-8")
            src.parent.mkdir(parents=True)
            src.write_text("overlay content\n", encoding="utf-8")
            os.utime(src, (old_time, old_time))

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--qemu-source",
                    str(qemu_source),
                    "--overlay",
                    str(overlay),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(dst.read_text(encoding="utf-8"), "overlay content\n")
            self.assertGreater(dst.stat().st_mtime, src.stat().st_mtime + 1.0)

    def test_qemu_windows_build_runs_multiline_bash_from_script_file(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "qemu"
            / "scripts"
            / "build_qemu_windows.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('[System.IO.File]::WriteAllText(', script)
        self.assertIn('& $MsysBash $buildScriptPosix', script)
        self.assertIn('Remove-Item -LiteralPath $buildScript', script)
        self.assertNotIn('& $MsysBash -lc $configure', script)

    def test_bbk9588_intc_tcu_source_follows_jz4740_register_semantics(self) -> None:
        overlay = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        board = (overlay / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        intc_source = (overlay / "hw" / "intc" / "jz4740_intc.c").read_text(
            encoding="utf-8"
        )
        intc_header = (
            overlay / "include" / "hw" / "intc" / "jz4740_intc.h"
        ).read_text(encoding="utf-8")
        tcu_source = (overlay / "hw" / "timer" / "jz4740_tcu.c").read_text(
            encoding="utf-8"
        )
        tcu_header = (
            overlay / "include" / "hw" / "timer" / "jz4740_tcu.h"
        ).read_text(encoding="utf-8")
        diag = (overlay / "hw" / "misc" / "bbk9588_diag.c").read_text(
            encoding="utf-8"
        )

        self.assertIn("#define JZ4740_INTC_ICSR      0x00u", intc_source)
        self.assertIn("#define JZ4740_INTC_ICMR      0x04u", intc_source)
        self.assertIn("#define JZ4740_INTC_ICMSR     0x08u", intc_source)
        self.assertIn("#define JZ4740_INTC_ICMCR     0x0cu", intc_source)
        self.assertIn("#define JZ4740_INTC_ICPR      0x10u", intc_source)
        self.assertIn("JZ4740_INTC_IRQ_TCU0 = 23", intc_header)
        self.assertIn("JZ4740_INTC_IRQ_TCU1 = 22", intc_header)
        self.assertIn("JZ4740_INTC_IRQ_TCU2 = 21", intc_header)
        self.assertIn("s->unmasked_pending = s->pending & ~s->mask;", intc_source)
        self.assertIn("case JZ4740_INTC_ICPR:\n    default:\n        break;", intc_source)
        self.assertIn("s->mask = JZ4740_INTC_MASK_RESET;", intc_source)

        self.assertIn('#define TYPE_JZ4740_TCU "jz4740-tcu"', tcu_header)
        self.assertIn("#define JZ4740_TCU_CHANNELS 8u", tcu_header)
        self.assertIn("JZ4740_TCU_IRQ_TCU0", tcu_header)
        self.assertIn("JZ4740_TCU_IRQ_TCU1", tcu_header)
        self.assertIn("JZ4740_TCU_IRQ_TCU2", tcu_header)
        self.assertIn("JZ4740_TCU_EVENT", tcu_header)
        self.assertIn("#define TCU_TSR             0x1cu", tcu_source)
        self.assertIn("#define TCU_TSSR            0x2cu", tcu_source)
        self.assertIn("#define TCU_TSCR            0x3cu", tcu_source)
        self.assertIn("#define TCU_HALF_SHIFT      16u", tcu_source)
        self.assertIn("#define TCU_FLAG_MASK", tcu_source)
        self.assertIn("uint32_t counter[JZ4740_TCU_CHANNELS];", tcu_source)
        self.assertIn("int64_t counter_anchor_ns[JZ4740_TCU_CHANNELS];", tcu_source)
        self.assertIn("static uint32_t tcu_current_counter", tcu_source)
        self.assertIn("static void tcu_write_counter", tcu_source)
        self.assertIn("static void tcu_update_compare", tcu_source)
        self.assertIn("static uint64_t tcu_ticks_to_ns", tcu_source)
        self.assertIn("!(s->stop_mask & bit)", tcu_source)
        self.assertIn("uint32_t half_bit = bit << TCU_HALF_SHIFT;", tcu_source)
        self.assertIn("newly_pending |= half_bit;", tcu_source)
        self.assertIn("case TCU_TSSR:", tcu_source)
        self.assertIn("s->stop_mask |= value;", tcu_source)
        self.assertIn("case TCU_TSCR:", tcu_source)
        self.assertIn("s->stop_mask &= ~value;", tcu_source)
        self.assertIn("s->pending_mask |= value & TCU_FLAG_MASK;", tcu_source)
        self.assertIn("s->irq_mask |= value & TCU_FLAG_MASK;", tcu_source)
        self.assertIn("tcu_write_counter(s, channel, value);", tcu_source)
        self.assertIn("tcu_update_compare(s, channel, reg, value);", tcu_source)
        self.assertIn("offset == TCU_TSR", tcu_source)
        self.assertIn("value = s->stop_mask;", tcu_source)
        self.assertIn("value = s->half_compare[channel];", tcu_source)
        self.assertIn("timer_new_ns(QEMU_CLOCK_VIRTUAL, tcu_timer_cb, s)", tcu_source)
        self.assertIn("VMSTATE_UINT32_ARRAY(counter,", tcu_source)
        self.assertIn("rc->phases.hold = tcu_reset_hold;", tcu_source)

        self.assertIn('#include "hw/timer/jz4740_tcu.h"', board)
        self.assertIn("qdev_new(TYPE_JZ4740_TCU)", board)
        self.assertIn('qdev_prop_set_uint32(dev, "period-ms", board->tcu_period_ms);', board)
        self.assertIn("BBK9588_KSEG_TO_PHYS(0xb0002000u)", board)
        self.assertIn("jz4740_tcu_irq_level(board->tcu, JZ4740_TCU_IRQ_TCU0)", board)
        self.assertIn("jz4740_tcu_irq_level(board->tcu, JZ4740_TCU_IRQ_TCU1));", board)
        self.assertNotIn("BBK9588_SYSCTRL_WAKE_PROXY_IRQ", board)
        self.assertNotIn("sysctrl_wake_pending", board)
        self.assertIn("jz4740_tcu_irq_level(board->tcu, JZ4740_TCU_IRQ_TCU2)", board)
        self.assertIn("output == JZ4740_TCU_EVENT ? bbk9588_tcu_event_handler", board)
        self.assertIn(
            "jz4740_tcu_get_diagnostics(s->sources.tcu, &tcu);", diag
        )
        self.assertIn("if (level && board->cpu_irq_output_enabled)", board)
        self.assertNotIn("BBK9588_MMIO_TCU", board)
        self.assertNotIn("static void bbk9588_tcu_write(", board)
        self.assertNotIn("static uint32_t bbk9588_tcu_read(", board)
        self.assertNotIn("QEMUTimer *tcu_irq_timer;", board)
    def test_jz4740_cpm_source_uses_register_reset_state(self) -> None:
        overlay = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (overlay / "hw" / "misc" / "jz4740_cpm.c").read_text(
            encoding="utf-8"
        )
        board = (overlay / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )

        self.assertIn("#define JZ4740_CPM_CPCCR     0x00u", source)
        self.assertIn("#define JZ4740_CPM_LCR       0x04u", source)
        self.assertIn("#define JZ4740_CPM_CPPCR     0x10u", source)
        self.assertIn("#define JZ4740_CPM_CLKGR     0x20u", source)
        self.assertIn("#define JZ4740_CPM_SCR       0x24u", source)
        self.assertIn("#define JZ4740_CPM_I2SCDR    0x60u", source)
        self.assertIn("#define JZ4740_CPM_LPCDR     0x64u", source)
        self.assertIn("#define JZ4740_CPM_MSCCDR    0x68u", source)
        self.assertIn("#define JZ4740_CPM_UHCCDR    0x6cu", source)
        self.assertIn("#define JZ4740_CPM_SSICDR    0x74u", source)
        self.assertIn("#define JZ4740_CPM_CPCCR_RESET  0x00000008u", source)
        self.assertIn("#define JZ4740_CPM_LCR_RESET    0x000000f8u", source)
        self.assertIn("#define JZ4740_CPM_CPPCR_RESET  0x28080011u", source)
        self.assertIn("#define JZ4740_CPM_CLKGR_RESET  0x00000000u", source)
        self.assertIn("#define JZ4740_CPM_SCR_RESET    0x00001500u", source)
        self.assertIn("#define JZ4740_CPM_I2SCDR_RESET 0x00000004u", source)
        self.assertIn("#define JZ4740_CPM_LPCDR_RW_MASK 0x800007ffu", source)
        self.assertIn("#define JZ4740_CPM_MSCCDR_RW_MASK 0x0000001fu", source)
        self.assertIn("#define JZ4740_CPM_UHCCDR_RW_MASK 0x0000000fu", source)
        self.assertIn("#define JZ4740_CPM_SSICDR_RW_MASK 0x8000000fu", source)
        self.assertIn("static bool jz4740_cpm_word_only_reg", source)
        self.assertIn("static uint32_t jz4740_cpm_mask_write", source)
        self.assertIn("value |= JZ4740_CPM_CPPCR_PLLS;", source)
        self.assertIn("size != sizeof(uint32_t)", source)
        self.assertIn("VMSTATE_UINT32_ARRAY(regs, JZ4740CPMState", source)
        self.assertIn("rc->phases.hold = jz4740_cpm_reset_hold;", source)
        self.assertIn("rc->phases.exit = jz4740_cpm_reset_exit;", source)
        self.assertIn("sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0000000u));", board)
        self.assertIn("jz4740_cpm_set_update(board->cpm, bbk9588_cpm_update, board);", board)
        self.assertNotIn("jz4740_cpm_wake_enabled(board->cpm)", board)
        self.assertNotIn("BBK9588_MMIO_SYSCTRL", board)
        self.assertNotIn("bbk9588.sysctrl", board)

    def test_bbk9588_cim_source_follows_jz4740_idle_semantics(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "misc" / "jz4740_cim.c").read_text(
            encoding="utf-8"
        )
        header = (root / "include" / "hw" / "misc" / "jz4740_cim.h").read_text(
            encoding="utf-8"
        )
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        meson = (root / "hw" / "mips" / "meson.build").read_text(
            encoding="utf-8"
        )

        self.assertIn('#define TYPE_JZ4740_CIM "jz4740-cim"', header)
        self.assertIn("#define CIM_CFG                    0x00u", source)
        self.assertIn("#define CIM_CR                     0x04u", source)
        self.assertIn("#define CIM_ST                     0x08u", source)
        self.assertIn("#define CIM_IID                    0x0cu", source)
        self.assertIn("#define CIM_RXFIFO                 0x10u", source)
        self.assertIn("#define CIM_DA                     0x20u", source)
        self.assertIn("#define CIM_CFG_RW_MASK            0x0000f373u", source)
        self.assertIn("#define CIM_CR_RW_MASK             0xff0f3f77u", source)
        self.assertIn("#define CIM_ST_RESET               CIM_ST_RF_EMPTY", source)
        self.assertIn("static bool cim_irq_pending", source)
        self.assertIn("s->status &= ((uint32_t)value | ~CIM_ST_W0C_MASK);", source)
        self.assertIn("s->status |= CIM_ST_VDD;", source)
        self.assertIn("vmstate_jz4740_cim", source)
        self.assertIn("qdev_new(TYPE_JZ4740_CIM)", board)
        self.assertIn(
            "sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb3060000u));",
            board,
        )
        self.assertIn("JZ4740_INTC_IRQ_CIM", board)
        self.assertIn("../misc/jz4740_cim.c", meson)
        self.assertNotIn("Bbk9588MmioState", board)
        self.assertNotIn("bbk9588.misc306", board)
        self.assertNotIn("bbk9588_mmio_ops", board)

    def test_qemu_jz4740_cim_idle_reset_masks_w0c_and_irq(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        cim = 0x13060000
        intc_status = 0x10001000
        cim_irq = 1 << 17

        with _QTestClient(qemu) as client:
            self.assertEqual(client.readl(cim + 0x00), 0)
            self.assertEqual(client.readl(cim + 0x04), 0)
            self.assertEqual(client.readl(cim + 0x08), 2)
            self.assertEqual(client.readl(cim + 0x0C), 0)
            self.assertEqual(client.readl(cim + 0x10), 0)

            client.writel(cim + 0x00, 0xFFFFFFFF)
            client.writel(cim + 0x04, 0xFFFFFFFF)
            self.assertEqual(client.readl(cim + 0x00), 0xF373)
            self.assertEqual(client.readl(cim + 0x04), 0xFF0F3F77)
            self.assertEqual(client.readl(cim + 0x08), 2)
            self.assertEqual(client.readl(intc_status) & cim_irq, 0)

            client.writel(cim + 0x04, 0x2001)
            client.writel(cim + 0x04, 0x2000)
            self.assertEqual(client.readl(cim + 0x08), 3)
            self.assertEqual(client.readl(intc_status) & cim_irq, cim_irq)

            client.writel(cim + 0x08, 0xFFFFFFFE)
            self.assertEqual(client.readl(cim + 0x08), 2)
            self.assertEqual(client.readl(intc_status) & cim_irq, 0)

            client.writel(cim + 0x20, 0x12345)
            self.assertEqual(client.readl(cim + 0x20), 0x12340)
            for offset in (0x0C, 0x10, 0x24, 0x28, 0x2C):
                client.writel(cim + offset, 0xFFFFFFFF)
                self.assertEqual(client.readl(cim + offset), 0)

    def test_bbk9588_lcd_source_follows_jz4740_register_semantics(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "display" / "jz4740_lcd.c").read_text(
            encoding="utf-8"
        )
        header = (
            root / "include" / "hw" / "display" / "jz4740_lcd.h"
        ).read_text(encoding="utf-8")
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        host_bridge = (
            root / "hw" / "display" / "bbk9588_host_bridge.c"
        ).read_text(encoding="utf-8")

        self.assertIn('#define TYPE_JZ4740_LCD "jz4740-lcd"', header)
        self.assertIn("#define LCD_CTRL_ENA               0x00000008u", source)
        self.assertIn("#define LCD_CTRL_OFUM              0x00000800u", source)
        self.assertIn("#define LCD_CTRL_IFUM0             0x00000400u", source)
        self.assertIn("#define LCD_CTRL_IFUM1             0x00000200u", source)
        self.assertIn("#define LCD_CTRL_RW_MASK           0x3fff3fffu", source)
        self.assertIn("#define LCD_CFG_RW_MASK            0x80ffffbfu", source)
        self.assertIn("#define LCD_VSYNC_RW_MASK          0x000007ffu", source)
        self.assertIn("#define LCD_TIMING_RW_MASK         0x07ff07ffu", source)
        self.assertIn("#define LCD_REV_RW_MASK            0x07ff0000u", source)
        self.assertIn("#define LCD_STATE_EOF              0x00000020u", source)
        self.assertIn("#define LCD_STATE_SOF              0x00000010u", source)
        self.assertIn("#define LCD_CMD_PAL                0x10000000u", source)
        self.assertIn("#define LCD_CMD_LEN_MASK           0x00ffffffu", source)
        self.assertIn("static bool jz4740_lcd_irq_pending", source)
        self.assertIn("static void jz4740_lcd_latch_iid", source)
        self.assertIn("static bool jz4740_lcd_fetch_descriptor", source)
        self.assertIn("static void jz4740_lcd_finish_channel", source)
        self.assertIn("command &= LCD_CMD_RW_MASK;", source)
        self.assertIn("command & ~LCD_CMD_LEN_MASK", source)
        self.assertIn("jz4740_lcd_latch_iid(s, LCD_STATE_SOF", source)
        self.assertIn("jz4740_lcd_latch_iid(s, LCD_STATE_EOF", source)
        self.assertIn("address_space_read(&address_space_memory", source)
        self.assertIn("VMSTATE_UINT32_ARRAY(regs, JZ4740LCDState", source)
        self.assertIn("rc->phases.hold = jz4740_lcd_reset_hold;", source)
        self.assertIn("sysbus_init_irq(sbd, &s->irq);", source)

        self.assertIn("TYPE_JZ4740_LCD", board)
        self.assertIn(
            "sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb3050000u));",
            board,
        )
        self.assertIn("JZ4740_INTC_IRQ_LCD", board)
        self.assertIn("jz4740_lcd_signal_frame_done(s->lcd);", host_bridge)
        self.assertIn("jz4740_lcd_get_frame_source(s->lcd, &fb_va)", host_bridge)
        self.assertNotIn("jz4740_lcd_observe_alias_write", board)
        self.assertIn("host_guest_ram_address_valid(", host_bridge)
        self.assertIn(
            "segment == 0 || segment == 0x80000000u", host_bridge
        )
        self.assertNotIn("BBK9588_LCD_MIRROR_CONFIG", board)
        self.assertNotIn("BBK9588_FRAMEBUFFER_VA", board)
        self.assertNotIn("0x804a6b88", board.lower())
        self.assertNotIn("0xa1f82000", board.lower())
        self.assertNotIn("bbk9588.display0", board)
        self.assertNotIn("uint32_t jz_lcd_ctrl;", board)
        self.assertNotIn("static bool bbk9588_jz_lcd_irq_pending", board)
        self.assertNotIn("static void bbk9588_jz_lcd_write", board)
        self.assertIn("bbk9588_panel_set_frame_done(s->panel);", host_bridge)
        self.assertNotIn("graphics_status", board)
        self.assertNotIn('oc, "graphics-status"', board)
        self.assertNotIn('oc, "lcd-status"', board)

    def test_bbk9588_panel_source_owns_board_status_window(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "display" / "bbk9588_panel.c").read_text(
            encoding="utf-8"
        )
        header = (
            root / "include" / "hw" / "display" / "bbk9588_panel.h"
        ).read_text(encoding="utf-8")
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        host_bridge = (
            root / "hw" / "display" / "bbk9588_host_bridge.c"
        ).read_text(encoding="utf-8")
        meson = (root / "hw" / "mips" / "meson.build").read_text(
            encoding="utf-8"
        )

        self.assertIn('#define TYPE_BBK9588_PANEL "bbk9588-panel"', header)
        self.assertIn("#define PANEL_STATUS               0x0cu", source)
        self.assertIn("#define PANEL_STATUS_FRAME_DONE", source)
        self.assertIn("#define PANEL_STATUS_READY", source)
        self.assertIn("s->frame_status &= ~(lane_value &", source)
        self.assertIn("s->status |= PANEL_STATUS_READY;", source)
        self.assertIn("void bbk9588_panel_set_frame_done", source)
        self.assertIn("vmstate_bbk9588_panel", source)
        self.assertIn("qdev_new(TYPE_BBK9588_PANEL)", board)
        self.assertIn(
            "sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0043000u));",
            board,
        )
        self.assertIn("bbk9588_panel_set_write_callback", board)
        self.assertIn("bbk9588_panel_set_frame_done(s->panel);", host_bridge)
        self.assertIn("../display/bbk9588_panel.c", meson)
        self.assertNotIn('"bbk9588.lcd"', board)
        self.assertNotIn("lcd_irq_status", board)
        self.assertNotIn("lcd_status", board)

    def test_bbk9588_host_bridge_owns_host_output_state(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (
            root / "hw" / "display" / "bbk9588_host_bridge.c"
        ).read_text(encoding="utf-8")
        header = (
            root / "include" / "hw" / "display" /
            "bbk9588_host_bridge.h"
        ).read_text(encoding="utf-8")
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        meson = (root / "hw" / "mips" / "meson.build").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '#define TYPE_BBK9588_HOST_BRIDGE "bbk9588-host-bridge"',
            header,
        )
        self.assertIn("struct Bbk9588HostBridgeState", source)
        self.assertIn("static void host_gfx_update", source)
        self.assertIn("static void host_audio_output", source)
        self.assertIn("static bool host_send_metrics", source)
        self.assertIn("bbk9588_host_bridge_connect_display", board)
        self.assertIn("bbk9588_host_bridge_connect_audio", board)
        self.assertIn("../display/bbk9588_host_bridge.c", meson)
        self.assertNotIn("CharFrontend frame_chr;", board)
        self.assertNotIn("QemuConsole *lcd_console;", board)
        self.assertNotIn("static void bbk9588_lcd_gfx_update", board)
        self.assertNotIn("static void bbk9588_audio_output", board)

    def test_bbk9588_host_input_owns_chardev_protocol_state(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (
            root / "hw" / "input" / "bbk9588_host_input.c"
        ).read_text(encoding="utf-8")
        header = (
            root / "include" / "hw" / "input" / "bbk9588_host_input.h"
        ).read_text(encoding="utf-8")
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        meson = (root / "hw" / "mips" / "meson.build").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '#define TYPE_BBK9588_HOST_INPUT "bbk9588-host-input"',
            header,
        )
        self.assertIn("CharFrontend chr;", source)
        self.assertIn('sscanf(line, "T %u %u %u %u %u"', source)
        self.assertIn('sscanf(line, "K %u %u"', source)
        self.assertIn("bbk9588_host_input_configure", board)
        self.assertIn("bbk9588_key_apply_host_input", board)
        self.assertIn("#define BBK9588_HOST_KEY_POWER     11u", board)
        self.assertIn("case BBK9588_HOST_KEY_POWER:", board)
        self.assertIn("*mask = 0x20000000u;", board)
        self.assertIn("bbk9588_touch_apply_host_input", board)
        self.assertIn("../input/bbk9588_host_input.c", meson)
        self.assertNotIn("CharFrontend input_chr;", board)
        self.assertNotIn("static void bbk9588_input_handle_line", board)
        self.assertNotIn("static void bbk9588_input_read", board)

    def test_bbk9588_diag_owns_trace_and_input_ring_state(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "misc" / "bbk9588_diag.c").read_text(
            encoding="utf-8"
        )
        header = (
            root / "include" / "hw" / "misc" / "bbk9588_diag.h"
        ).read_text(encoding="utf-8")
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        dma_bridge = (root / "hw" / "dma" / "bbk9588_dma_bridge.c").read_text(
            encoding="utf-8"
        )
        meson = (root / "hw" / "mips" / "meson.build").read_text(
            encoding="utf-8"
        )

        self.assertIn('#define TYPE_BBK9588_DIAG "bbk9588-diag"', header)
        self.assertIn("struct Bbk9588DiagState", source)
        self.assertIn("bbk9588_diag_queue_input", source)
        self.assertIn("bbk9588_diag_storage_record", source)
        self.assertIn("bbk9588_diag_dmac_sample", source)
        self.assertIn("bbk9588_diag_touch_record", source)
        self.assertIn("bbk9588_diag_progress_record", source)
        self.assertIn("bbk9588_diag_panel_write", source)
        self.assertIn("Bbk9588DiagSources sources;", source)
        self.assertIn("uint32_t graphics_count;", source)
        self.assertIn("uint32_t progress_seq;", source)
        self.assertIn("uint32_t nand_ready_count;", source)
        self.assertIn("qdev_new(TYPE_BBK9588_DIAG)", board)
        self.assertIn("bbk9588_diag_dmac_sample", dma_bridge)
        self.assertNotIn("bbk9588_diag_dmac_sample", board)
        self.assertIn("bbk9588_diag_connect_sources", board)
        self.assertIn("../misc/bbk9588_diag.c", meson)
        self.assertNotIn("bbk9588_active_board", board)
        self.assertNotIn("uint32_t input_event_words", board)
        self.assertNotIn("uint32_t dmac_trace_seq", board)
        self.assertNotIn("uint32_t graphics_trace_count", board)
        self.assertNotIn("uint32_t progress_trace_seq", board)
        self.assertNotIn("BBK9588_TOUCH_TRACE_VA", board)
        self.assertNotIn("bbk9588_storage_trace_record", board)

    def test_qemu_bbk9588_panel_ready_frame_done_and_w1c(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        panel = 0x10043000
        lcd = 0x13050000
        descriptor = 0x1000
        framebuffer = 0x2000

        with _QTestClient(qemu) as client:
            self.assertEqual(client.readl(panel + 0x0C), 0)
            client.writeb(panel + 0x00, 0x5A)
            self.assertEqual(client.readl(panel + 0x00), 0x5A)
            self.assertEqual(client.readl(panel + 0x0C), 0x80)
            client.writeb(panel + 0x21, 0xA5)
            self.assertEqual(client.readl(panel + 0x20), 0xA500)

            client.writel(descriptor + 0x00, 0)
            client.writel(descriptor + 0x04, framebuffer)
            client.writel(descriptor + 0x08, 0x1234)
            client.writel(descriptor + 0x0C, 0xC0009600)
            client.writel(lcd + 0x40, descriptor)
            client.writel(lcd + 0x30, 0x3008)

            self.assertEqual(client.readl(panel + 0x0C), 0x81)
            client.writel(panel + 0x0C, 1)
            self.assertEqual(client.readl(panel + 0x0C), 0x80)

    def test_bbk9588_sadc_source_follows_jz4740_register_semantics(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "input" / "jz4740_sadc.c").read_text(
            encoding="utf-8"
        )
        header = (
            root / "include" / "hw" / "input" / "jz4740_sadc.h"
        ).read_text(encoding="utf-8")
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        diag = (root / "hw" / "misc" / "bbk9588_diag.c").read_text(
            encoding="utf-8"
        )

        self.assertIn('#define TYPE_JZ4740_SADC "jz4740-sadc"', header)
        self.assertIn("#define JZ4740_SADC_DEFAULT_BATTERY_RAW 0x0e68u", header)
        self.assertIn("#define SADC_CONFIG_RESET           0x0002002cu", source)
        self.assertIn("#define SADC_DATA_MASK              0x0fffu", source)
        self.assertIn("#define SADC_ADENA                  0x00u", source)
        self.assertIn("#define SADC_ADTCH                  0x18u", source)
        self.assertIn("#define SADC_ADSDAT                 0x20u", source)
        self.assertIn("#define SADC_CONFIG_XYZ_MASK        0x00006000u", source)
        self.assertIn("#define SADC_CONFIG_XYZ_ZS          1u", source)
        self.assertIn("#define SADC_CONFIG_XYZ_Z12         2u", source)
        self.assertIn("#define SADC_FIFO_DEPTH             2u", source)
        self.assertIn("#define SADC_STATE_DTCH             0x04u", source)
        self.assertIn("#define SADC_STATE_PENU             0x08u", source)
        self.assertIn("#define SADC_STATE_PEND             0x10u", source)
        self.assertIn("#define SADC_TOUCH_TYPE0            0x00008000u", source)
        self.assertIn("#define SADC_TOUCH_TYPE1            0x80000000u", source)
        self.assertIn("#define SADC_TOUCH_ZS_RAW           0x0800u", source)

        self.assertIn("QEMUTimer *timer;", source)
        self.assertIn("static uint32_t sadc_pack_touch_pair", source)
        self.assertIn("static uint32_t sadc_touch_fifo_pop", source)
        self.assertIn("static void sadc_complete_cpu_samples", source)
        self.assertIn("static uint32_t sadc_touch_delay_ms", source)
        self.assertIn("static void sadc_schedule_conversion", source)
        self.assertIn("static void sadc_timer_cb", source)
        self.assertIn("s->pending_enable |= requested;", source)
        self.assertIn("uint64_t scaled = (uint64_t)ticks * 128u;", source)
        self.assertIn("(scaled + 11999u) / 12000u", source)
        self.assertIn("timer_new_ms(QEMU_CLOCK_REALTIME, sadc_timer_cb, s)", source)
        self.assertIn("case SADC_CONFIG_XYZ_ZS:", source)
        self.assertIn("case SADC_CONFIG_XYZ_Z12:", source)
        self.assertIn("case SADC_CONFIG_XYZ_XY:", source)
        self.assertIn("case SADC_ADENA:", source)
        self.assertIn("case SADC_ADCFG:", source)
        self.assertIn("case SADC_ADCTRL:", source)
        self.assertIn("case SADC_ADSTATE:", source)
        self.assertIn("case SADC_ADTCH:", source)
        self.assertIn("case SADC_ADBDAT:", source)
        self.assertIn("case SADC_ADSDAT:", source)
        self.assertIn("qemu_set_irq(s->irq, level);", source)
        self.assertIn("VMSTATE_UINT32_ARRAY(touch_fifo, JZ4740SADCState", source)
        self.assertIn("rc->phases.hold = sadc_reset_hold;", source)
        self.assertIn('DEFINE_PROP_UINT32("battery-raw"', source)
        self.assertIn("void jz4740_sadc_set_touch", source)
        self.assertIn("void jz4740_sadc_get_diagnostics", source)

        self.assertIn("TYPE_JZ4740_SADC", board)
        self.assertIn(
            "sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0070000u));",
            board,
        )
        self.assertIn("JZ4740_INTC_IRQ_SADC", board)
        self.assertIn("jz4740_sadc_set_touch(board->sadc, raw_x, raw_y, down);", board)
        self.assertIn("jz4740_sadc_get_diagnostics(s->sources.sadc, &sadc);", diag)
        self.assertNotIn("QEMUTimer *sadc_timer;", board)
        self.assertNotIn("uint8_t sadc_status_event;", board)
        self.assertNotIn("static uint32_t bbk9588_sadc_read", board)
        self.assertNotIn('"bbk9588.misc7"', board)

    def test_bbk9588_gpio_source_follows_jz4740_port_register_semantics(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "qemu/overlay/hw/gpio/jz4740_gpio.c").read_text(
            encoding="utf-8"
        )
        header = (root / "qemu/overlay/include/hw/gpio/jz4740_gpio.h").read_text(
            encoding="utf-8"
        )
        board = (root / "qemu/overlay/hw/mips/bbk9588.c").read_text(
            encoding="utf-8"
        )

        self.assertIn('#define TYPE_JZ4740_GPIO "jz4740-gpio"', header)
        self.assertIn("#define JZ4740_GPIO_NUM_PORTS 4u", header)
        self.assertIn("#define GPIO_PORT_STRIDE           0x0100u", source)
        self.assertIn("#define GPIO_PIN                   0x00u", source)
        self.assertIn("#define GPIO_DAT                   0x10u", source)
        self.assertIn("#define GPIO_DATS                  0x14u", source)
        self.assertIn("#define GPIO_FLGC                  GPIO_DATS", source)
        self.assertIn("#define GPIO_DATC                  0x18u", source)
        self.assertIn("#define GPIO_IM                    0x20u", source)
        self.assertIn("#define GPIO_FLG                   0x80u", source)
        self.assertIn("#define GPIO_IM_RESET              0xffffffffu", source)

        self.assertIn("case GPIO_DATS:", source)
        self.assertIn("case GPIO_DATC:", source)
        self.assertIn("case GPIO_IMS:", source)
        self.assertIn("case GPIO_IMC:", source)
        self.assertIn("case GPIO_FUNS:", source)
        self.assertIn("case GPIO_FUNC:", source)
        self.assertIn("case GPIO_DIRS:", source)
        self.assertIn("case GPIO_DIRC:", source)
        self.assertIn("case GPIO_TRGS:", source)
        self.assertIn("case GPIO_TRGC:", source)
        self.assertIn("s->flag[port] &= ~lane_value;", source)
        self.assertIn("gpio_update_irq(s, port);", source)
        self.assertIn("qdev_init_gpio_in_named(DEVICE(obj), gpio_pin_input", source)
        self.assertIn("VMSTATE_UINT32_ARRAY(regs, JZ4740GPIOState", source)
        self.assertIn("rc->phases.hold = gpio_reset_hold;", source)

        self.assertIn('#include "hw/gpio/jz4740_gpio.h"', board)
        self.assertIn("TYPE_JZ4740_GPIO", board)
        self.assertIn(
            "sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0010000u));",
            board,
        )
        self.assertIn("JZ4740_INTC_IRQ_GPIO0", board)
        self.assertIn("JZ4740_INTC_IRQ_GPIO3", board)
        self.assertIn("board->usb_power_connected ? 0x78000000u", board)
        self.assertIn('object_class_property_add_bool(oc, "usb-power-connected"', board)
        touch_body = board[
            board.index("static void bbk9588_touch_set_state(", board.index("static void bbk9588_touch_set_state(") + 1):
            board.index("static void bbk9588_set_usb_power_state(")
        ]
        self.assertNotIn("JZ4740_GPIO_PORT_B", touch_body)
        self.assertIn("jz4740_gpio_set_input_level(board->gpio, port, mask,", board)
        self.assertIn("jz4740_gpio_raise_flag(board->gpio, JZ4740_GPIO_PORT_C", board)
        self.assertIn("jz4740_gpio_set_input_sample_callback(", board)
        self.assertNotIn("BBK9588_MMIO_GPIO", board)
        self.assertNotIn("uint32_t key_gpio_down_100;", board)
        self.assertNotIn("static void bbk9588_gpio_apply_write", board)

    def test_bbk9588_rtc_source_follows_jz4740_register_semantics(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "qemu/overlay/hw/rtc/jz4740_rtc.c").read_text(
            encoding="utf-8"
        )
        header = (root / "qemu/overlay/include/hw/rtc/jz4740_rtc.h").read_text(
            encoding="utf-8"
        )
        board = (root / "qemu/overlay/hw/mips/bbk9588.c").read_text(
            encoding="utf-8"
        )

        self.assertIn('#define TYPE_JZ4740_RTC "jz4740-rtc"', header)
        self.assertIn('#include "system/rtc.h"', source)
        self.assertIn("#define RTC_RTCCR_RESET            0x00000081u", source)
        self.assertIn("#define RTC_RTCCR_WRDY             0x00000080u", source)
        self.assertIn("#define RTC_RTCCR_1HZ              0x00000040u", source)
        self.assertIn("#define RTC_RTCCR_1HZIE            0x00000020u", source)
        self.assertIn("#define RTC_HCR_PD                 0x00000001u", source)
        self.assertIn("#define RTC_HWRSR_PPR              0x00000010u", source)
        self.assertIn("#define RTC_HWRSR_HR               0x00000020u", source)
        self.assertIn("#define RTC_HWRSR_PIN              0x00000002u", source)
        self.assertIn("static uint32_t rtc_host_seconds", source)
        self.assertIn("uint32_t jz4740_rtc_seconds", source)
        self.assertIn("static uint32_t rtc_latch_flags", source)
        self.assertIn("static bool rtc_irq_pending", source)
        self.assertIn("static void rtc_schedule", source)
        self.assertIn("static void rtc_timer_cb", source)
        self.assertIn("static void rtc_enter_hibernate", source)
        self.assertIn("JZ4740RTCPowerDownCallback power_down_callback;", source)
        self.assertIn("s->power_down_callback(s->power_down_opaque);", source)
        self.assertIn('DEFINE_PROP_BOOL("hibernate-wakeup", JZ4740RTCState,', source)
        self.assertIn("RTC_HWRSR_HR | RTC_HWRSR_PIN : RTC_HWRSR_PPR", source)
        self.assertIn("static void rtc_write_while_hibernating", source)
        self.assertIn("case RTC_RTCCR:", source)
        self.assertIn("case RTC_RTCSR:", source)
        self.assertIn("case RTC_HCR:", source)
        self.assertIn("case RTC_HWRSR:", source)
        self.assertIn("case RTC_HSPR:", source)
        self.assertIn("s->hwrsr |= RTC_HWRSR_ALM;", source)
        self.assertIn("s->hcr &= ~RTC_HCR_PD;", source)
        self.assertIn("qemu_get_timedate(&tm, 0);", source)
        self.assertIn("seconds = mktimegm(&tm);", source)
        self.assertIn("timer_new_ns(rtc_clock, rtc_timer_cb, s)", source)
        self.assertIn("timer_mod(s->timer, next_ns);", source)
        self.assertIn("VMSTATE_INT64(base_ns, JZ4740RTCState)", source)
        self.assertIn("rc->phases.hold = rtc_reset_hold;", source)
        self.assertNotIn("qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)", source)

        self.assertIn('#include "hw/rtc/jz4740_rtc.h"', board)
        self.assertIn("TYPE_JZ4740_RTC", board)
        self.assertIn(
            "sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0003000u));",
            board,
        )
        self.assertIn("JZ4740_INTC_IRQ_RTC", board)
        self.assertIn("qemu_system_shutdown_request(SHUTDOWN_CAUSE_GUEST_SHUTDOWN);", board)
        self.assertIn("bbk9588: RTC HCR.PD requested guest shutdown", board)
        self.assertIn('object_class_property_add_bool(oc, "hibernate-poweroff"', board)
        self.assertIn('object_class_property_add_bool(oc, "hibernate-wakeup"', board)
        self.assertIn('qdev_prop_set_bit(dev, "hibernate-wakeup",', board)
        self.assertIn('qdev_prop_set_uint32(dev, "input-reset-d", 0x20200000u);', board)
        self.assertNotIn("BBK9588_MMIO_RTC", board)
        self.assertNotIn("QEMUTimer *rtc_timer;", board)
        self.assertNotIn("static uint32_t bbk9588_rtc_read", board)
        self.assertNotIn('"ready-status"', board)

    def test_qemu_jz4740_rtc_hcr_requests_guest_shutdown(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        rtc = 0x10003000
        gpio_d_pin = 0x10010300
        with _QTestClient(qemu, hibernate_poweroff=True) as client:
            self.assertEqual(client.readl(gpio_d_pin) & 0x20000000, 0x20000000)
            self.assertEqual(client.readl(rtc + 0x20), 0)
            client.writel(rtc + 0x20, 1)
            self.assertEqual(client.process.wait(timeout=5), 0)

    def test_qemu_jz4740_rtc_hibernate_pin_wakeup_status(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        rtc = 0x10003000
        with _QTestClient(qemu, hibernate_wakeup=True) as client:
            self.assertEqual(client.readl(rtc + 0x20), 0)
            self.assertEqual(client.readl(rtc + 0x30), 0x22)

    def test_jz4740_dmac_source_follows_channel_semantics(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "dma" / "jz4740_dmac.c").read_text(
            encoding="utf-8"
        )
        header = (
            root / "include" / "hw" / "dma" / "jz4740_dmac.h"
        ).read_text(encoding="utf-8")
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        dma_bridge = (root / "hw" / "dma" / "bbk9588_dma_bridge.c").read_text(
            encoding="utf-8"
        )
        dma_header = (
            root / "include" / "hw" / "dma" / "bbk9588_dma_bridge.h"
        ).read_text(encoding="utf-8")
        meson = (root / "hw" / "mips" / "meson.build").read_text(
            encoding="utf-8"
        )

        self.assertIn("#define JZ4740_DMAC_CHANNELS 6u", header)
        self.assertIn("#define DMAC_DMACR            0x300u", source)
        self.assertIn("#define DMAC_DIRQP            0x304u", source)
        self.assertIn("#define DMAC_DRT_AUTO         8u", source)
        self.assertIn("#define JZ4740_DMAC_REQUEST_AIC_TX 24u", header)
        self.assertIn("#define JZ4740_DMAC_REQUEST_AIC_RX 25u", header)
        self.assertIn("#define DMAC_DTC_MASK         0x00ffffffu", source)
        self.assertIn("#define DMAC_DCS_NDES         0x80000000u", source)
        self.assertIn("#define DMAC_DCS_TT           0x00000008u", source)
        self.assertIn("#define DMAC_DCS_CT           0x00000002u", source)
        self.assertIn("#define DMAC_DCM_SAI          0x00800000u", source)
        self.assertIn("#define DMAC_DCM_DAI          0x00400000u", source)
        self.assertIn("#define DMAC_DCM_LINK         0x00000001u", source)
        self.assertIn("#define DMAC_DDA_DBA_MASK     0xfffff000u", source)
        self.assertIn("#define DMAC_DDA_DOA_MASK     0x00000ff0u", source)
        self.assertIn("#define DMAC_DESC_BYTES       16u", source)
        self.assertIn("static bool dmac_channel_offset", source)
        self.assertIn("static void dmac_sync_irq", source)
        self.assertIn("static void dmac_set_terminal_count", source)
        self.assertIn("static void dmac_fetch_descriptor", source)
        self.assertIn("static void dmac_finish_transfer", source)
        self.assertIn("dmac_descriptor_next(desc_addr, desc_dtc)", source)
        self.assertIn("static bool dmac_try_bulk_transfer", source)
        self.assertIn("static void dmac_try_auto_transfer", source)
        self.assertIn("static void dmac_try_audio_transfer", source)
        self.assertIn("dmac_memory_read(source + done, chunk, n);", source)
        self.assertIn("dmac_memory_write(target + done, chunk, n);", source)
        self.assertIn(
            "s->ops.write(s->ops_opaque, request, unit, unit_bytes,", source
        )
        self.assertIn(
            "s->ops.read(s->ops_opaque, request, unit, unit_bytes,", source
        )
        self.assertIn("VMSTATE_UINT32_ARRAY(regs, JZ4740DMACState", source)
        self.assertIn("qemu_set_irq(s->irq, level);", source)
        self.assertIn("qdev_init_gpio_in(DEVICE(obj), dmac_request_input", source)
        self.assertIn("qdev_get_gpio_in(DEVICE(board->dmac),", board)
        self.assertIn('#define TYPE_BBK9588_DMA_BRIDGE "bbk9588-dma-bridge"', dma_header)
        self.assertIn("jz4740_dmac_set_peripheral_ops(s->dmac,", dma_bridge)
        self.assertIn(
            "bridge_msc_dma_transfer(s, channel, source, target, count)",
            dma_bridge,
        )
        self.assertIn(
            "sysbus_mmio_map(sbd, 0, "
            "BBK9588_KSEG_TO_PHYS(0xb3020000u));",
            board,
        )
        self.assertNotIn("BBK9588_MMIO_DMAC", board)
        self.assertNotIn("bbk9588.dmac", board)
        self.assertNotIn("static void bbk9588_dmac_try_audio_transfer", board)
        self.assertNotIn("JZ4740DMACPeripheralOps", board)
        self.assertIn("static void bbk9588_dmac_irq_handler", board)
        self.assertIn("jz4740_intc_set_irq(board->intc, JZ4740_INTC_IRQ_DMA", board)
        self.assertIn("bbk9588_update_irq(board);", board)
        self.assertIn(
            "board->dmac_irq = qemu_allocate_irq(bbk9588_dmac_irq_handler",
            board,
        )
        self.assertIn("../dma/bbk9588_dma_bridge.c", meson)


    def test_jz4740_aic_source_models_fifo_dma_irq_and_host_audio(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "audio" / "jz4740_aic.c").read_text(
            encoding="utf-8"
        )
        header = (root / "include" / "hw" / "audio" / "jz4740_aic.h").read_text(
            encoding="utf-8"
        )
        board = (root / "hw" / "mips" / "bbk9588.c").read_text(encoding="utf-8")
        dma_bridge = (root / "hw" / "dma" / "bbk9588_dma_bridge.c").read_text(
            encoding="utf-8"
        )
        host_bridge = (
            root / "hw" / "display" / "bbk9588_host_bridge.c"
        ).read_text(encoding="utf-8")
        meson = (root / "hw" / "audio" / "meson.build").read_text(
            encoding="utf-8"
        )

        self.assertIn('#define TYPE_JZ4740_AIC "jz4740-aic"', header)
        self.assertIn("JZ4740_AIC_TX_DMA_REQUEST", header)
        self.assertIn("JZ4740_AIC_RX_DMA_REQUEST", header)
        self.assertIn("#define JZ4740_AIC_FIFO_DEPTH      32u", source)
        self.assertIn("#define JZ4740_AIC_TICK_NS         1000000LL", source)
        self.assertIn("#define AICFR_RESET        0x00007800u", source)
        self.assertIn("#define I2SDIV_RESET       0x00000003u", source)
        self.assertIn("#define CDCCR1_RESET       0x021b2302u", source)
        self.assertIn("#define CDCCR2_RESET       0x00170803u", source)
        self.assertIn("jz4740_aic_tx_dma_requested", source)
        self.assertIn("jz4740_aic_rx_dma_requested", source)
        self.assertIn("jz4740_aic_notify_tx_dma_boundary", header)
        self.assertIn("uint64_t pending_output_frames;", source)
        self.assertIn("if (s->tx_dma_boundary)", source)
        self.assertIn("jz4740_aic_process_output(s, pending)", source)
        self.assertIn("jz4740_aic_notify_tx_dma_boundary(s->aic);", dma_bridge)
        self.assertIn("qemu_set_irq(s->irqs[JZ4740_AIC_IRQ], irq);", source)
        self.assertIn("audio_be_open_out", source)
        self.assertIn("audio_be_open_in", source)
        self.assertIn("audio_be_write", source)
        self.assertIn("audio_be_read", source)
        self.assertIn("bool input_voice_attempted;", source)
        self.assertIn(
            "if (recording && !s->in_voice && !s->input_voice_attempted)",
            source,
        )
        self.assertIn("s->input_voice_attempted = true;", source)
        self.assertIn("timer_new_ns(QEMU_CLOCK_VIRTUAL", source)
        self.assertIn("8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000", source)
        self.assertIn("void jz4740_aic_get_diagnostics", source)
        self.assertIn("s->tx_dma_samples += done / sample_bytes;", source)
        self.assertIn("s->rx_dma_samples += done / sample_bytes;", source)
        self.assertIn("s->underruns++;", source)
        self.assertIn("s->overruns++;", source)
        self.assertIn("JZ4740AICOutputCallback output_callback;", source)
        self.assertIn("jz4740_aic_stream_output(s, output, actual);", source)
        self.assertIn("JZ4740_AIC_STREAM_HZ       50u", source)

        self.assertIn("TYPE_JZ4740_AIC", board)
        self.assertIn("sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0020000u));", board)
        self.assertIn("TYPE_JZ4740_MSC", board)
        self.assertIn("machine_add_audiodev_property(mc);", board)
        self.assertIn("#define BBK9588_AUDIO_MAGIC", host_bridge)
        self.assertIn("jz4740_aic_set_output_callback", host_bridge)
        self.assertIn("CONFIG_BBK9588", meson)
        self.assertIn("jz4740_aic.c", meson)

    def test_bbk9588_msc_dma_does_not_use_raw_nand_backing(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        dma_bridge = (root / "hw" / "dma" / "bbk9588_dma_bridge.c").read_text(
            encoding="utf-8"
        )
        nand_header = (
            root / "include" / "hw" / "block" / "bbk9588_nand.h"
        ).read_text(encoding="utf-8")
        start = dma_bridge.index("static bool bridge_msc_dma_transfer")
        end = dma_bridge.index("static void bridge_dmac_trace", start)
        msc_complete = dma_bridge[start:end]

        self.assertNotIn("msc_oob_lba", source)
        self.assertNotIn("Bbk9588NandState", msc_complete)
        self.assertNotIn("nand->", msc_complete)
        self.assertNotIn("nand_dev", msc_complete)
        self.assertIn("#define BBK9588_NAND_PAGES_PER_BLOCK 64u", nand_header)
        self.assertIn("#define BBK9588_NAND_BLOCKS          4096u", nand_header)
        self.assertIn("No removable MSC medium is attached by default", msc_complete)
        self.assertIn("buf = g_malloc0(transfer.sectors * 512u);", msc_complete)
        self.assertIn("cpu_physical_memory_write", msc_complete)
        self.assertIn("cpu_physical_memory_read", msc_complete)
        self.assertNotIn("initial_data", source)
        self.assertNotIn("initial_size", source)
        self.assertNotIn("bbk9588_nand_build_ftl_map", source)
        self.assertNotIn("bbk9588_nand_build_oob_logical_map", source)
        self.assertNotIn("bbk9588_nand_translate_oob_mapped_page", source)
        self.assertNotIn("oob_logical_to_physical_block", source)
        self.assertNotIn("bbk9588_nand_translate_data_page", source)
        self.assertNotIn("bbk9588_read_ftl_logical_sector", source)
        self.assertNotIn("bbk9588_write_ftl_logical_sector", source)
        self.assertNotIn("bbk9588_find_fat16_layout", msc_complete)
        self.assertNotIn("bbk9588_read_logical_sector", msc_complete)
        self.assertNotIn("bbk9588_write_logical_sector", msc_complete)
        self.assertNotIn("enum { pages_per_block = 64 }", source)
        self.assertNotIn("last_oob_page", source)
        self.assertNotIn("0x809066c0u", source)
        self.assertNotIn("0x8090674cu", source)

    def test_bbk9588_msc_source_follows_jz4740_register_reset_state(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "sd" / "jz4740_msc.c").read_text(
            encoding="utf-8"
        )
        header = (root / "include" / "hw" / "sd" / "jz4740_msc.h").read_text(
            encoding="utf-8"
        )
        machine = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        dma_bridge = (root / "hw" / "dma" / "bbk9588_dma_bridge.c").read_text(
            encoding="utf-8"
        )
        diag = (root / "hw" / "misc" / "bbk9588_diag.c").read_text(
            encoding="utf-8"
        )
        meson = (root / "hw" / "mips" / "meson.build").read_text(
            encoding="utf-8"
        )

        self.assertIn("#define MSC_STRPCL                 0x0000u", source)
        self.assertIn("#define MSC_STAT                   0x0004u", source)
        self.assertIn("#define MSC_RESTO                  0x0010u", source)
        self.assertIn("#define MSC_RDTO                   0x0014u", source)
        self.assertIn("#define MSC_IMASK                  0x0024u", source)
        self.assertIn("#define MSC_IREG                   0x0028u", source)
        self.assertIn("#define MSC_CMD                    0x002cu", source)
        self.assertIn("#define MSC_ARG                    0x0030u", source)
        self.assertIn("#define MSC_RES                    0x0034u", source)
        self.assertIn("#define MSC_STAT_RESET             0x00000040u", source)
        self.assertIn("#define MSC_RESTO_RESET            0x00000040u", source)
        self.assertIn("#define MSC_RDTO_RESET             0x0000ffffu", source)
        self.assertIn("#define MSC_IMASK_RESET            0x000000ffu", source)
        self.assertIn("static uint32_t msc_interrupt_status", source)
        self.assertIn("msc_interrupt_status(s) & ~mask", source)
        self.assertIn("msc_set_reg(s, MSC_IREG, msc_reg(s, MSC_IREG) & ~lane_value);", source)
        self.assertIn("static void msc_prepare_response", source)
        self.assertIn("static uint32_t msc_read_response", source)
        self.assertIn("vmstate_jz4740_msc", source)
        self.assertIn("bool jz4740_msc_begin_dma", source)
        self.assertIn("void jz4740_msc_finish_dma", source)
        self.assertIn("typedef struct JZ4740MSCDMATransfer", header)
        self.assertIn("typedef struct JZ4740MSCDiagnostics", header)

        self.assertIn("qdev_new(TYPE_JZ4740_MSC)", machine)
        self.assertIn(
            "sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0021000u));",
            machine,
        )
        self.assertIn("JZ4740_INTC_IRQ_MSC", machine)
        self.assertIn("jz4740_msc_set_kick_callback", dma_bridge)
        self.assertIn("jz4740_msc_set_command_callback", dma_bridge)
        self.assertIn("bbk9588_dma_bridge_connect", machine)
        self.assertIn("jz4740_msc_get_diagnostics(s->sources.msc, &msc);", diag)
        self.assertIn("../sd/jz4740_msc.c", meson)
        self.assertNotIn("bbk9588_is_msc_window", machine)
        self.assertNotIn("BBK9588_MMIO_GRAPHICS", machine)
        self.assertNotIn('"bbk9588.msc"', machine)

    def test_qemu_jz4740_msc_response_dma_data_ready_and_irq(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        msc = 0x10021000
        dmac = 0x13020000
        intc_status = 0x10001000
        msc_irq = 1 << 14
        dma_target = 0x1000

        with _QTestClient(qemu) as client:
            self.assertEqual(client.readl(msc + 0x04), 0x40)
            self.assertEqual(client.readl(msc + 0x10), 0x40)
            self.assertEqual(client.readl(msc + 0x14), 0xFFFF)
            self.assertEqual(client.readl(msc + 0x24), 0xFF)
            self.assertEqual(client.readl(msc + 0x28), 0)

            client.writeb(msc + 0x14, 0x5A)
            client.writeb(msc + 0x15, 0xA5)
            self.assertEqual(client.readl(msc + 0x14), 0xA55A)

            client.writel(dma_target, 0xDEADBEEF)
            client.writel(msc + 0x24, 0xFC)
            client.writel(dmac + 0x00, 0)
            client.writel(dmac + 0x04, dma_target)
            client.writel(dmac + 0x08, 128)
            client.writel(dmac + 0x0C, 0)
            client.writel(dmac + 0x14, 0)
            client.writel(dmac + 0x10, 0x80000001)
            client.writel(dmac + 0x300, 1)

            client.writel(msc + 0x2C, 0x11)
            client.writel(msc + 0x30, 0x24600)
            client.writel(msc + 0x00, 6)

            self.assertEqual(client.readw(msc + 0x34), 0x1100)
            self.assertEqual(client.readb(msc + 0x34), 0)
            self.assertEqual(client.readl(dma_target), 0)
            self.assertEqual(client.readl(dmac + 0x08), 0)
            self.assertEqual(client.readl(msc + 0x28), 3)
            self.assertEqual(client.readl(intc_status) & msc_irq, msc_irq)

            client.writel(msc + 0x28, 3)
            self.assertEqual(client.readl(msc + 0x28), 0)
            self.assertEqual(client.readl(intc_status) & msc_irq, 0)

            dma_source = 0x1200
            channel1 = dmac + 0x20
            client.writel(dma_source, 0xCAFEBABE)
            client.writel(channel1 + 0x00, dma_source)
            client.writel(channel1 + 0x04, 0)
            client.writel(channel1 + 0x08, 128)
            client.writel(channel1 + 0x0C, 0)
            client.writel(channel1 + 0x14, 0)
            client.writel(channel1 + 0x10, 0x80000001)

            client.writel(msc + 0x2C, 0x18)
            client.writel(msc + 0x30, 0x40000)
            client.writel(msc + 0x00, 6)

            self.assertEqual(client.readb(msc + 0x34), 0x18)
            self.assertEqual(client.readl(dma_source), 0xCAFEBABE)
            self.assertEqual(client.readl(channel1 + 0x08), 0)
            self.assertEqual(client.readl(msc + 0x28), 3)
            self.assertEqual(client.readl(intc_status) & msc_irq, msc_irq)
            client.writel(msc + 0x28, 3)
            self.assertEqual(client.readl(intc_status) & msc_irq, 0)

    def test_bbk9588_nand_geometry_detection_uses_raw_oob_stride(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "block" / "bbk9588_nand.c").read_text(
            encoding="utf-8"
        )
        header = (root / "include" / "hw" / "block" / "bbk9588_nand.h").read_text(
            encoding="utf-8"
        )
        start = source.index("static void nand_detect_geometry")
        end = source.index("static bool nand_load_backing", start)
        detect = source[start:end]

        self.assertIn("s->size % BBK9588_NAND_RAW_STRIDE == 0", detect)
        self.assertIn("s->page_stride = BBK9588_NAND_RAW_STRIDE;", detect)
        self.assertIn("s->page_stride = BBK9588_NAND_PAGE_SIZE;", detect)
        self.assertNotIn("oob_map_valid", detect)
        self.assertNotIn("oob_logical_to_physical_block", source)
        self.assertNotIn("msc_oob_lba", source)
        self.assertIn("#define BBK9588_NAND_PAGE_SIZE       2048u", header)
        self.assertIn("#define BBK9588_NAND_SPARE_SIZE      64u", header)
        self.assertIn("#define BBK9588_NAND_PAGES_PER_BLOCK 64u", header)
        self.assertIn("#define BBK9588_NAND_BLOCKS          4096u", header)
        self.assertNotIn("last_oob_page", source)
        self.assertNotIn("0x809066c0u", source)
        self.assertNotIn("0x8090674cu", source)
        self.assertNotIn("ftl_map_valid", source)
        self.assertNotIn("ftl_logical_to_physical", source)
        self.assertNotIn("FAT16", detect)
        self.assertNotIn("fat16_layout", detect)
        self.assertNotIn("bbk9588_nand_geometry_score", source)
        self.assertNotIn("bbk9588_nand_sector_looks_fat16", source)

    def test_bbk9588_nand_program_erase_do_not_protect_fat_page_ranges(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "qemu/overlay/hw/block/bbk9588_nand.c"
        ).read_text(encoding="utf-8")
        program_start = source.index("static void nand_commit_program")
        program_end = source.index("static void nand_commit_erase", program_start)
        append_start = source.index("static void nand_append_program_data")
        append_end = source.index("static void nand_backend_update", append_start)
        erase_start = source.index("static void nand_commit_erase")
        erase_end = source.index("static uint32_t nand_read_data", erase_start)
        program = source[program_start:program_end]
        append = source[append_start:append_end]
        erase = source[erase_start:erase_end]

        self.assertIn("uint32_t program_start;", source)
        self.assertIn("s->program_start = s->program_column;", append)
        self.assertIn("column = MIN(s->program_start, s->program_len);", program)
        self.assertIn("s->program_len - column", program)
        self.assertIn("s->data[data_offset + i] &=", program)
        self.assertIn("memset(s->data + offset, 0xff, length);", erase)
        self.assertIn(
            "(uint64_t)BBK9588_NAND_PAGES_PER_BLOCK * stride", erase
        )
        self.assertEqual(erase.count("nand_backend_update(s, offset, length);"), 1)
        self.assertNotIn("for (uint32_t page = block_start", erase)
        self.assertNotIn("column = 0;", program)
        self.assertNotIn("BBK9588_NAND_READ_SOURCE_INITIAL", source)
        self.assertNotIn("initial_data", source)
        self.assertNotIn("g_memdup2(nand->data", source)
        self.assertNotIn("NAND_FAT_PROTECT", source)
        self.assertNotIn("nand-fat-protect", source)
        self.assertNotIn("nand_fat_protect", source)
        self.assertNotIn("bbk9588_nand_page_is_fat_protected", source)
        self.assertNotIn("bbk9588-nand-program-protect", source)
        self.assertNotIn("bbk9588-nand-erase-protect", source)

    def test_bbk9588_nand_uses_periodic_async_and_shutdown_flushes(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "qemu/overlay/hw/block/bbk9588_nand.c"
        ).read_text(encoding="utf-8")

        self.assertIn("#define NAND_FLUSH_INTERVAL_MS    1000u", source)
        self.assertIn("blk_aio_flush(s->blk, nand_flush_complete, s)", source)
        self.assertIn("timer_new_ms(QEMU_CLOCK_REALTIME", source)
        self.assertIn("qemu_register_shutdown_notifier", source)
        self.assertIn("ret = blk_flush(s->blk);", source)

    def test_qemu_bbk9588_nand_program_and_erase_failure_status(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        nand_data = 0x18000000
        nand_command = nand_data + 0x8000
        nand_address = nand_data + 0x10000
        stride = 2048 + 64
        pages_per_block = 64
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "failure.bin"
            raw = bytearray(b"\xff" * (stride * pages_per_block * 3))
            raw[2 * pages_per_block * stride] = 0x5A
            image.write_bytes(raw)
            with _QTestClient(
                qemu,
                nand_image=image,
                extra_args=(
                    "-global",
                    "bbk9588-nand.fail-program-block=1",
                    "-global",
                    "bbk9588-nand.fail-erase-block=2",
                ),
            ) as client:
                def address_page(page: int) -> None:
                    for value in (0, 0, page & 0xFF, (page >> 8) & 0xFF, 0):
                        client.writeb(nand_address, value)

                def read_first_byte(page: int) -> int:
                    client.writeb(nand_command, 0x00)
                    address_page(page)
                    client.writeb(nand_command, 0x30)
                    return client.readb(nand_data)

                failed_page = pages_per_block
                client.writeb(nand_command, 0x80)
                address_page(failed_page)
                client.writeb(nand_data, 0x00)
                client.writeb(nand_command, 0x10)
                client.writeb(nand_command, 0x70)
                self.assertEqual(client.readb(nand_data), 0x41)
                self.assertEqual(read_first_byte(failed_page), 0xFF)

                erase_page = 2 * pages_per_block
                client.writeb(nand_command, 0x60)
                for value in (erase_page & 0xFF, (erase_page >> 8) & 0xFF, 0):
                    client.writeb(nand_address, value)
                client.writeb(nand_command, 0xD0)
                client.writeb(nand_command, 0x70)
                self.assertEqual(client.readb(nand_data), 0x41)
                self.assertEqual(read_first_byte(erase_page), 0x5A)

                client.writeb(nand_command, 0x80)
                address_page(0)
                client.writeb(nand_data, 0xA5)
                client.writeb(nand_command, 0x10)
                client.writeb(nand_command, 0x70)
                self.assertEqual(client.readb(nand_data), 0x40)
                self.assertEqual(read_first_byte(0), 0xA5)
                time.sleep(1.2)
                self.assertIsNone(client.process.poll())

            persisted = image.read_bytes()
            self.assertEqual(persisted[0], 0xA5)
            self.assertEqual(persisted[pages_per_block * stride], 0xFF)
            self.assertEqual(
                persisted[2 * pages_per_block * stride],
                0x5A,
            )

    def test_bbk9588_nand_controller_source_follows_jz4740_ecc_register_semantics(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        emc = (root / "hw" / "mem" / "jz4740_emc.c").read_text(encoding="utf-8")
        nand = (root / "hw" / "block" / "bbk9588_nand.c").read_text(
            encoding="utf-8"
        )
        machine = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        meson = (root / "hw" / "mips" / "meson.build").read_text(encoding="utf-8")

        self.assertIn("#define EMC_NFCSR_RW_MASK          0x000000ffu", emc)
        self.assertIn("#define EMC_NFECCR_RW_MASK         0x0000000du", emc)
        self.assertIn("#define EMC_NFECCR_ERST            0x00000002u", emc)
        self.assertIn("#define EMC_NFECCR_PRDY            0x00000010u", emc)
        self.assertIn("#define EMC_NFINTE_RW_MASK         0x00000017u", emc)
        self.assertIn("case EMC_NFINTS:", emc)
        self.assertIn("*reg &= value | ~EMC_NFINTS_STATUS_MASK;", emc)
        self.assertIn("jz4740_rs_decode(s->ecc_data, parity, corrections);", emc)
        self.assertIn("bbk9588_nand_set_data_callback(nand, emc_nand_data, s);", emc)
        self.assertIn("s->data_callback(s->data_opaque, value, size, false);", nand)
        self.assertNotIn("ecc_status", nand)
        self.assertNotIn("ecc_busy_reads", nand)
        self.assertNotIn("bbk9588_nand_ecc_", nand)
        self.assertIn("qdev_new(TYPE_JZ4740_EMC)", machine)
        self.assertIn("qdev_new(TYPE_BBK9588_NAND)", machine)
        self.assertIn("jz4740_emc_attach_nand(board->emc, board->nand_dev);", machine)
        self.assertIn("../block/bbk9588_nand.c", meson)
        self.assertIn("../mem/jz4740_ecc.c", meson)
        self.assertIn("../mem/jz4740_emc.c", meson)
        self.assertNotIn("struct Bbk9588NandState {", machine)
        self.assertNotIn("BBK9588_NAND_NFCSR_OFF", machine)
        self.assertNotIn("bbk9588.extgpio", machine)

    def test_jz4740_rs_encoder_matches_known_erased_page_vector(self) -> None:
        expected = bytes.fromhex("cd9d9058f48bffb76f")

        self.assertEqual(jz4740_rs_encode(b"\xff" * 512), expected)
        self.assertEqual(
            jz4740_page_oob_ecc(b"\xff" * 2048),
            b"\xff" * 6 + expected * 4,
        )
        self.assertEqual(
            jz4740_page_oob_ecc(b"\xff" * 2048, offset=4),
            b"\xff" * 4 + expected * 4,
        )

    def test_qemu_jz4740_emc_ecc_mmio_encode_decode_status_and_irq(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        emc = 0x13010000
        nand_data = 0x18000000
        nand_command = nand_data + 0x8000
        nand_address = nand_data + 0x10000
        intc_status = 0x10001000
        nfeccr = emc + 0x100
        nfecc = emc + 0x104
        nfpar = (emc + 0x108, emc + 0x10C, emc + 0x110)
        nfints = emc + 0x114
        nfinte = emc + 0x118
        nferr = (emc + 0x11C, emc + 0x120, emc + 0x124, emc + 0x128)

        with _QTestClient(qemu) as client:
            client.writel(nfinte, 0xFFFFFFFF)
            self.assertEqual(client.readl(nfinte), 0x17)
            client.writel(nfeccr, 0x0F)
            self.assertEqual(client.readl(nfeccr), 0x0D)
            for _ in range(128):
                client.writel(nand_data, 0xFFFFFFFF)
            self.assertEqual(client.readl(nfpar[0]), 0x58909DCD)
            self.assertEqual(client.readl(nfpar[1]), 0xB7FF8BF4)
            self.assertEqual(client.readl(nfpar[2]), 0x6F)
            self.assertEqual(client.readl(nfints), 0x04)
            self.assertEqual(client.readl(intc_status) & (1 << 3), 1 << 3)
            client.writel(nfints, 0)
            self.assertEqual(client.readl(nfints), 0)
            self.assertEqual(client.readl(intc_status) & (1 << 3), 0)
            client.writel(nfeccr, 0x02)
            client.writel(nfeccr, 0x01)
            for _ in range(128):
                client.writel(nand_data, 0xFFFFFFFF)
            self.assertEqual(client.readl(nfecc), 0x00FFFFFF)

        original = bytes((index * 37 + index // 7 + 0x5A) & 0xFF for index in range(512))
        parity = jz4740_rs_encode(original)
        pages = []
        for error_bytes in ((0, 64, 128, 192), (0, 64, 128, 192, 256)):
            page = bytearray(original + b"\xff" * (2048 - len(original)))
            for byte in error_bytes:
                page[byte] ^= 1
            pages.append(page + b"\xff" * 64)

        def read_first_ecc_block(client: _QTestClient, page: int) -> None:
            client.writeb(nand_command, 0x00)
            for address_byte in (0, 0, page & 0xFF, (page >> 8) & 0xFF, 0):
                client.writeb(nand_address, address_byte)
            client.writeb(nand_command, 0x30)
            client.writel(nfeccr, 0x07)
            for _ in range(128):
                client.readl(nand_data)
            self.assertEqual(client.readl(nfints), 0x10)
            client.writel(nfpar[0], int.from_bytes(parity[:4], "little"))
            client.writel(nfpar[1], int.from_bytes(parity[4:8], "little"))
            client.writel(nfpar[2], parity[8])
            client.writel(nfeccr, 0x15)

        with tempfile.TemporaryDirectory() as tmp:
            nand = Path(tmp) / "ecc-mmio.bin"
            nand.write_bytes(b"".join(pages))
            with _QTestClient(qemu, nand_image=nand) as client:
                client.writel(nfinte, 0xFFFFFFFF)
                read_first_ecc_block(client, 0)
                self.assertEqual(client.readl(nfints), 0x80000019)
                self.assertTrue(all(client.readl(address) != 0 for address in nferr))
                self.assertEqual(client.readl(intc_status) & (1 << 3), 1 << 3)
                client.writel(nfints, 0)
                self.assertEqual(client.readl(nfints), 0x80000000)
                self.assertEqual(client.readl(intc_status) & (1 << 3), 0)

                read_first_ecc_block(client, 1)
                self.assertEqual(client.readl(nfints), 0x1B)
                self.assertTrue(all(client.readl(address) == 0 for address in nferr))
                self.assertEqual(client.readl(intc_status) & (1 << 3), 1 << 3)

    def test_bbk9588_uart_source_follows_jz4740_16550_semantics(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "char" / "jz4740_uart.c").read_text(
            encoding="utf-8"
        )
        machine = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        meson = (root / "hw" / "mips" / "meson.build").read_text(
            encoding="utf-8"
        )

        self.assertIn("#define UART_FIFO_SIZE              16u", source)
        self.assertIn("#define UART_IER                    0x04u", source)
        self.assertIn("#define UART_IIR                    0x08u", source)
        self.assertIn("#define UART_FCR                    0x08u", source)
        self.assertIn("#define UART_LSR                    0x14u", source)
        self.assertIn("#define UART_LSR_RESET", source)
        self.assertIn("#define UART_LCR_DLAB               0x80u", source)
        self.assertIn("bool thr_irq_latched;", source)
        self.assertIn("static void uart_latch_thr_irq", source)
        self.assertIn("static uint8_t uart_iir_value", source)
        self.assertIn("static unsigned uart_rx_trigger_level", source)
        self.assertIn("s->thr_irq_latched &&", source)
        self.assertIn("case UART_RBR:", source)
        self.assertIn("s->lcr & UART_LCR_DLAB", source)
        self.assertIn("case UART_IER:", source)
        self.assertIn("case UART_FCR:", source)
        self.assertIn("value & UART_FCR_TFRT", source)
        self.assertIn("case UART_LSR:", source)
        self.assertIn("(value & 0x0fu) == UART_IIR_TDR", source)
        self.assertIn("s->thr_irq_latched = false;", source)
        self.assertIn("s->fcr = value & (UART_FCR_FME | UART_FCR_DME |", source)
        self.assertIn("s->status = UART_LSR_RESET;", source)
        self.assertIn("vmstate_jz4740_uart", source)
        self.assertIn("qdev_new(TYPE_JZ4740_UART)", machine)
        self.assertIn("JZ4740_INTC_IRQ_UART0", machine)
        self.assertIn("../char/jz4740_uart.c", meson)
        self.assertNotIn("BBK9588_MMIO_UART", machine)
        self.assertNotIn("uart_thr_irq_latched", machine)
        self.assertNotIn("bbk9588_uart_", machine)

    def test_qemu_jz4740_uart_mmio_loopback_dlab_and_irq(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        uart = 0x10030000
        intc_status = 0x10001000
        uart_irq = 1 << 9

        with _QTestClient(qemu) as client:
            self.assertEqual(client.readb(uart + 0x14), 0x60)
            self.assertEqual(client.readb(uart + 0x08), 0x01)

            client.writeb(uart + 0x08, 0x11)
            client.writeb(uart + 0x10, 0x92)
            client.writeb(uart + 0x04, 0x01)
            client.writeb(uart, 0x5A)
            self.assertEqual(client.readb(uart + 0x14), 0x61)
            self.assertEqual(client.readb(uart + 0x08), 0xC4)
            self.assertEqual(client.readl(intc_status) & uart_irq, uart_irq)
            self.assertEqual(client.readb(uart), 0x5A)
            self.assertEqual(client.readb(uart + 0x14), 0x60)
            self.assertEqual(client.readl(intc_status) & uart_irq, 0)

            client.writeb(uart + 0x0C, 0x80)
            client.writeb(uart, 0xAB)
            client.writeb(uart + 0x04, 0x1C)
            self.assertEqual(client.readb(uart), 0xAB)
            self.assertEqual(client.readb(uart + 0x04), 0x1C)
            client.writeb(uart + 0x0C, 0)

            client.writeb(uart + 0x04, 0x02)
            self.assertEqual(client.readl(intc_status) & uart_irq, uart_irq)
            self.assertEqual(client.readb(uart + 0x08), 0xC2)
            self.assertEqual(client.readl(intc_status) & uart_irq, 0)
            self.assertEqual(client.readb(uart + 0x08), 0xC1)

    def test_bbk9588_udc_source_follows_jz4740_no_host_register_semantics(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qemu" / "overlay"
        source = (root / "hw" / "usb" / "jz4740_udc.c").read_text(
            encoding="utf-8"
        )
        machine = (root / "hw" / "mips" / "bbk9588.c").read_text(
            encoding="utf-8"
        )
        meson = (root / "hw" / "mips" / "meson.build").read_text(
            encoding="utf-8"
        )

        self.assertIn("#define UDC_POWER_RESET            0x20u", source)
        self.assertIn("#define UDC_INTRINE_RESET          0xffffu", source)
        self.assertIn("#define UDC_INTROUTE_RESET         0xfffeu", source)
        self.assertIn("#define UDC_INTRUSBE_RESET         0x06u", source)
        self.assertIn("#define UDC_INTRIN_ENDPOINT_MASK   0x000fu", source)
        self.assertIn("#define UDC_INTROUT_ENDPOINT_MASK  0x0006u", source)
        self.assertIn("#define UDC_EPINFO_VALUE           0x23u", source)
        self.assertIn("static bool udc_irq_pending", source)
        self.assertIn("static bool udc_in_ep_valid", source)
        self.assertIn("static bool udc_out_ep_valid", source)
        self.assertIn("static uint8_t udc_read_byte", source)
        self.assertIn("static void udc_write", source)
        self.assertIn("case UDC_POWER:", source)
        self.assertIn("case UDC_INTRINE:", source)
        self.assertIn("case UDC_INTROUTE:", source)
        self.assertIn("case UDC_INTRUSBE:", source)
        self.assertIn("case UDC_EPINFO:", source)
        self.assertIn("vmstate_jz4740_udc", source)
        self.assertIn("qdev_new(TYPE_JZ4740_UDC)", machine)
        self.assertIn("JZ4740_INTC_IRQ_UDC", machine)
        self.assertIn("../usb/jz4740_udc.c", meson)
        self.assertNotIn("BBK9588_MMIO_UDC", machine)
        self.assertNotIn("BBK9588_UDC_", machine)
        self.assertNotIn("bbk9588_udc_", machine)

    def test_qemu_jz4740_udc_mmio_reset_masks_and_indexed_endpoints(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        udc = 0x13040000
        intc_status = 0x10001000
        udc_irq = 1 << 24

        with _QTestClient(qemu) as client:
            self.assertEqual(client.readb(udc + 0x00), 0)
            self.assertEqual(client.readb(udc + 0x01), 0x20)
            self.assertEqual(client.readw(udc + 0x06), 0x000F)
            self.assertEqual(client.readw(udc + 0x08), 0x0006)
            self.assertEqual(client.readb(udc + 0x0B), 0x06)
            self.assertEqual(client.readb(udc + 0x78), 0x23)
            self.assertEqual(client.readb(udc + 0x79), 0)

            client.writeb(udc + 0x00, 0x55)
            client.writeb(udc + 0x01, 0xFF)
            client.writew(udc + 0x06, 0xFFFF)
            client.writew(udc + 0x08, 0xFFFF)
            client.writeb(udc + 0x0B, 0xFF)
            client.writeb(udc + 0x0F, 0xFF)
            self.assertEqual(client.readb(udc + 0x00), 0xD5)
            self.assertEqual(client.readb(udc + 0x01), 0xE5)
            self.assertEqual(client.readw(udc + 0x06), 0x000F)
            self.assertEqual(client.readw(udc + 0x08), 0x0006)
            self.assertEqual(client.readb(udc + 0x0B), 0x0F)
            self.assertEqual(client.readb(udc + 0x0F), 0x3F)

            client.writeb(udc + 0x0E, 1)
            client.writew(udc + 0x10, 0xFFFF)
            client.writew(udc + 0x12, 0xFFFF)
            client.writew(udc + 0x14, 0xFFFF)
            client.writew(udc + 0x16, 0xFFFF)
            self.assertEqual(client.readw(udc + 0x10), 0x07FF)
            self.assertEqual(client.readw(udc + 0x12), 0xFC10)
            self.assertEqual(client.readw(udc + 0x14), 0x07FF)
            self.assertEqual(client.readw(udc + 0x16), 0xF820)

            client.writeb(udc + 0x0E, 3)
            client.writew(udc + 0x10, 0x0123)
            client.writew(udc + 0x14, 0x0456)
            self.assertEqual(client.readw(udc + 0x10), 0x0123)
            self.assertEqual(client.readw(udc + 0x14), 0)

            client.writeb(udc + 0x0E, 4)
            client.writew(udc + 0x10, 0x0789)
            self.assertEqual(client.readw(udc + 0x10), 0)
            self.assertEqual(client.readw(udc + 0x02), 0)
            self.assertEqual(client.readw(udc + 0x04), 0)
            self.assertEqual(client.readb(udc + 0x0A), 0)
            self.assertEqual(client.readl(intc_status) & udc_irq, 0)

    def test_qemu_subprocess_env_adds_msys_paths_for_source_build(self) -> None:
        env = qemu_subprocess_env(r"E:\qemu-src\build-bbk9588-win\qemu-system-mipsel.exe")

        path = env.get("PATH", "").replace("\\", "/").lower()
        self.assertIn("c:/msys64/ucrt64/bin", path)

    def test_qemu_process_backend_uses_below_normal_priority_on_windows(self) -> None:
        calls: dict[str, object] = {}

        class FakeProcess:
            stdout: list[str] = []
            stderr: list[str] = []
            returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = 0
                return 0

            def kill(self) -> None:
                self.returncode = -9

        def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
            calls["args"] = args
            calls["kwargs"] = kwargs
            return FakeProcess()

        with tempfile.TemporaryDirectory() as tmp:
            nand = Path(tmp) / "nand.bin"
            nand.write_bytes(b"\xff" * 4096)
            config = QemuSystemConfig(
                executable=sys.executable,
                machine="bbk9588",
                monitor="none",
                gdb="none",
                bbk_input="none",
                bbk_frame="none",
                bbk_machine_options=("bootrom-nand=on",),
                nand_image=nand,
            )
            backend = QemuProcessBackend(config)
            with mock.patch.object(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, create=True):
                with mock.patch.object(subprocess, "Popen", side_effect=fake_popen):
                    backend.start()
                    backend.stop()
            nand_survived = nand.exists()

        kwargs = calls.get("kwargs")
        self.assertIsInstance(kwargs, dict)
        assert isinstance(kwargs, dict)
        self.assertEqual(kwargs.get("creationflags"), 0x4000)
        popen_args = calls.get("args")
        self.assertIsInstance(popen_args, tuple)
        assert isinstance(popen_args, tuple)
        command = popen_args[0]
        self.assertIn(nand.resolve().as_posix(), " ".join(command))
        self.assertNotIn("qemu_nand_runs", " ".join(command))
        self.assertIn("-S", command)
        self.assertTrue(nand_survived)

    def test_qemu_process_backend_resumes_paused_debug_stub_with_stop_query(self) -> None:
        class FakeProcess:
            stdout: list[str] = []
            stderr: list[str] = []
            returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = 0
                return 0

            def kill(self) -> None:
                self.returncode = -9

        with tempfile.TemporaryDirectory() as tmp:
            nand = Path(tmp) / "nand.bin"
            nand.write_bytes(b"\xff" * 4096)
            config = QemuSystemConfig(
                executable=sys.executable,
                machine="bbk9588",
                monitor="none",
                gdb="auto",
                bbk_input="null,id=bbk9588-input",
                bbk_frame="null,id=bbk9588-frame",
                bbk_machine_options=("bootrom-nand=on",),
                nand_image=nand,
                extra_args=("-qmp", "none"),
            )
            backend = QemuProcessBackend(config)
            gdb_socket = mock.MagicMock()
            hmp_socket = mock.MagicMock()
            with (
                mock.patch.object(subprocess, "Popen", return_value=FakeProcess()),
                mock.patch("emu.qemu.system._connect_hmp", return_value=hmp_socket),
                mock.patch("emu.qemu.system._connect_gdb", return_value=gdb_socket),
                mock.patch("emu.qemu.system._gdb_packet", return_value="T05core:01;") as query,
                mock.patch("emu.qemu.system._gdb_continue") as resume,
                mock.patch(
                    "emu.qemu.system._hmp_command",
                    side_effect=lambda _sock, command: (
                        "VM status: paused" if command == "info status" else ""
                    ),
                ) as hmp_command,
            ):
                backend.start()
                self.assertEqual(hmp_command.call_args_list[0], mock.call(hmp_socket, "info status"))
                query.assert_called_once_with(gdb_socket, "?")
                resume.assert_called_once_with(gdb_socket)
                self.assertIs(backend.gdb_sock, gdb_socket)
                backend.stop()

        gdb_socket.close.assert_called_once_with()

    @unittest.skipUnless(os.name == "nt", "Windows Job Object test")
    def test_windows_job_handle_close_terminates_child_process(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            handle, error = _assign_windows_kill_on_close_job(proc)
            self.assertIsNone(error)
            self.assertIsNotNone(handle)
            _close_windows_job(handle)
            proc.wait(timeout=5)
            self.assertIsNotNone(proc.returncode)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_qemu_process_backend_stop_prefers_hmp_quit(self) -> None:
        class FakeProcess:
            returncode: int | None = None
            terminate_count = 0
            kill_count = 0

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminate_count += 1
                self.returncode = -15

            def wait(self, timeout: float | None = None) -> int:
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("qemu", timeout)
                return self.returncode

            def kill(self) -> None:
                self.kill_count += 1
                self.returncode = -9

        class FakeSocket:
            closed = False

            def close(self) -> None:
                self.closed = True

        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        process = FakeProcess()
        hmp = FakeSocket()
        backend.proc = process  # type: ignore[assignment]
        backend.hmp_sock = hmp  # type: ignore[assignment]

        def fake_hmp_command(sock: object, command: str) -> str:
            self.assertIs(sock, hmp)
            self.assertEqual(command, "quit")
            process.returncode = 0
            return ""

        with mock.patch("emu.qemu.system._hmp_command", side_effect=fake_hmp_command) as hmp_command:
            backend.stop()

        hmp_command.assert_called_once()
        self.assertEqual(process.terminate_count, 0)
        self.assertEqual(process.kill_count, 0)
        self.assertTrue(hmp.closed)
        self.assertEqual(backend.returncode, 0)
        self.assertEqual(backend.exit_reason, "user-stop")

    def test_qemu_process_backend_stop_accepts_hmp_disconnect_after_quit(self) -> None:
        class FakeProcess:
            returncode: int | None = None
            terminate_count = 0

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminate_count += 1
                self.returncode = -15

            def wait(self, timeout: float | None = None) -> int:
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("qemu", timeout)
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9

        class FakeSocket:
            def close(self) -> None:
                pass

        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        process = FakeProcess()
        backend.proc = process  # type: ignore[assignment]
        backend.hmp_sock = FakeSocket()  # type: ignore[assignment]

        def disconnect_after_quit(sock: object, command: str) -> str:
            process.returncode = 0
            raise ConnectionResetError("monitor closed")

        with mock.patch("emu.qemu.system._hmp_command", side_effect=disconnect_after_quit):
            backend.stop()

        self.assertEqual(process.terminate_count, 0)
        self.assertIsNone(backend.last_error)
        self.assertEqual(backend.returncode, 0)
        self.assertEqual(backend.exit_reason, "user-stop")

    def test_qemu_process_backend_classifies_unrequested_process_exit(self) -> None:
        class FakeProcess:
            def __init__(self, returncode: int):
                self.returncode = returncode
                self.pid = 1

            def poll(self) -> int:
                return self.returncode

        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.proc = FakeProcess(0)  # type: ignore[assignment]
        normal = backend.snapshot(refresh=False)

        self.assertFalse(normal["running"])
        self.assertEqual(normal["exit_reason"], "process-exit")
        self.assertEqual(normal["returncode"], 0)

        guest_shutdown = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        guest_shutdown._handle_qmp_message_locked({
            "event": "SHUTDOWN",
            "data": {"guest": True, "reason": "guest-shutdown"},
        })
        guest_shutdown.proc = FakeProcess(0)  # type: ignore[assignment]
        shutdown = guest_shutdown.snapshot(refresh=False)

        self.assertEqual(shutdown["exit_reason"], "guest-shutdown")
        self.assertEqual(shutdown["qmp_last_event"], {
            "event": "SHUTDOWN",
            "data": {"guest": True, "reason": "guest-shutdown"},
            "timestamp": None,
        })

        failed = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        failed.proc = FakeProcess(7)  # type: ignore[assignment]
        error = failed.snapshot(refresh=False)

        self.assertEqual(error["exit_reason"], "process-error")
        self.assertEqual(error["returncode"], 7)

    def test_frontend_stop_detail_preserves_guest_shutdown_reason(self) -> None:
        state = FrontendState.__new__(FrontendState)
        state.last_error = None
        state.job_finished_at = None

        detail = state._stop_detail(
            False,
            {
                "exit_reason": "guest-shutdown",
                "returncode": 0,
                "finished_at": 123.0,
                "last_error": "HMP ConnectionResetError: monitor closed",
            },
        )

        self.assertEqual(detail, {
            "code": "guest-shutdown",
            "returncode": 0,
            "occurred_at": 123.0,
            "expected": True,
            "error": None,
        })

    @staticmethod
    def _make_safe_shutdown_state(backend) -> FrontendState:
        state = FrontendState.__new__(FrontendState)
        state.lock = threading.RLock()
        state.input_lock = threading.RLock()
        state.pending_keys = deque()
        state.qemu_backend = backend
        state.frontend_key_leases = {("page", 11): time.monotonic() + 60.0}
        state.frontend_active_key_codes = {11}
        state.last_input_event = None
        state.input_wake_count = 0
        state.safe_shutdown_state = "idle"
        state.safe_shutdown_started_at = None
        state.safe_shutdown_finished_at = None
        state.safe_shutdown_error = None
        state.usb_power_connected = True
        state._publish_snapshot_locked = lambda: None  # type: ignore[method-assign]
        return state

    def test_frontend_safe_shutdown_holds_power_until_guest_shutdown(self) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.running = True
                self.events: list[tuple[int, bool]] = []
                self.usb_power_events: list[bool] = []

            def snapshot(self, *, refresh: bool = False) -> dict[str, object]:
                return {
                    "running": self.running,
                    "exit_reason": None if self.running else "guest-shutdown",
                    "returncode": None if self.running else 0,
                }

            def apply_gui_key_event(self, code: int, down: bool) -> dict[str, object]:
                self.events.append((code, down))
                if not down:
                    self.running = False
                return {"applied": True, "code": code, "down": down}

            def apply_usb_power_state(self, connected: bool) -> dict[str, object]:
                self.usb_power_events.append(connected)
                return {"applied": True, "connected": connected}

        backend = FakeBackend()
        state = self._make_safe_shutdown_state(backend)

        snapshot = state._request_guest_shutdown_locked(hold_seconds=0.0, timeout=0.2)

        self.assertEqual(backend.events, [(11, True), (11, False)])
        self.assertEqual(backend.usb_power_events, [False])
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["exit_reason"], "guest-shutdown")
        self.assertEqual(state.safe_shutdown_state, "complete")
        self.assertEqual(state.frontend_key_leases, {})
        self.assertEqual(state.frontend_active_key_codes, set())

    def test_frontend_safe_shutdown_timeout_keeps_qemu_running(self) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.events: list[tuple[int, bool]] = []
                self.usb_power_events: list[bool] = []

            def snapshot(self, *, refresh: bool = False) -> dict[str, object]:
                return {"running": True, "exit_reason": None, "returncode": None}

            def apply_gui_key_event(self, code: int, down: bool) -> dict[str, object]:
                self.events.append((code, down))
                return {"applied": True, "code": code, "down": down}

            def apply_usb_power_state(self, connected: bool) -> dict[str, object]:
                self.usb_power_events.append(connected)
                return {"applied": True, "connected": connected}

        backend = FakeBackend()
        state = self._make_safe_shutdown_state(backend)

        with self.assertRaisesRegex(RuntimeError, "firmware did not complete"):
            state._request_guest_shutdown_locked(hold_seconds=0.0, timeout=0.02)

        self.assertEqual(backend.events, [(11, True), (11, False)])
        self.assertEqual(backend.usb_power_events, [False, True])
        self.assertEqual(state.safe_shutdown_state, "failed")
        self.assertIn("TimeoutError", state.safe_shutdown_error or "")

    def test_frontend_reset_host_stops_when_firmware_has_no_frame(self) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.running = True
                self.stop_count = 0

            def snapshot(self, *, refresh: bool = False) -> dict[str, object]:
                return {"running": self.running, "frame_chardev_count": 0}

            def stop(self) -> None:
                self.stop_count += 1
                self.running = False

        backend = FakeBackend()
        state = FrontendState.__new__(FrontendState)
        state._request_guest_shutdown_locked = mock.Mock()  # type: ignore[method-assign]

        fallback = state._stop_backend_for_reset_locked(backend)  # type: ignore[arg-type]

        self.assertIsNotNone(fallback)
        self.assertEqual(backend.stop_count, 1)
        state._request_guest_shutdown_locked.assert_not_called()
        self.assertIn("固件尚未完成启动", fallback[0])  # type: ignore[index]

    def test_frontend_reset_falls_back_after_guest_shutdown_failure(self) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.running = True
                self.stop_count = 0

            def snapshot(self, *, refresh: bool = False) -> dict[str, object]:
                return {"running": self.running, "frame_chardev_count": 3}

            def stop(self) -> None:
                self.stop_count += 1
                self.running = False

        backend = FakeBackend()
        state = FrontendState.__new__(FrontendState)
        state._request_guest_shutdown_locked = mock.Mock(  # type: ignore[method-assign]
            side_effect=RuntimeError("safe shutdown failed: timeout")
        )

        fallback = state._stop_backend_for_reset_locked(  # type: ignore[arg-type]
            backend,
            shutdown_timeout=0.02,
        )

        self.assertIsNotNone(fallback)
        self.assertEqual(backend.stop_count, 1)
        state._request_guest_shutdown_locked.assert_called_once_with(timeout=0.02)
        self.assertIn("safe shutdown failed: timeout", fallback[1])  # type: ignore[index]

    def test_frontend_force_stop_command_uses_explicit_force_path(self) -> None:
        state = FrontendState.__new__(FrontendState)
        state.force_stop = mock.Mock(return_value={"forced": True})  # type: ignore[method-assign]

        result = state.command({"op": "force-stop"})

        self.assertEqual(result, {"forced": True})
        state.force_stop.assert_called_once_with()

    def test_frontend_key_lease_coalesces_heartbeats_and_releases_stale_keys(self) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.events: list[tuple[int, bool]] = []

            def running(self) -> bool:
                return True

            def apply_gui_key_event(self, code: int, down: bool) -> dict[str, object]:
                self.events.append((code, down))
                return {"applied": True, "code": code, "down": down}

        backend = FakeBackend()
        state = FrontendState.__new__(FrontendState)
        state.lock = threading.RLock()
        state.input_lock = threading.RLock()
        state.pending_keys = deque()
        state.qemu_backend = backend
        state.frontend_key_leases = {}
        state.frontend_active_key_codes = set()
        state.frontend_key_lease_expiration_count = 0
        state.last_input_event = None
        state.input_wake_count = 0
        state._ensure_qemu_started_locked = lambda: backend
        state._notify_frontend_activity = lambda: None

        state.key(6, True, include_snapshot=False, input_session="page-a", source="gamepad")
        state.key(6, True, include_snapshot=False, input_session="page-a", source="heartbeat")
        state.key(6, True, include_snapshot=False, input_session="page-b", source="gamepad")
        state.key(6, False, include_snapshot=False, input_session="page-a", source="gamepad")
        self.assertEqual(backend.events, [(6, True)])

        state.key(6, False, include_snapshot=False, input_session="page-b", source="gamepad")
        self.assertEqual(backend.events, [(6, True), (6, False)])

        state.key(6, True, include_snapshot=False, input_session="page-a", source="gamepad")
        state.frontend_key_leases[("page-a", 6)] = 0.0
        state._expire_frontend_key_leases_locked()
        self.assertEqual(backend.events[-2:], [(6, True), (6, False)])
        self.assertEqual(state.frontend_active_key_codes, set())
        self.assertEqual(state.frontend_key_lease_expiration_count, 1)
        self.assertEqual(state.last_input_event["source"], "frontend-key-lease-expired")

    def test_qemu_process_backend_reports_guest_shutdown_lifecycle(self) -> None:
        qemu = find_qemu()
        if qemu is None:
            self.skipTest("qemu-system-mipsel is not installed")

        words = (
            0x3C08B000,  # lui t0, 0xb000
            0x35083020,  # ori t0, t0, 0x3020 (RTC HCR)
            0x24090001,  # addiu t1, zero, 1
            0xAD090000,  # sw t1, 0(t0)
            0x08001004,  # j 0x80004010
            0x00000000,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "rtc-powerdown.bin"
            image.write_bytes(struct.pack(f"<{len(words)}I", *words))
            config = build_bbk_qemu_config(
                boot_mode="c200",
                executable=qemu,
                image=image,
                firmware_patches=("none",),
                extra_args=("-S",),
            )
            backend = QemuProcessBackend(config)
            try:
                backend.start()
                backend.refresh()
                assert backend.proc is not None
                self.assertEqual(backend.proc.wait(timeout=5), 0)
                deadline = time.time() + 2.0
                snapshot = backend.snapshot(refresh=False)
                while snapshot.get("exit_reason") != "guest-shutdown" and time.time() < deadline:
                    time.sleep(0.02)
                    snapshot = backend.snapshot(refresh=False)
            finally:
                backend.stop()

        self.assertEqual(snapshot.get("exit_reason"), "guest-shutdown", snapshot)
        self.assertIsNotNone(snapshot.get("qmp_port"))
        self.assertIsNone(snapshot.get("last_qmp_error"))
        self.assertTrue(any(
            "bbk9588: RTC HCR.PD requested guest shutdown" in str(line)
            for line in snapshot.get("stderr_tail", [])
        ))

    def test_qemu_wav_capture_header_is_finalized_and_pcm_is_analyzed(self) -> None:
        samples = [
            0,
            0,
            1000,
            1000,
            -2000,
            -2000,
            4000,
            4000,
        ] * 2000
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            0,
            b"WAVE",
            b"fmt ",
            16,
            1,
            2,
            8000,
            32000,
            4,
            16,
            b"data",
            0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "qemu.wav"
            capture.write_bytes(header + pcm)

            self.assertTrue(finalize_qemu_wav_header(capture))
            self.assertFalse(finalize_qemu_wav_header(capture))
            finalized = capture.read_bytes()[:44]
            self.assertEqual(struct.unpack_from("<I", finalized, 4)[0], 36 + len(pcm))
            self.assertEqual(struct.unpack_from("<I", finalized, 40)[0], len(pcm))

            analysis = analyze_pcm_wav(capture)
            validate_audio_regression(
                analysis,
                expected_sample_rate=8000,
                min_duration_seconds=0.5,
                min_peak_abs=1000,
                min_significant_ratio=0.5,
            )

        self.assertEqual(analysis.channels, 2)
        self.assertEqual(analysis.sample_rate_hz, 8000)
        self.assertEqual(analysis.bits_per_sample, 16)
        self.assertEqual(analysis.frames, len(samples) // 2)
        self.assertEqual(analysis.peak_abs, 4000)
        self.assertEqual(analysis.stereo_mismatch_frames, 0)
        self.assertEqual(analysis.clipped_samples, 0)
        self.assertAlmostEqual(analysis.nonzero_ratio, 0.75)

    def test_qemu_wav_header_rejects_inconsistent_nonzero_lengths(self) -> None:
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            123,
            b"WAVE",
            b"fmt ",
            16,
            1,
            2,
            8000,
            32000,
            4,
            16,
            b"data",
            4,
        )
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "bad.wav"
            capture.write_bytes(header + b"\0\0\0\0")
            with self.assertRaisesRegex(ValueError, "RIFF length"):
                finalize_qemu_wav_header(capture)

    def test_cli_dry_run_emits_nand_first_stage_command_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nand = Path(tmp) / "nand.bin"
            nand.write_bytes(b"\xff" * 0x1000)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "emu.qemu_app",
                    "--nand-image",
                    str(nand),
                    "--machine",
                    "bbk9588",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        command = json.loads(completed.stdout)["command"]
        machine_arg = command[command.index("-M") + 1]
        self.assertIn("bootrom-nand=on", machine_arg)
        self.assertNotIn("bootrom-page=0x0", machine_arg)
        self.assertNotIn("bootrom-size=0x2000", machine_arg)
        self.assertNotIn("bootrom-fat-kernel=on", machine_arg)
        self.assertIn("firmware-phys=0x0", machine_arg)
        self.assertIn("reset-pc=0x80000004", machine_arg)
        self.assertIn("-drive", command)
        self.assertNotIn("-kernel", command)
        self.assertFalse(any("C200.bin" in arg for arg in command), command)
        self.assertFalse(any("u_boot_9588_4740.bin" in arg for arg in command), command)
        self.assertFalse(any(arg.startswith("loader,file=") for arg in command), command)

    def test_cli_dry_run_emits_uboot_nand_first_stage_command_when_requested(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "emu.qemu_app",
                "--boot-mode",
                "uboot",
                "--machine",
                "bbk9588",
                "--dry-run",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        command = json.loads(completed.stdout)["command"]
        machine_arg = command[command.index("-M") + 1]
        self.assertIn("bootrom-nand=on", machine_arg)
        self.assertNotIn("bootrom-page=0x40", machine_arg)
        self.assertNotIn("bootrom-size=0x80000", machine_arg)
        self.assertIn("firmware-phys=0x0", machine_arg)
        self.assertIn("reset-pc=0x80000004", machine_arg)
        self.assertNotIn("-kernel", command)
        self.assertFalse(any("C200.bin" in arg for arg in command), command)
        self.assertFalse(any("u_boot_9588_4740.bin" in arg for arg in command), command)
        self.assertFalse(any(arg.startswith("loader,file=") for arg in command), command)

    def test_public_cli_help_marks_machine_options_and_firmware_patches_diagnostic(self) -> None:
        qemu_app_help = subprocess.run(
            [sys.executable, "-m", "emu.qemu_app", "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        frontend_help = subprocess.run(
            [sys.executable, "-m", "emu.web.frontend", "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(qemu_app_help.returncode, 0, qemu_app_help.stderr)
        self.assertEqual(frontend_help.returncode, 0, frontend_help.stderr)
        combined_help = qemu_app_help.stdout + frontend_help.stdout
        self.assertIn("diagnostic bbk9588 -M option", combined_help)
        self.assertIn("progress-trace=on", combined_help)
        self.assertIn("Legacy diagnostic QEMU-only firmware patch", combined_help)
        self.assertNotIn("synthetic-wait-wake", combined_help)

    def test_cli_dry_run_emits_uboot_payload_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            boot = root / "u_boot_9588_4740.bin"
            payload = root / "C200.bin"
            boot.write_bytes(b"\0" * 4)
            payload.write_bytes(b"\0" * 4)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "emu.qemu_app",
                    "--boot-mode",
                    "uboot",
                    "--image",
                    str(boot),
                    "--payload",
                    str(payload),
                    "--machine",
                    "malta",
                    "--qemu-firmware-patch",
                    "none",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        command = json.loads(completed.stdout)["command"]
        boot_qemu = str(boot.resolve()).replace("\\", "/")
        self.assertIn(f"loader,file={boot_qemu},addr=0x900000,force-raw=on", command)
        payload_qemu = str(payload.resolve()).replace("\\", "/")
        self.assertIn(f"loader,file={payload_qemu},addr=0x4000,force-raw=on", command)
        self.assertIn("loader,addr=0x80900000,cpu-num=0", command)

    def test_make_combined_nand_places_loader_backup_and_raw_uboot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loader = root / "loader_9588_4740.bin"
            uboot = root / "u_boot_9588_4740.bin"
            fat = root / "fat.img"
            out = root / "nand.bin"
            loader_bytes = b"LOADER-FIRST-STAGE" * 140
            uboot_bytes = b"UBOOT-PAYLOAD-0123456789"
            fat_bytes = b"FATDATA-0123456789"
            loader.write_bytes(loader_bytes)
            uboot.write_bytes(uboot_bytes)
            fat.write_bytes(fat_bytes)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "tools" / "make_combined_nand.py"),
                    "--loader-image",
                    str(loader),
                    "--loader-page-base",
                    "0",
                    "--uboot-image",
                    str(uboot),
                    "--uboot-page-base",
                    "8",
                    "--uboot-loader-copy-bytes",
                    "0",
                    "--uboot-load-phys",
                    "0x900000",
                    "--uboot-entry",
                    "0x80900000",
                    "--fat-image",
                    str(fat),
                    "--fat-page-base",
                    "12",
                    "--output",
                    str(out),
                    "--page-size",
                    "2048",
                    "--spare-size",
                    "64",
                    "--free-blocks",
                    "0",
                    "--pages-per-block",
                    "4",
                    "--physical-blocks",
                    "4",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            data = out.read_bytes()

        page_size = 2048
        spare_size = 64
        stride = page_size + spare_size

        def page_data(page: int) -> bytes:
            off = page * stride
            return data[off : off + page_size]

        def page_spare(page: int) -> bytes:
            off = page * stride + page_size
            return data[off : off + spare_size]

        self.assertEqual(len(data), 4 * 4 * stride)
        self.assertEqual((page_data(0) + page_data(1))[: len(loader_bytes)], loader_bytes)
        self.assertEqual((page_data(4) + page_data(5))[: len(loader_bytes)], loader_bytes)
        self.assertNotEqual(page_data(8)[:8], b"BBKUBOOT")
        self.assertEqual(page_data(8)[: len(uboot_bytes)], uboot_bytes)
        self.assertEqual(page_spare(0)[2:5], b"\x00\x00\x00")
        self.assertEqual(page_spare(1)[2:5], b"\x00\x00\x00")
        self.assertEqual(page_spare(4)[2:5], b"\x00\x00\x00")
        self.assertEqual(page_spare(5)[2:5], b"\x00\x00\x00")
        self.assertEqual(page_spare(8)[2:5], b"\x00\x00\x00")
        self.assertEqual(page_spare(9), b"\xff" * spare_size)
        self.assertEqual(page_data(12)[: len(fat_bytes)], fat_bytes)
        for page in (0, 1, 4, 5, 8):
            expected_oob = jz4740_page_oob_ecc(page_data(page), offset=6)
            self.assertEqual(page_spare(page)[6 : len(expected_oob)], expected_oob[6:])
        expected_fat_oob = jz4740_page_oob_ecc(page_data(12), offset=4)
        self.assertEqual(page_spare(12)[4 : len(expected_fat_oob)], expected_fat_oob[4:])

    def test_make_combined_nand_can_write_legacy_uboot_header_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uboot = root / "u_boot_9588_4740.bin"
            fat = root / "fat.img"
            out = root / "nand.bin"
            uboot_bytes = b"UBOOT-PAYLOAD-0123456789"
            fat.write_bytes(b"FATDATA")
            uboot.write_bytes(uboot_bytes)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "tools" / "make_combined_nand.py"),
                    "--uboot-image",
                    str(uboot),
                    "--uboot-page-base",
                    "4",
                    "--legacy-uboot-header",
                    "--fat-image",
                    str(fat),
                    "--fat-page-base",
                    "8",
                    "--output",
                    str(out),
                    "--page-size",
                    "2048",
                    "--spare-size",
                    "64",
                    "--pages-per-block",
                    "4",
                    "--physical-blocks",
                    "3",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            data = out.read_bytes()

        stride = 2048 + 64
        header = data[4 * stride : 4 * stride + 2048]
        self.assertEqual(header[:8], b"BBKUBOOT")
        self.assertEqual(struct.unpack_from("<IIII", header, 8), (1, 0x900000, 0x80900000, len(uboot_bytes)))
        payload = data[5 * stride : 5 * stride + len(uboot_bytes)]
        self.assertEqual(payload, uboot_bytes)

    def test_stamp_nand_ecc_preserves_ftl_tags_and_skips_erased_pages(self) -> None:
        page_size = 2048
        spare_size = 64
        stride = page_size + spare_size
        image = bytearray(b"\xff" * (3 * stride))
        first_page = bytes((index * 17 + 3) & 0xFF for index in range(page_size))
        third_page = bytes((index * 29 + 11) & 0xFF for index in range(page_size))
        image[:page_size] = first_page
        image[page_size + 1 : page_size + 5] = b"\x00\x3f\x00\x7a"
        image[stride + page_size + 2 : stride + page_size + 5] = b"\x00\x00\x00"
        third_offset = 2 * stride
        image[third_offset : third_offset + page_size] = third_page
        image[third_offset + page_size + 58 : third_offset + stride] = b"FTLTAG"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            output = root / "stamped.bin"
            source.write_bytes(image)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "tools" / "stamp_nand_ecc.py"),
                    str(source),
                    str(output),
                    "--boot-ecc-end-page",
                    "2",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            stamped = output.read_bytes()

        first_oob = stamped[page_size:stride]
        erased_oob = stamped[stride + page_size : 2 * stride]
        third_oob = stamped[third_offset + page_size : third_offset + stride]
        self.assertEqual(first_oob[1:5], b"\x00\x3f\x00\x7a")
        self.assertEqual(first_oob[6:42], jz4740_page_oob_ecc(first_page)[6:])
        self.assertEqual(erased_oob[2:5], b"\x00\x00\x00")
        self.assertEqual(erased_oob[6:42], jz4740_page_oob_ecc(b"\xff" * page_size)[6:])
        self.assertEqual(third_oob[4:40], jz4740_page_oob_ecc(third_page, offset=4)[4:])
        self.assertEqual(third_oob[58:64], b"FTLTAG")

    def test_make_fat16_image_places_uboot_kernel_file_under_system_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            system = root / "系统"
            data_dir = system / "数据"
            apps = root / "应用"
            data_dir.mkdir(parents=True)
            apps.mkdir()
            kernel = data_dir / "kj409588.bin"
            kernel.write_bytes(b"KJ-KERNEL")
            out = root / "fat.img"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "tools" / "make_fat16_image.py"),
                    "--output",
                    str(out),
                    "--free-clusters",
                    "0",
                    "--volume-sectors",
                    "0",
                    str(system),
                    str(apps),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            image = out.read_bytes()

        boot_off = next(
            off
            for off in range(0, min(len(image), 0x20000), 512)
            if image[off + 54 : off + 62] == b"FAT16   " and image[off + 510 : off + 512] == b"\x55\xaa"
        )
        bytes_per_sector = struct.unpack_from("<H", image, boot_off + 11)[0]
        sectors_per_cluster = image[boot_off + 13]
        reserved = struct.unpack_from("<H", image, boot_off + 14)[0]
        fat_copies = image[boot_off + 16]
        root_entries = struct.unpack_from("<H", image, boot_off + 17)[0]
        sectors_per_fat = struct.unpack_from("<H", image, boot_off + 22)[0]
        root_dir_sectors = (root_entries * 32 + bytes_per_sector - 1) // bytes_per_sector
        self.assertEqual(root_entries, 512)
        self.assertEqual(root_dir_sectors, 32)
        root_off = boot_off + (reserved + fat_copies * sectors_per_fat) * bytes_per_sector
        first_data_off = root_off + root_dir_sectors * bytes_per_sector
        cluster_size = bytes_per_sector * sectors_per_cluster

        def cluster_off(cluster: int) -> int:
            return first_data_off + (cluster - 2) * cluster_size

        def short_dir_name(name: str) -> bytes:
            return name.encode("gbk").ljust(8, b" ") + b"   "

        def entries_at(off: int, size: int) -> dict[bytes, tuple[int, int, int]]:
            entries: dict[bytes, tuple[int, int, int]] = {}
            for entry_off in range(off, off + size, 32):
                entry = image[entry_off : entry_off + 32]
                if not entry or entry[0] == 0:
                    break
                if entry[0] == 0xE5 or entry[11] == 0x0F:
                    continue
                cluster = struct.unpack_from("<H", entry, 26)[0]
                size_bytes = struct.unpack_from("<I", entry, 28)[0]
                entries[bytes(entry[:11])] = (entry[11], cluster, size_bytes)
            return entries

        root_entries_by_short = entries_at(root_off, root_dir_sectors * bytes_per_sector)
        self.assertNotIn(b"KJ409588BIN", root_entries_by_short)
        system_attr, system_cluster, _ = root_entries_by_short[short_dir_name("系统")]
        self.assertEqual(system_attr & 0x10, 0x10)

        system_entries = entries_at(cluster_off(system_cluster), cluster_size)
        data_attr, data_cluster, _ = system_entries[short_dir_name("数据")]
        self.assertEqual(data_attr & 0x10, 0x10)

        data_entries = entries_at(cluster_off(data_cluster), cluster_size)
        kernel_attr, _, kernel_size = data_entries[b"KJ409588BIN"]
        self.assertEqual(kernel_attr & 0x20, 0x20)
        self.assertEqual(kernel_size, len(b"KJ-KERNEL"))

    def test_probe_dry_run_writes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 4)
            out_dir = root / "out"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.run_qemu_system_probe",
                    "--image",
                    str(image),
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "qemu_probe_test",
                    "--qemu-firmware-patch",
                    "none",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["dry_run"])
            self.assertIn("-M", summary["command"])
            machine_arg = summary["command"][summary["command"].index("-M") + 1]
            self.assertTrue(machine_arg.startswith("bbk9588"), machine_arg)
            self.assertNotIn("touch-autocal=on", machine_arg)
            self.assertIn("-kernel", summary["command"])
            self.assertIn(str(image.resolve()), summary["command"])

    def test_probe_input_event_queue_dry_run_keeps_command_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 4)
            out_dir = root / "out"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.run_qemu_system_probe",
                    "--image",
                    str(image),
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "qemu_probe_input_queue_test",
                    "--input-event-queue-probe",
                    "--qemu-firmware-patch",
                    "none",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["dry_run"])
            self.assertNotIn("input_event_queue_probe", summary)
            self.assertIn("-M", summary["command"])
            machine_arg = summary["command"][summary["command"].index("-M") + 1]
            self.assertTrue(machine_arg.startswith("bbk9588"), machine_arg)
            self.assertNotIn("touch-autocal=on", machine_arg)

    def test_probe_msc_dma_write_dry_run_keeps_command_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 4)
            out_dir = root / "out"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.run_qemu_system_probe",
                    "--image",
                    str(image),
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "qemu_probe_msc_dma_write_test",
                    "--msc-dma-write-probe",
                    "--msc-dma-write-lba",
                    "0x40",
                    "--qemu-machine-option",
                    "storage-trace=on",
                    "--qemu-firmware-patch",
                    "none",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["dry_run"])
            self.assertNotIn("msc_dma_write_probe", summary)
            self.assertIn("storage-trace=on", summary["qemu_machine_options"])

    def test_msc_dma_write_probe_code_returns_to_ra(self) -> None:
        from tests import run_qemu_system_probe as probe

        code = probe._build_msc_dma_write_probe_code(0x40)

        self.assertGreater(len(code), 32)
        self.assertEqual(code[-8:], struct.pack("<II", 0x03E00008, 0))

    def test_probe_uart_register_dry_run_keeps_command_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 4)
            out_dir = root / "out"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.run_qemu_system_probe",
                    "--image",
                    str(image),
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "qemu_probe_uart_register_test",
                    "--uart-register-probe",
                    "--qemu-firmware-patch",
                    "none",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["dry_run"])
            self.assertNotIn("uart_register_probe", summary)

    def test_uart_register_probe_code_returns_to_ra(self) -> None:
        from tests import run_qemu_system_probe as probe

        code = probe._build_uart_register_probe_code()

        self.assertGreater(len(code), 32)
        self.assertEqual(code[-8:], struct.pack("<II", 0x03E00008, 0))

    def test_sadc_battery_probe_code_returns_to_ra(self) -> None:
        from tests import run_qemu_system_probe as probe

        code = probe._build_sadc_battery_probe_code()

        self.assertGreater(len(code), 32)
        self.assertEqual(code[-8:], struct.pack("<II", 0x03E00008, 0))

    def test_rtc_hibernate_probe_code_returns_to_ra(self) -> None:
        from tests import run_qemu_system_probe as probe

        code = probe._build_rtc_hibernate_probe_code()

        self.assertGreater(len(code), 32)
        self.assertEqual(code[-8:], struct.pack("<II", 0x03E00008, 0))

    def test_rtc_alarm_irq_probe_code_returns_to_ra(self) -> None:
        from tests import run_qemu_system_probe as probe

        code = probe._build_rtc_alarm_irq_probe_code()

        self.assertGreater(len(code), 32)
        self.assertEqual(code[-8:], struct.pack("<II", 0x03E00008, 0))

    def test_probe_lcd_frame_done_dry_run_keeps_command_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 4)
            out_dir = root / "out"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.run_qemu_system_probe",
                    "--image",
                    str(image),
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "qemu_probe_lcd_frame_done_test",
                    "--lcd-frame-done-probe",
                    "--qemu-firmware-patch",
                    "none",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["dry_run"])
            self.assertNotIn("lcd_frame_done_probe", summary)
            self.assertIn("-M", summary["command"])

    def test_lcd_frame_done_probe_code_returns_to_ra(self) -> None:
        from tests import run_qemu_system_probe as probe

        ack_code = probe._build_lcd_status_ack_probe_code()
        read_code = probe._build_lcd_status_read_probe_code()

        self.assertGreater(len(ack_code), 24)
        self.assertGreater(len(read_code), 16)
        self.assertEqual(ack_code[-8:], struct.pack("<II", 0x03E00008, 0))
        self.assertEqual(read_code[-8:], struct.pack("<II", 0x03E00008, 0))

    def test_probe_touch_move_sadc_dry_run_keeps_command_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 4)
            out_dir = root / "out"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.run_qemu_system_probe",
                    "--image",
                    str(image),
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "qemu_probe_touch_move_sadc_test",
                    "--touch-move-sadc-probe",
                    "--qemu-firmware-patch",
                    "none",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["dry_run"])
            self.assertNotIn("touch_move_sadc_probe", summary)
            self.assertIn("-M", summary["command"])

    def test_probe_key_gpio_dry_run_keeps_command_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 4)
            out_dir = root / "out"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.run_qemu_system_probe",
                    "--image",
                    str(image),
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "qemu_probe_key_gpio_test",
                    "--key-gpio-probe",
                    "--qemu-firmware-patch",
                    "none",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["dry_run"])
            self.assertNotIn("key_gpio_probe", summary)
            self.assertIn("-M", summary["command"])

    def test_key_gpio_probe_code_returns_to_ra(self) -> None:
        from tests import run_qemu_system_probe as probe

        code = probe._build_key_gpio_ack_probe_code()

        self.assertGreater(len(code), 40)
        self.assertEqual(code[-8:], struct.pack("<II", 0x03E00008, 0))

    def test_probe_semaphore_flow_dry_run_keeps_command_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 4)
            out_dir = root / "out"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.run_qemu_system_probe",
                    "--image",
                    str(image),
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "qemu_probe_semaphore_flow_test",
                    "--semaphore-flow-probe",
                    "--qemu-machine-option",
                    "progress-trace=on",
                    "--qemu-firmware-patch",
                    "none",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["dry_run"])
            self.assertNotIn("semaphore_flow_probe", summary)
            self.assertIn("progress-trace=on", summary["qemu_machine_options"])
            self.assertIn("-M", summary["command"])
            self.assertTrue(
                any(str(part).startswith("bbk9588") for part in summary["command"])
            )

    def test_probe_alarm_ui_dry_run_keeps_command_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 4)
            out_dir = root / "out"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.run_qemu_system_probe",
                    "--image",
                    str(image),
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "qemu_probe_alarm_ui_test",
                    "--alarm-ui-probe",
                    "--qemu-machine-option",
                    "progress-trace=on",
                    "--qemu-firmware-patch",
                    "none",
                    "--dry-run",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["dry_run"])
            self.assertNotIn("alarm_ui_probe", summary)
            self.assertIn("-M", summary["command"])
            self.assertTrue(
                any(
                    str(part).startswith("bbk9588")
                    and "progress-trace=on" in str(part)
                    for part in summary["command"]
                ),
                summary["command"],
            )

    def test_probe_resource_path_has_no_guest_exception(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.run_qemu_system_probe",
                    "--timeout",
                    "5",
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "qemu_guest_exception_guard_test",
                    "--qemu-machine-option",
                    "progress-trace=on",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(completed.returncode, 0, completed.stdout)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"], summary)
            self.assertNotIn("guest_exceptions", summary)

    def test_default_c200_config_uses_bbk9588_machine_without_patches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 0x200000)

            config = build_bbk_qemu_config(boot_mode="c200", image=image)

            self.assertEqual(DEFAULT_QEMU_MACHINE, "bbk9588")
            self.assertEqual(config.machine, "bbk9588")
            self.assertEqual(config.firmware_patches, ())
            self.assertNotIn("tcu-period-ms=1", config.bbk_machine_options)
            self.assertNotIn("touch-autocal=on", config.bbk_machine_options)
            assert config.boot_payload is not None
            self.assertEqual(config.boot_payload.path.resolve(), image.resolve())

    def test_bbk9588_launcher_preserves_explicit_machine_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            image.write_bytes(b"\0" * 0x200000)

            config = build_bbk_qemu_config(
                boot_mode="c200",
                image=image,
                bbk_machine_options=("storage-trace=on", "progress-trace=on"),
            )

            self.assertIn("storage-trace=on", config.bbk_machine_options)
            self.assertIn("progress-trace=on", config.bbk_machine_options)
            self.assertNotIn("touch-autocal=on", config.bbk_machine_options)

    def test_malta_c200_config_uses_qemu_only_patch_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "C200.bin"
            original = bytearray(b"\0" * 0x129100)
            original[0xE314 : 0xE318] = bytes.fromhex("0c00028c")
            original[0xE338 : 0xE33C] = bytes.fromhex("0c00028c")
            original[0x1320 : 0x1324] = bytes.fromhex("044c428c")
            original[0x1328 : 0x132C] = bytes.fromhex("1010638c")
            image.write_bytes(original)

            config = build_bbk_qemu_config(boot_mode="c200", image=image, machine="malta")

            assert config.boot_payload is not None
            patched = config.boot_payload.path
            self.assertRegex(
                patched.name,
                r"^C200_[0-9a-f]{12}_patches_[0-9a-f]{12}\.bin$",
            )
            self.assertNotEqual(patched.resolve(), image.resolve())
            data = patched.read_bytes()
            self.assertEqual(data[0x1248 : 0x124C], bytes.fromhex("0800e003"))
            self.assertEqual(data[0x124C : 0x1250], b"\0\0\0\0")
            self.assertEqual(data[0xE314 : 0xE318], bytes.fromhex("80000234"))
            self.assertEqual(data[0xE338 : 0xE33C], bytes.fromhex("80000234"))
            self.assertEqual(data[0x1320 : 0x1324], bytes.fromhex("21100000"))
            self.assertEqual(data[0x1328 : 0x132C], bytes.fromhex("21180000"))
            self.assertEqual(data[0xA3FA4 : 0xA3FA8], b"\0\0\0\0")
            self.assertEqual(data[0xA40B4 : 0xA40B8], b"\0\0\0\0")
            self.assertEqual(data[0xA4134 : 0xA4138], b"\0\0\0\0")
            self.assertEqual(data[0x128CFC : 0x128D00], bytes.fromhex("21280000"))
            self.assertEqual(data[0xC05C : 0xC060], bytes.fromhex("00080234"))
            self.assertEqual(data[0xC060 : 0xC064], b"\0\0\0\0")
            self.assertEqual(data[0x13BA4 : 0x13BA8], bytes.fromhex("0008033c"))
            self.assertEqual(data[0x55F68 : 0x55F6C], bytes.fromhex("7f80023c"))
            self.assertEqual(data[0x55F6C : 0x55F70], bytes.fromhex("10714290"))
            self.assertEqual(data[0x55F70 : 0x55F74], bytes.fromhex("0800e003"))
            self.assertEqual(data[0x55F74 : 0x55F78], b"\0\0\0\0")
            self.assertEqual(data[0x1C9C : 0x1CA0], bytes.fromhex("20000234"))
            self.assertEqual(data[0x1CD8 : 0x1CDC], bytes.fromhex("40000234"))
            self.assertEqual(data[0x1CDC : 0x1CE0], b"\0\0\0\0")
            self.assertEqual(data[0x1D2C : 0x1D30], bytes.fromhex("20000234"))
            self.assertEqual(data[0x1D30 : 0x1D34], b"\0\0\0\0")
            self.assertEqual(data[0x57CD4 : 0x57CD8], b"\0\0\0\0")
            self.assertEqual(data[0x57DE8 : 0x57DEC], b"\0\0\0\0")
            self.assertEqual(data[0x03A0 : 0x03A4], bytes.fromhex("0800e003"))
            self.assertEqual(data[0x03A4 : 0x03A8], b"\0\0\0\0")
            self.assertEqual(data[0x54CB4 : 0x54CB8], bytes.fromhex("21100000"))
            self.assertEqual(data[0x54CB8 : 0x54CBC], bytes.fromhex("0800e003"))
            self.assertEqual(data[0x54CBC : 0x54CC0], b"\0\0\0\0")
            self.assertEqual(image.read_bytes(), bytes(original))

    def test_workspace_file_lookup_ignores_generated_build_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build" / "qemu_payloads").mkdir(parents=True)
            (root / "build" / "qemu_payloads" / "C200.bin").write_bytes(b"generated")
            (root / "system").mkdir()
            source = root / "system" / "C200.bin"
            source.write_bytes(b"source")
            old_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                self.assertEqual(find_workspace_file("C200.bin"), Path("system") / "C200.bin")
            finally:
                os.chdir(old_cwd)

    def test_qemu_storage_layout_prefers_combined_nand_backing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build = root / "build"
            build.mkdir()
            (build / "bbk9588_fs_fat16.img").write_bytes(_fat16_boot_sector(hidden=0, root_entries=0x400))
            nand = bytearray(b"\xFF" * ((0x20 // 4 + 1) * (2048 + 64)))
            page = 0x20 // 4
            page_off = page * (2048 + 64)
            nand[page_off : page_off + 512] = _fat16_boot_sector(hidden=0x20, root_entries=0x200)
            (build / "bbk9588_nand_c200_fat_page1c40_root512_ftloob.bin").write_bytes(nand)
            old_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                backend = QemuProcessBackend(QemuSystemConfig())
                layout = backend._fat16_layout_from_backing()
            finally:
                os.chdir(old_cwd)
            self.assertIsInstance(layout, dict)
            assert layout is not None
            self.assertEqual(layout["volume_lba"], 0x20)
            self.assertEqual(layout["root_dir_sectors"], 0x20)
            self.assertEqual(layout["root_lba"], 0x119)
            self.assertEqual(layout["first_data_lba"], 0x139)

    def test_nand_file_manager_round_trips_fat16_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fat_image, nand_image = _make_nand_file_manager_fixture(root)

            root_entries = list_nand_directory(nand_image)
            self.assertEqual([entry["name"] for entry in root_entries["entries"]], ["应用"])

            def install(fs: PyFatFS) -> None:
                fs.makedir("/应用/安装")
                fs.writebytes("/应用/安装/demo.bda", b"BDA-TEST")
                fs.move("/应用/安装/demo.bda", "/应用/安装/雷霆.bda")
                fs.movedir("/应用/安装", "/应用/游戏", create=True)

            before_mutation = nand_image.read_bytes()
            mutate_nand_files(nand_image, install)
            listing = list_nand_directory(nand_image, "/应用/游戏")
            self.assertEqual(listing["entries"][0]["name"], "雷霆.bda")
            self.assertEqual(read_nand_file(nand_image, "/应用/游戏/雷霆.bda")[1], b"BDA-TEST")
            raw = nand_image.read_bytes()
            changed_pages = []
            for page_offset in range(0, len(raw), PAGE_STRIDE):
                page_data = raw[page_offset : page_offset + PAGE_SIZE]
                if page_data == before_mutation[page_offset : page_offset + PAGE_SIZE]:
                    continue
                changed_pages.append(page_offset // PAGE_STRIDE)
                expected_ecc = jz4740_page_oob_ecc(page_data, offset=4)
                self.assertEqual(
                    raw[
                        page_offset + PAGE_SIZE + 4 :
                        page_offset + PAGE_SIZE + len(expected_ecc)
                    ],
                    expected_ecc[4:],
                )
            self.assertTrue(changed_pages)

            mutate_nand_files(nand_image, lambda writable: writable.removetree("/应用/游戏"))
            self.assertEqual(list_nand_directory(nand_image, "/应用")["entries"], [])
            self.assertEqual(normalize_nand_path("A:\\应用\\游戏"), "/应用/游戏")
            with self.assertRaises(ValueError):
                normalize_nand_path("/应用/../系统")

    def test_nand_file_manager_safely_replaces_multicluster_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _fat_image, nand_image = _make_nand_file_manager_fixture(Path(tmp))
            original = bytes((index * 17 + 3) & 0xff for index in range(256 * 1024))
            neighbor = bytes((index * 29 + 7) & 0xff for index in range(256 * 1024))
            replacement = bytes((index * 11 + 5) & 0xff for index in range(80 * 1024))

            def install(fs: PyFatFS) -> None:
                fs.writebytes("/应用/replace.bda", original)
                fs.writebytes("/应用/keep.bin", neighbor)

            mutate_nand_files(nand_image, install)

            def replace(fs: PyFatFS) -> None:
                replace_fat_file(fs, "/应用/replace.bda", io.BytesIO(replacement))

            mutate_nand_files(nand_image, replace)
            self.assertEqual(
                read_nand_file(nand_image, "/应用/replace.bda")[1],
                replacement,
            )
            self.assertEqual(
                read_nand_file(nand_image, "/应用/keep.bin")[1],
                neighbor,
            )

    def test_nand_validation_rejects_mapped_block_ecc_damage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _fat_image, nand_image = _make_nand_file_manager_fixture(Path(tmp))
            validation = validate_nand_image(nand_image)
            self.assertGreater(validation["ecc_pages_checked"], 0)

            first_mapped_page = PAGES_PER_BLOCK
            with nand_image.open("r+b") as stream:
                stream.seek(first_mapped_page * PAGE_STRIDE + PAGE_SIZE + 4)
                parity_byte = stream.read(1)
                stream.seek(first_mapped_page * PAGE_STRIDE + PAGE_SIZE + 4)
                stream.write(bytes([parity_byte[0] ^ 1]))

            with self.assertRaisesRegex(
                ValueError,
                r"invalid RS ECC at physical page 0x40.*stamp_nand_ecc\.py",
            ):
                validate_nand_image(nand_image)

    def test_nand_candidate_validation_failure_preserves_original_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fat_image, nand_image = _make_nand_file_manager_fixture(root)
            before = hashlib.sha256(nand_image.read_bytes()).hexdigest()

            def install(fs: PyFatFS) -> None:
                fs.writebytes("/应用/demo.bda", b"BDA-TEST")

            def reject_candidate(_fs: PyFatFS) -> None:
                raise ValueError("target validation failed")

            with self.assertRaisesRegex(ValueError, "target validation failed"):
                mutate_nand_files(
                    nand_image,
                    install,
                    validator=reject_candidate,
                )

            after = hashlib.sha256(nand_image.read_bytes()).hexdigest()
            self.assertEqual(after, before)
            self.assertEqual(list_nand_directory(nand_image, "/应用")["entries"], [])
            self.assertEqual(list(root.glob(".*.files.tmp")), [])

    def test_nand_source_validation_failure_preserves_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fat_image, valid = _make_nand_file_manager_fixture(root)
            destination = root / "runtime" / "bbk9588_nand.bin"
            destination.parent.mkdir()
            destination.write_bytes(valid.read_bytes())
            imported = import_nand_source(valid, destination)
            before = hashlib.sha256(destination.read_bytes()).hexdigest()
            self.assertEqual(imported["sha256"], before)
            self.assertEqual(list_nand_directory(destination)["entries"][0]["name"], "应用")
            invalid = root / "invalid.bin"
            invalid.write_bytes(b"not a raw NAND image")

            with self.assertRaisesRegex(ValueError, "unsupported NAND geometry"):
                import_nand_source(invalid, destination)

            self.assertEqual(
                hashlib.sha256(destination.read_bytes()).hexdigest(),
                before,
            )
            self.assertEqual(list(destination.parent.glob(".*.importing")), [])

    def test_stream_upload_rejects_short_and_oversized_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload = root / "upload.bin"
            payload = b"BDA" * 4096

            digest = stream_upload_to_path(io.BytesIO(payload), upload, len(payload))

            self.assertEqual(upload.read_bytes(), payload)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())

            short = root / "short.bin"
            with self.assertRaisesRegex(EOFError, "short NAND upload"):
                stream_upload_to_path(io.BytesIO(payload), short, len(payload) + 1)
            self.assertFalse(short.exists())

            oversized = root / "oversized.bin"
            with self.assertRaisesRegex(ValueError, "invalid NAND upload size"):
                stream_upload_to_path(
                    io.BytesIO(payload),
                    oversized,
                    len(payload),
                    max_length=len(payload) - 1,
                )
            self.assertFalse(oversized.exists())

    def test_nand_image_lease_rejects_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "bbk9588_nand.bin"
            ready = root / "locked"
            image.write_bytes(b"\xff")
            script = "\n".join(
                (
                    "import sys, time",
                    "from pathlib import Path",
                    "from emu.qemu.nand_lock import NandImageLease",
                    "lease = NandImageLease()",
                    "lease.acquire(Path(sys.argv[1]))",
                    "Path(sys.argv[2]).write_text('locked', encoding='ascii')",
                    "time.sleep(30)",
                )
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(image), str(ready)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            lease = NandImageLease()
            try:
                deadline = time.monotonic() + 10.0
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not ready.exists():
                    self.fail(
                        "NAND lock holder did not start "
                        f"(returncode={process.poll()})"
                    )

                with self.assertRaisesRegex(NandImageInUseError, "already in use"):
                    lease.acquire(image)
            finally:
                process.terminate()
                process.wait(timeout=5)

            lease.acquire(image)
            self.assertEqual(lease.path, image.resolve())
            lease.release()

    def test_nand_lifecycle_lock_serializes_mutation_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "bbk9588_nand.bin"
            image.write_bytes(b"\xff")
            state = FrontendState.__new__(FrontendState)
            state.nand_lifecycle_lock = threading.RLock()
            state.nand_files_lock = state.nand_lifecycle_lock
            state.nand_image_lease = NandImageLease()
            state.qemu_backend = None
            state._nand_files_image = lambda: image  # type: ignore[method-assign]
            state._invalidate_nand_files_cache = lambda: None  # type: ignore[method-assign]
            state.snapshot = lambda: {}  # type: ignore[method-assign]

            mutation_entered = threading.Event()
            allow_mutation = threading.Event()
            reset_entered = threading.Event()
            failures: list[BaseException] = []

            def fake_mutation(_image, _operation, *, validator=None) -> None:
                mutation_entered.set()
                if not allow_mutation.wait(timeout=5):
                    raise TimeoutError("mutation test timed out")

            def fake_reset() -> dict[str, object]:
                reset_entered.set()
                return {}

            state._reset_nand_locked = fake_reset  # type: ignore[method-assign]

            def run_mutation() -> None:
                try:
                    state._mutate_nand_files(lambda _fs: None)
                except BaseException as exc:
                    failures.append(exc)

            def run_reset() -> None:
                try:
                    state.reset()
                except BaseException as exc:
                    failures.append(exc)

            with mock.patch("emu.web.frontend_state.mutate_nand_files", fake_mutation):
                mutation_thread = threading.Thread(target=run_mutation)
                reset_thread = threading.Thread(target=run_reset)
                mutation_thread.start()
                self.assertTrue(mutation_entered.wait(timeout=2))
                reset_thread.start()
                self.assertFalse(reset_entered.wait(timeout=0.2))
                allow_mutation.set()
                mutation_thread.join(timeout=5)
                reset_thread.join(timeout=5)

            state.nand_image_lease.release()
            self.assertFalse(mutation_thread.is_alive())
            self.assertFalse(reset_thread.is_alive())
            self.assertTrue(reset_entered.is_set())
            self.assertEqual(failures, [])

    def test_backend_status_exposes_direct_active_nand(self) -> None:
        image = Path("runtime") / "bbk9588_nand.bin"
        backend = QemuProcessBackend(QemuSystemConfig(nand_image=image))

        snapshot = backend.snapshot()

        self.assertEqual(snapshot["nand_write_mode"], "direct")
        self.assertEqual(snapshot["nand_image"], str(image.resolve()))
        self.assertNotIn("nand_runtime_image", snapshot)
        self.assertNotIn("nand_checkpoint_image", snapshot)
        self.assertNotIn("nand_writes_persistent", snapshot)

    def test_legacy_checkpoint_migrates_once_into_active_nand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "bbk9588_nand.bin"
            active.write_bytes(b"\xff" * RAW_BLOCK_SIZE)
            digest = hashlib.sha1(
                os.path.normcase(str(active.resolve())).encode("utf-8")
            ).hexdigest()[:16]
            checkpoint = root / "runtime" / "qemu_nand_persistent" / f"nand_{digest}.bin"
            checkpoint.parent.mkdir(parents=True)
            latest = bytearray(b"\xff" * RAW_BLOCK_SIZE)
            latest[0] = 0xA5
            checkpoint.write_bytes(latest)
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                migrated = migrate_legacy_nand_checkpoint(active)
                repeated = migrate_legacy_nand_checkpoint(active)
            finally:
                os.chdir(old_cwd)

            self.assertEqual(migrated, checkpoint.resolve())
            self.assertIsNone(repeated)
            self.assertEqual(active.read_bytes()[0], 0xA5)
            self.assertFalse(checkpoint.exists())

    def test_frontend_file_manager_targets_selected_active_nand(self) -> None:
        state = FrontendState.__new__(FrontendState)
        selected = Path("runtime") / "bbk9588_nand.bin"
        state.args = argparse.Namespace(nand_image=selected)

        self.assertEqual(state._nand_files_image(), selected.resolve())

    def test_frontend_storage_trace_command_routes_to_qemu(self) -> None:
        state = FrontendState.__new__(FrontendState)
        backend = mock.Mock()
        backend.set_storage_trace.return_value = {
            "storage_trace_enabled": True,
        }
        state.qemu_backend = backend

        result = state.command({"op": "qemu-storage-trace", "enabled": True})

        backend.set_storage_trace.assert_called_once_with(True)
        self.assertTrue(result.get("storage_trace_enabled"), result)

    def test_qemu_legacy_python_storage_hook_seed_is_disabled(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        backend._write_u32_paused_locked = lambda va, value: self.fail("unexpected GDB u32 seed write")  # type: ignore[method-assign]
        backend._write_u16_paused_locked = lambda va, value: self.fail("unexpected GDB u16 seed write")  # type: ignore[method-assign]
        backend._write_u8_paused_locked = lambda va, value: self.fail("unexpected GDB u8 seed write")  # type: ignore[method-assign]

        row = backend._seed_legacy_python_storage_hook_globals_paused_locked()

        self.assertFalse(row.get("seeded"), row)
        self.assertTrue(row.get("disabled"), row)
        self.assertEqual(row.get("event"), "qemu-legacy-python-storage-hook-seed")
        self.assertIn("removed from the hardware-model path", str(row.get("reason")))

    def test_qemu_bbk9588_legacy_python_storage_hook_seed_does_not_write_guest_memory(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend._write_u32_paused_locked = lambda va, value: self.fail("unexpected GDB u32 seed write")  # type: ignore[method-assign]
        backend._write_u16_paused_locked = lambda va, value: self.fail("unexpected GDB u16 seed write")  # type: ignore[method-assign]
        backend._write_u8_paused_locked = lambda va, value: self.fail("unexpected GDB u8 seed write")  # type: ignore[method-assign]

        row = backend._seed_legacy_python_storage_hook_globals_paused_locked()

        self.assertFalse(row.get("seeded"), row)
        self.assertTrue(row.get("disabled"), row)

    def test_qemu_legacy_python_resource_hook_rounds_skip_when_bbk9588_c_machine_ready(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend._read_u32_paused_locked = lambda va: 0 if va == 0x804BF440 else 0  # type: ignore[method-assign]
        backend._read_u8_paused_locked = lambda va: 1 if va == 0x804BF444 else 0  # type: ignore[method-assign]
        backend._read_pc_paused_locked = lambda: 0x8005BC70  # type: ignore[method-assign]
        backend._service_legacy_python_resource_hook_paused_locked = lambda **kwargs: self.fail("unexpected legacy Python resource hook")  # type: ignore[method-assign]

        row = backend._service_legacy_python_resource_hook_rounds_paused_locked(rounds=3)

        self.assertTrue(row.get("skipped"), row)
        self.assertEqual(row.get("source"), "qemu-c-machine")
        self.assertEqual(row.get("reason"), "qemu-c-resource-refresh-ready")
        self.assertEqual(row.get("handled_count"), 0)
        refresh = row.get("resource_refresh")
        self.assertIsInstance(refresh, dict)
        assert isinstance(refresh, dict)
        self.assertTrue(refresh.get("ready"), refresh)

    def test_qemu_legacy_python_resource_hook_rounds_skip_bbk9588_without_priming_refresh(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend._read_u32_paused_locked = lambda va: 0  # type: ignore[method-assign]
        backend._read_u8_paused_locked = lambda va: 0  # type: ignore[method-assign]
        backend._read_pc_paused_locked = lambda: 0x8005BC70  # type: ignore[method-assign]
        backend._prime_resource_refresh_paused_locked = lambda: self.fail("unexpected Python refresh prime")  # type: ignore[method-assign]
        backend._service_legacy_python_resource_hook_paused_locked = lambda **kwargs: self.fail("unexpected legacy Python resource hook")  # type: ignore[method-assign]

        row = backend._service_legacy_python_resource_hook_rounds_paused_locked(rounds=3)

        self.assertTrue(row.get("skipped"), row)
        self.assertTrue(row.get("disabled"), row)
        self.assertEqual(row.get("source"), "qemu-c-machine")
        self.assertEqual(row.get("reason"), "bbk9588-c-machine-default-path")
        self.assertEqual(row.get("handled_count"), 0)

    def test_qemu_snapshot_uses_legacy_python_hook_status_names(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.refresh = lambda: None  # type: ignore[method-assign]
        backend.legacy_python_storage_hook_count = 2
        backend.legacy_python_storage_hook_events = [{"event": "qemu-legacy-python-storage-hook"}]

        row = backend.snapshot()

        self.assertEqual(row.get("legacy_python_storage_hook_count"), 2)
        self.assertEqual(row.get("legacy_python_storage_hook_events"), [{"event": "qemu-legacy-python-storage-hook"}])
        self.assertNotIn("storage_" + "fastpath_count", row)
        self.assertNotIn("storage_" + "fastpath_events", row)

    def test_qemu_storage_trace_runtime_toggle_uses_machine_property(self) -> None:
        class _RunningProc:
            def poll(self) -> None:
                return None

        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.proc = _RunningProc()  # type: ignore[assignment]
        backend.hmp_sock = object()  # type: ignore[assignment]

        with mock.patch("emu.qemu.system._hmp_command", return_value="") as hmp:
            enabled = backend.set_storage_trace(True)
            disabled = backend.set_storage_trace(False)

        self.assertTrue(enabled.get("storage_trace_enabled"), enabled)
        self.assertFalse(disabled.get("storage_trace_enabled"), disabled)
        self.assertEqual(
            [call.args[1] for call in hmp.call_args_list],
            [
                "qom-set /machine storage-trace true",
                "qom-set /machine storage-trace false",
            ],
        )

    def test_qemu_usb_power_runtime_toggle_uses_machine_property(self) -> None:
        class _RunningProc:
            def poll(self) -> None:
                return None

        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.proc = _RunningProc()  # type: ignore[assignment]
        backend.hmp_sock = object()  # type: ignore[assignment]

        with mock.patch("emu.qemu.system._hmp_command", return_value="") as hmp:
            disconnected = backend.apply_usb_power_state(False)
            connected = backend.apply_usb_power_state(True)

        self.assertTrue(disconnected.get("applied"), disconnected)
        self.assertFalse(disconnected.get("connected"), disconnected)
        self.assertTrue(connected.get("connected"), connected)
        self.assertTrue(backend.usb_power_connected)
        self.assertEqual(
            [call.args[1] for call in hmp.call_args_list],
            [
                "qom-set /machine usb-power-connected false",
                "qom-set /machine usb-power-connected true",
            ],
        )

    def test_qemu_performance_metrics_compute_rates(self) -> None:
        class _RunningProc:
            pid = 12345

            def poll(self) -> None:
                return None

        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.proc = _RunningProc()  # type: ignore[assignment]
        backend.started_at = 90.0
        backend.frame_chardev_count = 10

        with mock.patch("emu.qemu.system._process_cpu_time_seconds", return_value=1.0):
            backend._update_performance_metrics_locked(100.0, 10.0)
        backend.frame_chardev_count = 16
        with mock.patch("emu.qemu.system._process_cpu_time_seconds", return_value=1.6):
            row = backend._update_performance_metrics_locked(101.0, 11.0)

        self.assertEqual(row.get("frame_chardev_fps"), 6.0)
        self.assertEqual(row.get("qemu_cpu_one_core_percent"), 60.0)
        self.assertFalse(row.get("guest_ips_available"), row)
        self.assertIsNone(row.get("guest_ips"))

    def test_qemu_performance_metrics_compute_guest_ips_from_perf_packets(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))

        backend._record_guest_insn_count_locked(1_000, 1000, 10.0)
        backend._record_guest_insn_count_locked(3_500, 2000, 11.0)
        row = backend._update_performance_metrics_locked(11.5, 1.5)

        self.assertTrue(row.get("guest_ips_available"), row)
        self.assertEqual(row.get("guest_ips"), 2500.0)
        self.assertEqual(row.get("guest_ips_source"), "bbk9588-frame-chardev")
        self.assertEqual(row.get("guest_insn_count"), 3500)
        self.assertEqual(row.get("guest_insn_packet_count"), 2)

    def test_qemu_performance_metrics_include_aic_diagnostics(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        values = (
            44100,
            12,
            2,
            0x19,
            0x00007821,
            0x00804802,
            0x031B2302,
            0x00170703,
            4096,
            128,
            2048,
            64,
            3,
            1,
            10,
            9,
            500_000,
            900_000,
            4_500_000,
            2,
            12,
            1024,
            31,
            0,
        )

        backend._record_audio_metrics_locked(values, 12.5)
        row = backend._update_performance_metrics_locked(12.6, 1.0)
        audio = row.get("audio")

        self.assertIsInstance(audio, dict)
        self.assertEqual(audio.get("sample_rate_hz"), 44100)
        self.assertEqual(audio.get("tx_fifo_level"), 12)
        self.assertTrue(audio.get("playing"))
        self.assertTrue(audio.get("timer_running"))
        self.assertTrue(audio.get("output_voice"))
        self.assertEqual(audio.get("tx_dma_samples"), 4096)
        self.assertEqual(audio.get("underruns"), 3)
        self.assertEqual(audio.get("dma_completion_count"), 10)
        self.assertEqual(audio.get("dma_last_rearm_gap_ns"), 500_000)
        self.assertEqual(audio.get("dma_last_units"), 1024)
        self.assertEqual(row.get("audio_packet_count"), 1)

    def test_qemu_dmac_trace_snapshot_decodes_audio_channel_state(self) -> None:
        class RunningProcess:
            def poll(self) -> None:
                return None

        words = (
            0x444D4B42,
            123,
            6,
            3,
            0x60,
            0x20,
            0x80012340,
            0x00100000,
            0xFFEFFFFF,
            0x8,
            0,
            0,
            0,
            24,
            0x00001641,
            17,
        )
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.proc = RunningProcess()  # type: ignore[assignment]

        with mock.patch.object(
            backend,
            "_read_guest_ram_snapshot_locked",
            return_value=struct.pack("<16I", *words),
        ):
            trace = backend.dmac_trace_snapshot()

        self.assertTrue(trace.get("available"))
        self.assertEqual(trace.get("event_name"), "audio-partial")
        self.assertEqual(trace.get("channel"), 3)
        channel3 = trace.get("channel3")
        self.assertIsInstance(channel3, dict)
        self.assertEqual(channel3.get("status"), "0x00000018")
        self.assertEqual(channel3.get("config"), "0x00001641")
        self.assertEqual(channel3.get("count"), 17)

    def test_qemu_frame_reader_accepts_aic_performance_packet(self) -> None:
        values = (
            44100, 8, 0, 0x19, 1, 2, 3, 4, 1024, 0, 512, 0, 2, 0,
            10, 9, 500_000, 900_000, 4_500_000, 2, 12, 1024, 31, 0,
        )
        payload = QEMU_BBK_AIC_PERF_PAYLOAD.pack(*values)
        packet = QEMU_BBK_FRAME_HEADER.pack(
            QEMU_BBK_PERF_MAGIC,
            23,
            1,
            0,
            0,
            QEMU_BBK_PERF_FORMAT_AIC,
            len(payload),
        ) + payload

        class FrameSocket:
            def recv(self, size: int) -> bytes:
                nonlocal packet
                chunk, packet = packet[:size], packet[size:]
                return chunk

        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.bbk_frame_sock = FrameSocket()  # type: ignore[assignment]
        backend._frame_reader()

        self.assertEqual(backend.audio_packet_count, 1)
        self.assertEqual(backend.audio_metrics.get("sample_rate_hz"), 44100)
        self.assertEqual(backend.audio_metrics.get("tx_dma_samples"), 1024)

    def test_qemu_frame_reader_forwards_pcm_audio_packet(self) -> None:
        payload = struct.pack("<8h", 0, 1000, -1000, 2000, -2000, 3000, -3000, 0)
        packet = QEMU_BBK_FRAME_HEADER.pack(
            QEMU_BBK_AUDIO_MAGIC,
            29,
            44100,
            2,
            4,
            QEMU_BBK_AUDIO_FORMAT_S16LE,
            len(payload),
        ) + payload

        class FrameSocket:
            def recv(self, size: int) -> bytes:
                nonlocal packet
                chunk, packet = packet[:size], packet[size:]
                return chunk

        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.bbk_frame_sock = FrameSocket()  # type: ignore[assignment]
        received: list[tuple[int, int, int, bytes]] = []
        backend.set_audio_ready_callback(
            lambda seq, rate, channels, pcm: received.append(
                (seq, rate, channels, pcm)
            )
        )

        backend._frame_reader()

        self.assertEqual(backend.audio_stream_packet_count, 1)
        self.assertEqual(backend.audio_stream_bytes, len(payload))
        self.assertEqual(received, [(29, 44100, 2, payload)])
        self.assertEqual(
            backend.latest_audio_chardev,
            (29, mock.ANY, 44100, 2, payload),
        )

    def test_qemu_frame_reader_notifies_frontend_immediately(self) -> None:
        payload = b"\x00\x00" * (240 * 320)
        packet = QEMU_BBK_FRAME_HEADER.pack(
            QEMU_BBK_FRAME_MAGIC,
            17,
            240,
            320,
            480,
            QEMU_BBK_FRAME_FORMAT_RGB565,
            len(payload),
        ) + payload

        class FrameSocket:
            def recv(self, size: int) -> bytes:
                nonlocal packet
                chunk, packet = packet[:size], packet[size:]
                return chunk

        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.bbk_frame_sock = FrameSocket()  # type: ignore[assignment]
        notifications: list[float] = []
        backend.set_frame_ready_callback(lambda: notifications.append(time.time()))

        backend._frame_reader()

        self.assertEqual(backend.frame_chardev_count, 1)
        self.assertEqual(backend.latest_frame_chardev[0] if backend.latest_frame_chardev else None, 17)
        self.assertEqual(len(notifications), 1)

    def test_bbk9588_pen_up_preserves_unread_touch_sample(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "qemu/overlay/hw/input/jz4740_sadc.c").read_text(
            encoding="utf-8"
        )
        touch_set_state = source.split("void jz4740_sadc_set_touch(", 1)[1].split(
            "bool jz4740_sadc_touch_down", 1
        )[0]

        self.assertNotIn("sadc_touch_fifo_clear(s);", touch_set_state)
        self.assertIn("s->status_event & ~SADC_STATE_PEND", touch_set_state)
        self.assertNotIn("SADC_STATE_PEND | SADC_STATE_DTCH", touch_set_state)
        self.assertIn("s->conversion_events_remaining = 5;", touch_set_state)
        self.assertIn("bool touch_move_pending;", source)
        self.assertIn("was_down = s->touch_down;", touch_set_state)
        self.assertIn("} else if (position_changed) {", touch_set_state)
        self.assertIn("s->touch_move_pending = true;", touch_set_state)
        self.assertIn("if (was_down != down) {", touch_set_state)
        initial_down = touch_set_state.split("if (!was_down) {", 1)[1].split(
            "} else if (position_changed) {", 1
        )[0]
        move = touch_set_state.split("} else if (position_changed) {", 1)[1].split(
            "    } else if (was_down) {", 1
        )[0]
        self.assertIn("s->conversion_events_remaining = 5;", initial_down)
        self.assertIn("SADC_STATE_PEND", initial_down)
        self.assertNotIn("s->conversion_events_remaining = 5;", move)
        self.assertNotIn("SADC_STATE_PEND", move)
        self.assertIn("sadc_queue_next_touch_sample(s);", move)

        queue_next = source.split(
            "static bool sadc_queue_next_touch_sample(", 1
        )[1].split("static void sadc_complete_cpu_samples", 1)[0]
        self.assertIn("s->status_event & SADC_STATE_DTCH", queue_next)
        self.assertIn("s->touch_fifo_count != 0", queue_next)
        self.assertIn("s->pending_enable & SADC_ADENA_TCHEN", queue_next)
        self.assertIn("sadc_touch_delay_ms(s, true)", queue_next)
        self.assertIn("sadc_touch_delay_ms(s, false)", queue_next)

        read_source = source.split("static uint64_t sadc_read(", 1)[1].split(
            "static void sadc_write(", 1
        )[0]
        adtch_read = read_source.split("case SADC_ADTCH:", 1)[1].split(
            "case SADC_ADBDAT:", 1
        )[0]
        self.assertIn("sadc_touch_fifo_pop(s)", adtch_read)
        self.assertNotIn("status_event & SADC_STATE_DTCH", adtch_read)

        write_source = source.split("static void sadc_write(", 1)[1].split(
            "static const MemoryRegionOps", 1
        )[0]
        adtch_write = write_source.split("case SADC_ADTCH:", 1)[1].split(
            "case SADC_ADBDAT:", 1
        )[0]
        self.assertIn("sadc_touch_fifo_clear(s);", adtch_write)
        self.assertNotIn("status_event", adtch_write)

    def test_frontend_coalesces_touch_moves_to_animation_frames(self) -> None:
        root = Path(__file__).resolve().parents[1]
        frontend = (root / "emu/web/frontend.py").read_text(encoding="utf-8")

        self.assertIn("function queueTouchMove(clientX, clientY, source = 'pointer')", frontend)
        self.assertIn("const minTouchMoveIntervalMs = 1000 / 30;", frontend)
        self.assertIn("const touchMoveBackpressureMs = 1000 / 30;", frontend)
        self.assertIn("let touchMoveAwaitingFrame = false;", frontend)
        self.assertIn("function schedulePendingTouchMove()", frontend)
        self.assertIn("const rateDelay = minTouchMoveIntervalMs - elapsed;", frontend)
        self.assertIn("const frameDelay = touchMoveAwaitingFrame ? touchMoveBackpressureMs - elapsed : 0;", frontend)
        self.assertIn("pendingTouchMoveTimer = setTimeout(() => {", frontend)
        self.assertIn("pendingTouchMoveFrame = requestAnimationFrame(() => {", frontend)
        self.assertIn("function flushPendingTouchMove()", frontend)
        self.assertIn("function noteScreenFrame()", frontend)
        self.assertIn(
            "queueTouchMove(ev.clientX, ev.clientY, ev.pointerType || 'pointer');",
            frontend,
        )
        self.assertIn("flushPendingTouchMove();\n  const elapsed", frontend)

    def test_qemu_legacy_python_resource_hook_rounds_still_run_for_malta(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="malta"))
        calls: list[dict[str, object]] = []
        backend._read_u32_paused_locked = lambda va: 0  # type: ignore[method-assign]
        backend._read_u8_paused_locked = lambda va: 1 if va == 0x804BF444 else 0  # type: ignore[method-assign]
        backend._read_pc_paused_locked = lambda: 0x80004000  # type: ignore[method-assign]
        backend._prime_resource_refresh_paused_locked = lambda: None  # type: ignore[method-assign]
        backend._service_legacy_python_resource_hook_paused_locked = lambda **kwargs: calls.append(dict(kwargs)) or {  # type: ignore[method-assign]
            "event": "qemu-legacy-python-resource-hook-service",
            "events": [],
            "handled_count": 0,
        }

        row = backend._service_legacy_python_resource_hook_rounds_paused_locked(rounds=1)

        self.assertFalse(row.get("skipped"), row)
        self.assertEqual(len(calls), 1)

    def test_qemu_bbk9588_storage_breakpoints_omit_c_ready_idle_checks(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        pcs = backend._legacy_python_storage_hook_pcs_for_machine()

        self.assertEqual(pcs, ())
        self.assertNotIn(0x8000BA84, pcs)
        self.assertNotIn(0x8000BB98, pcs)
        self.assertNotIn(0x80007648, pcs)
        self.assertNotIn(0x800067F4, pcs)
        self.assertNotIn(0x8000F7F8, pcs)
        self.assertNotIn(0x8000F8A0, pcs)
        self.assertNotIn(0x8000F0B0, pcs)
        self.assertNotIn(0x80182D6C, pcs)
        self.assertEqual(backend._scheduler_dispatch_pcs_for_machine(), ())
        self.assertEqual(backend._resource_trace_pcs_for_machine(), ())
        self.assertEqual(backend._resource_trace_service_pcs_for_machine(), ())

    def test_qemu_malta_storage_breakpoints_keep_ready_idle_checks(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="malta"))
        pcs = backend._legacy_python_storage_hook_pcs_for_machine()

        self.assertIn(0x8000BA84, pcs)
        self.assertIn(0x8000BB98, pcs)
        self.assertIn(0x80007648, pcs)
        self.assertIn(0x800067F4, pcs)
        self.assertIn(0x8000F7F8, pcs)
        self.assertIn(0x8000F8A0, pcs)
        self.assertIn(0x8000F0B0, pcs)
        self.assertIn(0x80182D6C, pcs)
        self.assertEqual(backend._scheduler_dispatch_pcs_for_machine(), (0x8000818C,))
        self.assertIn(0x8000818C, backend._resource_trace_pcs_for_machine())
        self.assertIn(0x8000BA84, backend._resource_trace_service_pcs_for_machine())

    def test_qemu_bbk9588_legacy_python_storage_hook_service_is_disabled(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend._seed_legacy_python_storage_hook_globals_paused_locked = lambda: self.fail("unexpected seed call")  # type: ignore[method-assign]

        row = backend._service_legacy_python_storage_hooks_paused_locked()

        self.assertTrue(row.get("disabled"), row)
        self.assertFalse(row.get("events"), row)
        self.assertEqual(row.get("handled_count"), 0)
        self.assertEqual(row.get("event"), "qemu-legacy-python-storage-hook-service")

    def test_qemu_bbk9588_uses_lcd_descriptor_scanout(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))

        row = backend.enable_lcd_mirror()

        self.assertTrue(row.get("enabled"), row)
        self.assertTrue(row.get("skipped"), row)
        self.assertEqual(row.get("source"), "jz4740-lcd")
        self.assertEqual(row.get("reason"), "jz4740-lcd-descriptor-scanout")

    def test_qemu_touch_device_snapshot_requires_touch_trace_option(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend._read_guest_ram_snapshot_locked = lambda *_args: self.fail("unexpected touch trace read")  # type: ignore[method-assign]

        row = backend.guest_touch_device_snapshot()

        self.assertFalse(row.get("available"), row)
        self.assertTrue(row.get("disabled"), row)
        self.assertIn("touch-trace", str(row.get("reason")))

    def test_qemu_touch_device_snapshot_reads_when_touch_trace_enabled(self) -> None:
        class _RunningProc:
            def poll(self) -> None:
                return None

        backend = QemuProcessBackend(
            QemuSystemConfig(machine="bbk9588", bbk_machine_options=("touch-trace=on",))
        )
        backend.proc = _RunningProc()  # type: ignore[assignment]
        trace = bytearray(0x144)
        struct.pack_into("<I", trace, 0, 0x54434B42)
        backend._read_guest_ram_snapshot_locked = lambda _addr, size: bytes(trace[:size])  # type: ignore[method-assign]

        row = backend.guest_touch_device_snapshot()

        self.assertTrue(row.get("available"), row)
        self.assertEqual(row.get("magic"), "0x54434b42")

    def test_qemu_legacy_python_storage_hook_breaks_are_disabled(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        backend._write_register_paused_locked = lambda reg, value: self.fail("unexpected GDB register write")  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = lambda va, data: self.fail("unexpected GDB memory write")  # type: ignore[method-assign]
        pcs = (
            0x8017BEF4,
            0x80182D58,
            0x80175E40,
            0x80174C9C,
            0x8017B4E0,
            0x8017CA10,
        )

        for pc in pcs:
            with self.subTest(pc=f"0x{pc:08x}"):
                row = backend._handle_legacy_python_storage_hook_break_paused_locked(pc)
                self.assertFalse(row.get("handled"), row)
                self.assertTrue(row.get("disabled"), row)
                self.assertEqual(row.get("pc"), f"0x{pc:08x}")
    def test_qemu_native_root_dirent_scan_skips_lfn_and_converts_entry(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        lfn = bytearray(0x20)
        lfn[0] = 0x41
        lfn[0x0B] = 0x0F
        dirent = bytearray(0x20)
        dirent[0:11] = b"MENU    BIN"
        dirent[0x0B] = 0x20
        struct.pack_into("<H", dirent, 0x14, 0x0001)
        struct.pack_into("<H", dirent, 0x1A, 0x2345)
        struct.pack_into("<I", dirent, 0x1C, 0x6789)
        sector = bytes(lfn + dirent + bytes(512 - 0x40))

        backend._fat16_layout_from_backing = lambda: {"root_lba": 0x159, "root_dir_sectors": 1}  # type: ignore[method-assign]
        backend._read_backing_sector = lambda sector_id: sector if sector_id == 0x159 else None  # type: ignore[method-assign]

        row = backend._first_root_dirent_from_backing()

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.get("name_hex"), b"MENU    BIN".hex())
        self.assertEqual(row.get("offset"), 0x20)
        self.assertEqual(row.get("cluster"), 0x00012345)
        self.assertEqual(row.get("size"), 0x6789)
        firmware = row.get("firmware")
        self.assertIsInstance(firmware, bytes)
        self.assertEqual(struct.unpack_from("<I", firmware, 0x14)[0], 0x00012345)

    def test_qemu_gui_timer_service_sets_event_flag(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="malta"))
        blocks: dict[int, bytearray] = {}
        writes: dict[int, bytes] = {}
        table = bytearray(0x40)
        struct.pack_into("<I", table, 0, 0x80801000)
        blocks[0x804A6B40] = table
        entry = bytearray(0x10)
        struct.pack_into("<IIII", entry, 0, 0x80802000, 7, 2, 1)
        blocks[0x80801000] = entry
        owner = bytearray(0xF4)
        struct.pack_into("<I", owner, 0xF0, 0x80803000)
        blocks[0x80802000] = owner
        event = bytearray(0xA0)
        struct.pack_into("<I", event, 0x20 + 3 * 4, 0x80802000)
        struct.pack_into("<I", event, 0x60 + 3 * 4, 7)
        blocks[0x80803000] = event

        def read_mem(va: int, size: int) -> bytes:
            for base, data in blocks.items():
                if base <= va and va + size <= base + len(data):
                    offset = va - base
                    return bytes(data[offset : offset + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            writes[va] = bytes(data)
            for base, block in blocks.items():
                if base <= va and va + len(data) <= base + len(block):
                    offset = va - base
                    block[offset : offset + len(data)] = data
                    return

        backend._is_guest_ram_va = lambda va, size=1: va >= 0x80000000 and size >= 0  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]

        result = backend._service_gui_timer_entries_paused_locked()

        self.assertEqual(result.get("fired"), 1, result)
        self.assertEqual(struct.unpack("<I", writes[0x8080100C])[0], 0)
        self.assertEqual(struct.unpack("<I", writes[0x80803000])[0], 1 << 3)
        self.assertEqual(backend.gui_timer_fire_count, 1)

    def test_bbk9588_gui_timer_service_uses_c_machine(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))

        result = backend._service_gui_timer_entries_paused_locked()

        self.assertTrue(result.get("skipped"), result)
        self.assertEqual(result.get("source"), "qemu-c-machine")
        self.assertEqual(result.get("reason"), "qemu-c-machine-gui-timer-service")

    def test_qemu_task_context_trace_reads_target_context(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        blocks: dict[int, bytearray] = {}
        globals_block = bytearray(0x80)
        struct.pack_into("<I", globals_block, 0x30, 0x806C5530)
        struct.pack_into("<I", globals_block, 0x50, 0x806C5370)
        globals_block[0x10] = 0x3F
        globals_block[0x11] = 0x09
        struct.pack_into("<I", globals_block, 0x1C, 4)
        blocks[0x80473F00] = globals_block
        target_node = bytearray(0x80)
        struct.pack_into("<I", target_node, 0, 0x8078D600)
        target_node[0x35] = 9
        target_node[0x36] = 1
        target_node[0x50:0x56] = b"task9\0"
        blocks[0x806C5530] = target_node
        current_node = bytearray(0x80)
        struct.pack_into("<I", current_node, 0, 0x806C5000)
        current_node[0x35] = 0x3F
        blocks[0x806C5370] = current_node
        ctx = bytearray(0x7C)
        struct.pack_into("<I", ctx, 0x70, 0x80173504)
        blocks[0x8078D600] = ctx
        regs = {29: 0x8078C000, 31: 0x800080F8}

        def read_mem(va: int, size: int) -> bytes:
            for base, data in blocks.items():
                if base <= va and va + size <= base + len(data):
                    offset = va - base
                    return bytes(data[offset : offset + size])
            raise KeyError(hex(va))

        backend._is_guest_ram_va = lambda va, size=1: va >= 0x80000000 and size >= 0  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: regs[reg]  # type: ignore[method-assign]

        row = backend._task_context_trace_row_paused_locked(0x800A7C18)

        self.assertEqual(row.get("kind"), "task-context-switch-save")
        self.assertEqual(row.get("target_node"), "0x806c5530")
        self.assertEqual(row.get("current_node"), "0x806c5370")
        self.assertEqual(row.get("target_ctx_sp"), "0x8078d600")
        self.assertEqual(row.get("target_pc"), "0x80173504")
        self.assertEqual(row.get("target_task", {}).get("name"), "task9")

    def test_qemu_scheduled_fs_scan_service_runs_only_for_fs_task(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        memory: dict[int, bytearray] = {
            0x806C5D10 + 9 * 4: bytearray(struct.pack("<I", 0x806C5530)),
            0x806C5530: bytearray(0x80),
            0x8078D600: bytearray(0x80),
            0x80473F08: bytearray(0x50),
            0x8024A998: bytearray(range(0x100)),
        }
        memory[0x80473F08][0x01] = 1
        memory[0x80473F08][0x08] = 0x3F
        memory[0x80473F08][0x09] = 0x3F
        memory[0x80473F08][0x30] = 1
        memory[0x80473F08][0x39] = 1
        struct.pack_into("<I", memory[0x806C5530], 0, 0x8078D600)
        memory[0x806C5530][0x35] = 9
        memory[0x806C5530][0x36] = 1
        memory[0x806C5530][0x50:0x56] = b"task9\0"
        struct.pack_into("<I", memory[0x8078D600], 0x70, 0x80173504)
        calls: list[tuple[float, int]] = []

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va and va + size <= 0x81000000  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._service_fs_scan_probe_paused_locked = lambda *, timeout, max_hits: calls.append((timeout, max_hits)) or {  # type: ignore[method-assign]
            "event": "qemu-fs-scan-probe",
            "native_fs_scan_fallback": {"available": True, "applied": True},
            "result_dirent": {"name_hex": "4141412020202020202020"},
        }

        row = backend._service_scheduled_fs_scan_task_paused_locked(9, timeout=1.25, max_hits=99)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(calls, [(1.25, 99)])
        self.assertEqual(row.get("context", {}).get("target_pc"), "0x80173504")
        self.assertEqual(row.get("context", {}).get("pc_candidates", {}).get("ctx_70"), "0x80173504")
        self.assertEqual(row.get("native_fs_scan_fallback", {}).get("applied"), True)

        struct.pack_into("<I", memory[0x8078D600], 0x70, 0x800080F0)
        struct.pack_into("<I", memory[0x8078D600], 0x74, 0x80173504)
        calls.clear()
        row = backend._service_scheduled_fs_scan_task_paused_locked(9)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(calls, [(2.0, 512)])

        struct.pack_into("<I", memory[0x8078D600], 0x74, 0x800080F0)
        calls.clear()
        row = backend._service_scheduled_fs_scan_task_paused_locked(9)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("trigger"), "scheduler-selected-task")
        self.assertEqual(calls, [(2.0, 512)])

        memory[0x80473F08][0x39] = 2
        calls.clear()
        row = backend._service_scheduled_fs_scan_task_paused_locked(9)

        self.assertFalse(row.get("handled"), row)
        self.assertEqual(row.get("reason"), "task-context-is-not-fs-scan")
        self.assertEqual(calls, [])

    def test_qemu_scheduler_ready_seed_marks_task_ready(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        scheduler = bytearray(0x3A)
        scheduler[0x01] = 1
        scheduler[0x08] = 0x3F
        scheduler[0x09] = 0x3F
        scheduler[0x30] = 0x80
        struct.pack_into("<I", scheduler, 0x28, 0x806C5370)
        memory: dict[int, bytearray] = {
            0x80473F08: scheduler,
            0x806C5D10 + 9 * 4: bytearray(struct.pack("<I", 0x806C5530)),
            0x806C5530: bytearray(0x80),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    off = va - base
                    block[off : off + len(data)] = data
                    return
            memory[va] = bytearray(data)

        backend._is_guest_ram_va = lambda va, size=1: va >= 0x80000000 and size >= 0  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]

        row = backend._seed_scheduler_ready_task_paused_locked(9)

        self.assertTrue(row.get("seeded"), row)
        self.assertEqual(row.get("group_after"), "0x82")
        self.assertEqual(read_mem(0x80473F38, 1), b"\x82")
        self.assertEqual(read_mem(0x80473F41, 1), b"\x02")
        self.assertEqual(read_mem(0x80473F08, 1), b"\x01")

    def test_qemu_bbk9588_scheduler_ready_seed_is_handled_by_c_machine(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))

        row = backend._seed_scheduler_ready_task_paused_locked(9)

        self.assertTrue(row.get("seeded"), row)
        self.assertTrue(row.get("skipped"), row)
        self.assertEqual(row.get("source"), "qemu-c-machine")

    def test_qemu_bbk9588_scheduler_tick_clamp_is_handled_by_c_machine(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))

        row = backend._clamp_scheduler_tick_paused_locked()

        self.assertTrue(row.get("clamped"), row)
        self.assertTrue(row.get("skipped"), row)
        self.assertEqual(row.get("source"), "qemu-c-machine")

    def test_qemu_bbk9588_settle_initial_gui_skips_python_services(self) -> None:
        class DummyProc:
            def poll(self) -> None:
                return None

        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.proc = DummyProc()  # type: ignore[assignment]
        backend.gdb_sock = None
        backend.snapshot = lambda: {"pc": "0x8000985c"}  # type: ignore[method-assign]
        backend._pause_for_gdb_locked = lambda: self.fail("unexpected GDB pause")  # type: ignore[method-assign]
        backend._service_legacy_python_storage_hooks_paused_locked = lambda **kwargs: self.fail(  # type: ignore[method-assign]
            "unexpected legacy Python storage hook service"
        )
        backend._service_legacy_python_resource_hook_rounds_paused_locked = lambda **kwargs: self.fail(  # type: ignore[method-assign]
            "unexpected legacy Python resource hook service"
        )
        backend._seed_scheduler_ready_task_paused_locked = lambda task: self.fail(  # type: ignore[method-assign]
            "unexpected Python scheduler ready seed"
        )
        backend._clamp_scheduler_tick_paused_locked = lambda: self.fail(  # type: ignore[method-assign]
            "unexpected Python scheduler tick clamp"
        )
        backend._service_system_boot_file_probes_paused_locked = lambda **kwargs: self.fail(  # type: ignore[method-assign]
            "unexpected Python boot file probes"
        )
        backend._service_fs_scan_probe_paused_locked = lambda **kwargs: self.fail(  # type: ignore[method-assign]
            "unexpected Python fs scan probe"
        )
        backend._pump_gui_event_poller_paused_locked = lambda: self.fail("unexpected Python event poller")  # type: ignore[method-assign]
        backend._pump_gui_idle_dispatcher_paused_locked = lambda: self.fail("unexpected Python idle dispatcher")  # type: ignore[method-assign]

        row = backend.settle_initial_gui()

        self.assertEqual(row.get("source"), "qemu-c-machine")
        self.assertTrue(row.get("skipped_python_services"), row)
        self.assertEqual(row.get("reason"), "bbk9588-c-machine-default-path")
        self.assertNotIn("system_boot_file_probes", row)
        self.assertNotIn("event_poller", row)
        self.assertEqual(row.get("final_pc"), "0x8000985c")
        self.assertTrue(row.get("legacy_python_storage_hook", {}).get("skipped"), row)
        self.assertTrue(row.get("legacy_python_resource_hook", {}).get("skipped"), row)

    def test_qemu_bbk9588_python_guest_services_are_disabled(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.proc = object()  # type: ignore[assignment]
        backend.gdb_sock = object()  # type: ignore[assignment]
        backend._is_guest_ram_va = lambda va, size=1: True  # type: ignore[method-assign]
        backend._read_u32_paused_locked = lambda va: self.fail("unexpected guest read")  # type: ignore[method-assign]
        backend._read_u8_paused_locked = lambda va: self.fail("unexpected guest read")  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda regno: self.fail("unexpected register read")  # type: ignore[method-assign]
        backend._write_u8_paused_locked = lambda va, value: self.fail("unexpected guest write")  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = lambda va, data: self.fail("unexpected guest write")  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda regno, value: self.fail("unexpected register write")  # type: ignore[method-assign]
        backend._call_guest_function_stepped_paused_locked = lambda *args, **kwargs: self.fail("unexpected guest call")  # type: ignore[method-assign]

        rows = [
            backend._service_fs_scan_probe_paused_locked(),
            backend._service_scheduled_fs_scan_task_paused_locked(),
            backend._service_task_context_trace_paused_locked(),
            backend._service_fs_trace_paused_locked(),
            backend._service_event_loop_trace_paused_locked(),
            backend._prepare_backing_file_path_probe_paused_locked(event="qemu-file-open-probe"),
            backend._service_first_file_open_probe_paused_locked(),
            backend._service_first_file_high_level_open_probe_paused_locked(),
            backend._pump_gui_idle_dispatcher_paused_locked(),
            backend._settle_gui_modal_close_paused_locked(),
            backend._pump_gui_event_poller_paused_locked(),
            backend._settle_gui_repaint_paused_locked(),
        ]

        self.assertIsNone(backend._prime_resource_refresh_paused_locked())
        for row in rows:
            self.assertTrue(row.get("disabled"), row)
            self.assertTrue(row.get("skipped"), row)
            self.assertEqual(row.get("source"), "qemu-c-machine")
            self.assertEqual(row.get("reason"), "bbk9588-c-machine-default-path")
            self.assertFalse(row.get("handled"), row)

    def test_qemu_scheduler_ready_sanitize_clears_missing_task_nodes(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="malta"))
        memory: dict[int, bytearray] = {
            0x80473F38: bytearray(b"\x82"),
            0x80473F40: bytearray(b"\x01\x06"),
            0x806C5D10: bytearray(struct.pack("<I", 0x806C5300)),
            0x806C5D10 + 9 * 4: bytearray(struct.pack("<I", 0x806C5530)),
            0x806C5300: bytearray(0x80),
            0x806C5530: bytearray(0x80),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    off = va - base
                    block[off : off + len(data)] = data
                    return
            memory[va] = bytearray(data)

        backend._is_guest_ram_va = lambda va, size=1: 0x806C0000 <= va and va + size <= 0x80700000  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]

        row = backend._sanitize_scheduler_ready_bits_paused_locked()

        self.assertTrue(row.get("changed"), row)
        self.assertEqual(row.get("cleared_tasks"), ["0x0a"])
        self.assertEqual(read_mem(0x80473F38, 1), b"\x82")
        self.assertEqual(read_mem(0x80473F40, 2), b"\x01\x02")

    def test_bbk9588_scheduler_ready_sanitize_uses_c_machine(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))

        row = backend._sanitize_scheduler_ready_bits_paused_locked()

        self.assertFalse(row.get("changed"), row)
        self.assertTrue(row.get("skipped"), row)
        self.assertEqual(row.get("source"), "qemu-c-machine")
        self.assertEqual(row.get("reason"), "qemu-c-machine-scheduler-ready-sanitize")

    def test_qemu_scheduler_dispatch_task_node_returns_on_missing_node(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0, 37: 0x8000818C}

        backend._is_guest_ram_va = lambda va, size=1: False  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_scheduler_dispatch_task_node_paused_locked(0x8000818C)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("reason"), "missing-task-node-return-dispatcher")
        self.assertEqual(registers[37], 0x800081B8)

    def test_qemu_scheduler_dispatch_snapshot_computes_next_task(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        fields = bytearray(0x50)
        fields[0x01] = 1
        fields[0x08] = 0x3F
        fields[0x09] = 0x3F
        fields[0x30] = 1
        fields[0x39] = 1
        order = bytearray(range(0x100))
        memory: dict[int, bytearray] = {
            0x80473F08: fields,
            0x8024A998: order,
            0x806C5D10 + 9 * 4: bytearray(struct.pack("<I", 0x806C5530)),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]

        row = backend._scheduler_dispatch_snapshot_paused_locked()

        self.assertTrue(row.get("computed"), row)
        self.assertEqual(row.get("next_task"), "0x09")
        self.assertEqual(row.get("target_node"), "0x806c5530")

    def test_qemu_event_loop_empty_return_uses_scratch_event(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0x000000FF, 4: 0x8024A22C}
        memory: dict[int, bytearray] = {
            0x80473F6C: bytearray(struct.pack("<I", 0x806C5160)),
            0x804BF440: bytearray(struct.pack("<I", 0x00080000)),
            0x807F7300: bytearray(b"\xAA" * 0x1C),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    off = va - base
                    block[off : off + len(data)] = data
                    return
            memory[va] = bytearray(data)

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va < 0x81000000 and size >= 0  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        first = backend._handle_event_loop_empty_return_paused_locked(0x8012CCFC)
        self.assertTrue(first.get("handled"), first)
        self.assertEqual(first.get("event_code"), "0x00000006")
        self.assertEqual(registers[2], 0x807F7300)
        self.assertEqual(registers[5], 6)
        self.assertEqual(registers[37], 0x8012CD00)
        words = struct.unpack("<7I", read_mem(0x807F7300, 0x1C))
        self.assertEqual(words[1], 6)
        self.assertEqual(backend.event_loop_empty_fix_count, 1)
        self.assertEqual(backend.event_loop_synth_event_count, 1)

        registers[2] = 0x000000FF
        second = backend._handle_event_loop_empty_return_paused_locked(0x8012CCFC)
        self.assertTrue(second.get("handled"), second)
        self.assertEqual(second.get("event_code"), "0x00000007")
        self.assertEqual(registers[5], 7)
        self.assertEqual(registers[37], 0x8012CD00)
        words = struct.unpack("<7I", read_mem(0x807F7300, 0x1C))
        self.assertEqual(words[1], 7)
        self.assertEqual(backend.event_loop_empty_fix_count, 2)
        self.assertEqual(backend.event_loop_synth_event_count, 2)

        registers[2] = 0x000000FF
        third = backend._handle_event_loop_empty_return_paused_locked(0x8012CCFC)
        self.assertTrue(third.get("handled"), third)
        self.assertEqual(third.get("event_code"), "0x00000004")
        self.assertEqual(registers[5], 4)
        self.assertEqual(
            third.get("resource_state_pump"),
            {
                "stage": "initialized-resource-flag",
                "flags_before": "0x00080000",
                "flags_after": "0x00000004",
                "byte_804bf444_before": "0x00",
                "byte_804bf444_after": "0x00",
            },
        )
        words = struct.unpack("<7I", read_mem(0x807F7300, 0x1C))
        self.assertEqual(words[1], 4)
        self.assertEqual(struct.unpack("<I", read_mem(0x804BF440, 4))[0], 0x00000004)
        self.assertEqual(backend.event_loop_empty_fix_count, 3)
        self.assertEqual(backend.event_loop_synth_event_count, 3)

        registers[2] = 0x000000FF
        fourth = backend._handle_event_loop_empty_return_paused_locked(0x8012CCFC)
        self.assertTrue(fourth.get("handled"), fourth)
        self.assertNotIn("stop_service", fourth)
        self.assertEqual(fourth.get("event_code"), "0x00000004")
        self.assertEqual(registers[5], 4)
        self.assertEqual(
            fourth.get("resource_state_pump"),
            {
                "stage": "arm-resource-refresh",
                "flags_before": "0x00000004",
                "flags_after": "0x00000000",
                "byte_804bf444_before": "0x00",
                "byte_804bf444_after": "0x01",
            },
        )
        self.assertEqual(struct.unpack("<I", read_mem(0x804BF440, 4))[0], 0x00000000)
        self.assertEqual(read_mem(0x804BF444, 1), b"\x01")
        self.assertEqual(backend.event_loop_empty_fix_count, 4)
        self.assertEqual(backend.event_loop_synth_event_count, 4)

        registers[2] = 0x000000FF
        fifth = backend._handle_event_loop_empty_return_paused_locked(0x8012CCFC)
        self.assertTrue(fifth.get("handled"), fifth)
        self.assertNotIn("stop_service", fifth)
        self.assertEqual(fifth.get("event_code"), "0x00000004")
        self.assertEqual(registers[5], 4)
        self.assertEqual(
            fifth.get("resource_state_pump"),
            {
                "stage": "resource-refresh-ready",
                "flags_before": "0x00000000",
                "flags_after": "0x00000000",
                "byte_804bf444_before": "0x01",
                "byte_804bf444_after": "0x01",
            },
        )
        self.assertEqual(backend.event_loop_empty_fix_count, 5)
        self.assertEqual(backend.event_loop_synth_event_count, 5)

        registers[2] = 0x000000FF
        sixth = backend._handle_event_loop_empty_return_paused_locked(0x8012CCFC)
        self.assertTrue(sixth.get("handled"), sixth)
        self.assertNotIn("stop_service", sixth)
        self.assertEqual(sixth.get("event_code"), "0x00000003")
        self.assertEqual(registers[5], 3)
        self.assertEqual(backend.event_loop_empty_fix_count, 6)
        self.assertEqual(backend.event_loop_synth_event_count, 6)

        registers[2] = 0x000000FF
        seventh = backend._handle_event_loop_empty_return_paused_locked(0x8012CCFC)
        self.assertTrue(seventh.get("handled"), seventh)
        self.assertTrue(seventh.get("stop_service"), seventh)
        self.assertEqual(seventh.get("event_code"), "0x00000000")
        self.assertEqual(registers[5], 0)
        words = struct.unpack("<7I", read_mem(0x807F7300, 0x1C))
        self.assertEqual(words[1], 0)
        self.assertEqual(backend.event_loop_empty_fix_count, 7)
        self.assertEqual(backend.event_loop_synth_event_count, 6)

    def test_qemu_event_loop_empty_skips_legacy_python_resource_hook_when_bbk9588_c_ready(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig(machine="bbk9588"))
        backend.event_loop_synth_event_count = 2
        registers = {2: 0x000000FF, 4: 0x8024A22C}
        memory: dict[int, bytearray] = {
            0x80473F6C: bytearray(struct.pack("<I", 0x806C5160)),
            0x804BF440: bytearray(struct.pack("<I", 0)),
            0x804BF444: bytearray(b"\x01"),
            0x807F7300: bytearray(b"\xAA" * 0x1C),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    off = va - base
                    block[off : off + len(data)] = data
                    return
            memory[va] = bytearray(data)

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va < 0x81000000 and size >= 0  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]
        backend._pump_resource_state_globals_paused_locked = lambda: self.fail("unexpected legacy Python resource hook")  # type: ignore[method-assign]

        row = backend._handle_event_loop_empty_return_paused_locked(0x8012CCFC)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("event_code"), "0x00000004")
        self.assertNotIn("resource_state_pump", row)
        skipped = row.get("resource_state_pump_skipped")
        self.assertIsInstance(skipped, dict)
        assert isinstance(skipped, dict)
        self.assertEqual(skipped.get("source"), "qemu-c-machine")
        self.assertEqual(registers[5], 4)

    def test_qemu_resource_trace_row_reads_service_globals(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0x12345678, 4: 0x804BF43C, 5: 0, 6: 0x807F7300, 7: 7, 29: 0x8033AE50, 31: 0x8012CEBC}
        memory: dict[int, bytearray] = {
            0x804BF43C: bytearray(struct.pack("<IIII", 0x806C7000, 0x00000001, 0x00000002, 0x00000003)),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]

        row = backend._resource_trace_row_paused_locked(0x8017B198)

        self.assertEqual(row.get("pc"), "0x8017b198")
        regs = row.get("regs")
        self.assertIsInstance(regs, dict)
        assert isinstance(regs, dict)
        self.assertEqual(regs.get("a0"), "0x804bf43c")
        globals_ = row.get("globals")
        self.assertIsInstance(globals_, dict)
        assert isinstance(globals_, dict)
        self.assertEqual(globals_.get("resource_queue_804bf43c"), "0x806c7000")
        self.assertEqual(globals_.get("resource_flags_804bf440"), "0x00000001")
        self.assertIn("desktop_resource_mgr_80478358", globals_)
        self.assertIn("desktop_resource_count_8047835c", globals_)

    def test_qemu_resource_trace_pcs_cover_dir_scan_consumers(self) -> None:
        pcs = set(QemuProcessBackend._resource_trace_pcs())

        self.assertIn(0x80171620, pcs)
        self.assertIn(0x801716FC, pcs)
        self.assertIn(0x801717F4, pcs)
        self.assertIn(0x80171800, pcs)
        self.assertIn(0x801718A0, pcs)
        self.assertIn(0x801718B4, pcs)
        self.assertIn(0x801718BC, pcs)
        self.assertIn(0x80173920, pcs)
        self.assertIn(0x8017395C, pcs)
        self.assertIn(0x8001E8B4, pcs)
        self.assertIn(0x8001E8C0, pcs)
        self.assertIn(0x8000FC74, pcs)
        self.assertIn(0x800100DC, pcs)
        self.assertIn(0x8001028C, pcs)
        self.assertIn(0x8001032C, pcs)
        self.assertIn(0x800E1A94, pcs)
        self.assertIn(0x800E1BF0, pcs)
        self.assertIn(0x800E1C68, pcs)
        self.assertIn(0x800E1C84, pcs)
        self.assertIn(0x800E3C68, pcs)
        self.assertIn(0x800E447C, pcs)
        self.assertIn(0x800E5C44, pcs)
        self.assertIn(0x800E5C58, pcs)
        self.assertIn(0x800DFC68, pcs)
        self.assertIn(0x8001E900, pcs)
        self.assertIn(0x80172630, pcs)
        self.assertIn(0x80172670, pcs)
        self.assertIn(0x8017268C, pcs)
        self.assertIn(0x801726F4, pcs)
        self.assertIn(0x80172700, pcs)
        self.assertIn(0x8017E000, pcs)
        self.assertIn(0x801813E0, pcs)
        self.assertIn(0x80181400, pcs)

    def test_qemu_resource_trace_branch_handles_dirent_attribute_delay_slot(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0x10, 20: 0, 29: 0x8078DA90, 37: 0x80173928}
        memory = {0x8078DA90 + 0x7C: bytearray(struct.pack("<I", 0x000030F4))}

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]

        row = backend._handle_resource_trace_branch_paused_locked(0x80173928)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("return_pc"), "0x80173930")
        self.assertEqual(registers[20], 0x000030F4)
        self.assertEqual(registers[37], 0x80173930)

        registers[2] = 0
        row = backend._handle_resource_trace_branch_paused_locked(0x80173928)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("return_pc"), "0x80173e84")
        self.assertEqual(registers[37], 0x80173E84)

    def test_qemu_resource_trace_branch_returns_ready_for_desktop_resource_buffer(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        data_va = 0x80960200
        registers = {2: 0, 19: data_va, 31: 0x801754E4, 37: 0x801705EC}
        memory = {data_va: bytearray(b"\xE5LX\x07")}

        def read_mem(va: int, size: int) -> bytes:
            offset = va - data_va
            return bytes(memory[data_va][offset : offset + size])

        def write_mem(va: int, data: bytes) -> None:
            offset = va - data_va
            memory[data_va][offset : offset + len(data)] = data

        backend._is_guest_ram_va = lambda va, size=1: data_va <= va and va + size <= data_va + len(memory[data_va])  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_resource_trace_branch_paused_locked(0x801705EC)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("reason"), "desktop-resource-buffer-ready")
        self.assertEqual(row.get("restored_header"), "DLX")
        self.assertEqual(registers[2], 1)
        self.assertEqual(registers[37], 0x801754E4)
        self.assertEqual(bytes(memory[data_va][:3]), b"DLX")

    def test_qemu_resource_trace_callsite_returns_ready_for_desktop_resource_buffer(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        data_va = 0x80960200
        registers = {2: 0, 19: data_va, 37: 0x801754DC}
        memory = {data_va: bytearray(b"\xE5M6\x36")}

        def read_mem(va: int, size: int) -> bytes:
            offset = va - data_va
            return bytes(memory[data_va][offset : offset + size])

        def write_mem(va: int, data: bytes) -> None:
            offset = va - data_va
            memory[data_va][offset : offset + len(data)] = data

        backend._is_guest_ram_va = lambda va, size=1: data_va <= va and va + size <= data_va + len(memory[data_va])  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_resource_trace_branch_paused_locked(0x801754DC)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("reason"), "desktop-resource-buffer-ready-at-callsite")
        self.assertEqual(row.get("restored_header"), "BM")
        self.assertEqual(registers[2], 1)
        self.assertEqual(registers[37], 0x801754E4)
        self.assertEqual(bytes(memory[data_va][:3]), b"BM6")

    def test_qemu_semaphore_wait_fastpath_forces_empty_acquire(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0, 4: 0x806C5230, 6: 0x8033AE20, 31: 0x8000F838}
        memory: dict[int, bytearray] = {
            0x806C5230: bytearray(b"\x03\x00\x00\x00" + struct.pack("<I", 0)),
            0x8033AE20: bytearray(b"\xAA"),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    off = va - base
                    block[off : off + len(data)] = data
                    return
            memory[va] = bytearray(data)

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va < 0x81000000 and size >= 0  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_semaphore_wait_paused_locked(0x8000BA84)

        self.assertTrue(row.get("handled"), row)
        self.assertTrue(row.get("forced_empty_acquire"), row)
        self.assertEqual(read_mem(0x8033AE20, 1), b"\x00")
        self.assertEqual(registers[2], 0)
        self.assertEqual(registers[37], 0x8000F838)

    def test_qemu_semaphore_release_fastpath_increments_count(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0xFFFFFFFF, 4: 0x806C5230, 31: 0x8017FC94}
        memory: dict[int, bytearray] = {
            0x806C5230: bytearray(b"\x03\x00\x00\x00" + struct.pack("<I", 0)),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    off = va - base
                    block[off : off + len(data)] = data
                    return
            memory[va] = bytearray(data)

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va < 0x81000000 and size >= 0  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_semaphore_release_paused_locked(0x8000BB98)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("count_after"), "0x00000001")
        self.assertEqual(read_mem(0x806C5234, 4), struct.pack("<I", 1))
        self.assertEqual(registers[2], 0)
        self.assertEqual(registers[37], 0x8017FC94)

    def test_qemu_storage_ready_check_fastpath_returns_ready(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0xFFFFFFFF, 31: 0x80170310}
        memory: dict[int, bytearray] = {
            0x80477CE0: bytearray(struct.pack("<I", 0x806C5230)),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_storage_ready_check_paused_locked(0x8000F7F8)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("kind"), "storage-ready-check")
        self.assertEqual(row.get("storage_object_80477ce0"), "0x806c5230")
        self.assertEqual(registers[2], 0)
        self.assertEqual(registers[37], 0x80170310)

    def test_qemu_storage_idle_check_fastpath_returns_not_busy(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0xFFFFFFFF, 31: 0x80170600}
        memory: dict[int, bytearray] = {
            0x80477CE0: bytearray(struct.pack("<I", 0x806C5230)),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_storage_idle_check_paused_locked(0x8000F8A0)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("kind"), "storage-idle-check")
        self.assertEqual(row.get("storage_object_80477ce0"), "0x806c5230")
        self.assertEqual(row.get("value"), "0x00000000")
        self.assertEqual(registers[2], 0)
        self.assertEqual(registers[37], 0x80170600)

    def test_qemu_storage_idle_check_fastpath_reports_resource_refresh_ready(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0, 31: 0x801705FC}
        memory: dict[int, bytearray] = {
            0x80477CE0: bytearray(struct.pack("<I", 0x806C5230)),
            0x804BF440: bytearray(struct.pack("<I", 0)),
            0x804BF444: bytearray(b"\x01"),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_storage_idle_check_paused_locked(0x8000F8A0)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("resource_flags_804bf440"), "0x00000000")
        self.assertEqual(row.get("resource_byte_804bf444"), "0x01")
        self.assertEqual(row.get("value"), "0x00000001")
        self.assertEqual(registers[2], 1)
        self.assertEqual(registers[37], 0x801705FC)

    def test_qemu_heap_alloc_fastpath_returns_zeroed_guest_buffer(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        backend.qemu_heap_next = 0x80960000
        registers = {2: 0, 4: 0x21, 31: 0x801735B8}
        memory: dict[int, bytearray] = {}

        def write_mem(va: int, data: bytes) -> None:
            memory[va] = bytearray(data)

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va and va + size <= 0x81000000  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_heap_alloc_paused_locked(0x80007648)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("kind"), "heap-alloc")
        self.assertEqual(row.get("ptr"), "0x80960000")
        self.assertEqual(registers[2], 0x80960000)
        self.assertEqual(registers[37], 0x801735B8)
        self.assertEqual(bytes(memory[0x80960000]), bytes(0x30))
        self.assertEqual(backend.qemu_heap_next, 0x80960030)

    def test_qemu_heap_free_fastpath_noops(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0xFFFFFFFF, 4: 0x80960000, 31: 0x8017379C}
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_heap_free_paused_locked(0x800067F4)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("kind"), "heap-free")
        self.assertEqual(registers[2], 0)
        self.assertEqual(registers[37], 0x8017379C)

    def test_qemu_raw_sector_read_fastpath_reads_backing_sector(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0xFFFFFFFF, 4: 3, 5: 0x8078D280, 31: 0x801706E8}
        dest = bytearray(b"\xAA" * 512)
        memory: dict[int, bytearray] = {0x8078D280: dest}
        sector = bytes((i & 0xFF) for i in range(512))

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    off = va - base
                    block[off : off + len(data)] = data
                    return
            memory[va] = bytearray(data)

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va < 0x81000000 and size >= 0  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]
        backend._read_backing_sector = lambda value: sector if value == 3 else None  # type: ignore[method-assign]

        row = backend._handle_raw_sector_read_paused_locked(0x8000F0B0)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("kind"), "raw-sector-read")
        self.assertEqual(read_mem(0x8078D280, 16), sector[:16])
        self.assertEqual(registers[2], 0)
        self.assertEqual(registers[37], 0x801706E8)

    def test_qemu_cached_sector_read_fastpath_reads_block_offset(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0xFFFFFFFF, 4: 0x8078D800, 5: 2, 6: 1, 7: 2, 31: 0x80182B38}
        dest = bytearray(b"\xAA" * 1024)
        memory: dict[int, bytearray] = {
            0x8078D800: dest,
            0x804BF480: bytearray(struct.pack("<I", 0x800)),
        }
        sectors = {9: b"\x09" * 512, 10: b"\x0A" * 512}

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    off = va - base
                    block[off : off + len(data)] = data
                    return
            memory[va] = bytearray(data)

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va < 0x81000000 and size >= 0  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]
        backend._read_backing_sector = lambda value: sectors.get(value)  # type: ignore[method-assign]

        row = backend._handle_cached_sector_read_paused_locked(0x80182D6C)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("start_sector"), "0x9")
        self.assertEqual(read_mem(0x8078D800, 512), b"\x09" * 512)
        self.assertEqual(read_mem(0x8078DA00, 512), b"\x0A" * 512)
        self.assertEqual(registers[2], 0)
        self.assertEqual(registers[37], 0x80182B38)

    def test_qemu_dir_sector_read_fastpath_reads_backing_sector(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {2: 0xFFFFFFFF, 4: 0, 5: 0x159, 6: 0x80960000, 31: 0x80173628}
        dest = bytearray(b"\xAA" * 512)
        sector = bytes((0x80 + i) & 0xFF for i in range(512))
        memory: dict[int, bytearray] = {0x80960000: dest}

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    off = va - base
                    block[off : off + len(data)] = data
                    return
            memory[va] = bytearray(data)

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va and va + size <= 0x81000000  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]
        backend._read_backing_sector = lambda sector_id: sector if sector_id == 0x159 else None  # type: ignore[method-assign]

        row = backend._handle_dir_sector_read_paused_locked(0x80175D9C)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("kind"), "dir-sector-read")
        self.assertEqual(dest, bytearray(sector))
        self.assertEqual(registers[2], 0)
        self.assertEqual(registers[37], 0x80173628)

    def test_qemu_legacy_python_storage_hook_cache16_breaks_are_disabled(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        backend._write_register_paused_locked = lambda reg, value: self.fail("unexpected GDB register write")  # type: ignore[method-assign]
        backend._write_u32_paused_locked = lambda va, value: self.fail("unexpected GDB memory write")  # type: ignore[method-assign]

        row = backend._handle_legacy_python_storage_hook_break_paused_locked(0x8017CA10)

        self.assertFalse(row.get("handled"), row)
        self.assertTrue(row.get("disabled"), row)
        self.assertEqual(row.get("pc"), "0x8017ca10")
    def test_qemu_fs_dir_scan_branch_path_matches_expected_translation(self) -> None:
        cases = [
            (0x80173630, {21: 4, 22: 4}, {4: 0x12345678, 37: 0x80173F2C}),
            (0x80173640, {3: 1}, {4: 0xE5, 37: 0x80173F14}),
            (0x80173710, {2: 0xAA, 3: 0xAA}, {2: 0xCAFEBABE, 37: 0x8017375C}),
            (0x80173768, {3: 1, 17: 0x80960020}, {17: 0x80960040, 37: 0x80173630}),
            (0x80173F14, {3: 0xE5, 4: 0xE5}, {2: 0x2E, 37: 0x8017375C}),
            (0x80173F1C, {2: 0x11, 3: 0x22, 18: 0x80960000}, {2: 0x80960020, 37: 0x80173704}),
            (0x80173F24, {2: 0x12345678}, {18: 0x5678, 37: 0x80173764}),
            (0x80173F2C, {4: 0}, {2: 0x87654321, 37: 0x80173638}),
        ]
        for pc, initial, expected in cases:
            with self.subTest(pc=f"0x{pc:08x}"):
                backend = QemuProcessBackend(QemuSystemConfig())
                registers = {
                    2: 0,
                    3: 0,
                    4: 0,
                    17: 0x80960000,
                    18: 0,
                    21: 0,
                    22: 1,
                    29: 0x8078D000,
                    37: pc,
                }
                registers.update(initial)
                stack_words = {
                    0x8078D000 + 0x88: 0xCAFEBABE,
                    0x8078D000 + 0x90: 0x87654321,
                    0x8078D000 + 0x9C: 0x12345678,
                }

                backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
                backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]
                backend._read_u32_paused_locked = lambda va: stack_words.get(va)  # type: ignore[method-assign]

                row = backend._handle_fs_dir_scan_branch_paused_locked(pc)

                self.assertTrue(row.get("handled"), row)
                self.assertEqual(row.get("kind"), "fs-dir-scan-branch")
                for reg, value in expected.items():
                    self.assertEqual(registers[reg], value)

    def test_qemu_dirent_path_match_returns_consumed_path_pointer(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        registers = {
            4: 0,
            5: 1,
            6: 0x8078A000,
            7: 0x80960020,
            29: 0x8078D000,
            31: 0x80173754,
            37: 0x801747C4,
        }
        memory: dict[int, bytearray] = {
            0x8078D010: bytearray(struct.pack("<I", 0x80961000)),
            0x80961000: bytearray(b"\\*.*\0" + bytes(0x80)),
            0x80960020: bytearray(bytes.fromhex("d3a6d3c3202020202020201000000000215c215c00000000215cf43000000000")),
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    off = va - base
                    return bytes(data[off : off + size])
            raise KeyError(hex(va))

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va and va + size <= 0x81000000  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._read_u8_paused_locked = lambda va: read_mem(va, 1)[0]  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_dirent_path_match_paused_locked(0x801747C4)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("kind"), "dirent-path-match")
        self.assertEqual(row.get("matched"), True)
        self.assertEqual(registers[2], 0x80961004)
        self.assertEqual(registers[37], 0x80173754)

    def test_qemu_resource_dir_scan_fast_forward_targets_later_loaded_dirent(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        base = 0x809650A0
        registers = {
            7: base + 0x1E0,
            17: base + 0x1E0,
            18: 0x1E0,
            19: 0x8096462C,
        }
        cluster = bytearray(0x2000)
        cedic = bytearray(0x20)
        cedic[:11] = b"CEDIC   DAT"
        cedic[0x0B] = 0x20
        systp = bytearray(0x20)
        systp[:11] = b"SYSTP   CFG"
        systp[0x0B] = 0x20
        cluster[0x1E0 : 0x200] = cedic
        for offset in range(0x200, 0x780, 0x20):
            cluster[offset] = 0xE5
        cluster[0x780 : 0x7A0] = systp
        memory: dict[int, bytearray] = {
            base: cluster,
            0x8096462C: bytearray(b"\\SysTp.cfg\0" + bytes(0x80)),
        }

        def read_mem(va: int, size: int) -> bytes:
            for item_base, data in memory.items():
                if item_base <= va and va + size <= item_base + len(data):
                    offset = va - item_base
                    return bytes(data[offset : offset + size])
            raise KeyError(hex(va))

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va and va + size <= 0x81000000  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._read_u8_paused_locked = lambda va: read_mem(va, 1)[0]  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]
        backend._fat16_long_name_aliases_by_raw = lambda: {}  # type: ignore[method-assign]

        row = backend._prepare_resource_dir_scan_fast_forward_paused_locked(0x80173A90)

        self.assertTrue(row.get("applied"), row)
        self.assertEqual(row.get("found_offset"), "0x780")
        self.assertEqual(registers[7], base + 0x780)
        self.assertEqual(registers[17], base + 0x780)
        self.assertEqual(registers[18], 0x780)

    def test_qemu_resource_dir_branch_uses_current_dirent_cluster(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        sp = 0x8078D7F8
        dirent_va = 0x80967A60
        registers = {
            2: 0x10,
            4: dirent_va,
            20: 2,
            29: sp,
            37: 0x80173928,
        }
        desktop = bytearray(0x20)
        desktop[:11] = b"DESKTOP    "
        desktop[0x0B] = 0x10
        struct.pack_into("<H", desktop, 0x1A, 3)
        stack = bytearray(0x100)
        struct.pack_into("<I", stack, 0x7C, 2)
        memory: dict[int, bytearray] = {
            dirent_va: desktop,
            sp: stack,
        }

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    offset = va - base
                    return bytes(data[offset : offset + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    offset = va - base
                    block[offset : offset + len(data)] = data
                    return
            raise KeyError(hex(va))

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va and va + size <= 0x81000000  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]

        row = backend._handle_resource_trace_branch_paused_locked(0x80173928)

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("stacked_s4"), "0x00000002")
        self.assertEqual(row.get("loaded_s4"), "0x00000003")
        self.assertEqual(row.get("synced_dir_cluster"), True)
        self.assertEqual(registers[20], 3)
        self.assertEqual(struct.unpack_from("<I", memory[sp], 0x7C)[0], 3)

    def test_qemu_resource_open_return_can_succeed_from_system_backing_file(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        path = b"A:\\\xcf\xb5\xcd\xb3\\Desktop\\c200dts1a.dlx\x00"
        path_va = 0x80966D50
        registers = {2: 0xFFFFFFFF, 17: path_va}
        memory = {path_va: bytearray(path + bytes(0x200))}
        entry = {"path": b"\\\xcf\xb5\xcd\xb3\\Desktop\\c200dts1a.dlx", "cluster": 4, "size": 8}

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    offset = va - base
                    return bytes(data[offset : offset + size])
            raise KeyError(hex(va))

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va and va + size <= 0x81000000  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._read_u8_paused_locked = lambda va: read_mem(va, 1)[0]  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]
        backend._find_path_from_backing = lambda raw: entry if bytes(raw).rstrip(b"\0").lower().endswith(b"c200dts1a.dlx") else None  # type: ignore[method-assign]
        backend._system_boot_file_paths_from_backing = lambda: [entry["path"]]  # type: ignore[method-assign]
        backend._read_backing_file_bytes = lambda item: b"DLX\x00data" if item is entry else None  # type: ignore[method-assign]

        row = backend._prepare_resource_open_success_from_backing_paused_locked(0x80172700)

        self.assertTrue(row.get("applied"), row)
        self.assertEqual(registers[2], 0)
        self.assertEqual(row.get("cluster"), "0x00000004")
        self.assertEqual(row.get("read_size"), 8)

    def test_qemu_resource_object_count_uses_previous_system_dlx_path(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        path_va = 0x8078DC00
        current_path_va = path_va + 0x32
        registers = {2: 0xFFFFFFFF, 16: current_path_va}
        dlx_block = bytearray(0x78)
        dlx_block[:6] = b"DLX\x07\x01\x03"
        struct.pack_into("<I", dlx_block, 12, 0x78)
        dlx = bytes(dlx_block)
        memory = {
            path_va: bytearray(
                b"a:\\\xcf\xb5\xcd\xb3\\Desktop\\c200dts1a.dlx\x00"
                + bytes(current_path_va - path_va - len(b"a:\\\xcf\xb5\xcd\xb3\\Desktop\\c200dts1a.dlx\x00"))
                + b"a:\\\xcf\xb5\xcd\xb3\\Desktop\\c200dts1b.dlx\x00"
                + bytes(0x200)
            ),
            0x8047835C: bytearray(4),
        }
        entry = {"path": b"\\\xcf\xb5\xcd\xb3\\Desktop\\c200dts1a.dlx", "cluster": 4, "size": len(dlx)}

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    offset = va - base
                    return bytes(data[offset : offset + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    offset = va - base
                    block[offset : offset + len(data)] = data
                    return
            raise KeyError(hex(va))

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va and va + size <= 0x81000000  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]
        backend._find_path_from_backing = lambda raw: entry if bytes(raw).rstrip(b"\0").lower().endswith(b"c200dts1a.dlx") else None  # type: ignore[method-assign]
        backend._read_backing_file_bytes = lambda item: dlx if item is entry else None  # type: ignore[method-assign]

        row = backend._prepare_resource_object_count_from_backing_paused_locked(0x8001E8D0)

        self.assertTrue(row.get("applied"), row)
        self.assertEqual(registers[2], 7)
        self.assertEqual(struct.unpack("<I", memory[0x8047835C])[0], 7)
        self.assertEqual(row.get("count"), 7)

    def test_qemu_synthetic_desktop_resource_manager_builds_countable_list(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        backend.qemu_heap_next = 0x80960000
        path_va = 0x8078DC00
        current_path_va = path_va + 0x32
        registers = {16: current_path_va}
        dlx_block = bytearray(0x78)
        dlx_block[:6] = b"DLX\x03\x01\x03"
        struct.pack_into("<I", dlx_block, 12, 0x78)
        dlx = bytes(dlx_block)
        memory: dict[int, bytearray] = {
            path_va: bytearray(
                b"a:\\\xcf\xb5\xcd\xb3\\Desktop\\c200dts1a.dlx\x00"
                + bytes(current_path_va - path_va - len(b"a:\\\xcf\xb5\xcd\xb3\\Desktop\\c200dts1a.dlx\x00"))
                + b"a:\\\xcf\xb5\xcd\xb3\\Desktop\\c200dts1b.dlx\x00"
                + bytes(0x200)
            ),
            0x80478358: bytearray(8),
            0x80960000: bytearray(0x1000),
        }
        entry = {"path": b"\\\xcf\xb5\xcd\xb3\\Desktop\\c200dts1a.dlx", "cluster": 4, "size": len(dlx)}

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    offset = va - base
                    return bytes(data[offset : offset + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    offset = va - base
                    block[offset : offset + len(data)] = data
                    return
            if 0x80960000 <= va and va + len(data) <= 0x80961000:
                offset = va - 0x80960000
                memory[0x80960000][offset : offset + len(data)] = data
                return
            raise KeyError(hex(va))

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va and va + size <= 0x80A00000  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]
        backend._write_register_paused_locked = lambda reg, value: registers.__setitem__(reg, value & 0xFFFFFFFF)  # type: ignore[method-assign]
        backend._find_path_from_backing = lambda raw: entry if bytes(raw).rstrip(b"\0").lower().endswith(b"c200dts1a.dlx") else None  # type: ignore[method-assign]
        backend._read_backing_file_bytes = lambda item: dlx if item is entry else None  # type: ignore[method-assign]

        row = backend._prepare_synthetic_desktop_resource_manager_paused_locked(0x8001E8C0)

        self.assertTrue(row.get("applied"), row)
        manager = struct.unpack_from("<I", memory[0x80478358], 0)[0]
        self.assertEqual(manager, 0x80960000)
        self.assertEqual(struct.unpack_from("<I", memory[0x80478358], 4)[0], 3)
        self.assertEqual(registers.get(5), manager)
        state = struct.unpack("<I", read_mem(manager + 0x84, 4))[0]
        head = struct.unpack("<I", read_mem(state + 0x38, 4))[0]
        self.assertEqual(struct.unpack("<I", read_mem(state + 4, 4))[0], 3)
        self.assertEqual(struct.unpack("<I", read_mem(head + 0x0C, 4))[0], 1)
        self.assertEqual(struct.unpack("<I", read_mem(head + 0x10, 4))[0], 1)
        self.assertNotEqual(struct.unpack("<I", read_mem(head + 0x18, 4))[0], 0)

    def test_qemu_file_read_context_sync_uses_file_cluster(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        obj = 0x80964C70
        ctx = 0x806C5098
        registers = {16: obj, 22: ctx}
        memory: dict[int, bytearray] = {
            obj: bytearray(0x58),
            ctx: bytearray(struct.pack("<II", 0x236, 0x60) + bytes(0x38)),
        }
        struct.pack_into("<I", memory[obj], 0x18, 0x2312)
        struct.pack_into("<I", memory[obj], 0x20, 0x47)
        memory[obj][0x0F] = 0x20

        def read_mem(va: int, size: int) -> bytes:
            for base, data in memory.items():
                if base <= va and va + size <= base + len(data):
                    offset = va - base
                    return bytes(data[offset : offset + size])
            raise KeyError(hex(va))

        def write_mem(va: int, data: bytes) -> None:
            for base, block in memory.items():
                if base <= va and va + len(data) <= base + len(block):
                    offset = va - base
                    block[offset : offset + len(data)] = data
                    return
            raise KeyError(hex(va))

        backend._is_guest_ram_va = lambda va, size=1: 0x80000000 <= va and va + size <= 0x81000000  # type: ignore[method-assign]
        backend._read_virtual_memory_paused_locked = read_mem  # type: ignore[method-assign]
        backend._write_virtual_memory_paused_locked = write_mem  # type: ignore[method-assign]
        backend._read_register_paused_locked = lambda reg: registers.get(reg, 0)  # type: ignore[method-assign]

        row = backend._prepare_file_read_context_paused_locked(0x801716FC)

        self.assertTrue(row.get("applied"), row)
        self.assertEqual(struct.unpack_from("<I", memory[ctx], 0)[0], 0x2312)
        self.assertEqual(struct.unpack_from("<I", memory[ctx], 4)[0], 0)

    def test_qemu_first_root_directory_scan_pattern_uses_backing_dirent(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        firmware = bytearray(0x20)
        firmware[:11] = bytes.fromhex("d3a6d3c320202020202020")
        firmware[0x0B] = 0x10
        backend._first_root_dirent_from_backing = lambda: {"firmware": bytes(firmware)}  # type: ignore[method-assign]

        self.assertEqual(
            backend._first_root_directory_scan_pattern_from_backing(),
            b"\\" + bytes.fromhex("d3a6d3c3") + b"\\*.*\x00",
        )

    def test_qemu_first_child_directory_scan_pattern_uses_backing_tree(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        root = bytearray(0x20)
        root[:11] = b"ROOTDIR    "
        root[0x0B] = 0x10
        struct.pack_into("<I", root, 0x14, 0x30F4)
        dot = bytearray(0x20)
        dot[:11] = b".          "
        dot[0x0B] = 0x10
        dotdot = bytearray(0x20)
        dotdot[:11] = b"..         "
        dotdot[0x0B] = 0x10
        lfn = bytearray(0x20)
        lfn[0] = 0x41
        lfn[0x0B] = 0x0F
        child = bytearray(0x20)
        child[:11] = b"CHILD      "
        child[0x0B] = 0x10
        struct.pack_into("<I", child, 0x14, 0x30F5)
        cluster = bytes(dot + dotdot + lfn + child + bytes(512 - 0x80))

        backend._first_root_dirent_from_backing = lambda: {  # type: ignore[method-assign]
            "firmware": bytes(root),
            "cluster": 0x30F4,
        }
        backend._fat16_cluster_data_from_backing = lambda cluster_id: cluster if cluster_id == 0x30F4 else None  # type: ignore[method-assign]

        self.assertEqual(
            backend._first_child_directory_scan_pattern_from_backing(),
            b"\\ROOTDIR\\CHILD\\*.*\x00",
        )

    def test_qemu_first_child_directory_scan_pattern_requires_child_directory(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        root = bytearray(0x20)
        root[:11] = b"ROOTDIR    "
        root[0x0B] = 0x10
        struct.pack_into("<I", root, 0x14, 0x30F4)
        file_entry = bytearray(0x20)
        file_entry[:11] = b"README  TXT"
        file_entry[0x0B] = 0x20
        cluster = bytes(file_entry + bytes(512 - 0x20))

        backend._first_root_dirent_from_backing = lambda: {  # type: ignore[method-assign]
            "firmware": bytes(root),
            "cluster": 0x30F4,
        }
        backend._fat16_cluster_data_from_backing = lambda cluster_id: cluster if cluster_id == 0x30F4 else None  # type: ignore[method-assign]

        self.assertIsNone(backend._first_child_directory_scan_pattern_from_backing())

    def test_qemu_first_file_path_from_backing_descends_directories(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        root_dir = bytearray(0x20)
        root_dir[:11] = b"ROOTDIR    "
        root_dir[0x0B] = 0x10
        struct.pack_into("<H", root_dir, 0x1A, 0x30F4)
        root_file = bytearray(0x20)
        root_file[:11] = b"LATER   BIN"
        root_file[0x0B] = 0x20
        struct.pack_into("<H", root_file, 0x1A, 0x4000)
        child_file = bytearray(0x20)
        child_file[:11] = b"FIRST   BIN"
        child_file[0x0B] = 0x20
        struct.pack_into("<H", child_file, 0x1A, 0x30F5)
        struct.pack_into("<I", child_file, 0x1C, 0x1234)
        root_data = bytes(root_dir + root_file + bytes(512 - 0x40))
        child_data = bytes(child_file + bytes(512 - 0x20))

        backend._root_directory_data_from_backing = lambda: root_data  # type: ignore[method-assign]
        backend._fat16_cluster_data_from_backing = lambda cluster_id: child_data if cluster_id == 0x30F4 else None  # type: ignore[method-assign]

        row = backend._first_file_path_from_backing()

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.get("path"), b"\\ROOTDIR\\FIRST.BIN\x00")
        self.assertEqual(row.get("cluster"), 0x30F5)
        self.assertEqual(row.get("size"), 0x1234)

    def test_qemu_find_system_paths_from_backing_uses_lfn_aliases(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())

        def entry(short: bytes, attr: int, cluster: int, size: int = 0) -> bytes:
            item = bytearray(0x20)
            item[:11] = short
            item[0x0B] = attr
            struct.pack_into("<H", item, 0x1A, cluster)
            struct.pack_into("<I", item, 0x1C, size)
            return bytes(item)

        root = entry(b"SYSTEM     ", 0x10, 2) + bytes(512 - 0x20)
        system_dir = (
            entry(b"DATA       ", 0x10, 3)
            + entry(b"DESKTOP    ", 0x10, 4)
            + bytes(512 - 0x40)
        )
        data_dir = entry(b"SYSTP   CFG", 0x20, 5, 71) + bytes(512 - 0x20)
        desktop_dir = entry(b"C200DT~1DLX", 0x20, 6, 513548) + bytes(512 - 0x20)

        backend._root_directory_data_from_backing = lambda: root  # type: ignore[method-assign]
        backend._fat16_cluster_data_from_backing = lambda cluster_id: {  # type: ignore[method-assign]
            2: system_dir,
            3: data_dir,
            4: desktop_dir,
        }.get(cluster_id)
        backend._fat16_long_name_aliases_by_raw = lambda: {  # type: ignore[method-assign]
            b"SYSTEM     ": ["系统".encode("gbk")],
            b"DATA       ": ["数据".encode("gbk")],
            b"C200DT~1DLX": [b"c200dts1a.dlx"],
        }

        systp = backend._find_path_from_backing("\\系统\\数据\\SysTp.cfg")
        drive_systp = backend._find_path_from_backing("A:\\系统\\数据\\SysTp.cfg")
        desktop = backend._find_path_from_backing("\\系统\\Desktop\\c200dts1a.dlx")

        self.assertIsNotNone(systp)
        assert systp is not None
        self.assertEqual(systp.get("cluster"), 5)
        self.assertEqual(systp.get("size"), 71)
        self.assertEqual(systp.get("name_hex"), b"SYSTP   CFG".hex())
        self.assertIsNotNone(drive_systp)
        assert drive_systp is not None
        self.assertEqual(drive_systp.get("cluster"), 5)
        self.assertIsNotNone(desktop)
        assert desktop is not None
        self.assertEqual(desktop.get("cluster"), 6)
        self.assertEqual(desktop.get("size"), 513548)
        self.assertEqual(desktop.get("name_hex"), b"C200DT~1DLX".hex())

    def test_qemu_system_boot_file_probe_reads_backing_without_firmware_call(self) -> None:
        backend = QemuProcessBackend(QemuSystemConfig())
        entry = {
            "path": "\\系统\\数据\\SysTp.cfg".encode("gbk") + b"\x00",
            "cluster": 5,
            "size": 8,
        }

        backend._system_boot_file_entries_from_backing = lambda: [entry]  # type: ignore[method-assign]
        backend._fat16_cluster_data_from_backing = lambda cluster_id: b"SysTpOK!" if cluster_id == 5 else None  # type: ignore[method-assign]

        row = backend._service_system_boot_file_probes_paused_locked()

        self.assertTrue(row.get("handled"), row)
        self.assertEqual(row.get("read_count"), 1)
        files = row.get("files")
        self.assertIsInstance(files, list)
        assert isinstance(files, list)
        self.assertEqual(files[0].get("event"), "qemu-system-file-backing-read-probe")
        self.assertEqual(files[0].get("read_size"), 8)
        self.assertNotIn("call", files[0])

    def test_qemu_first_path_segment_bounds_skips_drive_prefix(self) -> None:
        path = "A:\\系统\\数据\\SysTp.cfg".encode("gbk")

        start, end = QemuProcessBackend._first_path_segment_bounds(path)

        self.assertEqual(path[start:end], "系统".encode("gbk"))

    def test_frontend_qemu_frontend_input_calibration_releases_last_touch_before_complete(self) -> None:
        state = FrontendState.__new__(FrontendState)
        state.args = argparse.Namespace(frontend_input_calibration=True, boot_mode="c200")
        state.frontend_input_calibration_stage = 0
        state.frontend_input_calibration_last_stage_step = -1
        state.qemu_frontend_input_calibration_last_action_at = 0.0
        state.qemu_frontend_input_calibration_log = []
        backend = _FakeFrontendQemuBackend()

        for _index in range(len(FRONTEND_INPUT_CALIBRATION_TARGETS) * 2 + 1):
            state.qemu_frontend_input_calibration_last_action_at = time.time() - 1.0
            state._apply_frontend_input_calibration_locked(backend)  # type: ignore[arg-type]

        self.assertFalse(backend.completed)
        self.assertEqual(state.frontend_input_calibration_stage, 12)
        self.assertEqual(len(backend.touches), len(FRONTEND_INPUT_CALIBRATION_TARGETS) * 2)
        self.assertEqual(backend.touches[-1], (*FRONTEND_INPUT_CALIBRATION_TARGETS[-1], False))
        self.assertEqual([down for _x, _y, down in backend.touches], [True, False, True, False, True, False, True, False])

    def test_frontend_input_calibration_does_not_complete_on_gui_probe_error(self) -> None:
        class GuiErrorBackend(_FakeFrontendQemuBackend):
            def guest_gui_state_snapshot(self) -> dict[str, object]:
                raise TimeoutError("guest probe timed out")

        state = FrontendState.__new__(FrontendState)
        state.args = argparse.Namespace(frontend_input_calibration=True, boot_mode="c200")
        state.frontend_input_calibration_stage = 0
        state.frontend_input_calibration_last_stage_step = -1
        state.qemu_frontend_input_calibration_last_action_at = 0.0
        state.qemu_frontend_input_calibration_log = []
        backend = GuiErrorBackend()

        for _index in range(len(FRONTEND_INPUT_CALIBRATION_TARGETS) * 2 + 1):
            state.qemu_frontend_input_calibration_last_action_at = time.time() - 1.0
            state._apply_frontend_input_calibration_locked(backend)  # type: ignore[arg-type]

        self.assertEqual(state.frontend_input_calibration_stage, len(FRONTEND_INPUT_CALIBRATION_TARGETS) * 2)
        self.assertEqual(len(backend.touches), len(FRONTEND_INPUT_CALIBRATION_TARGETS) * 2)
        self.assertEqual(state.qemu_frontend_input_calibration_log[-1]["event"], "qemu-frontend-input-calibration-status-deferred")
        self.assertIn("TimeoutError", str(state.qemu_frontend_input_calibration_log[-1].get("error")))

    def test_frontend_input_calibration_accepts_an_already_active_gui(self) -> None:
        class ActiveGuiBackend(_FakeFrontendQemuBackend):
            def snapshot(self) -> dict[str, object]:
                return {"pc": "0x8005bdec", "running": True}

            def guest_gui_state_snapshot(self) -> dict[str, object]:
                return {
                    "active_object_ready": True,
                    "active_object_80474048": "0x809632a4",
                }

        state = FrontendState.__new__(FrontendState)
        state.args = argparse.Namespace(frontend_input_calibration=True, boot_mode="nand")
        state.frontend_input_calibration_stage = 0
        state.frontend_input_calibration_last_stage_step = -1
        state.qemu_frontend_input_calibration_last_action_at = 0.0
        state.qemu_frontend_input_calibration_log = []
        backend = ActiveGuiBackend()

        state._apply_frontend_input_calibration_locked(backend)  # type: ignore[arg-type]

        self.assertEqual(state.frontend_input_calibration_stage, 12)
        self.assertEqual(backend.touches, [])
        self.assertEqual(
            state.qemu_frontend_input_calibration_log[-1]["reason"],
            "main-menu-already-active",
        )

    def test_frontend_input_calibration_waits_for_pc_before_unknown_pc_fallback(self) -> None:
        class UnknownPcBackend(_FakeFrontendQemuBackend):
            def snapshot(self) -> dict[str, object]:
                return {"running": True}

        state = FrontendState.__new__(FrontendState)
        state.args = argparse.Namespace(frontend_input_calibration=True, boot_mode="nand")
        state.frontend_input_calibration_stage = 0
        state.frontend_input_calibration_last_stage_step = -1
        state.qemu_frontend_input_calibration_last_action_at = 0.0
        state.qemu_frontend_input_calibration_log = []
        state.reset_at = time.time()
        backend = UnknownPcBackend()

        state._apply_frontend_input_calibration_locked(backend)  # type: ignore[arg-type]
        self.assertEqual(state.frontend_input_calibration_stage, 0)

        state.reset_at = time.time() - 60.0
        for _index in range(2):
            state.qemu_frontend_input_calibration_last_action_at = time.time() - 1.0
            state._apply_frontend_input_calibration_locked(backend)  # type: ignore[arg-type]

        self.assertEqual(state.frontend_input_calibration_stage, 2)
        self.assertEqual(backend.touches[:2], [
            (*FRONTEND_INPUT_CALIBRATION_TARGETS[0], True),
            (*FRONTEND_INPUT_CALIBRATION_TARGETS[0], False),
        ])

    def test_frontend_full_status_keeps_heavy_traces_explicit(self) -> None:
        state = self._frontend_state_without_qemu(
            argparse.Namespace(
                frontend_input_calibration=False,
                boot_mode="nand",
                nand_image=None,
                orientation="rot180",
                frame_push_min_interval=0.04,
                frame_info_min_interval=1.0,
            )
        )
        backend = _FakeFrontendQemuBackend()
        state.qemu_backend = backend  # type: ignore[assignment]

        full = state.snapshot(detail="full")

        self.assertEqual(full.get("detail"), "full")
        self.assertIn("guest_gui_state", full)
        self.assertIn("guest_display_surface", full)
        self.assertNotIn("guest_storage_trace", full)
        self.assertNotIn("guest_fs_probe_trace", full)
        legacy_hooks = full.get("legacy_python_hooks")
        self.assertIsInstance(legacy_hooks, dict)
        assert isinstance(legacy_hooks, dict)
        self.assertFalse(legacy_hooks.get("enabled"), legacy_hooks)
        self.assertNotIn("resource_cache_enabled", legacy_hooks)
        self.assertNotIn("fast_" + "hooks", full)
        self.assertNotIn("resource_" + "cache16", full)
        self.assertNotIn("qemu_" + "storage_" + "bootstrap_log", full)
        self.assertEqual(backend.trace_calls, [])

        traces = state.snapshot(detail="traces")

        self.assertEqual(traces.get("detail"), "traces")
        self.assertIn("guest_display_surface", traces)
        self.assertIn("guest_storage_trace", traces)
        self.assertIn("guest_fs_probe_trace", traces)
        self.assertEqual(
            backend.trace_calls,
            ["surface", "storage", "msc", "fs_probe", "progress"],
        )

    def test_frontend_websocket_recovers_from_stale_half_open_connection(self) -> None:
        root = Path(__file__).resolve().parents[1]
        frontend = (root / "emu/web/frontend.py").read_text(encoding="utf-8")
        server = (root / "emu/web/frontend_server.py").read_text(encoding="utf-8")

        self.assertIn("const wsIdleReconnectMs = 5000;", frontend)
        self.assertIn("startWsWatchdog();", frontend)
        self.assertIn("wsLastMessageAt = performance.now();", frontend)
        self.assertIn("function commandFetchFallback(msg)", frontend)
        self.assertIn("function wsIsStale()", frontend)
        self.assertIn("function dropWs(reason = 'stale websocket')", frontend)
        self.assertIn("if (wsIsStale()) {", frontend)
        self.assertIn("return commandFetchFallback(msg);", frontend)
        self.assertIn("return wsOpenPromise.then(sock => {", frontend)
        self.assertIn("ws.close(4000, 'stale websocket');", frontend)
        self.assertIn("self.connection.shutdown(socket.SHUT_RDWR)", server)
        self.assertIn("self.connection.close()", server)

    def test_frontend_screen_png_reuses_same_chardev_frame(self) -> None:
        class FrameBackend:
            latest_frame_chardev = (42, time.time() - 1.0, b"\x00\x00" * (240 * 320))

        state = self._frontend_state_without_qemu(
            argparse.Namespace(
                frontend_input_calibration=False,
                boot_mode="nand",
                nand_image=None,
                orientation="rot180",
                frame_push_min_interval=0.08,
                frame_info_min_interval=1.0,
            )
        )
        state.qemu_backend = FrameBackend()  # type: ignore[assignment]
        state._ensure_qemu_started_locked = lambda: state.qemu_backend  # type: ignore[method-assign]

        with mock.patch("emu.web.frontend_state.png_bytes_from_rgb", return_value=b"png-frame") as encode_png:
            first = state.dump_frame()
            second = state.dump_frame()

        self.assertEqual(first, b"png-frame")
        self.assertEqual(second, b"png-frame")
        self.assertEqual(encode_png.call_count, 1)
        self.assertEqual(state.cached_frame_seq, 42)

    def test_frontend_ws_frame_cursor_is_per_connection(self) -> None:
        class FrameBackend:
            latest_frame_chardev = (17, time.time(), b"\x00\x00" * (240 * 320))

        state = FrontendState.__new__(FrontendState)
        state.lock = threading.RLock()
        state.frontend_activity_condition = threading.Condition()
        state.frontend_activity_seq = 0
        state.qemu_backend = FrameBackend()
        state.qemu_last_ws_frame_seq = None
        state.cached_ws_frame_bytes = None
        state.cached_ws_frame_time = 0.0
        state.frame_push_min_interval = 0.0
        state.frame_push_last_time = 0.0
        state.frame_push_throttle_count = 0
        state.frame_push_error_count = 0
        state.frame_push_replace_count = 0
        state.frame_push_last_source_lag_ms = None
        state.frame_push_max_source_lag_ms = 0.0
        state.frame_push_queued_count = 0
        state.last_error = None

        client_a = state.latest_ws_frame_after(None)
        client_b = state.latest_ws_frame_after(None)

        self.assertIsNotNone(client_a)
        self.assertIsNotNone(client_b)
        self.assertEqual(client_a, client_b)
        self.assertEqual(state.frame_push_queued_count, 1)

        state.qemu_backend.latest_frame_chardev = (19, time.time(), b"\x01\x00" * (240 * 320))
        next_a = state.latest_ws_frame_after(17)
        next_b = state.latest_ws_frame_after(17)

        self.assertEqual(next_a, next_b)
        self.assertEqual(next_a[0] if next_a else None, 19)
        self.assertEqual(state.frame_push_queued_count, 2)
        self.assertEqual(state.frame_push_replace_count, 1)

    def test_frontend_audio_queue_packages_pcm_and_drops_stale_packets(self) -> None:
        state = self._frontend_state_without_qemu(
            argparse.Namespace(
                frontend_input_calibration=False,
                boot_mode="nand",
                nand_image=None,
                orientation="rot180",
                frame_push_min_interval=1.0 / 30.0,
                frame_info_min_interval=1.0,
            )
        )
        pcm = struct.pack("<4h", 100, -100, 200, -200)
        state._on_qemu_audio_ready(7, 44100, 2, pcm)

        packets = state.audio_packets_after(0)
        self.assertEqual(len(packets), 1)
        delivery_seq, packet = packets[0]
        magic, qemu_seq, rate, channels, bits, payload_bytes = WS_AUDIO_HEADER.unpack(
            packet[: WS_AUDIO_HEADER.size]
        )
        self.assertEqual(magic, WS_AUDIO_MAGIC)
        self.assertEqual((qemu_seq, rate, channels, bits), (7, 44100, 2, 16))
        self.assertEqual(payload_bytes, len(pcm))
        self.assertEqual(packet[WS_AUDIO_HEADER.size :], pcm)

        for seq in range(8, 28):
            state._on_qemu_audio_ready(seq, 44100, 2, pcm)
        latest = state.audio_packets_after(delivery_seq, max_packets=4)
        self.assertEqual(len(latest), 4)
        self.assertGreater(state.audio_dropped_packets, 0)

    def test_web_qemu_audio_defaults_to_muted_host_backend(self) -> None:
        machine_options, extra_args = web_qemu_audio_options(
            ("bootrom-nand=on",),
            (),
            host_audio=False,
        )

        self.assertIn("audiodev=bbk9588-web-none", machine_options)
        self.assertEqual(
            extra_args[-2:],
            ("-audiodev", "driver=none,id=bbk9588-web-none"),
        )

        self.assertEqual(
            web_qemu_audio_options(("bootrom-nand=on",), (), host_audio=True),
            (("bootrom-nand=on",), ()),
        )
        custom = web_qemu_audio_options(
            ("audiodev=wavcap",),
            ("-audiodev", "driver=wav,id=wavcap,path=capture.wav"),
            host_audio=False,
        )
        self.assertEqual(
            custom,
            (
                ("audiodev=wavcap",),
                ("-audiodev", "driver=wav,id=wavcap,path=capture.wav"),
            ),
        )

    def test_web_qemu_timing_defaults_to_icount_and_single_thread_tcg(self) -> None:
        accel, extra_args = web_qemu_timing_options(
            "tcg,thread=multi,tb-size=256",
            (),
            icount=DEFAULT_WEB_QEMU_ICOUNT,
        )

        self.assertEqual(accel, "tcg,thread=single,tb-size=256")
        self.assertEqual(extra_args, ("-icount", DEFAULT_WEB_QEMU_ICOUNT))
        self.assertEqual(
            web_qemu_timing_options(
                "tcg,thread=multi,tb-size=256",
                ("-icount", "shift=7"),
                icount=DEFAULT_WEB_QEMU_ICOUNT,
            ),
            ("tcg,thread=single,tb-size=256", ("-icount", "shift=7")),
        )
        self.assertEqual(
            web_qemu_timing_options(
                "tcg,thread=multi,tb-size=256",
                (),
                icount=None,
            ),
            ("tcg,thread=multi,tb-size=256", ()),
        )

    def test_frontend_performance_metrics_compute_web_and_png_rates(self) -> None:
        state = self._frontend_state_without_qemu(
            argparse.Namespace(
                frontend_input_calibration=False,
                boot_mode="nand",
                nand_image=None,
                orientation="rot180",
                frame_push_min_interval=0.08,
                frame_info_min_interval=1.0,
            )
        )
        state.ws_frame_sent_count = 5
        state.frame_push_queued_count = 5
        state.screen_png_count = 2
        state._frontend_performance_snapshot_locked(100.0, 10.0)
        state.ws_frame_sent_count = 8
        state.frame_push_queued_count = 8
        state.screen_png_count = 4

        row = state._frontend_performance_snapshot_locked(101.0, 11.0)

        self.assertEqual(row.get("websocket_fps"), 3.0)
        self.assertEqual(row.get("screen_png_fps"), 2.0)
        self.assertEqual(row.get("websocket_average_fps"), 0.73)
        self.assertEqual(row.get("websocket_transport_fps"), 3.0)
        self.assertEqual(row.get("screen_png_count"), 4)

    def test_frontend_status_displays_performance_metrics(self) -> None:
        root = Path(__file__).resolve().parents[1]
        frontend = (root / "emu/web/frontend.py").read_text(encoding="utf-8")

        self.assertIn("function firstNumber(...values)", frontend)
        self.assertIn("function formatRate(value, unit, fallback = 'n/a')", frontend)
        self.assertIn("function formatPercent(value, fallback = 'n/a')", frontend)
        self.assertIn("function formatAudioMode(audio)", frontend)
        self.assertIn("const minKeyHoldMs = 100;", frontend)
        self.assertIn("function beginKeyButton(btn)", frontend)
        self.assertIn("function endKeyButton(btn, phase)", frontend)
        self.assertIn(
            "['qemu fps', formatRate(firstNumber(qemuPerf.frame_chardev_fps, qemuPerf.frame_chardev_average_fps), 'fps')]",
            frontend,
        )
        self.assertIn("['web fps', formatRate(frontendPerf.websocket_fps, 'fps')]", frontend)
        self.assertIn("['web tx', formatRate(frontendPerf.websocket_transport_fps, 'fps')]", frontend)
        self.assertIn("['ws clients', s.frame_push?.ws_connections ?? 0]", frontend)
        self.assertIn("['png fps', formatRate(frontendPerf.screen_png_fps, 'fps')]", frontend)
        self.assertIn(
            "['qemu cpu', formatPercent(firstNumber(qemuPerf.qemu_cpu_one_core_percent, qemuPerf.qemu_cpu_host_percent))]",
            frontend,
        )
        self.assertIn("['guest ips', formatGuestIps(qemuPerf)]", frontend)
        self.assertIn("['audio', formatAudioMode(qemuAudio)]", frontend)
        self.assertIn(
            "['audio fifo', `tx ${qemuAudio.tx_fifo_level ?? 0} / rx ${qemuAudio.rx_fifo_level ?? 0}`]",
            frontend,
        )
        self.assertIn(
            "['audio dma', `tx ${formatCounter(qemuAudio.tx_dma_samples)} / rx ${formatCounter(qemuAudio.rx_dma_samples)}`]",
            frontend,
        )
        self.assertIn(
            "['audio xrun', `${formatCounter(qemuAudio.underruns)} / ${formatCounter(qemuAudio.overruns)}`]",
            frontend,
        )

    def test_frontend_layout_rotation_and_custom_keymap_controls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        frontend = (root / "emu/web/frontend.py").read_text(encoding="utf-8")
        frontend_server = (root / "emu/web/frontend_server.py").read_text(encoding="utf-8")

        self.assertIn('class="workspace"', frontend)
        self.assertIn('class="control-sidebar"', frontend)
        self.assertIn('class="emulator-stage"', frontend)
        self.assertIn('class="status-sidebar"', frontend)
        self.assertIn('id="openControlsDrawer"', frontend)
        self.assertIn('id="openStatusDrawer"', frontend)
        self.assertIn('id="drawerBackdrop"', frontend)
        self.assertIn("const mobileLayoutQuery = window.matchMedia('(max-width: 760px)');", frontend)
        self.assertIn('id="settingsDialog" class="settings-dialog"', frontend)
        self.assertIn('id="keymapSettingsTab"', frontend)
        self.assertIn('id="touchSettingsTab"', frontend)
        self.assertIn('id="powerSettingsTab"', frontend)
        self.assertIn('id="usbPowerConnected" type="checkbox" checked', frontend)
        self.assertIn('id="keymapSettingsPane" class="settings-pane keymap-panel"', frontend)
        self.assertIn("if (settingsDialogEl.open) return;", frontend)
        self.assertIn("if (!settingsDialogEl.open) settingsDialogEl.showModal();", frontend)
        self.assertIn(".control-sidebar.drawer-open, .status-sidebar.drawer-open", frontend)
        self.assertIn("controlsDrawerEl.inert = mobile && !controlsOpen;", frontend)
        self.assertIn("if (ev.code === 'Escape' && activeDrawer !== null)", frontend)
        self.assertLess(frontend.index('id="screenWrap"'), frontend.index('class="device-keypad"'))
        self.assertIn(".kv-value { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }", frontend)
        self.assertIn("html, body { width: 100%; height: 100%; overflow: hidden; }", frontend)
        self.assertIn(".workspace { height: calc(100vh - 56px); min-height: 0;", frontend)
        self.assertIn("#filesTab { flex: 1; display: flex; flex-direction: column; overflow: hidden; }", frontend)
        self.assertIn("#filesTab[hidden] { display: none; }", frontend)
        self.assertIn(".file-list { flex: 1; min-height: 0;", frontend)
        self.assertIn("overflow-y: auto; overscroll-behavior: contain;", frontend)
        self.assertIn("statusEl.replaceChildren(...statusNodes);", frontend)
        self.assertNotIn("最近事件", frontend)
        self.assertNotIn("const eventsEl", frontend)
        self.assertIn('id="imageStatus" class="image-status grow">bbk9588_nand.bin', frontend)
        self.assertNotIn("运行一片", frontend)
        self.assertNotIn("连续运行", frontend)
        self.assertNotIn("每片指令", frontend)
        self.assertNotIn('id="nandImageSelect"', frontend)
        self.assertNotIn('id="nandImagePath"', frontend)
        self.assertNotIn('id="applyNandImage"', frontend)
        self.assertNotIn("refreshImages", frontend)
        self.assertNotIn("wsSend({op:'step'", frontend)
        self.assertNotIn("wsSend({op:'run-start'", frontend)
        self.assertIn('id="statusTabButton"', frontend)
        self.assertIn('id="filesTabButton"', frontend)
        self.assertIn('id="fileMkdir"', frontend)
        self.assertIn('id="fileImport"', frontend)
        self.assertIn("/api/files/export?path=", frontend)
        self.assertIn("/api/files/rename", frontend)
        self.assertIn("/api/files/delete", frontend)
        self.assertIn('id="rotateLeft"', frontend)
        self.assertIn('id="rotateRight"', frontend)
        self.assertIn('id="openSettings"', frontend)
        self.assertLess(frontend.index('id="openSettings"'), frontend.index('id="rotateLeft"'))
        self.assertLess(frontend.index('id="settingsDialog"'), frontend.index('data-binding-code="4"'))
        device_controls = frontend[
            frontend.index('<div class="device-controls">'):frontend.index('</main>')
        ]
        self.assertNotIn('data-binding-code=', device_controls)
        self.assertIn('id="toggleFullscreen"', frontend)
        self.assertIn('id="toggleAudio"', frontend)
        self.assertIn('class="audio-icon" aria-hidden="true"', frontend)
        self.assertIn("toggleAudioEl.classList.toggle('is-muted', !audioEnabled);", frontend)
        self.assertNotIn("toggleAudioEl.textContent", frontend)
        self.assertNotIn("🔊", frontend)
        self.assertNotIn("🔇", frontend)
        self.assertIn('id="powerKey"', frontend)
        self.assertIn('data-key="11" data-name="power"', frontend)
        self.assertIn('id="stop" class="secondary">安全关机</button>', frontend)
        self.assertIn('id="forceStop" class="warn">强制停止</button>', frontend)
        self.assertIn("runLifecycleCommand('stop', '正在等待固件安全关机')", frontend)
        self.assertIn("runLifecycleCommand('reset', '正在安全关机并重新启动')", frontend)
        self.assertIn("setOperationStatus(status.lifecycle_notice || '')", frontend)
        self.assertIn("runLifecycleCommand('force-stop', '正在强制停止')", frontend)
        self.assertIn("wsSend({op:'set-usb-power', connected:usbPowerConnectedEl.checked})", frontend)
        self.assertIn("强制停止可能中断 NAND 写入并损坏文件系统", frontend)
        self.assertNotIn("wsSend({op:'stop'})", frontend)
        self.assertIn('id="screenWrap" class="screen-wrap"', frontend)
        self.assertIn('id="screenStopOverlay"', frontend)
        self.assertIn('id="restartFromOverlay"', frontend)
        self.assertIn("detail.code === 'guest-shutdown'", frontend)
        self.assertIn("系统固件已通过 RTC 电源管理完成关机。", frontend)
        self.assertIn('id="exitFullscreen"', frontend)
        self.assertIn("wsSend({op:'set-orientation', orientation:next})", frontend)
        self.assertIn("screenWrapEl.requestFullscreen || screenWrapEl.webkitRequestFullscreen", frontend)
        self.assertIn("document.addEventListener('fullscreenchange', updateFullscreenScreenSize)", frontend)
        self.assertIn("screenResizeObserver.observe(screenWrapEl)", frontend)
        self.assertIn("screenWrapEl.clientHeight - verticalPadding", frontend)
        self.assertIn("Math.min(availableWidth / screen.width, availableHeight / screen.height)", frontend)
        self.assertIn(".screen-wrap:fullscreen #screen", frontend)
        self.assertIn("const keyBindingStorageKey = 'bbk9588.keyBindings.v1';", frontend)
        self.assertIn("4:'KeyW'", frontend)
        self.assertIn("5:'KeyS'", frontend)
        self.assertIn("6:'KeyA'", frontend)
        self.assertIn("7:'KeyD'", frontend)
        self.assertIn("9:'KeyK'", frontend)
        self.assertIn("10:'KeyJ'", frontend)
        self.assertIn("const gamepadBindingStorageKey = 'bbk9588.gamepadBindings.v1';", frontend)
        self.assertIn("const defaultGamepadBindings = Object.freeze({\n  4:'button:12'", frontend)
        self.assertIn("5:'button:13'", frontend)
        self.assertIn("6:'button:14'", frontend)
        self.assertIn("7:'button:15'", frontend)
        self.assertIn("9:'button:1'", frontend)
        self.assertIn("10:'button:0'", frontend)
        self.assertIn("const legacyDefaultKeyBindings = Object.freeze({9:'Escape', 10:'Space'});", frontend)
        self.assertIn("const legacyDefaultGamepadBindings = Object.freeze({\n  4:'axis:1:-'", frontend)
        self.assertIn("if (migrated) localStorage.setItem(keyBindingStorageKey", frontend)
        self.assertIn("if (migrated) localStorage.setItem(gamepadBindingStorageKey", frontend)
        self.assertIn("'1:-':'左摇杆↑'", frontend)
        self.assertIn("navigator.getGamepads", frontend)
        self.assertIn("capturedGamepadBinding(gamepad, previous)", frontend)
        self.assertIn("gamepadBindingActive(gamepad, binding, activeGamepadInputs.has(sourceId))", frontend)
        self.assertIn("const gamepadPressThreshold = 0.55;", frontend)
        self.assertIn("const gamepadReleaseThreshold = 0.35;", frontend)
        self.assertIn("const gamepadCaptureThreshold = 0.5;", frontend)
        self.assertIn("function gamepadHasActivity(gamepad)", frontend)
        self.assertIn("if (detectedGamepads.some(gamepadHasActivity)) gamepadInputFocused = true;", frontend)
        self.assertIn(
            "Math.abs(previousValue) < gamepadCaptureThreshold",
            frontend,
        )
        self.assertNotIn(
            "Math.abs(previousValue) < gamepadReleaseThreshold",
            frontend,
        )
        self.assertIn("const keyLeaseHeartbeatMs = 250;", frontend)
        self.assertIn("function refreshActiveKeyLeases()", frontend)
        self.assertIn("input_session:inputSessionId", frontend)
        self.assertIn("setInterval(refreshActiveKeyLeases, keyLeaseHeartbeatMs);", frontend)
        self.assertIn("releaseButtonInputs('button-pagehide');", frontend)
        self.assertIn("window.addEventListener('gamepaddisconnected'", frontend)
        self.assertIn("captureSuppressedGamepadInputs", frontend)
        self.assertIn('id="gamepadStatus"', frontend)
        self.assertIn("function readGamepads()", frontend)
        self.assertIn("window.addEventListener('pointerdown', () => {", frontend)
        self.assertIn("const AudioContextClass = window.AudioContext || window.webkitAudioContext;", frontend)
        self.assertIn("audioContext = new AudioContextClass();", frontend)
        self.assertIn("function primeAudioContext(context)", frontend)
        self.assertIn("source.buffer = context.createBuffer(1, 1", frontend)
        self.assertIn("audioContext?.state === 'interrupted'", frontend)
        self.assertIn("context.resume().then(() => {", frontend)
        self.assertIn("audioClockStalled", frontend)
        self.assertIn("new WebSocket(`${protocol}//${location.host}/audio`)", frontend)
        self.assertIn("window.addEventListener('touchend', unlockAudio", frontend)
        self.assertIn('elif parsed.path == "/audio":', frontend_server)
        self.assertIn("const touchMoveBackpressureMs = 1000 / 30;", frontend)
        self.assertIn("reply:false", frontend)
        self.assertIn('self.send_header("Permissions-Policy", "gamepad=(self)")', frontend_server)
        self.assertIn('if msg.get("reply") is False:', frontend_server)
        self.assertIn(".key-cancel { grid-column: 1; grid-row: 1 / 3; }", frontend)
        self.assertIn(".key-up { grid-column: 3; grid-row: 1; }", frontend)
        self.assertIn(".key-ok { grid-column: 5; grid-row: 1 / 3; }", frontend)
        self.assertIn("touch-action: manipulation; user-select: none; -webkit-user-select: none;", frontend)
        self.assertIn(".device-key > * { pointer-events: none;", frontend)
        self.assertIn("btn.addEventListener('contextmenu', ev => ev.preventDefault());", frontend)
        self.assertIn("btn.addEventListener('selectstart', ev => ev.preventDefault());", frontend)
        self.assertIn("btn.addEventListener('dblclick', ev => ev.preventDefault());", frontend)
        self.assertEqual(frontend.count('class="device-key '), 6)
        self.assertEqual(frontend.count('data-binding-code="'), 6)

    def test_release_workflow_includes_version_change_notes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn(
            'git tag --list --merged $target --sort=-version:refname "v[0-9]*" "emu-v*"',
            workflow,
        )
        self.assertIn("$tagExitCode = $LASTEXITCODE", workflow)
        self.assertLess(workflow.index("$tagExitCode = $LASTEXITCODE"), workflow.index("$previousTag ="))
        self.assertIn('git log --reverse --format="%H%x09%s" $range', workflow)
        self.assertIn("## 本版本更新", workflow)
        self.assertIn("CHANGELOG.md", workflow)
        self.assertIn("## [v0.1.2]", changelog)

    def test_frontend_touch_without_reply_skips_status_snapshot(self) -> None:
        state = self._frontend_state_without_qemu(
            argparse.Namespace(
                frontend_input_calibration=False,
                boot_mode="nand",
                nand_image=None,
                orientation="rot180",
                frame_push_min_interval=1.0 / 30.0,
                frame_info_min_interval=1.0,
            )
        )
        backend = _FakeFrontendQemuBackend()
        state.qemu_backend = backend  # type: ignore[assignment]
        publisher = mock.Mock(side_effect=AssertionError("input fast path built a status snapshot"))
        state._publish_snapshot_locked = publisher  # type: ignore[method-assign]

        result = state.command(
            {
                "op": "touch",
                "x": 120,
                "y": 160,
                "down": True,
                "phase": "move",
                "reply": False,
                "run": True,
            }
        )

        self.assertEqual(backend.touches, [(120, 160, True)])
        self.assertEqual(set(result), {"input_accepted", "qemu_input_result"})
        self.assertTrue(result["input_accepted"])
        publisher.assert_not_called()

    def test_frontend_orientation_command_preserves_raw_frame(self) -> None:
        state = FrontendState.__new__(FrontendState)
        state.args = argparse.Namespace(orientation="rot180")
        state.lock = threading.RLock()
        state.qemu_backend = None
        state.cached_frame_bytes = b"png"
        state.cached_frame_seq = 7
        state.cached_frame_time = 1.0
        state.cached_ws_frame_bytes = b"raw-rgb565-frame"
        state.last_frame = None
        state._publish_snapshot_locked = lambda: None  # type: ignore[method-assign]
        state.snapshot = lambda: {"orientation": state.args.orientation}  # type: ignore[method-assign]

        row = state.command({"op": "set-orientation", "orientation": "cw90"})

        self.assertEqual(row.get("orientation"), "cw90")
        self.assertTrue(row.get("orientation_changed"), row)
        self.assertIsNone(state.cached_frame_bytes)
        self.assertIsNone(state.cached_frame_seq)
        self.assertEqual(state.cached_frame_time, 0.0)
        self.assertEqual(state.cached_ws_frame_bytes, b"raw-rgb565-frame")
        invalid = state.command({"op": "set-orientation", "orientation": "diagonal"})
        self.assertIn("unsupported orientation", str(invalid.get("error")))

    def test_rotated_frontend_touch_coordinates_follow_visible_screen(self) -> None:
        self.assertEqual(display_to_touch_point(0, 0, 240, 320, "rot180"), (0, 0))
        self.assertEqual(display_to_touch_point(0, 0, 240, 320, "raw"), (239, 319))
        self.assertEqual(display_to_touch_point(0, 0, 320, 240, "cw90"), (239, 0))
        self.assertEqual(display_to_touch_point(0, 0, 320, 240, "ccw90"), (0, 319))

    def test_frontend_qemu_storage_service_error_uses_legacy_hook_terms(self) -> None:
        state = self._frontend_state_without_qemu(
            argparse.Namespace(
                nand_image=None,
                image=None,
                payload=None,
                boot_mode="nand",
                orientation="rot180",
                qemu=DEFAULT_QEMU_EXECUTABLE,
                qemu_machine=DEFAULT_QEMU_MACHINE,
                qemu_cpu=DEFAULT_QEMU_CPU,
                qemu_accel="tcg",
                qemu_gdb="none",
                qemu_timeout=5.0,
                qemu_machine_option=[],
                qemu_extra_arg=[],
                qemu_firmware_patch=None,
                ram_mb=160,
                frontend_input_calibration=False,
                frame_push_min_interval=0.08,
                frame_info_min_interval=1.0,
            )
        )

        result = state.command({"op": "qemu-storage-service"})

        self.assertIn("legacy Python/GDB storage hooks", str(result.get("error")))
        self.assertNotIn("fastpath", str(result.get("error")).lower())

    def test_frontend_intrusive_diagnostics_are_disabled_by_default(self) -> None:
        fail = self.fail

        class _Backend:
            def watch_guest_write_once(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                fail("unexpected watch_guest_write_once call")

            def trace_guest_breakpoints_once(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                fail("unexpected trace_guest_breakpoints_once call")

        state = FrontendState.__new__(FrontendState)
        state.args = argparse.Namespace()
        state.qemu_backend = _Backend()

        watch = state.command({"op": "qemu-watch-write", "addr": "0x80000000", "size": 4})
        trace = state.command({"op": "qemu-trace-breakpoints", "pcs": ["0x80000000"]})

        self.assertIn("disabled by default", str(watch.get("error")))
        self.assertIn("--allow-gdb-diagnostics", str(watch.get("error")))
        self.assertIn("disabled by default", str(trace.get("error")))
        self.assertIn("--allow-gdb-diagnostics", str(trace.get("error")))

    def test_frontend_intrusive_diagnostics_require_explicit_flag(self) -> None:
        class _Backend:
            def watch_guest_write_once(self, *args: object, **_kwargs: object) -> dict[str, object]:
                return {"called": "watch", "addr": args[0], "size": args[1]}

            def trace_guest_breakpoints_once(self, pcs: object, **kwargs: object) -> dict[str, object]:
                return {"called": "trace", "pcs": pcs, "max_hits": kwargs.get("max_hits")}

        state = FrontendState.__new__(FrontendState)
        state.args = argparse.Namespace(allow_gdb_diagnostics=True)
        state.qemu_backend = _Backend()

        watch = state.command({"op": "qemu-watch-write", "addr": "0x80000000", "size": 4, "timeout": 0.1})
        trace = state.command({"op": "qemu-trace-breakpoints", "pcs": ["0x80000004"], "max_hits": 2})

        self.assertEqual(watch.get("called"), "watch")
        self.assertEqual(watch.get("addr"), 0x80000000)
        self.assertEqual(watch.get("size"), 4)
        self.assertEqual(trace.get("called"), "trace")
        self.assertEqual(trace.get("pcs"), (0x80000004,))
        self.assertEqual(trace.get("max_hits"), 2)

    def test_frontend_nand_image_catalog_marks_selected_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nand = Path(tmp) / "custom_nand.bin"
            nand.write_bytes(b"\xff" * 4096)
            state = FrontendState.__new__(FrontendState)
            state.args = argparse.Namespace(nand_image=nand)

            catalog = state.nand_image_catalog()

        self.assertEqual(catalog["current_path"], str(nand.resolve()))
        images = catalog.get("images")
        self.assertIsInstance(images, list)
        assert isinstance(images, list)
        selected = [item for item in images if item.get("current")]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].get("path"), str(nand.resolve()))
        self.assertEqual(selected[0].get("size"), 4096)

    def test_default_active_nand_lives_outside_build_directory(self) -> None:
        self.assertEqual(
            DEFAULT_QEMU_NAND_IMAGE,
            Path("runtime") / "bbk9588_nand.bin",
        )

    def test_explicit_nand_import_runs_inside_frontend_nand_lease(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "packaging" / "start-web.ps1"
        ).read_text(encoding="utf-8")
        importer = (
            Path(__file__).resolve().parents[1] / "emu" / "qemu" / "nand_source.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"--nand-import-source"', script)
        self.assertNotIn("function Import-NandSource", script)
        self.assertNotIn("Move-Item -LiteralPath $temporaryImage", script)
        self.assertIn('"qemu_nand_persistent"', importer)
        self.assertNotIn("qemu_nand_runs", script)

    def test_release_launcher_waits_for_frontend_before_opening_browser(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "packaging" / "start-web.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('$StatusUrl = "${url}api/status"', script)
        self.assertIn("$BrowserReadyJob = Start-Job -ScriptBlock", script)
        self.assertIn("Invoke-WebRequest", script)
        self.assertRegex(
            script,
            r"if \(\[int\]\$response\.StatusCode -eq 200\) \{\s+"
            r"Start-Process \$BrowserUrl",
        )
        self.assertNotIn("Start-Process $url", script)

    def test_frontend_qemu_backend_status_and_stop(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        port = _find_free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "emu/app.py",
                "--backend",
                "qemu",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--boot-mode",
                "c200",
                "--quiet",
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            deadline = time.time() + 20
            status: dict[str, object] | None = None
            while time.time() < deadline:
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=1)
                    self.fail(f"frontend exited early rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}")
                try:
                    status = _http_json(port, "GET", "/api/status?detail=full")
                    qemu = status.get("qemu") if isinstance(status.get("qemu"), dict) else {}
                    if isinstance(status.get("pc"), str) and isinstance(qemu.get("pc"), str):
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            self.assertIsNotNone(status)
            assert status is not None
            self.assertEqual(status.get("backend"), "qemu")
            self.assertIsInstance(status.get("pc"), str)
            self.assertIsInstance(status.get("qemu_pc_classification"), dict)
            self.assertTrue(status.get("qemu_pc_region"))
            self.assertIn("qemu", status)
            qemu = status["qemu"]
            self.assertIsInstance(qemu, dict)
            self.assertTrue(qemu.get("running"), qemu)
            self.assertIsInstance(qemu.get("pc"), str)
            self.assertIsInstance(qemu.get("pc_classification"), dict)
            self.assertTrue(qemu.get("gdb_connected"), qemu)
            sample = qemu.get("register_sample")
            self.assertIsInstance(sample, dict)
            assert isinstance(sample, dict)
            self.assertNotEqual(sample.get("pc"), "0x80004000")
            screen_status, png = _http_bytes(port, "/screen.png")
            self.assertEqual(screen_status, 200)
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            after_screen = _http_json(port, "GET", "/api/status?detail=full")
            framebuffer = after_screen.get("framebuffer")
            self.assertIsInstance(framebuffer, dict)
            assert isinstance(framebuffer, dict)
            self.assertEqual(framebuffer.get("backend"), "qemu")
            self.assertEqual(framebuffer.get("source"), "qemu-frame-chardev")
            after_qemu = after_screen.get("qemu")
            self.assertIsInstance(after_qemu, dict)
            assert isinstance(after_qemu, dict)
            self.assertGreaterEqual(int(after_qemu.get("frame_chardev_count") or 0), 1)
            self.assertGreaterEqual(int(after_qemu.get("gdb_read_count") or 0), 1)
            event_queue = after_screen.get("event_queue")
            self.assertIsInstance(event_queue, dict)
            assert isinstance(event_queue, dict)
            self.assertEqual(event_queue.get("global_addr"), "0x80473f6c")
            self.assertIn("global_value", event_queue)
            display_queue = after_screen.get("display_event_queue")
            self.assertIsInstance(display_queue, dict)
            assert isinstance(display_queue, dict)
            self.assertEqual(display_queue.get("queue_va"), "0x80825840")
            gui_state = after_screen.get("guest_gui_state")
            self.assertIsInstance(gui_state, dict)
            assert isinstance(gui_state, dict)
            self.assertIn("active_object_80474048", gui_state)
            self.assertIn("gui_busy_count_80825800", gui_state)
            self.assertIn("gui_busy_count_80825820", gui_state)
            self.assertIn("touch_mode_flag_8048daf4", gui_state)
            self.assertIn("fs_volume_count_80474254", gui_state)
            self.assertIn("resource_cache_enabled_804bf434", gui_state)
            key_reply = _http_json(port, "POST", "/api/key?code=7&down=1")
            self.assertEqual(key_reply.get("backend"), "qemu")
            self.assertTrue(key_reply.get("input_accepted"), key_reply.get("qemu_input_result"))
            input_result = key_reply.get("qemu_input_result")
            self.assertIsInstance(input_result, dict)
            assert isinstance(input_result, dict)
            self.assertTrue(input_result.get("applied"), input_result)
            key_release = _http_json(port, "POST", "/api/key?code=7&down=0")
            self.assertEqual(key_release.get("backend"), "qemu")
            self.assertTrue(key_release.get("input_accepted"), key_release.get("qemu_input_result"))
            release_result = key_release.get("qemu_input_result")
            self.assertIsInstance(release_result, dict)
            assert isinstance(release_result, dict)
            self.assertFalse(release_result.get("down"), release_result)
            self.assertEqual(release_result.get("source"), "qemu-c-machine-chardev")
            touch_reply = _http_json(port, "POST", "/api/touch?x=120&y=160&down=1")
            self.assertEqual(touch_reply.get("backend"), "qemu")
            self.assertTrue(touch_reply.get("input_accepted"), touch_reply.get("qemu_input_result"))
            touch_result = touch_reply.get("qemu_input_result")
            self.assertIsInstance(touch_result, dict)
            assert isinstance(touch_result, dict)
            self.assertTrue(touch_result.get("applied"), touch_result)
            self.assertEqual(touch_result.get("source"), "qemu-c-machine-chardev")
            self.assertNotIn("touch_x_addr", touch_result)
            self.assertNotIn("firmware_touch_x_addr", touch_result)
            gui_handler = touch_result.get("gui_handler")
            self.assertIsInstance(gui_handler, dict)
            assert isinstance(gui_handler, dict)
            self.assertTrue(gui_handler.get("skipped"), gui_handler)
            self.assertEqual(gui_handler.get("source"), "qemu-c-machine")
            stopped = _http_json(port, "POST", "/api/stop")
            self.assertEqual(stopped.get("backend"), "qemu")
            self.assertFalse(stopped.get("running"))
        finally:
            try:
                _http_json(port, "POST", "/api/shutdown")
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            try:
                proc.communicate(timeout=1)
            except Exception:
                pass

    def test_qemu_gdb_virtual_memory_bridge_reads_boot_payload(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        config = build_bbk_qemu_config(
            boot_mode="c200",
            gdb="auto",
            bbk_machine_options=("hibernate-poweroff=off",),
            timeout_seconds=1.5,
        )
        backend = QemuProcessBackend(config)
        try:
            backend.start()
            self.assertTrue(backend.running())
            assert config.boot_payload is not None
            expected = config.boot_payload.path.read_bytes()[:4]
            data = backend.read_virtual_memory(DEFAULT_C200_BASE, 4)
            self.assertEqual(data, expected)
            snap = backend.snapshot()
            self.assertGreaterEqual(int(snap.get("gdb_read_count") or 0), 1)
            self.assertIsNone(snap.get("last_gdb_error"))
        finally:
            backend.stop()

    def test_qemu_gdb_register_bridge_reads_and_writes_registers(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        config = build_bbk_qemu_config(
            boot_mode="c200",
            timeout_seconds=1.5,
            bbk_machine_options=("cpu-irq-output=off",),
        )
        backend = QemuProcessBackend(config)
        try:
            backend.start()
            self.assertTrue(backend.running())
            time.sleep(0.5)
            pc = backend.read_pc()
            self.assertTrue(0x80000000 <= pc < 0x81000000, f"pc=0x{pc:08x}")
            original_v0 = backend.read_register(2)
            try:
                checked = backend.write_registers_checked({2: 0x12345678, 37: pc})
                self.assertEqual(checked["2"], "0x12345678")
                self.assertEqual(checked["37"], f"0x{pc:08x}")
            finally:
                backend.write_register(2, original_v0)
            snap = backend.snapshot()
            self.assertGreaterEqual(int(snap.get("gdb_register_read_count") or 0), 3)
            self.assertGreaterEqual(int(snap.get("gdb_register_write_count") or 0), 2)
        finally:
            backend.stop()

    def test_qemu_gdb_stepped_guest_call_invokes_tick_getter(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        config = build_bbk_qemu_config(boot_mode="c200", timeout_seconds=1.5)
        backend = QemuProcessBackend(config)
        try:
            backend.start()
            self.assertTrue(backend.running())
            time.sleep(0.5)
            expected = int.from_bytes(backend.read_virtual_memory(0x80474058, 4), "little")
            result = backend.call_guest_function_stepped(0x800DE144, return_pc=DEFAULT_C200_BASE, max_steps=4)
            self.assertTrue(result.get("returned"), result)
            self.assertEqual(result.get("final_pc"), f"0x{DEFAULT_C200_BASE:08x}")
            returned_tick = int(str(result.get("v0")), 16)
            self.assertGreaterEqual(returned_tick, expected)
            snap = backend.snapshot()
            self.assertGreaterEqual(int(snap.get("gdb_step_count") or 0), 1)
            self.assertGreaterEqual(int(snap.get("guest_call_count") or 0), 1)
        finally:
            backend.stop()

    def test_qemu_gdb_guest_queue_snapshot_reads_global_pointer(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        config = build_bbk_qemu_config(boot_mode="c200", timeout_seconds=1.5)
        backend = QemuProcessBackend(config)
        try:
            backend.start()
            self.assertTrue(backend.running())
            snapshot = backend.guest_queue_snapshot(0x80473F6C)
            self.assertEqual(snapshot.get("global_addr"), "0x80473f6c")
            self.assertIn("global_value", snapshot)
            self.assertNotIn("error", snapshot)
            qemu = backend.snapshot()
            self.assertGreaterEqual(int(qemu.get("gdb_read_count") or 0), 1)
        finally:
            backend.stop()

    def test_qemu_gdb_gui_state_snapshot_reads_active_object_globals(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        config = build_bbk_qemu_config(boot_mode="c200", timeout_seconds=1.5)
        backend = QemuProcessBackend(config)
        try:
            backend.start()
            self.assertTrue(backend.running())
            time.sleep(1.0)
            gui = backend.guest_gui_state_snapshot()
            self.assertIn("active_object_80474048", gui)
            self.assertIn("touch_mode_flag_8048daf4", gui)
            self.assertIn("active_object_ready", gui)
            qemu = backend.snapshot()
            self.assertGreaterEqual(int(qemu.get("gdb_read_count") or 0), 1)
        finally:
            backend.stop()

    def test_qemu_gdb_gui_key_bridge_applies_key_state(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        config = build_bbk_qemu_config(boot_mode="c200", timeout_seconds=1.5)
        backend = QemuProcessBackend(config)
        try:
            backend.start()
            self.assertTrue(backend.running())
            time.sleep(1.0)
            result = backend.apply_gui_key_event(7)
            self.assertTrue(result.get("applied"), result)
            self.assertEqual(result.get("source"), "qemu-c-machine-chardev")
            self.assertTrue(result.get("down"), result)
            self.assertIsNone(result.get("mailbox"))
            self.assertGreaterEqual(int(result.get("bbk_input_write_count") or 0), 1)
            time.sleep(0.08)
            gpio_down = struct.unpack("<I", backend.read_virtual_memory(0xB0010100, 4))[0]
            self.assertEqual(gpio_down & 0x08000000, 0)
            gpio_flag = struct.unpack("<I", backend.read_virtual_memory(0xB0010180, 4))[0]
            intc_pending = struct.unpack("<I", backend.read_virtual_memory(0xB0001010, 4))[0]
            self.assertIsInstance(gpio_flag, int)
            self.assertIsInstance(intc_pending, int)
            backend.write_virtual_memory(0xB0010114, struct.pack("<I", 0x08000000))
            gpio_flag_cleared = struct.unpack("<I", backend.read_virtual_memory(0xB0010180, 4))[0]
            self.assertEqual(gpio_flag_cleared & 0x08000000, 0)
            surface = backend.guest_display_surface_snapshot()
            self.assertEqual(
                surface.get("lcd_scanout_source"), "jz4740-lcd-descriptor"
            )
            qemu = backend.snapshot()
            self.assertGreaterEqual(int(qemu.get("bbk_input_write_count") or 0), 1)
            self.assertTrue(qemu.get("guest_input_events"))
            release = backend.apply_gui_key_event(7, False)
            self.assertTrue(release.get("applied"), release)
            self.assertFalse(release.get("down"), release)
            self.assertEqual(release.get("source"), "qemu-c-machine-chardev")
            time.sleep(0.08)
            gpio_up = struct.unpack("<I", backend.read_virtual_memory(0xB0010100, 4))[0]
            self.assertEqual(gpio_up & 0x08000000, 0x08000000)

            power_down = backend.apply_gui_key_event(11, True)
            self.assertTrue(power_down.get("applied"), power_down)
            time.sleep(0.08)
            gpio_d_down = struct.unpack("<I", backend.read_virtual_memory(0xB0010300, 4))[0]
            self.assertEqual(gpio_d_down & 0x20000000, 0)
            power_up = backend.apply_gui_key_event(11, False)
            self.assertTrue(power_up.get("applied"), power_up)
            time.sleep(0.08)
            gpio_d_up = struct.unpack("<I", backend.read_virtual_memory(0xB0010300, 4))[0]
            self.assertEqual(gpio_d_up & 0x20000000, 0x20000000)
        finally:
            backend.stop()

    def test_qemu_gpio_key_wakes_cpu_from_cpm_sleep_wait(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")

        marker_va = 0x80006000
        marker = 0x51EECAFE
        words = (
            0x3C08B000,  # lui t0, 0xb000
            0x35081000,  # ori t0, t0, 0x1000 (INTC)
            0x3C090800,  # lui t1, 0x0800 (GPIO1 source 27)
            0xAD09000C,  # sw t1, ICMCR(t0)
            0x3C08B000,  # lui t0, 0xb000 (CPM)
            0x24090001,  # addiu t1, zero, 1 (LCR.SLEEP)
            0xAD090004,  # sw t1, LCR(t0)
            0x400A6000,  # mfc0 t2, Status
            0x354A0400,  # ori t2, t2, Status.IM2
            0x408A6000,  # mtc0 t2, Status
            0x42000020,  # wait
            0x3C0A8000,  # lui t2, 0x8000
            0x354A6000,  # ori t2, t2, 0x6000
            0x3C0B51EE,  # lui t3, 0x51ee
            0x356BCAFE,  # ori t3, t3, 0xcafe
            0xAD4B0000,  # sw t3, 0(t2)
            0x08001010,  # j 0x80004040
            0x00000000,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "cpm-sleep-wait.bin"
            image.write_bytes(struct.pack(f"<{len(words)}I", *words))
            config = build_bbk_qemu_config(
                boot_mode="c200",
                image=image,
                firmware_patches=("none",),
                gdb="auto",
                timeout_seconds=2.0,
            )
            backend = QemuProcessBackend(config)
            try:
                backend.start()
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    lcr = struct.unpack(
                        "<I", backend.read_virtual_memory(0xB0000004, 4)
                    )[0]
                    if lcr == 1:
                        break
                    time.sleep(0.02)
                self.assertEqual(lcr, 1)
                self.assertEqual(
                    struct.unpack("<I", backend.read_virtual_memory(marker_va, 4))[0],
                    0,
                )

                press = backend.apply_gui_key_event(7, True)
                self.assertTrue(press.get("applied"), press)
                deadline = time.time() + 2.0
                observed = 0
                while time.time() < deadline:
                    observed = struct.unpack(
                        "<I", backend.read_virtual_memory(marker_va, 4)
                    )[0]
                    if observed == marker:
                        break
                    time.sleep(0.02)
                self.assertEqual(observed, marker)
                self.assertEqual(
                    struct.unpack("<I", backend.read_virtual_memory(0xB0001010, 4))[0]
                    & (1 << 27),
                    1 << 27,
                )
                backend.apply_gui_key_event(7, False)
            finally:
                backend.stop()

    def test_qemu_power_key_chardev_drives_gpio_d29(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")

        config = build_bbk_qemu_config(
            boot_mode="c200",
            gdb="auto",
            bbk_machine_options=("hibernate-poweroff=off",),
            timeout_seconds=1.5,
        )
        backend = QemuProcessBackend(config)
        try:
            backend.start()
            self.assertTrue(backend.running())
            idle = struct.unpack("<I", backend.read_virtual_memory(0xB0010300, 4))[0]
            self.assertEqual(idle & 0x20000000, 0x20000000)

            press = backend.apply_gui_key_event(11, True)
            self.assertTrue(press.get("applied"), press)
            time.sleep(0.08)
            down = struct.unpack("<I", backend.read_virtual_memory(0xB0010300, 4))[0]
            self.assertEqual(down & 0x20000000, 0)

            release = backend.apply_gui_key_event(11, False)
            self.assertTrue(release.get("applied"), release)
            time.sleep(0.08)
            up = struct.unpack("<I", backend.read_virtual_memory(0xB0010300, 4))[0]
            self.assertEqual(up & 0x20000000, 0x20000000)
        finally:
            backend.stop()

    def test_bbk9588_touch_refuses_guest_ram_fallback_without_chardev(self) -> None:
        class _RunningProc:
            def poll(self) -> None:
                return None

        config = QemuSystemConfig(machine="bbk9588")
        backend = QemuProcessBackend(config)
        backend.proc = _RunningProc()  # type: ignore[assignment]
        backend.gdb_sock = object()  # type: ignore[assignment]
        backend.bbk_input_sock = None

        def fail_gdb(*_args: object) -> object:
            self.fail("unexpected GDB touch fallback access")

        def fail_write(*_args: object) -> None:
            self.fail("unexpected guest-RAM touch fallback write")

        backend._pause_for_gdb_locked = fail_gdb  # type: ignore[method-assign]
        backend._resume_after_gdb_locked = fail_gdb  # type: ignore[method-assign]
        backend._read_pc_paused_locked = fail_gdb  # type: ignore[method-assign]
        backend._read_u32_paused_locked = fail_gdb  # type: ignore[method-assign]
        backend._read_u8_paused_locked = fail_gdb  # type: ignore[method-assign]
        backend._read_u16_paused_locked = fail_gdb  # type: ignore[method-assign]
        backend._write_u8_paused_locked = fail_write  # type: ignore[method-assign]
        backend._write_u16_paused_locked = fail_write  # type: ignore[method-assign]
        backend._write_u32_paused_locked = fail_write  # type: ignore[method-assign]

        result = backend.apply_touch_state(120, 160, True)

        self.assertFalse(result.get("applied"), result)
        self.assertFalse(result.get("firmware_globals_written"), result)
        self.assertIsNone(result.get("mailbox"))
        self.assertIn("refusing guest-RAM mailbox/global fallback", str(result.get("error")))

    def test_bbk9588_key_refuses_guest_ram_fallback_without_chardev(self) -> None:
        class _RunningProc:
            def poll(self) -> None:
                return None

        config = QemuSystemConfig(machine="bbk9588")
        backend = QemuProcessBackend(config)
        backend.proc = _RunningProc()  # type: ignore[assignment]
        backend.gdb_sock = object()  # type: ignore[assignment]
        backend.bbk_input_sock = None

        def fail_gdb(*_args: object) -> object:
            self.fail("unexpected GDB key fallback access")

        backend._pause_for_gdb_locked = fail_gdb  # type: ignore[method-assign]
        backend._resume_after_gdb_locked = fail_gdb  # type: ignore[method-assign]
        backend._read_u32_paused_locked = fail_gdb  # type: ignore[method-assign]
        backend._read_u8_paused_locked = fail_gdb  # type: ignore[method-assign]
        backend._write_u8_paused_locked = fail_gdb  # type: ignore[method-assign]

        result = backend.apply_gui_key_event(7, True)

        self.assertFalse(result.get("applied"), result)
        self.assertIsNone(result.get("mailbox"))
        self.assertIn("refusing guest-RAM mailbox/global fallback", str(result.get("error")))

    def test_qemu_bbk9588_touch_uses_chardev_without_guest_ram_global_writes(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        config = build_bbk_qemu_config(
            boot_mode="c200",
            gdb="auto",
            bbk_machine_options=("hibernate-poweroff=off",),
            timeout_seconds=1.5,
        )
        backend = QemuProcessBackend(config)
        try:
            backend.start()
            self.assertTrue(backend.running())
            time.sleep(1.0)
            gdb_writes_before = backend.gdb_write_count
            register_writes_before = backend.gdb_register_write_count
            result = backend.apply_touch_state(120, 160, True)
            self.assertTrue(result.get("applied"), result)
            self.assertEqual(result.get("source"), "qemu-c-machine-chardev")
            self.assertFalse(result.get("calibration_release_seeded"))
            self.assertFalse(result.get("firmware_globals_written"), result)
            self.assertNotIn("touch_x_addr", result)
            self.assertNotIn("touch_y_addr", result)
            self.assertNotIn("firmware_touch_x_addr", result)
            self.assertNotIn("firmware_touch_y_addr", result)
            self.assertNotIn("latch_addr", result)
            self.assertNotIn("diagnostic_latch_addr", result)
            self.assertIsNone(result.get("mailbox"))
            self.assertGreaterEqual(int(result.get("bbk_input_write_count") or 0), 1)
            self.assertEqual(backend.gdb_write_count, gdb_writes_before)
            self.assertEqual(backend.gdb_register_write_count, register_writes_before)
            gui_handler = result.get("gui_handler")
            self.assertIsInstance(gui_handler, dict)
            assert isinstance(gui_handler, dict)
            self.assertTrue(gui_handler.get("skipped"), gui_handler)
            self.assertEqual(gui_handler.get("source"), "qemu-c-machine")
            time.sleep(0.08)
            touch_globals_x = backend.read_virtual_memory(0x80370FC8, 4)
            touch_globals_y = backend.read_virtual_memory(0x80370FCC, 4)
            self.assertIn(touch_globals_x, {b"\xff\xff\x00\x00", (120).to_bytes(4, "little")})
            self.assertIn(touch_globals_y, {b"\xff\xff\x00\x00", (160).to_bytes(4, "little")})
            gpio = struct.unpack("<I", backend.read_virtual_memory(0xB0010100, 4))[0]
            self.assertEqual(gpio & 0x00040000, 0)
            sadc_status = backend.read_virtual_memory(0xB007000C, 1)[0]
            intc_pending = struct.unpack("<I", backend.read_virtual_memory(0xB0001010, 4))[0]
            if sadc_status & 0x14 == 0x14:
                self.assertEqual(sadc_status & 0x14, 0x14)
                self.assertEqual(intc_pending & (1 << 12), 1 << 12)
            else:
                self.assertEqual(intc_pending & (1 << 12), 0)
            gdb_writes_before_release = backend.gdb_write_count
            register_writes_before_release = backend.gdb_register_write_count
            release = backend.apply_touch_state(120, 160, False)
            self.assertTrue(release.get("applied"), release)
            self.assertFalse(release.get("firmware_globals_written"), release)
            self.assertEqual(backend.gdb_write_count, gdb_writes_before_release)
            self.assertEqual(backend.gdb_register_write_count, register_writes_before_release)
            time.sleep(0.08)
            released_gpio = struct.unpack("<I", backend.read_virtual_memory(0xB0010100, 4))[0]
            self.assertEqual(released_gpio & 0x00040000, 0)
            released_status = backend.read_virtual_memory(0xB007000C, 1)[0]
            released_intc_pending = struct.unpack("<I", backend.read_virtual_memory(0xB0001010, 4))[0]
            if released_status & 0x08:
                self.assertEqual(released_status & 0x08, 0x08)
            else:
                self.assertEqual(released_intc_pending & (1 << 12), 0)
            qemu = backend.snapshot()
            self.assertGreaterEqual(int(qemu.get("bbk_input_write_count") or 0), 1)
            self.assertTrue(qemu.get("guest_input_events"))

            disconnected = backend.apply_usb_power_state(False)
            self.assertTrue(disconnected.get("applied"), disconnected)
            time.sleep(0.08)
            disconnected_gpio = struct.unpack(
                "<I", backend.read_virtual_memory(0xB0010100, 4)
            )[0]
            self.assertEqual(disconnected_gpio & 0x00040000, 0x00040000)

            backend.apply_touch_state(120, 160, True)
            backend.apply_touch_state(120, 160, False)
            time.sleep(0.08)
            after_touch_gpio = struct.unpack(
                "<I", backend.read_virtual_memory(0xB0010100, 4)
            )[0]
            self.assertEqual(after_touch_gpio & 0x00040000, 0x00040000)

            connected = backend.apply_usb_power_state(True)
            self.assertTrue(connected.get("applied"), connected)
            time.sleep(0.08)
            connected_gpio = struct.unpack(
                "<I", backend.read_virtual_memory(0xB0010100, 4)
            )[0]
            self.assertEqual(connected_gpio & 0x00040000, 0)
        finally:
            backend.stop()

    def test_qemu_gdb_touch_bridge_calls_gui_handler_after_active_object(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        config = build_bbk_qemu_config(boot_mode="c200", machine="malta", timeout_seconds=1.5)
        backend = QemuProcessBackend(config)
        try:
            backend.start()
            self.assertTrue(backend.running())
            time.sleep(0.8)
            gui: dict[str, object] = {}
            for x, y in ((10, 10), (230, 10), (230, 310), (10, 310)):
                self.assertTrue(backend.apply_touch_state(x, y, True).get("applied"))
                time.sleep(0.45)
                release = backend.apply_touch_state(x, y, False)
                self.assertTrue(release.get("applied"), release)
                time.sleep(0.45)
                gui = backend.guest_gui_state_snapshot()
                if gui.get("active_object_ready"):
                    break
            self.assertTrue(gui.get("active_object_ready"), gui)

            result = backend.apply_touch_state(150, 205, True)
            self.assertTrue(result.get("applied"), result)
            gui_handler = result.get("gui_handler")
            self.assertIsInstance(gui_handler, dict)
            assert isinstance(gui_handler, dict)
            self.assertTrue(gui_handler.get("called"), gui_handler)
            handler_active = gui_handler.get("active")
            call = gui_handler.get("call")
            self.assertIsInstance(call, dict)
            assert isinstance(call, dict)
            self.assertTrue(call.get("returned"), call)
            self.assertEqual(call.get("mode"), "continue")
            self.assertEqual(call.get("final_pc"), "0x80008a8c")
            gui_ring_pump = result.get("gui_ring_pump")
            self.assertIsInstance(gui_ring_pump, dict)
            assert isinstance(gui_ring_pump, dict)
            self.assertTrue(gui_ring_pump.get("pumped"), gui_ring_pump)
            self.assertTrue(gui_ring_pump.get("called"), gui_ring_pump)
            self.assertEqual(gui_ring_pump.get("queue"), "0x80825840")
            self.assertEqual(gui_ring_pump.get("call", {}).get("final_pc"), "0x80008a8c")
            gui_idle_pump = result.get("gui_idle_pump")
            self.assertIsInstance(gui_idle_pump, dict)
            assert isinstance(gui_idle_pump, dict)
            self.assertTrue(gui_idle_pump.get("returned"), gui_idle_pump)
            self.assertEqual(gui_idle_pump.get("call", {}).get("final_pc"), "0x80008a8c")

            release = backend.apply_touch_state(150, 205, False)
            self.assertTrue(release.get("applied"), release)
            self.assertEqual(release.get("touch_capture_active"), handler_active)
            release_handler = release.get("gui_handler")
            self.assertIsInstance(release_handler, dict)
            assert isinstance(release_handler, dict)
            self.assertEqual(release_handler.get("active"), handler_active)
            modal_close = release.get("gui_modal_close_settle")
            self.assertIsInstance(modal_close, dict)
            assert isinstance(modal_close, dict)
            if modal_close.get("attempted"):
                self.assertTrue(modal_close.get("closed"), modal_close)
                self.assertEqual(modal_close.get("modal_after"), "0x00000000")
                self.assertEqual(modal_close.get("remove_call", {}).get("final_pc"), "0x80008a8c")
                self.assertEqual(modal_close.get("close_call", {}).get("final_pc"), "0x80008a8c")
            else:
                self.assertEqual(modal_close.get("reason"), "no-blocking-busy-node")
            event_poller = release.get("gui_event_poller")
            self.assertIsInstance(event_poller, dict)
            assert isinstance(event_poller, dict)
            self.assertTrue(event_poller.get("drained"), event_poller)
            self.assertEqual(event_poller.get("flags_after"), "0x00000000")
            self.assertTrue(event_poller.get("events"), event_poller)
            storage_service = release.get("legacy_python_storage_hook")
            self.assertIsInstance(storage_service, dict)
            assert isinstance(storage_service, dict)
            self.assertIn("seed", storage_service)
            repaint_settle = release.get("gui_repaint_settle")
            self.assertIsInstance(repaint_settle, dict)
            assert isinstance(repaint_settle, dict)
            self.assertTrue(repaint_settle.get("settled"), repaint_settle)
            self.assertEqual(repaint_settle.get("final_flags"), "0x00000000")
            self.assertTrue(repaint_settle.get("rounds"), repaint_settle)
        finally:
            backend.stop()

    def test_comparison_benchmark_quick_mode(self) -> None:
        if find_qemu() is None:
            self.skipTest("qemu-system-mipsel is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            completed = subprocess.run(
                [
                    sys.executable,
                    "tests/run_qemu_comparison_benchmark.py",
                    "--out-dir",
                    str(out_dir),
                    "--prefix",
                    "comparison_quick_test",
                    "--qemu-timeout",
                    "1.5",
                ],
                cwd=Path(__file__).resolve().parents[1],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            summary = json.loads(Path(output["summary"]).read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["qemu_process"]["pc_progressed"])
            pcs = summary["qemu_process"]["sampled_pcs"]
            self.assertTrue(all(pc != "0x80012314" for pc in pcs), pcs)


if __name__ == "__main__":
    unittest.main()
