#!/usr/bin/env python3
"""Probe the experimental QEMU system-emulation backend."""

from __future__ import annotations

import argparse
import json
import struct
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emu.qemu.system import (
    DEFAULT_QEMU_EXECUTABLE,
    DEFAULT_QEMU_MACHINE,
    TOUCH_CALIBRATION_REFERENCE_POINTS,
    QemuProcessBackend,
    _gdb_continue_wait,
    _gdb_insert_breakpoint,
    _gdb_interrupt,
    _gdb_packet,
    _gdb_remove_breakpoint,
    build_bbk_qemu_config,
    build_qemu_command,
    classify_guest_pc,
    find_qemu,
    run_qemu,
)

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
BBK_DIAG_BASE = 0x89F00000
BBK_EVENT_QUEUE_VA = BBK_DIAG_BASE + 0x0040
BBK_EVENT_QUEUE_MAGIC = 0x514B4242
BBK_EVENT_QUEUE_SLOTS = 8
BBK_EVENT_QUEUE_WORDS = 5
BBK_EVENT_QUEUE_HEADER_WORDS = 4
BBK_EVENT_SCRATCH_VA = BBK_DIAG_BASE + 0x0000
BBK_TOUCH_TRACE_VA = BBK_DIAG_BASE + 0x0100
BBK_EVENT_LOOP_HELPER_PC = 0x8012CCFC
BBK_EVENT_LOOP_RETURN_PC = 0x8012CD00
BBK_WAIT_PROBE_PC = 0x8005BCD8
BBK_WAIT_PROBE_RETURN_PC = 0x8005BCE8
BBK_GUI_EVENT_POLLER_PC = 0x800DC588
BBK_GUI_EVENT_POLLER_RETURN_PC = 0x80008A8C
BBK_GUI_EVENT_QUEUE_VA = 0x80825840
BBK_GUI_EVENT_BUFFER_VA = 0x807F7500
BBK_GUI_ACTIVE_OBJECT_VA = 0x807F7600
BBK_UDC_BASE_VA = 0xB3040000
BBK_INTC_BASE_VA = 0xB0001000
BBK_IRQ24_HANDLER_ENTRY_VA = 0x80474684 + 24 * 8
BBK_IRQ24_EXPECTED_HANDLER = 0x8000EFD0
BBK_SEMAPHORE_WAIT_PC = 0x8000BA84
BBK_SEMAPHORE_RELEASE_PC = 0x8000BB98
BBK_RESOURCE_RELEASE_LOCKED_PC = 0x8017A928
BBK_RESOURCE_OBJECT_RELEASE_PC = 0x80170C90
BBK_RESOURCE_GLOBALS_BASE_VA = 0x804BF430
BBK_RESOURCE_OBJECT_TABLE_VA = 0x8086CCE0
BBK_FILE_OPENFAIL_RELEASE_CALL_PC = 0x800140E8
BBK_FILE_OPENFAIL_STRING_VA = 0x80277DD0
BBK_CACHE_SCAN_ENTRY_PC = 0x8017BE8C
BBK_CACHE_SCAN_TAIL_PC = 0x8017BEF4
BBK_CACHE_SCAN_CURSOR_VA = 0x8047425C
BBK_CACHE_SCAN_LIMIT_VA = 0x80474264
BBK_CACHE_SCAN_RESOURCE_ENABLED_VA = 0x804BF434
BBK_CACHE_SCAN_TABLE_VA = 0x8086D180
BBK_CLUSTER_CACHE_TABLE_VA = 0x8086D200
BBK_SECTOR_CACHE_TABLE_VA = 0x804BF4C0
BBK_NAND_FREE_CURSOR_VA = 0x804BF48C
BBK_NAND_STATUS_TABLE_VA_PTR = 0x804BF46C
BBK_NAND_TAG_TABLE_VA_PTR = 0x804BF470
BBK_NAND_BLOCK_COUNT_VA = 0x804BF474
BBK_NAND_PAGES_PER_BLOCK_VA = 0x804BF4B8
BBK_FILE_RW_ENTRY_PC = 0x80177920
BBK_FILE_OPEN_CONTEXT_PCS = (
    0x801714EC,
    0x8017159C,
    0x80171618,
    0x80171620,
    0x80171640,
    0x801716FC,
    0x80171708,
    0x80171718,
    0x80171724,
    0x8017172C,
    0x80171744,
    0x80171754,
    0x80171154,
    BBK_FILE_RW_ENTRY_PC,
    0x8017C920,
    0x8017AB2C,
    0x80173504,
    0x80173628,
    0x8017374C,
    0x801738BC,
    0x80173920,
    0x8017395C,
    0x80173964,
    0x80173E84,
    0x80173EA0,
    0x80173ED0,
    0x801747C4,
    0x80174B9C,
    0x80174DDC,
    0x80175E40,
    0x801753BC,
    0x80175428,
    0x801754A0,
    0x80175504,
)
BBK_CACHE_SCAN_CALLER_PCS = (
    BBK_FILE_RW_ENTRY_PC,
    0x801780D0,
    0x801780D8,
    0x801780F4,
    0x80178114,
    0x8017811C,
    0x8017812C,
    0x80177B5C,
)
BBK_FAT_CACHE_IO_PCS = (
    0x8017FBC0,  # low-level FAT/data sector read
    0x8017FD2C,  # low-level FAT/data sector write
    0x8017D534,  # FAT cache miss fill writes a 16-bit entry and marks dirty
    0x8017D5F8,  # FAT cache hit writes a 16-bit entry and marks dirty
    0x8017CBAC,  # FAT cache read path flushes a dirty victim
    0x8017D578,  # FAT cache write path flushes a dirty victim
    0x80182BF4,  # sector-cache write entry
    0x8018308C,  # sector-cache page write entry
    0x80182674,  # sector-cache dirty entry flush/writeback helper
    0x801826A0,  # sector-cache dirty flush reads dirty page count
    0x801826A8,  # sector-cache dirty flush branches on dirty page count
    0x801826B0,  # sector-cache dirty flush returns with no dirty pages
    0x801826E4,  # sector-cache dirty flush enters writeback path
    0x80182714,  # sector-cache dirty flush tests scratch/free block allocation
    0x8018282C,  # sector-cache dirty flush decrements dirty page count
    0x801828DC,  # sector-cache dirty flush reports allocation failure
    0x80182950,  # sector-cache dirty flush prepares a page program
    0x80182998,  # sector-cache dirty flush calls page program helper
    0x801829B0,  # sector-cache dirty flush prepares a tail page program
    0x801829DC,  # sector-cache dirty flush calls tail page program helper
    0x80184BD4,  # sector-cache release/flush helper
    0x80181AE4,  # sector-cache teardown caller
    0x8018024C,  # resource/sector-cache flush caller
    0x801806EC,  # resource/sector-cache flush caller
    0x80182514,  # NAND block remap/flush helper
    0x80183B04,  # NAND block erase/check helper
    0x80184468,  # NAND page program helper
    0x80184054,  # NAND page program tail helper
    0x80183D04,  # sector-cache miss/fill callback
    0x80183244,  # sector-cache marks a page dirty
    0x801833A4,  # sector-cache marks final partial page dirty
    0x80010178,  # DMAC read-channel word-count poll
    0x80010260,  # DMAC write-channel word-count poll
)
BBK_FAT_CACHE_IO_KIND_BY_PC = {
    0x8017FBC0: "sector-read-call",
    0x8017FD2C: "sector-write-call",
    0x8017D534: "fat-entry-write-miss-fill-dirty",
    0x8017D5F8: "fat-entry-write-hit-dirty",
    0x8017CBAC: "read-path-dirty-victim-flush",
    0x8017D578: "write-path-dirty-victim-flush",
    0x80182BF4: "sector-cache-write-entry",
    0x8018308C: "sector-cache-page-write-entry",
    0x80182674: "sector-cache-dirty-flush",
    0x801826A0: "sector-cache-flush-read-dirty-count",
    0x801826A8: "sector-cache-flush-branch-on-dirty",
    0x801826B0: "sector-cache-flush-return-clean",
    0x801826E4: "sector-cache-flush-writeback-entry",
    0x80182714: "sector-cache-flush-scratch-alloc-test",
    0x8018282C: "sector-cache-flush-decrement-dirty",
    0x801828DC: "sector-cache-flush-allocation-failure",
    0x80182950: "sector-cache-flush-program-prepare",
    0x80182998: "sector-cache-flush-program-call",
    0x801829B0: "sector-cache-flush-tail-program-prepare",
    0x801829DC: "sector-cache-flush-tail-program-call",
    0x80184BD4: "sector-cache-release-flush",
    0x80181AE4: "sector-cache-teardown-caller",
    0x8018024C: "resource-sector-flush-caller",
    0x801806EC: "resource-sector-flush-caller-2",
    0x80182514: "nand-block-remap-flush",
    0x80183B04: "nand-block-erase-check",
    0x80184468: "nand-page-program",
    0x80184054: "nand-page-program-tail",
    0x80183D04: "sector-cache-fill-callback",
    0x80183244: "sector-cache-mark-dirty",
    0x801833A4: "sector-cache-mark-tail-dirty",
    0x80010178: "dmac-read-poll",
    0x80010260: "dmac-write-poll",
}
BBK_FAT_CACHE_FLUSH_ONLY_PCS = tuple(
    pc
    for pc in BBK_FAT_CACHE_IO_PCS
    if pc
    not in {
        0x8017FBC0,
        0x8017FD2C,
        0x80182BF4,
        0x8018308C,
        0x80183D04,
        0x80183244,
        0x801833A4,
    }
)
BBK_CONFIG_INF_NAME_WORDS = ("0x464e4f43", "0x20204749", "0x20464e49")
BBK_STORAGE_TRACE_VA = BBK_DIAG_BASE + 0x2000
BBK_STORAGE_TRACE_MAGIC = 0x53544B42
BBK_STORAGE_TRACE_HEADER_WORDS = 4
BBK_STORAGE_TRACE_SLOTS = 4096
BBK_STORAGE_TRACE_ENTRY_WORDS = 4
BBK_NAND_TARGET_TRACE_VA = BBK_DIAG_BASE + 0x0600
BBK_NAND_TARGET_TRACE_MAGIC = 0x4E544B42
BBK_NAND_TARGET_TRACE_HEADER_WORDS = 4
BBK_NAND_TARGET_TRACE_SLOTS = 8
BBK_NAND_TARGET_TRACE_ENTRY_WORDS = 6
BBK_CLUSTER_TRACE_VA = BBK_DIAG_BASE + 0x0420
BBK_CLUSTER_TRACE_MAGIC = 0x434B4242
BBK_CLUSTER_TRACE_WORDS = 8
BBK_MSC_DMA_PROBE_CODE_VA = 0x807F7000
BBK_MSC_DMA_PROBE_WRITE_BUFFER_VA = 0x807F8000
BBK_MSC_DMA_PROBE_READ_BUFFER_VA = 0x807F8200
BBK_MSC_DMA_PROBE_STATUS_VA = 0x807F8400
BBK_MSC_DMA_PROBE_LBA = 0x40
BBK_BCH_STATUS_PROBE_CODE_VA = 0x807F8600
BBK_BCH_STATUS_PROBE_STATUS_VA = 0x807F8700
BBK_LCD_FRAME_DONE_PROBE_CODE_VA = 0x807F8800
BBK_LCD_FRAME_DONE_PROBE_STATUS_VA = 0x807F8900
BBK_LCD_FRAME_DONE_PROBE_FRAME_VA = 0xA1F82000
BBK_LCD_FRAME_DONE_PROBE_BYTES = 240 * 320 * 2
BBK_KEY_GPIO_PROBE_CODE_VA = 0x807F8A00
BBK_KEY_GPIO_PROBE_STATUS_VA = 0x807F8B00
BBK_UART_REGISTER_PROBE_CODE_VA = 0x807F8C00
BBK_UART_REGISTER_PROBE_STATUS_VA = 0x807F8D00
BBK_SADC_BATTERY_PROBE_CODE_VA = 0x807F8E00
BBK_SADC_BATTERY_PROBE_STATUS_VA = 0x807F8F00
BBK_RTC_HIBERNATE_PROBE_CODE_VA = 0x807F9000
BBK_RTC_HIBERNATE_PROBE_STATUS_VA = 0x807FA000
BBK_RTC_ALARM_IRQ_PROBE_CODE_VA = 0x807FB000
BBK_RTC_ALARM_IRQ_PROBE_STATUS_VA = 0x807FC000
BBK_SADC_DEFAULT_BATTERY_RAW = 0x0E68
BBK_SADC_DEFAULT_SADCIN_RAW = 0x0000
BBK_KEY_GPIO_PROBE_CODE = 7
BBK_KEY_GPIO_PROBE_MASK = 0x08000000
BBK_KEY_GPIO_PROBE_IRQ_MASK = 1 << 27
BBK_LCD_MIRROR_CONFIG_VA = 0x804A6B88
BBK_PROGRESS_TRACE_VA = BBK_DIAG_BASE + 0x0500
BBK_PROGRESS_TRACE_MAGIC = 0x50544B42
BBK_PROGRESS_TRACE_HEADER_WORDS = 4
BBK_PROGRESS_TRACE_SLOTS = 8
BBK_PROGRESS_TRACE_ENTRY_WORDS = 12
BBK_ALARM_UI_ENTRY_PC = 0x80013D30
BBK_ALARM_FLAG_BRANCH_PC = 0x80014130
BBK_ALARM_DB_OPEN_CALL_PC = 0x80013C6C
BBK_ALARM_DB_OPEN_RETURN_PC = 0x80013C74
BBK_ALARM_RECORD_AUDIO_CALL_PC = 0x80014010
BBK_ALARM_RECORD_AUDIO_CALL_RETURN_PC = 0x80014018
BBK_ALARM_AUDIO_CALL_PC = 0x8001409C
BBK_ALARM_AUDIO_CALL_RETURN_PC = 0x800140A4
BBK_ALARM_AUDIO_OPENFAIL_PRINT_PC = 0x800140DC
BBK_ALARM_AUDIO_OPEN_PC = 0x80184D30
BBK_ALARM_AUDIO_LOOKUP_RETURN_PC = 0x80170BA0
BBK_POWEROFF_PATH_PCS = (
    0x800055A0,  # prints/logs and enters RTC/PMU shutdown sequence
    0x800055B0,  # call to diagnostic print/log helper returned
    0x800055C0,  # UART/log helper returned; interrupts are about to be masked
    0x800055D0,  # starts polling RTC/PMU status at b0003000
    0x800055E8,  # writes b0003024
    0x80005604,  # writes b0003028
    0x80005620,  # writes b0003020
    0x80005638,  # terminal self-loop
)
BBK_POWEROFF_KIND_BY_PC = {
    0x800055A0: "poweroff-entry",
    0x800055B0: "poweroff-after-log-call",
    0x800055C0: "poweroff-after-uart-log",
    0x800055D0: "poweroff-mask-intc",
    0x800055E8: "poweroff-rtc-write-3024",
    0x80005604: "poweroff-rtc-write-3028",
    0x80005620: "poweroff-rtc-write-3020",
    0x80005638: "poweroff-terminal-loop",
}
BBK_RTC_STATUS_VA = 0xB0003000
BBK_RTC_REG3020_VA = 0xB0003020
BBK_RTC_REG3024_VA = 0xB0003024
BBK_RTC_REG3028_VA = 0xB0003028
BBK_RTC_RESET_STATUS_VA = 0xB0003030
BBK_SCHEDULER_GLOBALS_VA = 0x80473F00
BBK_GUI_GLOBALS_VA = 0x80474040
BBK_ALARM_AUDIO_RESOURCE_OPEN_RETURN_PC = 0x80185330
BBK_RESOURCE_CONSTRUCT_LOOKUP_RETURN_PC = 0x801708A8
BBK_RESOURCE_CONSTRUCT_OPEN_CALL_PC = 0x801708BC
BBK_RESOURCE_CONSTRUCT_OPEN_RETURN_PC = 0x801708C4
BBK_RESOURCE_CONSTRUCT_SUCCESS_PC = 0x80170994
BBK_RESOURCE_CONSTRUCT_DIR_SECTOR_READ_RETURN_PC = 0x80173628
BBK_RESOURCE_CONSTRUCT_DIRENT_MATCH_CALL_PC = 0x8017374C
BBK_RESOURCE_CONSTRUCT_DIRENT_HIT_PC = 0x801738BC
BBK_RESOURCE_CONSTRUCT_DIRENT_COPY_RETURN_PC = 0x80173920
BBK_RESOURCE_CONSTRUCT_CLUSTER_READ_CALL_PC = 0x8017395C
BBK_RESOURCE_CONSTRUCT_CLUSTER_READ_RETURN_PC = 0x80173964
BBK_CLUSTER_READ_ENTRY_PC = 0x8017B4E0
BBK_CLUSTER_READ_WRAPPER_RETURN_PC = 0x8017E058
BBK_CLUSTER_READ_RETURN_REPLACED_CLEAN_PC = 0x8017B678
BBK_CLUSTER_READ_RETURN_EMPTY_FILL_PC = 0x8017B704
BBK_CLUSTER_READ_RETURN_CACHE_HIT_PC = 0x8017B740
BBK_CLUSTER_READ_RETURN_IO_ERROR_PC = 0x8017B70C
BBK_CLUSTER_READ_INTERNAL_RETURN_PCS = {
    BBK_CLUSTER_READ_RETURN_REPLACED_CLEAN_PC: "replaced-clean-cache-return-zero",
    BBK_CLUSTER_READ_RETURN_EMPTY_FILL_PC: "empty-cache-fill-return-one",
    BBK_CLUSTER_READ_RETURN_CACHE_HIT_PC: "cache-hit-return-one",
    BBK_CLUSTER_READ_RETURN_IO_ERROR_PC: "io-error-return-minus-one",
}
BBK_RESOURCE_CONSTRUCT_ERROR_PCS = {
    0x80170A00,
    0x80170A70,
    0x80170A8C,
    0x80170ADC,
}
BBK_ALARM_FLAG_VA = 0x80477D4C
BBK_ALARM_RECORD_VA = 0x80477D54
BBK_ALARM_STATE_VA = 0x80477D60
BBK_RTC_STATUS_VA = 0xB0003000
BBK_RTC_RESET_STATUS_VA = 0xB0003030
BBK_FAT_SECTORS_PER_CLUSTER_VA = 0x80474254
BBK_FAT_ROOT_DIR_SECTORS_VA = 0x80474234
BBK_FAT_ROOT_LBA_VA = 0x80474244
BBK_FAT_BYTES_PER_SECTOR_VA = 0x8047427A
BBK_FAT_FAT_LBA_VA = 0x80474260
BBK_FAT_FIRST_DATA_LBA_VA = 0x80474238
BBK_FAT_DRIVE_READY_VA = 0x8047428D
BBK_FAT_EXPECTED_LAYOUT = {
    "sectors_per_cluster": 0x20,
    "root_dir_sectors": 0x20,
    "root_lba": 0x119,
    "fat_lba": 0x21,
    "first_data_lba": 0x139,
}
BBK_FAT_EXPECTED_DRIVE_READY = {1, 2}


def _parse_event_queue(data: bytes) -> dict[str, object]:
    words = struct.unpack("<" + "I" * (len(data) // 4), data)
    magic, read_idx, write_idx, count = words[:4]
    slots = []
    base = BBK_EVENT_QUEUE_HEADER_WORDS
    for index in range(BBK_EVENT_QUEUE_SLOTS):
        offset = base + index * BBK_EVENT_QUEUE_WORDS
        code, kind, arg0, arg1, arg2 = words[offset : offset + BBK_EVENT_QUEUE_WORDS]
        slots.append(
            {
                "index": index,
                "code": f"0x{code:08x}",
                "kind": f"0x{kind:08x}",
                "arg0": f"0x{arg0:08x}",
                "arg1": f"0x{arg1:08x}",
                "arg2": f"0x{arg2:08x}",
            }
        )
    return {
        "magic": f"0x{magic:08x}",
        "valid_magic": magic == BBK_EVENT_QUEUE_MAGIC,
        "read_idx": read_idx,
        "write_idx": write_idx,
        "count": count,
        "slots": slots,
    }


def _parse_event_record(data: bytes) -> dict[str, object]:
    words = struct.unpack("<7I", data)
    return {
        "words": [f"0x{word:08x}" for word in words],
        "code": f"0x{words[1]:08x}",
        "kind": f"0x{words[2]:08x}",
        "arg0": f"0x{words[3]:08x}",
        "arg1": f"0x{words[4]:08x}",
        "arg2": f"0x{words[5]:08x}",
    }


def _parse_scheduler_wake(data: bytes, task9_node: int) -> dict[str, object]:
    countdown = data[0]
    group = data[0x30]
    ready_group1 = data[0x39]
    valid_node = 0x80000000 <= task9_node < 0x8A000000
    return {
        "task9_node": f"0x{task9_node:08x}",
        "task9_node_valid": valid_node,
        "countdown_80473f08": f"0x{countdown:02x}",
        "group_80473f38": f"0x{group:02x}",
        "ready_80473f41": f"0x{ready_group1:02x}",
        "task9_ready": bool((group & 0x02) and (ready_group1 & 0x02)),
        "tick_armed": countdown == 1,
    }


def _run_input_event_queue_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    queue_size = (
        BBK_EVENT_QUEUE_HEADER_WORDS + BBK_EVENT_QUEUE_SLOTS * BBK_EVENT_QUEUE_WORDS
    ) * 4
    row: dict[str, object] = {
        "event": "qemu-input-event-queue-probe",
        "queue": "qemu-cpu-env",
        "guest_ram_mirror": f"0x{BBK_EVENT_QUEUE_VA:08x}",
        "queue_size": queue_size,
        "ok": False,
    }
    try:
        backend.start()
        time.sleep(0.8)
        with backend._lock:  # The probe intentionally observes paused guest RAM.
            backend._pause_for_gdb_locked()
            backend._write_register_paused_locked(37, BBK_WAIT_PROBE_PC)
            wait_pc_before = backend._read_register_paused_locked(37) & 0xFFFFFFFF
            sent = [
                backend._send_bbk_input_locked("K 7 1"),
                backend._send_bbk_input_locked("K 7 0"),
                backend._send_bbk_input_locked("T 120 160 512 512 1"),
            ]
            time.sleep(0.08)
            wait_pc_after = backend._read_register_paused_locked(37) & 0xFFFFFFFF
            wait_wake = {
                "wait_pc": f"0x{BBK_WAIT_PROBE_PC:08x}",
                "expected_synthetic_return_pc": f"0x{BBK_WAIT_PROBE_RETURN_PC:08x}",
                "pc_before": f"0x{wait_pc_before:08x}",
                "pc_after_input": f"0x{wait_pc_after:08x}",
                "advanced_off_wait": wait_pc_after != BBK_WAIT_PROBE_PC,
                "synthetic_return": wait_pc_after == BBK_WAIT_PROBE_RETURN_PC,
                "ok": wait_pc_before == BBK_WAIT_PROBE_PC
                and wait_pc_after != BBK_WAIT_PROBE_PC,
            }
            data = backend._read_virtual_memory_paused_locked(
                BBK_EVENT_QUEUE_VA, queue_size
            )
            queue = _parse_event_queue(data)
            task9_node = struct.unpack(
                "<I",
                backend._read_virtual_memory_paused_locked(0x806C5D10 + 9 * 4, 4),
            )[0]
            scheduler_wake = _parse_scheduler_wake(
                backend._read_virtual_memory_paused_locked(0x80473F08, 0x50),
                task9_node,
            )
            consume: dict[str, object] = {"ok": False}
            valid_record_consume: dict[str, object] = {"ok": False}
            if int(queue.get("count") or 0) > 0:
                backend._write_register_paused_locked(2, 0x000000FF)
                backend._write_register_paused_locked(37, BBK_EVENT_LOOP_HELPER_PC)
                stop_reply = backend._step_paused_locked()
                pc_after = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                v0_after = backend._read_register_paused_locked(2) & 0xFFFFFFFF
                a1_after = backend._read_register_paused_locked(5) & 0xFFFFFFFF
                queue_after = _parse_event_queue(
                    backend._read_virtual_memory_paused_locked(
                        BBK_EVENT_QUEUE_VA, queue_size
                    )
                )
                scratch = _parse_event_record(
                    backend._read_virtual_memory_paused_locked(
                        BBK_EVENT_SCRATCH_VA, 0x1C
                    )
                )
                consumed_slot = None
                slots_after = queue_after.get("slots")
                if isinstance(slots_after, list) and slots_after:
                    consumed_slot = slots_after[0]
                consumed_slot_cleared = (
                    isinstance(consumed_slot, dict)
                    and consumed_slot.get("code") == "0x00000000"
                    and consumed_slot.get("kind") == "0x00000000"
                    and consumed_slot.get("arg0") == "0x00000000"
                    and consumed_slot.get("arg1") == "0x00000000"
                    and consumed_slot.get("arg2") == "0x00000000"
                )
                consume.update(
                    {
                        "stop_reply": stop_reply,
                        "pc_after": f"0x{pc_after:08x}",
                        "v0_after": f"0x{v0_after:08x}",
                        "a1_after": f"0x{a1_after:08x}",
                        "scratch": scratch,
                        "queue_after": queue_after,
                        "consumed_slot_cleared": consumed_slot_cleared,
                        "ok": (
                            pc_after == BBK_EVENT_LOOP_RETURN_PC
                            and v0_after == BBK_EVENT_SCRATCH_VA
                            and a1_after == 3
                            and int(queue_after.get("count") or 0)
                            == int(queue.get("count") or 0) - 1
                            and scratch.get("code") == "0x00000003"
                            and consumed_slot_cleared
                        ),
                    }
                )
                if int(queue_after.get("count") or 0) > 0:
                    backend._write_virtual_memory_paused_locked(
                        BBK_EVENT_SCRATCH_VA, b"\x00" * 0x1C
                    )
                    backend._write_register_paused_locked(2, BBK_EVENT_SCRATCH_VA)
                    backend._write_register_paused_locked(37, BBK_EVENT_LOOP_HELPER_PC)
                    valid_stop_reply = backend._step_paused_locked()
                    valid_pc_after = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    valid_v0_after = backend._read_register_paused_locked(2) & 0xFFFFFFFF
                    valid_a1_after = backend._read_register_paused_locked(5) & 0xFFFFFFFF
                    valid_queue_after = _parse_event_queue(
                        backend._read_virtual_memory_paused_locked(
                            BBK_EVENT_QUEUE_VA, queue_size
                        )
                    )
                    valid_record = _parse_event_record(
                        backend._read_virtual_memory_paused_locked(
                            BBK_EVENT_SCRATCH_VA, 0x1C
                        )
                    )
                    valid_record_consume.update(
                        {
                            "stop_reply": valid_stop_reply,
                            "pc_after": f"0x{valid_pc_after:08x}",
                            "v0_after": f"0x{valid_v0_after:08x}",
                            "a1_after": f"0x{valid_a1_after:08x}",
                            "record": valid_record,
                            "queue_after": valid_queue_after,
                            "ok": (
                                valid_pc_after == BBK_EVENT_LOOP_RETURN_PC
                                and valid_v0_after == BBK_EVENT_SCRATCH_VA
                                and valid_a1_after == 3
                                and int(valid_queue_after.get("count") or 0)
                                == int(queue_after.get("count") or 0) - 1
                                and valid_record.get("code") == "0x00000003"
                            ),
                        }
                    )
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row["sent"] = sent
        row["queue_state"] = queue
        row["scheduler_wake"] = scheduler_wake
        row["wait_wake"] = wait_wake
        row["helper_consume"] = consume
        row["valid_record_consume"] = valid_record_consume
        scheduler_ok = (
            not scheduler_wake.get("task9_node_valid")
            or bool(scheduler_wake.get("task9_ready"))
        )
        row["ok"] = (
            all(sent)
            and bool(queue.get("valid_magic"))
            and int(queue.get("count") or 0) >= 3
            and scheduler_ok
            and bool(wait_wake.get("ok"))
            and bool(consume.get("ok"))
            and bool(valid_record_consume.get("ok"))
        )
        if not row["ok"]:
            row["error"] = "input queue write/consume verification failed"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_gui_event_poller_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    scratch = BBK_EVENT_SCRATCH_VA
    record_words = [
        0x806C5160,
        0x000000D2,
        0x00000000,
        0x00CD0096,
        0x00000000,
        0x00000000,
        0x00000000,
    ]
    queue_words = [
        0x40000000,
        0,
        0,
        0,
        BBK_GUI_EVENT_BUFFER_VA,
        2,
        0,
        1,
    ]
    row: dict[str, object] = {
        "event": "qemu-gui-event-poller-probe",
        "queue": f"0x{BBK_GUI_EVENT_QUEUE_VA:08x}",
        "buffer": f"0x{BBK_GUI_EVENT_BUFFER_VA:08x}",
        "scratch": f"0x{scratch:08x}",
        "ok": False,
    }
    try:
        backend.start()
        time.sleep(0.8)
        with backend._lock:
            backend._pause_for_gdb_locked()
            backend._write_virtual_memory_paused_locked(
                BBK_GUI_EVENT_QUEUE_VA,
                struct.pack("<8I", *queue_words),
            )
            backend._write_virtual_memory_paused_locked(
                BBK_GUI_EVENT_BUFFER_VA,
                struct.pack("<7I", *record_words) + (b"\x00" * 0x1C),
            )
            backend._write_virtual_memory_paused_locked(scratch, b"\x00" * 0x1C)
            backend._write_register_paused_locked(4, scratch)
            backend._write_register_paused_locked(31, BBK_GUI_EVENT_POLLER_RETURN_PC)
            backend._write_register_paused_locked(37, BBK_GUI_EVENT_POLLER_PC)
            stop_reply = backend._step_paused_locked()
            pc_after = backend._read_register_paused_locked(37) & 0xFFFFFFFF
            scratch_words = list(
                struct.unpack(
                    "<7I",
                    backend._read_virtual_memory_paused_locked(scratch, 0x1C),
                )
            )
            queue_after_words = list(
                struct.unpack(
                    "<8I",
                    backend._read_virtual_memory_paused_locked(
                        BBK_GUI_EVENT_QUEUE_VA, 0x20
                    ),
                )
            )
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row.update(
            {
                "stop_reply": stop_reply,
                "pc_after": f"0x{pc_after:08x}",
                "scratch_words": [f"0x{word:08x}" for word in scratch_words],
                "queue_after_words": [
                    f"0x{word:08x}" for word in queue_after_words
                ],
                "read_index_after": queue_after_words[6],
                "flags_after": f"0x{queue_after_words[0]:08x}",
                "ok": (
                    pc_after == BBK_GUI_EVENT_POLLER_RETURN_PC
                    and scratch_words == record_words
                    and queue_after_words[6] == 1
                    and queue_after_words[0] == 0
                ),
            }
        )
        if not row["ok"]:
            row["error"] = "GUI event poller verification failed"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_udc_idle_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-udc-idle-probe",
        "base": f"0x{BBK_UDC_BASE_VA:08x}",
        "ok": False,
    }
    try:
        backend.start()
        time.sleep(0.8)
        with backend._lock:
            backend._pause_for_gdb_locked()
            data = backend._read_virtual_memory_paused_locked(BBK_UDC_BASE_VA, 0x80)
            words = list(struct.unpack("<8I", data[:0x20]))
            pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        faddr = data[0x00]
        power = data[0x01]
        intr_in = struct.unpack_from("<H", data, 0x02)[0]
        intr_out = struct.unpack_from("<H", data, 0x04)[0]
        intr_in_enable = struct.unpack_from("<H", data, 0x06)[0]
        intr_out_enable = struct.unpack_from("<H", data, 0x08)[0]
        intr_usb = data[0x0A]
        intr_usb_enable = data[0x0B]
        frame = struct.unpack_from("<H", data, 0x0C)[0]
        index = data[0x0E]
        testmode = data[0x0F]
        in_maxp = struct.unpack_from("<H", data, 0x10)[0]
        in_csr = struct.unpack_from("<H", data, 0x12)[0]
        out_maxp = struct.unpack_from("<H", data, 0x14)[0]
        out_csr = struct.unpack_from("<H", data, 0x16)[0]
        count = struct.unpack_from("<H", data, 0x18)[0]
        epinfo = data[0x78]
        raminfo = data[0x79]
        errors: list[str] = []
        if power & 0x1A:
            errors.append(f"Power has read-only host-state bits set: 0x{power:02x}")
        if power & ~0xE5:
            errors.append(f"Power exposes bits outside writable mask: 0x{power:02x}")
        if intr_in != 0:
            errors.append(f"IntrIn active in no-host idle: 0x{intr_in:04x}")
        if intr_out != 0:
            errors.append(f"IntrOut active in no-host idle: 0x{intr_out:04x}")
        if intr_usb != 0:
            errors.append(f"IntrUSB active in no-host idle: 0x{intr_usb:02x}")
        if intr_out_enable & 0x0001:
            errors.append(f"IntrOutE endpoint0 enable bit should stay clear: 0x{intr_out_enable:04x}")
        if intr_usb_enable & ~0x0F:
            errors.append(f"IntrUSBE exposes undefined bits: 0x{intr_usb_enable:02x}")
        if frame & ~0x07FF:
            errors.append(f"Frame exceeds 11-bit counter width: 0x{frame:04x}")
        if index & ~0x0F:
            errors.append(f"Index exposes undefined bits: 0x{index:02x}")
        if testmode & ~0x3F:
            errors.append(f"Testmode exposes undefined bits: 0x{testmode:02x}")
        if count != 0:
            errors.append(f"Count0/OutCount should be empty without host data: 0x{count:04x}")
        if any(data[0x20:0x60]):
            errors.append("UDC FIFO window is not idle zero")
        if epinfo != 0x23:
            errors.append(f"EPInfo should report 3 IN / 2 OUT endpoints: 0x{epinfo:02x}")
        if raminfo != 0:
            errors.append(f"RAMInfo expected idle zero: 0x{raminfo:02x}")
        row.update(
            {
                "pc": f"0x{pc:08x}",
                "classification": classify_guest_pc(f"0x{pc:08x}"),
                "words": [f"0x{word:08x}" for word in words],
                "registers": {
                    "faddr": f"0x{faddr:02x}",
                    "power": f"0x{power:02x}",
                    "intr_in": f"0x{intr_in:04x}",
                    "intr_out": f"0x{intr_out:04x}",
                    "intr_in_enable": f"0x{intr_in_enable:04x}",
                    "intr_out_enable": f"0x{intr_out_enable:04x}",
                    "intr_usb": f"0x{intr_usb:02x}",
                    "intr_usb_enable": f"0x{intr_usb_enable:02x}",
                    "frame": f"0x{frame:04x}",
                    "index": f"0x{index:02x}",
                    "testmode": f"0x{testmode:02x}",
                    "in_maxp": f"0x{in_maxp:04x}",
                    "in_csr": f"0x{in_csr:04x}",
                    "out_maxp": f"0x{out_maxp:04x}",
                    "out_csr": f"0x{out_csr:04x}",
                    "count": f"0x{count:04x}",
                    "epinfo": f"0x{epinfo:02x}",
                    "raminfo": f"0x{raminfo:02x}",
                },
                "ok": not errors,
            }
        )
        if not row["ok"]:
            row["error"] = "; ".join(errors)
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_storage_layout_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-storage-layout-probe",
        "ok": False,
    }
    try:
        backend.start()
        deadline = time.perf_counter() + max(1.0, float(config.timeout_seconds))
        attempts: list[dict[str, object]] = []
        pc = 0
        values: dict[str, int] = {}
        mismatches: dict[str, dict[str, str]] = {}
        while time.perf_counter() < deadline:
            with backend._lock:
                backend._pause_for_gdb_locked()
                bytes_per_sector = struct.unpack(
                    "<H",
                    backend._read_virtual_memory_paused_locked(
                        BBK_FAT_BYTES_PER_SECTOR_VA, 2
                    ),
                )[0]
                values = {
                    "sectors_per_cluster": backend._read_u8_paused_locked(
                        BBK_FAT_SECTORS_PER_CLUSTER_VA
                    ),
                    "root_dir_sectors": backend._read_u16_paused_locked(
                        BBK_FAT_ROOT_DIR_SECTORS_VA
                    ),
                    "root_lba": backend._read_u32_paused_locked(BBK_FAT_ROOT_LBA_VA),
                    "bytes_per_sector": bytes_per_sector,
                    "fat_lba": backend._read_u32_paused_locked(BBK_FAT_FAT_LBA_VA),
                    "first_data_lba": backend._read_u32_paused_locked(
                        BBK_FAT_FIRST_DATA_LBA_VA
                    ),
                    "drive_ready": backend._read_u8_paused_locked(BBK_FAT_DRIVE_READY_VA),
                }
                pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
            mismatches = {
                key: {
                    "expected": f"0x{expected:x}",
                    "actual": f"0x{values.get(key, 0):x}",
                }
                for key, expected in BBK_FAT_EXPECTED_LAYOUT.items()
                if values.get(key) != expected
            }
            if values.get("drive_ready") not in BBK_FAT_EXPECTED_DRIVE_READY:
                mismatches["drive_ready"] = {
                    "expected": "0x1 or 0x2",
                    "actual": f"0x{values.get('drive_ready', 0):x}",
                }
            attempts.append(
                {
                    "pc": f"0x{pc:08x}",
                    "values": {key: f"0x{value:x}" for key, value in values.items()},
                    "mismatch_count": len(mismatches),
                }
            )
            if not mismatches:
                break
            time.sleep(0.25)
        if len(attempts) > 8:
            row["attempts"] = attempts[:3] + [{"omitted": len(attempts) - 6}] + attempts[-3:]
        else:
            row["attempts"] = attempts
        if not values:
            raise RuntimeError("storage layout probe did not collect a sample")
        if mismatches:
            row["timed_out"] = True
            row["waited_seconds"] = round(max(0.0, float(config.timeout_seconds)), 3)
        row.update(
            {
                "pc": f"0x{pc:08x}",
                "classification": classify_guest_pc(f"0x{pc:08x}"),
                "values": {key: f"0x{value:x}" for key, value in values.items()},
                "mismatches": mismatches,
                "ok": not mismatches,
            }
        )
        if not row["ok"]:
            row["error"] = "FAT16 storage globals do not match the logical NAND volume layout"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _format_u32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08x}"


def _read_optional_words(
    backend: QemuProcessBackend, va: int, count: int
) -> list[str] | str:
    if not (0x80000000 <= va < 0x8A000000):
        return "not-guest-ram"
    try:
        data = backend._read_virtual_memory_paused_locked(va, count * 4)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return [_format_u32(word) for word in struct.unpack("<" + "I" * count, data)]


def _read_optional_bytes(
    backend: QemuProcessBackend, va: int, size: int
) -> bytes | None:
    if not (0x80000000 <= va < 0x8A000000):
        return None
    try:
        return backend._read_virtual_memory_paused_locked(va, size)
    except Exception:
        return None


def _decode_cache_table(
    backend: QemuProcessBackend,
    table_va: int,
    *,
    slots: int,
    buffer_words: int,
) -> list[dict[str, object]] | None:
    words = _read_optional_words(backend, table_va, slots * 4)
    if not isinstance(words, list) or len(words) < slots * 4:
        return None
    decoded: list[dict[str, object]] = []
    for slot in range(slots):
        base = slot * 4
        try:
            sector = int(words[base], 16)
            buffer_va = int(words[base + 1], 16)
            hits = int(words[base + 2], 16)
            flags = int(words[base + 3], 16)
        except (TypeError, ValueError):
            continue
        row: dict[str, object] = {
            "slot": slot,
            "sector": _format_u32(sector),
            "buffer": _format_u32(buffer_va),
            "hits": _format_u32(hits),
            "flags": _format_u32(flags),
        }
        buffer_sample = _read_optional_words(backend, buffer_va, buffer_words)
        if buffer_sample is not None:
            row["buffer_words"] = buffer_sample
        decoded.append(row)
    return decoded


def _decode_cluster_cache_words(
    backend: QemuProcessBackend,
    words: object,
    *,
    buffer_words: int,
) -> list[dict[str, object]] | None:
    if not isinstance(words, list) or len(words) < 4:
        return None
    decoded: list[dict[str, object]] = []
    slots = len(words) // 4
    for slot in range(slots):
        base = slot * 4
        try:
            cluster = int(str(words[base]), 16)
            buffer_va = int(str(words[base + 1]), 16)
            hits = int(str(words[base + 2]), 16)
            flags = int(str(words[base + 3]), 16)
        except (TypeError, ValueError):
            continue
        row: dict[str, object] = {
            "slot": slot,
            "cluster": _format_u32(cluster),
            "buffer": _format_u32(buffer_va),
            "hits": _format_u32(hits),
            "flags": _format_u32(flags),
        }
        buffer_sample = _read_optional_words(backend, buffer_va, buffer_words)
        if buffer_sample is not None:
            row["buffer_words"] = buffer_sample
        decoded.append(row)
    return decoded


def _decode_sector_cache_table(
    backend: QemuProcessBackend,
    *,
    slots: int = 6,
    buffer_words: int = 4,
    flag_bytes: int = 0x100,
) -> list[dict[str, object]] | None:
    words = _read_optional_words(backend, BBK_SECTOR_CACHE_TABLE_VA, slots * 5)
    if not isinstance(words, list) or len(words) < slots * 5:
        return None
    decoded: list[dict[str, object]] = []
    for slot in range(slots):
        base = slot * 5
        raw_words = words[base : base + 5]
        try:
            word0 = int(raw_words[0], 16)
            word1 = int(raw_words[1], 16)
            word2 = int(raw_words[2], 16)
            word3 = int(raw_words[3], 16)
            word4 = int(raw_words[4], 16)
        except (TypeError, ValueError):
            continue
        row: dict[str, object] = {
            "slot": slot,
            "raw_words": raw_words,
            "sector": _format_u32(word0),
            "use_count": _format_u32(word1 & 0xffff),
            "dirty_count": _format_u32((word1 >> 16) & 0xffff),
            "age_or_hits": _format_u32(word2),
            "flag_buffer": _format_u32(word3),
            "buffer": _format_u32(word4),
        }
        flags = _read_optional_bytes(backend, word3, flag_bytes)
        if flags is not None:
            row["flag_bytes"] = [f"0x{byte:02x}" for byte in flags]
        buffer_sample = _read_optional_words(backend, word4, buffer_words)
        if buffer_sample is not None:
            row["buffer_words"] = buffer_sample
        decoded.append(row)
    return decoded


def _byte_counts(data: bytes | None) -> dict[str, int] | None:
    if data is None:
        return None
    counts: dict[str, int] = {}
    for value in data:
        key = f"0x{value:02x}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _gdb_insert_write_watchpoint(sock, addr: int, kind: int = 4) -> str:
    reply = _gdb_packet(sock, f"Z2,{addr & 0xFFFFFFFF:x},{kind:x}")
    if reply != "OK":
        raise IOError(
            f"GDB could not insert write watchpoint at "
            f"0x{addr & 0xFFFFFFFF:08x}: {reply!r}"
        )
    return reply


def _gdb_remove_write_watchpoint(sock, addr: int, kind: int = 4) -> str:
    reply = _gdb_packet(sock, f"z2,{addr & 0xFFFFFFFF:x},{kind:x}")
    if reply not in {"OK", ""}:
        raise IOError(
            f"GDB could not remove write watchpoint at "
            f"0x{addr & 0xFFFFFFFF:08x}: {reply!r}"
        )
    return reply


def _mips_i(opcode: int, rs: int, rt: int, imm: int) -> int:
    return (
        ((opcode & 0x3F) << 26)
        | ((rs & 0x1F) << 21)
        | ((rt & 0x1F) << 16)
        | (imm & 0xFFFF)
    )


def _mips_r(rs: int, rt: int, rd: int, shamt: int, funct: int) -> int:
    return (
        ((rs & 0x1F) << 21)
        | ((rt & 0x1F) << 16)
        | ((rd & 0x1F) << 11)
        | ((shamt & 0x1F) << 6)
        | (funct & 0x3F)
    )


def _mips_lui(rt: int, imm: int) -> int:
    return _mips_i(0x0F, 0, rt, imm)


def _mips_ori(rt: int, rs: int, imm: int) -> int:
    return _mips_i(0x0D, rs, rt, imm)


def _mips_sw(rt: int, offset: int, base: int) -> int:
    return _mips_i(0x2B, base, rt, offset)


def _mips_sb(rt: int, offset: int, base: int) -> int:
    return _mips_i(0x28, base, rt, offset)


def _mips_lw(rt: int, offset: int, base: int) -> int:
    return _mips_i(0x23, base, rt, offset)


def _mips_li(rt: int, value: int) -> list[int]:
    return [_mips_lui(rt, (value >> 16) & 0xFFFF), _mips_ori(rt, rt, value & 0xFFFF)]


def _pack_mips_words(words: list[int]) -> bytes:
    return b"".join(struct.pack("<I", word & 0xFFFFFFFF) for word in words)


def _decode_storage_trace_entry(
    seq: int, logical_word: str, absolute_word: str, first_word: str
) -> dict[str, object]:
    logical = int(logical_word, 16)
    source = "logical-read"
    decoded: dict[str, object] = {
        "seq": _format_u32(seq),
        "lba": logical_word,
        "absolute": absolute_word,
        "first_word": first_word,
    }
    if logical & 0x80000000:
        source = "nand-page"
        decoded["page"] = _format_u32(logical & 0x7fffffff)
        decoded["column"] = absolute_word
    elif logical & 0x40000000:
        source = "logical-write"
        decoded["lba"] = _format_u32(logical & 0x3fffffff)
    elif logical & 0x20000000:
        source = "nand-program"
        decoded["page"] = _format_u32(logical & 0x1fffffff)
        decoded["column"] = absolute_word
    elif logical & 0x10000000:
        source = "nand-erase"
        decoded["block_start_page"] = _format_u32(logical & 0x0fffffff)
        decoded["pages"] = absolute_word
    elif logical & 0x08000000:
        source = "dmac-transfer"
        decoded["direction"] = "read" if (logical & 0x1) else (
            "write" if (logical & 0x2) else "none"
        )
        decoded["words"] = absolute_word
        decoded["dma_phys"] = first_word
    decoded["source"] = source
    return decoded


def _storage_trace_snapshot(backend: QemuProcessBackend) -> dict[str, object] | None:
    storage_trace_words = _read_optional_words(
        backend,
        BBK_STORAGE_TRACE_VA,
        BBK_STORAGE_TRACE_HEADER_WORDS
        + BBK_STORAGE_TRACE_SLOTS * BBK_STORAGE_TRACE_ENTRY_WORDS,
    )
    if not isinstance(storage_trace_words, list) or len(storage_trace_words) < 4:
        return None
    try:
        magic = int(storage_trace_words[0], 16)
        seq = int(storage_trace_words[1], 16)
        slot = int(storage_trace_words[2], 16)
        slots = int(storage_trace_words[3], 16)
    except (TypeError, ValueError):
        return None
    if magic != BBK_STORAGE_TRACE_MAGIC:
        return None

    entries = []
    entry_start = BBK_STORAGE_TRACE_HEADER_WORDS
    entry_limit = entry_start + BBK_STORAGE_TRACE_SLOTS * BBK_STORAGE_TRACE_ENTRY_WORDS
    for index in range(
        entry_start,
        min(len(storage_trace_words), entry_limit),
        BBK_STORAGE_TRACE_ENTRY_WORDS,
    ):
        try:
            entry_seq = int(storage_trace_words[index], 16)
            if entry_seq == 0:
                continue
            entries.append(
                _decode_storage_trace_entry(
                    entry_seq,
                    storage_trace_words[index + 1],
                    storage_trace_words[index + 2],
                    storage_trace_words[index + 3],
                )
            )
        except (IndexError, TypeError, ValueError):
            continue
    entries.sort(key=lambda item: int(str(item["seq"]), 16))
    return {
        "seq": _format_u32(seq),
        "slot": _format_u32(slot),
        "slots": slots,
        "recent_entries": entries[-16:],
    }


def _nand_target_trace_snapshot(backend: QemuProcessBackend) -> dict[str, object] | None:
    words = _read_optional_words(
        backend,
        BBK_NAND_TARGET_TRACE_VA,
        BBK_NAND_TARGET_TRACE_HEADER_WORDS
        + BBK_NAND_TARGET_TRACE_SLOTS * BBK_NAND_TARGET_TRACE_ENTRY_WORDS,
    )
    if not isinstance(words, list) or len(words) < BBK_NAND_TARGET_TRACE_HEADER_WORDS:
        return None
    try:
        magic = int(words[0], 16)
        seq = int(words[1], 16)
        slot = int(words[2], 16)
        slots = int(words[3], 16)
    except (TypeError, ValueError):
        return None
    if magic != BBK_NAND_TARGET_TRACE_MAGIC:
        return None

    event_names = {
        1: "erase",
        2: "program",
        3: "logical-write",
    }
    entries = []
    entry_start = BBK_NAND_TARGET_TRACE_HEADER_WORDS
    entry_limit = (
        entry_start + BBK_NAND_TARGET_TRACE_SLOTS * BBK_NAND_TARGET_TRACE_ENTRY_WORDS
    )
    for index in range(
        entry_start,
        min(len(words), entry_limit),
        BBK_NAND_TARGET_TRACE_ENTRY_WORDS,
    ):
        try:
            entry_seq = int(words[index], 16)
            event = int(words[index + 1], 16)
            if entry_seq == 0:
                continue
            entries.append(
                {
                    "seq": _format_u32(entry_seq),
                    "event": event_names.get(event, f"event-{event}"),
                    "a": words[index + 2],
                    "b": words[index + 3],
                    "c": words[index + 4],
                    "pc": words[index + 5],
                }
            )
        except (IndexError, TypeError, ValueError):
            continue
    entries.sort(key=lambda item: int(str(item["seq"]), 16))
    return {
        "seq": _format_u32(seq),
        "slot": _format_u32(slot),
        "slots": slots,
        "recent_entries": entries[-8:],
    }


def _cluster_trace_snapshot(backend: QemuProcessBackend) -> dict[str, object] | None:
    words = _read_optional_words(backend, BBK_CLUSTER_TRACE_VA, BBK_CLUSTER_TRACE_WORDS)
    if not isinstance(words, list) or len(words) < BBK_CLUSTER_TRACE_WORDS:
        return None
    try:
        magic = int(words[0], 16)
    except (TypeError, ValueError):
        return None
    if magic != BBK_CLUSTER_TRACE_MAGIC:
        return None
    return {
        "count": words[1],
        "cluster": words[2],
        "buffer": words[3],
        "ra": words[4],
        "status": words[5],
        "sectors_per_transfer": words[6],
        "length": words[7],
    }


def _progress_trace_snapshot(backend: QemuProcessBackend) -> dict[str, object] | None:
    words = _read_optional_words(
        backend,
        BBK_PROGRESS_TRACE_VA,
        BBK_PROGRESS_TRACE_HEADER_WORDS
        + BBK_PROGRESS_TRACE_SLOTS * BBK_PROGRESS_TRACE_ENTRY_WORDS,
    )
    if not isinstance(words, list) or len(words) < BBK_PROGRESS_TRACE_HEADER_WORDS:
        return None
    try:
        magic = int(words[0], 16)
        seq = int(words[1], 16)
        slot = int(words[2], 16)
        slots = int(words[3], 16)
    except (TypeError, ValueError):
        return None
    if magic != BBK_PROGRESS_TRACE_MAGIC:
        return None

    entries: list[dict[str, object]] = []
    entry_start = BBK_PROGRESS_TRACE_HEADER_WORDS
    entry_limit = entry_start + BBK_PROGRESS_TRACE_SLOTS * BBK_PROGRESS_TRACE_ENTRY_WORDS
    fields = (
        "seq",
        "reason",
        "pc",
        "intc_pending",
        "intc_mask",
        "tcu_pending",
        "resource_flags_804bf440",
        "resource_refresh_804bf444",
        "scheduler_tick_80473f08",
        "scheduler_group_80473f38",
        "cp0_cause",
        "cp0_status",
    )
    for index in range(
        entry_start,
        min(len(words), entry_limit),
        BBK_PROGRESS_TRACE_ENTRY_WORDS,
    ):
        try:
            entry_seq = int(words[index], 16)
        except (TypeError, ValueError):
            continue
        if entry_seq == 0:
            continue
        entry = {
            field: words[index + offset]
            for offset, field in enumerate(fields)
            if index + offset < len(words)
        }
        pc_text = entry.get("pc")
        if isinstance(pc_text, str):
            entry["classification"] = classify_guest_pc(pc_text)
        entries.append(entry)
    entries.sort(key=lambda item: int(str(item["seq"]), 16))
    return {
        "seq": _format_u32(seq),
        "slot": _format_u32(slot),
        "slots": slots,
        "recent_entries": entries[-8:],
    }


def _ascii_preview(data: bytes | None) -> str | None:
    if data is None:
        return None
    chars = []
    for byte in data:
        if 0x20 <= byte < 0x7F:
            chars.append(chr(byte))
        elif byte == 0:
            chars.append(".")
        else:
            chars.append("?")
    return "".join(chars)


def _c_string_preview(data: bytes | None) -> str | None:
    if data is None:
        return None
    head = data.split(b"\0", 1)[0]
    return _ascii_preview(head)


def _pointer_context(backend: QemuProcessBackend, value: int) -> dict[str, object]:
    row: dict[str, object] = {"value": _format_u32(value)}
    if not (0x80000000 <= value < 0x8A000000):
        row["mapped"] = False
        return row
    data = _read_optional_bytes(backend, value, 0x80)
    row["mapped"] = data is not None
    if data is None:
        return row
    row["words"] = _read_optional_words(backend, value, 8)
    row["ascii"] = _ascii_preview(data[:0x40])
    row["cstring"] = _c_string_preview(data)
    row["hex_00_1f"] = data[:0x20].hex()
    try:
        row["gbk_cstring"] = data.split(b"\0", 1)[0].decode("gbk", errors="replace")
    except Exception:
        pass
    return row


def _decode_file_object(
    backend: QemuProcessBackend, file_object_va: int
) -> dict[str, object] | str | None:
    if not file_object_va:
        return None
    if not (0x80000000 <= file_object_va < 0x8A000000):
        return "not-guest-ram"
    try:
        return {
            "name_words": _read_optional_words(backend, file_object_va + 0x04, 3),
            "cluster_18": _format_u32(
                backend._read_u32_paused_locked(file_object_va + 0x18)
            ),
            "size_20": _format_u32(
                backend._read_u32_paused_locked(file_object_va + 0x20)
            ),
            "data_ptr_24": _format_u32(
                backend._read_u32_paused_locked(file_object_va + 0x24)
            ),
            "size_2c": _format_u32(
                backend._read_u32_paused_locked(file_object_va + 0x2c)
            ),
            "cluster_30": _format_u32(
                backend._read_u32_paused_locked(file_object_va + 0x30)
            ),
            "aux_cluster_34": _format_u32(
                backend._read_u32_paused_locked(file_object_va + 0x34)
            ),
            "current_cluster_38": _format_u32(
                backend._read_u32_paused_locked(file_object_va + 0x38)
            ),
            "current_offset_44": _format_u32(
                backend._read_u32_paused_locked(file_object_va + 0x44)
            ),
            "status_48": _format_u32(
                backend._read_u32_paused_locked(file_object_va + 0x48)
            ),
        }
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _decode_file_context(
    backend: QemuProcessBackend, context_va: int
) -> dict[str, object] | str | None:
    if not context_va:
        return None
    if not (0x80000000 <= context_va < 0x8A000000):
        return "not-guest-ram"
    try:
        return {
            "current_cluster_00": _format_u32(
                backend._read_u32_paused_locked(context_va)
            ),
            "current_offset_04": _format_u32(
                backend._read_u32_paused_locked(context_va + 4)
            ),
            "words_00_0c": _read_optional_words(backend, context_va, 4),
        }
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _capture_semaphore_break(
    backend: QemuProcessBackend, pc: int, stop_reply: str
) -> dict[str, object]:
    regs = {
        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
    }
    obj = regs["a0"]
    status_ptr = regs["a2"]
    obj_bytes = _read_optional_bytes(backend, obj, 0x60)
    row: dict[str, object] = {
        "pc": _format_u32(pc),
        "region": classify_guest_pc(_format_u32(pc)).get("region"),
        "stop_reply": stop_reply,
        "registers": {key: _format_u32(value) for key, value in regs.items()},
        "object_words": _read_optional_words(backend, obj, 6),
        "object_ascii": _ascii_preview(obj_bytes),
        "resource_globals": _read_optional_words(
            backend, BBK_RESOURCE_GLOBALS_BASE_VA, 12
        ),
    }
    if pc in {BBK_RESOURCE_RELEASE_LOCKED_PC, BBK_RESOURCE_OBJECT_RELEASE_PC}:
        row["resource_object_table_head"] = _read_optional_words(
            backend, BBK_RESOURCE_OBJECT_TABLE_VA, 16
        )
    if 0x80000000 <= status_ptr < 0x8A000000:
        try:
            row["status_byte"] = _format_u32(
                backend._read_u8_paused_locked(status_ptr)
            )
        except Exception as exc:
            row["status_byte"] = f"{type(exc).__name__}: {exc}"
    if pc == BBK_RESOURCE_OBJECT_RELEASE_PC:
        row["resource_words"] = _read_optional_words(backend, obj, 20)
    return row


def _trace_guest_steps(
    backend: QemuProcessBackend, *, max_steps: int = 64
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(max_steps):
        try:
            stop_reply = backend._step_paused_locked()
            pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
            regs = {
                "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
                "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
                "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
                "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
                "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
                "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
                "badva": backend._read_register_paused_locked(35) & 0xFFFFFFFF,
                "cause": backend._read_register_paused_locked(36) & 0xFFFFFFFF,
            }
        except Exception as exc:
            rows.append(
                {
                    "step": index,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            break
        row = {
            "step": index,
            "stop_reply": stop_reply,
            "pc": _format_u32(pc),
            "region": classify_guest_pc(_format_u32(pc)).get("region"),
            "registers": {key: _format_u32(value) for key, value in regs.items()},
        }
        rows.append(row)
        if classify_guest_pc(_format_u32(pc)).get("region") == "c200-exception-report-tcu-restore":
            break
    return rows


def _capture_cache_scan_tail(
    backend: QemuProcessBackend, pc: int, stop_reply: str
) -> dict[str, object]:
    regs = {
        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
        "s0": backend._read_register_paused_locked(16) & 0xFFFFFFFF,
        "s1": backend._read_register_paused_locked(17) & 0xFFFFFFFF,
        "s2": backend._read_register_paused_locked(18) & 0xFFFFFFFF,
        "s3": backend._read_register_paused_locked(19) & 0xFFFFFFFF,
        "s4": backend._read_register_paused_locked(20) & 0xFFFFFFFF,
        "s5": backend._read_register_paused_locked(21) & 0xFFFFFFFF,
        "s6": backend._read_register_paused_locked(22) & 0xFFFFFFFF,
        "s7": backend._read_register_paused_locked(23) & 0xFFFFFFFF,
        "fp": backend._read_register_paused_locked(30) & 0xFFFFFFFF,
        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
    }
    globals_row = {
        "cursor_8047425c": _format_u32(
            backend._read_u32_paused_locked(BBK_CACHE_SCAN_CURSOR_VA)
        ),
        "limit_80474264": _format_u32(
            backend._read_u32_paused_locked(BBK_CACHE_SCAN_LIMIT_VA)
        ),
        "resource_enabled_804bf434": _format_u32(
            backend._read_u32_paused_locked(BBK_CACHE_SCAN_RESOURCE_ENABLED_VA)
        ),
    }
    cache_words = _read_optional_words(backend, BBK_CACHE_SCAN_TABLE_VA, 32)
    target_sector = (regs["s2"] >> 8) & 0xFFFFFFFF
    target_low = regs["s2"] & 0xFF
    base_sector = backend._read_u32_paused_locked(BBK_CACHE_SCAN_LIMIT_VA - 4)
    if base_sector is not None:
        target_sector = (target_sector + base_sector) & 0xFFFFFFFF
    cache_hit: dict[str, object] | None = None
    if isinstance(cache_words, list):
        for slot in range(0, min(len(cache_words), 32), 4):
            try:
                sector = int(cache_words[slot], 16)
                buffer_va = int(cache_words[slot + 1], 16)
            except (IndexError, TypeError, ValueError):
                continue
            if sector != target_sector:
                continue
            entry_va = (buffer_va + target_low * 2) & 0xFFFFFFFF
            entry_bytes = _read_optional_bytes(backend, entry_va, 2)
            cache_hit = {
                "slot": slot // 4,
                "sector": _format_u32(sector),
                "buffer": _format_u32(buffer_va),
                "entry_index": _format_u32(regs["s2"]),
                "entry_offset": _format_u32(target_low * 2),
                "entry_value": (
                    _format_u32(struct.unpack("<H", entry_bytes)[0])
                    if entry_bytes is not None and len(entry_bytes) == 2
                    else None
                ),
                "buffer_words_00_1c": _read_optional_words(backend, buffer_va, 8),
            }
            break
    storage_trace_words = _read_optional_words(
        backend,
        BBK_STORAGE_TRACE_VA,
        BBK_STORAGE_TRACE_HEADER_WORDS
        + BBK_STORAGE_TRACE_SLOTS * BBK_STORAGE_TRACE_ENTRY_WORDS,
    )
    storage_trace: dict[str, object] | None = None
    if isinstance(storage_trace_words, list) and len(storage_trace_words) >= 4:
        try:
            magic = int(storage_trace_words[0], 16)
            seq = int(storage_trace_words[1], 16)
            slot = int(storage_trace_words[2], 16)
            slots = int(storage_trace_words[3], 16)
        except (TypeError, ValueError):
            magic = 0
            seq = 0
            slot = 0
            slots = 0
        if magic == BBK_STORAGE_TRACE_MAGIC:
            entries = []
            entry_start = BBK_STORAGE_TRACE_HEADER_WORDS
            entry_limit = entry_start + BBK_STORAGE_TRACE_SLOTS * BBK_STORAGE_TRACE_ENTRY_WORDS
            for index in range(
                entry_start,
                min(len(storage_trace_words), entry_limit),
                BBK_STORAGE_TRACE_ENTRY_WORDS,
            ):
                try:
                    entry_seq = int(storage_trace_words[index], 16)
                    if entry_seq == 0:
                        continue
                    entries.append(
                        _decode_storage_trace_entry(
                            entry_seq,
                            storage_trace_words[index + 1],
                            storage_trace_words[index + 2],
                            storage_trace_words[index + 3],
                        )
                    )
                except (IndexError, TypeError, ValueError):
                    continue
            entries.sort(key=lambda item: int(str(item["seq"]), 16))
            storage_trace = {
                "seq": _format_u32(seq),
                "slot": _format_u32(slot),
                "slots": slots,
                "recent_entries": entries[-16:],
            }
    return {
        "pc": _format_u32(pc),
        "kind": "cache-scan-tail",
        "region": classify_guest_pc(_format_u32(pc)).get("region"),
        "stop_reply": stop_reply,
        "registers": {key: _format_u32(value) for key, value in regs.items()},
        "globals": globals_row,
        "target_fat_sector": _format_u32(target_sector),
        "target_fat_entry_low": _format_u32(target_low),
        "resource_cache_hit": cache_hit,
        "storage_trace": storage_trace,
        "resource_cache_table": cache_words,
        "resource_cache_slots": _decode_cache_table(
            backend, BBK_CACHE_SCAN_TABLE_VA, slots=8, buffer_words=4
        ),
        "cluster_cache_table": _read_optional_words(
            backend, BBK_CLUSTER_CACHE_TABLE_VA, 24
        ),
        "cluster_cache_slots": _decode_cache_table(
            backend, BBK_CLUSTER_CACHE_TABLE_VA, slots=6, buffer_words=4
        ),
    }


def _capture_cache_scan_entry(
    backend: QemuProcessBackend, pc: int, stop_reply: str
) -> dict[str, object]:
    regs = {
        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
        "s0": backend._read_register_paused_locked(16) & 0xFFFFFFFF,
        "s1": backend._read_register_paused_locked(17) & 0xFFFFFFFF,
        "s2": backend._read_register_paused_locked(18) & 0xFFFFFFFF,
        "s3": backend._read_register_paused_locked(19) & 0xFFFFFFFF,
        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
    }
    nand_status_table = backend._read_u32_paused_locked(BBK_NAND_STATUS_TABLE_VA_PTR)
    nand_tag_table = backend._read_u32_paused_locked(BBK_NAND_TAG_TABLE_VA_PTR)
    nand_block_count = backend._read_u32_paused_locked(BBK_NAND_BLOCK_COUNT_VA)
    nand_sample_count = min(max(nand_block_count, 0), 0x4000)
    return {
        "pc": _format_u32(pc),
        "kind": "cache-scan-entry",
        "region": classify_guest_pc(_format_u32(pc)).get("region"),
        "stop_reply": stop_reply,
        "registers": {key: _format_u32(value) for key, value in regs.items()},
        "globals": {
            "cursor_8047425c": _format_u32(
                backend._read_u32_paused_locked(BBK_CACHE_SCAN_CURSOR_VA)
            ),
            "limit_80474264": _format_u32(
                backend._read_u32_paused_locked(BBK_CACHE_SCAN_LIMIT_VA)
            ),
            "resource_enabled_804bf434": _format_u32(
                backend._read_u32_paused_locked(BBK_CACHE_SCAN_RESOURCE_ENABLED_VA)
            ),
        },
        "resource_cache_table": _read_optional_words(
            backend, BBK_CACHE_SCAN_TABLE_VA, 32
        ),
        "resource_cache_slots": _decode_cache_table(
            backend, BBK_CACHE_SCAN_TABLE_VA, slots=8, buffer_words=4
        ),
        "cluster_cache_table": _read_optional_words(
            backend, BBK_CLUSTER_CACHE_TABLE_VA, 24
        ),
        "cluster_cache_slots": _decode_cache_table(
            backend, BBK_CLUSTER_CACHE_TABLE_VA, slots=6, buffer_words=4
        ),
    }


def _run_cache_scan_probe(config, *, max_events: int = 16) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-cache-scan-probe",
        "breakpoints": [
            _format_u32(BBK_CACHE_SCAN_ENTRY_PC),
            _format_u32(BBK_CACHE_SCAN_TAIL_PC),
        ],
        "ok": False,
        "events": [],
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted: set[int] = set()
            try:
                _gdb_insert_breakpoint(backend.gdb_sock, BBK_CACHE_SCAN_ENTRY_PC)
                inserted.add(BBK_CACHE_SCAN_ENTRY_PC)
                _gdb_insert_breakpoint(backend.gdb_sock, BBK_CACHE_SCAN_TAIL_PC)
                inserted.add(BBK_CACHE_SCAN_TAIL_PC)
                deadline = time.time() + float(config.timeout_seconds)
                while len(row["events"]) < max_events and time.time() < deadline:
                    wait_timeout = max(0.2, deadline - time.time())
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock, timeout=wait_timeout
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_breakpoint"] = True
                        _gdb_interrupt(backend.gdb_sock)
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    if pc == BBK_CACHE_SCAN_ENTRY_PC:
                        event = _capture_cache_scan_entry(backend, pc, stop_reply)
                    else:
                        event = _capture_cache_scan_tail(backend, pc, stop_reply)
                    row["events"].append(event)
                    if pc in {BBK_CACHE_SCAN_ENTRY_PC, BBK_CACHE_SCAN_TAIL_PC}:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                        inserted.discard(pc)
                        event["step_stop"] = backend._step_paused_locked()
                        event["after_step_pc"] = _format_u32(
                            backend._read_register_paused_locked(37) & 0xFFFFFFFF
                        )
                        event["after_step_v0"] = _format_u32(
                            backend._read_register_paused_locked(2) & 0xFFFFFFFF
                        )
                        event["after_step_cursor_8047425c"] = _format_u32(
                            backend._read_u32_paused_locked(
                                BBK_CACHE_SCAN_CURSOR_VA
                            )
                        )
                        _gdb_insert_breakpoint(backend.gdb_sock, pc)
                        inserted.add(pc)
                    else:
                        row["unexpected_stop_pc"] = _format_u32(pc)
                        break
                if len(row["events"]) >= max_events:
                    row["max_events_reached"] = True
            finally:
                if inserted:
                    for bp in tuple(inserted):
                        try:
                            _gdb_remove_breakpoint(backend.gdb_sock, bp)
                        except Exception:
                            pass
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row["ok"] = bool(row["events"])
        if not row["ok"]:
            row["error"] = "cache scan tail breakpoint was not reached"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _capture_cache_scan_caller_hit(
    backend: QemuProcessBackend, pc: int, stop_reply: str
) -> dict[str, object]:
    regs = {
        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
        "v1": backend._read_register_paused_locked(3) & 0xFFFFFFFF,
        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
        "s0": backend._read_register_paused_locked(16) & 0xFFFFFFFF,
        "s1": backend._read_register_paused_locked(17) & 0xFFFFFFFF,
        "s2": backend._read_register_paused_locked(18) & 0xFFFFFFFF,
        "s3": backend._read_register_paused_locked(19) & 0xFFFFFFFF,
        "s4": backend._read_register_paused_locked(20) & 0xFFFFFFFF,
        "s5": backend._read_register_paused_locked(21) & 0xFFFFFFFF,
        "s6": backend._read_register_paused_locked(22) & 0xFFFFFFFF,
        "s7": backend._read_register_paused_locked(23) & 0xFFFFFFFF,
        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
        "fp": backend._read_register_paused_locked(30) & 0xFFFFFFFF,
        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
    }
    file_object_va = regs["a0"] if pc == BBK_FILE_RW_ENTRY_PC else regs["s5"]
    file_object_decoded = _decode_file_object(backend, file_object_va)
    kind_by_pc = {
        BBK_FILE_RW_ENTRY_PC: "file-rw-entry",
        0x801780D0: "cache-scan-call",
        0x801780D8: "cache-scan-return",
        0x801780F4: "cache-scan-selected-cluster",
        0x80178114: "cache-scan-second-call",
        0x8017811C: "cache-scan-second-return",
        0x8017812C: "fat-link-update-call",
        0x80177B5C: "file-rw-return",
    }
    return {
        "pc": _format_u32(pc),
        "kind": kind_by_pc.get(pc, "cache-scan-caller"),
        "region": classify_guest_pc(_format_u32(pc)).get("region"),
        "stop_reply": stop_reply,
        "registers": {key: _format_u32(value) for key, value in regs.items()},
        "file_object_context": _pointer_context(backend, file_object_va),
        "file_object_decoded": file_object_decoded,
        "file_context_decoded": _decode_file_context(backend, regs["s6"]),
        "file_context_words": _read_optional_words(backend, regs["s6"], 4),
        "file_object_words_00_4c": _read_optional_words(backend, file_object_va, 20),
        "stack_words_00_8c": _read_optional_words(backend, regs["sp"], 36),
        "parent_saved_ra_guess": _format_u32(
            backend._read_u32_paused_locked((regs["sp"] + 0x78) & 0xFFFFFFFF)
        )
        if 0x80000000 <= regs["sp"] < 0x8A000000
        else None,
        "globals": {
            "cursor_8047425c": _format_u32(
                backend._read_u32_paused_locked(BBK_CACHE_SCAN_CURSOR_VA)
            ),
            "limit_80474264": _format_u32(
                backend._read_u32_paused_locked(BBK_CACHE_SCAN_LIMIT_VA)
            ),
            "resource_enabled_804bf434": _format_u32(
                backend._read_u32_paused_locked(BBK_CACHE_SCAN_RESOURCE_ENABLED_VA)
            ),
            "fat_lba_80474260": _format_u32(
                backend._read_u32_paused_locked(BBK_FAT_FAT_LBA_VA)
            ),
            "first_data_lba_80474238": _format_u32(
                backend._read_u32_paused_locked(BBK_FAT_FIRST_DATA_LBA_VA)
            ),
        },
        "resource_cache_table": _read_optional_words(
            backend, BBK_CACHE_SCAN_TABLE_VA, 32
        ),
        "resource_cache_slots": _decode_cache_table(
            backend, BBK_CACHE_SCAN_TABLE_VA, slots=8, buffer_words=4
        ),
        "cluster_cache_table": _read_optional_words(
            backend, BBK_CLUSTER_CACHE_TABLE_VA, 24
        ),
        "cluster_cache_slots": _decode_cache_table(
            backend, BBK_CLUSTER_CACHE_TABLE_VA, slots=6, buffer_words=4
        ),
        "sector_cache_slots": _decode_sector_cache_table(backend),
        "storage_trace": _storage_trace_snapshot(backend),
    }


def _run_cache_scan_caller_probe(config, *, max_events: int = 32) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-cache-scan-caller-probe",
        "breakpoints": [_format_u32(pc) for pc in BBK_CACHE_SCAN_CALLER_PCS],
        "ok": False,
        "events": [],
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted: set[int] = set()
            try:
                for bp in BBK_CACHE_SCAN_CALLER_PCS:
                    _gdb_insert_breakpoint(backend.gdb_sock, bp)
                    inserted.add(bp)
                deadline = time.time() + float(config.timeout_seconds)
                while len(row["events"]) < max_events and time.time() < deadline:
                    wait_timeout = max(0.2, min(1.0, deadline - time.time()))
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock, timeout=wait_timeout
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_breakpoint"] = True
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    event = _capture_cache_scan_caller_hit(backend, pc, stop_reply)
                    row["events"].append(event)
                    if pc in inserted:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                        inserted.discard(pc)
                        event["step_stop"] = backend._step_paused_locked()
                        event["after_step_pc"] = _format_u32(
                            backend._read_register_paused_locked(37) & 0xFFFFFFFF
                        )
                        event["after_step_v0"] = _format_u32(
                            backend._read_register_paused_locked(2) & 0xFFFFFFFF
                        )
                        _gdb_insert_breakpoint(backend.gdb_sock, pc)
                        inserted.add(pc)
                    else:
                        row["unexpected_stop_pc"] = _format_u32(pc)
                        break
                if len(row["events"]) >= max_events:
                    row["max_events_reached"] = True
            finally:
                for bp in tuple(inserted):
                    try:
                        _gdb_remove_breakpoint(backend.gdb_sock, bp)
                    except Exception:
                        pass
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row["ok"] = bool(row["events"])
        if not row["ok"]:
            row["error"] = "cache scan caller breakpoints were not reached"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _capture_fat_cache_io_hit(
    backend: QemuProcessBackend, pc: int, stop_reply: str
) -> dict[str, object]:
    regs = {
        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
        "v1": backend._read_register_paused_locked(3) & 0xFFFFFFFF,
        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
        "s0": backend._read_register_paused_locked(16) & 0xFFFFFFFF,
        "s1": backend._read_register_paused_locked(17) & 0xFFFFFFFF,
        "s2": backend._read_register_paused_locked(18) & 0xFFFFFFFF,
        "s3": backend._read_register_paused_locked(19) & 0xFFFFFFFF,
        "s4": backend._read_register_paused_locked(20) & 0xFFFFFFFF,
        "s5": backend._read_register_paused_locked(21) & 0xFFFFFFFF,
        "s6": backend._read_register_paused_locked(22) & 0xFFFFFFFF,
        "s7": backend._read_register_paused_locked(23) & 0xFFFFFFFF,
        "fp": backend._read_register_paused_locked(30) & 0xFFFFFFFF,
        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
    }
    nand_status_table = backend._read_u32_paused_locked(BBK_NAND_STATUS_TABLE_VA_PTR)
    nand_tag_table = backend._read_u32_paused_locked(BBK_NAND_TAG_TABLE_VA_PTR)
    nand_block_count = backend._read_u32_paused_locked(BBK_NAND_BLOCK_COUNT_VA)
    nand_sample_count = min(max(nand_block_count, 0), 0x4000)
    return {
        "pc": _format_u32(pc),
        "kind": BBK_FAT_CACHE_IO_KIND_BY_PC.get(pc, "fat-cache-io"),
        "region": classify_guest_pc(_format_u32(pc)).get("region"),
        "stop_reply": stop_reply,
        "registers": {key: _format_u32(value) for key, value in regs.items()},
        "globals": {
            "cursor_8047425c": _format_u32(
                backend._read_u32_paused_locked(BBK_CACHE_SCAN_CURSOR_VA)
            ),
            "limit_80474264": _format_u32(
                backend._read_u32_paused_locked(BBK_CACHE_SCAN_LIMIT_VA)
            ),
            "fat_lba_80474260": _format_u32(
                backend._read_u32_paused_locked(BBK_CACHE_SCAN_LIMIT_VA - 4)
            ),
            "first_data_lba_80474238": _format_u32(
                backend._read_u32_paused_locked(0x80474238)
            ),
            "nand_free_cursor_804bf48c": _format_u32(
                backend._read_u32_paused_locked(BBK_NAND_FREE_CURSOR_VA)
            ),
            "nand_status_table_ptr_804bf46c": _format_u32(nand_status_table),
            "nand_tag_table_ptr_804bf470": _format_u32(nand_tag_table),
            "nand_block_count_804bf474": _format_u32(nand_block_count),
            "nand_pages_per_block_804bf4b8": _format_u32(
                backend._read_u32_paused_locked(BBK_NAND_PAGES_PER_BLOCK_VA)
            ),
        },
        "a0_words": _read_optional_words(backend, regs["a0"], 5),
    "nand_status_counts": _byte_counts(
            _read_optional_bytes(backend, nand_status_table, nand_sample_count)
        ),
        "nand_tag_counts": _byte_counts(
            _read_optional_bytes(backend, nand_tag_table, nand_sample_count)
        ),
        "nand_status_at_cursor": _read_optional_bytes(
            backend,
            (nand_status_table + backend._read_u32_paused_locked(BBK_NAND_FREE_CURSOR_VA))
            & 0xFFFFFFFF,
            64,
        ).hex(" ")
        if _read_optional_bytes(
            backend,
            (nand_status_table + backend._read_u32_paused_locked(BBK_NAND_FREE_CURSOR_VA))
            & 0xFFFFFFFF,
            64,
        )
        is not None
        else None,
        "nand_tag_at_cursor": _read_optional_bytes(
            backend,
            (nand_tag_table + backend._read_u32_paused_locked(BBK_NAND_FREE_CURSOR_VA))
            & 0xFFFFFFFF,
            64,
        ).hex(" ")
        if _read_optional_bytes(
            backend,
            (nand_tag_table + backend._read_u32_paused_locked(BBK_NAND_FREE_CURSOR_VA))
            & 0xFFFFFFFF,
            64,
        )
        is not None
        else None,
        "resource_cache_slots": _decode_cache_table(
            backend, BBK_CACHE_SCAN_TABLE_VA, slots=8, buffer_words=4
        ),
        "cluster_cache_slots": _decode_cache_table(
            backend, BBK_CLUSTER_CACHE_TABLE_VA, slots=6, buffer_words=4
        ),
        "sector_cache_slots": _decode_sector_cache_table(backend),
        "storage_trace": _storage_trace_snapshot(backend),
    }


def _run_fat_cache_io_probe(
    config, *, max_events: int = 64, flush_only: bool = False
) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    breakpoint_pcs = (
        BBK_FAT_CACHE_FLUSH_ONLY_PCS
        if flush_only
        else BBK_FAT_CACHE_IO_PCS
    )
    row: dict[str, object] = {
        "event": "qemu-fat-cache-io-probe",
        "breakpoints": [_format_u32(pc) for pc in breakpoint_pcs],
        "flush_only": flush_only,
        "ok": False,
        "events": [],
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted: set[int] = set()
            try:
                for bp in breakpoint_pcs:
                    _gdb_insert_breakpoint(backend.gdb_sock, bp)
                    inserted.add(bp)
                deadline = time.time() + float(config.timeout_seconds)
                while len(row["events"]) < max_events and time.time() < deadline:
                    wait_timeout = max(0.2, min(1.0, deadline - time.time()))
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock, timeout=wait_timeout
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_breakpoint"] = True
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    event = _capture_fat_cache_io_hit(backend, pc, stop_reply)
                    row["events"].append(event)
                    if pc not in inserted:
                        row["unexpected_stop_pc"] = _format_u32(pc)
                        break
                    _gdb_remove_breakpoint(backend.gdb_sock, pc)
                    inserted.discard(pc)
                    event["step_stop"] = backend._step_paused_locked()
                    event["after_step_pc"] = _format_u32(
                        backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    )
                    event["after_step_v0"] = _format_u32(
                        backend._read_register_paused_locked(2) & 0xFFFFFFFF
                    )
                    _gdb_insert_breakpoint(backend.gdb_sock, pc)
                    inserted.add(pc)
                if len(row["events"]) >= max_events:
                    row["max_events_reached"] = True
            finally:
                for bp in tuple(inserted):
                    try:
                        _gdb_remove_breakpoint(backend.gdb_sock, bp)
                    except Exception:
                        pass
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row["ok"] = bool(row["events"])
        if not row["ok"]:
            row["error"] = "FAT cache I/O breakpoints were not reached"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _build_msc_dma_write_probe_code(lba: int) -> bytes:
    t0 = 8
    t1 = 9
    t2 = 10
    t3 = 11
    v0 = 2
    ra = 31
    write_pa = BBK_MSC_DMA_PROBE_WRITE_BUFFER_VA & 0x1FFFFFFF
    read_pa = BBK_MSC_DMA_PROBE_READ_BUFFER_VA & 0x1FFFFFFF
    arg = (lba * 512) & 0xFFFFFFFF
    words: list[int] = []

    def li(reg: int, value: int) -> None:
        words.extend(_mips_li(reg, value))

    li(t0, 0xB0020000)
    li(t2, 0xB3020000)
    li(t3, BBK_MSC_DMA_PROBE_STATUS_VA)
    li(t1, 1)
    words.append(_mips_sw(t1, 0x300, t2))

    li(t1, 0x18)
    words.append(_mips_sw(t1, 0x102C, t0))
    li(t1, arg)
    words.append(_mips_sw(t1, 0x1030, t0))
    li(t1, 6)
    words.append(_mips_sw(t1, 0x1000, t0))
    words.append(_mips_lw(v0, 0x1028, t0))
    words.append(_mips_sw(v0, 0, t3))
    li(t3, write_pa)
    words.append(_mips_sw(t3, 0x20, t2))
    li(t1, 128)
    words.append(_mips_sw(t1, 0x28, t2))
    li(t1, 0x80000001)
    words.append(_mips_sw(t1, 0x30, t2))
    words.append(_mips_lw(v0, 0x28, t2))
    li(t3, BBK_MSC_DMA_PROBE_STATUS_VA)
    words.append(_mips_lw(v0, 0x1028, t0))
    words.append(_mips_sw(v0, 4, t3))
    li(t1, 3)
    words.append(_mips_sw(t1, 0x1028, t0))
    words.append(_mips_lw(v0, 0x1028, t0))
    words.append(_mips_sw(v0, 8, t3))
    words.append(_mips_lw(v0, 0x28, t2))
    words.append(_mips_sw(v0, 12, t3))
    words.append(_mips_lw(v0, 0x1028, t0))
    words.append(_mips_sw(v0, 16, t3))

    li(t1, 0x11)
    words.append(_mips_sw(t1, 0x102C, t0))
    li(t1, arg)
    words.append(_mips_sw(t1, 0x1030, t0))
    li(t1, 6)
    words.append(_mips_sw(t1, 0x1000, t0))
    li(t3, BBK_MSC_DMA_PROBE_STATUS_VA)
    words.append(_mips_lw(v0, 0x1028, t0))
    words.append(_mips_sw(v0, 20, t3))
    li(t3, read_pa)
    words.append(_mips_sw(t3, 0x04, t2))
    li(t1, 128)
    words.append(_mips_sw(t1, 0x08, t2))
    li(t1, 0x80000001)
    words.append(_mips_sw(t1, 0x10, t2))
    words.append(_mips_lw(v0, 0x08, t2))
    li(t3, BBK_MSC_DMA_PROBE_STATUS_VA)
    words.append(_mips_lw(v0, 0x1028, t0))
    words.append(_mips_sw(v0, 24, t3))
    li(t1, 3)
    words.append(_mips_sw(t1, 0x1028, t0))
    words.append(_mips_lw(v0, 0x1028, t0))
    words.append(_mips_sw(v0, 28, t3))
    words.append(_mips_lw(v0, 0x08, t2))
    words.append(_mips_sw(v0, 32, t3))
    words.append(_mips_lw(v0, 0x1028, t0))
    words.append(_mips_sw(v0, 36, t3))

    words.append(_mips_r(ra, 0, 0, 0, 0x08))
    words.append(0)
    return _pack_mips_words(words)


def _run_msc_dma_write_probe(config, *, lba: int = BBK_MSC_DMA_PROBE_LBA) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    pattern = bytes((0xA5 ^ (index * 37 + lba)) & 0xFF for index in range(512))
    row: dict[str, object] = {
        "event": "qemu-msc-dma-write-probe",
        "lba": _format_u32(lba),
        "code": _format_u32(BBK_MSC_DMA_PROBE_CODE_VA),
        "write_buffer": _format_u32(BBK_MSC_DMA_PROBE_WRITE_BUFFER_VA),
        "read_buffer": _format_u32(BBK_MSC_DMA_PROBE_READ_BUFFER_VA),
        "status_buffer": _format_u32(BBK_MSC_DMA_PROBE_STATUS_VA),
        "ok": False,
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            code = _build_msc_dma_write_probe_code(lba)
            backend._write_virtual_memory_paused_locked(BBK_MSC_DMA_PROBE_CODE_VA, code)
            backend._write_virtual_memory_paused_locked(BBK_MSC_DMA_PROBE_WRITE_BUFFER_VA, pattern)
            backend._write_virtual_memory_paused_locked(BBK_MSC_DMA_PROBE_READ_BUFFER_VA, b"\x00" * 512)
            backend._write_virtual_memory_paused_locked(BBK_MSC_DMA_PROBE_STATUS_VA, b"\x00" * 40)
            row["code_size"] = len(code)
            row["storage_trace_before"] = _storage_trace_snapshot(backend)
            call = backend._call_guest_function_stepped_paused_locked(
                BBK_MSC_DMA_PROBE_CODE_VA,
                return_pc=BBK_WAIT_PROBE_PC,
                max_steps=96,
                max_recorded_steps=16,
            )
            row["guest_call"] = call
            readback = backend._read_virtual_memory_paused_locked(
                BBK_MSC_DMA_PROBE_READ_BUFFER_VA, 512
            )
            status_data = backend._read_virtual_memory_paused_locked(
                BBK_MSC_DMA_PROBE_STATUS_VA, 40
            )
            status_words = list(struct.unpack("<10I", status_data))
            row["status_words"] = [_format_u32(value) for value in status_words]
            row["status_lifecycle_ok"] = (
                (status_words[0] & 0x03) == 0
                and (status_words[1] & 0x03) == 0x03
                and (status_words[2] & 0x03) == 0
                and status_words[3] == 0
                and (status_words[4] & 0x03) == 0
                and (status_words[5] & 0x03) == 0
                and (status_words[6] & 0x03) == 0x03
                and (status_words[7] & 0x03) == 0
                and status_words[8] == 0
                and (status_words[9] & 0x03) == 0
            )
            row["readback_first16"] = readback[:16].hex(" ")
            row["expected_first16"] = pattern[:16].hex(" ")
            row["storage_trace_after"] = _storage_trace_snapshot(backend)
            row["matched"] = readback == pattern
            row["ok"] = (
                bool(call.get("returned"))
                and readback == pattern
                and bool(row["status_lifecycle_ok"])
            )
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        if not row["ok"]:
            row["error"] = "MSC DMA write/readback did not match"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _build_uart_register_probe_code() -> bytes:
    t0 = 8
    t1 = 9
    t2 = 10
    v0 = 2
    ra = 31
    words: list[int] = []

    def li(reg: int, value: int) -> None:
        words.extend(_mips_li(reg, value))

    li(t0, 0xB0030000)
    li(t1, BBK_UART_REGISTER_PROBE_STATUS_VA)

    li(t2, 0x00)
    words.append(_mips_sw(t2, 0x0C, t0))  # ULCR.DLAB clear
    words.append(_mips_sw(t2, 0x08, t0))  # UFCR/FIFO disabled
    words.append(_mips_sw(t2, 0x04, t0))  # UIER disabled

    words.append(_mips_lw(v0, 0x14, t0))  # ULSR reset: TEMP | TDRQ
    words.append(_mips_sw(v0, 0, t1))
    words.append(_mips_lw(v0, 0x08, t0))  # UIIR reset: no pending
    words.append(_mips_sw(v0, 4, t1))

    li(t2, 0x80)  # ULCR.DLAB
    words.append(_mips_sw(t2, 0x0C, t0))
    li(t2, 0x34)
    words.append(_mips_sw(t2, 0x00, t0))  # UDLLR
    li(t2, 0x12)
    words.append(_mips_sw(t2, 0x04, t0))  # UDLHR
    words.append(_mips_lw(v0, 0x00, t0))
    words.append(_mips_sw(v0, 8, t1))
    words.append(_mips_lw(v0, 0x04, t0))
    words.append(_mips_sw(v0, 12, t1))

    li(t2, 0x03)  # 8-bit word length, DLAB clear
    words.append(_mips_sw(t2, 0x0C, t0))
    li(t2, 0x11)  # UFCR.FME | UFCR.UME
    words.append(_mips_sw(t2, 0x08, t0))
    li(t2, 0x02)  # UIER.TDRIE
    words.append(_mips_sw(t2, 0x04, t0))
    words.append(_mips_lw(v0, 0x08, t0))
    words.append(_mips_sw(v0, 16, t1))

    li(t2, 0x92)  # UMCR.MDCE | UMCR.LOOP | UMCR.RTS
    words.append(_mips_sw(t2, 0x10, t0))
    li(t2, 0x01)  # UIER.RDRIE
    words.append(_mips_sw(t2, 0x04, t0))
    li(t2, 0xA5)
    words.append(_mips_sw(t2, 0x00, t0))  # loopback into URBR
    words.append(_mips_lw(v0, 0x14, t0))
    words.append(_mips_sw(v0, 20, t1))
    words.append(_mips_lw(v0, 0x08, t0))
    words.append(_mips_sw(v0, 24, t1))
    words.append(_mips_lw(v0, 0x00, t0))
    words.append(_mips_sw(v0, 28, t1))
    words.append(_mips_lw(v0, 0x14, t0))
    words.append(_mips_sw(v0, 32, t1))

    li(t2, 0x13)  # UFCR.UME | UFCR.RFRT | UFCR.FME
    words.append(_mips_sw(t2, 0x08, t0))
    words.append(_mips_lw(v0, 0x14, t0))
    words.append(_mips_sw(v0, 36, t1))

    words.append(_mips_r(ra, 0, 0, 0, 0x08))
    words.append(0)
    return _pack_mips_words(words)


def _run_uart_register_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-uart-register-probe",
        "code": _format_u32(BBK_UART_REGISTER_PROBE_CODE_VA),
        "status_buffer": _format_u32(BBK_UART_REGISTER_PROBE_STATUS_VA),
        "ok": False,
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            code = _build_uart_register_probe_code()
            backend._write_virtual_memory_paused_locked(BBK_UART_REGISTER_PROBE_CODE_VA, code)
            backend._write_virtual_memory_paused_locked(BBK_UART_REGISTER_PROBE_STATUS_VA, b"\x00" * 40)
            row["code_size"] = len(code)
            call = backend._call_guest_function_stepped_paused_locked(
                BBK_UART_REGISTER_PROBE_CODE_VA,
                return_pc=BBK_WAIT_PROBE_PC,
                max_steps=80,
                max_recorded_steps=12,
            )
            row["guest_call"] = call
            status_data = backend._read_virtual_memory_paused_locked(
                BBK_UART_REGISTER_PROBE_STATUS_VA, 40
            )
            status_words = list(struct.unpack("<10I", status_data))
            row["status_words"] = [_format_u32(value) for value in status_words]
            status_low = [value & 0xFF for value in status_words]
            row["status_low"] = [f"0x{value:02x}" for value in status_low]
            row["status_lifecycle_ok"] = (
                status_low[0] == 0x60
                and status_low[1] == 0x01
                and status_low[2] == 0x34
                and status_low[3] == 0x12
                and status_low[4] == 0xC2
                and (status_low[5] & 0x61) == 0x61
                and status_low[6] == 0xC4
                and status_low[7] == 0xA5
                and status_low[8] == 0x60
                and status_low[9] == 0x60
            )
            row["ok"] = bool(call.get("returned")) and bool(row["status_lifecycle_ok"])
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        if not row["ok"]:
            row["error"] = "UART register lifecycle did not match JZ4740/16550 expectations"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _build_sadc_battery_probe_code() -> bytes:
    t0 = 8
    t1 = 9
    t2 = 10
    v0 = 2
    ra = 31
    words: list[int] = []

    def li(reg: int, value: int) -> None:
        words.extend(_mips_li(reg, value))

    li(t0, 0xB0070000)
    li(t1, BBK_SADC_BATTERY_PROBE_STATUS_VA)

    li(t2, 0x00)
    words.append(_mips_sw(t2, 0x00, t0))  # ADENA
    words.append(_mips_sw(t2, 0x1C, t0))  # ADBDAT clear
    words.append(_mips_sw(t2, 0x20, t0))  # ADSDAT clear
    li(t2, 0x1F)
    words.append(_mips_sw(t2, 0x0C, t0))  # ADSTATE clear all flags

    words.append(_mips_lw(v0, 0x00, t0))
    words.append(_mips_sw(v0, 0, t1))
    words.append(_mips_lw(v0, 0x0C, t0))
    words.append(_mips_sw(v0, 4, t1))

    li(t2, 0x03)  # ADENA.SADCINEN | ADENA.PBATEN
    words.append(_mips_sw(t2, 0x00, t0))
    words.append(_mips_lw(v0, 0x00, t0))
    words.append(_mips_sw(v0, 8, t1))
    words.append(_mips_lw(v0, 0x0C, t0))
    words.append(_mips_sw(v0, 12, t1))
    words.append(_mips_lw(v0, 0x1C, t0))
    words.append(_mips_sw(v0, 16, t1))
    words.append(_mips_lw(v0, 0x20, t0))
    words.append(_mips_sw(v0, 20, t1))

    li(t2, 0x00)
    words.append(_mips_sw(t2, 0x1C, t0))
    words.append(_mips_lw(v0, 0x1C, t0))
    words.append(_mips_sw(v0, 24, t1))
    words.append(_mips_lw(v0, 0x0C, t0))
    words.append(_mips_sw(v0, 28, t1))
    words.append(_mips_sw(t2, 0x20, t0))
    words.append(_mips_lw(v0, 0x20, t0))
    words.append(_mips_sw(v0, 32, t1))
    words.append(_mips_lw(v0, 0x0C, t0))
    words.append(_mips_sw(v0, 36, t1))

    li(t2, 0x02)  # ADENA.PBATEN
    words.append(_mips_sw(t2, 0x00, t0))
    words.append(_mips_lw(v0, 0x00, t0))
    words.append(_mips_sw(v0, 40, t1))
    words.append(_mips_lw(v0, 0x0C, t0))
    words.append(_mips_sw(v0, 44, t1))
    words.append(_mips_lw(v0, 0x1C, t0))
    words.append(_mips_sw(v0, 48, t1))

    words.append(_mips_r(ra, 0, 0, 0, 0x08))
    words.append(0)
    return _pack_mips_words(words)


def _run_sadc_battery_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-sadc-battery-probe",
        "code": _format_u32(BBK_SADC_BATTERY_PROBE_CODE_VA),
        "status_buffer": _format_u32(BBK_SADC_BATTERY_PROBE_STATUS_VA),
        "ok": False,
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            code = _build_sadc_battery_probe_code()
            backend._write_virtual_memory_paused_locked(BBK_SADC_BATTERY_PROBE_CODE_VA, code)
            backend._write_virtual_memory_paused_locked(BBK_SADC_BATTERY_PROBE_STATUS_VA, b"\x00" * 52)
            row["code_size"] = len(code)
            call = backend._call_guest_function_stepped_paused_locked(
                BBK_SADC_BATTERY_PROBE_CODE_VA,
                return_pc=BBK_WAIT_PROBE_PC,
                max_steps=96,
                max_recorded_steps=12,
            )
            row["guest_call"] = call
            status_data = backend._read_virtual_memory_paused_locked(
                BBK_SADC_BATTERY_PROBE_STATUS_VA, 52
            )
            status_words = list(struct.unpack("<13I", status_data))
            row["status_words"] = [_format_u32(value) for value in status_words]
            status_low = [value & 0xFFFF for value in status_words]
            row["status_low"] = [f"0x{value:04x}" for value in status_low]
            row["status_lifecycle_ok"] = (
                status_low[0] == 0x0000
                and status_low[1] == 0x0000
                and status_low[2] == 0x0000
                and status_low[3] == 0x0003
                and status_low[4] == BBK_SADC_DEFAULT_BATTERY_RAW
                and status_low[5] == BBK_SADC_DEFAULT_SADCIN_RAW
                and status_low[6] == 0x0000
                and status_low[7] == 0x0001
                and status_low[8] == 0x0000
                and status_low[9] == 0x0000
                and status_low[10] == 0x0000
                and status_low[11] == 0x0002
                and status_low[12] == BBK_SADC_DEFAULT_BATTERY_RAW
            )
            row["ok"] = bool(call.get("returned")) and bool(row["status_lifecycle_ok"])
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        if not row["ok"]:
            row["error"] = "SADC PBAT/SADCIN lifecycle did not match JZ4740 ADENA/ADSTATE/ADBDAT semantics"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _build_rtc_hibernate_probe_code() -> bytes:
    t0 = 8
    t1 = 9
    t2 = 10
    v0 = 2
    ra = 31
    words: list[int] = []

    def li(reg: int, value: int) -> None:
        words.extend(_mips_li(reg, value))

    li(t0, 0xB0003000)
    li(t1, BBK_RTC_HIBERNATE_PROBE_STATUS_VA)

    li(t2, 0x00000000)
    words.append(_mips_sw(t2, 0x20, t0))  # HCR.PD clear before setup
    words.append(_mips_sw(t2, 0x00, t0))  # RTCCR.RTCE off; keep seconds stable
    li(t2, 0x11111111)
    words.append(_mips_sw(t2, 0x04, t0))  # RTCSR
    li(t2, 0x22222222)
    words.append(_mips_sw(t2, 0x08, t0))  # RTCSAR
    li(t2, 0x000055AA)
    words.append(_mips_sw(t2, 0x0C, t0))  # RTCGR
    li(t2, 0x0000ABC0)
    words.append(_mips_sw(t2, 0x24, t0))  # HWFCR
    li(t2, 0x00000BE0)
    words.append(_mips_sw(t2, 0x28, t0))  # HRCR
    li(t2, 0x00000001)
    words.append(_mips_sw(t2, 0x2C, t0))  # HWCR.EALM
    li(t2, 0x12345678)
    words.append(_mips_sw(t2, 0x34, t0))  # HSPR

    for out_off, rtc_off in enumerate((0x00, 0x04, 0x08, 0x0C, 0x24, 0x28, 0x2C, 0x34, 0x30)):
        words.append(_mips_lw(v0, rtc_off, t0))
        words.append(_mips_sw(v0, out_off * 4, t1))

    li(t2, 0x00000001)
    words.append(_mips_sw(t2, 0x20, t0))  # enter hibernate
    words.append(_mips_lw(v0, 0x20, t0))
    words.append(_mips_sw(v0, 9 * 4, t1))

    li(t2, 0x33333333)
    words.append(_mips_sw(t2, 0x04, t0))
    li(t2, 0x44444444)
    words.append(_mips_sw(t2, 0x08, t0))
    li(t2, 0x0000AAAA)
    words.append(_mips_sw(t2, 0x0C, t0))
    li(t2, 0x00000000)
    words.append(_mips_sw(t2, 0x24, t0))
    words.append(_mips_sw(t2, 0x28, t0))
    words.append(_mips_sw(t2, 0x2C, t0))
    words.append(_mips_sw(t2, 0x30, t0))
    li(t2, 0x87654321)
    words.append(_mips_sw(t2, 0x34, t0))
    li(t2, 0x00000000)
    words.append(_mips_sw(t2, 0x20, t0))  # HCR.PD should not clear by write
    li(t2, 0x00000020)
    words.append(_mips_sw(t2, 0x00, t0))  # only RTCCR.1HZIE remains writable

    for idx, rtc_off in enumerate((0x00, 0x04, 0x08, 0x0C, 0x24, 0x28, 0x2C, 0x34, 0x30, 0x20), start=10):
        words.append(_mips_lw(v0, rtc_off, t0))
        words.append(_mips_sw(v0, idx * 4, t1))

    words.append(_mips_r(ra, 0, 0, 0, 0x08))
    words.append(0)
    return _pack_mips_words(words)


def _run_rtc_hibernate_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-rtc-hibernate-probe",
        "code": _format_u32(BBK_RTC_HIBERNATE_PROBE_CODE_VA),
        "status_buffer": _format_u32(BBK_RTC_HIBERNATE_PROBE_STATUS_VA),
        "ok": False,
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            code = _build_rtc_hibernate_probe_code()
            backend._write_virtual_memory_paused_locked(BBK_RTC_HIBERNATE_PROBE_CODE_VA, code)
            backend._write_virtual_memory_paused_locked(BBK_RTC_HIBERNATE_PROBE_STATUS_VA, b"\x00" * 80)
            row["code_size"] = len(code)
            call = backend._call_guest_function_stepped_paused_locked(
                BBK_RTC_HIBERNATE_PROBE_CODE_VA,
                return_pc=BBK_WAIT_PROBE_PC,
                max_steps=160,
                max_recorded_steps=12,
            )
            row["guest_call"] = call
            status_data = backend._read_virtual_memory_paused_locked(
                BBK_RTC_HIBERNATE_PROBE_STATUS_VA, 80
            )
            status_words = list(struct.unpack("<20I", status_data))
            row["status_words"] = [_format_u32(value) for value in status_words]
            row["status_lifecycle_ok"] = (
                status_words[0] == 0x00000080
                and status_words[1] == 0x11111111
                and status_words[2] == 0x22222222
                and status_words[3] == 0x000055AA
                and status_words[4] == 0x0000ABC0
                and status_words[5] == 0x00000BE0
                and status_words[6] == 0x00000001
                and status_words[7] == 0x12345678
                and status_words[9] == 0x00000001
                and status_words[10] == 0x000000A0
                and status_words[11] == 0x11111111
                and status_words[12] == 0x22222222
                and status_words[13] == 0x000055AA
                and status_words[14] == 0x0000ABC0
                and status_words[15] == 0x00000BE0
                and status_words[16] == 0x00000001
                and status_words[17] == 0x12345678
                and status_words[18] == status_words[8]
                and status_words[19] == 0x00000001
            )
            row["ok"] = bool(call.get("returned")) and bool(row["status_lifecycle_ok"])
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        if not row["ok"]:
            row["error"] = "RTC hibernate write protection did not match JZ4740 HCR.PD semantics"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _build_rtc_alarm_irq_probe_code() -> bytes:
    t0 = 8
    t1 = 9
    t2 = 10
    t3 = 11
    t4 = 12
    s0 = 16
    v0 = 2
    ra = 31
    rtc_irq_bit = 0x00008000
    words: list[int] = []

    def li(reg: int, value: int) -> None:
        words.extend(_mips_li(reg, value))

    def read_intc_masked(out_off: int, intc_off: int) -> None:
        words.append(_mips_lw(v0, intc_off, t2))
        li(t3, rtc_irq_bit)
        words.append(_mips_r(v0, t3, v0, 0, 0x24))  # and v0, v0, t3
        words.append(_mips_sw(v0, out_off, t1))

    words.append((0x10 << 26) | (s0 << 16) | (12 << 11))  # mfc0 s0, Status
    li(t3, 0xFFFFFFFE)
    words.append(_mips_r(s0, t3, t4, 0, 0x24))  # and t4, s0, t3
    words.append((0x10 << 26) | (4 << 21) | (t4 << 16) | (12 << 11))  # mtc0 t4, Status
    words.append(0)

    li(t0, 0xB0003000)
    li(t1, BBK_RTC_ALARM_IRQ_PROBE_STATUS_VA)
    li(t2, 0xB0001000)

    li(t3, 0x00000000)
    words.append(_mips_sw(t3, 0x00, t0))  # RTCCR disabled; clear flags
    words.append(_mips_sw(t3, 0x20, t0))  # HCR clear
    words.append(_mips_sw(t3, 0x2C, t0))  # HWCR clear
    words.append(_mips_sw(t3, 0x30, t0))  # HWRSR clear writable bits

    li(t3, 0x00001000)
    words.append(_mips_sw(t3, 0x04, t0))  # RTCSR
    words.append(_mips_sw(t3, 0x08, t0))  # RTCSAR
    li(t3, 0x0000000D)  # RTCE | AE | AIE
    words.append(_mips_sw(t3, 0x00, t0))
    li(t3, rtc_irq_bit)
    words.append(_mips_sw(t3, 0x0C, t2))  # ICMCR: unmask RTC source

    words.append(_mips_lw(v0, 0x00, t0))
    words.append(_mips_sw(v0, 0, t1))
    read_intc_masked(4, 0x00)   # ICSR RTC bit
    read_intc_masked(8, 0x10)   # ICPR RTC bit

    li(t3, 0x00002000)
    words.append(_mips_sw(t3, 0x08, t0))  # move alarm away; clears AF
    li(t3, 0x00000001)  # RTCE only; disable alarm IRQ
    words.append(_mips_sw(t3, 0x00, t0))
    words.append(_mips_lw(v0, 0x00, t0))
    words.append(_mips_sw(v0, 12, t1))
    read_intc_masked(16, 0x00)
    read_intc_masked(20, 0x10)

    li(t3, 0x00000000)
    words.append(_mips_sw(t3, 0x00, t0))
    words.append(_mips_sw(t3, 0x20, t0))
    words.append(_mips_sw(t3, 0x2C, t0))
    words.append(_mips_sw(t3, 0x30, t0))
    li(t3, 0x00003000)
    words.append(_mips_sw(t3, 0x04, t0))
    words.append(_mips_sw(t3, 0x08, t0))
    li(t3, 0x00000001)
    words.append(_mips_sw(t3, 0x2C, t0))  # HWCR.EALM
    li(t3, 0x00000005)  # RTCE | AE
    words.append(_mips_sw(t3, 0x00, t0))
    li(t3, 0x00000001)
    words.append(_mips_sw(t3, 0x20, t0))  # enter hibernate
    words.append(_mips_lw(v0, 0x00, t0))  # latch alarm and hibernate wake
    words.append(_mips_sw(v0, 24, t1))
    words.append(_mips_lw(v0, 0x20, t0))
    words.append(_mips_sw(v0, 28, t1))
    words.append(_mips_lw(v0, 0x30, t0))
    words.append(_mips_sw(v0, 32, t1))

    words.append((0x10 << 26) | (4 << 21) | (s0 << 16) | (12 << 11))  # mtc0 s0, Status
    words.append(0)
    words.append(_mips_r(ra, 0, 0, 0, 0x08))
    words.append(0)
    return _pack_mips_words(words)


def _run_rtc_alarm_irq_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-rtc-alarm-irq-probe",
        "code": _format_u32(BBK_RTC_ALARM_IRQ_PROBE_CODE_VA),
        "status_buffer": _format_u32(BBK_RTC_ALARM_IRQ_PROBE_STATUS_VA),
        "ok": False,
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            code = _build_rtc_alarm_irq_probe_code()
            backend._write_virtual_memory_paused_locked(BBK_RTC_ALARM_IRQ_PROBE_CODE_VA, code)
            backend._write_virtual_memory_paused_locked(BBK_RTC_ALARM_IRQ_PROBE_STATUS_VA, b"\x00" * 36)
            row["code_size"] = len(code)
            call = backend._call_guest_function_stepped_paused_locked(
                BBK_RTC_ALARM_IRQ_PROBE_CODE_VA,
                return_pc=BBK_WAIT_PROBE_PC,
                max_steps=180,
                max_recorded_steps=12,
            )
            row["guest_call"] = call
            status_data = backend._read_virtual_memory_paused_locked(
                BBK_RTC_ALARM_IRQ_PROBE_STATUS_VA, 36
            )
            status_words = list(struct.unpack("<9I", status_data))
            row["status_words"] = [_format_u32(value) for value in status_words]
            row["status_lifecycle_ok"] = (
                status_words[0] == 0x0000009D
                and status_words[1] == 0x00008000
                and status_words[2] == 0x00008000
                and status_words[3] == 0x00000081
                and status_words[4] == 0x00000000
                and status_words[5] == 0x00000000
                and status_words[6] == 0x00000095
                and status_words[7] == 0x00000000
                and status_words[8] == 0x00000001
            )
            row["ok"] = bool(call.get("returned")) and bool(row["status_lifecycle_ok"])
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        if not row["ok"]:
            row["error"] = "RTC alarm IRQ or hibernate alarm wake state did not match JZ4740 semantics"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _build_bch_status_probe_code() -> bytes:
    t0 = 8
    t1 = 9
    t2 = 10
    t3 = 11
    t4 = 12
    v0 = 2
    ra = 31
    words: list[int] = []

    def li(reg: int, value: int) -> None:
        words.extend(_mips_li(reg, value))

    li(t0, 0xB3010000)
    li(t1, BBK_BCH_STATUS_PROBE_STATUS_VA)
    words.append(_mips_lw(v0, 0x114, t0))
    words.append(_mips_sw(v0, 0, t1))
    li(t3, 0x0D)
    words.append(_mips_sw(t3, 0x114, t0))
    words.append(_mips_lw(v0, 0x114, t0))
    words.append(_mips_sw(v0, 4, t1))

    li(t2, 0xB8008000)
    li(t4, 0xB8010000)
    li(t3, 0x00)
    words.append(_mips_sb(t3, 0, t2))
    words.append(_mips_sb(t3, 0, t4))
    words.append(_mips_sb(t3, 0, t4))
    words.append(_mips_sb(t3, 0, t4))
    words.append(_mips_sb(t3, 0, t4))
    words.append(_mips_sb(t3, 0, t4))
    li(t3, 0x30)
    words.append(_mips_sb(t3, 0, t2))
    words.append(_mips_lw(v0, 0x114, t0))
    words.append(_mips_sw(v0, 8, t1))
    words.append(_mips_lw(v0, 0x114, t0))
    words.append(_mips_sw(v0, 12, t1))
    li(t3, 0x0D)
    words.append(_mips_sw(t3, 0x114, t0))
    words.append(_mips_lw(v0, 0x114, t0))
    words.append(_mips_sw(v0, 16, t1))

    words.append(_mips_r(ra, 0, 0, 0, 0x08))
    words.append(0)
    return _pack_mips_words(words)


def _run_bch_status_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-bch-status-probe",
        "code": _format_u32(BBK_BCH_STATUS_PROBE_CODE_VA),
        "status_buffer": _format_u32(BBK_BCH_STATUS_PROBE_STATUS_VA),
        "ok": False,
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            code = _build_bch_status_probe_code()
            backend._write_virtual_memory_paused_locked(BBK_BCH_STATUS_PROBE_CODE_VA, code)
            backend._write_virtual_memory_paused_locked(BBK_BCH_STATUS_PROBE_STATUS_VA, b"\x00" * 20)
            row["code_size"] = len(code)
            call = backend._call_guest_function_stepped_paused_locked(
                BBK_BCH_STATUS_PROBE_CODE_VA,
                return_pc=BBK_WAIT_PROBE_PC,
                max_steps=64,
                max_recorded_steps=16,
            )
            row["guest_call"] = call
            status_data = backend._read_virtual_memory_paused_locked(
                BBK_BCH_STATUS_PROBE_STATUS_VA, 20
            )
            status_words = list(struct.unpack("<5I", status_data))
            row["status_words"] = [_format_u32(value) for value in status_words]
            row["status_lifecycle_ok"] = (
                status_words[0] in (0, 0x0D)
                and status_words[1] == 0
                and status_words[2] == 0
                and status_words[3] == 0x0D
                and status_words[4] == 0
            )
            row["ok"] = bool(call.get("returned")) and bool(row["status_lifecycle_ok"])
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        if not row["ok"]:
            row["error"] = "BCH/ECC status lifecycle did not match initial -> ack -> busy -> ready -> ack"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _build_lcd_status_ack_probe_code() -> bytes:
    t0 = 8
    t1 = 9
    t2 = 10
    v0 = 2
    ra = 31
    words: list[int] = []

    def li(reg: int, value: int) -> None:
        words.extend(_mips_li(reg, value))

    li(t0, 0xB0043000)
    li(t1, BBK_LCD_FRAME_DONE_PROBE_STATUS_VA)
    words.append(_mips_lw(v0, 0x0C, t0))
    words.append(_mips_sw(v0, 0, t1))
    li(t2, 1)
    words.append(_mips_sw(t2, 0x0C, t0))
    words.append(_mips_lw(v0, 0x0C, t0))
    words.append(_mips_sw(v0, 4, t1))
    words.append(_mips_r(ra, 0, 0, 0, 0x08))
    words.append(0)
    return _pack_mips_words(words)


def _build_lcd_status_read_probe_code() -> bytes:
    t0 = 8
    t1 = 9
    v0 = 2
    ra = 31
    words: list[int] = []

    def li(reg: int, value: int) -> None:
        words.extend(_mips_li(reg, value))

    li(t0, 0xB0043000)
    li(t1, BBK_LCD_FRAME_DONE_PROBE_STATUS_VA)
    words.append(_mips_lw(v0, 0x0C, t0))
    words.append(_mips_sw(v0, 8, t1))
    words.append(_mips_r(ra, 0, 0, 0, 0x08))
    words.append(0)
    return _pack_mips_words(words)


def _lcd_mirror_config_for_frame(frame_va: int) -> bytes:
    data = bytearray(0xE0)
    struct.pack_into("<H", data, 0x00, 240)
    struct.pack_into("<H", data, 0x04, 320)
    struct.pack_into("<I", data, 0xD8, frame_va & 0xFFFFFFFF)
    data[0xDC] = 1
    return bytes(data)


def _run_lcd_frame_done_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-lcd-frame-done-probe",
        "code": _format_u32(BBK_LCD_FRAME_DONE_PROBE_CODE_VA),
        "status_buffer": _format_u32(BBK_LCD_FRAME_DONE_PROBE_STATUS_VA),
        "framebuffer": _format_u32(BBK_LCD_FRAME_DONE_PROBE_FRAME_VA),
        "ok": False,
    }

    def call_probe_code_with_irqs_disabled(
        code_va: int,
        *,
        max_steps: int,
        max_recorded_steps: int,
    ) -> dict[str, object]:
        cp0_status = backend._read_register_paused_locked(32) & 0xFFFFFFFF
        backend.gdb_register_read_count += 1
        backend._write_register_paused_locked(32, cp0_status & ~0x1)
        backend.gdb_register_write_count += 1
        try:
            call = backend._call_guest_function_stepped_paused_locked(
                code_va,
                return_pc=BBK_WAIT_PROBE_PC,
                max_steps=max_steps,
                max_recorded_steps=max_recorded_steps,
            )
            call["cp0_status_before"] = _format_u32(cp0_status)
            return call
        finally:
            backend._write_register_paused_locked(32, cp0_status)
            backend.gdb_register_write_count += 1

    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            backend._write_virtual_memory_paused_locked(
                BBK_LCD_MIRROR_CONFIG_VA,
                _lcd_mirror_config_for_frame(BBK_LCD_FRAME_DONE_PROBE_FRAME_VA),
            )
            backend._write_virtual_memory_paused_locked(
                BBK_LCD_FRAME_DONE_PROBE_STATUS_VA,
                b"\x00" * 12,
            )
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_baseline_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(0.55)
        with backend._lock:
            backend._pause_for_gdb_locked()
            ack_code = _build_lcd_status_ack_probe_code()
            backend._write_virtual_memory_paused_locked(
                BBK_LCD_FRAME_DONE_PROBE_CODE_VA,
                ack_code,
            )
            row["ack_code_size"] = len(ack_code)
            ack_call = call_probe_code_with_irqs_disabled(
                BBK_LCD_FRAME_DONE_PROBE_CODE_VA,
                max_steps=256,
                max_recorded_steps=16,
            )
            row["ack_guest_call"] = ack_call
            backend._write_virtual_memory_paused_locked(
                BBK_LCD_FRAME_DONE_PROBE_FRAME_VA,
                b"\xff\xff\x00\x00",
            )
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_changed_error"] = f"{type(exc).__name__}: {exc}"
        time.sleep(0.55)
        with backend._lock:
            backend._pause_for_gdb_locked()
            read_code = _build_lcd_status_read_probe_code()
            backend._write_virtual_memory_paused_locked(
                BBK_LCD_FRAME_DONE_PROBE_CODE_VA,
                read_code,
            )
            row["read_code_size"] = len(read_code)
            read_call = call_probe_code_with_irqs_disabled(
                BBK_LCD_FRAME_DONE_PROBE_CODE_VA,
                max_steps=24,
                max_recorded_steps=12,
            )
            row["read_guest_call"] = read_call
            status_data = backend._read_virtual_memory_paused_locked(
                BBK_LCD_FRAME_DONE_PROBE_STATUS_VA, 12
            )
            status_words = list(struct.unpack("<3I", status_data))
            row["status_words"] = [_format_u32(value) for value in status_words]
            row["baseline_frame_done"] = bool(status_words[0] & 0x01)
            row["ack_cleared_frame_done"] = (status_words[1] & 0x01) == 0
            row["changed_frame_done"] = bool(status_words[2] & 0x01)
            row["ready_bits_ok"] = all((value & 0x80) != 0 for value in status_words)
            row["ok"] = (
                bool(ack_call.get("returned"))
                and bool(read_call.get("returned"))
                and bool(row["baseline_frame_done"])
                and bool(row["ack_cleared_frame_done"])
                and bool(row["changed_frame_done"])
                and bool(row["ready_bits_ok"])
            )
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        if not row["ok"]:
            row["error"] = "LCD frame-done did not follow baseline -> ack clear -> changed-frame set"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _build_key_gpio_ack_probe_code(status_offset: int = 0) -> bytes:
    t0 = 8
    t1 = 9
    t2 = 10
    t3 = 11
    v0 = 2
    ra = 31
    words: list[int] = []

    def li(reg: int, value: int) -> None:
        words.extend(_mips_li(reg, value))

    li(t0, 0xB0010000)
    li(t1, 0xB0001000)
    li(t2, BBK_KEY_GPIO_PROBE_STATUS_VA + status_offset)
    li(t3, BBK_KEY_GPIO_PROBE_MASK)
    words.append(_mips_lw(v0, 0x100, t0))
    words.append(_mips_sw(v0, 0, t2))
    words.append(_mips_lw(v0, 0x180, t0))
    words.append(_mips_sw(v0, 4, t2))
    words.append(_mips_lw(v0, 0x010, t1))
    words.append(_mips_sw(v0, 8, t2))
    words.append(_mips_sw(t3, 0x114, t0))
    words.append(_mips_lw(v0, 0x180, t0))
    words.append(_mips_sw(v0, 12, t2))
    words.append(_mips_lw(v0, 0x010, t1))
    words.append(_mips_sw(v0, 16, t2))
    words.append(_mips_r(ra, 0, 0, 0, 0x08))
    words.append(0)
    return _pack_mips_words(words)


def _run_key_gpio_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-key-gpio-probe",
        "key_code": BBK_KEY_GPIO_PROBE_CODE,
        "gpio_mask": _format_u32(BBK_KEY_GPIO_PROBE_MASK),
        "irq_mask": _format_u32(BBK_KEY_GPIO_PROBE_IRQ_MASK),
        "code": _format_u32(BBK_KEY_GPIO_PROBE_CODE_VA),
        "status_buffer": _format_u32(BBK_KEY_GPIO_PROBE_STATUS_VA),
        "ok": False,
    }

    def send_key_and_sample(down: bool, status_offset: int) -> tuple[dict[str, object], dict[str, object], list[int]]:
        with backend._lock:
            sent = backend._send_bbk_input_locked(
                f"K {BBK_KEY_GPIO_PROBE_CODE} {1 if down else 0}"
            )
            time.sleep(0.01)
            backend._pause_for_gdb_locked()
            try:
                code = _build_key_gpio_ack_probe_code(status_offset)
                backend._write_virtual_memory_paused_locked(
                    BBK_KEY_GPIO_PROBE_CODE_VA,
                    code,
                )
                call = backend._call_guest_function_stepped_paused_locked(
                    BBK_KEY_GPIO_PROBE_CODE_VA,
                    return_pc=BBK_WAIT_PROBE_PC,
                    max_steps=40,
                    max_recorded_steps=12,
                )
                data = backend._read_virtual_memory_paused_locked(
                    BBK_KEY_GPIO_PROBE_STATUS_VA + status_offset,
                    20,
                )
                words = list(struct.unpack("<5I", data))
            finally:
                backend._resume_after_gdb_locked()
        event = {
            "event": "qemu-key-state",
            "key_code": BBK_KEY_GPIO_PROBE_CODE,
            "down": down,
            "applied": bool(sent),
            "source": "qemu-c-machine-chardev",
        }
        return event, call, words

    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            backend._write_virtual_memory_paused_locked(
                BBK_KEY_GPIO_PROBE_STATUS_VA,
                b"\x00" * 40,
            )
            backend._resume_after_gdb_locked()

        down_event, down_call, down_words = send_key_and_sample(True, 0)
        release_event, release_call, release_words = send_key_and_sample(False, 20)

        def word_map(words: list[int]) -> dict[str, str]:
            names = (
                "level",
                "flag",
                "pending",
                "flag_after_ack",
                "pending_after_ack",
            )
            return {name: _format_u32(value) for name, value in zip(names, words)}

        row["down_event"] = down_event
        row["release_event"] = release_event
        row["down_guest_call"] = down_call
        row["release_guest_call"] = release_call
        row["down_words"] = word_map(down_words)
        row["release_words"] = word_map(release_words)
        row["down_level_active_low"] = (down_words[0] & BBK_KEY_GPIO_PROBE_MASK) == 0
        row["down_flag_set"] = bool(down_words[1] & BBK_KEY_GPIO_PROBE_MASK)
        row["down_pending_set"] = bool(down_words[2] & BBK_KEY_GPIO_PROBE_IRQ_MASK)
        row["down_ack_cleared_flag"] = (down_words[3] & BBK_KEY_GPIO_PROBE_MASK) == 0
        row["down_ack_cleared_pending"] = (down_words[4] & BBK_KEY_GPIO_PROBE_IRQ_MASK) == 0
        row["release_level_restored"] = (release_words[0] & BBK_KEY_GPIO_PROBE_MASK) == BBK_KEY_GPIO_PROBE_MASK
        row["release_flag_set"] = bool(release_words[1] & BBK_KEY_GPIO_PROBE_MASK)
        row["release_pending_set"] = bool(release_words[2] & BBK_KEY_GPIO_PROBE_IRQ_MASK)
        row["release_ack_cleared_flag"] = (release_words[3] & BBK_KEY_GPIO_PROBE_MASK) == 0
        row["release_ack_cleared_pending"] = (release_words[4] & BBK_KEY_GPIO_PROBE_IRQ_MASK) == 0
        row["ok"] = (
            bool(down_event.get("applied"))
            and bool(release_event.get("applied"))
            and bool(down_call.get("returned"))
            and bool(release_call.get("returned"))
            and bool(row["down_level_active_low"])
            and bool(row["down_flag_set"])
            and bool(row["down_pending_set"])
            and bool(row["down_ack_cleared_flag"])
            and bool(row["down_ack_cleared_pending"])
            and bool(row["release_level_restored"])
            and bool(row["release_flag_set"])
            and bool(row["release_pending_set"])
            and bool(row["release_ack_cleared_flag"])
            and bool(row["release_ack_cleared_pending"])
        )
        if not row["ok"]:
            row["error"] = "key GPIO/INTC did not follow down/ack/release/ack lifecycle"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _capture_config_cluster_watch_hit(
    backend: QemuProcessBackend,
    *,
    pc: int,
    stop_reply: str,
    object_va: int,
) -> dict[str, object]:
    regs = {
        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
        "v1": backend._read_register_paused_locked(3) & 0xFFFFFFFF,
        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
        "s0": backend._read_register_paused_locked(16) & 0xFFFFFFFF,
        "s1": backend._read_register_paused_locked(17) & 0xFFFFFFFF,
        "s2": backend._read_register_paused_locked(18) & 0xFFFFFFFF,
        "s3": backend._read_register_paused_locked(19) & 0xFFFFFFFF,
        "s4": backend._read_register_paused_locked(20) & 0xFFFFFFFF,
        "s5": backend._read_register_paused_locked(21) & 0xFFFFFFFF,
        "s6": backend._read_register_paused_locked(22) & 0xFFFFFFFF,
        "s7": backend._read_register_paused_locked(23) & 0xFFFFFFFF,
        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
        "fp": backend._read_register_paused_locked(30) & 0xFFFFFFFF,
        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
    }
    parent_sp = (regs["sp"] + 0x48) & 0xFFFFFFFF
    grandparent_sp = (parent_sp + 0x38) & 0xFFFFFFFF
    file_rw_frame: dict[str, object] | None = None
    parent_frame: dict[str, object] | None = None
    grandparent_frame: dict[str, object] | None = None
    if 0x80000000 <= regs["sp"] < 0x8A000000:
        file_rw_frame = {
            "sp": _format_u32(regs["sp"]),
            "caller_80171154_s1_buffer": _format_u32(
                backend._read_u32_paused_locked(regs["sp"] + 0x24)
            ),
            "caller_80171154_s5_object": _format_u32(
                backend._read_u32_paused_locked(regs["sp"] + 0x34)
            ),
            "caller_80171154_s6_count": _format_u32(
                backend._read_u32_paused_locked(regs["sp"] + 0x38)
            ),
            "caller_80171154_s7_tail": _format_u32(
                backend._read_u32_paused_locked(regs["sp"] + 0x3c)
            ),
            "return_to_80171154": _format_u32(
                backend._read_u32_paused_locked(regs["sp"] + 0x44)
            ),
        }
    if 0x80000000 <= parent_sp < 0x8A000000:
        parent_frame = {
            "sp": _format_u32(parent_sp),
            "saved_previous_s1": _format_u32(
                backend._read_u32_paused_locked(parent_sp + 0x14)
            ),
            "saved_previous_s5": _format_u32(
                backend._read_u32_paused_locked(parent_sp + 0x24)
            ),
            "saved_previous_s6": _format_u32(
                backend._read_u32_paused_locked(parent_sp + 0x28)
            ),
            "saved_previous_s7": _format_u32(
                backend._read_u32_paused_locked(parent_sp + 0x2c)
            ),
            "saved_ra": _format_u32(
                backend._read_u32_paused_locked(parent_sp + 0x30)
            ),
            "words_00_34": _read_optional_words(backend, parent_sp, 14),
        }
    if 0x80000000 <= grandparent_sp < 0x8A000000:
        grandparent_frame = {
            "sp": _format_u32(grandparent_sp),
            "saved_previous_s1": _format_u32(
                backend._read_u32_paused_locked(grandparent_sp + 0x1c)
            ),
            "saved_previous_s2": _format_u32(
                backend._read_u32_paused_locked(grandparent_sp + 0x20)
            ),
            "saved_previous_s3": _format_u32(
                backend._read_u32_paused_locked(grandparent_sp + 0x24)
            ),
            "saved_previous_s4": _format_u32(
                backend._read_u32_paused_locked(grandparent_sp + 0x28)
            ),
            "saved_ra": _format_u32(
                backend._read_u32_paused_locked(grandparent_sp + 0x2c)
            ),
            "words_00_2c": _read_optional_words(backend, grandparent_sp, 12),
        }
    if pc == 0x801780F8:
        write_class = "fat-free-cluster-allocation"
    elif 0x80006BD0 <= pc <= 0x80006C20:
        write_class = "object-init-zero-fill"
    else:
        write_class = "other-write"
    return {
        "pc": _format_u32(pc),
        "write_class": write_class,
        "region": classify_guest_pc(_format_u32(pc)).get("region"),
        "stop_reply": stop_reply,
        "object_va": _format_u32(object_va),
        "watch_va": _format_u32((object_va + 0x18) & 0xFFFFFFFF),
        "registers": {key: _format_u32(value) for key, value in regs.items()},
        "file_object_decoded": _decode_file_object(backend, object_va),
        "file_object_words_00_4c": _read_optional_words(backend, object_va, 20),
        "stack_words_00_8c": _read_optional_words(backend, regs["sp"], 36),
        "file_rw_live_frame": file_rw_frame,
        "parent_saved_ra_guess": _format_u32(
            backend._read_u32_paused_locked((regs["sp"] + 0x78) & 0xFFFFFFFF)
        )
        if 0x80000000 <= regs["sp"] < 0x8A000000
        else None,
        "parent_80171154_frame": parent_frame,
        "grandparent_8017ab2c_frame": grandparent_frame,
        "resource_cache_slots": _decode_cache_table(
            backend, BBK_CACHE_SCAN_TABLE_VA, slots=8, buffer_words=4
        ),
        "cluster_cache_slots": _decode_cache_table(
            backend, BBK_CLUSTER_CACHE_TABLE_VA, slots=6, buffer_words=4
        ),
        "storage_trace": _storage_trace_snapshot(backend),
        "nand_target_trace": _nand_target_trace_snapshot(backend),
    }


def _run_config_cluster_watch_probe(
    config,
    *,
    timeout: float | None = None,
    calibrate_first: bool = False,
    settle_seconds: float = 0.5,
    hold_seconds: float = 0.5,
) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-config-cluster-watch-probe",
        "find_breakpoint": _format_u32(BBK_FILE_RW_ENTRY_PC),
        "watch_offset": "0x18",
        "ok": False,
        "found_config_object": False,
        "watch_hit": False,
        "calibration_points": [],
    }
    deadline_seconds = float(timeout if timeout is not None else config.timeout_seconds)
    try:
        backend.start()

        def feed_calibration() -> None:
            time.sleep(0.5)
            for index, (x, y, raw_x, raw_y) in enumerate(TOUCH_CALIBRATION_REFERENCE_POINTS):
                point: dict[str, object] = {
                    "index": index,
                    "x": x,
                    "y": y,
                    "expected_raw_x": f"0x{raw_x:04x}",
                    "expected_raw_y": f"0x{raw_y:04x}",
                }
                point["down"] = {
                    "applied": backend._send_bbk_input_locked(
                        f"T {x} {y} {raw_x} {raw_y} 1"
                    ),
                    "source": "qemu-c-machine-chardev",
                }
                time.sleep(hold_seconds)
                point["up"] = {
                    "applied": backend._send_bbk_input_locked(
                        f"T {x} {y} {raw_x} {raw_y} 0"
                    ),
                    "source": "qemu-c-machine-chardev",
                }
                row["calibration_points"].append(point)
                time.sleep(settle_seconds)

        feeder = (
            threading.Thread(target=feed_calibration, daemon=True)
            if calibrate_first
            else None
        )
        if feeder is not None:
            feeder.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted_bp = False
            inserted_watch = False
            object_va = 0
            watch_va = 0
            try:
                _gdb_insert_breakpoint(backend.gdb_sock, BBK_FILE_RW_ENTRY_PC)
                inserted_bp = True
                deadline = time.time() + deadline_seconds
                while time.time() < deadline:
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock,
                            timeout=max(0.2, deadline - time.time()),
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_config_object"] = True
                        _gdb_interrupt(backend.gdb_sock)
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    if pc != BBK_FILE_RW_ENTRY_PC:
                        row["unexpected_find_pc"] = _format_u32(pc)
                        break
                    candidate = backend._read_register_paused_locked(4) & 0xFFFFFFFF
                    decoded = _decode_file_object(backend, candidate)
                    if (
                        isinstance(decoded, dict)
                        and tuple(decoded.get("name_words", ())) == BBK_CONFIG_INF_NAME_WORDS
                    ):
                        object_va = candidate
                        watch_va = (object_va + 0x18) & 0xFFFFFFFF
                        row["found_config_object"] = True
                        row["config_object_va"] = _format_u32(object_va)
                        row["config_object_at_find"] = decoded
                        row["find_stop_reply"] = stop_reply
                        _gdb_remove_breakpoint(backend.gdb_sock, BBK_FILE_RW_ENTRY_PC)
                        inserted_bp = False
                        _gdb_insert_write_watchpoint(backend.gdb_sock, watch_va, 4)
                        inserted_watch = True
                        break
                    _gdb_remove_breakpoint(backend.gdb_sock, BBK_FILE_RW_ENTRY_PC)
                    inserted_bp = False
                    backend._step_paused_locked()
                    _gdb_insert_breakpoint(backend.gdb_sock, BBK_FILE_RW_ENTRY_PC)
                    inserted_bp = True
                if inserted_watch:
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock,
                            timeout=max(0.2, deadline - time.time()),
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_watchpoint"] = True
                        _gdb_interrupt(backend.gdb_sock)
                    else:
                        pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                        row["watch_hit"] = True
                        row["watch_event"] = _capture_config_cluster_watch_hit(
                            backend,
                            pc=pc,
                            stop_reply=stop_reply,
                            object_va=object_va,
                        )
                row["ok"] = bool(row.get("found_config_object")) and "error" not in row
            finally:
                if inserted_watch:
                    try:
                        _gdb_remove_write_watchpoint(backend.gdb_sock, watch_va, 4)
                    except Exception as exc:
                        row["watch_remove_error"] = f"{type(exc).__name__}: {exc}"
                if inserted_bp:
                    try:
                        _gdb_remove_breakpoint(backend.gdb_sock, BBK_FILE_RW_ENTRY_PC)
                    except Exception as exc:
                        row["breakpoint_remove_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _capture_file_open_context_hit(
    backend: QemuProcessBackend, pc: int, stop_reply: str
) -> dict[str, object]:
    regs = {
        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
        "v1": backend._read_register_paused_locked(3) & 0xFFFFFFFF,
        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
        "s0": backend._read_register_paused_locked(16) & 0xFFFFFFFF,
        "s1": backend._read_register_paused_locked(17) & 0xFFFFFFFF,
        "s2": backend._read_register_paused_locked(18) & 0xFFFFFFFF,
        "s3": backend._read_register_paused_locked(19) & 0xFFFFFFFF,
        "s4": backend._read_register_paused_locked(20) & 0xFFFFFFFF,
        "s5": backend._read_register_paused_locked(21) & 0xFFFFFFFF,
        "s6": backend._read_register_paused_locked(22) & 0xFFFFFFFF,
        "s7": backend._read_register_paused_locked(23) & 0xFFFFFFFF,
        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
        "fp": backend._read_register_paused_locked(30) & 0xFFFFFFFF,
        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
    }
    kind_by_pc = {
        0x801714EC: "file-open-entry",
        0x8017159C: "construct-return",
        0x80171618: "open-branch-1618",
        0x80171620: "open-branch-1620",
        0x80171640: "open-branch-1640",
        0x801716FC: "file-context-call-site",
        0x80171708: "file-context-first-return",
        0x80171718: "file-context-second-call",
        0x80171724: "file-context-second-return",
        0x8017172C: "file-open-context-lookup",
        0x80171744: "file-open-context-rebuild-call",
        0x80171754: "file-open-context-rebuild-return",
        0x80171154: "resource-read-chunk",
        BBK_FILE_RW_ENTRY_PC: "file-rw-entry",
        0x8017C920: "fat-cleanup-entry",
        0x8017AB2C: "resource-load-call",
        0x80173504: "resource-construct-entry",
        0x80173628: "resource-construct-dir-sector-read-return",
        0x8017374C: "resource-construct-dirent-match-call",
        0x801738BC: "resource-construct-dirent-hit",
        0x80173920: "resource-construct-dirent-copy-return",
        0x8017395C: "resource-construct-cluster-read-call",
        0x80173964: "resource-construct-cluster-read-return",
        0x80173E84: "resource-construct-file-entry-object-copy-call",
        0x80173EA0: "resource-construct-file-entry-output-write",
        0x80173ED0: "resource-construct-file-entry-return",
        0x801747C4: "resource-state-builder-entry",
        0x80174B9C: "resource-state-build-loop-entry",
        0x80174DDC: "resource-state-current-cluster-store",
        0x80175E40: "resource-dirent-to-file-entry-copy-entry",
        0x801753BC: "resource-context-rebuild-entry",
        0x80175428: "resource-context-read-return",
        0x801754A0: "resource-context-read-fail",
        0x80175504: "resource-context-rebuild-mode1",
    }
    candidates = {
        name: value
        for name, value in {
            "v0": regs["v0"],
            "a0": regs["a0"],
            "a1": regs["a1"],
            "a2": regs["a2"],
            "a3": regs["a3"],
            "s0": regs["s0"],
            "s3": regs["s3"],
            "s6": regs["s6"],
            "fp": regs["fp"],
        }.items()
        if 0x80000000 <= value < 0x8A000000
    }
    decoded = {
        name: _decode_file_object(backend, value)
        for name, value in candidates.items()
    }
    row: dict[str, object] = {
        "pc": _format_u32(pc),
        "kind": kind_by_pc.get(pc, "file-open-context"),
        "region": classify_guest_pc(_format_u32(pc)).get("region"),
        "stop_reply": stop_reply,
        "registers": {key: _format_u32(value) for key, value in regs.items()},
        "decoded_candidates": decoded,
        "file_context_decoded": _decode_file_context(backend, regs["s6"]),
        "a2_context_decoded": _decode_file_context(backend, regs["a2"]),
        "a3_context_decoded": _decode_file_context(backend, regs["a3"]),
        "candidate_contexts": {
            name: _pointer_context(backend, value)
            for name, value in candidates.items()
        },
        "storage_trace": _storage_trace_snapshot(backend),
        "cluster_trace": _cluster_trace_snapshot(backend),
        "resource_cache_slots": _decode_cache_table(
            backend, BBK_CACHE_SCAN_TABLE_VA, slots=8, buffer_words=4
        ),
        "cluster_cache_slots": _decode_cache_table(
            backend, BBK_CLUSTER_CACHE_TABLE_VA, slots=6, buffer_words=4
        ),
    }
    if pc in {
        BBK_RESOURCE_CONSTRUCT_DIRENT_MATCH_CALL_PC,
        BBK_RESOURCE_CONSTRUCT_DIRENT_HIT_PC,
        BBK_RESOURCE_CONSTRUCT_DIRENT_COPY_RETURN_PC,
    }:
        dirent_va = regs["s1"]
        row["dirent_pointer"] = _format_u32(dirent_va)
        row["dirent_raw_00_1f"] = (
            data.hex()
            if 0x80000000 <= dirent_va < 0x8A000000
            and (data := _read_optional_bytes(backend, dirent_va, 0x20)) is not None
            else None
        )
    if pc == BBK_RESOURCE_CONSTRUCT_DIRENT_COPY_RETURN_PC:
        firmware_dirent = (regs["sp"] + 0x68) & 0xFFFFFFFF
        row["firmware_dirent_pointer"] = _format_u32(firmware_dirent)
        row["firmware_dirent_words_68_90"] = _read_optional_words(
            backend, firmware_dirent, 10
        )
        row["firmware_dirent_words_68_b0"] = _read_optional_words(
            backend, firmware_dirent, 18
        )
        row["firmware_dirent_raw_68_87"] = (
            data.hex()
            if (data := _read_optional_bytes(backend, firmware_dirent, 0x20))
            is not None
            else None
        )
    if pc in {
        BBK_RESOURCE_CONSTRUCT_CLUSTER_READ_CALL_PC,
        BBK_RESOURCE_CONSTRUCT_CLUSTER_READ_RETURN_PC,
    }:
        try:
            cluster_buffer = backend._read_u32_paused_locked(regs["sp"] + 0x94)
        except Exception:
            cluster_buffer = 0
        row["cluster_read_args"] = {
            "mode": _format_u32(regs["a0"]),
            "cluster": _format_u32(regs["a1"]),
            "buffer_arg": _format_u32(regs["a2"]),
            "stack_buffer": _format_u32(cluster_buffer),
        }
        if 0x80000000 <= cluster_buffer < 0x8A000000:
            row["cluster_buffer_words_00_3c"] = _read_optional_words(
                backend, cluster_buffer, 16
            )
    if pc in {0x80173E84, 0x80173EA0, 0x80173ED0}:
        row["resource_object_context"] = _pointer_context(backend, regs["fp"])
        row["resource_object_words_00_50"] = _read_optional_words(
            backend, regs["fp"], 20
        )
        obj_words = row["resource_object_words_00_50"]
        if isinstance(obj_words, list) and len(obj_words) > 13:
            row["file_entry_cluster_state"] = {
                "cluster_18": obj_words[6],
                "size_20": obj_words[8] if len(obj_words) > 8 else None,
                "size_2c": obj_words[11],
                "current_cluster_30": obj_words[12],
                "aux_cluster_34": obj_words[13],
                "current_matches_file_cluster": obj_words[6] == obj_words[12] == obj_words[13],
            }
        row["construct_stack_words_58_68"] = _read_optional_words(
            backend, regs["sp"] + 0x58, 4
        )
        row["output_arg_words"] = _read_optional_words(backend, regs["sp"] + 0xF8, 1)
        try:
            output_arg = backend._read_u32_paused_locked(regs["sp"] + 0xF8)
        except Exception:
            output_arg = 0
        row["output_arg"] = _format_u32(output_arg)
        if 0x80000000 <= output_arg < 0x8A000000:
            row["output_arg_target_words"] = _read_optional_words(
                backend, output_arg, 4
            )
    if pc in {0x801747C4, 0x80174B9C, 0x80174DDC, 0x80175E40}:
        row["resource_state_builder"] = {
            "a2_context": _pointer_context(backend, regs["a2"]),
            "s7_context": _pointer_context(backend, regs["s7"]),
            "s7_words_00_1c": _read_optional_words(backend, regs["s7"], 8),
            "sp_words_40_80": _read_optional_words(backend, regs["sp"] + 0x40, 16),
            "sp_words_58_68": _read_optional_words(backend, regs["sp"] + 0x58, 4),
            "s4": _format_u32(regs["s4"]),
            "s5": _format_u32(regs["s5"]),
            "s6": _format_u32(regs["s6"]),
        }
    if pc in {0x801753BC, 0x80175428, 0x801754A0, 0x80175504}:
        row["resource_context_contexts"] = {
            name: _decode_file_context(backend, value)
            for name, value in {
                "a2": regs["a2"],
                "s4": regs["s4"],
                "s6": regs["s6"],
            }.items()
        }
        buffer_candidates = {
            name: value
            for name, value in {
                "a1": regs["a1"],
                "a2": regs["a2"],
                "s3": regs["s3"],
            }.items()
            if 0x80000000 <= value < 0x8A000000
        }
        row["resource_context_buffers"] = {}
        for name, value in buffer_candidates.items():
            row["resource_context_buffers"][name] = {
                "addr": _format_u32(value),
                "words_00_3c": _read_optional_words(backend, value, 16),
                "bytes_00_7f": (
                    data.hex()
                    if (data := _read_optional_bytes(backend, value, 0x80))
                    is not None
                    else None
                ),
            }
    if pc == 0x8017AB2C:
        row["resource_load_call"] = {
            "a0_context": _pointer_context(backend, regs["a0"]),
            "a1_context": _pointer_context(backend, regs["a1"]),
            "s0_object": _pointer_context(backend, regs["s0"]),
            "s5_object": _pointer_context(backend, regs["s5"]),
            "stack_words_00_5c": _read_optional_words(backend, regs["sp"], 24),
        }
    if pc == 0x80171154:
        buffer_va = regs["s1"]
        object_va = regs["s5"]
        row["resource_read_chunk"] = {
            "buffer": _format_u32(buffer_va),
            "object": _format_u32(object_va),
            "count_s6": _format_u32(regs["s6"]),
            "tail_s7": _format_u32(regs["s7"]),
            "object_decoded": _decode_file_object(backend, object_va),
            "object_words_00_50": _read_optional_words(backend, object_va, 20),
            "buffer_words_00_3c": _read_optional_words(backend, buffer_va, 16),
            "buffer_bytes_00_7f": (
                data.hex()
                if (data := _read_optional_bytes(backend, buffer_va, 0x80))
                is not None
                else None
            ),
        }
    if pc == BBK_FILE_RW_ENTRY_PC:
        row["file_rw_entry"] = {
            "object_a0": _decode_file_object(backend, regs["a0"]),
            "buffer_a1": _format_u32(regs["a1"]),
            "count_a2": _format_u32(regs["a2"]),
            "mode_a3": _format_u32(regs["a3"]),
            "buffer_words_00_3c": _read_optional_words(backend, regs["a1"], 16),
            "buffer_bytes_00_7f": (
                data.hex()
                if (data := _read_optional_bytes(backend, regs["a1"], 0x80))
                is not None
                else None
            ),
            "stack_words_00_8c": _read_optional_words(backend, regs["sp"], 36),
        }
    return row


def _diagnostic_sync_file_entry_current_cluster(
    backend: QemuProcessBackend, obj: int
) -> dict[str, object]:
    row: dict[str, object] = {
        "object": _format_u32(obj),
        "applied": False,
    }
    if not (0x80000000 <= obj < 0x8A000000):
        row["reason"] = "object pointer is outside guest RAM"
        return row
    try:
        cluster = backend._read_u32_paused_locked(obj + 0x18) or 0
        current = backend._read_u32_paused_locked(obj + 0x30) or 0
        aux = backend._read_u32_paused_locked(obj + 0x34) or 0
    except Exception as exc:
        row["reason"] = f"read failed: {exc}"
        return row
    row.update(
        {
            "cluster_18_before": _format_u32(cluster),
            "current_cluster_30_before": _format_u32(current),
            "aux_cluster_34_before": _format_u32(aux),
        }
    )
    if cluster in (0, 0xFFFFFFFF):
        row["reason"] = "file cluster is not initialized"
        return row
    if current == cluster and aux == cluster:
        row["reason"] = "already synchronized"
        return row
    backend._write_u32_paused_locked(obj + 0x30, cluster)
    backend._write_u32_paused_locked(obj + 0x34, cluster)
    row.update(
        {
            "applied": True,
            "current_cluster_30_after": _format_u32(
                backend._read_u32_paused_locked(obj + 0x30) or 0
            ),
            "aux_cluster_34_after": _format_u32(
                backend._read_u32_paused_locked(obj + 0x34) or 0
            ),
        }
    )
    return row


def _run_file_open_context_probe(
    config,
    *,
    max_events: int = 48,
    calibrate_first: bool = False,
    sync_current_cluster: bool = False,
    settle_seconds: float = 0.5,
    hold_seconds: float = 0.5,
) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-file-open-context-probe",
        "breakpoints": [_format_u32(pc) for pc in BBK_FILE_OPEN_CONTEXT_PCS],
        "ok": False,
        "events": [],
        "calibration_points": [],
        "diagnostic_sync_current_cluster": bool(sync_current_cluster),
        "diagnostic_sync_patches": [],
    }
    try:
        backend.start()

        def feed_calibration() -> None:
            time.sleep(0.5)
            for index, (x, y, raw_x, raw_y) in enumerate(TOUCH_CALIBRATION_REFERENCE_POINTS):
                point: dict[str, object] = {
                    "index": index,
                    "x": x,
                    "y": y,
                    "expected_raw_x": f"0x{raw_x:04x}",
                    "expected_raw_y": f"0x{raw_y:04x}",
                }
                sent_down = backend._send_bbk_input_locked(
                    f"T {x} {y} {raw_x} {raw_y} 1"
                )
                point["down"] = {"applied": sent_down, "source": "qemu-c-machine-chardev"}
                time.sleep(hold_seconds)
                sent_up = backend._send_bbk_input_locked(
                    f"T {x} {y} {raw_x} {raw_y} 0"
                )
                point["up"] = {"applied": sent_up, "source": "qemu-c-machine-chardev"}
                row["calibration_points"].append(point)
                time.sleep(settle_seconds)

        feeder = (
            threading.Thread(target=feed_calibration, daemon=True)
            if calibrate_first
            else None
        )
        if feeder is not None:
            feeder.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted: set[int] = set()
            try:
                for bp in BBK_FILE_OPEN_CONTEXT_PCS:
                    _gdb_insert_breakpoint(backend.gdb_sock, bp)
                    inserted.add(bp)
                deadline = time.time() + float(config.timeout_seconds)
                while len(row["events"]) < max_events and time.time() < deadline:
                    wait_timeout = max(0.2, min(1.0, deadline - time.time()))
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock, timeout=wait_timeout
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_breakpoint"] = True
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    event = _capture_file_open_context_hit(backend, pc, stop_reply)
                    if sync_current_cluster and pc in {0x80173EA0, 0x80173ED0}:
                        patch = _diagnostic_sync_file_entry_current_cluster(
                            backend,
                            backend._read_register_paused_locked(30) & 0xFFFFFFFF,
                        )
                        event["diagnostic_sync_current_cluster"] = patch
                        row["diagnostic_sync_patches"].append(patch)
                    row["events"].append(event)
                    if pc in inserted:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                        inserted.discard(pc)
                        event["step_stop"] = backend._step_paused_locked()
                        event["after_step_pc"] = _format_u32(
                            backend._read_register_paused_locked(37) & 0xFFFFFFFF
                        )
                        event["after_step_v0"] = _format_u32(
                            backend._read_register_paused_locked(2) & 0xFFFFFFFF
                        )
                        _gdb_insert_breakpoint(backend.gdb_sock, pc)
                        inserted.add(pc)
                    else:
                        row["unexpected_stop_pc"] = _format_u32(pc)
                        break
                if len(row["events"]) >= max_events:
                    row["max_events_reached"] = True
            finally:
                for bp in tuple(inserted):
                    try:
                        _gdb_remove_breakpoint(backend.gdb_sock, bp)
                    except Exception:
                        pass
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row["ok"] = bool(row["events"])
        if not row["ok"]:
            row["error"] = "file-open context breakpoints were not reached"
        if feeder is not None:
            feeder.join(timeout=2.0)
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_semaphore_flow_probe(
    config,
    *,
    max_events: int = 32,
    resource_only: bool = False,
    stop_on_resource_release: bool = True,
) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    if resource_only:
        breakpoints = (
            BBK_RESOURCE_RELEASE_LOCKED_PC,
            BBK_RESOURCE_OBJECT_RELEASE_PC,
        )
    else:
        breakpoints = (
            BBK_SEMAPHORE_WAIT_PC,
            BBK_SEMAPHORE_RELEASE_PC,
            BBK_RESOURCE_RELEASE_LOCKED_PC,
            BBK_RESOURCE_OBJECT_RELEASE_PC,
        )
    row: dict[str, object] = {
        "event": "qemu-semaphore-flow-probe",
        "breakpoints": [_format_u32(pc) for pc in breakpoints],
        "resource_only": resource_only,
        "stop_on_resource_release": stop_on_resource_release,
        "ok": False,
        "events": [],
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted: list[int] = []
            gdb_wait_timed_out = False
            try:
                for addr in breakpoints:
                    _gdb_insert_breakpoint(backend.gdb_sock, addr)
                    inserted.append(addr)
                deadline = time.time() + float(config.timeout_seconds)
                while len(row["events"]) < max_events and time.time() < deadline:
                    wait_timeout = max(0.2, min(1.0, deadline - time.time()))
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock, timeout=wait_timeout
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_breakpoint"] = True
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    event = _capture_semaphore_break(backend, pc, stop_reply)
                    row["events"].append(event)
                    if pc == BBK_RESOURCE_OBJECT_RELEASE_PC:
                        row["hit_resource_object_release"] = True
                        if not stop_on_resource_release:
                            if pc in inserted:
                                _gdb_remove_breakpoint(backend.gdb_sock, pc)
                                event["step_stop"] = backend._step_paused_locked()
                                _gdb_insert_breakpoint(backend.gdb_sock, pc)
                            continue
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                        event["post_steps"] = _trace_guest_steps(backend)
                        row["hit_resource_object_release"] = True
                        break
                    if pc in inserted:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                        event["step_stop"] = backend._step_paused_locked()
                        _gdb_insert_breakpoint(backend.gdb_sock, pc)
                    else:
                        row["unexpected_stop_pc"] = _format_u32(pc)
                        break
                if len(row["events"]) >= max_events:
                    row["max_events_reached"] = True
                for addr in reversed(inserted):
                    try:
                        _gdb_remove_breakpoint(backend.gdb_sock, addr)
                    except Exception:
                        pass
            finally:
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
        wait_pc = _format_u32(BBK_SEMAPHORE_WAIT_PC)
        release_pc = _format_u32(BBK_SEMAPHORE_RELEASE_PC)
        wait_events = [
            event
            for event in row["events"]
            if isinstance(event, dict) and event.get("pc") == wait_pc
        ]
        release_events = [
            event
            for event in row["events"]
            if isinstance(event, dict) and event.get("pc") == release_pc
        ]
        row["wait_count"] = len(wait_events)
        row["release_count"] = len(release_events)
        row["ok"] = (
            bool(row.get("hit_resource_object_release"))
            if resource_only
            else bool(wait_events)
            and (bool(release_events) or bool(row.get("hit_resource_object_release")))
        )
        if not row["ok"]:
            row["error"] = "semaphore flow did not reach wait plus release/fault boundary"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_exception_context_probe(config, *, delay_seconds: float) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-exception-context-probe",
        "delay_seconds": delay_seconds,
        "ok": False,
    }
    try:
        backend.start()
        time.sleep(delay_seconds)
        with backend._lock:
            backend._pause_for_gdb_locked()
            regs = {
                "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
                "v1": backend._read_register_paused_locked(3) & 0xFFFFFFFF,
                "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
                "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
                "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
                "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
                "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
                "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
                "epc": backend._read_register_paused_locked(34) & 0xFFFFFFFF,
                "badva": backend._read_register_paused_locked(35) & 0xFFFFFFFF,
                "cause": backend._read_register_paused_locked(36) & 0xFFFFFFFF,
                "pc": backend._read_register_paused_locked(37) & 0xFFFFFFFF,
            }
            a0 = regs["a0"]
            epc = regs["epc"]
            badva = regs["badva"]
            a0_words = _read_optional_words(backend, a0, 8)
            a0_ascii = _ascii_preview(_read_optional_bytes(backend, a0, 0x40))
            badva_words = _read_optional_words(backend, badva, 4)
            badva_ascii = _ascii_preview(_read_optional_bytes(backend, badva, 0x40))
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row.update(
            {
                "registers": {
                    key: _format_u32(value) for key, value in regs.items()
                },
                "pc_classification": classify_guest_pc(_format_u32(regs["pc"])),
                "epc_classification": classify_guest_pc(_format_u32(epc)),
                "a0_words": a0_words,
                "a0_ascii": a0_ascii,
                "badva_words": badva_words,
                "badva_ascii": badva_ascii,
                "ok": True,
            }
        )
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _capture_poweroff_path_hit(
    backend: QemuProcessBackend, pc: int, stop_reply: str
) -> dict[str, object]:
    regs = {
        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
        "v1": backend._read_register_paused_locked(3) & 0xFFFFFFFF,
        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
        "t0": backend._read_register_paused_locked(8) & 0xFFFFFFFF,
        "t1": backend._read_register_paused_locked(9) & 0xFFFFFFFF,
        "s0": backend._read_register_paused_locked(16) & 0xFFFFFFFF,
        "s1": backend._read_register_paused_locked(17) & 0xFFFFFFFF,
        "s2": backend._read_register_paused_locked(18) & 0xFFFFFFFF,
        "s3": backend._read_register_paused_locked(19) & 0xFFFFFFFF,
        "s4": backend._read_register_paused_locked(20) & 0xFFFFFFFF,
        "s5": backend._read_register_paused_locked(21) & 0xFFFFFFFF,
        "s6": backend._read_register_paused_locked(22) & 0xFFFFFFFF,
        "s7": backend._read_register_paused_locked(23) & 0xFFFFFFFF,
        "gp": backend._read_register_paused_locked(28) & 0xFFFFFFFF,
        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
        "fp": backend._read_register_paused_locked(30) & 0xFFFFFFFF,
        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
        "epc": backend._read_register_paused_locked(34) & 0xFFFFFFFF,
        "cause": backend._read_register_paused_locked(36) & 0xFFFFFFFF,
        "pc": backend._read_register_paused_locked(37) & 0xFFFFFFFF,
    }
    row: dict[str, object] = {
        "pc": _format_u32(pc),
        "kind": BBK_POWEROFF_KIND_BY_PC.get(pc, "poweroff-path"),
        "region": classify_guest_pc(_format_u32(pc)).get("region"),
        "stop_reply": stop_reply,
        "registers": {key: _format_u32(value) for key, value in regs.items()},
        "pc_classification": classify_guest_pc(_format_u32(pc)),
        "ra_classification": classify_guest_pc(_format_u32(regs["ra"])),
        "epc_classification": classify_guest_pc(_format_u32(regs["epc"])),
        "stack_words_00_7c": _read_optional_words(backend, regs["sp"], 32),
        "stack_words_minus_40": _read_optional_words(backend, regs["sp"] - 0x40, 16),
        "a0_ascii": _ascii_preview(_read_optional_bytes(backend, regs["a0"], 0x80)),
        "a0_words": _read_optional_words(backend, regs["a0"], 8),
        "scheduler_globals_80473f00": _read_optional_words(
            backend, BBK_SCHEDULER_GLOBALS_VA, 32
        ),
        "gui_globals_80474040": _read_optional_words(
            backend, BBK_GUI_GLOBALS_VA, 12
        ),
        "event_queue": _read_optional_words(backend, 0x80473F6C, 1),
        "display_queue": _read_optional_words(backend, BBK_GUI_EVENT_QUEUE_VA, 8),
        "rtc_status_b0003000": _read_probe_u32(backend, BBK_RTC_STATUS_VA),
        "rtc_reg3020_b0003020": _read_probe_u32(backend, BBK_RTC_REG3020_VA),
        "rtc_reg3024_b0003024": _read_probe_u32(backend, BBK_RTC_REG3024_VA),
        "rtc_reg3028_b0003028": _read_probe_u32(backend, BBK_RTC_REG3028_VA),
        "rtc_reset_status_b0003030": _read_probe_u32(
            backend, BBK_RTC_RESET_STATUS_VA
        ),
        "intc_words_b0001000": _read_optional_words(backend, BBK_INTC_BASE_VA, 8),
    }
    return row


def _run_poweroff_path_probe(config, *, max_events: int = 32) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-poweroff-path-probe",
        "breakpoints": [_format_u32(pc) for pc in BBK_POWEROFF_PATH_PCS],
        "ok": False,
        "events": [],
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted: list[int] = []
            timed_out = False
            try:
                for pc in BBK_POWEROFF_PATH_PCS:
                    _gdb_insert_breakpoint(backend.gdb_sock, pc)
                    inserted.append(pc)
                deadline = time.time() + float(config.timeout_seconds)
                while len(row["events"]) < max_events and time.time() < deadline:
                    wait_timeout = max(0.2, deadline - time.time())
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock, timeout=wait_timeout
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_breakpoint"] = True
                        timed_out = True
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    event = _capture_poweroff_path_hit(backend, pc, stop_reply)
                    row["events"].append(event)
                    if pc == 0x80005638:
                        row["hit_terminal_loop"] = True
                        break
                    if pc in inserted:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                        event["step_stop"] = backend._step_paused_locked()
                        event["after_step_pc"] = _format_u32(
                            backend._read_register_paused_locked(37) & 0xFFFFFFFF
                        )
                        _gdb_insert_breakpoint(backend.gdb_sock, pc)
            finally:
                if timed_out and backend.gdb_sock is not None:
                    try:
                        row["timeout_interrupt_stop"] = _gdb_interrupt(
                            backend.gdb_sock
                        )
                    except Exception as exc:
                        row["timeout_interrupt_error"] = f"{type(exc).__name__}: {exc}"
                for pc in reversed(inserted):
                    try:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                    except Exception:
                        pass
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row["ok"] = bool(row.get("hit_terminal_loop"))
        if not row["ok"]:
            row["error"] = "poweroff terminal loop was not reached"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _capture_null_release_probe_hit(
    backend: QemuProcessBackend, pc: int, stop_reply: str
) -> dict[str, object]:
    regs = {
        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
        "s0": backend._read_register_paused_locked(16) & 0xFFFFFFFF,
        "s1": backend._read_register_paused_locked(17) & 0xFFFFFFFF,
        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
    }
    row: dict[str, object] = {
        "pc": _format_u32(pc),
        "region": classify_guest_pc(_format_u32(pc)).get("region"),
        "stop_reply": stop_reply,
        "registers": {key: _format_u32(value) for key, value in regs.items()},
    }
    if pc == BBK_FILE_OPENFAIL_RELEASE_CALL_PC:
        row["kind"] = "file-openfail-release-call"
        row["message_va"] = _format_u32(BBK_FILE_OPENFAIL_STRING_VA)
        row["message"] = _c_string_preview(
            _read_optional_bytes(backend, BBK_FILE_OPENFAIL_STRING_VA, 0x20)
        )
        row["delay_slot_sets_a0_zero"] = True
        row["pointer_context"] = {
            name: _pointer_context(backend, value)
            for name, value in regs.items()
            if name in {"a0", "a1", "a2", "a3", "s0", "s1", "sp", "ra"}
        }
        row["stack_words"] = _read_optional_words(backend, regs["sp"], 24)
        row["resource_globals"] = _read_optional_words(
            backend, BBK_RESOURCE_GLOBALS_BASE_VA, 12
        )
        row["resource_object_table_head"] = _read_optional_words(
            backend, BBK_RESOURCE_OBJECT_TABLE_VA, 16
        )
    elif pc == BBK_RESOURCE_RELEASE_LOCKED_PC:
        row["kind"] = "resource-release-locked-entry"
        row["null_resource_argument"] = regs["a0"] == 0
        row["resource_globals"] = _read_optional_words(
            backend, BBK_RESOURCE_GLOBALS_BASE_VA, 12
        )
        row["resource_object_table_head"] = _read_optional_words(
            backend, BBK_RESOURCE_OBJECT_TABLE_VA, 16
        )
    return row


def _run_null_resource_release_probe(config, *, max_events: int = 16) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    breakpoints = (BBK_FILE_OPENFAIL_RELEASE_CALL_PC, BBK_RESOURCE_RELEASE_LOCKED_PC)
    row: dict[str, object] = {
        "event": "qemu-null-resource-release-probe",
        "breakpoints": [_format_u32(pc) for pc in breakpoints],
        "ok": False,
        "events": [],
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted: set[int] = set()
            try:
                for pc in breakpoints:
                    _gdb_insert_breakpoint(backend.gdb_sock, pc)
                    inserted.add(pc)
                deadline = time.time() + float(config.timeout_seconds)
                while len(row["events"]) < max_events and time.time() < deadline:
                    wait_timeout = max(0.2, min(1.0, deadline - time.time()))
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock, timeout=wait_timeout
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_breakpoint"] = True
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    event = _capture_null_release_probe_hit(backend, pc, stop_reply)
                    row["events"].append(event)
                    if (
                        pc == BBK_RESOURCE_RELEASE_LOCKED_PC
                        and event.get("null_resource_argument")
                    ):
                        row["hit_null_release"] = True
                        break
                    if pc in inserted:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                        inserted.remove(pc)
                        event["step_stop"] = backend._step_paused_locked()
                        _gdb_insert_breakpoint(backend.gdb_sock, pc)
                        inserted.add(pc)
            finally:
                for pc in inserted:
                    try:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                    except Exception:
                        pass
                row["cluster_trace"] = _cluster_trace_snapshot(backend)
                row["storage_trace"] = _storage_trace_snapshot(backend)
                cluster_cache_table = _read_optional_words(
                    backend, BBK_CLUSTER_CACHE_TABLE_VA, 24
                )
                row["cluster_cache_table"] = cluster_cache_table
                row["cluster_cache_slots"] = _decode_cluster_cache_words(
                    backend, cluster_cache_table, buffer_words=8
                )
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row["ok"] = bool(row.get("hit_null_release"))
        if not row["ok"]:
            row["error"] = "null resource release was not observed"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _capture_alarm_audio_open_probe_hit(
    backend: QemuProcessBackend, pc: int, stop_reply: str
) -> dict[str, object]:
    regs = {
        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
        "v1": backend._read_register_paused_locked(3) & 0xFFFFFFFF,
        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
        "s0": backend._read_register_paused_locked(16) & 0xFFFFFFFF,
        "s1": backend._read_register_paused_locked(17) & 0xFFFFFFFF,
        "s2": backend._read_register_paused_locked(18) & 0xFFFFFFFF,
        "s3": backend._read_register_paused_locked(19) & 0xFFFFFFFF,
        "s4": backend._read_register_paused_locked(20) & 0xFFFFFFFF,
        "s5": backend._read_register_paused_locked(21) & 0xFFFFFFFF,
        "s6": backend._read_register_paused_locked(22) & 0xFFFFFFFF,
        "s7": backend._read_register_paused_locked(23) & 0xFFFFFFFF,
        "fp": backend._read_register_paused_locked(30) & 0xFFFFFFFF,
        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
    }
    row: dict[str, object] = {
        "pc": _format_u32(pc),
        "region": classify_guest_pc(_format_u32(pc)).get("region"),
        "stop_reply": stop_reply,
        "registers": {key: _format_u32(value) for key, value in regs.items()},
    }
    if pc == BBK_ALARM_DB_OPEN_CALL_PC:
        row["kind"] = "alarm-db-open-call"
        row["path_context"] = _pointer_context(backend, regs["a0"])
        row["mode_context"] = _pointer_context(backend, 0x80277D60)
    elif pc == BBK_ALARM_DB_OPEN_RETURN_PC:
        row["kind"] = "alarm-db-open-return"
        row["path_context"] = _pointer_context(backend, regs["s0"])
        row["resource_context"] = _pointer_context(backend, regs["v0"])
        row["drive_ready_8047428d"] = _format_u32(
            backend._read_u8_paused_locked(BBK_FAT_DRIVE_READY_VA)
        )
        row["fat_globals"] = {
            "fat_lba": _format_u32(
                backend._read_u32_paused_locked(BBK_FAT_FAT_LBA_VA)
            ),
            "first_data_lba": _format_u32(
                backend._read_u32_paused_locked(BBK_FAT_FIRST_DATA_LBA_VA)
            ),
        }
        row["resource_globals"] = _read_optional_words(
            backend, BBK_RESOURCE_GLOBALS_BASE_VA, 8
        )
    elif pc in {BBK_ALARM_RECORD_AUDIO_CALL_PC, BBK_ALARM_AUDIO_CALL_PC}:
        row["kind"] = "alarm-audio-call"
        stack_offset = 0xFD0 if pc == BBK_ALARM_RECORD_AUDIO_CALL_PC else 0xFA0
        row["call_site"] = _format_u32(pc)
        arg = (regs["sp"] + stack_offset) & 0xFFFFFFFF
        row["arg_pointer"] = _format_u32(arg)
        row["arg_words"] = _read_optional_words(backend, arg, 8)
        if isinstance(row["arg_words"], list) and len(row["arg_words"]) >= 3:
            try:
                path = int(row["arg_words"][2], 16)
            except (TypeError, ValueError):
                path = 0
            row["path_context"] = _pointer_context(backend, path)
    elif pc in {BBK_ALARM_RECORD_AUDIO_CALL_RETURN_PC, BBK_ALARM_AUDIO_CALL_RETURN_PC}:
        row["kind"] = "alarm-audio-call-return"
        stack_offset = 0xFD0 if pc == BBK_ALARM_RECORD_AUDIO_CALL_RETURN_PC else 0xFA0
        row["call_site"] = _format_u32(pc)
        arg = (regs["sp"] + stack_offset) & 0xFFFFFFFF
        row["arg_pointer"] = _format_u32(arg)
        row["arg_words"] = _read_optional_words(backend, arg, 8)
        if isinstance(row["arg_words"], list) and len(row["arg_words"]) >= 3:
            try:
                path = int(row["arg_words"][2], 16)
            except (TypeError, ValueError):
                path = 0
            row["path_context"] = _pointer_context(backend, path)
    elif pc == BBK_ALARM_AUDIO_OPENFAIL_PRINT_PC:
        row["kind"] = "alarm-audio-openfail-print"
        row["message_context"] = _pointer_context(backend, regs["a0"])
    elif pc == BBK_ALARM_AUDIO_OPEN_PC:
        row["kind"] = "alarm-audio-open-entry"
        arg = regs["a0"]
        row["arg_pointer"] = _format_u32(arg)
        row["arg_words"] = _read_optional_words(backend, arg, 8)
        if isinstance(row["arg_words"], list) and len(row["arg_words"]) >= 3:
            try:
                path = int(row["arg_words"][2], 16)
            except (TypeError, ValueError):
                path = 0
            row["path_context"] = _pointer_context(backend, path)
    elif pc == BBK_ALARM_AUDIO_LOOKUP_RETURN_PC:
        row["kind"] = "alarm-audio-path-lookup-return"
        row["path_context"] = _pointer_context(backend, regs["s0"])
        row["work_object_context"] = _pointer_context(backend, regs["s1"])
        row["drive_ready_8047428d"] = _format_u32(
            backend._read_u8_paused_locked(BBK_FAT_DRIVE_READY_VA)
        )
        row["fat_globals"] = {
            "fat_lba": _format_u32(
                backend._read_u32_paused_locked(BBK_FAT_FAT_LBA_VA)
            ),
            "first_data_lba": _format_u32(
                backend._read_u32_paused_locked(BBK_FAT_FIRST_DATA_LBA_VA)
            ),
        }
        row["resource_globals"] = _read_optional_words(
            backend, BBK_RESOURCE_GLOBALS_BASE_VA, 8
        )
    elif pc == BBK_ALARM_AUDIO_RESOURCE_OPEN_RETURN_PC:
        row["kind"] = "alarm-audio-resource-open-return"
        row["path_context"] = _pointer_context(backend, regs["s1"])
        row["resource_context"] = _pointer_context(backend, regs["v0"])
    elif pc == BBK_RESOURCE_CONSTRUCT_LOOKUP_RETURN_PC:
        row["kind"] = "resource-construct-lookup-return"
        row["resource_object_context"] = _pointer_context(backend, regs["s0"])
        row["work_object_context"] = _pointer_context(backend, regs["s4"])
        row["stack_probe_words"] = _read_optional_words(backend, regs["sp"] + 0x10, 4)
    elif pc == BBK_RESOURCE_CONSTRUCT_DIR_SECTOR_READ_RETURN_PC:
        row["kind"] = "resource-construct-dir-sector-read-return"
        row["resource_object_context"] = _pointer_context(backend, regs["fp"])
        row["dir_sector_lba"] = _format_u32(regs["s4"])
        row["dir_sector_buffer_context"] = _pointer_context(backend, regs["s1"])
        row["dir_sector_words_00_1c"] = _read_optional_words(backend, regs["s1"], 8)
        row["construct_stack_words_88_c0"] = _read_optional_words(
            backend, regs["sp"] + 0x88, 15
        )
    elif pc == BBK_RESOURCE_CONSTRUCT_DIRENT_MATCH_CALL_PC:
        row["kind"] = "resource-construct-dirent-match-call"
        row["resource_object_context"] = _pointer_context(backend, regs["fp"])
        row["dirent_context"] = _pointer_context(backend, regs["s1"])
        row["dirent_raw_00_1f"] = (
            data.hex()
            if (data := _read_optional_bytes(backend, regs["s1"], 0x20)) is not None
            else None
        )
        row["match_name_context"] = _pointer_context(backend, regs["sp"] + 0x40)
        row["match_ext_context"] = _pointer_context(backend, regs["sp"] + 0x50)
        row["construct_stack_words_88_c0"] = _read_optional_words(
            backend, regs["sp"] + 0x88, 15
        )
    elif pc == BBK_RESOURCE_CONSTRUCT_DIRENT_HIT_PC:
        row["kind"] = "resource-construct-dirent-hit"
        row["resource_object_context"] = _pointer_context(backend, regs["fp"])
        row["dirent_context"] = _pointer_context(backend, regs["s1"])
        row["dirent_raw_00_1f"] = (
            data.hex()
            if (data := _read_optional_bytes(backend, regs["s1"], 0x20)) is not None
            else None
        )
        row["construct_stack_words_88_c0"] = _read_optional_words(
            backend, regs["sp"] + 0x88, 15
        )
    elif pc == BBK_RESOURCE_CONSTRUCT_DIRENT_COPY_RETURN_PC:
        row["kind"] = "resource-construct-dirent-copy-return"
        row["resource_object_context"] = _pointer_context(backend, regs["fp"])
        row["firmware_dirent_context"] = _pointer_context(backend, regs["sp"] + 0x68)
        row["firmware_dirent_words_68_90"] = _read_optional_words(
            backend, regs["sp"] + 0x68, 10
        )
        row["dirent_raw_00_1f"] = (
            data.hex()
            if (data := _read_optional_bytes(backend, regs["s7"], 0x20)) is not None
            else None
        )
    elif pc == BBK_RESOURCE_CONSTRUCT_CLUSTER_READ_CALL_PC:
        row["kind"] = "resource-construct-cluster-read-call"
        cluster_buffer = backend._read_u32_paused_locked(regs["sp"] + 0x94)
        row["resource_object_context"] = _pointer_context(backend, regs["fp"])
        row["cluster_read_args"] = {
            "mode": _format_u32(regs["a0"]),
            "cluster": _format_u32(regs["a1"]),
            "buffer_arg": _format_u32(regs["a2"]),
            "stack_buffer": _format_u32(cluster_buffer),
        }
        row["cluster_buffer_words_00_3c"] = _read_optional_words(
            backend, cluster_buffer, 16
        )
    elif pc == BBK_RESOURCE_CONSTRUCT_CLUSTER_READ_RETURN_PC:
        row["kind"] = "resource-construct-cluster-read-return"
        cluster_buffer = backend._read_u32_paused_locked(regs["sp"] + 0x94)
        row["resource_object_context"] = _pointer_context(backend, regs["fp"])
        row["cluster_read_return"] = _format_u32(regs["v0"])
        row["cluster_buffer"] = _format_u32(cluster_buffer)
        row["cluster_buffer_words_00_3c"] = _read_optional_words(
            backend, cluster_buffer, 16
        )
        row["cluster_buffer_bytes_00_3f"] = (
            data.hex()
            if (data := _read_optional_bytes(backend, cluster_buffer, 0x40)) is not None
            else None
        )
    elif pc in {0x801747C4, 0x80174B9C, 0x80174DDC, 0x80175E40}:
        row["kind"] = {
            0x801747C4: "resource-state-builder-entry",
            0x80174B9C: "resource-state-build-loop-entry",
            0x80174DDC: "resource-state-current-cluster-store",
            0x80175E40: "resource-dirent-to-file-entry-copy-entry",
        }[pc]
        row["resource_state_builder"] = {
            "a2_context": _pointer_context(backend, regs["a2"]),
            "s7_context": _pointer_context(backend, regs["s7"]),
            "s7_words_00_1c": _read_optional_words(backend, regs["s7"], 8),
            "sp_words_40_80": _read_optional_words(backend, regs["sp"] + 0x40, 16),
            "sp_words_58_68": _read_optional_words(backend, regs["sp"] + 0x58, 4),
            "s4": _format_u32(regs["s4"]),
            "s5": _format_u32(regs["s5"]),
            "s6": _format_u32(regs["s6"]),
        }
    elif pc == BBK_RESOURCE_CONSTRUCT_OPEN_CALL_PC:
        row["kind"] = "resource-construct-open-call"
        row["resource_object_context"] = _pointer_context(backend, regs["s0"])
        row["work_object_context"] = _pointer_context(backend, regs["s4"])
        row["stack_probe_words"] = _read_optional_words(backend, regs["sp"] + 0x10, 4)
    elif pc == BBK_RESOURCE_CONSTRUCT_OPEN_RETURN_PC:
        row["kind"] = "resource-construct-open-return"
        row["resource_object_context"] = _pointer_context(backend, regs["s0"])
        row["work_object_context"] = _pointer_context(backend, regs["s4"])
        row["stack_probe_words"] = _read_optional_words(backend, regs["sp"] + 0x10, 4)
    elif pc == BBK_RESOURCE_CONSTRUCT_SUCCESS_PC:
        row["kind"] = "resource-construct-success"
        row["resource_object_context"] = _pointer_context(backend, regs["s0"])
        row["work_object_context"] = _pointer_context(backend, regs["s4"])
    elif pc in BBK_RESOURCE_CONSTRUCT_ERROR_PCS:
        row["kind"] = "resource-construct-error"
        row["resource_object_context"] = _pointer_context(backend, regs["s0"])
        row["work_object_context"] = _pointer_context(backend, regs["s4"])
        row["stack_probe_words"] = _read_optional_words(backend, regs["sp"] + 0x10, 4)
    return row


def _alarm_event_is_relevant(event: dict[str, object]) -> bool:
    kind = event.get("kind")
    if kind in {
        "alarm-db-open-call",
        "alarm-db-open-return",
        "alarm-audio-call",
        "alarm-audio-call-return",
        "alarm-audio-open-entry",
        "alarm-audio-openfail-print",
        "alarm-audio-resource-open-return",
        "resource-construct-lookup-return",
        "resource-construct-open-call",
        "resource-construct-open-return",
        "resource-construct-success",
        "resource-construct-error",
        "resource-construct-dir-sector-read-return",
        "resource-construct-dirent-match-call",
        "resource-construct-dirent-hit",
        "resource-construct-dirent-copy-return",
        "resource-construct-cluster-read-call",
        "resource-construct-cluster-read-return",
        "resource-state-builder-entry",
        "resource-state-build-loop-entry",
        "resource-state-current-cluster-store",
        "resource-dirent-to-file-entry-copy-entry",
    }:
        return True
    path_context = event.get("path_context")
    if isinstance(path_context, dict):
        for key in ("gbk_cstring", "cstring", "ascii"):
            value = path_context.get(key)
            if isinstance(value, str) and (
                "alarm.mp3" in value.lower() or "alarm.db" in value.lower()
            ):
                return True
    return False


def _event_path_contains(event: dict[str, object], needle: str) -> bool:
    path_context = event.get("path_context")
    if not isinstance(path_context, dict):
        return False
    needle = needle.lower()
    for key in ("gbk_cstring", "cstring", "ascii"):
        value = path_context.get(key)
        if isinstance(value, str) and needle in value.lower():
            return True
    return False


def _run_alarm_audio_open_probe(config, *, max_events: int = 32) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    resource_construct_breakpoints = (
        BBK_RESOURCE_CONSTRUCT_LOOKUP_RETURN_PC,
        BBK_RESOURCE_CONSTRUCT_DIR_SECTOR_READ_RETURN_PC,
        BBK_RESOURCE_CONSTRUCT_DIRENT_MATCH_CALL_PC,
        BBK_RESOURCE_CONSTRUCT_DIRENT_HIT_PC,
        BBK_RESOURCE_CONSTRUCT_DIRENT_COPY_RETURN_PC,
        BBK_RESOURCE_CONSTRUCT_CLUSTER_READ_CALL_PC,
        BBK_RESOURCE_CONSTRUCT_CLUSTER_READ_RETURN_PC,
        0x801747C4,
        0x80174B9C,
        0x80174DDC,
        0x80175E40,
        BBK_RESOURCE_CONSTRUCT_OPEN_CALL_PC,
        BBK_RESOURCE_CONSTRUCT_OPEN_RETURN_PC,
        BBK_RESOURCE_CONSTRUCT_SUCCESS_PC,
        *tuple(sorted(BBK_RESOURCE_CONSTRUCT_ERROR_PCS)),
    )
    breakpoints = (
        BBK_ALARM_DB_OPEN_CALL_PC,
        BBK_ALARM_DB_OPEN_RETURN_PC,
        BBK_ALARM_RECORD_AUDIO_CALL_PC,
        BBK_ALARM_RECORD_AUDIO_CALL_RETURN_PC,
        BBK_ALARM_AUDIO_CALL_PC,
        BBK_ALARM_AUDIO_CALL_RETURN_PC,
        BBK_ALARM_AUDIO_OPENFAIL_PRINT_PC,
        BBK_ALARM_AUDIO_OPEN_PC,
        BBK_ALARM_AUDIO_RESOURCE_OPEN_RETURN_PC,
    )
    row: dict[str, object] = {
        "event": "qemu-alarm-audio-open-probe",
        "breakpoints": [_format_u32(pc) for pc in breakpoints],
        "ok": False,
        "events": [],
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted: set[int] = set()
            try:
                for pc in breakpoints:
                    _gdb_insert_breakpoint(backend.gdb_sock, pc)
                    inserted.add(pc)
                deadline = time.time() + float(config.timeout_seconds)
                while len(row["events"]) < max_events and time.time() < deadline:
                    wait_timeout = max(0.2, min(1.0, deadline - time.time()))
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock, timeout=wait_timeout
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_breakpoint"] = True
                        gdb_wait_timed_out = True
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    event = _capture_alarm_audio_open_probe_hit(
                        backend, pc, stop_reply
                    )
                    if _alarm_event_is_relevant(event):
                        row["events"].append(event)
                    if (
                        pc
                        in {BBK_ALARM_AUDIO_OPEN_PC, BBK_ALARM_DB_OPEN_CALL_PC}
                        and BBK_ALARM_AUDIO_LOOKUP_RETURN_PC not in inserted
                    ):
                        _gdb_insert_breakpoint(
                            backend.gdb_sock, BBK_ALARM_AUDIO_LOOKUP_RETURN_PC
                        )
                        inserted.add(BBK_ALARM_AUDIO_LOOKUP_RETURN_PC)
                        row["breakpoints"].append(
                            _format_u32(BBK_ALARM_AUDIO_LOOKUP_RETURN_PC)
                        )
                    if (
                        pc == BBK_ALARM_AUDIO_LOOKUP_RETURN_PC
                        and _event_path_contains(event, "alarm.db")
                    ):
                        for addr in resource_construct_breakpoints:
                            if addr not in inserted:
                                _gdb_insert_breakpoint(backend.gdb_sock, addr)
                                inserted.add(addr)
                                row["breakpoints"].append(_format_u32(addr))
                    if event.get("kind") == "alarm-audio-openfail-print":
                        break
                    if pc in inserted:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                        inserted.remove(pc)
                        event["step_stop"] = backend._step_paused_locked()
                        _gdb_insert_breakpoint(backend.gdb_sock, pc)
                        inserted.add(pc)
            finally:
                for pc in inserted:
                    try:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                    except Exception:
                        pass
                row["cluster_trace"] = _cluster_trace_snapshot(backend)
                row["storage_trace"] = _storage_trace_snapshot(backend)
                cluster_cache_table = _read_optional_words(
                    backend, BBK_CLUSTER_CACHE_TABLE_VA, 24
                )
                row["cluster_cache_table"] = cluster_cache_table
                row["cluster_cache_slots"] = _decode_cluster_cache_words(
                    backend, cluster_cache_table, buffer_words=8
                )
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row["hit_alarm_db_open"] = any(
            isinstance(event, dict) and event.get("kind") == "alarm-db-open-return"
            for event in row["events"]
        )
        row["hit_audio_call_return"] = any(
            isinstance(event, dict)
            and event.get("kind") == "alarm-audio-call-return"
            for event in row["events"]
        )
        row["hit_openfail"] = any(
            isinstance(event, dict)
            and event.get("kind") == "alarm-audio-openfail-print"
            for event in row["events"]
        )
        row["ok"] = bool(row["hit_alarm_db_open"]) and (
            bool(row["hit_audio_call_return"]) or bool(row["hit_openfail"])
        )
        if not row["ok"]:
            row["error"] = "alarm db open and audio call return/openfail were not both observed"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_irq24_handler_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-irq24-handler-probe",
        "handler_entry": f"0x{BBK_IRQ24_HANDLER_ENTRY_VA:08x}",
        "ok": False,
    }
    try:
        backend.start()
        time.sleep(1.0)
        with backend._lock:
            backend._pause_for_gdb_locked()
            pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
            handler, arg = struct.unpack(
                "<2I",
                backend._read_virtual_memory_paused_locked(
                    BBK_IRQ24_HANDLER_ENTRY_VA, 8
                ),
            )
            intc_words = struct.unpack(
                "<8I",
                backend._read_virtual_memory_paused_locked(BBK_INTC_BASE_VA, 0x20),
            )
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        pending = intc_words[4]
        mask = intc_words[1]
        unmasked_pending = pending & ~mask
        row.update(
            {
                "pc": f"0x{pc:08x}",
                "classification": classify_guest_pc(f"0x{pc:08x}"),
                "handler": f"0x{handler:08x}",
                "arg": f"0x{arg:08x}",
                "intc_words": [f"0x{word:08x}" for word in intc_words],
                "pending": f"0x{pending:08x}",
                "mask": f"0x{mask:08x}",
                "unmasked_pending": f"0x{unmasked_pending:08x}",
                "ok": (
                    handler == BBK_IRQ24_EXPECTED_HANDLER
                    and (unmasked_pending & (1 << 24)) == 0
                ),
            }
        )
        if not row["ok"]:
            row["error"] = "IRQ24 handler registration or pending-clear verification failed"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_gui_touch_ring_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    queue_words = [
        0,
        0,
        0,
        0,
        BBK_GUI_EVENT_BUFFER_VA,
        4,
        0,
        0,
    ]
    active_words = [0] * 0x24
    active_words[1] = 100
    active_words[2] = 200
    expected_packed = (50 & 0xFFFF) | ((5 & 0xFFFF) << 16)
    row: dict[str, object] = {
        "event": "qemu-gui-touch-ring-probe",
        "queue": f"0x{BBK_GUI_EVENT_QUEUE_VA:08x}",
        "buffer": f"0x{BBK_GUI_EVENT_BUFFER_VA:08x}",
        "active": f"0x{BBK_GUI_ACTIVE_OBJECT_VA:08x}",
        "ok": False,
    }
    try:
        backend.start()
        time.sleep(0.8)
        with backend._lock:
            backend._pause_for_gdb_locked()
            backend._write_virtual_memory_paused_locked(
                0x80474048,
                struct.pack("<I", BBK_GUI_ACTIVE_OBJECT_VA),
            )
            backend._write_virtual_memory_paused_locked(
                BBK_GUI_ACTIVE_OBJECT_VA,
                struct.pack("<" + "I" * len(active_words), *active_words),
            )
            backend._write_virtual_memory_paused_locked(
                BBK_GUI_EVENT_QUEUE_VA,
                struct.pack("<8I", *queue_words),
            )
            backend._write_virtual_memory_paused_locked(
                BBK_GUI_EVENT_BUFFER_VA,
                b"\x00" * (4 * 0x1C),
            )
            sent = backend._send_bbk_input_locked("T 150 205 512 512 1")
            time.sleep(0.08)
            queue_after = list(
                struct.unpack(
                    "<8I",
                    backend._read_virtual_memory_paused_locked(
                        BBK_GUI_EVENT_QUEUE_VA, 0x20
                    ),
                )
            )
            record_words = list(
                struct.unpack(
                    "<7I",
                    backend._read_virtual_memory_paused_locked(
                        BBK_GUI_EVENT_BUFFER_VA, 0x1C
                    ),
                )
            )
            try:
                backend._resume_after_gdb_locked()
            except Exception as exc:
                row["resume_error"] = f"{type(exc).__name__}: {exc}"
        row.update(
            {
                "sent": sent,
                "queue_after_words": [f"0x{word:08x}" for word in queue_after],
                "record_words": [f"0x{word:08x}" for word in record_words],
                "write_index_after": queue_after[7],
                "flags_after": f"0x{queue_after[0]:08x}",
                "expected_packed": f"0x{expected_packed:08x}",
                "ok": (
                    sent
                    and queue_after[0] == 0x40000000
                    and queue_after[7] == 1
                    and record_words[0] == BBK_GUI_ACTIVE_OBJECT_VA
                    and record_words[1] == 1
                    and record_words[3] == expected_packed
                ),
            }
        )
        if not row["ok"]:
            row["error"] = "GUI touch ring enqueue verification failed"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_gui_touch_natural_probe(
    config,
    settle_seconds: float = 1.0,
    ready_timeout: float = 5.0,
    sample_interval: float = 0.5,
    release: bool = False,
    calibrate_first: bool = False,
    calibration_hold_seconds: float = 0.5,
    calibration_settle_seconds: float = 0.5,
) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-gui-touch-natural-probe",
        "queue": f"0x{BBK_GUI_EVENT_QUEUE_VA:08x}",
        "settle_seconds": settle_seconds,
        "ready_timeout": ready_timeout,
        "sample_interval": sample_interval,
        "release": release,
        "calibrate_first": calibrate_first,
        "ok": False,
    }

    def observed_touch_record(
        snapshot: dict[str, object],
        gui_state: dict[str, object],
        *,
        event_type: int,
        x: int = 100,
        y: int = 104,
    ) -> dict[str, object]:
        active = int(str(gui_state.get("active_object_80474048", "0x0")), 0)
        expected_packed = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        entries = snapshot.get("entries") if isinstance(snapshot, dict) else None
        out: dict[str, object] = {
            "active": f"0x{active:08x}",
            "event_type": event_type,
            "expected_packed": f"0x{expected_packed:08x}",
            "seen": False,
        }
        if not isinstance(entries, list):
            return out
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            words = entry.get("words")
            if not isinstance(words, list) or len(words) < 4:
                continue
            try:
                record_active = int(str(words[0]), 0)
                record_event = int(str(words[1]), 0)
                record_packed = int(str(words[3]), 0)
            except ValueError:
                continue
            if (
                record_active == active
                and record_event == event_type
                and record_packed == expected_packed
            ):
                out.update(
                    {
                        "seen": True,
                        "index": entry.get("index"),
                        "record": words,
                    }
                )
                break
        return out

    def hw_snapshot() -> dict[str, object]:
        def read_u32(addr: int) -> str:
            return f"0x{int.from_bytes(backend.read_virtual_memory(addr, 4), 'little'):08x}"

        def read_u8(addr: int) -> str:
            return f"0x{backend.read_virtual_memory(addr, 1)[0]:02x}"

        out: dict[str, object] = {}
        for name, addr in (
            ("intc_pending_unmasked_1000", 0xB0001000),
            ("intc_mask_1004", 0xB0001004),
            ("intc_pending_raw_1010", 0xB0001010),
            ("tcu_ctrl_2018", 0xB0002018),
            ("tcu_pending_2028", 0xB0002028),
            ("tcu_enabled_2038", 0xB0002038),
            ("sadc_status_7000_000c", 0xB007000C),
            ("gpio100_level", 0xB0010100),
            ("gpio100_flag", 0xB0010180),
            ("gpio200_level", 0xB0010200),
            ("gpio200_flag", 0xB0010280),
        ):
            try:
                out[name] = read_u8(addr) if name == "tcu_ctrl_2018" else read_u32(addr)
            except Exception as exc:
                out[name] = f"{type(exc).__name__}: {exc}"
        return out

    def register_snapshot() -> dict[str, object]:
        out: dict[str, object] = {}
        for name, regno in (
            ("a0", 4),
            ("a1", 5),
            ("a2", 6),
            ("a3", 7),
            ("sp", 29),
            ("ra", 31),
            ("cp0_status", 32),
            ("badvaddr", 35),
            ("cp0_cause", 36),
            ("pc", 37),
        ):
            try:
                out[name] = f"0x{backend.read_register(regno) & 0xffffffff:08x}"
            except Exception as exc:
                out[name] = f"{type(exc).__name__}: {exc}"
        return out

    try:
        backend.start()
        if calibrate_first:
            calibration_points: list[dict[str, object]] = []
            for index, (cal_x, cal_y, raw_x, raw_y) in enumerate(TOUCH_CALIBRATION_REFERENCE_POINTS):
                point: dict[str, object] = {
                    "index": index,
                    "x": cal_x,
                    "y": cal_y,
                    "expected_raw_x": f"0x{raw_x:04x}",
                    "expected_raw_y": f"0x{raw_y:04x}",
                }
                point["down"] = backend.apply_touch_state(cal_x, cal_y, True)
                time.sleep(max(0.0, calibration_hold_seconds))
                point["up"] = backend.apply_touch_state(cal_x, cal_y, False)
                time.sleep(max(0.0, calibration_settle_seconds))
                point["touch_device"] = backend.guest_touch_device_snapshot()
                calibration_points.append(point)
            row["calibration_points"] = calibration_points
        deadline = time.perf_counter() + max(0.1, ready_timeout)
        ready_gui: dict[str, object] = {}
        before: dict[str, object] = {}
        while time.perf_counter() < deadline:
            ready_gui = backend.guest_gui_state_snapshot()
            before = backend.guest_display_queue_snapshot(BBK_GUI_EVENT_QUEUE_VA)
            capacity = int(str(before.get("capacity", 0)), 0) if before else 0
            if ready_gui.get("active_object_ready") and capacity > 0:
                break
            time.sleep(0.2)
        row["ready_gui_state"] = ready_gui
        row["ready_touch_device"] = backend.guest_touch_device_snapshot()
        touch = backend.apply_touch_state(100, 104, True)
        samples: list[dict[str, object]] = []
        sample_deadline = time.perf_counter() + max(0.0, settle_seconds)
        while time.perf_counter() < sample_deadline:
            time.sleep(max(0.05, min(sample_interval, sample_deadline - time.perf_counter())))
            try:
                snap = backend.snapshot()
                pc = snap.get("pc")
                samples.append(
                    {
                        "elapsed_after_touch": round(
                            max(0.0, settle_seconds - (sample_deadline - time.perf_counter())),
                            3,
                        ),
                        "pc": pc,
                        "pc_classification": classify_guest_pc(pc) if pc else None,
                        "cp0": snap.get("cp0"),
                        "hw": hw_snapshot(),
                        "regs": register_snapshot(),
                    }
                )
            except Exception as exc:
                samples.append({"error": f"{type(exc).__name__}: {exc}"})
        after = backend.guest_display_queue_snapshot(BBK_GUI_EVENT_QUEUE_VA)
        row["touch_device_after_touch"] = backend.guest_touch_device_snapshot()
        touch_record = observed_touch_record(after, ready_gui, event_type=1)
        release_touch: dict[str, object] = {}
        release_after: dict[str, object] = {}
        release_samples: list[dict[str, object]] = []
        release_record: dict[str, object] = {}
        if release:
            release_touch = backend.apply_touch_state(100, 104, False)
            release_deadline = time.perf_counter() + max(0.0, settle_seconds)
            while time.perf_counter() < release_deadline:
                time.sleep(max(0.05, min(sample_interval, release_deadline - time.perf_counter())))
                try:
                    snap = backend.snapshot()
                    pc = snap.get("pc")
                    release_samples.append(
                        {
                            "elapsed_after_release": round(
                                max(0.0, settle_seconds - (release_deadline - time.perf_counter())),
                                3,
                            ),
                            "pc": pc,
                            "pc_classification": classify_guest_pc(pc) if pc else None,
                            "cp0": snap.get("cp0"),
                            "hw": hw_snapshot(),
                            "regs": register_snapshot(),
                        }
                    )
                except Exception as exc:
                        release_samples.append({"error": f"{type(exc).__name__}: {exc}"})
            release_after = backend.guest_display_queue_snapshot(BBK_GUI_EVENT_QUEUE_VA)
            row["touch_device_after_release"] = backend.guest_touch_device_snapshot()
            release_record = observed_touch_record(release_after, ready_gui, event_type=2)
        row["before"] = before
        row["touch"] = touch
        row["after"] = after
        row["touch_record_seen"] = touch_record
        row["samples_after_touch"] = samples
        if release:
            row["release_touch"] = release_touch
            row["release_after"] = release_after
            row["release_record_seen"] = release_record
            row["samples_after_release"] = release_samples
        row["backend_snapshot"] = backend.snapshot()

        before_read = int(str(before.get("read_index", 0)), 0) if before else 0
        before_write = int(str(before.get("write_index", 0)), 0) if before else 0
        after_read = int(str(after.get("read_index", 0)), 0) if after else 0
        after_write = int(str(after.get("write_index", 0)), 0) if after else 0
        flags_after = 0
        words_after = after.get("words_00_1c") if isinstance(after, dict) else None
        if isinstance(words_after, list) and words_after:
            flags_after = int(str(words_after[0]), 0)
        row["read_index_before"] = before_read
        row["write_index_before"] = before_write
        row["read_index_after"] = after_read
        row["write_index_after"] = after_write
        row["flags_after"] = f"0x{flags_after:08x}"
        row["touch_applied"] = bool(touch.get("applied")) if isinstance(touch, dict) else False
        row["enqueued"] = (
            after_write != before_write
            or bool(flags_after & 0x40000000)
            or bool(touch_record.get("seen"))
        )
        row["consumed"] = after_read != before_read and after_read == after_write
        if release:
            release_read = int(str(release_after.get("read_index", 0)), 0) if release_after else 0
            release_write = int(str(release_after.get("write_index", 0)), 0) if release_after else 0
            row["release_read_index_after"] = release_read
            row["release_write_index_after"] = release_write
            row["release_touch_applied"] = bool(release_touch.get("applied")) if isinstance(release_touch, dict) else False
            row["release_consumed"] = release_read == release_write or bool(release_record.get("seen"))
        row["ready"] = bool(ready_gui.get("active_object_ready")) and before_write >= 0
        if release:
            row["ok"] = (
                bool(row["ready"])
                and bool(row["touch_applied"])
                and bool(row["release_touch_applied"])
                and bool(row["release_consumed"])
            )
            row["tap_consumed"] = bool(row["release_consumed"])
            row["down_event_consumed"] = bool(row["consumed"])
        else:
            row["ok"] = (
                bool(row["ready"])
                and bool(row["touch_applied"])
                and bool(row["enqueued"])
                and bool(row["consumed"])
            )
        if not row["ok"]:
            if release and row.get("consumed") and not row.get("release_consumed"):
                row["error"] = "touch release event was not naturally consumed by the initialized firmware GUI ring"
            else:
                row["error"] = (
                    "touch event was not naturally consumed by the initialized firmware "
                    "GUI ring; scheduler/event wake modeling is still incomplete"
                )
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_touch_input_calibration_probe(
    config,
    settle_seconds: float = 0.5,
    hold_seconds: float = 0.5,
    ready_timeout: float = 10.0,
) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-touch-input-calibration-probe",
        "settle_seconds": settle_seconds,
        "hold_seconds": hold_seconds,
        "ready_timeout": ready_timeout,
        "points": [],
        "ok": False,
    }

    def wait_snapshot(seconds: float) -> dict[str, object]:
        deadline = time.perf_counter() + max(0.0, seconds)
        snap: dict[str, object] = {}
        while time.perf_counter() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.perf_counter())))
            try:
                snap = backend.snapshot()
            except Exception as exc:
                snap = {"error": f"{type(exc).__name__}: {exc}"}
        return snap

    try:
        backend.start()
        row["initial_touch_device"] = backend.guest_touch_device_snapshot()
        row["initial_gui_state"] = backend.guest_gui_state_snapshot()
        for index, (x, y, raw_x, raw_y) in enumerate(TOUCH_CALIBRATION_REFERENCE_POINTS):
            point: dict[str, object] = {
                "index": index,
                "x": x,
                "y": y,
                "expected_raw_x": f"0x{raw_x:04x}",
                "expected_raw_y": f"0x{raw_y:04x}",
            }
            point["before_touch_device"] = backend.guest_touch_device_snapshot()
            point["down"] = backend.apply_touch_state(x, y, True)
            point["after_down_touch_device"] = backend.guest_touch_device_snapshot()
            point["hold_snapshot"] = wait_snapshot(hold_seconds)
            point["up"] = backend.apply_touch_state(x, y, False)
            point["after_up_touch_device"] = backend.guest_touch_device_snapshot()
            point["settle_snapshot"] = wait_snapshot(settle_seconds)
            row["points"].append(point)

        deadline = time.perf_counter() + max(0.1, ready_timeout)
        gui_state: dict[str, object] = {}
        queue_state: dict[str, object] = {}
        samples: list[dict[str, object]] = []
        while time.perf_counter() < deadline:
            gui_state = backend.guest_gui_state_snapshot()
            queue_state = backend.guest_display_queue_snapshot(BBK_GUI_EVENT_QUEUE_VA)
            touch_device = backend.guest_touch_device_snapshot()
            snap = backend.snapshot()
            pc = snap.get("pc")
            samples.append(
                {
                    "pc": pc,
                    "pc_classification": classify_guest_pc(pc) if pc else None,
                    "gui_active": gui_state.get("active_object_ready"),
                    "queue_capacity": queue_state.get("capacity") if isinstance(queue_state, dict) else None,
                    "touch_device": touch_device,
                }
            )
            capacity = int(str(queue_state.get("capacity", 0)), 0) if isinstance(queue_state, dict) else 0
            if gui_state.get("active_object_ready") and capacity > 0:
                break
            time.sleep(0.25)

        row["final_gui_state"] = gui_state
        row["final_queue_state"] = queue_state
        row["final_touch_device"] = backend.guest_touch_device_snapshot()
        row["samples_after_calibration"] = samples[-12:]
        row["backend_snapshot"] = backend.snapshot()
        capacity = int(str(queue_state.get("capacity", 0)), 0) if isinstance(queue_state, dict) else 0
        row["ok"] = bool(gui_state.get("active_object_ready")) and capacity > 0
        if not row["ok"]:
            row["error"] = "input-driven calibration did not reach active GUI object"
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_cluster_read_path_probe(
    config,
    *,
    max_events: int = 24,
    settle_seconds: float = 0.5,
    hold_seconds: float = 0.5,
    ready_timeout: float = 10.0,
) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-cluster-read-path-probe",
        "breakpoints": [
            _format_u32(pc)
            for pc in (
                BBK_CLUSTER_READ_ENTRY_PC,
                BBK_CLUSTER_READ_WRAPPER_RETURN_PC,
                *BBK_CLUSTER_READ_INTERNAL_RETURN_PCS,
            )
        ],
        "events": [],
        "calibration_points": [],
        "ok": False,
    }

    def wait_snapshot(seconds: float) -> None:
        deadline = time.perf_counter() + max(0.0, seconds)
        while time.perf_counter() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.perf_counter())))
            try:
                backend.snapshot()
            except Exception:
                pass

    try:
        backend.start()
        def feed_calibration() -> None:
            time.sleep(0.5)
            for index, (x, y, raw_x, raw_y) in enumerate(TOUCH_CALIBRATION_REFERENCE_POINTS):
                point: dict[str, object] = {
                    "index": index,
                    "x": x,
                    "y": y,
                    "expected_raw_x": f"0x{raw_x:04x}",
                    "expected_raw_y": f"0x{raw_y:04x}",
                }
                sent_down = backend._send_bbk_input_locked(
                    f"T {x} {y} {raw_x} {raw_y} 1"
                )
                point["down"] = {"applied": sent_down, "source": "qemu-c-machine-chardev"}
                time.sleep(hold_seconds)
                sent_up = backend._send_bbk_input_locked(
                    f"T {x} {y} {raw_x} {raw_y} 0"
                )
                point["up"] = {"applied": sent_up, "source": "qemu-c-machine-chardev"}
                row["calibration_points"].append(point)
                time.sleep(settle_seconds)

        feeder = threading.Thread(target=feed_calibration, daemon=True)
        feeder.start()

        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted: list[int] = []
            last_call: dict[str, int] | None = None
            try:
                for pc in (
                    BBK_CLUSTER_READ_ENTRY_PC,
                    BBK_CLUSTER_READ_WRAPPER_RETURN_PC,
                    *BBK_CLUSTER_READ_INTERNAL_RETURN_PCS,
                ):
                    _gdb_insert_breakpoint(backend.gdb_sock, pc)
                    inserted.append(pc)
                deadline = time.perf_counter() + float(config.timeout_seconds)
                while len(row["events"]) < max_events and time.perf_counter() < deadline:
                    try:
                        _gdb_continue_wait(
                            backend.gdb_sock,
                            timeout=max(0.2, min(1.0, deadline - time.perf_counter())),
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_breakpoint"] = True
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    regs = {
                        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
                        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
                        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
                        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
                        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
                        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
                    }
                    event: dict[str, object] = {
                        "pc": _format_u32(pc),
                        "registers": {name: _format_u32(value) for name, value in regs.items()},
                    }
                    if pc == BBK_CLUSTER_READ_ENTRY_PC:
                        last_call = {
                            "cluster": regs["a0"],
                            "dest": regs["a1"],
                            "arg_a2": regs["a2"],
                            "arg_a3": regs["a3"],
                            "ra": regs["ra"],
                        }
                        event["kind"] = "cluster-read-entry"
                        event["call"] = {key: _format_u32(value) for key, value in last_call.items()}
                    elif pc in BBK_CLUSTER_READ_INTERNAL_RETURN_PCS:
                        event["kind"] = "cluster-read-internal-return-path"
                        event["return_path"] = BBK_CLUSTER_READ_INTERNAL_RETURN_PCS[pc]
                        if last_call is not None:
                            event["last_call"] = {
                                key: _format_u32(value) for key, value in last_call.items()
                            }
                            dest = last_call.get("dest", 0)
                            if 0x80000000 <= dest < 0x8A000000:
                                event["dest_words"] = _read_optional_words(backend, dest, 8)
                                data = _read_optional_bytes(backend, dest, 32)
                                event["dest_bytes_00_1f"] = None if data is None else data.hex()
                    elif pc == BBK_CLUSTER_READ_WRAPPER_RETURN_PC:
                        event["kind"] = "cluster-read-wrapper-return"
                        event["return_v0"] = _format_u32(regs["v0"])
                        if last_call is not None:
                            event["last_call"] = {
                                key: _format_u32(value) for key, value in last_call.items()
                            }
                            dest = last_call.get("dest", 0)
                            if 0x80000000 <= dest < 0x8A000000:
                                event["dest_words"] = _read_optional_words(backend, dest, 8)
                                data = _read_optional_bytes(backend, dest, 32)
                                event["dest_bytes_00_1f"] = None if data is None else data.hex()
                    row["events"].append(event)
                    event["step_stop"] = backend._step_paused_locked()
            finally:
                for pc in inserted:
                    try:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                    except Exception:
                        pass
                row["cluster_trace"] = _cluster_trace_snapshot(backend)
                row["storage_trace"] = _storage_trace_snapshot(backend)
                cluster_cache_table = _read_optional_words(backend, BBK_CLUSTER_CACHE_TABLE_VA, 24)
                row["cluster_cache_table"] = cluster_cache_table
                row["cluster_cache_slots"] = _decode_cluster_cache_words(
                    backend, cluster_cache_table, buffer_words=8
                )
                try:
                    backend._resume_after_gdb_locked()
                except Exception as exc:
                    row["resume_error"] = f"{type(exc).__name__}: {exc}"
        feeder.join(timeout=max(0.0, ready_timeout))
        try:
            row["post_calibration_gui_state"] = backend.guest_gui_state_snapshot()
            row["post_calibration_queue_state"] = backend.guest_display_queue_snapshot(
                BBK_GUI_EVENT_QUEUE_VA
            )
        except Exception as exc:
            row["post_calibration_error"] = f"{type(exc).__name__}: {exc}"
        row["backend_snapshot"] = backend.snapshot()
        row["ok"] = any(
            isinstance(event, dict)
            and event.get("kind") == "cluster-read-wrapper-return"
            and event.get("return_v0") in {"0x00000000", "0x00000001"}
            for event in row["events"]
        )
        if not row["ok"]:
            row["error"] = "cluster-read entry/return was not observed after input calibration"
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _read_probe_u32(backend: QemuProcessBackend, va: int) -> str:
    try:
        data = backend._read_virtual_memory_paused_locked(va, 4)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return _format_u32(struct.unpack("<I", data)[0])


def _run_touch_move_sadc_probe(config) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    row: dict[str, object] = {
        "event": "qemu-touch-move-sadc-probe",
        "ok": False,
    }
    labels = (
        "magic",
        "reserved_04",
        "reserved_08",
        "reserved_0c",
        "touch_down",
        "touch_raw_x",
        "touch_raw_y",
        "sadc_status_event",
        "sadc_next_axis",
        "intc_pending_mask",
        "pc",
        "reason",
        "sadc_conversion_events_remaining",
    )

    def read_touch_trace_paused() -> dict[str, object]:
        data = backend._read_virtual_memory_paused_locked(BBK_TOUCH_TRACE_VA, 0x40)
        words = [
            struct.unpack_from("<I", data, offset)[0]
            for offset in range(0, 0x34, 4)
        ]
        out: dict[str, object] = {
            label: f"0x{value:08x}" for label, value in zip(labels, words)
        }
        out["available"] = words[0] == 0x54434B42
        out["touch_down_bool"] = bool(words[4])
        out["touch_raw_x_int"] = int(words[5])
        out["touch_raw_y_int"] = int(words[6])
        out["sadc_conversion_events_remaining_int"] = int(words[12])
        return out

    def send_touch_and_sample(x: int, y: int, down: bool) -> tuple[dict[str, object], dict[str, object]]:
        raw_x, raw_y = backend._touch_panel_to_adc(x, y)
        with backend._lock:
            sent = backend._send_bbk_input_locked(
                f"T {x} {y} {raw_x} {raw_y} {1 if down else 0}"
            )
            time.sleep(0.01)
            backend._pause_for_gdb_locked()
            try:
                trace = read_touch_trace_paused()
            finally:
                backend._resume_after_gdb_locked()
        event = {
            "event": "qemu-touch-state",
            "x": x,
            "y": y,
            "down": down,
            "raw_x": f"0x{raw_x:04x}",
            "raw_y": f"0x{raw_y:04x}",
            "applied": bool(sent),
            "source": "qemu-c-machine-chardev",
        }
        return event, trace

    try:
        backend.start()
        first, after_down = send_touch_and_sample(100, 104, True)
        moved, after_move = send_touch_and_sample(112, 118, True)
        released, after_release = send_touch_and_sample(112, 118, False)

        down_remaining = int(after_down.get("sadc_conversion_events_remaining_int", 0))
        move_remaining = int(after_move.get("sadc_conversion_events_remaining_int", 0))
        release_remaining = int(after_release.get("sadc_conversion_events_remaining_int", 0))
        down_status = int(str(after_down.get("sadc_status_event", "0")), 0)
        move_status = int(str(after_move.get("sadc_status_event", "0")), 0)
        release_status = int(str(after_release.get("sadc_status_event", "0")), 0)
        down_raw = (
            int(after_down.get("touch_raw_x_int", 0)),
            int(after_down.get("touch_raw_y_int", 0)),
        )
        move_raw = (
            int(after_move.get("touch_raw_x_int", 0)),
            int(after_move.get("touch_raw_y_int", 0)),
        )

        row.update(
            {
                "down": first,
                "move": moved,
                "release": released,
                "after_down": after_down,
                "after_move": after_move,
                "after_release": after_release,
                "down_remaining": down_remaining,
                "move_remaining": move_remaining,
                "release_remaining": release_remaining,
                "down_status": _format_u32(down_status),
                "move_status": _format_u32(move_status),
                "release_status": _format_u32(release_status),
                "down_raw": [f"0x{down_raw[0]:04x}", f"0x{down_raw[1]:04x}"],
                "move_raw": [f"0x{move_raw[0]:04x}", f"0x{move_raw[1]:04x}"],
            }
        )
        row["ok"] = (
            bool(first.get("applied"))
            and bool(moved.get("applied"))
            and bool(released.get("applied"))
            and bool(after_down.get("touch_down_bool"))
            and bool(after_move.get("touch_down_bool"))
            and not bool(after_release.get("touch_down_bool"))
            and down_raw != move_raw
            and (down_status & 0x14) == 0x14
            and (move_status & 0x14) == 0x14
            and (release_status & 0x08) == 0x08
            and down_remaining > 0
            and move_remaining > 0
            and release_remaining == 0
        )
        if not row["ok"]:
            row["error"] = "touch move did not refresh SADC sample-ready state"
        row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def _run_alarm_ui_probe(config, *, max_events: int = 64) -> dict[str, object]:
    backend = QemuProcessBackend(config)
    breakpoints = (
        0x80013BD4,
        0x80013C2C,
        0x80013C50,
        0x80013D20,
        BBK_ALARM_UI_ENTRY_PC,
        BBK_ALARM_FLAG_BRANCH_PC,
        0x80015014,
        0x800154C8,
    )
    row: dict[str, object] = {
        "event": "qemu-alarm-ui-probe",
        "breakpoints": [_format_u32(pc) for pc in breakpoints],
        "ok": False,
        "events": [],
    }
    try:
        backend.start()
        with backend._lock:
            backend._pause_for_gdb_locked()
            if backend.gdb_sock is None:
                raise RuntimeError("QEMU GDB stub is not connected")
            inserted: list[int] = []
            gdb_wait_timed_out = False
            try:
                for addr in breakpoints:
                    _gdb_insert_breakpoint(backend.gdb_sock, addr)
                    inserted.append(addr)
                deadline = time.time() + float(config.timeout_seconds)
                while len(row["events"]) < max_events and time.time() < deadline:
                    wait_timeout = max(0.2, deadline - time.time())
                    try:
                        stop_reply = _gdb_continue_wait(
                            backend.gdb_sock, timeout=wait_timeout
                        )
                    except TimeoutError:
                        row["timeout_waiting_for_breakpoint"] = True
                        gdb_wait_timed_out = True
                        break
                    pc = backend._read_register_paused_locked(37) & 0xFFFFFFFF
                    regs = {
                        "a0": backend._read_register_paused_locked(4) & 0xFFFFFFFF,
                        "a1": backend._read_register_paused_locked(5) & 0xFFFFFFFF,
                        "a2": backend._read_register_paused_locked(6) & 0xFFFFFFFF,
                        "a3": backend._read_register_paused_locked(7) & 0xFFFFFFFF,
                        "v0": backend._read_register_paused_locked(2) & 0xFFFFFFFF,
                        "v1": backend._read_register_paused_locked(3) & 0xFFFFFFFF,
                        "sp": backend._read_register_paused_locked(29) & 0xFFFFFFFF,
                        "ra": backend._read_register_paused_locked(31) & 0xFFFFFFFF,
                    }
                    event: dict[str, object] = {
                        "pc": _format_u32(pc),
                        "stop": stop_reply,
                        "registers": {
                            name: _format_u32(value) for name, value in regs.items()
                        },
                        "alarm_flag_80477d4c": _read_probe_u32(
                            backend, BBK_ALARM_FLAG_VA
                        ),
                        "alarm_record_80477d54": _read_probe_u32(
                            backend, BBK_ALARM_RECORD_VA
                        ),
                        "alarm_state_80477d60": _read_probe_u32(
                            backend, BBK_ALARM_STATE_VA
                        ),
                        "rtc_status_b0003000": _read_probe_u32(
                            backend, BBK_RTC_STATUS_VA
                        ),
                        "rtc_reset_status_b0003030": _read_probe_u32(
                            backend, BBK_RTC_RESET_STATUS_VA
                        ),
                    }
                    record_ptr_text = event["alarm_record_80477d54"]
                    if isinstance(record_ptr_text, str) and record_ptr_text.startswith("0x"):
                        record_ptr = int(record_ptr_text, 16)
                        event["alarm_record_words_00_2c"] = _read_optional_words(
                            backend, record_ptr, 12
                        )
                    row["events"].append(event)
                    if pc == BBK_ALARM_FLAG_BRANCH_PC:
                        row["hit_alarm_flag_setter"] = True
                    if pc == BBK_ALARM_UI_ENTRY_PC:
                        row["hit_alarm_ui_entry"] = True
                    if pc in inserted:
                        _gdb_remove_breakpoint(backend.gdb_sock, pc)
                        event["step_stop"] = backend._step_paused_locked()
                        _gdb_insert_breakpoint(backend.gdb_sock, pc)
                if not gdb_wait_timed_out:
                    for addr in reversed(inserted):
                        try:
                            _gdb_remove_breakpoint(backend.gdb_sock, addr)
                        except Exception:
                            pass
            finally:
                if not gdb_wait_timed_out:
                    try:
                        backend._resume_after_gdb_locked()
                    except Exception as exc:
                        row["resume_error"] = f"{type(exc).__name__}: {exc}"
                elif backend.gdb_sock is not None:
                    try:
                        row["timeout_interrupt_stop"] = _gdb_interrupt(
                            backend.gdb_sock
                        )
                        row["progress_trace"] = _progress_trace_snapshot(backend)
                        for addr in reversed(inserted):
                            try:
                                _gdb_remove_breakpoint(backend.gdb_sock, addr)
                            except Exception:
                                pass
                    except Exception as exc:
                        row["progress_trace_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
        row["ok"] = bool(row.get("hit_alarm_flag_setter")) and bool(
            row.get("hit_alarm_ui_entry")
        )
        if not row["ok"]:
            row["error"] = "alarm UI flag setter and entry were not both observed"
        if row.get("timeout_waiting_for_breakpoint"):
            row["backend_snapshot_skipped"] = (
                "skipped after GDB continue timeout to avoid blocking cleanup"
            )
        else:
            row["backend_snapshot"] = backend.snapshot()
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row["backend_snapshot"] = backend.snapshot()
        except Exception:
            pass
        return row
    finally:
        backend.stop()


def run_probe(ns: argparse.Namespace) -> int:
    ns.out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    config = build_bbk_qemu_config(
        boot_mode=ns.boot_mode,
        executable=ns.qemu,
        image=ns.image,
        payload=ns.payload,
        payload_addr=ns.payload_addr,
        nand_image=ns.nand_image,
        load_addr=ns.load_addr,
        pc=ns.pc,
        machine=ns.machine,
        cpu=ns.cpu,
        ram_mb=ns.ram_mb,
        accel=ns.accel,
        display=ns.display,
        serial=ns.serial,
        monitor=ns.monitor,
        gdb=ns.gdb,
        bbk_machine_options=tuple(ns.qemu_machine_option or ()),
        timeout_seconds=ns.timeout,
        extra_args=tuple(ns.extra_arg or ()),
        firmware_patches=ns.qemu_firmware_patch,
    )
    command = build_qemu_command(config)
    resolved = find_qemu(ns.qemu)
    summary: dict[str, object] = {
        "ok": bool(ns.dry_run),
        "backend": "qemu-system-mipsel",
        "qemu": ns.qemu,
        "resolved_qemu": resolved,
        "boot_mode": ns.boot_mode,
        "command": command,
        "qemu_machine_options": list(ns.qemu_machine_option or ()),
        "timeout_seconds": ns.timeout,
    }
    if ns.dry_run:
        summary["dry_run"] = True
        if resolved is None:
            summary["warning"] = "qemu-system-mipsel was not found; command generation only"
    elif resolved is None:
        summary["failure"] = "qemu-system-mipsel was not found on PATH"
    elif (
        ns.input_event_queue_probe
        or ns.udc_idle_probe
        or ns.storage_layout_probe
        or ns.cache_scan_probe
        or ns.cache_scan_caller_probe
        or ns.fat_cache_io_probe
        or ns.msc_dma_write_probe
        or ns.uart_register_probe
        or ns.sadc_battery_probe
        or ns.rtc_hibernate_probe
        or ns.rtc_alarm_irq_probe
        or ns.bch_status_probe
        or ns.lcd_frame_done_probe
        or ns.config_cluster_watch_probe
        or ns.file_open_context_probe
        or ns.semaphore_flow_probe
        or ns.exception_context_probe
        or ns.poweroff_path_probe
        or ns.null_resource_release_probe
        or ns.alarm_audio_open_probe
        or ns.irq24_handler_probe
        or ns.gui_event_poller_probe
        or ns.gui_touch_ring_probe
        or ns.gui_touch_natural_probe
        or ns.touch_input_calibration_probe
        or ns.cluster_read_path_probe
        or ns.touch_move_sadc_probe
        or ns.key_gpio_probe
        or ns.alarm_ui_probe
    ):
        summary["ok"] = True
        if ns.input_event_queue_probe:
            input_probe = _run_input_event_queue_probe(config)
            summary["input_event_queue_probe"] = input_probe
            summary["ok"] = bool(summary["ok"]) and bool(input_probe.get("ok"))
        if ns.udc_idle_probe:
            udc_probe = _run_udc_idle_probe(config)
            summary["udc_idle_probe"] = udc_probe
            summary["ok"] = bool(summary["ok"]) and bool(udc_probe.get("ok"))
        if ns.storage_layout_probe:
            storage_probe = _run_storage_layout_probe(config)
            summary["storage_layout_probe"] = storage_probe
            summary["ok"] = bool(summary["ok"]) and bool(storage_probe.get("ok"))
        if ns.cache_scan_probe:
            cache_probe = _run_cache_scan_probe(
                config,
                max_events=ns.cache_scan_max_events,
            )
            summary["cache_scan_probe"] = cache_probe
            summary["ok"] = bool(summary["ok"]) and bool(cache_probe.get("ok"))
        if ns.cache_scan_caller_probe:
            caller_probe = _run_cache_scan_caller_probe(
                config,
                max_events=ns.cache_scan_caller_max_events,
            )
            summary["cache_scan_caller_probe"] = caller_probe
            summary["ok"] = bool(summary["ok"]) and bool(caller_probe.get("ok"))
        if ns.fat_cache_io_probe:
            fat_cache_probe = _run_fat_cache_io_probe(
                config,
                max_events=ns.fat_cache_io_max_events,
                flush_only=ns.fat_cache_io_flush_only,
            )
            summary["fat_cache_io_probe"] = fat_cache_probe
            summary["ok"] = bool(summary["ok"]) and bool(fat_cache_probe.get("ok"))
        if ns.msc_dma_write_probe:
            msc_dma_probe = _run_msc_dma_write_probe(
                config,
                lba=ns.msc_dma_write_lba,
            )
            summary["msc_dma_write_probe"] = msc_dma_probe
            summary["ok"] = bool(summary["ok"]) and bool(msc_dma_probe.get("ok"))
        if ns.uart_register_probe:
            uart_probe = _run_uart_register_probe(config)
            summary["uart_register_probe"] = uart_probe
            summary["ok"] = bool(summary["ok"]) and bool(uart_probe.get("ok"))
        if ns.sadc_battery_probe:
            sadc_probe = _run_sadc_battery_probe(config)
            summary["sadc_battery_probe"] = sadc_probe
            summary["ok"] = bool(summary["ok"]) and bool(sadc_probe.get("ok"))
        if ns.rtc_hibernate_probe:
            rtc_probe = _run_rtc_hibernate_probe(config)
            summary["rtc_hibernate_probe"] = rtc_probe
            summary["ok"] = bool(summary["ok"]) and bool(rtc_probe.get("ok"))
        if ns.rtc_alarm_irq_probe:
            rtc_alarm_probe = _run_rtc_alarm_irq_probe(config)
            summary["rtc_alarm_irq_probe"] = rtc_alarm_probe
            summary["ok"] = bool(summary["ok"]) and bool(rtc_alarm_probe.get("ok"))
        if ns.bch_status_probe:
            bch_probe = _run_bch_status_probe(config)
            summary["bch_status_probe"] = bch_probe
            summary["ok"] = bool(summary["ok"]) and bool(bch_probe.get("ok"))
        if ns.lcd_frame_done_probe:
            lcd_probe = _run_lcd_frame_done_probe(config)
            summary["lcd_frame_done_probe"] = lcd_probe
            summary["ok"] = bool(summary["ok"]) and bool(lcd_probe.get("ok"))
        if ns.config_cluster_watch_probe:
            watch_probe = _run_config_cluster_watch_probe(
                config,
                timeout=ns.config_cluster_watch_timeout,
                calibrate_first=ns.config_cluster_watch_calibrate_first,
                settle_seconds=ns.touch_input_calibration_settle,
                hold_seconds=ns.touch_input_calibration_hold,
            )
            summary["config_cluster_watch_probe"] = watch_probe
            summary["ok"] = bool(summary["ok"]) and bool(watch_probe.get("ok"))
        if ns.file_open_context_probe:
            file_open_probe = _run_file_open_context_probe(
                config,
                max_events=ns.file_open_context_max_events,
                calibrate_first=ns.file_open_context_calibrate_first,
                sync_current_cluster=ns.file_open_context_sync_current_cluster,
                settle_seconds=ns.touch_input_calibration_settle,
                hold_seconds=ns.touch_input_calibration_hold,
            )
            summary["file_open_context_probe"] = file_open_probe
            summary["ok"] = bool(summary["ok"]) and bool(file_open_probe.get("ok"))
        if ns.semaphore_flow_probe:
            semaphore_probe = _run_semaphore_flow_probe(
                config,
                max_events=ns.semaphore_flow_max_events,
                resource_only=ns.semaphore_flow_resource_only,
                stop_on_resource_release=not ns.semaphore_flow_continue_resource,
            )
            summary["semaphore_flow_probe"] = semaphore_probe
            summary["ok"] = bool(summary["ok"]) and bool(semaphore_probe.get("ok"))
        if ns.exception_context_probe:
            exception_probe = _run_exception_context_probe(
                config, delay_seconds=ns.exception_context_delay
            )
            summary["exception_context_probe"] = exception_probe
            summary["ok"] = bool(summary["ok"]) and bool(exception_probe.get("ok"))
        if ns.poweroff_path_probe:
            poweroff_probe = _run_poweroff_path_probe(
                config,
                max_events=ns.poweroff_path_max_events,
            )
            summary["poweroff_path_probe"] = poweroff_probe
            summary["ok"] = bool(summary["ok"]) and bool(poweroff_probe.get("ok"))
        if ns.null_resource_release_probe:
            null_release_probe = _run_null_resource_release_probe(
                config,
                max_events=ns.null_resource_release_max_events,
            )
            summary["null_resource_release_probe"] = null_release_probe
            summary["ok"] = bool(summary["ok"]) and bool(null_release_probe.get("ok"))
        if ns.alarm_audio_open_probe:
            alarm_audio_probe = _run_alarm_audio_open_probe(
                config,
                max_events=ns.alarm_audio_open_max_events,
            )
            summary["alarm_audio_open_probe"] = alarm_audio_probe
            summary["ok"] = bool(summary["ok"]) and bool(alarm_audio_probe.get("ok"))
        if ns.irq24_handler_probe:
            irq24_probe = _run_irq24_handler_probe(config)
            summary["irq24_handler_probe"] = irq24_probe
            summary["ok"] = bool(summary["ok"]) and bool(irq24_probe.get("ok"))
        if ns.gui_event_poller_probe:
            gui_poller_probe = _run_gui_event_poller_probe(config)
            summary["gui_event_poller_probe"] = gui_poller_probe
            summary["ok"] = bool(summary["ok"]) and bool(gui_poller_probe.get("ok"))
        if ns.gui_touch_ring_probe:
            gui_touch_ring_probe = _run_gui_touch_ring_probe(config)
            summary["gui_touch_ring_probe"] = gui_touch_ring_probe
            summary["ok"] = bool(summary["ok"]) and bool(gui_touch_ring_probe.get("ok"))
        if ns.gui_touch_natural_probe:
            gui_touch_natural_probe = _run_gui_touch_natural_probe(
                config,
                settle_seconds=ns.gui_touch_natural_settle,
                ready_timeout=ns.gui_touch_natural_ready_timeout,
                sample_interval=ns.gui_touch_natural_sample_interval,
                release=ns.gui_touch_natural_release,
                calibrate_first=ns.gui_touch_natural_calibrate_first,
                calibration_hold_seconds=ns.touch_input_calibration_hold,
                calibration_settle_seconds=ns.touch_input_calibration_settle,
            )
            summary["gui_touch_natural_probe"] = gui_touch_natural_probe
            summary["ok"] = bool(summary["ok"]) and bool(gui_touch_natural_probe.get("ok"))
        if ns.touch_input_calibration_probe:
            touch_calibration_probe = _run_touch_input_calibration_probe(
                config,
                settle_seconds=ns.touch_input_calibration_settle,
                hold_seconds=ns.touch_input_calibration_hold,
                ready_timeout=ns.touch_input_calibration_ready_timeout,
            )
            summary["touch_input_calibration_probe"] = touch_calibration_probe
            summary["ok"] = bool(summary["ok"]) and bool(touch_calibration_probe.get("ok"))
        if ns.cluster_read_path_probe:
            cluster_read_probe = _run_cluster_read_path_probe(
                config,
                max_events=ns.cluster_read_path_max_events,
                settle_seconds=ns.touch_input_calibration_settle,
                hold_seconds=ns.touch_input_calibration_hold,
                ready_timeout=ns.touch_input_calibration_ready_timeout,
            )
            summary["cluster_read_path_probe"] = cluster_read_probe
            summary["ok"] = bool(summary["ok"]) and bool(cluster_read_probe.get("ok"))
        if ns.touch_move_sadc_probe:
            touch_move_probe = _run_touch_move_sadc_probe(config)
            summary["touch_move_sadc_probe"] = touch_move_probe
            summary["ok"] = bool(summary["ok"]) and bool(touch_move_probe.get("ok"))
        if ns.key_gpio_probe:
            key_gpio_probe = _run_key_gpio_probe(config)
            summary["key_gpio_probe"] = key_gpio_probe
            summary["ok"] = bool(summary["ok"]) and bool(key_gpio_probe.get("ok"))
        if ns.alarm_ui_probe:
            alarm_ui_probe = _run_alarm_ui_probe(
                config,
                max_events=ns.alarm_ui_max_events,
            )
            summary["alarm_ui_probe"] = alarm_ui_probe
            summary["ok"] = bool(summary["ok"]) and bool(alarm_ui_probe.get("ok"))
    else:
        sample_offsets = () if ns.no_hmp_samples else tuple(ns.hmp_sample)
        result = run_qemu(config, hmp_sample_offsets=sample_offsets)
        summary.update(result.to_json_dict())
        pcs = [
            str(sample.get("pc"))
            for sample in result.hmp_samples
            if isinstance(sample, dict) and sample.get("pc")
        ]
        if pcs:
            summary["sampled_pcs"] = pcs
            summary["pc_progressed"] = len(set(pcs)) > 1 or pcs[-1] != f"0x{config.boot_pc & 0xFFFFFFFF:08x}"
            summary["pc_classifications"] = [classify_guest_pc(pc) for pc in pcs]
        guest_exceptions = []
        for sample in result.hmp_samples:
            if not isinstance(sample, dict):
                continue
            cp0 = sample.get("cp0")
            if not isinstance(cp0, dict):
                continue
            exception = cp0.get("exception")
            if exception and exception != "interrupt":
                guest_exceptions.append(
                    {
                        "sample_at_seconds": sample.get("sample_at_seconds"),
                        "pc": sample.get("pc"),
                        "pc_classification": classify_guest_pc(sample.get("pc")),
                        "exception": exception,
                        "epc": cp0.get("epc"),
                        "epc_classification": classify_guest_pc(cp0.get("epc")),
                        "cause": cp0.get("cause"),
                    }
                )
        if guest_exceptions:
            summary["guest_exceptions"] = guest_exceptions
        summary["ok"] = (result.returncode == 0 or result.timed_out) and not guest_exceptions
    summary["elapsed_seconds"] = round(time.perf_counter() - started, 3)

    summary_path = ns.out_dir / f"{ns.prefix}_summary.json"
    report_path = ns.out_dir / f"{ns.prefix}_report.md"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [
        "# QEMU System Probe",
        "",
        f"- Result: {'PASS' if summary['ok'] else 'FAIL'}",
        f"- Boot mode: {ns.boot_mode}",
        f"- QEMU: {resolved or ns.qemu}",
        f"- Timeout seconds: {ns.timeout}",
        "",
        "## Command",
        "",
        "```text",
        " ".join(str(part) for part in summary.get("command", command)),
        "```",
    ]
    if "failure" in summary:
        lines += ["", "## Failure", "", str(summary["failure"])]
    if "stderr" in summary and summary["stderr"]:
        lines += ["", "## Stderr", "", "```text", str(summary["stderr"])[:4000], "```"]
    guest_exceptions = summary.get("guest_exceptions")
    if isinstance(guest_exceptions, list) and guest_exceptions:
        lines += ["", "## Guest Exceptions", ""]
        for row in guest_exceptions:
            if isinstance(row, dict):
                lines.append(
                    f"- t={row.get('sample_at_seconds')}s pc={row.get('pc')} "
                    f"exception={row.get('exception')} epc={row.get('epc')}"
                )
                pc_classification = row.get("pc_classification")
                if isinstance(pc_classification, dict):
                    lines.append(
                        f"  - pc region={pc_classification.get('region')}: "
                        f"{pc_classification.get('description')}"
                    )
                epc_classification = row.get("epc_classification")
                if isinstance(epc_classification, dict):
                    lines.append(
                        f"  - epc region={epc_classification.get('region')}: "
                        f"{epc_classification.get('description')}"
                    )
    input_probe = summary.get("input_event_queue_probe")
    if isinstance(input_probe, dict):
        lines += ["", "## Input Event Queue Probe", ""]
        lines.append(f"- Result: {'PASS' if input_probe.get('ok') else 'FAIL'}")
        lines.append(f"- Queue: {input_probe.get('queue')}")
        queue_state = input_probe.get("queue_state")
        if isinstance(queue_state, dict):
            lines.append(
                f"- Header: magic={queue_state.get('magic')} count={queue_state.get('count')} "
                f"read={queue_state.get('read_idx')} write={queue_state.get('write_idx')}"
            )
            slots = queue_state.get("slots")
            if isinstance(slots, list):
                for slot in slots[: min(8, len(slots))]:
                    if isinstance(slot, dict):
                        lines.append(
                            f"- slot {slot.get('index')}: code={slot.get('code')} "
                            f"kind={slot.get('kind')} arg0={slot.get('arg0')} "
                            f"arg1={slot.get('arg1')} arg2={slot.get('arg2')}"
                        )
        scheduler_wake = input_probe.get("scheduler_wake")
        if isinstance(scheduler_wake, dict):
            lines += ["", "### Scheduler Wake", ""]
            lines.append(
                f"- task9={scheduler_wake.get('task9_node')} "
                f"valid={scheduler_wake.get('task9_node_valid')}"
            )
            lines.append(
                f"- group={scheduler_wake.get('group_80473f38')} "
                f"ready={scheduler_wake.get('ready_80473f41')} "
                f"countdown={scheduler_wake.get('countdown_80473f08')}"
            )
            lines.append(
                f"- task9_ready={scheduler_wake.get('task9_ready')} "
                f"tick_armed={scheduler_wake.get('tick_armed')}"
            )
        wait_wake = input_probe.get("wait_wake")
        if isinstance(wait_wake, dict):
            lines += ["", "### Input WAIT Wake", ""]
            lines.append(f"- Result: {'PASS' if wait_wake.get('ok') else 'FAIL'}")
            lines.append(
                f"- pc_before={wait_wake.get('pc_before')} "
                f"pc_after_input={wait_wake.get('pc_after_input')}"
            )
            lines.append(
                f"- advanced_off_wait={wait_wake.get('advanced_off_wait')} "
                f"synthetic_return={wait_wake.get('synthetic_return')}"
            )
        consume = input_probe.get("helper_consume")
        if isinstance(consume, dict):
            lines += ["", "### Helper Consume", ""]
            lines.append(f"- Result: {'PASS' if consume.get('ok') else 'FAIL'}")
            lines.append(
                f"- pc_after={consume.get('pc_after')} v0={consume.get('v0_after')} "
                f"a1={consume.get('a1_after')} stop={consume.get('stop_reply')}"
            )
            scratch = consume.get("scratch")
            if isinstance(scratch, dict):
                lines.append(
                    f"- scratch: code={scratch.get('code')} kind={scratch.get('kind')} "
                    f"arg0={scratch.get('arg0')} arg1={scratch.get('arg1')} "
                    f"arg2={scratch.get('arg2')}"
                )
            queue_after = consume.get("queue_after")
            if isinstance(queue_after, dict):
                lines.append(
                    f"- queue_after: count={queue_after.get('count')} "
                    f"read={queue_after.get('read_idx')} write={queue_after.get('write_idx')}"
                )
            lines.append(f"- consumed_slot_cleared={consume.get('consumed_slot_cleared')}")
        valid_record_consume = input_probe.get("valid_record_consume")
        if isinstance(valid_record_consume, dict):
            lines += ["", "### Valid Empty Record Consume", ""]
            lines.append(
                f"- Result: {'PASS' if valid_record_consume.get('ok') else 'FAIL'}"
            )
            lines.append(
                f"- pc_after={valid_record_consume.get('pc_after')} "
                f"v0={valid_record_consume.get('v0_after')} "
                f"a1={valid_record_consume.get('a1_after')} "
                f"stop={valid_record_consume.get('stop_reply')}"
            )
            record = valid_record_consume.get("record")
            if isinstance(record, dict):
                lines.append(
                    f"- record: code={record.get('code')} kind={record.get('kind')} "
                    f"arg0={record.get('arg0')} arg1={record.get('arg1')} "
                    f"arg2={record.get('arg2')}"
                )
            queue_after = valid_record_consume.get("queue_after")
            if isinstance(queue_after, dict):
                lines.append(
                    f"- queue_after: count={queue_after.get('count')} "
                    f"read={queue_after.get('read_idx')} write={queue_after.get('write_idx')}"
                )
        if input_probe.get("error"):
            lines.append(f"- Error: {input_probe.get('error')}")
    udc_probe = summary.get("udc_idle_probe")
    if isinstance(udc_probe, dict):
        lines += ["", "## UDC Idle Probe", ""]
        lines.append(f"- Result: {'PASS' if udc_probe.get('ok') else 'FAIL'}")
        lines.append(f"- base={udc_probe.get('base')} pc={udc_probe.get('pc')}")
        words = udc_probe.get("words")
        if isinstance(words, list):
            lines.append(f"- words={words}")
        registers = udc_probe.get("registers")
        if isinstance(registers, dict):
            lines.append(f"- registers={registers}")
        if udc_probe.get("error"):
            lines.append(f"- Error: {udc_probe.get('error')}")
    sadc_probe = summary.get("sadc_battery_probe")
    if isinstance(sadc_probe, dict):
        lines += ["", "## SADC Battery Probe", ""]
        lines.append(f"- Result: {'PASS' if sadc_probe.get('ok') else 'FAIL'}")
        lines.append(f"- code={sadc_probe.get('code')} status={sadc_probe.get('status_buffer')}")
        status_low = sadc_probe.get("status_low")
        if isinstance(status_low, list):
            lines.append(f"- status_low={status_low}")
        if sadc_probe.get("error"):
            lines.append(f"- Error: {sadc_probe.get('error')}")
    rtc_probe = summary.get("rtc_hibernate_probe")
    if isinstance(rtc_probe, dict):
        lines += ["", "## RTC Hibernate Probe", ""]
        lines.append(f"- Result: {'PASS' if rtc_probe.get('ok') else 'FAIL'}")
        lines.append(f"- code={rtc_probe.get('code')} status={rtc_probe.get('status_buffer')}")
        status_words = rtc_probe.get("status_words")
        if isinstance(status_words, list):
            lines.append(f"- status_words={status_words}")
        if rtc_probe.get("error"):
            lines.append(f"- Error: {rtc_probe.get('error')}")
    rtc_alarm_probe = summary.get("rtc_alarm_irq_probe")
    if isinstance(rtc_alarm_probe, dict):
        lines += ["", "## RTC Alarm IRQ Probe", ""]
        lines.append(f"- Result: {'PASS' if rtc_alarm_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- code={rtc_alarm_probe.get('code')} "
            f"status={rtc_alarm_probe.get('status_buffer')}"
        )
        status_words = rtc_alarm_probe.get("status_words")
        if isinstance(status_words, list):
            lines.append(f"- status_words={status_words}")
        if rtc_alarm_probe.get("error"):
            lines.append(f"- Error: {rtc_alarm_probe.get('error')}")
    storage_probe = summary.get("storage_layout_probe")
    if isinstance(storage_probe, dict):
        lines += ["", "## Storage Layout Probe", ""]
        lines.append(f"- Result: {'PASS' if storage_probe.get('ok') else 'FAIL'}")
        lines.append(f"- pc={storage_probe.get('pc')}")
        values = storage_probe.get("values")
        if isinstance(values, dict):
            lines.append(f"- values={values}")
        mismatches = storage_probe.get("mismatches")
        if isinstance(mismatches, dict) and mismatches:
            lines.append(f"- mismatches={mismatches}")
        if storage_probe.get("error"):
            lines.append(f"- Error: {storage_probe.get('error')}")
    cache_probe = summary.get("cache_scan_probe")
    if isinstance(cache_probe, dict):
        lines += ["", "## Cache Scan Probe", ""]
        lines.append(f"- Result: {'PASS' if cache_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- breakpoint={cache_probe.get('breakpoint')} "
            f"max_events_reached={cache_probe.get('max_events_reached')}"
        )
        events = cache_probe.get("events")
        if isinstance(events, list):
            for index, event in enumerate(events[:16]):
                if not isinstance(event, dict):
                    continue
                regs = event.get("registers")
                reg_text = ""
                if isinstance(regs, dict):
                    reg_text = (
                        f" v0={regs.get('v0')} s0={regs.get('s0')} "
                        f"s1={regs.get('s1')} s2={regs.get('s2')} "
                        f"s3={regs.get('s3')} ra={regs.get('ra')}"
                    )
                lines.append(
                    f"- event {index}: pc={event.get('pc')} kind={event.get('kind')} "
                    f"region={event.get('region')}{reg_text}"
                )
                cache_hit = event.get("resource_cache_hit")
                if isinstance(cache_hit, dict):
                    lines.append(
                        f"  - cache_hit slot={cache_hit.get('slot')} "
                        f"sector={cache_hit.get('sector')} "
                        f"entry={cache_hit.get('entry_index')} "
                        f"value={cache_hit.get('entry_value')} "
                        f"buffer={cache_hit.get('buffer')}"
                    )
                lines.append(f"  - globals={event.get('globals')}")
                lines.append(
                    f"  - after_step_pc={event.get('after_step_pc')} "
                    f"after_step_v0={event.get('after_step_v0')} "
                    f"after_step_cursor={event.get('after_step_cursor_8047425c')}"
                )
                storage_trace = event.get("storage_trace")
                if isinstance(storage_trace, dict):
                    lines.append(
                        f"  - storage_trace seq={storage_trace.get('seq')} "
                        f"slot={storage_trace.get('slot')} "
                        f"recent={storage_trace.get('recent_entries')}"
                    )
                if event.get("resource_cache_table") is not None:
                    lines.append(
                        f"  - resource_cache_table="
                        f"{event.get('resource_cache_table')}"
                    )
                if event.get("resource_cache_slots") is not None:
                    lines.append(
                        f"  - resource_cache_slots="
                        f"{event.get('resource_cache_slots')}"
                    )
                if event.get("cluster_cache_slots") is not None:
                    lines.append(
                        f"  - cluster_cache_slots="
                        f"{event.get('cluster_cache_slots')}"
                    )
        if cache_probe.get("error"):
            lines.append(f"- Error: {cache_probe.get('error')}")
    caller_probe = summary.get("cache_scan_caller_probe")
    if isinstance(caller_probe, dict):
        lines += ["", "## Cache Scan Caller Probe", ""]
        lines.append(f"- Result: {'PASS' if caller_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- breakpoints={caller_probe.get('breakpoints')} "
            f"max_events_reached={caller_probe.get('max_events_reached')}"
        )
        events = caller_probe.get("events")
        if isinstance(events, list):
            for index, event in enumerate(events[:24]):
                if not isinstance(event, dict):
                    continue
                regs = event.get("registers")
                reg_text = ""
                if isinstance(regs, dict):
                    reg_text = (
                        f" v0={regs.get('v0')} a0={regs.get('a0')} "
                        f"a1={regs.get('a1')} a2={regs.get('a2')} "
                        f"a3={regs.get('a3')} s3={regs.get('s3')} "
                        f"s4={regs.get('s4')} s5={regs.get('s5')} "
                        f"s6={regs.get('s6')} fp={regs.get('fp')} "
                        f"sp={regs.get('sp')} ra={regs.get('ra')}"
                    )
                lines.append(
                    f"- event {index}: pc={event.get('pc')} "
                    f"region={event.get('region')}{reg_text}"
                )
                lines.append(
                    f"  - after_step_pc={event.get('after_step_pc')} "
                    f"after_step_v0={event.get('after_step_v0')}"
                )
                lines.append(f"  - globals={event.get('globals')}")
                if event.get("file_context_decoded") is not None:
                    lines.append(
                        f"  - file_context={event.get('file_context_decoded')}"
                    )
                if event.get("parent_saved_ra_guess") is not None:
                    lines.append(
                        f"  - parent_saved_ra_guess="
                        f"{event.get('parent_saved_ra_guess')}"
                    )
                if event.get("file_object_words_00_4c") is not None:
                    lines.append(
                        f"  - file_object_words={event.get('file_object_words_00_4c')}"
                    )
                storage_trace = event.get("storage_trace")
                if isinstance(storage_trace, dict):
                    lines.append(
                        f"  - storage_trace seq={storage_trace.get('seq')} "
                        f"slot={storage_trace.get('slot')} "
                        f"recent={storage_trace.get('recent_entries')}"
                    )
                if event.get("resource_cache_slots") is not None:
                    lines.append(
                        f"  - resource_cache_slots="
                        f"{event.get('resource_cache_slots')}"
                    )
                if event.get("cluster_cache_slots") is not None:
                    lines.append(
                        f"  - cluster_cache_slots="
                        f"{event.get('cluster_cache_slots')}"
                    )
        if caller_probe.get("error"):
            lines.append(f"- Error: {caller_probe.get('error')}")
    fat_cache_probe = summary.get("fat_cache_io_probe")
    if isinstance(fat_cache_probe, dict):
        lines += ["", "## FAT Cache I/O Probe", ""]
        lines.append(f"- Result: {'PASS' if fat_cache_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- breakpoints={fat_cache_probe.get('breakpoints')} "
            f"max_events_reached={fat_cache_probe.get('max_events_reached')}"
        )
        events = fat_cache_probe.get("events")
        if isinstance(events, list):
            for index, event in enumerate(events[:32]):
                if not isinstance(event, dict):
                    continue
                regs = event.get("registers")
                reg_text = ""
                if isinstance(regs, dict):
                    reg_text = (
                        f" a0={regs.get('a0')} a1={regs.get('a1')} "
                        f"a2={regs.get('a2')} a3={regs.get('a3')} "
                        f"s0={regs.get('s0')} s1={regs.get('s1')} "
                        f"s2={regs.get('s2')} s3={regs.get('s3')} "
                        f"s4={regs.get('s4')} s5={regs.get('s5')} "
                        f"sp={regs.get('sp')} ra={regs.get('ra')}"
                    )
                lines.append(
                    f"- event {index}: pc={event.get('pc')} "
                    f"kind={event.get('kind')} region={event.get('region')}"
                    f"{reg_text}"
                )
                lines.append(
                    f"  - after_step_pc={event.get('after_step_pc')} "
                    f"after_step_v0={event.get('after_step_v0')}"
                )
                lines.append(f"  - globals={event.get('globals')}")
                storage_trace = event.get("storage_trace")
                if isinstance(storage_trace, dict):
                    lines.append(
                        f"  - storage_trace seq={storage_trace.get('seq')} "
                        f"slot={storage_trace.get('slot')} "
                        f"recent={storage_trace.get('recent_entries')}"
                    )
                if event.get("resource_cache_slots") is not None:
                    lines.append(
                        f"  - resource_cache_slots="
                        f"{event.get('resource_cache_slots')}"
                    )
                if event.get("cluster_cache_slots") is not None:
                    lines.append(
                        f"  - cluster_cache_slots={event.get('cluster_cache_slots')}"
                    )
        if fat_cache_probe.get("error"):
            lines.append(f"- Error: {fat_cache_probe.get('error')}")
    msc_dma_probe = summary.get("msc_dma_write_probe")
    if isinstance(msc_dma_probe, dict):
        lines += ["", "## MSC DMA Write Probe", ""]
        lines.append(f"- Result: {'PASS' if msc_dma_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- lba={msc_dma_probe.get('lba')} "
            f"matched={msc_dma_probe.get('matched')} "
            f"status_lifecycle_ok={msc_dma_probe.get('status_lifecycle_ok')} "
            f"code_size={msc_dma_probe.get('code_size')}"
        )
        lines.append(
            f"- write_buffer={msc_dma_probe.get('write_buffer')} "
            f"read_buffer={msc_dma_probe.get('read_buffer')} "
            f"status_buffer={msc_dma_probe.get('status_buffer')}"
        )
        if msc_dma_probe.get("status_words") is not None:
            lines.append(f"- status_words={msc_dma_probe.get('status_words')}")
        lines.append(
            f"- expected_first16={msc_dma_probe.get('expected_first16')} "
            f"readback_first16={msc_dma_probe.get('readback_first16')}"
        )
        call = msc_dma_probe.get("guest_call")
        if isinstance(call, dict):
            lines.append(
                f"- guest_call returned={call.get('returned')} "
                f"final_pc={call.get('final_pc')} v0={call.get('v0')} "
                f"steps={call.get('step_count')}"
            )
            lines.append("- lifecycle: initial may be busy if firmware was already in NAND I/O; probe verifies ack -> busy -> ready -> ack")
            if call.get("error"):
                lines.append(f"  - call_error={call.get('error')}")
        storage_trace = msc_dma_probe.get("storage_trace_after")
        if isinstance(storage_trace, dict):
            lines.append(
                f"- storage_trace seq={storage_trace.get('seq')} "
                f"slot={storage_trace.get('slot')} "
                f"recent={storage_trace.get('recent_entries')}"
            )
        if msc_dma_probe.get("error"):
            lines.append(f"- Error: {msc_dma_probe.get('error')}")
    uart_probe = summary.get("uart_register_probe")
    if isinstance(uart_probe, dict):
        lines += ["", "## UART Register Probe", ""]
        lines.append(f"- Result: {'PASS' if uart_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- status_lifecycle_ok={uart_probe.get('status_lifecycle_ok')} "
            f"code_size={uart_probe.get('code_size')} "
            f"status_buffer={uart_probe.get('status_buffer')}"
        )
        if uart_probe.get("status_low") is not None:
            lines.append(f"- status_low={uart_probe.get('status_low')}")
        if uart_probe.get("status_words") is not None:
            lines.append(f"- status_words={uart_probe.get('status_words')}")
        call = uart_probe.get("guest_call")
        if isinstance(call, dict):
            lines.append(
                f"- guest_call returned={call.get('returned')} "
                f"final_pc={call.get('final_pc')} v0={call.get('v0')} "
                f"steps={call.get('step_count')}"
            )
            if call.get("error"):
                lines.append(f"  - call_error={call.get('error')}")
        if uart_probe.get("error"):
            lines.append(f"- Error: {uart_probe.get('error')}")
    bch_probe = summary.get("bch_status_probe")
    if isinstance(bch_probe, dict):
        lines += ["", "## BCH/ECC Status Probe", ""]
        lines.append(f"- Result: {'PASS' if bch_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- status_lifecycle_ok={bch_probe.get('status_lifecycle_ok')} "
            f"code_size={bch_probe.get('code_size')} "
            f"status_buffer={bch_probe.get('status_buffer')}"
        )
        if bch_probe.get("status_words") is not None:
            lines.append(f"- status_words={bch_probe.get('status_words')}")
        call = bch_probe.get("guest_call")
        if isinstance(call, dict):
            lines.append(
                f"- guest_call returned={call.get('returned')} "
                f"final_pc={call.get('final_pc')} v0={call.get('v0')} "
                f"steps={call.get('step_count')}"
            )
            if call.get("error"):
                lines.append(f"  - call_error={call.get('error')}")
        if bch_probe.get("error"):
            lines.append(f"- Error: {bch_probe.get('error')}")
    lcd_probe = summary.get("lcd_frame_done_probe")
    if isinstance(lcd_probe, dict):
        lines += ["", "## LCD Frame-Done Probe", ""]
        lines.append(f"- Result: {'PASS' if lcd_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- baseline_frame_done={lcd_probe.get('baseline_frame_done')} "
            f"ack_cleared_frame_done={lcd_probe.get('ack_cleared_frame_done')} "
            f"changed_frame_done={lcd_probe.get('changed_frame_done')} "
            f"ready_bits_ok={lcd_probe.get('ready_bits_ok')}"
        )
        lines.append(
            f"- framebuffer={lcd_probe.get('framebuffer')} "
            f"status_buffer={lcd_probe.get('status_buffer')} "
            f"status_words={lcd_probe.get('status_words')}"
        )
        ack_call = lcd_probe.get("ack_guest_call")
        if isinstance(ack_call, dict):
            lines.append(
                f"- ack_guest_call returned={ack_call.get('returned')} "
                f"final_pc={ack_call.get('final_pc')} steps={ack_call.get('step_count')}"
            )
        read_call = lcd_probe.get("read_guest_call")
        if isinstance(read_call, dict):
            lines.append(
                f"- read_guest_call returned={read_call.get('returned')} "
                f"final_pc={read_call.get('final_pc')} steps={read_call.get('step_count')}"
            )
        if lcd_probe.get("error"):
            lines.append(f"- Error: {lcd_probe.get('error')}")
    watch_probe = summary.get("config_cluster_watch_probe")
    if isinstance(watch_probe, dict):
        lines += ["", "## CONFIG Cluster Watch Probe", ""]
        lines.append(f"- Result: {'PASS' if watch_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- found_config_object={watch_probe.get('found_config_object')} "
            f"watch_hit={watch_probe.get('watch_hit')} "
            f"object={watch_probe.get('config_object_va')}"
        )
        if watch_probe.get("config_object_at_find") is not None:
            lines.append(
                f"- config_object_at_find={watch_probe.get('config_object_at_find')}"
            )
        event = watch_probe.get("watch_event")
        if isinstance(event, dict):
            registers = event.get("registers")
            reg_text = ""
            if isinstance(registers, dict):
                reg_text = (
                    f" v0={registers.get('v0')} a0={registers.get('a0')} "
                    f"a1={registers.get('a1')} a2={registers.get('a2')} "
                    f"a3={registers.get('a3')} s3={registers.get('s3')} "
                    f"s5={registers.get('s5')} sp={registers.get('sp')} "
                    f"ra={registers.get('ra')}"
                )
            lines.append(
                f"- watch_event pc={event.get('pc')} region={event.get('region')}"
                f" write_class={event.get('write_class')}{reg_text}"
            )
            lines.append(
                f"  - object={event.get('object_va')} watch={event.get('watch_va')} "
                f"parent_saved_ra_guess={event.get('parent_saved_ra_guess')}"
            )
            lines.append(
                f"  - file_object={event.get('file_object_decoded')}"
            )
            if event.get("file_rw_live_frame") is not None:
                lines.append(
                    f"  - file_rw_live_frame={event.get('file_rw_live_frame')}"
                )
            if event.get("parent_80171154_frame") is not None:
                lines.append(
                    f"  - parent_80171154_frame="
                    f"{event.get('parent_80171154_frame')}"
                )
            if event.get("grandparent_8017ab2c_frame") is not None:
                lines.append(
                    f"  - grandparent_8017ab2c_frame="
                    f"{event.get('grandparent_8017ab2c_frame')}"
                )
            if event.get("resource_cache_slots") is not None:
                lines.append(
                    f"  - resource_cache_slots={event.get('resource_cache_slots')}"
                )
            if event.get("cluster_cache_slots") is not None:
                lines.append(
                    f"  - cluster_cache_slots={event.get('cluster_cache_slots')}"
                )
            storage_trace = event.get("storage_trace")
            if isinstance(storage_trace, dict):
                lines.append(
                    f"  - storage_trace seq={storage_trace.get('seq')} "
                    f"recent={storage_trace.get('recent_entries')}"
                )
            nand_target_trace = event.get("nand_target_trace")
            if isinstance(nand_target_trace, dict):
                lines.append(
                    f"  - nand_target_trace seq={nand_target_trace.get('seq')} "
                    f"recent={nand_target_trace.get('recent_entries')}"
                )
        for key in (
            "timeout_waiting_for_config_object",
            "timeout_waiting_for_watchpoint",
            "unexpected_find_pc",
            "watch_remove_error",
            "breakpoint_remove_error",
            "resume_error",
            "error",
        ):
            if watch_probe.get(key):
                lines.append(f"- {key}={watch_probe.get(key)}")
    file_open_probe = summary.get("file_open_context_probe")
    if isinstance(file_open_probe, dict):
        lines += ["", "## File Open Context Probe", ""]
        lines.append(
            f"- Result: {'PASS' if file_open_probe.get('ok') else 'FAIL'}"
        )
        lines.append(
            f"- breakpoints={file_open_probe.get('breakpoints')} "
            f"max_events_reached={file_open_probe.get('max_events_reached')}"
        )
        events = file_open_probe.get("events")
        if isinstance(events, list):
            for index, event in enumerate(events[:32]):
                if not isinstance(event, dict):
                    continue
                regs = event.get("registers")
                reg_text = ""
                if isinstance(regs, dict):
                    reg_text = (
                        f" v0={regs.get('v0')} a0={regs.get('a0')} "
                        f"a1={regs.get('a1')} a2={regs.get('a2')} "
                        f"s0={regs.get('s0')} s3={regs.get('s3')} "
                        f"s6={regs.get('s6')} fp={regs.get('fp')} "
                        f"ra={regs.get('ra')}"
                    )
                lines.append(
                    f"- event {index}: pc={event.get('pc')} "
                    f"kind={event.get('kind')} region={event.get('region')}"
                    f"{reg_text}"
                )
                decoded = event.get("decoded_candidates")
                if isinstance(decoded, dict):
                    compact = {
                        name: {
                            key: value
                            for key, value in row.items()
                            if key
                            in {
                                "name_words",
                                "cluster_18",
                                "size_20",
                                "size_2c",
                                "cluster_30",
                                "aux_cluster_34",
                                "current_cluster_38",
                                "current_offset_44",
                                "status_48",
                            }
                        }
                        for name, row in decoded.items()
                        if isinstance(row, dict)
                    }
                    lines.append(f"  - decoded_candidates={compact}")
                if event.get("file_context_decoded") is not None:
                    lines.append(
                        f"  - file_context={event.get('file_context_decoded')}"
                    )
                if event.get("a2_context_decoded") is not None:
                    lines.append(
                        f"  - a2_context={event.get('a2_context_decoded')}"
                    )
                if event.get("a3_context_decoded") is not None:
                    lines.append(
                        f"  - a3_context={event.get('a3_context_decoded')}"
                    )
                storage_trace = event.get("storage_trace")
                if isinstance(storage_trace, dict):
                    lines.append(
                        f"  - storage_trace seq={storage_trace.get('seq')} "
                        f"recent={storage_trace.get('recent_entries')}"
                    )
                cluster_trace = event.get("cluster_trace")
                if isinstance(cluster_trace, dict):
                    lines.append(f"  - cluster_trace={cluster_trace}")
        if file_open_probe.get("error"):
            lines.append(f"- Error: {file_open_probe.get('error')}")
    semaphore_probe = summary.get("semaphore_flow_probe")
    if isinstance(semaphore_probe, dict):
        lines += ["", "## Semaphore Flow Probe", ""]
        lines.append(
            f"- Result: {'PASS' if semaphore_probe.get('ok') else 'FAIL'}"
        )
        lines.append(
            f"- waits={semaphore_probe.get('wait_count')} "
            f"releases={semaphore_probe.get('release_count')} "
            f"hit_resource_object_release={semaphore_probe.get('hit_resource_object_release')} "
            f"max_events_reached={semaphore_probe.get('max_events_reached')}"
        )
        lines.append(
            f"- resource_only={semaphore_probe.get('resource_only')} "
            f"stop_on_resource_release={semaphore_probe.get('stop_on_resource_release')}"
        )
        events = semaphore_probe.get("events")
        if isinstance(events, list):
            for index, event in enumerate(events[:16]):
                if not isinstance(event, dict):
                    continue
                registers = event.get("registers")
                reg_text = ""
                if isinstance(registers, dict):
                    reg_text = (
                        f" a0={registers.get('a0')} a2={registers.get('a2')} "
                        f"ra={registers.get('ra')}"
                    )
                lines.append(
                    f"- event {index}: pc={event.get('pc')} "
                    f"region={event.get('region')}{reg_text}"
                )
                lines.append(f"  - object_words={event.get('object_words')}")
                if event.get("object_ascii") is not None:
                    lines.append(f"  - object_ascii={event.get('object_ascii')}")
                if event.get("resource_words") is not None:
                    lines.append(f"  - resource_words={event.get('resource_words')}")
                if event.get("resource_globals") is not None:
                    lines.append(f"  - resource_globals={event.get('resource_globals')}")
                if event.get("resource_object_table_head") is not None:
                    lines.append(
                        f"  - resource_object_table_head="
                        f"{event.get('resource_object_table_head')}"
                    )
                post_steps = event.get("post_steps")
                if isinstance(post_steps, list):
                    for step in post_steps[:12]:
                        if not isinstance(step, dict):
                            continue
                        regs = step.get("registers")
                        reg_text = ""
                        if isinstance(regs, dict):
                            reg_text = (
                                f" a0={regs.get('a0')} a3={regs.get('a3')} "
                                f"badva={regs.get('badva')} cause={regs.get('cause')}"
                            )
                        lines.append(
                            f"  - step {step.get('step')}: pc={step.get('pc')} "
                            f"region={step.get('region')}{reg_text}"
                        )
        if semaphore_probe.get("error"):
            lines.append(f"- Error: {semaphore_probe.get('error')}")
    exception_probe = summary.get("exception_context_probe")
    if isinstance(exception_probe, dict):
        lines += ["", "## Exception Context Probe", ""]
        lines.append(
            f"- Result: {'PASS' if exception_probe.get('ok') else 'FAIL'}"
        )
        registers = exception_probe.get("registers")
        if isinstance(registers, dict):
            lines.append(
                f"- pc={registers.get('pc')} epc={registers.get('epc')} "
                f"badva={registers.get('badva')} cause={registers.get('cause')}"
            )
            lines.append(
                f"- a0={registers.get('a0')} a1={registers.get('a1')} "
                f"a2={registers.get('a2')} a3={registers.get('a3')} "
                f"ra={registers.get('ra')} sp={registers.get('sp')}"
            )
        pc_classification = exception_probe.get("pc_classification")
        epc_classification = exception_probe.get("epc_classification")
        if isinstance(pc_classification, dict):
            lines.append(f"- pc region={pc_classification.get('region')}")
        if isinstance(epc_classification, dict):
            lines.append(f"- epc region={epc_classification.get('region')}")
        lines.append(f"- a0_words={exception_probe.get('a0_words')}")
        lines.append(f"- a0_ascii={exception_probe.get('a0_ascii')}")
        lines.append(f"- badva_words={exception_probe.get('badva_words')}")
        lines.append(f"- badva_ascii={exception_probe.get('badva_ascii')}")
        if exception_probe.get("error"):
            lines.append(f"- Error: {exception_probe.get('error')}")
    poweroff_probe = summary.get("poweroff_path_probe")
    if isinstance(poweroff_probe, dict):
        lines += ["", "## Poweroff Path Probe", ""]
        lines.append(f"- Result: {'PASS' if poweroff_probe.get('ok') else 'FAIL'}")
        lines.append(f"- breakpoints={poweroff_probe.get('breakpoints')}")
        events = poweroff_probe.get("events")
        if isinstance(events, list):
            lines.append(f"- events={len(events)}")
            for event in events[:16]:
                if isinstance(event, dict):
                    regs = event.get("registers")
                    lines.append(
                        f"- pc={event.get('pc')} kind={event.get('kind')} "
                        f"stop={event.get('stop_reply')}"
                    )
                    if isinstance(regs, dict):
                        lines.append(
                            f"  - ra={regs.get('ra')} sp={regs.get('sp')} "
                            f"a0={regs.get('a0')} v0={regs.get('v0')} "
                            f"epc={regs.get('epc')} cause={regs.get('cause')}"
                        )
                    lines.append(
                        f"  - rtc3000={event.get('rtc_status_b0003000')} "
                        f"3020={event.get('rtc_reg3020_b0003020')} "
                        f"3024={event.get('rtc_reg3024_b0003024')} "
                        f"3028={event.get('rtc_reg3028_b0003028')}"
                    )
                    a0_ascii = event.get("a0_ascii")
                    if a0_ascii:
                        lines.append(f"  - a0_ascii={a0_ascii}")
        if poweroff_probe.get("error"):
            lines.append(f"- Error: {poweroff_probe.get('error')}")
    null_release_probe = summary.get("null_resource_release_probe")
    if isinstance(null_release_probe, dict):
        lines += ["", "## Null Resource Release Probe", ""]
        lines.append(
            f"- Result: {'PASS' if null_release_probe.get('ok') else 'FAIL'}"
        )
        lines.append(
            f"- hit_null_release={null_release_probe.get('hit_null_release')} "
            f"breakpoints={null_release_probe.get('breakpoints')}"
        )
        events = null_release_probe.get("events")
        if isinstance(events, list):
            if len(events) <= 10:
                display_events = list(enumerate(events))
            else:
                display_events = [
                    *list(enumerate(events[:6])),
                    *list(enumerate(events[-4:], start=len(events) - 4)),
                ]
            for index, event in display_events:
                if not isinstance(event, dict):
                    continue
                regs = event.get("registers")
                a0 = regs.get("a0") if isinstance(regs, dict) else None
                lines.append(
                    f"- event {index}: pc={event.get('pc')} kind={event.get('kind')} "
                    f"a0={a0} null={event.get('null_resource_argument')}"
                )
                if event.get("message"):
                    lines.append(f"  - message={event.get('message')}")
                pointer_context = event.get("pointer_context")
                if isinstance(pointer_context, dict):
                    for name, context in pointer_context.items():
                        if not isinstance(context, dict):
                            continue
                        lines.append(
                            f"  - {name}: value={context.get('value')} "
                            f"cstring={context.get('cstring')} "
                            f"gbk={context.get('gbk_cstring')} "
                            f"words={context.get('words')}"
                        )
                if event.get("stack_words") is not None:
                    lines.append(f"  - stack_words={event.get('stack_words')}")
                if event.get("resource_globals") is not None:
                    lines.append(
                        f"  - resource_globals={event.get('resource_globals')}"
                    )
                if event.get("resource_object_table_head") is not None:
                    lines.append(
                        f"  - resource_object_table_head="
                        f"{event.get('resource_object_table_head')}"
                    )
        if null_release_probe.get("error"):
            lines.append(f"- Error: {null_release_probe.get('error')}")
    alarm_audio_probe = summary.get("alarm_audio_open_probe")
    if isinstance(alarm_audio_probe, dict):
        lines += ["", "## Alarm Audio Open Probe", ""]
        lines.append(f"- Result: {'PASS' if alarm_audio_probe.get('ok') else 'FAIL'}")
        lines.append(f"- breakpoints={alarm_audio_probe.get('breakpoints')}")
        lines.append(
            f"- hit_alarm_db_open={alarm_audio_probe.get('hit_alarm_db_open')} "
            f"hit_audio_call_return={alarm_audio_probe.get('hit_audio_call_return')} "
            f"hit_openfail={alarm_audio_probe.get('hit_openfail')}"
        )
        if alarm_audio_probe.get("cluster_trace") is not None:
            lines.append(f"- cluster_trace={alarm_audio_probe.get('cluster_trace')}")
        if alarm_audio_probe.get("cluster_cache_table") is not None:
            lines.append(
                f"- cluster_cache_table={alarm_audio_probe.get('cluster_cache_table')}"
            )
        if alarm_audio_probe.get("cluster_cache_slots") is not None:
            lines.append(
                f"- cluster_cache_slots={alarm_audio_probe.get('cluster_cache_slots')}"
            )
        if alarm_audio_probe.get("storage_trace") is not None:
            lines.append(f"- storage_trace={alarm_audio_probe.get('storage_trace')}")
        events = alarm_audio_probe.get("events")
        if isinstance(events, list):
            for index, event in enumerate(events[:24]):
                if not isinstance(event, dict):
                    continue
                regs = event.get("registers")
                reg_text = ""
                if isinstance(regs, dict):
                    reg_text = (
                        f" v0={regs.get('v0')} v1={regs.get('v1')} "
                        f"a0={regs.get('a0')} s1={regs.get('s1')} "
                        f"s2={regs.get('s2')} s3={regs.get('s3')}"
                    )
                lines.append(
                    f"- event {index}: pc={event.get('pc')} "
                    f"kind={event.get('kind')}{reg_text}"
                )
                if event.get("arg_words") is not None:
                    lines.append(f"  - arg_words={event.get('arg_words')}")
                path_context = event.get("path_context")
                if isinstance(path_context, dict):
                    lines.append(
                        f"  - path={path_context.get('value')} "
                        f"cstring={path_context.get('cstring')} "
                        f"gbk={path_context.get('gbk_cstring')} "
                        f"words={path_context.get('words')}"
                    )
                resource_context = event.get("resource_context")
                if isinstance(resource_context, dict):
                    lines.append(
                        f"  - resource={resource_context.get('value')} "
                        f"words={resource_context.get('words')} "
                        f"ascii={resource_context.get('ascii')}"
                    )
                construct_context = event.get("resource_object_context")
                if isinstance(construct_context, dict):
                    lines.append(
                        f"  - construct_resource={construct_context.get('value')} "
                        f"words={construct_context.get('words')} "
                        f"ascii={construct_context.get('ascii')}"
                    )
                work_context = event.get("work_object_context")
                if isinstance(work_context, dict):
                    lines.append(
                        f"  - work_object={work_context.get('value')} "
                        f"gbk={work_context.get('gbk_cstring')} "
                        f"words={work_context.get('words')}"
                    )
                if event.get("stack_probe_words") is not None:
                    lines.append(
                        f"  - stack_probe_words={event.get('stack_probe_words')}"
                    )
                if event.get("drive_ready_8047428d") is not None:
                    lines.append(
                        f"  - drive_ready={event.get('drive_ready_8047428d')} "
                        f"fat_globals={event.get('fat_globals')}"
                    )
                if event.get("resource_globals") is not None:
                    lines.append(
                        f"  - resource_globals={event.get('resource_globals')}"
                    )
                if event.get("cluster_read_args") is not None:
                    lines.append(
                        f"  - cluster_read_args={event.get('cluster_read_args')}"
                    )
                if event.get("cluster_read_return") is not None:
                    lines.append(
                        f"  - cluster_read_return={event.get('cluster_read_return')} "
                        f"buffer={event.get('cluster_buffer')}"
                    )
                if event.get("cluster_buffer_words_00_3c") is not None:
                    lines.append(
                        "  - cluster_buffer_words="
                        f"{event.get('cluster_buffer_words_00_3c')}"
                    )
                if event.get("cluster_buffer_bytes_00_3f") is not None:
                    lines.append(
                        "  - cluster_buffer_bytes="
                        f"{event.get('cluster_buffer_bytes_00_3f')}"
                    )
        if alarm_audio_probe.get("error"):
            lines.append(f"- Error: {alarm_audio_probe.get('error')}")
    irq24_probe = summary.get("irq24_handler_probe")
    if isinstance(irq24_probe, dict):
        lines += ["", "## IRQ24 Handler Probe", ""]
        lines.append(f"- Result: {'PASS' if irq24_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- handler={irq24_probe.get('handler')} arg={irq24_probe.get('arg')} "
            f"pc={irq24_probe.get('pc')}"
        )
        lines.append(
            f"- pending={irq24_probe.get('pending')} mask={irq24_probe.get('mask')} "
            f"unmasked={irq24_probe.get('unmasked_pending')}"
        )
        if irq24_probe.get("error"):
            lines.append(f"- Error: {irq24_probe.get('error')}")
    gui_poller_probe = summary.get("gui_event_poller_probe")
    if isinstance(gui_poller_probe, dict):
        lines += ["", "## GUI Event Poller Probe", ""]
        lines.append(f"- Result: {'PASS' if gui_poller_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- pc_after={gui_poller_probe.get('pc_after')} "
            f"read_index_after={gui_poller_probe.get('read_index_after')} "
            f"flags_after={gui_poller_probe.get('flags_after')}"
        )
        scratch_words = gui_poller_probe.get("scratch_words")
        if isinstance(scratch_words, list):
            lines.append(f"- scratch_words={scratch_words}")
        if gui_poller_probe.get("error"):
            lines.append(f"- Error: {gui_poller_probe.get('error')}")
    gui_touch_ring_probe = summary.get("gui_touch_ring_probe")
    if isinstance(gui_touch_ring_probe, dict):
        lines += ["", "## GUI Touch Ring Probe", ""]
        lines.append(
            f"- Result: {'PASS' if gui_touch_ring_probe.get('ok') else 'FAIL'}"
        )
        lines.append(
            f"- sent={gui_touch_ring_probe.get('sent')} "
            f"write_index_after={gui_touch_ring_probe.get('write_index_after')} "
            f"flags_after={gui_touch_ring_probe.get('flags_after')}"
        )
        lines.append(f"- expected_packed={gui_touch_ring_probe.get('expected_packed')}")
        record_words = gui_touch_ring_probe.get("record_words")
        if isinstance(record_words, list):
            lines.append(f"- record_words={record_words}")
        if gui_touch_ring_probe.get("error"):
            lines.append(f"- Error: {gui_touch_ring_probe.get('error')}")
    gui_touch_natural_probe = summary.get("gui_touch_natural_probe")
    if isinstance(gui_touch_natural_probe, dict):
        lines += ["", "## GUI Touch Natural Probe", ""]
        lines.append(
            f"- Result: {'PASS' if gui_touch_natural_probe.get('ok') else 'FAIL'}"
        )
        lines.append(
            f"- touch_applied={gui_touch_natural_probe.get('touch_applied')} "
            f"enqueued={gui_touch_natural_probe.get('enqueued')} "
            f"consumed={gui_touch_natural_probe.get('consumed')} "
            f"ready={gui_touch_natural_probe.get('ready')}"
        )
        lines.append(
            f"- calibrate_first={gui_touch_natural_probe.get('calibrate_first')} "
            f"release={gui_touch_natural_probe.get('release')}"
        )
        lines.append(
            f"- read/write before="
            f"{gui_touch_natural_probe.get('read_index_before')}/"
            f"{gui_touch_natural_probe.get('write_index_before')} "
            f"after={gui_touch_natural_probe.get('read_index_after')}/"
            f"{gui_touch_natural_probe.get('write_index_after')} "
            f"flags_after={gui_touch_natural_probe.get('flags_after')}"
        )
        touch_record = gui_touch_natural_probe.get("touch_record_seen")
        if isinstance(touch_record, dict):
            lines.append(
                f"- touch_record_seen={touch_record.get('seen')} "
                f"index={touch_record.get('index')} "
                f"expected={touch_record.get('expected_packed')}"
            )
        touch_device = gui_touch_natural_probe.get("touch_device_after_touch")
        if isinstance(touch_device, dict):
            lines.append(
                f"- touch_device down={touch_device.get('touch_down')} "
                f"raw={touch_device.get('touch_raw_x')}/{touch_device.get('touch_raw_y')} "
                f"sadc={touch_device.get('sadc_status_event')} "
                f"reason={touch_device.get('reason')}"
            )
        if gui_touch_natural_probe.get("release"):
            lines.append(
                f"- tap_consumed={gui_touch_natural_probe.get('tap_consumed')} "
                f"down_event_consumed={gui_touch_natural_probe.get('down_event_consumed')}"
            )
            lines.append(
                f"- release_applied={gui_touch_natural_probe.get('release_touch_applied')} "
                f"release_consumed={gui_touch_natural_probe.get('release_consumed')} "
                f"release_read/write="
                f"{gui_touch_natural_probe.get('release_read_index_after')}/"
                f"{gui_touch_natural_probe.get('release_write_index_after')}"
            )
            release_record = gui_touch_natural_probe.get("release_record_seen")
            if isinstance(release_record, dict):
                lines.append(
                    f"- release_record_seen={release_record.get('seen')} "
                    f"index={release_record.get('index')} "
                    f"expected={release_record.get('expected_packed')}"
                )
        if gui_touch_natural_probe.get("error"):
            lines.append(f"- Error: {gui_touch_natural_probe.get('error')}")
        natural_samples = gui_touch_natural_probe.get("samples_after_touch")
        if isinstance(natural_samples, list):
            for sample in natural_samples[:8]:
                if not isinstance(sample, dict):
                    continue
                classification = sample.get("pc_classification")
                region = ""
                if isinstance(classification, dict):
                    region = f" region={classification.get('region')}"
                cp0 = sample.get("cp0")
                cp0_text = ""
                if isinstance(cp0, dict):
                    cp0_text = (
                        f" cp0={cp0.get('exception')}"
                        f" exl={cp0.get('exl')}"
                        f" enabled={cp0.get('pending_enabled_interrupts')}"
                        f" epc={cp0.get('epc')}"
                    )
                lines.append(
                    f"- sample t={sample.get('elapsed_after_touch')}s "
                    f"pc={sample.get('pc')}{region}{cp0_text}"
                )
        release_samples = gui_touch_natural_probe.get("samples_after_release")
        if isinstance(release_samples, list):
            for sample in release_samples[:8]:
                if not isinstance(sample, dict):
                    continue
                classification = sample.get("pc_classification")
                region = ""
                if isinstance(classification, dict):
                    region = f" region={classification.get('region')}"
                cp0 = sample.get("cp0")
                cp0_text = ""
                if isinstance(cp0, dict):
                    cp0_text = (
                        f" cp0={cp0.get('exception')}"
                        f" exl={cp0.get('exl')}"
                        f" enabled={cp0.get('pending_enabled_interrupts')}"
                        f" epc={cp0.get('epc')}"
                    )
                lines.append(
                    f"- release sample t={sample.get('elapsed_after_release')}s "
                    f"pc={sample.get('pc')}{region}{cp0_text}"
                )
    touch_calibration_probe = summary.get("touch_input_calibration_probe")
    if isinstance(touch_calibration_probe, dict):
        lines += ["", "## Touch Input Calibration Probe", ""]
        lines.append(
            f"- Result: {'PASS' if touch_calibration_probe.get('ok') else 'FAIL'}"
        )
        lines.append(
            f"- hold={touch_calibration_probe.get('hold_seconds')}s "
            f"settle={touch_calibration_probe.get('settle_seconds')}s "
            f"ready_timeout={touch_calibration_probe.get('ready_timeout')}s"
        )
        final_touch = touch_calibration_probe.get("final_touch_device")
        if isinstance(final_touch, dict):
            lines.append(
                f"- final_touch down={final_touch.get('touch_down')} "
                f"raw={final_touch.get('touch_raw_x')}/{final_touch.get('touch_raw_y')} "
                f"sadc={final_touch.get('sadc_status_event')}"
            )
        final_gui = touch_calibration_probe.get("final_gui_state")
        if isinstance(final_gui, dict):
            lines.append(
                f"- final_gui active_ready={final_gui.get('active_object_ready')} "
                f"active={final_gui.get('active_object_80474048')}"
            )
        points = touch_calibration_probe.get("points")
        if isinstance(points, list):
            for point in points[:8]:
                if not isinstance(point, dict):
                    continue
                down = point.get("down")
                up = point.get("up")
                down_applied = down.get("applied") if isinstance(down, dict) else None
                up_applied = up.get("applied") if isinstance(up, dict) else None
                after_up = point.get("after_up_touch_device")
                sadc = after_up.get("sadc_status_event") if isinstance(after_up, dict) else None
                lines.append(
                    f"- point {point.get('index')} xy={point.get('x')},{point.get('y')} "
                    f"expected_raw={point.get('expected_raw_x')}/{point.get('expected_raw_y')} "
                    f"down_applied={down_applied} up_applied={up_applied} sadc_after_up={sadc}"
                )
        if touch_calibration_probe.get("error"):
            lines.append(f"- Error: {touch_calibration_probe.get('error')}")
        samples = touch_calibration_probe.get("samples_after_calibration")
        if isinstance(samples, list):
            for sample in samples[:8]:
                if not isinstance(sample, dict):
                    continue
                classification = sample.get("pc_classification")
                region = ""
                if isinstance(classification, dict):
                    region = f" region={classification.get('region')}"
                lines.append(
                    f"- sample pc={sample.get('pc')}{region} "
                    f"gui_active={sample.get('gui_active')} "
                    f"queue_capacity={sample.get('queue_capacity')}"
                )
    cluster_read_probe = summary.get("cluster_read_path_probe")
    if isinstance(cluster_read_probe, dict):
        lines += ["", "## Cluster Read Path Probe", ""]
        lines.append(f"- Result: {'PASS' if cluster_read_probe.get('ok') else 'FAIL'}")
        lines.append(f"- breakpoints={cluster_read_probe.get('breakpoints')}")
        post_gui = cluster_read_probe.get("post_calibration_gui_state")
        if isinstance(post_gui, dict):
            lines.append(
                f"- post_calibration active_ready={post_gui.get('active_object_ready')} "
                f"active={post_gui.get('active_object_80474048')}"
            )
        events = cluster_read_probe.get("events")
        if isinstance(events, list):
            for index, event in enumerate(events[:24]):
                if not isinstance(event, dict):
                    continue
                lines.append(
                    f"- event {index}: kind={event.get('kind')} pc={event.get('pc')} "
                    f"regs={event.get('registers')}"
                )
                if event.get("call") is not None:
                    lines.append(f"  - call={event.get('call')}")
                if event.get("last_call") is not None:
                    lines.append(f"  - last_call={event.get('last_call')}")
                if event.get("return_path") is not None:
                    lines.append(f"  - return_path={event.get('return_path')}")
                if event.get("return_v0") is not None:
                    lines.append(f"  - return_v0={event.get('return_v0')}")
                if event.get("dest_words") is not None:
                    lines.append(f"  - dest_words={event.get('dest_words')}")
        if cluster_read_probe.get("cluster_trace") is not None:
            lines.append(f"- cluster_trace={cluster_read_probe.get('cluster_trace')}")
        if cluster_read_probe.get("cluster_cache_slots") is not None:
            lines.append(f"- cluster_cache_slots={cluster_read_probe.get('cluster_cache_slots')}")
        if cluster_read_probe.get("storage_trace") is not None:
            lines.append(f"- storage_trace={cluster_read_probe.get('storage_trace')}")
        if cluster_read_probe.get("error"):
            lines.append(f"- Error: {cluster_read_probe.get('error')}")
    touch_move_probe = summary.get("touch_move_sadc_probe")
    if isinstance(touch_move_probe, dict):
        lines += ["", "## Touch Move SADC Probe", ""]
        lines.append(f"- Result: {'PASS' if touch_move_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- down_status={touch_move_probe.get('down_status')} "
            f"move_status={touch_move_probe.get('move_status')} "
            f"release_status={touch_move_probe.get('release_status')}"
        )
        lines.append(
            f"- down_remaining={touch_move_probe.get('down_remaining')} "
            f"move_remaining={touch_move_probe.get('move_remaining')} "
            f"release_remaining={touch_move_probe.get('release_remaining')}"
        )
        lines.append(
            f"- down_raw={touch_move_probe.get('down_raw')} "
            f"move_raw={touch_move_probe.get('move_raw')}"
        )
        if touch_move_probe.get("error"):
            lines.append(f"- Error: {touch_move_probe.get('error')}")
    key_gpio_probe = summary.get("key_gpio_probe")
    if isinstance(key_gpio_probe, dict):
        lines += ["", "## Key GPIO/INTC Probe", ""]
        lines.append(f"- Result: {'PASS' if key_gpio_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- key_code={key_gpio_probe.get('key_code')} "
            f"gpio_mask={key_gpio_probe.get('gpio_mask')} "
            f"irq_mask={key_gpio_probe.get('irq_mask')}"
        )
        lines.append(
            f"- down active_low={key_gpio_probe.get('down_level_active_low')} "
            f"flag={key_gpio_probe.get('down_flag_set')} "
            f"pending={key_gpio_probe.get('down_pending_set')} "
            f"ack_flag={key_gpio_probe.get('down_ack_cleared_flag')} "
            f"ack_pending={key_gpio_probe.get('down_ack_cleared_pending')}"
        )
        lines.append(
            f"- release level_restored={key_gpio_probe.get('release_level_restored')} "
            f"flag={key_gpio_probe.get('release_flag_set')} "
            f"pending={key_gpio_probe.get('release_pending_set')} "
            f"ack_flag={key_gpio_probe.get('release_ack_cleared_flag')} "
            f"ack_pending={key_gpio_probe.get('release_ack_cleared_pending')}"
        )
        lines.append(f"- down_words={key_gpio_probe.get('down_words')}")
        lines.append(f"- release_words={key_gpio_probe.get('release_words')}")
        if key_gpio_probe.get("error"):
            lines.append(f"- Error: {key_gpio_probe.get('error')}")
    alarm_probe = summary.get("alarm_ui_probe")
    if isinstance(alarm_probe, dict):
        lines += ["", "## Alarm UI Probe", ""]
        lines.append(f"- Result: {'PASS' if alarm_probe.get('ok') else 'FAIL'}")
        lines.append(
            f"- hit_flag_setter={alarm_probe.get('hit_alarm_flag_setter')} "
            f"hit_ui_entry={alarm_probe.get('hit_alarm_ui_entry')}"
        )
        events = alarm_probe.get("events")
        if isinstance(events, list):
            for index, event in enumerate(events[:16]):
                if not isinstance(event, dict):
                    continue
                registers = event.get("registers")
                reg_text = ""
                if isinstance(registers, dict):
                    reg_text = (
                        f" v0={registers.get('v0')} v1={registers.get('v1')} "
                        f"ra={registers.get('ra')}"
                    )
                lines.append(f"- event {index}: pc={event.get('pc')}{reg_text}")
                lines.append(
                    f"  - alarm_flag={event.get('alarm_flag_80477d4c')} "
                    f"alarm_record={event.get('alarm_record_80477d54')} "
                    f"alarm_state={event.get('alarm_state_80477d60')}"
                )
                lines.append(
                    f"  - rtc_status={event.get('rtc_status_b0003000')} "
                    f"rtc_reset_status={event.get('rtc_reset_status_b0003030')}"
                )
                record_words = event.get("alarm_record_words_00_2c")
                if record_words not in (None, "not-guest-ram"):
                    lines.append(f"  - alarm_record_words={record_words}")
        progress_trace = alarm_probe.get("progress_trace")
        if isinstance(progress_trace, dict):
            lines.append(
                f"- progress_trace seq={progress_trace.get('seq')} "
                f"slot={progress_trace.get('slot')}"
            )
            recent_entries = progress_trace.get("recent_entries")
            if isinstance(recent_entries, list):
                for entry in recent_entries[-8:]:
                    if not isinstance(entry, dict):
                        continue
                    lines.append(
                        f"  - seq={entry.get('seq')} reason={entry.get('reason')} "
                        f"pc={entry.get('pc')} class={entry.get('classification')} "
                        f"pending={entry.get('intc_pending')} "
                        f"mask={entry.get('intc_mask')} "
                        f"res={entry.get('resource_flags_804bf440')}/"
                        f"{entry.get('resource_refresh_804bf444')}"
                    )
        if alarm_probe.get("progress_trace_error"):
            lines.append(
                f"- progress_trace_error={alarm_probe.get('progress_trace_error')}"
            )
        if alarm_probe.get("error"):
            lines.append(f"- Error: {alarm_probe.get('error')}")
    samples = summary.get("hmp_samples")
    if isinstance(samples, list) and samples:
        lines += ["", "## HMP Register Samples", ""]
        for sample in samples:
            if isinstance(sample, dict) and "pc" in sample:
                classification = classify_guest_pc(sample.get("pc"))
                region = "" if not classification else f" region={classification.get('region')}"
                cp0 = sample.get("cp0")
                cp0_text = ""
                if isinstance(cp0, dict):
                    cp0_text = (
                        f" cp0={cp0.get('exception')}"
                        f" pending={cp0.get('pending_interrupts')}"
                        f" enabled={cp0.get('pending_enabled_interrupts')}"
                        f" epc={cp0.get('epc')}"
                    )
                lines.append(
                    f"- t={sample.get('sample_at_seconds')}s pc={sample.get('pc')} ra={sample.get('ra')} "
                    f"sp={sample.get('sp')}{region}{cp0_text}"
                )
            else:
                lines.append(f"- `{json.dumps(sample, ensure_ascii=False)}`")
    classifications = summary.get("pc_classifications")
    if isinstance(classifications, list) and classifications:
        lines += ["", "## PC Classification", ""]
        for row in classifications:
            if isinstance(row, dict):
                lines.append(f"- {row.get('pc')}: {row.get('region')} - {row.get('description')}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"ok": summary["ok"], "summary": str(summary_path), "report": str(report_path)}, ensure_ascii=False))
    return 0 if summary["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the experimental QEMU system-emulation probe.")
    ap.add_argument("--qemu", default=DEFAULT_QEMU_EXECUTABLE)
    ap.add_argument("--boot-mode", choices=["nand", "c200", "uboot"], default="c200")
    ap.add_argument("--image", type=Path)
    ap.add_argument("--payload", type=Path)
    ap.add_argument(
        "--nand-image",
        type=Path,
        help="Caller-owned writable NAND fixture; omitted by default.",
    )
    ap.add_argument("--payload-addr", type=lambda value: int(value, 0), default=0x4000)
    ap.add_argument("--load-addr", type=lambda value: int(value, 0))
    ap.add_argument("--pc", type=lambda value: int(value, 0))
    ap.add_argument("--machine", default=DEFAULT_QEMU_MACHINE)
    ap.add_argument("--cpu", default="24Kf")
    ap.add_argument("--ram-mb", type=int, default=160)
    ap.add_argument("--accel", default="tcg,thread=multi,tb-size=256")
    ap.add_argument("--display", default="none")
    ap.add_argument("--serial", default="mon:stdio")
    ap.add_argument("--monitor", default="none")
    ap.add_argument("--gdb", default="none")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--hmp-sample", type=float, action="append", default=[0.2, 1.0])
    ap.add_argument("--no-hmp-samples", action="store_true")
    ap.add_argument("--extra-arg", action="append")
    ap.add_argument("--qemu-machine-option", action="append")
    ap.add_argument("--qemu-firmware-patch", action="append", default=None)
    ap.add_argument("--input-event-queue-probe", action="store_true")
    ap.add_argument("--udc-idle-probe", action="store_true")
    ap.add_argument("--storage-layout-probe", action="store_true")
    ap.add_argument("--cache-scan-probe", action="store_true")
    ap.add_argument("--cache-scan-max-events", type=int, default=16)
    ap.add_argument("--cache-scan-caller-probe", action="store_true")
    ap.add_argument("--cache-scan-caller-max-events", type=int, default=32)
    ap.add_argument("--fat-cache-io-probe", action="store_true")
    ap.add_argument("--fat-cache-io-max-events", type=int, default=64)
    ap.add_argument("--fat-cache-io-flush-only", action="store_true")
    ap.add_argument("--msc-dma-write-probe", action="store_true")
    ap.add_argument("--msc-dma-write-lba", type=lambda value: int(value, 0), default=BBK_MSC_DMA_PROBE_LBA)
    ap.add_argument("--uart-register-probe", action="store_true")
    ap.add_argument("--sadc-battery-probe", action="store_true")
    ap.add_argument("--rtc-hibernate-probe", action="store_true")
    ap.add_argument("--rtc-alarm-irq-probe", action="store_true")
    ap.add_argument("--bch-status-probe", action="store_true")
    ap.add_argument("--lcd-frame-done-probe", action="store_true")
    ap.add_argument("--config-cluster-watch-probe", action="store_true")
    ap.add_argument("--config-cluster-watch-timeout", type=float, default=None)
    ap.add_argument(
        "--config-cluster-watch-calibrate-first",
        action="store_true",
        help="Feed cold-boot touchscreen calibration through the QEMU input chardev before watching CONFIG INF cluster writes.",
    )
    ap.add_argument("--file-open-context-probe", action="store_true")
    ap.add_argument("--file-open-context-max-events", type=int, default=48)
    ap.add_argument(
        "--file-open-context-calibrate-first",
        action="store_true",
        help="Feed cold-boot touchscreen calibration through the QEMU input chardev before tracing file/resource open paths.",
    )
    ap.add_argument(
        "--file-open-context-sync-current-cluster",
        action="store_true",
        help="Diagnostic only: when a resource file object is built, mirror cluster_18 into current_cluster_30/aux_cluster_34 to test the dirent-copy dependency.",
    )
    ap.add_argument("--semaphore-flow-probe", action="store_true")
    ap.add_argument("--semaphore-flow-max-events", type=int, default=32)
    ap.add_argument("--semaphore-flow-resource-only", action="store_true")
    ap.add_argument("--semaphore-flow-continue-resource", action="store_true")
    ap.add_argument("--exception-context-probe", action="store_true")
    ap.add_argument("--exception-context-delay", type=float, default=1.0)
    ap.add_argument("--poweroff-path-probe", action="store_true")
    ap.add_argument("--poweroff-path-max-events", type=int, default=32)
    ap.add_argument("--null-resource-release-probe", action="store_true")
    ap.add_argument("--null-resource-release-max-events", type=int, default=16)
    ap.add_argument("--alarm-audio-open-probe", action="store_true")
    ap.add_argument("--alarm-audio-open-max-events", type=int, default=32)
    ap.add_argument("--irq24-handler-probe", action="store_true")
    ap.add_argument("--gui-event-poller-probe", action="store_true")
    ap.add_argument("--gui-touch-ring-probe", action="store_true")
    ap.add_argument("--gui-touch-natural-probe", action="store_true")
    ap.add_argument("--gui-touch-natural-settle", type=float, default=1.0)
    ap.add_argument("--gui-touch-natural-ready-timeout", type=float, default=5.0)
    ap.add_argument("--gui-touch-natural-sample-interval", type=float, default=0.5)
    ap.add_argument("--gui-touch-natural-release", action="store_true")
    ap.add_argument(
        "--gui-touch-natural-calibrate-first",
        action="store_true",
        help="Drive cold-boot touch calibration through the QEMU input chardev before testing natural GUI touch consumption.",
    )
    ap.add_argument("--touch-input-calibration-probe", action="store_true")
    ap.add_argument("--cluster-read-path-probe", action="store_true")
    ap.add_argument("--cluster-read-path-max-events", type=int, default=24)
    ap.add_argument("--touch-move-sadc-probe", action="store_true")
    ap.add_argument("--key-gpio-probe", action="store_true")
    ap.add_argument("--touch-input-calibration-settle", type=float, default=0.5)
    ap.add_argument("--touch-input-calibration-hold", type=float, default=0.5)
    ap.add_argument("--touch-input-calibration-ready-timeout", type=float, default=10.0)
    ap.add_argument("--alarm-ui-probe", action="store_true")
    ap.add_argument("--alarm-ui-max-events", type=int, default=64)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=BUILD)
    ap.add_argument("--prefix", default="qemu_system_probe")
    return run_probe(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
